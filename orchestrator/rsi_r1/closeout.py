"""RSI-R1 T9 离线结案（Plan §11.1/§11.2、AC34–AC36）。

计划原文（§9 T9 行，逐字）::

    T9 结案与R2交接 | closure独立重算，旧根hash保护，成本/人工/监护/容器/证据；
    影子建议只归档 | COMPLETE/INCOMPLETE与R1结论、R2可行性提案不部署；AC34–36

    任何阶段STOP都进入离线T9。

本模块是「离线 T9」的唯一写入口，五条纪律：

1. **离线**：只读盘面（ledger / approvals / reservations / protocol /
   calibration / repair / watchdog / human_events），**不触网、不调用模型、
   不启动容器**。``docker`` 只做一次只读探针（``ps -a``），失败只记录不阻断。
2. **不造 PASS**（AC35）：axes 由盘面事实推出；缺批准件/缺协议报告/部分 batch
   时照常出 readout，但 ``process=CLOSEOUT_INCOMPLETE`` 且 ``reasons`` 逐条列出。
   声明与事实矛盾直接 ``CLOSEOUT_CONTRADICTION``，绝不把缺失写成完成。
3. **自锚**：readout 写 ``document_sha256``（canonical json 去掉自身键），
   机判（``check_rsi_r1_closure.py``）独立重算，不信自述。
4. **旧根保护**（AC36）：carryover 登记的 source sha 必须与旧根现账逐字节相同；
   不等即 ``OLD_ROOT_CHANGED``（旧实验只读，被改写就说明有人动过证据）。
5. **成本口径**（AC34）：逐呼 tokens 与 posts 记 **EXACT**；USD 在账本行里没有
   价格字段，故记 **UNKNOWN** 并以 envelope 的 ``max_usd`` 作 **BOUND**；
   未用尽的批准额度单独列 ``approved_not_used``，不混进「已花」。

R1 结论按 §11.1 的唯一优先级推出：**研究未启动→NOT_MEASURED**；本模块不允许
自动给出更强的结论——更强的结论只能来自研究轨迹与盲审（T7/T8），不是结案脚本。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import human_events as HE
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import repair as RPR

SCHEMA = "simpraxis.rsi_r1.closeout.v1"
SCHEMA_VERSION = 1

READOUT_RELPATH = "closeout/r1_readout.json"
FAILURE_MAP_RELPATH = "closeout/failure_map.json"
R2_READINESS_RELPATH = "closeout/r2_readiness.json"
SHADOW_RELPATH = "closeout/shadow_proposals.jsonl"
DELIVERABLE_RELPATH = "deliverable/closure.json"
PACKET_STATUS_RELPATH = "research_packets/{study}/status.json"

PROTOCOLS = ("probe", "practice")
STUDIES = ("A", "B")

#: §11.1 四轴取值（逐字）。``claim`` 多一个 ``NONE``：研究未启动时没有主张可判，
#: 它不是科学结论，机判只在 ``study_execution=NOT_STARTED`` 时接受。
AXIS_VALUES: Mapping[str, "tuple[str, ...]"] = {
    "process": ("COMPLETE", "CLOSEOUT_INCOMPLETE"),
    "measurement": ("VALID", "INVALID_INSTRUMENT", "CONTAMINATED", "INCOMPLETE"),
    "study_execution": ("NOT_STARTED", "COMPLETED", "VALID_FAIL", "BLOCKED",
                        "ASSISTED"),
    "claim": ("NONE", "SUPPORTED", "REFUTED", "INCONCLUSIVE", "CLAIM_INVALID"),
    "r1": ("NOT_MEASURED", "INCOMPLETE_COVERAGE", "NOT_ESTABLISHED",
           "MINIMAL_RESEARCH_LOOP_ESTABLISHED"),
}

#: 研究已启动的判据（任一存在即算启动，与声明无关）。
STUDY_ARTIFACTS: Mapping[str, "tuple[str, ...]"] = {
    "confirm": ("confirm/manifest_{study}.json",),
    "judge": ("judge/report_{study}.json",),
    "replay": ("replay/comparison_{study}.json",),
}


class CloseoutError(RuntimeError):
    """结案配置/盘面错误（不是科学结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _sha256_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> "dict[str, Any] | None":
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _rows(root: Path) -> "list[dict[str, Any]]":
    path = root / "quota" / "ledger.jsonl"
    if not path.is_file():
        return []
    rows: "list[dict[str, Any]]" = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CloseoutError("LEDGER_UNREADABLE", f"第 {lineno} 行：{exc}") from None
        if not isinstance(row, dict):
            raise CloseoutError("LEDGER_UNREADABLE", f"第 {lineno} 行不是对象")
        rows.append(row)
    return rows


