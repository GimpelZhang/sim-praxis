"""RSI-R1.2 结案（T9）：五轴 + 失败链 + 族/账对账（零 POST，离线）。

纪律（Plan §6 T9 行 / §8 退出表）：

- **PASS 只指过程闭合**；研究结论另轴（``claim``/``r1``），不因过程完整而晋升。
- 轴位只按**盘面事实**推：账本行、批准件、protocol 报告、族文档、研究产物清点。
  任何自述结论都不参与轴位。
- 类失败**实测可复算**：``instrument_facts`` 逐字节比对「候选分析源码 sha」
  与宿主 prompt 里的 ``protocol.ANALYSIS_TEMPLATE`` sha，并读回执
  ``reason_codes``；两者同时命中才把 measurement 判 ``INVALID_INSTRUMENT``。
  判定面不传 EURheuristic——同一个事实换个方向写就会落到别的轴位。
- 族账口径：``历史 31 + 本轮实呼 n ≤ 155``；本轮实呼以**账本 call 行**为准，
  族文档 ``posts_used`` 与之不符即 process 不完整（不是 PASS）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import closeout as CO
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as ST

SCHEMA = "simpraxis.rsi_r12.closeout.v1"
SCHEMA_VERSION = 1

READOUT_RELPATH = "closeout/r12_readout.json"
FAILURE_MAP_RELPATH = "closeout/r12_failure_map.json"

#: 证据包只收 R1.2 盘面（不把旧 R1 的 confirm/judge/replay 空目录塞进包）。
PACK_PREFIXES: "tuple[str, ...]" = (
    "contract", "state", "protocol", "attempts", "quota", "human_events",
    "validation", "closeout",
)

#: 轴位合法值（与 R1 同一套；R1.2 不新增轴，避免两套口径并存）。
AXIS_VALUES = CO.AXIS_VALUES

CLASS_INSTRUMENT_TEMPLATE = "INSTRUMENT_DEFECT_TEMPLATE_GATE_CONTRADICTION"
CLASS_MODEL_FAILURE = "MODEL_OR_OTHER_FAILURE"
#: 宿主 prompt 把候选指向合同**自身**的 schema 键（`analysis_output.schema`），
#: 该键值是合同文档 schema（`analysis_output_schema.v1`），而门要求输出 schema
#: （`analysis_output.v1`）——照做必被门拒。属于仪器自相矛盾（2026-11-13 实测）。
CLASS_INSTRUMENT_MISDIRECT = "INSTRUMENT_DEFECT_CONTRACT_KEY_MISDIRECTION"
PROMPT_MISDIRECT_POINTER = "analysis_output.schema"
#: 候选侧**合同违背**码（不是仪器缺陷，也不是传输未知）：仪器正常工作了，
#: 只是候选没按公开合同动作 ⇒ measurement 仍 VALID，研究 NOT_STARTED。
CLASS_PROTOCOL_VIOLATION = "MODEL_PROTOCOL_VIOLATION"
#: 沙盒容量缺陷（2026-11-13 轮 D 实测）：宿主的公开分析模板在真实确认批数据上
#: 峰值内存 > 沙盒 MEMORY_LIMIT，容器被 OOM 杀（exit 137）——离线演练的合成
#: 数据量小，照不出这个容量墙。
CLASS_INSTRUMENT_CAPACITY = "INSTRUMENT_DEFECT_SANDBOX_CAPACITY"
#: 指标锚点矛盾（2026-11-13 轮 G 实测）：prompt 模板按 qpos[0] 算位移，
#: judge 按 meta.initial_x_m 算；两者逐 run 差 ~2e-4，配对后均值差 1.4e-6，
#: 恒超 3e-8 容差。候选照抄模板必被判 INVALID_NUMERICS。
CLASS_INSTRUMENT_METRIC_ANCHOR = "INSTRUMENT_DEFECT_METRIC_ANCHOR"
CONTRACT_VIOLATION_CODES = frozenset({
    "UNKNOWN_KEY", "MISSING_KEY", "WRONG_TYPE", "SCHEMA_MISMATCH",
    "INVALID_VALUE", "PLAN_INVALID", "ACTION_UNKNOWN"})
CLASS_NOT_APPLICABLE = "NOT_APPLICABLE"

#: 历史模板 sha（`r12-paid-20261113a` 那轮的旧示例）。当前模板在 D-1 修复后
#: 与门单一源，不再写旧 schema；旧轮的仪器判定仍按当时字节可复算。
#: 模板本身写什么 schema 由源码文本判定（``source_writes_gate_schema``），
#: 不靠 sha 列表猜——sha 列表只回答「这是不是宿主历史上公开过的模板」。
TEMPLATE_SHA256_HISTORY = (
    "daf30754f8eb9b0e6b468d6d714c9579b73a2b6afbf668b9b7edafcc3e7d4f63",
    "afcaa68ffdd051560968f751f6d108b97927e5beb1fca76b6bb195f3242fe9a5",
    "9d2b8f5e2866dfe020eab5b2907790148184b0841e818419e209135afb378592")


class Closeout12Error(RuntimeError):
    """结案配置/盘面错误（不是科学结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _read_json(path: Path) -> "dict[str, Any] | None":
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 仪器事实：失败是否可归因为「宿主模板 ↔ 门」自相矛盾
# ---------------------------------------------------------------------------

