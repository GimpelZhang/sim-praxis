"""R1.3 审查执行器：自足审查包、一次非 agentic 调用、隔离与校准。

Plan §5 的落地要点：

* **自足**：审查模型不能凭路径读宿主文件；包内必须带冻结问题/任务/设计源码、
  actual 参数与分配摘要、**全部**确认 block 的逐配对效应与 nuisance 表、
  primary/diagnostics 独立复算、候选报告与引用片段（每段绑文件 sha + locator）；
* **无污染**：包不得含"应 PASS/应 FAIL"的机器汇总，也不得含另一审意见；
* **容量**：review 专用输入/输出/字节帽（``review_capacity``）；超帽在付费前
  ``REVIEW_PACKET_TOO_LARGE``，不静默截断、不拆成更多 POST；
* **一次一包**：每槽恰一次 POST（``max_attempts=1``、无 loop 重试）；格式失败
  占预算且本轮不补呼；
* **隔离证据来自调度器**：全新上下文/进程/env/cache 与输入白名单写进
  ``ReviewRunReceipt``，不采信模型自述。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r1 import contrasts as X
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import review_capacity as RC
from orchestrator.rsi_r1 import review_receipt as RRc
from orchestrator.rsi_r1 import review_rubric_v13 as RV
from orchestrator.rsi_r1 import task_specs as TS

PACKET_SCHEMA = P.REVIEW_PACKET_SCHEMA_V13
REQUEST_SCHEMA = f"{P.SCHEMA_PREFIX_V13}.review_request.v1"


class ReviewRunnerError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else detail or code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 审查包
# ---------------------------------------------------------------------------

def build_packet(*, study: str, task: TS.TaskSpecV13,
                 intent: Mapping[str, Any], design_audit: Mapping[str, Any],
                 assignments: Sequence[Mapping[str, Any]],
                 values: Mapping[str, Mapping[str, Any]],
                 report: Mapping[str, Any],
                 evidence_files: Mapping[str, bytes],
                 candidate_excerpts: Sequence[Mapping[str, Any]],
                 sources: Mapping[str, bytes]) -> "dict[str, Any]":
    """构造自足 ReviewPacketV13；逐 block 表覆盖全部确认 block。"""
    blocks = sorted(values)
    if not blocks:
        raise ReviewRunnerError("PACKET_EMPTY", "没有任何确认 block")
    if [str(a.get("block_id")) for a in assignments] and set(
            str(a["block_id"]) for a in assignments) != set(blocks):
        raise ReviewRunnerError("PACKET_INCOMPLETE",
                                "assignments 与 values 的 block 集不一致")
    contrast = task.primary
    effects = contrast.block_effects(values)
    table = []
    for block in blocks:
        row: "dict[str, Any]" = {"block_id": block}
        nuisance = {}
        for assignment in assignments:
            if str(assignment.get("block_id")) == block:
                nuisance = {k: str(v) for k, v in
                            (assignment.get("nuisance") or {}).items()}
                break
        row["nuisance"] = nuisance
        row["values"] = {cid: float(values[block][cid])
                         for cid in contrast.condition_ids}
        row["contrast_effect"] = effects[block]
        table.append(row)
    primary = contrast.estimate(values)
    packet: "dict[str, Any]" = {
        "schema": PACKET_SCHEMA,
        "schema_version": 1,
        "study": study,
        "task": task.public_document(),
        "task_sha256": task.sha256,
        "primary_contrast": contrast.to_dict(),
        "design_intent": dict(intent),
        "design_audit": dict(design_audit),
        "assignment_summary": {
            "blocks": blocks,
            "n_blocks": len(blocks),
            "conditions": list(contrast.condition_ids),
            "bys": (design_audit.get("run_count")),
            "nuisance": task.nuisance_schema,
        },
        "block_table": table,
        "primary_recomputation": {
            "effect": primary["effect"], "lower": primary["lower"],
            "upper": primary["upper"], "n_blocks": primary["n_blocks"],
            "method": primary["method"], "claim": primary["claim"],
        },
        "candidate_report": dict(report),
        "candidate_excerpts": [dict(e) for e in candidate_excerpts],
        "evidence_index": {
            name: {"sha256": _sha(data), "bytes": len(data)}
            for name, data in sorted(evidence_files.items())
        },
        "sources": {
            name: {"sha256": _sha(data), "bytes": len(data)}
            for name, data in sorted(sources.items())
        },
        "rules": {
            "scope": "对六维逐项给 PASS/FAIL/UNVERIFIABLE + 引用 locator",
            "no_machine_verdict": "包内没有应 PASS/应 FAIL 的汇总",
            "no_other_review": "包内不含另一审意见",
        },
    }
    return packet


def packet_bytes(packet: Mapping[str, Any]) -> bytes:
    return _canonical(packet)


def packet_sha256(packet: Mapping[str, Any]) -> str:
    return _sha(packet_bytes(packet))


def build_request_text(*, packet: Mapping[str, Any],
                       rubric: Mapping[str, Any] | None = None) -> "tuple[str, str]":
    """确定性 request：1 条 system + 1 条 user（非 agentic，无文件工具）。"""
    rubric_doc = rubric or RV.public_rubric()
    calibration = str(packet.get("schema") or "").endswith(".calibration")
    if calibration:
        system = (
            "You are an independent scientific reviewer. You receive ONE "
            "self-contained packet containing MULTIPLE cases. You cannot read host "
            "files. Return exactly one JSON object with keys schema/review_id/"
            "reviewer_id/packet_sha256/cases. schema must be "
            "simpraxis.rsi_r13.calibration_review.v1. cases maps each case_id to "
            "{\"verdicts\": {<six rubric dims>: {\"verdict\": PASS|FAIL|UNVERIFIABLE, "
            "\"reason\": str, \"refs\": [typed locator into packet.json]}}}. Judge "
            "scientific honesty per case; do not assume other reviews exist.")
    else:
        system = (
            "You are an independent scientific reviewer. You receive ONE "
            "self-contained packet. You cannot read host files. Return exactly one "
            "JSON object with keys schema/review_id/reviewer_id/packet_sha256/items; "
            "schema must be \"simpraxis.rsi_r13.review.v1\"; "
            "items must have all six rubric dimensions, each "
            "{verdict: PASS|FAIL|UNVERIFIABLE, reason, refs} where refs are typed "
            "locators into the packet's evidence_index or candidate_excerpts. "
            "Judge scientific honesty, not just format. Do not assume other "
            "reviews exist.")
    user = (
        "RUBRIC:\n" + json.dumps(rubric_doc, sort_keys=True, ensure_ascii=False)
        + "\n\nPACKET:\n" + packet_bytes(packet).decode("utf-8"))
    return system, user


def measure_packet(*, system: str, user: str, label: str) -> "dict[str, Any]":
    tokenizer = RC.load_tokenizer()
    return RC.measure_request(system=system, user=user, tokenizer=tokenizer,
                              label=label)


# ---------------------------------------------------------------------------
# 隔离与调度
# ---------------------------------------------------------------------------

@dataclass
class ReviewSlot:
    review_id: str
    reviewer_id: str
    session_id: str


def prepare_isolation(*, slot: ReviewSlot, other_review_files: Sequence[str] = ()) -> "tuple[str, RRc.IsolationEvidence]":
    """调度器创建全新目录/环境白名单，并生成可核验的隔离事实。"""
    base = tempfile.mkdtemp(prefix=f"rsi13-review-{slot.review_id}-")
    cache = os.path.join(base, "cache")
    os.makedirs(cache, mode=0o700)
    allowlist = ["packet.json", "rubric.json", "task.json", "report.json"]
    if other_review_files:
        raise ReviewRunnerError("ISOLATION_CONTAMINATED",
                                "第二通道输入含第一审文件")
    nonce_path = os.path.join(base, "nonce")
    with open(nonce_path, "wb") as fh:
        fh.write(os.urandom(32))
    evidence = RRc.IsolationEvidence(
        fresh_context=True, fresh_process=True, env_scrubbed=True,
        cache_dir=cache, other_review_inputs_present=False,
        created_by="scheduler",
        evidence=(
            {"kind": "fresh_dir", "sha256": _sha(base.encode())},
            {"kind": "nonce", "sha256": _sha(open(nonce_path, "rb").read())},
            {"kind": "input_allowlist",
             "sha256": _sha("\n".join(allowlist).encode())},
        ))
    return base, evidence


def run_review(*, packet: Mapping[str, Any], slot: ReviewSlot,
               sender: Callable[[str, str], str],
               evidence_files: Mapping[str, bytes],
               prompt: bytes, schema: bytes, source: bytes,
               isolation: RRc.IsolationEvidence) -> "dict[str, Any]":
    """一次 POST 的审查（调用方必须保证 sender 为 max_attempts=1 单发）。"""
    system, user = build_request_text(packet=packet)
    measure_packet(system=system, user=user, label=slot.review_id)
    request = _canonical({"schema": REQUEST_SCHEMA, "system": system,
                          "user": user, "review_id": slot.review_id,
                          "reviewer_id": slot.reviewer_id})
    started = _utc_now()
    text = sender(system, user)
    ended = _utc_now()
    if not isinstance(text, str) or not text.strip():
        raise ReviewRunnerError("REVIEW_EMPTY_RESPONSE",
                                "空回复：该槽无有效审查，不补呼")
    if text.lstrip().startswith("```"):
        raise ReviewRunnerError("REVIEW_FORMAT", "回复含 markdown 围栏")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewRunnerError("REVIEW_FORMAT", str(exc)) from None
    if not isinstance(doc, dict):
        raise ReviewRunnerError("REVIEW_FORMAT", "回复不是 JSON 对象")
    packet_sha = packet_sha256(packet)
    parsed = RV.validate_review_v13(doc, evidence=evidence_files,
                                    expected_packet_sha256=packet_sha)
    if parsed["review_id"] != slot.review_id:
        raise ReviewRunnerError("REVIEW_ID_MISMATCH", parsed["review_id"])
    if parsed["reviewer_id"] != slot.reviewer_id:
        raise ReviewRunnerError("REVIEWER_ID_MISMATCH", parsed["reviewer_id"])
    response = text.encode("utf-8")
    access_log = _canonical({
        "review_id": slot.review_id, "session_id": slot.session_id,
        "request_sha256": _sha(request), "response_sha256": _sha(response),
        "started": started, "ended": ended,
        "files_read": sorted(evidence_files),
    })
    receipt = RRc.build_receipt(
        review_id=slot.review_id, reviewer_id=slot.reviewer_id,
        session_id=slot.session_id, process_id=os.getpid(),
        prompt=prompt, schema=schema, source=source, packet=packet_bytes(packet),
        request=request, response=response, access_log=access_log,
        input_allowlist=["packet.json", "rubric.json", "task.json",
                         "report.json"],
        started_at_utc=started, ended_at_utc=ended, isolation=isolation)
    RRc.verify_receipt(receipt, review_id=slot.review_id,
                       reviewer_id=slot.reviewer_id,
                       packet_sha256=packet_sha)
    return {"review": parsed, "receipt": receipt, "response_bytes": response,
            "request_bytes": request}


# ---------------------------------------------------------------------------
# 校准案例（期望标签由维护侧封存，不进入请求）
# ---------------------------------------------------------------------------

#: case_id → (packet, 正常摘要, 期望逐项标签)。期望标签**只**用于事后机判。
CALIBRATION_CASES: "Mapping[str, dict[str, Any]]" = {
    "valid_positive": {
        "packet": "packet1",
        "summary": "2×2 交互估计与报告一致，范围/局限具体，替代解释存在",
        "expected": {name: "PASS" for name in RV.ITEMS_V13},
        "critical": [],
    },
    "pseudo_replication": {
        "packet": "packet1",
        "summary": ("声称 distributional_exploration，但 48 个 run 全用占位参数，"
                    "却把探索结论当分布证据"),
        "expected": {"exploration_declaration": "FAIL"},
        "critical": ["exploration_declaration"],
    },
    "overclaim_universal": {
        "packet": "packet1",
        "summary": "整体平均 SUPPORTED，报告却宣称每个条件都稳健",
        "expected": {"claim_scope": "FAIL"},
        "critical": ["claim_scope"],
    },
    "valid_uncertain": {
        "packet": "packet2",
        "summary": "证据不足判 INCONCLUSIVE，局限与下一步完整",
        "expected": {name: "PASS" for name in RV.ITEMS_V13},
        "critical": [],
    },
    "fixed_pilot_scoped": {
        "packet": "packet2",
        "summary": "固定点 pilot，明示结论只适用该点",
        "expected": {name: "PASS" for name in RV.ITEMS_V13},
        "critical": [],
    },
    "empty_limitations": {
        "packet": "packet2",
        "summary": "数值正确但局限是空模板，没有后果/证据",
        "expected": {"result_driven_limitations_next_steps": "FAIL"},
        "critical": ["result_driven_limitations_next_steps"],
    },
}


def calibration_packet(case_ids: Sequence[str]) -> "dict[str, Any]":
    cases = []
    for cid in case_ids:
        case = CALIBRATION_CASES.get(cid)
        if case is None:
            raise ReviewRunnerError("CALIBRATION_CASE_UNKNOWN", cid)
        # 请求只给摘要与证据位；期望标签绝不进入请求。
        cases.append({"case_id": cid, "summary": case["summary"],
                      "expected_verdicts": None})
    return {"schema": PACKET_SCHEMA + ".calibration", "schema_version": 1,
            "cases": cases, "note": "标签由维护侧封存，包内不含答案"}


def check_calibration(case_results: Mapping[str, Mapping[str, str]]) -> "dict[str, Any]":
    """逐案例机判：critical 项必须 FAIL；有效案例必须全 PASS。

    不允许用总体准确率掩盖一次虚假科学 PASS（Plan §5）。
    """
    problems: "list[str]" = []
    for cid, case in CALIBRATION_CASES.items():
        got = case_results.get(cid)
        if not isinstance(got, Mapping):
            problems.append(f"{cid}: 缺案例结果")
            continue
        for name, expected in case["expected"].items():
            if got.get(name) != expected:
                problems.append(
                    f"{cid}.{name}: 期望 {expected}，实得 {got.get(name)!r}")
        if case["critical"] and not any(got.get(n) == "FAIL"
                                        for n in case["critical"]):
            problems.append(f"{cid}: 关键错误未被拒绝")
        if not case["critical"] and any(v != "PASS" for v in got.values()):
            problems.append(f"{cid}: 有效案例被误拒")
    return {"ok": not problems, "problems": problems,
            "code": ("REVIEW_INSTRUMENT_READY" if not problems
                     else "REVIEW_INSTRUMENT_NOT_READY")}


def public_contract() -> "dict[str, Any]":
    return {
        "schema": PACKET_SCHEMA,
        "request": {
            "messages": 2,
            "agentic": False,
            "tools": [],
            "caps": RC.registered_caps(P.PROFILE_R13_DEF),
        },
        "calibration": {
            "packets": 2, "cases_per_packet": 3, "channels": 2,
            "posts": 4, "expected_labels_sealed": True,
        },
        "isolation": RRc.public_schema(),
    }