def _carryover(root: Path) -> "dict[str, Any] | None":
    p = root / RPR.CARRYOVER_RELPATH
    return _read_json(p) if p.is_file() else None


def artifact_inventory(root: Path) -> "dict[str, Any]":
    """盘面上「研究是否启动」的唯一事实源（不看任何声明）。

    只清点研究本身的产物：本模块自己写的件（readout/failure_map/packets 等）
    一概不进 inventory，否则清单会随结案自身变化、失去不动点。
    """
    studies: "dict[str, dict[str, bool]]" = {}
    for study in STUDIES:
        studies[study] = {
            key: any((root / rel.format(study=study)).is_file() for rel in rels)
            for key, rels in STUDY_ARTIFACTS.items()
        }
    practice = root / "protocol" / "report_practice.json"
    return {"studies": studies, "practice_report": practice.is_file()}


def studies_started(inventory: Mapping[str, Any]) -> bool:
    """只认 **A/B 的研究产物**。``practice`` 报告是「研究前的协议练习」，它存在
    （尤其练习帽尽 ``NOT_MEASURED``）**不等于**研究已启动——§11.2 明写练习帽尽
    落 NOT_MEASURED；把 practice 当启动会把预注册的退出抬成 INCOMPLETE_COVERAGE。
    """
    for study in STUDIES:
        entry = (inventory.get("studies") or {}).get(study) or {}
        if any(bool(v) for v in entry.values()):
            return True
    return False


# ---------------------------------------------------------------------------
# 盘面事实
# ---------------------------------------------------------------------------

def ledger_facts(rows: "list[dict[str, Any]]") -> "dict[str, Any]":
    calls = [r for r in rows if r.get("event", "call") == "call"]
    events = [r for r in rows if r.get("event", "call") != "call"]
    by_task: "dict[str, int]" = {}
    by_stage: "dict[str, int]" = {}
    tokens = {"in": 0, "out": 0, "cache_create": 0, "cache_read": 0}
    for row in calls:
        task = row.get("task")
        key = str(int(task)) if task is not None else "null"
        by_task[key] = by_task.get(key, 0) + 1
        stage = str(row.get("stage") or "")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        tokens["in"] += int(row.get("tokens_in") or 0)
        tokens["out"] += int(row.get("tokens_out") or 0)
        tokens["cache_create"] += int(row.get("tokens_cache_create") or 0)
        tokens["cache_read"] += int(row.get("tokens_cache_read") or 0)
    seqs = [int(r["seq"]) for r in rows if isinstance(r.get("seq"), int)]
    return {
        "rows": len(rows),
        "calls": len(calls),
        "events": len(events),
        "ok": sum(1 for r in calls if r.get("ok") is True),
        "failed": sum(1 for r in calls if r.get("ok") is False),
        "unknown_outcome": sum(1 for r in calls if r.get("ok") is None),
        "by_task": dict(sorted(by_task.items())),
        "by_stage": dict(sorted(by_stage.items())),
        "tokens": tokens,
        "seq_contiguous": seqs == list(range(1, len(seqs) + 1)),
        "seq_max": max(seqs) if seqs else 0,
        "models": sorted({str(r.get("model")) for r in calls if r.get("model")}),
    }


def batch_facts(root: Path, rows: "list[dict[str, Any]]",
                carryover: "Mapping[str, Any] | None" = None) -> "dict[str, Any]":
    """逐 batch 的成本与批准件核对（缺件即 process 不完整，不是 PASS）。

    §8.1 修复重开时旧账逐字节搬入新账：旧 batch 的批准件留在**旧根**
    ``quota/approvals/``。故先查本根，未命中再查 carryover 登记的 source_root；
    结果记 ``approval_in``，不把「在旧址里齐全」误报成缺件。
    """
    carried_n = int((carryover or {}).get("carried_rows") or 0)
    source_root = (Path(str((carryover or {}).get("source_root") or ""))
                   if carried_n else None)
    batches: "dict[str, dict[str, Any]]" = {}
    # 继承判定按 **call 行**的下标：账本里可能有 ``event != "call"`` 的开账/事件行，
    # 用全表下标会把事件行算进或挤掉 call 行。
    carried_seen = 0
    for row in rows:
        if row.get("event", "call") != "call":
            continue
        bid = str(row.get("batch_id") or "")
        entry = batches.setdefault(bid, {"calls": 0, "ok": 0, "failed": 0,
                                         "carried_calls": 0,
                                         "approval_present": False,
                                         "approval_in": None,
                                         "approved": None,
                                         "approval_path": None,
                                         "task": None})
        entry["calls"] += 1
        if carried_seen < carried_n:
            entry["carried_calls"] += 1
        carried_seen += 1
        if row.get("ok") is True:
            entry["ok"] += 1
        elif row.get("ok") is False:
            entry["failed"] += 1
    for bid, entry in batches.items():
        candidates = [("campaign", root / "quota" / "approvals" / f"{bid}.json")]
        if source_root is not None:
            candidates.append(("carryover_source",
                               source_root / "quota" / "approvals" / f"{bid}.json"))
        for where, path in candidates:
            doc = _read_json(path) if path.is_file() else None
            if doc is None:
                continue
            entry.update({"approval_present": True, "approval_in": where,
                          "approved": doc.get("approved"),
                          "approval_path": str(path),
                          "task": doc.get("task")})
            break
        else:
            entry["approval_path"] = str(candidates[0][1])
    return batches