def instrument_facts(root: Path, *, lane: str = "probe") -> "dict[str, Any]":
    """复算 ``AC46`` 的仪器失败判定面（不看任何自述分类）。

    判据（三条同时成立才判仪器缺陷）：
    1. protocol 报告 verdict != PASS 且首个错误码是分析门拒绝类；
    2. 失败动作是 ``analyze``；
    3. 候选 analysis 源码 sha 与宿主 prompt 模板
       ``protocol.ANALYSIS_TEMPLATE`` 的 sha **逐字节相等**，且回执
       ``reason_codes`` 命中 ``OUTPUT_SCHEMA_MISMATCH``。
    """
    root = Path(root)
    report = _read_json(root / "protocol" / f"report_{lane}.json")
    if report is None:
        return {"available": False, "classification": CLASS_NOT_APPLICABLE}
    verdict = str(report.get("verdict") or "")
    qual = report.get("qualification") or {}
    first_error = qual.get("first_error") or {}
    code = str(first_error.get("code") or "")
    name = str(first_error.get("name") or "")
    attempt_rel = str(report.get("attempt_relpath") or "")
    facts: "dict[str, Any]" = {
        "available": True,
        "lane": lane,
        "verdict": verdict,
        "first_error_code": code or None,
        "first_error_name": name or None,
        "milestones_ok": qual.get("first_attempt_ok_count"),
        "attempted_positions": qual.get("attempted_positions"),
        "attempt_relpath": attempt_rel or None,
        "template_sha256": hashlib.sha256(
            PT.ANALYSIS_TEMPLATE.encode("utf-8")).hexdigest(),
        "gate_schema": AR.OUTPUT_SCHEMA,
        "classification": CLASS_NOT_APPLICABLE,
    }
    if verdict == PT.VERDICT_PASS or not attempt_rel:
        return facts
    attempt = root / attempt_rel
    confirm_receipt = _read_json(
        attempt / "private" / "receipts" / "A-confirm.json")
    if confirm_receipt is not None:
        facts["_confirm_receipt"] = {
            "reason_codes": list(confirm_receipt.get("reason_codes") or []),
            "exit_code": confirm_receipt.get("exit_code"),
        }
    receipt = _read_json(attempt / "private" / "receipts" / "A-explore.json")
    if receipt is None:
        # 没走到分析门：按首个拒绝码定性（合同违背 vs 其他）。
        if code in CONTRACT_VIOLATION_CODES or code == "BUDGET_UNREACHABLE":
            facts["classification"] = CLASS_PROTOCOL_VIOLATION
        elif code or name:
            facts["classification"] = CLASS_MODEL_FAILURE
        return facts
    reasons = [str(c) for c in (receipt.get("reason_codes") or [])]
    source_sha = str(receipt.get("source_sha256") or "")
    source_path = attempt / "designs" / f"{source_sha}.py"
    source_ok = source_path.is_file()
    facts.update({
        "analysis_source_sha256": source_sha or None,
        "analysis_reason_codes": reasons,
        "analysis_receipt_relpath": f"{attempt_rel}/private/receipts/A-explore.json",
        "source_file_present": source_ok,
        "source_sha_equals_template": bool(
            source_ok and source_sha == facts["template_sha256"]),
        "source_is_known_host_template": bool(
            source_ok and source_sha in TEMPLATE_SHA256_HISTORY),
    })
    if source_ok:
        text = source_path.read_text(encoding="utf-8", errors="replace")
        facts["source_writes_gate_schema"] = AR.OUTPUT_SCHEMA in text
        facts["source_reads_contract_key"] = (
            "analysis_output" in text and "schema" in text)
    # 合同键误导面：prompt 指 `analysis_output.schema`，而该键是合同文档自身
    # schema；候选按指示读取即写出错误 schema。
    contract = _read_json(root / "public" / "public_contract.json") or {}
    declared = ((contract.get("analysis_output") or {}).get("schema"))
    facts["contract_analysis_output_schema"] = declared
    facts["prompt_points_at_contract_schema"] = (
        PROMPT_MISDIRECT_POINTER in PT._SOURCE_TEMPLATES_BLOCK)
    if (name == "analyze"
            and "OUTPUT_SCHEMA_MISMATCH" in reasons
            and facts["source_is_known_host_template"]
            and not facts.get("source_writes_gate_schema")):
        facts["classification"] = CLASS_INSTRUMENT_TEMPLATE
    elif (name == "analyze"
          and "OUTPUT_SCHEMA_MISMATCH" in reasons
          and declared is not None
          and declared != AR.OUTPUT_SCHEMA
          and facts.get("source_reads_contract_key")):
        facts["classification"] = CLASS_INSTRUMENT_MISDIRECT
    elif code in CONTRACT_VIOLATION_CODES or code == "BUDGET_UNREACHABLE":
        facts["classification"] = CLASS_PROTOCOL_VIOLATION
    elif code or name:
        facts["classification"] = CLASS_MODEL_FAILURE
    return facts


