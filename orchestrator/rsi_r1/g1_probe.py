"""RSI-G1 **B 层**：一次配对交付诊断（6 案例 × 2 接口 × 2 会话 = 24 POST）。

Plan §5.2 的最小实现。硬边界：

* 只在**独立批准件**（``probe/approval.json``，用户逐字裁决）与
  ``SIMPRAXIS_ALLOW_PAID=1`` 双闸下才会构造 sender；A 层路径根本不导入本模块。
* 每个槽**一次请求**（``max_attempts=1``）、无缓存/复用、配置冻结后才发；
  任何重试/重复/补槽/配置漂移 ⇒ ``overall=DIAGNOSTIC_INCONCLUSIVE``。
* 两接口向模型提供**完全相同**的公开数据；唯一差别是提交形式
  （``verbatim`` 要求抄数值，``by_ref`` 只要求引用指针）。
* 24 是完整预注册上限：提前失败不补发，未调用槽保留在分母里。

本模块不做科学主张：机械面（parse/binding/数值保真）与语义面（预登记选择）
分开记；自由文本只作探索性。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.common import research_transport as T
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

SCHEMA_PREREG = "simpraxis.rsi_g1.probe_prereg.v1"
SCHEMA_REPORT = "simpraxis.rsi_g1.probe_report.v1"

INTERFACES = ("verbatim", "by_ref")
REPLICATES = (1, 2)
MAX_POSTS = 24
STAGE = "coding_debug"
#: 输出帽必须覆盖**思考 token**（G1-D1：1024 会让 thinking 型模型在
#: 思考里耗尽额度、finish=length、content 为空；R1.3 实测该 stage 单呼
#: tokens_out 可达 19,002，故按 profile 的 8192 下限起）。
G1_MAX_TOKENS = 8192
ABS_TOL = 1e-8
REL_TOL = 1e-6

SEMANTIC_OPTIONS = ("supported_mean", "refuted", "inconclusive",
                    "supported_at_point", "inconsistent_evidence")
SCOPES = ("population_mean", "all_blocks", "fixed_point", "general_population")

SLOT_FIELDS = ("diagnostic_id", "slot_id", "case_id", "interface", "replicate",
               "fixed_case_seed", "stage_config_sha", "provider_endpoint_class",
               "model_id_snapshot", "ledger_path", "cache_policy",
               "max_attempts", "input_manifest_sha", "expected_output_schema_sha")


class ProbeError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tol(reference: float) -> float:
    return ABS_TOL + REL_TOL * abs(float(reference))


# ---------------------------------------------------------------------------
# 六案例（公开数据 + 预登记语义）
# ---------------------------------------------------------------------------

def _contrast(cid: str, *, estimate: float, lower: float, upper: float,
              status: str = "SUPPORTED", metric: str = "displacement_x_m",
              proposition: str = "increase_gt", delta: str = "0.001"
              ) -> "dict[str, Any]":
    return {"contrast_id": cid, "metric_id": metric,
            "proposition": proposition, "delta": delta,
            "estimate": estimate, "lower": lower, "upper": upper,
            "status": status, "n_blocks": 4}


def _blocks(values: "list[float]") -> "list[dict[str, Any]]":
    return [{"block_id": f"b{i:02d}", "values": {"x": v}}
            for i, v in enumerate(values)]


def build_cases() -> "list[dict[str, Any]]":
    """6 个模拟诊断案例（无物理、无隐藏答案；expected 只在评分端）。"""
    cases: "list[dict[str, Any]]" = []
    cases.append({
        "case_id": "C1", "question": "平均效应是否超过 δ=0.001？",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("mean_effect", estimate=0.05,
                                    lower=0.01, upper=0.09)],
            "block_table": _blocks([0.06, 0.04, 0.0005, 0.09])}},
        "expected": {"contrast_id": "mean_effect", "scope": "population_mean",
                     "semantic": "supported_mean"},
        "note": "均值显著但部分 block 不达界 → 不得声称 all_blocks",
    })
    cases.append({
        "case_id": "C2", "question": "预设正向假设是否被数据反驳？",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("refuted_effect", estimate=-0.2,
                                    lower=-0.3, upper=-0.1, status="REFUTED")],
            "block_table": _blocks([-0.2, -0.19, -0.21, -0.2])}},
        "expected": {"contrast_id": "refuted_effect", "scope": "population_mean",
                     "semantic": "refuted"},
        "note": "合格反驳不是 invalid",
    })
    cases.append({
        "case_id": "C3", "question": "区间跨 δ 时该说什么？",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("crossing_effect", estimate=0.0005,
                                    lower=-0.02, upper=0.021,
                                    status="INCONCLUSIVE")],
            "block_table": _blocks([0.001, -0.002, 0.0, 0.003])}},
        "expected": {"contrast_id": "crossing_effect", "scope": "population_mean",
                     "semantic": "inconclusive"},
        "note": "跨 0 不是零效应证明",
    })
    cases.append({
        "case_id": "C4", "question": "固定 pilot 单点的结论范围？",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("pilot_effect", estimate=0.03,
                                    lower=0.01, upper=0.05)],
            "block_table": _blocks([0.03, 0.03, 0.03, 0.03])}},
        "expected": {"contrast_id": "pilot_effect", "scope": "fixed_point",
                     "semantic": "supported_at_point"},
        "note": "nuisance 只在单点固定，不得 general_population",
    })
    cases.append({
        "case_id": "C5", "question": "题目指定 contrast = secondary_effect；请引用它。",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("primary_effect", estimate=0.01,
                                    lower=0.005, upper=0.02),
                          _contrast("secondary_effect", estimate=0.07,
                                    lower=0.02, upper=0.12)],
            "block_table": _blocks([0.07, 0.06, 0.08, 0.07])}},
        "expected": {"contrast_id": "secondary_effect",
                     "scope": "population_mean", "semantic": "supported_mean"},
        "note": "多 contrast 时按题目选定，不挑更有利的一个",
    })
    cases.append({
        "case_id": "C6", "question": "分析结论与逐 block 数据是否一致？",
        "analysis": {"artifact_id": "analysis/output.json", "doc": {
            "schema": "simpraxis.rsi_g1.case_analysis.v1", "schema_version": 1,
            "contrasts": [_contrast("inconsistent_effect", estimate=0.05,
                                    lower=0.01, upper=0.09)],
            "block_table": _blocks([-0.05, -0.04, -0.06, -0.05])}},
        "expected": {"contrast_id": "inconsistent_effect",
                     "scope": "population_mean",
                     "semantic": "inconsistent_evidence"},
        "note": "cont rast 与 block 表矛盾；应指出不一致，且仍忠实引用候选值",
    })
    return cases


def public_view(case: Mapping[str, Any]) -> "dict[str, Any]":
    """发给模型的公开数据（两接口逐字节相同）。"""
    return {"case_id": case["case_id"], "question": case["question"],
            "analysis": case["analysis"], "allowed_scopes": list(SCOPES),
            "semantic_options": list(SEMANTIC_OPTIONS)}


def public_bytes(case: Mapping[str, Any]) -> bytes:
    return canonical(public_view(case))


# ---------------------------------------------------------------------------
# 预注册
# ---------------------------------------------------------------------------

def _stage_config_sha(path: Path) -> str:
    return sha(Path(path).read_bytes())


def build_prereg(*, diagnostic_id: str, ledger_path: Path,
                 stage_models_path: Path, model_id: str,
                 input_manifest_sha: str, seed: int = 20261115,
                 case_seed: int = 20261115) -> "dict[str, Any]":
    """24 槽冻结清单（顺序由固定 seed 打乱）。"""
    cases = build_cases()
    slots: "list[dict[str, Any]]" = []
    combos = [(c["case_id"], iface, rep) for c in cases
              for iface in INTERFACES for rep in REPLICATES]
    rng = random.Random(seed)
    order = list(combos)
    rng.shuffle(order)
    for idx, (case_id, iface, rep) in enumerate(order, start=1):
        slots.append({
            "diagnostic_id": diagnostic_id,
            "slot_id": f"S{idx:02d}",
            "case_id": case_id, "interface": iface, "replicate": rep,
            "fixed_case_seed": case_seed,
            "stage_config_sha": _stage_config_sha(stage_models_path),
            "provider_endpoint_class": "project_gateway_anthropic_messages",
            "model_id_snapshot": model_id,
            "ledger_path": str(ledger_path),
            "cache_policy": "off", "max_attempts": 1,
            "input_manifest_sha": input_manifest_sha,
            "expected_output_schema_sha": sha(canonical({
                "claim": ["hypothesis_id", "proposition", "metric_id",
                          "delta", "scope", "semantic"],
                "result_ref": ["artifact_id", "json_pointer"]})),
        })
    if len(slots) != MAX_POSTS:
        raise ProbeError("SLOT_COUNT", f"{len(slots)} != {MAX_POSTS}")
    return {"schema": SCHEMA_PREREG, "schema_version": 1,
            "diagnostic_id": diagnostic_id, "max_posts": MAX_POSTS,
            "stage": STAGE,
            "stage_config_sha": _stage_config_sha(stage_models_path),
            "model_id_snapshot": model_id,
            "slots": slots,
            "public_case_sha": {c["case_id"]: sha(public_bytes(c))
                                for c in cases},
            "rule": "冻结后才可发请求；每槽一次、无重试、不补槽",
            "created_at_utc": utc_now_iso_z()}


def write_prereg(root: Path, spec: Mapping[str, Any]) -> str:
    ensure_private_dir(Path(root) / "probe")
    text = json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    atomic_write_text(Path(root) / "probe" / "prereg.json", text, mode=0o600)
    return sha(text.encode())


def write_approval(root: Path, approval: Mapping[str, Any]) -> str:
    ensure_private_dir(Path(root) / "probe" / "approvals")
    doc = {"schema": "simpraxis.rsi_g1.probe_approval.v1", "schema_version": 1,
           "diagnostic_id": approval.get("diagnostic_id"),
           "approved_by": approval.get("approved_by"),
           "max_posts": int(approval.get("max_posts") or MAX_POSTS),
           "verbatim": approval.get("verbatim"),
           "created_at_utc": utc_now_iso_z()}
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    atomic_write_text(Path(root) / "probe" / "approvals" / "G1_DIAGNOSTIC_B.json",
                      text, mode=0o600)
    return sha(text.encode())


def load_prereg(root: Path) -> "dict[str, Any]":
    path = Path(root) / "probe" / "prereg.json"
    if not path.is_file():
        raise ProbeError("PREREG_MISSING", str(path))
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 提示（两接口共享同一份公开数据）
# ---------------------------------------------------------------------------

VERBATIM_RULE = (
    '输出**一个** JSON 对象：{"claim": {"hypothesis_id": "<str>", '
    '"proposition": "<str>", "metric_id": "<str>", "delta": <number>, '
    '"estimate": <number>, "lower": <number>, "upper": <number>, '
    '"scope": <allowed_scopes 之一>, "semantic": <semantic_options 之一>}, '
    '"result_ref": {"artifact_id": "analysis/output.json", '
    '"json_pointer": "<指向所选 contrast 的 JSON Pointer>"}}。'
    "estimate/lower/upper 必须**逐字复制**所引节点的同名字段，不得重算或四舍五入。")
BY_REF_RULE = (
    '输出**一个** JSON 对象：{"claim": {"hypothesis_id": "<str>", '
    '"proposition": "<str>", "metric_id": "<str>", "delta": <number>, '
    '"scope": <allowed_scopes 之一>, "semantic": <semantic_options 之一>}, '
    '"result_ref": {"artifact_id": "analysis/output.json", '
    '"json_pointer": "<指向所选 contrast 的 JSON Pointer>"}}。'
    "**不要**抄数值；只给引用指针，宿主会按指针取值。")


def build_request(case: Mapping[str, Any], interface: str) -> "T.Request":
    if interface not in INTERFACES:
        raise ProbeError("INTERFACE", interface)
    system = ("你是受控交付诊断中的研究候选。只输出一个 JSON 对象，"
              "不要解释、不要 Markdown。")
    data = json.dumps(public_view(case), ensure_ascii=False, sort_keys=True,
                      indent=1)
    rule = VERBATIM_RULE if interface == "verbatim" else BY_REF_RULE
    brief = ("### 公开数据（两接口逐字节相同）\n" + data
             + "\n\n### 提交要求（" + interface + "）\n" + rule)
    return T.build_request(system=system, task_brief=brief, turns=(),
                           counter=lambda text: max(1, len(text) // 4),
                           max_tokens=G1_MAX_TOKENS)


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------

def _load_reply_json(text: Any) -> "tuple[dict[str, Any] | None, str | None]":
    raw = str(text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) >= 3 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None, "NO_JSON"
        try:
            doc = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None, "BAD_JSON"
    if not isinstance(doc, dict):
        return None, "NOT_OBJECT"
    return doc, None


def _resolve(doc: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ProbeError("POINTER", pointer)
    node = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            node = node[token]
        elif isinstance(node, list):
            node = node[int(token)]
        else:
            raise ProbeError("POINTER", pointer)
    return node


def score_slot(case: Mapping[str, Any], interface: str,
               text: Any) -> "dict[str, Any]":
    """机械面 + 语义面逐项；返回值可直接进报告。"""
    exp = case["expected"]
    doc, err = _load_reply_json(text)
    out: "dict[str, Any]" = {"parse_ok": False, "binding_ok": False,
                             "numeric_ok": False, "semantic_ok": False,
                             "mechanical_ok": False, "error": err}
    if doc is None:
        return out
    claim = doc.get("claim")
    ref = doc.get("result_ref")
    if not isinstance(claim, Mapping) or not isinstance(ref, Mapping):
        out["error"] = "SHAPE"
        return out
    need = ["hypothesis_id", "proposition", "metric_id", "delta", "scope",
            "semantic"]
    if interface == "verbatim":
        need += ["estimate", "lower", "upper"]
    missing = [k for k in need if k not in claim]
    if missing:
        out["error"] = "MISSING:" + ",".join(missing)
        return out
    if str(claim["scope"]) not in SCOPES:
        out["error"] = "SCOPE_INVALID"
        return out
    if str(claim["semantic"]) not in SEMANTIC_OPTIONS:
        out["error"] = "SEMANTIC_INVALID"
        return out
    out["parse_ok"] = True

    public = case["analysis"]["doc"]
    try:
        if str(ref.get("artifact_id")) != "analysis/output.json":
            raise ProbeError("ARTIFACT", str(ref.get("artifact_id")))
        node = _resolve(public, str(ref.get("json_pointer")))
        if not isinstance(node, Mapping) or "estimate" not in node:
            raise ProbeError("NODE", "pointer 未指向 contrast 对象")
    except (ProbeError, KeyError, IndexError, ValueError, TypeError) as exc:
        out["error"] = f"BINDING:{type(exc).__name__}"
        return out
    out["binding_ok"] = True
    out["contrast_id"] = str(node.get("contrast_id"))
    out["binding_by_contrast"] = out["contrast_id"] == exp["contrast_id"]

    if interface == "verbatim":
        numeric = all(
            abs(float(claim[k]) - float(node[k])) <= tol(float(node[k]))
            for k in ("estimate", "lower", "upper"))
    else:
        numeric = True       # by_ref 的数值由宿主按指针取，天然保真
    out["numeric_ok"] = bool(numeric)
    out["mechanical_ok"] = bool(out["parse_ok"] and out["binding_ok"]
                                and out["numeric_ok"]
                                and out["binding_by_contrast"])
    out["semantic_ok"] = (str(claim["scope"]) == exp["scope"]
                          and str(claim["semantic"]) == exp["semantic"])
    return out


# ---------------------------------------------------------------------------
# 运行（24 槽硬帽；任何不变量破坏 ⇒ INCONCLUSIVE）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class G1Slot:
    slot_id: str
    case_id: str
    interface: str
    replicate: int


def _sent_rows(ledger_path: Path) -> "list[dict[str, Any]]":
    if not Path(ledger_path).is_file():
        return []
    rows = []
    for line in Path(ledger_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_probe(*, root: Path, prereg: Mapping[str, Any], sender: Any,
              approval_path: Path, ledger_path: Path) -> "dict[str, Any]":
    """按冻结顺序逐槽发一次；不重试、不补槽。"""
    if not Path(approval_path).is_file():
        raise ProbeError("APPROVAL_MISSING", str(approval_path))
    approval = json.loads(Path(approval_path).read_text(encoding="utf-8"))
    if int(approval.get("max_posts") or 0) != MAX_POSTS:
        raise ProbeError("APPROVAL_CAP", str(approval.get("max_posts")))
    cases = {c["case_id"]: c for c in build_cases()}
    records: "list[dict[str, Any]]" = []
    posts = 0
    invariant_failures: "list[str]" = []
    for slot in prereg["slots"]:
        slot_id = str(slot["slot_id"])
        prior = [row for row in _sent_rows(ledger_path)
                 if row.get("batch_id") == f"G1B-{slot_id}"]
        if prior:
            records.append({"slot_id": slot_id, "case_id": slot["case_id"],
                            "interface": slot["interface"],
                            "replicate": slot["replicate"],
                            "status": "DUPLICATE",
                            "ledger_seq": prior[0].get("seq")})
            invariant_failures.append(f"{slot_id}:DUPLICATE")
            continue
        if posts >= MAX_POSTS:
            records.append({"slot_id": slot_id, "case_id": slot["case_id"],
                            "interface": slot["interface"],
                            "replicate": slot["replicate"],
                            "status": "NOT_CALLED", "ledger_seq": None})
            continue
        case = cases[str(slot["case_id"])]
        request = build_request(case, str(slot["interface"]))
        try:
            reply = sender(request, slot_id=slot_id)
        except Exception as exc:  # noqa: BLE001 —— 传输失败即该槽 FAIL
            records.append({"slot_id": slot_id, "case_id": slot["case_id"],
                            "interface": slot["interface"],
                            "replicate": slot["replicate"],
                            "status": "CALLED_FAIL", "ledger_seq": None,
                            "error": f"{type(exc).__name__}: {exc}"[:300]})
            posts += 1
            break
        posts += 1
        score = score_slot(case, str(slot["interface"]), reply["text"])
        records.append({"slot_id": slot_id, "case_id": slot["case_id"],
                        "interface": slot["interface"],
                        "replicate": slot["replicate"],
                        "status": "CALLED_OK",
                        "ledger_seq": reply.get("ledger_seq"),
                        "request_sha256": reply.get("request_sha256"),
                        "response_id": reply.get("response_id"),
                        "attempts_http": reply.get("attempts_http"),
                        "visible_request_bytes": reply.get("n_bytes"),
                        "score": score,
                        "reply_sha256": sha(str(reply["text"]).encode())})
        if int(reply.get("attempts_http") or 1) != 1:
            invariant_failures.append(f"{slot_id}:ATTEMPTS")
    # 未走到的槽显式列 NOT_CALLED（提前失败不补发）
    seen = {r["slot_id"] for r in records}
    for slot in prereg["slots"]:
        if str(slot["slot_id"]) not in seen:
            records.append({"slot_id": str(slot["slot_id"]),
                            "case_id": slot["case_id"],
                            "interface": slot["interface"],
                            "replicate": slot["replicate"],
                            "status": "NOT_CALLED", "ledger_seq": None})
    axes = decide(prereg, records, invariant_failures)
    report = {"schema": SCHEMA_REPORT, "schema_version": 1,
              "diagnostic_id": prereg["diagnostic_id"],
              "posts": posts, "max_posts": MAX_POSTS,
              "slots": records, "invariants": invariant_failures,
              "axes": axes["axes"], "overall": axes["overall"],
              "created_at_utc": utc_now_iso_z(),
              "rule": ("24 呼只够局部定位；不出统计显著性，不出普遍成功率；"
                       "自由文本不参与主判定")}
    ensure_private_dir(Path(root) / "probe")
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    atomic_write_text(Path(root) / "probe" / "probe_report.json", text, mode=0o600)
    report["report_sha256"] = sha(text.encode())
    return report


def decide(prereg: Mapping[str, Any], records: Sequence[Mapping[str, Any]],
           invariant_failures: Sequence[str] = ()) -> "dict[str, Any]":
    """Plan §5.2 的双轴判定（机械轴优先匹配；语义轴独立）。"""
    expected = len(prereg["slots"])
    by_slot = {str(r["slot_id"]): r for r in records}
    statuses = [str(by_slot[s["slot_id"]]["status"]) for s in prereg["slots"]]
    complete = (len(records) == expected and set(statuses) == {"CALLED_OK"}
                and not invariant_failures)
    if not complete:
        bad = [s for s in prereg["slots"]
               if by_slot.get(str(s["slot_id"]), {}).get("status") != "CALLED_OK"]
        reason = []
        if invariant_failures:
            reason.append("INVARIANT:" + ",".join(sorted(set(invariant_failures))))
        if bad:
            reason.append(f"UNFILLED_SLOTS:{len(bad)}")
        return {"overall": "DIAGNOSTIC_INCONCLUSIVE",
                "axes": {"delivery": "NOT_MEASURED",
                         "semantic": "NOT_MEASURED"},
                "reason": "; ".join(reason) or "INCOMPLETE"}
    mech: "dict[str, list[bool]]" = {"verbatim": [], "by_ref": []}
    sem: "dict[str, int]" = {"verbatim": 0, "by_ref": 0}
    per_case: "dict[str, dict[str, Any]]" = {}
    for slot in prereg["slots"]:
        row = by_slot[str(slot["slot_id"])]
        score = row.get("score") or {}
        mech[str(slot["interface"])].append(bool(score.get("mechanical_ok")))
        if score.get("semantic_ok"):
            sem[str(slot["interface"])] += 1
        per_case.setdefault(str(slot["case_id"]), {}).setdefault(
            str(slot["interface"]), []).append(
            {"mechanical_ok": bool(score.get("mechanical_ok")),
             "semantic_ok": bool(score.get("semantic_ok"))})
    v_ok_all = all(mech["verbatim"]) and len(mech["verbatim"]) == 12
    r_ok_all = all(mech["by_ref"]) and len(mech["by_ref"]) == 12
    v_fail, r_fail = mech["verbatim"].count(False), mech["by_ref"].count(False)
    if not r_ok_all:
        delivery = "REFERENCE_INTERFACE_NOT_READY"
    elif v_ok_all and r_ok_all:
        delivery = "NO_DELIVERY_BOTTLENECK_OBSERVED"
    elif r_fail == 0 and v_fail >= 1 and sem["by_ref"] >= sem["verbatim"]:
        delivery = "DELIVERY_LEVER_SUPPORTED_LOCAL"
    else:
        delivery = "INCONCLUSIVE"
    # 语义轴：两臂机械全过时才有意义
    semantic = "NOT_MEASURED"
    if v_ok_all and r_ok_all:
        gap_cases = []
        for case_id, row in per_case.items():
            both_wrong = all(
                all(not x["semantic_ok"] for x in row.get(iface, []))
                for iface in INTERFACES)
            if both_wrong:
                gap_cases.append(case_id)
        semantic = ("SEMANTIC_GAP_SUSPECTED" if gap_cases
                    else "NO_REPEAT_GAP_OBSERVED")
    return {"overall": "DIAGNOSTIC_COMPLETE",
            "axes": {"delivery": delivery, "semantic": semantic},
            "counts": {"verbatim_mechanical_fail": v_fail,
                       "by_ref_mechanical_fail": r_fail,
                       "verbatim_semantic_ok": sem["verbatim"],
                       "by_ref_semantic_ok": sem["by_ref"]}}


# ---------------------------------------------------------------------------
# 独立账（24 子帽）与真实发送（仅 B 层；双闸）
# ---------------------------------------------------------------------------

class G1Ledger:
    """G1 诊断账：独立文件、24 硬帽、每槽一批、seq 连续、严格 UTC Z。

    只实现网关 ``BillingGate`` 需要的最小面（preflight/record/_record_blocked）；
    不接任何研究族账（Plan §6「不借 R1.3 余额」）。
    """

    def __init__(self, root: Path, *, cap: int = MAX_POSTS) -> None:
        self.root = Path(root)
        self.path = self.root / "quota" / "ledger.jsonl"
        self.cap = int(cap)
        ensure_private_dir(self.root / "quota")

    def rows(self) -> "list[dict[str, Any]]":
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def calls(self) -> int:
        return sum(1 for row in self.rows() if row.get("event") == "call")

    def preflight(self, stage: str, *, task: int, batch_id: str) -> "dict[str, Any]":
        if self.calls() >= self.cap:
            raise ProbeError("G1_CAP", f"{self.calls()} >= {self.cap}")
        for row in self.rows():
            if row.get("batch_id") == batch_id and row.get("event") == "call":
                raise ProbeError("G1_DUPLICATE_SLOT", batch_id)
        return {"ok": True, "stage": stage, "task": int(task),
                "batch_id": batch_id, "used": self.calls()}

    def _append(self, row: "dict[str, Any]") -> None:
        ensure_private_dir(self.path.parent)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def record(self, stage: str, *, task: int, batch_id: str, ok: bool,
               model: "str | None" = None, thread_id: str = "",
               tokens_in: "int | None" = None,
               tokens_out: "int | None" = None,
               tokens_cache_read: "int | None" = None,
               tokens_cache_create: "int | None" = None,
               err: "str | None" = None,
               evidence: "str | None" = None) -> "dict[str, Any]":
        used = self.calls()
        row = {"event": "call", "seq": used + 1, "ts_iso": utc_now_iso_z(),
               "stage": stage, "task": int(task), "batch_id": batch_id,
               "thread_id": thread_id, "model": model, "ok": bool(ok),
               "tokens_in": tokens_in, "tokens_out": tokens_out,
               "tokens_cache_read": tokens_cache_read,
               "tokens_cache_create": tokens_cache_create,
               "err": err, "evidence": evidence,
               "global_used_after": used + 1, "cap": self.cap}
        self._append(row)
        return row

    def _record_blocked(self, stage: str, task: int, batch_id: str,
                        reason: str, thread_id: str = "") -> None:
        self._append({"event": "preflight_blocked", "seq": None,
                      "ts_iso": utc_now_iso_z(), "stage": stage,
                      "task": int(task), "batch_id": batch_id,
                      "thread_id": thread_id, "reason": reason})


def require_probe_ready(root: Path, approval_path: Path) -> "dict[str, Any]":
    """B 层双闸：用户批准件 + ``SIMPRAXIS_ALLOW_PAID=1``。"""
    if str(os.environ.get("SIMPRAXIS_ALLOW_PAID", "")).strip() != "1":
        raise ProbeError("PAID_NOT_AUTHORIZED", "需 SIMPRAXIS_ALLOW_PAID=1")
    if not Path(approval_path).is_file():
        raise ProbeError("APPROVAL_MISSING", str(approval_path))
    return json.loads(Path(approval_path).read_text(encoding="utf-8"))


def build_sender(*, root: Path, diagnostic_id: str) -> Any:
    """真实 sender：网关 ``coding_debug`` + G1 独立账；每槽一次调用。"""
    from orchestrator.gateway.quota_ledger import billing_context
    from orchestrator.rsi_r1 import gateway_transport as GTX

    ledger = G1Ledger(root)
    gateway = GTX.build_gateway(ledger=ledger, stage=STAGE)
    model_id = gateway.model_id(STAGE)

    def sender(request: "T.Request", *, slot_id: str) -> "dict[str, Any]":
        messages = [{"role": "system", "content": request.system},
                    *[dict(m) for m in request.messages]]
        capture: "dict[str, Any]" = {}
        batch = f"G1B-{slot_id}"
        with billing_context(task=0, batch_id=batch,
                             thread_id=f"g1-{diagnostic_id}"):
            text, tokens = gateway.call(
                STAGE, messages, max_tokens=int(request.max_tokens),
                temperature=0.2, timeout=120, capture=capture)
        seqs = [row["seq"] for row in ledger.rows()
                if row.get("batch_id") == batch and row.get("event") == "call"]
        return {"text": str(text or ""), "request_sha256": request.sha256,
                "response_id": (f"g1b-{slot_id}-{request.sha256[:12]}"),
                "attempts_http": int(capture.get("attempts_http", 1) or 1),
                "ledger_seq": seqs[-1] if seqs else None,
                "n_bytes": int(request.n_bytes),
                "model_id": model_id, "tokens": list(tokens or [])}

    return sender