def protocol_facts(root: Path) -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for name in PROTOCOLS:
        doc = _read_json(root / "protocol" / f"report_{name}.json")
        if doc is None:
            out[name] = None
            continue
        studies = doc.get("studies") or []
        out[name] = {
            "verdict": doc.get("verdict"),
            "reason": doc.get("reason"),
            "billing_lane": doc.get("billing_lane"),
            "attempt": doc.get("attempt"),
            "posts": doc.get("posts"),
            "batch_id": doc.get("batch_id"),
            "root_before": doc.get("root_before"),
            "root_after": doc.get("root_after"),
            "stopped": [s.get("stopped") for s in studies if s.get("stopped")],
            "ok_count": [s.get("ok_count") for s in studies],
            "lock_sha256": doc.get("lock_sha256"),
        }
    return out


def calibration_facts(root: Path) -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for rel in ("contract/calibration_report_v2.json",
                "contract/calibration_report.json"):
        doc = _read_json(root / rel)
        if doc is None:
            continue
        out[Path(rel).stem] = {"verdict": doc.get("verdict"),
                               "checks_total": doc.get("checks_total"),
                               "failures": len(doc.get("failures") or []),
                               "node": rel}
    return out


def watchdog_facts(root: Path) -> "dict[str, Any]":
    state = _read_json(root / "state" / "watchdog.json") or {}
    pause = _read_json(root / "state" / "pause.json") or {}
    events_path = root / "state" / "watchdog_events.jsonl"
    n_events = 0
    if events_path.is_file():
        n_events = sum(1 for line in events_path.read_text(encoding="utf-8").splitlines()
                       if line.strip())
    return {"state": state.get("state"), "ticks": state.get("ticks"),
            "events": n_events,
            "paused": bool(pause.get("paused")),
            "pause_reason": pause.get("reason")}


def human_facts(ctx: Any) -> "dict[str, Any]":
    """人工时长：只认 ``human_events`` 机器计时；盘外裁决单独挂牌。

    AC34 要「全人工分钟」。带起止时间的只有 human_events；评审签名、用户裁决
    这类**盘外**人工动作没有机器计时，不能被计成 0 分钟、也不能被静默忽略，
    故单列 ``out_of_band``：记录谁在何时按了什么，并明说未计时。
    """
    try:
        summary = HE.load(ctx).summary()
    except Exception as exc:  # 人工事件日志损坏不能被静默成 0（AC34）
        raise CloseoutError("HUMAN_EVENTS_UNREADABLE", str(exc)) from None
    n_events = int(summary.get("n_events") or 0)
    decisions: "dict[str, Any]" = {}
    sig_dir = Path(ctx.root) / "contract" / "signatures"
    if sig_dir.is_dir():
        for path in sorted(sig_dir.glob("*.json")):
            doc = _read_json(path) or {}
            decisions[path.stem] = {"role": doc.get("role"),
                                    "actor": doc.get("actor"),
                                    "decision": doc.get("decision"),
                                    "at_utc": doc.get("signed_at_utc")
                                             or doc.get("created_at_utc")}
    repair = _read_json(Path(ctx.root) / RPR.AUTH_RELPATH)
    if repair:
        decisions["repair_authorization"] = {
            "role": "user", "actor": "user",
            "decision": "AUTHORIZED_REPAIR",
            "at_utc": repair.get("created_at_utc"),
            "decision_text_sha256": repair.get("decision_text_sha256")}
    # AC34「未知不填 0」：没有机器计时的事件（或存在未闭合事件）时，分钟数必须是
    # **null** 而不是 0.0——0.0 会被下游自动读成「0 人工分钟」。
    unknown_events = list(summary.get("unknown_event_ids") or [])
    timed = n_events > 0 and not unknown_events
    minutes = (round(float(summary.get("known_seconds_total") or 0.0) / 60.0, 3)
               if timed else None)
    secs = summary.get("known_seconds_total") if timed else None
    return {
        "n_events": summary.get("n_events"),
        "timing": "TIMED" if timed else "UNKNOWN",
        "known_seconds_total": secs,
        "known_minutes_total": minutes,
        "unknown_events": unknown_events,
        "complete": summary.get("complete"),
        "by_kind": summary.get("kinds"),
        "out_of_band": {
            "machine_timed": timed,
            "decisions_on_record": decisions,
            "note": ("human_events 有机器计时；盘外裁决仅按记录列出，不计分钟"
                     if timed else
                     "本轮无完整机器计时（timing=UNKNOWN，known_minutes_total=null）；"
                     "这不是「0 人工分钟」；盘外裁决见 decisions_on_record"),
        },
    }