# ---------------------------------------------------------------------------
# 族账对账（历史 31 + 已关闭轮实花 + 本轮实呼 ≤ 族帽 160）
# ---------------------------------------------------------------------------

def _peak_rss_kb(image_dir: Path, out_dir: Path) -> "tuple[int | None, str]":
    """用宿主公开模板在给定视图上实测峰值 RSS（KB）；失败返回 (None, 错误)。"""
    import resource
    import subprocess
    import sys
    import tempfile

    script = Path(tempfile.mkdtemp()) / "analysis.py"
    script.write_text(PT.ANALYSIS_TEMPLATE, encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([sys.executable, str(script), str(image_dir), str(out_dir)],
                       capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return int(usage.ru_maxrss), ""


def capacity_facts(attempt: Path, *, memory_limit: str) -> "dict[str, Any]":
    """实测宿主模板在**本轮确认批真实数据**上的峰值内存 vs 沙盒上限。"""
    import tempfile

    confirm_dirs = sorted((attempt / "observations").glob("confirm-*"))
    if not confirm_dirs:
        return {"available": False, "note": "无确认批观测目录"}
    data = confirm_dirs[0]
    plan = sorted((attempt / "public").glob("plan-*.json"))
    work = Path(tempfile.mkdtemp())
    view = work / "view"
    view.mkdir()
    total_bytes = 0
    for path in sorted(data.iterdir()):
        if path.is_file():
            (view / path.name).write_bytes(path.read_bytes())
            total_bytes += path.stat().st_size
    if plan:
        (view / plan[0].name).write_bytes(plan[0].read_bytes())
    peak, error = _peak_rss_kb(view, work / "out")
    limit_kb = None
    raw = str(memory_limit).strip().lower()
    try:
        if raw.endswith("g"):
            limit_kb = int(float(raw[:-1]) * 1024 * 1024)
        elif raw.endswith("m"):
            limit_kb = int(float(raw[:-1]) * 1024)
        elif raw.endswith("k"):
            limit_kb = int(float(raw[:-1]))
        else:
            limit_kb = int(float(raw))  # docker 默认单位是字节
    except ValueError:
        limit_kb = None
    return {
        "available": peak is not None,
        "confirm_data_bytes": total_bytes,
        "peak_rss_kb": peak,
        "memory_limit": memory_limit,
        "memory_limit_kb": limit_kb,
        "exceeds_limit": bool(peak and limit_kb and peak > limit_kb),
        "error": error,
    }


def _study_a_anchor_facts(root: Path, study: str = "A") -> "dict[str, Any]":
    """用确认批 raw 复算两种锚点的 primary 均值，与候选输出逐值对照。

    纯数据复算（不跑 bootstrap）：均值对锚点线性，足以判别候选用了哪个锚点。
    """
    plans = []
    for path in sorted((root / "public").glob("plan-*.json")):
        doc = _read_json(path) or {}
        if doc.get("phase") == "confirm" and doc.get("study_id") == study:
            plans.append(doc)
    confirm = sorted((root / "observations").glob(f"confirm-{study}-*"))
    raw_path = root / "private" / "analysis" / f"{study}-confirm.raw.json"
    if not plans or not confirm or not raw_path.is_file():
        return {"available": False}
    primary = (plans[0].get("primary") or {})
    conds = list(primary.get("condition_ids") or [])
    if len(conds) != 2:
        return {"available": False}
    by_block: "dict[str, dict[str, list[float]]]" = {}
    for meta in sorted(confirm[0].glob("*.meta.json")):
        rid = meta.name[: -len(".meta.json")]
        doc = _read_json(meta) or {}
        raw = confirm[0] / f"{rid}.jsonl"
        try:
            rec = json.loads(raw.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError):
            return {"available": False}
        qpos = rec.get("qpos") or []
        if not qpos:
            return {"available": False}
        x_last = float(qpos[-1][0])
        initial = float(doc.get("initial_x_m", qpos[0][0]))
        x0 = float(qpos[0][0])
        slot = by_block.setdefault(str(rec.get("block_id")), {})
        slot.setdefault(str(doc.get("condition_id")), {"initial": x_last - initial,
                                                       "qpos0": x_last - x0})
    t_cond, c_cond = conds
    d_init, d_q0 = [], []
    for block, pair in sorted(by_block.items()):
        if t_cond in pair and c_cond in pair:
            d_init.append(pair[t_cond]["initial"] - pair[c_cond]["initial"])
            d_q0.append(pair[t_cond]["qpos0"] - pair[c_cond]["qpos0"])
    if not d_init:
        return {"available": False}
    mean_init = sum(d_init) / len(d_init)
    mean_q0 = sum(d_q0) / len(d_q0)
    out = _read_json(raw_path) or {}
    cand = None
    for item in out.get("contrasts") or []:
        if str(item.get("contrast_id")) == str(primary.get("id")):
            cand = item.get("estimate")
    if cand is None:
        return {"available": False}
    tol = 1e-9
    return {
        "available": True,
        "candidate_estimate": float(cand),
        "mean_initial_anchor": mean_init,
        "mean_qpos0_anchor": mean_q0,
        "matches_qpos0": abs(float(cand) - mean_q0) <= tol,
        "matches_initial": abs(float(cand) - mean_init) <= tol,
        "n_blocks": len(d_init),
    }


def family_facts(family_root: Path, *, ledger_calls: int,
                 envelope_id: str) -> "dict[str, Any]":
    usage = FAM.usage(Path(family_root))
    members = usage.get("members") or []
    member = next((m for m in members if m.get("envelope_id") == envelope_id),
                  None)
    history = int(usage.get("history_posts") or 0)
    used = int((member or {}).get("posts_used") or 0)
    closed_rounds = sum(
        int(m.get("posts_used") or 0) for m in members
        if m.get("state") == "closed" and m.get("envelope_id") != envelope_id)
    total = history + closed_rounds + ledger_calls
    return {
        "family_cap": int(usage.get("family_cap") or 0),
        "history_posts": history,
        "closed_rounds_posts": closed_rounds,
        "round_calls": int(ledger_calls),
        "family_total_if_counted_now": total,
        "cap_respected": total <= int(usage.get("family_cap") or 0),
        "member": member,
        "member_posts_used": used,
        "member_usage_matches_ledger": used == int(ledger_calls),
        "reserved_total": int(usage.get("reserved_total") or 0),
        "members": members,
    }


# ---------------------------------------------------------------------------
# 冻结后漂移（诚实挂牌，不进矛盾判据）
# ---------------------------------------------------------------------------

def drift_facts(ctx: Any, *, repo_root: Any = None) -> "dict[str, Any]":
    """逐文件重算 LOCK 闭包 vs 当前仓库，登记「冻结后源码已变」。

    T9 的工具代码与付费链同处 ``cli_r12.py``（``entry`` 组）⇒ T9 一落地，
    冻结时的闭包必然失配。这是**时间事实**，不是运行期污染：probe 是在
    ``lock_verified=true`` 下跑的，根已 CLOSED、不可能再有付费调用。故本件
    只进 readout（``post_freeze_drift``）与 notes，不参与 process 矛盾判据。
    """
    lock = _read_json(Path(ctx.root) / "contract" / "lock.json")
    if lock is None:
        return {"available": False}
    base = Path(repo_root) if repo_root is not None \
        else Path(__file__).resolve().parents[2]
    closure = lock.get("closure") or {}
    mismatches = []
    checked = 0
    for gname, group in (closure.get("groups") or {}).items():
        for item in (group.get("files") or []):
            rel = str(item.get("path") or "")
            want = item.get("sha256")
            if not rel or not want:
                continue
            checked += 1
            path = base / rel
            got = _sha(path) if path.is_file() else None
            if got != want:
                mismatches.append({"group": gname, "path": rel,
                                   "locked": want, "current": got})
    return {
        "available": True,
        "repo_root": str(base),
        "lock_closure_sha256": closure.get("closure_sha256"),
        "files_checked": checked,
        "verify": "CLOSURE_DRIFT" if mismatches else "OK",
        "mismatches": mismatches,
        "note": ("冻结后源码变更（T9 工具落在 entry 组）；锁定时 verify rc=0 "
                 "已留档（protocol 报告 lock_verified=true），根已 CLOSED，"
                 "无后续付费调用；下一轮须重新注册并冻结新 LOCK。"),
    }


# ---------------------------------------------------------------------------
# 结案主体
# ---------------------------------------------------------------------------

def build(ctx: Any, *, image_pin: "str | None" = None,
          container_facts: "Mapping[str, Any] | None" = None,
          repo_root: Any = None,
          now: Any = None) -> "dict[str, Any]":
    """写 ``closeout/r12_readout.json`` + 失败链件；幂等（重跑覆盖同名件）。"""
    root = Path(ctx.root)
    rows = CO._rows(root)  # noqa: SLF001 —— 同一结案例程，读法单源
    ledger = CO.ledger_facts(rows)
    batches = CO.batch_facts(root, rows)
    protocol = CO.protocol_facts(root)
    calibration = CO.calibration_facts(root)
    human = CO.human_facts(ctx)
    inventory = CO.artifact_inventory(root)
    started = CO.studies_started(inventory)
    containers = dict(container_facts) if container_facts is not None \
        else CO.containers_probe(image_pin)
    envelope = _read_json(root / "contract" / "envelope.json") or {}
    profile = P.profile_by_id(str(envelope.get("profile_id") or P.PROFILE_R12))

    calls = int(ledger["calls"])
    fam = family_facts(_family_root(), ledger_calls=calls,
                       envelope_id=str(ctx.envelope_id))
    instrument = instrument_facts(root)
    capacity: "dict[str, Any]" = {"available": False}
    if instrument.get("attempt_relpath"):
        capacity = capacity_facts(root / str(instrument["attempt_relpath"]),
                                  memory_limit=SB.MEMORY_LIMIT)
    if (instrument.get("available")
            and instrument.get("first_error_name") == "run_confirm"
            and str((instrument.get("_confirm_receipt") or {}).get("exit_code"))
            == "137"
            and capacity.get("exceeds_limit")):
        instrument = dict(instrument)
        instrument["classification"] = CLASS_INSTRUMENT_CAPACITY
    drift = drift_facts(ctx, repo_root=repo_root)

    axes = dict(CO.compute_axes(inventory))
    root_state = ST.ResearchState(ctx).root_state()
    failures: "list[dict[str, Any]]" = []
    gaps: "list[str]" = []

    instrument_classes = (CLASS_INSTRUMENT_TEMPLATE, CLASS_INSTRUMENT_MISDIRECT,
                          CLASS_INSTRUMENT_CAPACITY)
    if instrument.get("classification") in instrument_classes:
        axes["measurement"] = "INVALID_INSTRUMENT"
        failures.append({
            "task": "T6",
            "lane": instrument.get("lane"),
            "batch_id": (protocol.get(str(instrument.get("lane"))) or {})
            .get("batch_id"),
            "verdict": instrument.get("verdict"),
            "first_error_code": instrument.get("first_error_code"),
            "first_error_name": instrument.get("first_error_name"),
            "milestones_ok": instrument.get("milestones_ok"),
            "classification": instrument.get("classification"),
            "detail": (
                ("沙盒容量缺陷：宿主公开分析模板在真实确认批数据上峰值 RSS "
                 "超过 MEMORY_LIMIT，容器被 OOM 杀（exit 137）")
                if instrument.get("classification") == CLASS_INSTRUMENT_CAPACITY
                else ("prompt 模板 ANALYSIS_TEMPLATE 写 protocol_analysis.v1，"
                      "而 T4 analysis_receipt 门要求 analysis_output.v1；"
                      "候选照抄宿主示例必被拒")
                if instrument.get("classification") == CLASS_INSTRUMENT_TEMPLATE
                else ("prompt 把候选指向合同自身的 schema 键 "
                      "`analysis_output.schema`（值是合同文档 schema "
                      "`analysis_output_schema.v1`），而门要求输出 schema "
                      "`analysis_output.v1`；按宿主指示读取即被门拒")),
            "evidence": {
                "instrument": {k: v for k, v in instrument.items()
                               if not str(k).startswith("_")},
                "capacity": capacity,
            },
        })
        # 仪器缺陷本身是 **measurement 轴结论**，不是「记录互相矛盾」：
        # 若把它计进 gaps 会让 process 永远 INCOMPLETE，违背「PASS 只指过程
        # 闭合、结论另轴」的判据面。它由 failure_chain + measurement 轴承载。
    elif instrument.get("classification") == CLASS_PROTOCOL_VIOLATION:
        # 仪器正常、候选违背合同（或因此无法在帽内闭合）：这是**测量有效的
        # 资格失败**，不是仪器阻断；研究仍未启动。
        axes["measurement"] = "VALID"
        failures.append({
            "task": "T6",
            "lane": instrument.get("lane"),
            "batch_id": (protocol.get(str(instrument.get("lane"))) or {})
            .get("batch_id"),
            "verdict": instrument.get("verdict"),
            "first_error_code": instrument.get("first_error_code"),
            "first_error_name": instrument.get("first_error_name"),
            "milestones_ok": instrument.get("milestones_ok"),
            "classification": CLASS_PROTOCOL_VIOLATION,
            "detail": ("候选动作违背公开合同（首错见 first_error_code）；"
                       "仪器按合同拒绝并可恢复，游标无法在帽内闭合时由 "
                       "BUDGET_UNREACHABLE 守卫停链——资格失败，研究未启动"),
            "evidence": instrument,
        })
    elif instrument.get("available") and instrument.get("verdict") != PT.VERDICT_PASS:
        failures.append({
            "task": "T6",
            "lane": instrument.get("lane"),
            "verdict": instrument.get("verdict"),
            "first_error_code": instrument.get("first_error_code"),
            "classification": instrument.get("classification"),
            "evidence": instrument,
        })

    # 练习 lane（probe PASS 之后才会跑）：非 PASS 必须进失败链，不能被 probe 的
    # PASS 掩盖（轮 E 实测：probe PASS、practice 因模型原生工具标记终止）。
    practice = instrument_facts(root, lane=PT.PRACTICE)
    if practice.get("available") and practice.get("verdict") != PT.VERDICT_PASS:
        failures.append({
            "task": "T6",
            "lane": practice.get("lane"),
            "verdict": practice.get("verdict"),
            "first_error_code": practice.get("first_error_code"),
            "first_error_name": practice.get("first_error_name"),
            "milestones_ok": practice.get("milestones_ok"),
            "classification": practice.get("classification"),
            "detail": ("练习资格失败：首个拒绝见 first_error_code"
                       "（工具/传输未知按 fail-closed 终止，研究未启动）"),
            "evidence": practice,
        })

    # T7 研究与判据事实（轮 F：A 完成但判拒、B 传输未知中断）——研究层的失败
    # 不能只留在 studies/ 报告里，必须在失败链上可见。
    for study in ("A", "B"):
        rep = _read_json(root / "studies" / f"report_{study}.json")
        if rep is None:
            continue
        judge = rep.get("judge") or {}
        ok = (rep.get("status") == "COMPLETED"
              and str(judge.get("verdict")) == "JUDGE_OK")
        if ok:
            continue
        keys = []
        judge_doc = _read_json(root / "judge" / f"report_{study}.json") or {}
        keys = list(judge_doc.get("reason_codes") or [])
        failures.append({
            "task": "T7",
            "study": study,
            "status": rep.get("status"),
            "stopped": rep.get("stopped"),
            "judge_verdict": judge.get("verdict"),
            "reason_codes": keys,
            "posts": rep.get("posts"),
            "classification": ("TRANSPORT_UNKNOWN"
                               if rep.get("stopped") in ("EmptyResponseError",
                                                         "PAUSED")
                               else "SCIENTIFIC_OR_PROTOCOL_FAILURE"),
            "detail": ("研究未产出可判读结果或判据拒绝；失败分母保留，"
                       "不调阈值、不换题"),
        })

    # 指标锚点矛盾判定：A 判 INVALID_NUMERICS 且候选数值与 qpos[0]-锚点逐值相符、
    # 与 initial_x-锚点不符 ⇒ 仪器缺陷（候选照抄了 prompt 模板的锚点）。
    anchor = _study_a_anchor_facts(root)
    if (anchor.get("available") and anchor.get("matches_qpos0")
            and not anchor.get("matches_initial")):
        axes["measurement"] = "INVALID_INSTRUMENT"
        for item in failures:
            if item.get("task") == "T7" and item.get("study") == "A":
                item["classification"] = CLASS_INSTRUMENT_METRIC_ANCHOR
                item["detail"] = ("指标锚点矛盾：prompt 模板用 qpos[0]，judge 用 "
                                  "meta.initial_x_m；候选数值与模板锚点逐值相符")
                item["evidence"] = {"anchor": anchor}

    if not fam["member_usage_matches_ledger"]:
        gaps.append(
            f"族账 posts_used={fam['member_posts_used']} 与账本 call 行 "
            f"{calls} 不符（paid 路径未调 FAM.record_usage；T9 收尾补记）")
    if not fam["cap_respected"]:
        gaps.append(f"历史 {fam['history_posts']} + 已关闭轮 "
                    f"{fam['closed_rounds_posts']} + 本轮 {calls} > 族帽 "
                    f"{fam['family_cap']}")
    if not batch_approvals_ok(batches):
        gaps.append("批次批准件缺失或批准数 < 实呼数")
    if not ledger["seq_contiguous"]:
        gaps.append("账本 seq 不连续")
    if ledger["unknown_outcome"]:
        gaps.append(f"账本有 {ledger['unknown_outcome']} 行 outcome=UNKNOWN")
    if any(row.get("cost_usd") in (None, 0) for row in rows):
        gaps.append("账本无 cost_usd（供应商无单价硬保证；费用按 token 记账）")
    attempt_reports = sorted(
        str(p.relative_to(root)) for p in (root / "attempts").rglob("*.json"))
    if not attempt_reports:
        gaps.append("attempts/ 无 T3 attempt 报告（probe 走 CLI 直调，未包 attempt 包装器）")
    if int(human.get("n_events") or 0) == 0:
        gaps.append("人工事件 0 条（T6 全自动，无人参与；人时轴 UNKNOWN，不填 0）")

    # 过程轴只认「记录之间互相矛盾」的那一类：缺批准件/seq 断/结果未知/族账与
    # 账本不符。有意的限制（无 cost_usd、无 attempt 包装、人工 0 条）单列 gaps，
    # 如实挂牌但不冒充「矛盾」。
    process_reasons = [g for g in gaps if not g.startswith(
        ("账本无 cost_usd", "attempts/ 无 T3 attempt", "人工事件 0 条"))]
    axes["process"] = "COMPLETE" if not process_reasons else "CLOSEOUT_INCOMPLETE"

    for name, value in axes.items():
        if value not in AXIS_VALUES[name]:
            raise Closeout12Error("AXIS_ILLEGAL", f"{name}={value!r}")

    evidence = {}
    rels = ["contract/envelope.json", "contract/budget.json",
            "contract/consent.json", "contract/lock.json",
            "contract/calibration_report_v2.json",
            "protocol/report_probe.json", "quota/ledger.jsonl"]
    if instrument.get("analysis_receipt_relpath"):
        rels.append(str(instrument["analysis_receipt_relpath"]))
    if instrument.get("analysis_source_sha256"):
        rels.append(f"{instrument['attempt_relpath']}/designs/"
                    f"{instrument['analysis_source_sha256']}.py")
    for lane in PT.PROTOCOL_LANES:
        bid = (protocol.get(lane) or {}).get("batch_id")
        if bid:
            rels.append(f"quota/approvals/{bid}.json")
    for rel in rels:
        path = root / rel
        if path.is_file():
            evidence[rel] = {"sha256": _sha(path), "bytes": path.stat().st_size}

    body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "campaign_root": str(root),
        "envelope_id": str(ctx.envelope_id),
        "profile_id": profile.profile_id,
        "root_state": root_state,
        "created_at_utc": (now or utc_now_iso_z)(),
        "axes": axes,
        "studies_started": started,
        "inventory": inventory,
        "ledger": ledger,
        "batches": batches,
        "protocol": protocol,
        "calibration": calibration,
        "human": human,
        "containers": containers,
        "family": fam,
        "instrument": instrument,
        "post_freeze_drift": drift,
        "failure_chain": failures,
        "gaps": gaps,
        "totals": {
            "history_posts": fam["history_posts"],
            "closed_rounds_posts": fam["closed_rounds_posts"],
            "round_calls": calls,
            "family_total": (fam["history_posts"] + fam["closed_rounds_posts"]
                             + calls),
            "family_cap": fam["family_cap"],
            "lane_caps": dict(profile.lane_post_caps),
            "by_lane_posts": {
                lane: int((protocol.get(lane) or {}).get("posts", {})
                          .get("this_attempt") or 0)
                for lane in PT.PROTOCOL_LANES
            },
            "ok": ledger["ok"],
            "failed": ledger["failed"],
            "unknown_outcome": ledger["unknown_outcome"],
            "tokens": ledger["tokens"],
            "cost_usd": "UNKNOWN（供应商无单价硬保证；按 token + 批准件记账）",
        },
        "evidence": evidence,
        "readout_relpath": READOUT_RELPATH,
        "failure_map_relpath": FAILURE_MAP_RELPATH,
        "notes": [
            "PASS 只指过程闭合；研究结论在 claim/r1 轴，不因过程完整而晋升。",
            "A/B 未启动：T6 资格检查未过（仪器缺陷），Plan L71/AC45/AC46 禁 A/B。",
            "本结案不修代码、不重测、不重开新根（Plan L228/§8）。",
            "T6 仪器缺陷：候选 analysis 源码与宿主 prompt 模板逐字节相同，"
            "模板写 protocol_analysis.v1 而 T4 门要求 analysis_output.v1"
            "（下一轮修模板 + 加「模板→门」一致性测试）。",
            ("冻结后漂移：" + "；".join(
                f"{m['path']} 已变" for m in drift.get("mismatches") or []))
            if drift.get("mismatches") else "闭包重算与 LOCK 一致（无冻结后漂移）。",
        ],
    }
    ctx.write_json(READOUT_RELPATH, body)
    ctx.write_json(FAILURE_MAP_RELPATH,
                   {"schema": "simpraxis.rsi_r12.failure_map.v1",
                    "schema_version": 1, "entries": failures,
                    "gaps": gaps})
    return body


def batch_approvals_ok(batches: Mapping[str, Any]) -> bool:
    for bid, entry in batches.items():
        if not entry.get("approval_present"):
            return False
        approved = entry.get("approved")
        if approved is None or int(approved) < int(entry.get("calls") or 0):
            return False
    return True


# ---------------------------------------------------------------------------
# T9 收尾动作：族用量补记 → 关根 → 证据包 → 独立复算
# ---------------------------------------------------------------------------

CHECKER_RELPATH = "scripts/verification/check_rsi_r12_closure.py"
JUDGE_REPORT_RELPATH = "closeout/r12_closure_judge.json"


def close_round(ctx: Any, *, reason: str) -> "dict[str, Any]":
    """T9 收尾：按**账本 call 行**补记族用量，再把根推进 ``CLOSED``。

    ``record_usage`` 幂等且单调（family.py:524 docstring 指定「由 4b/T9 收尾
    调用」）；族账缺失/不可写不阻断结案，但必须原样返回到 readout 的 gaps，
    不能静默成「已记账」。
    """
    calls = int(CO.ledger_facts(CO._rows(Path(ctx.root)))["calls"])
    note = None
    closed_member = False
    try:
        FAM.record_usage(_family_root(), str(ctx.envelope_id), calls)
        # 收尾同时关闭族成员：否则下一轮会被 FAMILY_ROUND_OPEN 挡死。
        # 关闭只归还未用预留，posts_used 继续占族帽（2026-11-13 口径）。
        FAM.close_member(
            _family_root(), str(ctx.envelope_id),
            reason=f"T9 结案：{reason}",
            evidence={"ledger_calls": calls, "readout": READOUT_RELPATH})
        closed_member = True
    except FAM.FamilyError as exc:
        note = f"FAMILY_USAGE_NOT_RECORDED {getattr(exc, 'code', '')}: {exc}"
    ST.ResearchState(ctx).close_root(reason=reason)
    return {"ledger_calls": calls, "family_note": note,
            "family_member_closed": closed_member,
            "root_state": ST.ResearchState(ctx).root_state()}


def build_pack(ctx: Any, *, dest: Any = None,
               repo_root: Any = None) -> "dict[str, Any]":
    """按 R1.2 前缀收证据包，并附带**新版**独立复算器源码一份。"""
    from orchestrator.rsi_r1 import closure as CL
    checker = Path(repo_root) if repo_root is not None \
        else Path(__file__).resolve().parents[2]
    extras = {f"checker/{Path(CHECKER_RELPATH).name}":
              checker / CHECKER_RELPATH}
    return CL.build_evidence_pack(ctx, dest=dest, prefixes=PACK_PREFIXES,
                                  extras=extras, repo_root=repo_root)


def verify_pack(pack: Any) -> "dict[str, Any]":
    from orchestrator.rsi_r1 import closure as CL
    return CL.verify_pack(pack)


def run_judge(ctx: Any, *, repo_root: Any = None, pack: Any = None,
              report: Any = None, timeout_s: float = 300.0) -> "dict[str, Any]":
    """外部进程跑**纯标准库**独立复算器，读回判决件（rc 0/1/2 都返回）。"""
    import subprocess
    import sys
    base = Path(repo_root) if repo_root is not None \
        else Path(__file__).resolve().parents[2]
    checker = base / CHECKER_RELPATH
    if not checker.is_file():
        raise Closeout12Error("CHECKER_MISSING", str(checker))
    dest = Path(report) if report is not None else ctx.relative(
        JUDGE_REPORT_RELPATH)
    argv = [sys.executable, "-I", str(checker), "--root", str(ctx.root),
            "--report", str(dest), "--repo-root", str(base)]
    from orchestrator.rsi_r1 import closure as CL
    default_pack = ctx.relative("evidence_pack")
    if pack is None:
        if (default_pack / CL.PACK_MANIFEST).is_file():
            argv += ["--verify", str(default_pack)]
    elif pack is not False:
        argv += ["--verify", str(Path(pack))]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout_s, check=False)
    try:
        judged = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Closeout12Error(
            "CHECKER_OUTPUT",
            f"rc={proc.returncode} stdout 非 JSON：{exc}；"
            f"stderr={proc.stderr[-400:]}") from None
    if not dest.is_file():
        from orchestrator.common.fsutil import ensure_private_dir
        from orchestrator.common.fsutil import atomic_write_text
        ensure_private_dir(dest.parent)
        atomic_write_text(dest, json.dumps(judged, ensure_ascii=False,
                                           indent=1, sort_keys=True) + "\n",
                          mode=0o600)
    judged["returncode"] = proc.returncode
    judged["ok"] = (proc.returncode == 0 and judged.get("verdict") == "PASS")
    judged["report_path"] = str(dest)
    judged["stderr_tail"] = proc.stderr[-800:]
    return judged


def _family_root() -> Path:
    from orchestrator.common import runtime_paths as RP
    return RP.rsi_r12_root()


__all__ = [
    "AXIS_VALUES", "CHECKER_RELPATH", "CLASS_INSTRUMENT_TEMPLATE",
    "CLASS_MODEL_FAILURE", "CLASS_NOT_APPLICABLE", "Closeout12Error",
    "FAILURE_MAP_RELPATH", "JUDGE_REPORT_RELPATH", "PACK_PREFIXES",
    "READOUT_RELPATH", "SCHEMA", "batch_approvals_ok", "build", "build_pack",
    "close_round", "drift_facts", "family_facts", "instrument_facts",
    "run_judge", "verify_pack",
]