def old_root_facts(root: Path, carryover: "Mapping[str, Any] | None") -> "dict[str, Any]":
    """AC36：旧根 hash 保护。登记值 vs 现值，任一读取失败都不算「未变」。"""
    if not carryover:
        return {"present": False}
    source = Path(str(carryover.get("source_root") or ""))
    declared = carryover.get("source_ledger_sha256")
    ledger = source / "quota" / "ledger.jsonl"
    observed = None
    if ledger.is_file():
        observed = _sha256_bytes(ledger.read_bytes())
    history = None
    report = source / "protocol" / "report_probe.json"
    if report.is_file():
        doc = _read_json(report) or {}
        history = {"probe_verdict": doc.get("verdict"),
                   "posts": doc.get("posts"),
                   "stopped": [s.get("stopped") for s in (doc.get("studies") or [])
                               if s.get("stopped")]}
    return {
        "present": True,
        "source_root": str(source),
        "ledger_relpath": "quota/ledger.jsonl",
        "declared_source_sha256": declared,
        "observed_sha256": observed,
        "unchanged": bool(observed) and observed == declared,
        "readable": observed is not None,
        "prior_probe_history": history,
    }


def containers_probe(image_pin: "str | None") -> "dict[str, Any]":
    """只读 ``docker ps -a`` 快照；不可用只记录，不阻断离线结案（AC35）。"""
    cmd = ["docker", "ps", "-a", "--format", "{{.Image}}|{{.Status}}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"probe": "docker ps -a", "available": False,
                "error": f"{type(exc).__name__}: {exc}", "own": None}
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    short = ""
    if image_pin and "@sha256:" in image_pin:
        short = image_pin.split("@sha256:", 1)[1][:12]
    available = proc.returncode == 0
    if not short:
        # 没有 digest 就**无法**判断哪些容器是本镜像的：记 UNKNOWN（``own=None``），
        # 不许写 0 —— 0 会被读成「已确认无残留」。
        return {"probe": "docker ps -a", "available": available,
                "total": len(lines), "own": None, "match": "UNKNOWN",
                "own_sample": [], "image_short_sha": "",
                "error": (proc.stderr or "").strip()[:300]}
    own = [ln for ln in lines if short in ln]
    return {"probe": "docker ps -a", "available": available,
            "total": len(lines), "own": len(own), "match": "SHA256_PREFIX",
            "own_sample": own[:5],
            "image_short_sha": short, "error": (proc.stderr or "").strip()[:300]}


# ---------------------------------------------------------------------------
# 结论轴（§11.1 唯一优先级）
# ---------------------------------------------------------------------------

def compute_axes(inventory: Mapping[str, Any]) -> "dict[str, str]":
    """只按盘面事实推轴；**不读**任何自述结论，也不接受「跳级」。

    协议（probe/practice）的结论不参与轴位——§11.2 的退出表说的是「练习帽尽 ⇒
    NOT_MEASURED」，而 NOT_MEASURED 判定的依据正是「研究没启动」，与 protocol
    报告里写了什么无关；报告内容只进 ``protocol``/``failure_map`` 事实面。
    """
    started = studies_started(inventory)
    if not started:
        return {"process": "CLOSEOUT_INCOMPLETE",  # 由 build 覆盖
                "measurement": "INCOMPLETE",
                "study_execution": "NOT_STARTED",
                "claim": "NONE",
                "r1": "NOT_MEASURED"}
    # 研究已启动：结案脚本不自动晋升任何更强结论。判据/复跑/盲审由 T7–T8 与
    # 独立 checker 给出；这里只区分「覆盖不全」与「覆盖齐但未评估」。
    complete = all((inventory.get("studies") or {}).get(s, {}).get("judge")
                   and (inventory.get("studies") or {}).get(s, {}).get("replay")
                   for s in STUDIES)
    if not complete:
        return {"process": "CLOSEOUT_INCOMPLETE",
                "measurement": "INCOMPLETE",
                "study_execution": "BLOCKED",
                "claim": "NONE",
                "r1": "INCOMPLETE_COVERAGE"}
    return {"process": "CLOSEOUT_INCOMPLETE",
            "measurement": "VALID",
            "study_execution": "COMPLETED",
            "claim": "NONE",
            "r1": "INCOMPLETE_COVERAGE"}


# ---------------------------------------------------------------------------
# 结案件
# ---------------------------------------------------------------------------

def _evidence_item(path: Path, rel: str) -> "dict[str, Any]":
    """证据项：文件记 sha256（机判按字节复核），目录只记存在性。"""
    if path.is_file():
        return {"path": rel, "kind": "file",
                "sha256": _sha256_bytes(path.read_bytes())}
    return {"path": rel, "kind": "dir" if path.is_dir() else "missing"}


def _failure_entries(root: Path, protocol: Mapping[str, Any],
                     repair_doc: "Mapping[str, Any] | None",
                     old_root: Mapping[str, Any]) -> "list[dict[str, Any]]":
    out: "list[dict[str, Any]]" = []
    for name, doc in protocol.items():
        if not doc or doc.get("verdict") == "PASS":
            continue
        attempt = doc.get("attempt")
        evidence = [_evidence_item(root / "protocol" / f"report_{name}.json",
                                   f"protocol/report_{name}.json")]
        attempt_rel = f"protocol/{name}/attempt-{attempt}"
        if attempt and (root / attempt_rel).is_dir():
            evidence.append(_evidence_item(root / attempt_rel, attempt_rel))
        out.append({
            "id": f"protocol_{name}",
            "stage": f"protocol/{name}",
            "verdict": doc.get("verdict"),
            "billing_lane": doc.get("billing_lane"),
            "batch_id": doc.get("batch_id"),
            "posts": doc.get("posts"),
            "stopped_codes": doc.get("stopped"),
            "evidence": evidence,
        })
    history = (old_root or {}).get("prior_probe_history")
    if history:
        report = Path(str(old_root.get("source_root"))) / "protocol" / \
            "report_probe.json"
        out.append({
            "id": "protocol_probe_prior_root",
            "stage": "protocol/probe（旧 root，只读留证）",
            "verdict": history.get("probe_verdict"),
            "posts": history.get("posts"),
            "stopped_codes": history.get("stopped"),
            "evidence": [_evidence_item(report, str(report))]
                        if report.is_file() else [],
            "note": "旧 campaign 原样保留；其仪器缺陷证据见 repair 授权的 evidence 项",
        })
    if repair_doc:
        # 授权件的 evidence 是 ``NAME → {path, sha256, bytes}``（旧形态可能是
        # ``NAME → 路径字符串``）。两种形态都收，登记**实际字节**的 sha 供机判复核。
        evidence: "list[dict[str, Any]]" = []
        for name, item in sorted((repair_doc.get("evidence") or {}).items()):
            if isinstance(item, dict):
                raw = str(item.get("path") or "")
                declared = item.get("sha256")
            else:
                raw, declared = str(item), None
            if not raw:
                continue
            path = Path(raw)
            if not path.exists():
                continue
            entry = _evidence_item(path, raw)
            entry["ref"] = name
            if declared:
                entry["declared_sha256"] = declared
                entry["sha_match"] = (entry.get("sha256") == declared
                                      if entry["kind"] == "file" else None)
            evidence.append(entry)
        out.append({
            "id": "repair_authorization",
            "stage": "repair/authorize（§8.1）",
            "decision_text_sha256": repair_doc.get("decision_text_sha256"),
            "document_sha256": repair_doc.get("document_sha256"),
            "prior_root": repair_doc.get("prior_root"),
            "prior_calls": repair_doc.get("prior_calls"),
            "evidence": evidence,
            "note": "一次性授权件；授权本身不是失败，登记在此是为了让失败谱系可追",
        })
    return out


def _process_reasons(ledger: Mapping[str, Any], batches: Mapping[str, Any],
                     old_root: Mapping[str, Any],
                     containers: Mapping[str, Any],
                     *, root: "Path | None" = None,
                     protocol: "Mapping[str, Any] | None" = None) -> "list[str]":
    reasons: "list[str]" = []
    if root is not None and not (root / "quota" / "ledger.jsonl").is_file():
        # 空账本会让 ``ledger_facts`` 的计数全 0、seq 判「连续」——那是真空满足，
        # 不能据此判 COMPLETE（AC35：缺件要出 missing，不是造 PASS）。
        reasons.append("缺账本 quota/ledger.jsonl")
    if protocol is not None and not any(protocol.get(n) for n in PROTOCOLS):
        reasons.append("缺协议报告 protocol/report_{probe,practice}.json（结案核心证据件）")
    if not ledger.get("seq_contiguous"):
        reasons.append("ledger seq 不连续")
    if ledger.get("unknown_outcome"):
        reasons.append(f"{ledger['unknown_outcome']} 呼结果未知（ogen UNKNOWN）")
    for bid, entry in sorted(batches.items()):
        if not entry.get("approval_present"):
            reasons.append(f"batch {bid!r} 缺批准件")
        elif entry.get("approved") is not None and entry["calls"] > int(entry["approved"]):
            reasons.append(f"batch {bid!r} 实呼 {entry['calls']} > 批准 {entry['approved']}")
    if old_root.get("present") and not old_root.get("unchanged"):
        reasons.append("旧 root 账本与 carryover 登记不符（旧实验只读保护失效）")
    if containers.get("available") and containers.get("own"):
        reasons.append(f"仍有 {containers['own']} 个本镜像容器未回收")
    if containers.get("available") and containers.get("own") is None:
        reasons.append("容器回收状态 UNKNOWN（无镜像 digest 可比对）——不算干净")
    return reasons


def _r2_proposal(protocol: Mapping[str, Any]) -> "dict[str, Any]":
    """R2 前置条件**只按协议事实生成**，不写死某一轮的失败原因。

    §11.3：R1 满足后也不是自动执行 R2——本件只登记「要再跑正式研究必须先满足什么」。
    摘要逐字引用报告 ``reason``（盘面事实），前置条件按 ``stopped`` 码给对应的
    可执行约束；未知码只留程序性三条，不臆测原因。
    """
    failed = [(name, doc) for name, doc in protocol.items()
              if isinstance(doc, Mapping) and doc.get("verdict")
              and doc.get("verdict") != "PASS"]
    if not failed:
        summary = ("R1 本轮未进入正式研究（无 PASS 协议报告）；"
                   "R2 不得自动启动（§11.3「R1 满足后不是自动执行 R2」）。")
    else:
        parts: "list[str]" = []
        for name, doc in failed:
            posts = doc.get("posts")
            post_txt = (f"{posts.get('this_attempt')}/{posts.get('allowed')}"
                        if isinstance(posts, Mapping) else str(posts))
            codes = [str(c) for c in (doc.get("stopped") or []) if c]
            detail = (f"{name} 判 {doc.get('verdict')}（lane={doc.get('billing_lane')}，"
                      f"attempt={doc.get('attempt')}，"
                      f"posts={post_txt}")
            if codes:
                detail += f"，stopped={','.join(codes)}"
            if doc.get("reason"):
                detail += f"；{doc['reason']}"
            parts.append(detail + "）")
        summary = ("R1 本轮未进入正式研究：" + "；".join(parts)
                   + "。R2 不得自动启动（§11.3「R1 满足后不是自动执行 R2」）。")
    codes = {str(c) for _, doc in failed for c in (doc.get("stopped") or []) if c}
    prereq: "list[str]" = []
    if "RESOURCE_EXCEEDED" in codes:
        prereq.append("阻断动作为合同资源帽（RESOURCE_EXCEEDED，本轮 run_batch 提出的 "
                      "matrix.cells 超过探索 runs 帽）：要么在候选侧遵守帽值，要么在"
                      "**新轮预注册**中显式放宽——不得回填/改动本轮判读")
    if "SOURCE_SHA_INVALID" in codes:
        prereq.append("阻断动作为引用哈希不一致（SOURCE_SHA_INVALID）：复核 brief 中 "
                      "parent_sha 与 plan_sha256 sentinel 的口径自洽性——修复须在"
                      "**新轮**生效，不得回填本轮")
    prereq.extend([
        "新研究轮须用户显式裁决（§11.2「需要未来新研究轮则另裁决」）",
        "新轮重新冻结 LOCK 与预算 envelope，旧账全部累计不归零",
    ])
    return {"summary": summary, "prerequisites": prereq}


def build(ctx: Any, *, image_pin: "str | None" = None,
          notes: "list[str] | None" = None,
          clock: Callable[[], str] = utc_now_iso_z,
          container_facts: "Mapping[str, Any] | None" = None
          ) -> "dict[str, Any]":
    """写全部 T9 结案件并返回 readout（幂等：重跑覆盖同名文件，不追加）。

    ``container_facts`` 仅供测试注入；生产走 ``containers_probe`` 只读探针。
    """
    root = Path(ctx.root)
    rows = _rows(root)
    inventory = artifact_inventory(root)
    ledger = ledger_facts(rows)
    carryover = _carryover(root)
    batches = batch_facts(root, rows, carryover)
    protocol = protocol_facts(root)
    calibration = calibration_facts(root)
    watchdog = watchdog_facts(root)
    human = human_facts(ctx)
    old_root = old_root_facts(root, carryover)
    repair_doc = None
    if ctx.exists(RPR.AUTH_RELPATH):
        repair_doc = RPR.load_authorization(ctx)
    containers = dict(container_facts) if container_facts is not None \
        else containers_probe(image_pin)
    reasons = _process_reasons(ledger, batches, old_root, containers,
                               root=root, protocol=protocol)
    axes = compute_axes(inventory)
    axes["process"] = "COMPLETE" if not reasons else "CLOSEOUT_INCOMPLETE"

    envelope = _read_json(root / "contract" / "envelope.json") or {}
    budget = _read_json(root / "contract" / "budget.json") or {}
    # 旧根（carryover）批的批准件在冻结旧根里：其剩余额度**不可能**在本轮使用，
    # 单列 ``carryover_batches``，不计入 ``approvals_not_used``（否则读成「还有额度」）。
    approvals_unused = {
        bid: int(entry["approved"]) - entry["calls"]
        for bid, entry in sorted(batches.items())
        if entry.get("approved") is not None
        and entry.get("approval_in") != "carryover_source"
    }
    carryover_batches = {
        bid: {"calls": entry["calls"], "approved": int(entry["approved"]),
              "remaining_unusable": int(entry["approved"]) - entry["calls"]}
        for bid, entry in sorted(batches.items())
        if entry.get("approved") is not None
        and entry.get("approval_in") == "carryover_source"
    }

    entries = _failure_entries(root, protocol, repair_doc, old_root)
    failure_map = {
        "schema": f"{SCHEMA}.failure_map",
        "schema_version": SCHEMA_VERSION,
        "envelope_id": ctx.envelope_id,
        "created_at_utc": clock(),
        "entries": entries,
    }
    ctx.write_json(FAILURE_MAP_RELPATH, failure_map)

    for study in STUDIES:
        started_entry = (inventory.get("studies") or {}).get(study) or {}
        ctx.write_json(PACKET_STATUS_RELPATH.format(study=study), {
            "schema": f"{SCHEMA}.packet_status",
            "schema_version": SCHEMA_VERSION,
            "envelope_id": ctx.envelope_id,
            "study": study,
            "status": "NOT_STARTED" if not any(started_entry.values()) else "STARTED",
            "reason": (("研究未启动（probe/practice 未过门，§9 T6「过门才研究」）；"
                        "本 packet 无轨迹、无主张、无 raw——不得据此推断任何科学结论")
                       if not any(started_entry.values()) else
                       "研究已启动：本 packet 仅登记产物在场情况，结论以 "
                       "confirm/judge/replay 与盲审签名为准"),
            "artifacts": started_entry,
            "created_at_utc": clock(),
        })

    # 交付件与 T7/T8 的 ``closure.write_deliverable`` **同 schema 同路径**：
    # 研究未启动时照写 BLOCKED（closure.py 对「产物缺失」的既定词义），
    # 研究已启动则不覆盖（那两份声明归判据/复跑链所有）。
    deliverable_path = root / DELIVERABLE_RELPATH
    if not (studies_started(inventory) and deliverable_path.is_file()):
        lock_sha = None
        lock_sha_path = root / "contract" / "lock.sha256"
        if lock_sha_path.is_file():
            lock_sha = lock_sha_path.read_text(encoding="utf-8").strip() or None

        def _declared(study: str) -> "dict[str, Any]":
            entry = (inventory.get("studies") or {}).get(study) or {}
            status = ("NOT_STARTED" if not any(entry.values())
                      else "STARTED_WITHOUT_DELIVERABLE")
            return {"claim": "BLOCKED",  # closure.py 对「产物缺失」的既定词义
                    "judge_ok": False, "replay_ok": False,
                    "judge_report": None,
                    "replay": {"ok": False,
                               "procedural_note": "未产生复跑比较件"},
                    "study_status": status}

        deliverable = {
            "schema": "simpraxis.rsi_r1.deliverable.v1",
            "schema_version": 1,
            "envelope_id": ctx.envelope_id,
            "created_at_utc": clock(),
            "lock_sha256": lock_sha,
            "studies": {s: _declared(s) for s in STUDIES},
            "human_time": human,
            "cost": {"posts": ledger["calls"], "tokens": ledger["tokens"],
                     "usd": None},
            "r1": axes["r1"],
            "note": ("T9 离线结案代写：研究未启动/未产出交付件时声明 BLOCKED，"
                     "不声明任何完成态；研究产物齐备时本件不覆盖既有交付件"),
        }
        ctx.write_json(DELIVERABLE_RELPATH, deliverable)

    # 影子建议：候选未进入正式 study，故为空（§11.3「可为空」）。
    ctx.write_text(SHADOW_RELPATH, "")

    r2_readiness = {
        "schema": f"{SCHEMA}.r2_readiness",
        "schema_version": SCHEMA_VERSION,
        "envelope_id": ctx.envelope_id,
        "created_at_utc": clock(),
        "deployed": False,
        "r1_axes": dict(axes),
        "proposal": _r2_proposal(protocol),
        "blocked_by": [e.get("id") for e in entries
                       if e.get("verdict") not in (None, "PASS")],
    }
    ctx.write_json(R2_READINESS_RELPATH, r2_readiness)

    body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "envelope_id": ctx.envelope_id,
        "campaign_root": str(root),
        "created_at_utc": clock(),
        "axes": dict(axes),
        "process_reasons": reasons,
        "inventory": inventory,
        "protocol": protocol,
        "calibration": calibration,
        "ledger": ledger,
        "batches": batches,
        "approvals_not_used": approvals_unused,
        "carryover_batches": carryover_batches,
        "cost": {
            "classification": {"posts": "EXACT", "tokens": "EXACT", "usd": "UNKNOWN"},
            "posts": {"used": ledger["calls"],
                      "cap": LANES.RSI_R1_HARD_CAP,
                      "normal": LANES.RSI_R1_NORMAL_BUDGET},
            "tokens": ledger["tokens"],
            "usd": {"value": None,
                    "bound_max": budget.get("max_usd"),
                    "reason": "账本行只记 tokens，不含价格字段；USD 记 UNKNOWN，"
                              "上限取 envelope 的 max_usd（BOUND）"},
        },
        "budget": {"hard_cap": LANES.RSI_R1_HARD_CAP,
                   "normal": LANES.RSI_R1_NORMAL_BUDGET,
                   "lane_caps": dict(LANES.LANE_POST_CAPS),
                   "declared": budget.get("posts_by_lane")},
        "human": human,
        "watchdog": watchdog,
        "containers": containers,
        "old_root": old_root,
        "repair": ({"authorization_relpath": RPR.AUTH_RELPATH,
                    "document_sha256": repair_doc.get("document_sha256"),
                    "decision_text_sha256": repair_doc.get("decision_text_sha256"),
                    "prior_root": repair_doc.get("prior_root"),
                    "prior_calls": repair_doc.get("prior_calls"),
                    "carryover_relpath": RPR.CARRYOVER_RELPATH,
                    "carryover": carryover}
                   if repair_doc else {"authorization_relpath": None}),
        "envelope_consent": {"consent_sha256": envelope.get("consent_sha256"),
                             "consent_document_sha256":
                                 envelope.get("consent_document_sha256"),
                             "cost_decision": envelope.get("cost_decision")},
        "failure_map_relpath": FAILURE_MAP_RELPATH,
        "r2_readiness_relpath": R2_READINESS_RELPATH,
        "packet_status_relpaths": [PACKET_STATUS_RELPATH.format(study=s)
                                   for s in STUDIES],
        "deliverable_relpath": DELIVERABLE_RELPATH,
        "shadow_proposals_relpath": SHADOW_RELPATH,
        "notes": list(notes or []),
    }
    body["document_sha256"] = RPR.canonical_sha(body)
    ctx.write_json(READOUT_RELPATH, body)
    return body


__all__ = ["AXIS_VALUES", "CloseoutError", "READOUT_RELPATH", "SCHEMA",
           "artifact_inventory", "build", "compute_axes", "containers_probe",
           "ledger_facts", "old_root_facts", "studies_started"]
