"""R1.3 审查调度：校准 4 呼与正式 2×2 呼（每次一个新进程、一份包）。

父进程负责：批准件、隔离目录、输入白名单、子进程启动、收据与合并；
子进程只做一次 POST。任何格式失败/空回答占预算且**不补呼**。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import review_receipt as RRc
from orchestrator.rsi_r1 import review_rubric_v13 as RV
from orchestrator.rsi_r1 import review_runner as RN
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.gateway import quota_ledger as QL

REVIEW_PERMIT_REL = "contract/review_permit.json"
CALIBRATION_REPORT_REL = "contract/review_calibration.json"
PERMIT_SCHEMA = f"{TS.P.SCHEMA_PREFIX_V13}.review_calibration_permit.v1"
CALIB_REVIEW_SCHEMA = f"{TS.P.SCHEMA_PREFIX_V13}.calibration_review.v1"
LANE_CAL = "review_calibration"
LANE_FINAL = "review_final"
TASK_CAL = 4
TASK_FINAL = 5


class ReviewRunError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_permit(ctx: Any, *, max_posts: int = 4) -> dict:
    lock_doc = ctx.read_json("contract/lock.json")
    lock_sha = _sha(json.dumps(lock_doc, sort_keys=True,
                               separators=(",", ":"),
                               ensure_ascii=False).encode())
    binding = lock_doc.get("validation_binding") or {}
    doc = {
        "schema": PERMIT_SCHEMA, "schema_version": 1,
        "envelope_id": ctx.envelope_id,
        "lane": LANE_CAL, "max_posts": int(max_posts),
        "paid_lock_sha256": lock_sha,
        "validation_lock_sha256": binding.get("validation_lock_sha256"),
        "t5_summary_sha256": binding.get("t5_summary_sha256"),
        "issued_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "只允许 review_calibration lane 恰 4 次 POST；不得转给候选",
    }
    ctx.write_json(REVIEW_PERMIT_REL, doc)
    return doc


def check_permit(ctx: Any) -> dict:
    if not ctx.exists(REVIEW_PERMIT_REL):
        raise ReviewRunError("PERMIT_MISSING", REVIEW_PERMIT_REL)
    doc = ctx.read_json(REVIEW_PERMIT_REL)
    if doc.get("schema") != PERMIT_SCHEMA:
        raise ReviewRunError("PERMIT_SCHEMA", str(doc.get("schema")))
    lock_doc = ctx.read_json("contract/lock.json")
    if not lock_doc.get("validation_binding"):
        raise ReviewRunError("PERMIT_BINDING", "paid LOCK 缺 validation_binding")
    return doc


def _clean_env() -> "dict[str, str]":
    keep_prefix = ("SIMPRAXIS_", "ANTHROPIC_")
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "TZ", "USER"}
    env = {k: v for k, v in os.environ.items()
           if k in keep or k.startswith(keep_prefix)}
    env["PYTHONPATH"] = str(Path(sys.executable).resolve().parents[0])
    return env


def _approval(ctx: Any, *, batch_id: str, lane: str, task: int,
              approved: int) -> None:
    """review lane 批准件（PT.ensure_approval 只认候选 lane）。"""
    path = QL.RSI_R13_NAMESPACE.approvals_dir_fn() / f"{batch_id}.json"
    want = {"batch_id": batch_id, "lane": lane, "task": int(task),
            "stage": "coding_debug", "approved": int(approved),
            "note": "R1.3 review lane（校准/正式）"}
    if path.is_file():
        got = json.loads(path.read_text(encoding="utf-8"))
        for key, value in want.items():
            if got.get(key) != value:
                raise ReviewRunError("APPROVAL_DRIFT", f"{batch_id}.{key}")
        return
    QL.write_approval(batch_id=batch_id, task=int(task),
                      stage="coding_debug", approved=int(approved),
                      beyond_normal=False, note=want["note"],
                      namespace=QL.RSI_R13_NAMESPACE)


def seal_packet(packet: Mapping[str, Any], slot: RN.ReviewSlot
                ) -> "tuple[dict[str, Any], bytes, str]":
    """给包写入自包含摘要（R13-D3）。

    ``packet_sha256`` = sha256(canonical(包内容 **不含** review_id/reviewer_id/
    packet_sha256))；两个审查通道因此共享同一逻辑包摘要，审查者只需回显。
    """
    content = {k: v for k, v in packet.items()
               if k not in ("review_id", "reviewer_id", "packet_sha256")}
    sha = _sha(json.dumps(content, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8"))
    doc = dict(content, review_id=slot.review_id,
               reviewer_id=slot.reviewer_id, packet_sha256=sha)
    return doc, json.dumps(doc, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8"), sha


def _load_spawned(work: Path, slot: RN.ReviewSlot) -> "dict[str, Any]":
    """从已存在的槽目录重建（不重发 POST；用于续跑/重解析）。"""
    call = json.loads((work / "call.json").read_text(encoding="utf-8"))
    packet_bytes = (work / "packet.json").read_bytes()
    packet_sha = json.loads(packet_bytes)["packet_sha256"]
    request = (work / "request.bin").read_bytes()
    response = str(call.get("text") or "").encode("utf-8")
    isolation = RRc.IsolationEvidence(
        fresh_context=True, fresh_process=True, env_scrubbed=True,
        cache_dir=str(work), other_review_inputs_present=False,
        created_by="scheduler",
        evidence=({"kind": "fresh_dir", "sha256": _sha(str(work).encode())},
                  {"kind": "nonce",
                   "sha256": _sha((work / "nonce").read_bytes())},
                  {"kind": "input_allowlist",
                   "sha256": _sha(b"packet.json")}))
    access = json.dumps({"review_id": slot.review_id,
                         "files_read": ["packet.json"],
                         "request_sha256": call["request_sha256"],
                         "response_sha256": _sha(response),
                         "started": call["started_at_utc"],
                         "ended": call["ended_at_utc"]},
                        sort_keys=True).encode()
    receipt = RRc.build_receipt(
        review_id=slot.review_id, reviewer_id=slot.reviewer_id,
        session_id=f"sess-{slot.review_id}", process_id=int(call["process_id"]),
        prompt=b"review-rubric-v13", schema=json.dumps(
            RV.public_rubric(), sort_keys=True).encode(),
        source=b"packet", packet=packet_bytes, request=request,
        response=response, access_log=access,
        input_allowlist=["packet.json"],
        started_at_utc=str(call["started_at_utc"]),
        ended_at_utc=str(call["ended_at_utc"]), isolation=isolation,
        packet_sha256=packet_sha)
    RRc.verify_receipt(receipt, review_id=slot.review_id,
                       reviewer_id=slot.reviewer_id,
                       packet_sha256=packet_sha)
    return {"call": call, "packet_bytes": packet_bytes,
            "packet_sha256": packet_sha, "receipt": receipt,
            "reused": True}


def _spawn(ctx: Any, *, packet: dict, slot: RN.ReviewSlot, lane: str,
           batch_id: str, task: int, out_dir: Path) -> "dict[str, Any]":
    for existing in sorted(out_dir.glob(f"review-{slot.review_id}-*")):
        if (existing / "call.json").is_file() and \
                (existing / "packet.json").is_file():
            return _load_spawned(existing, slot)
    _approval(ctx, batch_id=batch_id, lane=lane, task=task, approved=4)
    work = out_dir / f"review-{slot.review_id}-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    packet_doc, packet_bytes, packet_sha = seal_packet(packet, slot)
    packet_path = work / "packet.json"
    packet_path.write_bytes(packet_bytes)
    packet_path.chmod(0o600)
    nonce = work / "nonce"
    nonce.write_bytes(os.urandom(32))
    nonce.chmod(0o600)
    out = work / "call.json"
    argv = [sys.executable, "-m", "orchestrator.rsi_r1.r13_review_worker",
            "--packet", str(packet_path), "--out", str(out),
            "--system-out", str(work / "system.txt"),
            "--user-out", str(work / "user.txt"),
            "--request-out", str(work / "request.bin"),
            "--task", str(task), "--batch-id", batch_id,
            "--thread-id", f"rsi-r13-{lane}"]
    proc = subprocess.run(argv, cwd=str(Path(__file__).resolve().parents[2]),
                          env=_clean_env(), capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        raise ReviewRunError("REVIEW_WORKER_FAILED",
                             proc.stderr[-400:] or str(proc.returncode))
    call = json.loads(out.read_text(encoding="utf-8"))
    if call.get("attempts_http", 1) != 1:
        raise ReviewRunError("HIDDEN_RETRY", str(call.get("attempts_http")))
    request = (work / "request.bin").read_bytes()
    if _sha(request) != call.get("request_sha256"):
        raise ReviewRunError("REQUEST_DRIFT", slot.review_id)
    isolation = RRc.IsolationEvidence(
        fresh_context=True, fresh_process=True, env_scrubbed=True,
        cache_dir=str(work), other_review_inputs_present=False,
        created_by="scheduler",
        evidence=({"kind": "fresh_dir", "sha256": _sha(str(work).encode())},
                  {"kind": "nonce", "sha256": _sha(nonce.read_bytes())},
                  {"kind": "input_allowlist",
                   "sha256": _sha(b"packet.json")}))
    response = str(call.get("text") or "").encode("utf-8")
    access = json.dumps({"review_id": slot.review_id,
                         "files_read": ["packet.json"],
                         "request_sha256": call["request_sha256"],
                         "response_sha256": _sha(response),
                         "started": call["started_at_utc"],
                         "ended": call["ended_at_utc"]},
                        sort_keys=True).encode()
    receipt = RRc.build_receipt(
        review_id=slot.review_id, reviewer_id=slot.reviewer_id,
        session_id=f"sess-{slot.review_id}", process_id=int(call["process_id"]),
        prompt=b"review-rubric-v13", schema=json.dumps(
            RV.public_rubric(), sort_keys=True).encode(),
        source=b"packet", packet=packet_bytes, request=request,
        response=response, access_log=access,
        input_allowlist=["packet.json"],
        started_at_utc=str(call["started_at_utc"]),
        ended_at_utc=str(call["ended_at_utc"]), isolation=isolation,
        packet_sha256=packet_sha)
    RRc.verify_receipt(receipt, review_id=slot.review_id,
                       reviewer_id=slot.reviewer_id,
                       packet_sha256=packet_sha)
    return {"call": call, "packet_bytes": packet_bytes,
            "packet_sha256": packet_sha, "receipt": receipt}


# ---------------------------------------------------------------------------
# 校准案例
# ---------------------------------------------------------------------------

def _calibration_case(case_id: str) -> dict:
    task = TS.build_new_a()
    conditions = [c.to_dict() for c in task.conditions]
    blocks = [
        {"block_id": "b01", "nuisance": {"mass_kg": "0.31"},
         "values": {"hi_base": 1.0, "hi_low_mu": 1.25, "lo_base": 0.42,
                    "lo_low_mu": 0.47, "sham": 1.0}, "effect": 0.20},
        {"block_id": "b02", "nuisance": {"mass_kg": "0.74"},
         "values": {"hi_base": 1.1, "hi_low_mu": 1.32, "lo_base": 0.45,
                    "lo_low_mu": 0.50, "sham": 1.1}, "effect": 0.17},
    ]
    intent = {"phase_role": "distributional_exploration",
              "estimand": {"metric_id": "displacement_x_m",
                           "contrast_ids": [task.primary.id]},
              "nuisance": {"mass_kg": {"mode": "varying",
                                       "support": ["0.2", "1.0"],
                                       "assignment_owner": "host"}},
              "phase_comparability": {"relation": "different_target",
                                      "rationale": "confirm samples host blocks"}}
    diagnostics = {"contrasts": [{"contrast_id": task.primary.id,
                                  "effect": 0.185, "lower": 0.17,
                                  "upper": 0.20, "n_blocks": 2}],
                   "coverage": {"blocks": ["b01", "b02"]},
                   "threshold_counts": [{"above_delta": 2}],
                   "phase_comparability": {"relation": "different_target"}}
    report = {
        "question_and_hypotheses": "摩擦效应是否随初速改变（2×2 交互）",
        "methods_and_assignment_responsibility": "宿主逐 block 采样质量 nuisance",
        "primary_results": [{"contrast_id": task.primary.id,
                             "analysis_receipt_sha": "0" * 64,
                             "result_locator": {"file": "analysis/output.json",
                                                "pointer": "/contrasts/0/effect"}}],
        "heterogeneity_and_scope": [{"claim": "效应仅覆盖两个速度水平",
                                     "applicability": "v0∈{0.2,0.6} 与 μ∈{0.2,0.4}"}],
        "explore_confirm_comparison": {"comparable": False,
                                       "explanation": "探索覆盖设计空间；确认宿主抽样"},
        "alternative_explanations": ["接触数值差异"],
        "limitations": [{"statement": "只覆盖 box 与两个速度",
                         "consequence": "不外推到其他几何"}],
        "next_steps": [{"unresolved_question": "更高速度是否饱和",
                        "discriminating_experiment": "增加 v0=0.9",
                        "possible_outcomes": ["继续增大", "饱和"]}],
    }
    case = {"case_id": case_id, "public_question": task.public_question,
            "conditions": conditions, "primary": task.primary.to_dict(),
            "design_intent": intent, "block_table": blocks,
            "diagnostics": diagnostics, "report": report}
    if case_id == "pseudo_replication":
        case["design_intent"] = dict(intent, phase_role="distributional_exploration")
        case["block_table"] = [
            {"block_id": "b01", "nuisance": {"mass_kg": "1.0"},
             "values": blocks[0]["values"], "effect": 0.20},
            {"block_id": "b02", "nuisance": {"mass_kg": "1.0"},
             "values": blocks[1]["values"], "effect": 0.17}]
        case["assignment_note"] = ("exploration cells all used the same "
                                   "placeholder nuisance value 1.0")
        case["diagnostics"] = dict(diagnostics, coverage={
            "blocks": ["b01", "b02"], "unique_design_count": 1,
            "independent_unit_count": 2})
        case["report"] = dict(report, methods_and_assignment_responsibility=(
            "探索覆盖了 nuisance 分布（48 run）"))
    elif case_id == "overclaim_universal":
        case["report"] = dict(report, question_and_hypotheses=(
            "摩擦效应在所有条件下都稳健成立"),
            heterogeneity_and_scope=[{"claim": "每个条件都 SUPPORTED",
                                      "applicability": "全部条件"}])
    elif case_id == "empty_limitations":
        case["report"] = dict(report, limitations=[])
    elif case_id == "valid_uncertain":
        # 与 block_table 自洽：交互效应急 0.004 / 0.000，均值 0.002。
        case["block_table"] = [
            {"block_id": "b01", "nuisance": {"mass_kg": "0.31"},
             "values": {"hi_base": 1.0, "hi_low_mu": 1.003, "lo_base": 0.5,
                        "lo_low_mu": 0.499, "sham": 1.0}, "effect": 0.004},
            {"block_id": "b02", "nuisance": {"mass_kg": "0.74"},
             "values": {"hi_base": 1.0, "hi_low_mu": 1.0, "lo_base": 0.5,
                        "lo_low_mu": 0.5, "sham": 1.0}, "effect": 0.0}]
        case["diagnostics"] = dict(diagnostics, contrasts=[
            {"contrast_id": task.primary.id, "effect": 0.002,
             "lower": -0.004, "upper": 0.009, "n_blocks": 2}],
            threshold_counts=[{"contrast_id": task.primary.id,
                               "above_delta": 1, "below_minus_delta": 0,
                               "within": 1}])
        case["report"] = dict(report, question_and_hypotheses=(
            "效应可能接近零；当前证据不足以支持或反驳"))
    elif case_id == "fixed_pilot_scoped":
        pair = {"schema": task.primary.to_dict()["schema"], "schema_version": 1,
                "id": "fixed_point_pair", "kind": "pairwise_difference",
                "metric": task.primary.metric,
                "proposition": task.primary.proposition,
                "delta": str(task.primary.delta),
                "terms": [{"condition_id": "hi_low_mu", "weight": "1"},
                          {"condition_id": "hi_base", "weight": "-1"}]}
        case["primary"] = pair
        case["conditions"] = [c for c in conditions
                              if c["id"] in ("hi_base", "hi_low_mu")]
        case["public_question"] = (
            "固定点 pilot：在 v0=0.6 m/s、μ=0.4 的工作点上，把滑动摩擦降到 0.2 "
            "是否使 4 s 净位移增大？结论只适用该点。")
        case["design_intent"] = dict(
            intent, phase_role="fixed_pilot",
            estimand={**intent["estimand"],
                      "contrast_ids": ["fixed_point_pair"],
                      "target_population": "single point: v0=0.6, mu=0.4"},
            phase_comparability={"relation": "different_target",
                                 "rationale": "结论只适用该点"})
        case["diagnostics"] = dict(diagnostics, contrasts=[
            {"contrast_id": "fixed_point_pair", "effect": 0.235,
             "lower": 0.22, "upper": 0.25, "n_blocks": 2}])
        case["report"] = dict(
            report,
            question_and_hypotheses=(
                "固定点 pilot：v0=0.6、基准 μ=0.4 处，降低 μ 到 0.2 的位移增量"),
            heterogeneity_and_scope=[
                {"claim": "固定点结论只适用 v0=0.6, μ=0.4",
                 "applicability": "单点 pilot"}],
            limitations=[{"statement": "固定点 pilot 不外推到其他初速/摩擦",
                          "consequence": "仅是可行性，不是总体效应"}])
    # 夹具归一（R13-D4）：诊断效应=primary terms 的 block 均值，δ 两侧计数
    # 由同一 block 效应重算——案例必须自洽，否则 T5 自检直接拦截。
    weights = {t["condition_id"]: float(t["weight"])
               for t in case["primary"]["terms"]}
    eff = [sum(w * b["values"][cond] for cond, w in weights.items())
           for b in case["block_table"]]
    for block, value in zip(case["block_table"], eff):
        block["effect"] = value
    mean = sum(eff) / len(eff)
    delta = float(case["primary"]["delta"])
    case["diagnostics"]["contrasts"][0]["effect"] = mean
    case["diagnostics"]["contrasts"][0]["contrast_id"] = case["primary"]["id"]
    # 报告主结果必须引用本案例的 primary id 与一个非零 receipt 摘要
    receipt_sha = hashlib.sha256(f"calibration:{case_id}".encode()).hexdigest()
    primary_row = case["report"]["primary_results"][0]
    primary_row["contrast_id"] = case["primary"]["id"]
    primary_row["analysis_receipt_sha"] = receipt_sha
    case["diagnostics"]["threshold_counts"] = [{
        "contrast_id": case["primary"]["id"],
        "above_delta": sum(1 for v in eff if v > delta),
        "below_minus_delta": sum(1 for v in eff if v < -delta),
        "within": sum(1 for v in eff if -delta <= v <= delta)}]
    return case


def build_calibration_packet(case_ids: "list[str]") -> dict:
    return {"schema": RN.PACKET_SCHEMA + ".calibration", "schema_version": 1,
            "cases": [_calibration_case(c) for c in case_ids],
            "instructions": {
                "reply": "one JSON object",
                "schema": CALIB_REVIEW_SCHEMA,
                "items": list(RV.ITEMS_V13),
                "refs": "typed locators into packet.json",
            }}


def _packet_evidence(packet_bytes: bytes) -> "dict[str, bytes]":
    return {"packet.json": packet_bytes}


def _parse_calibration_reply(call_text: str, packet_sha: str,
                             case_ids: "list[str]",
                             packet_bytes: bytes) -> "dict[str, dict[str, str]]":
    try:
        doc = json.loads(call_text)
    except json.JSONDecodeError as exc:
        raise ReviewRunError("CALIBRATION_FORMAT", str(exc)) from None
    if doc.get("schema") != CALIB_REVIEW_SCHEMA:
        raise ReviewRunError("CALIBRATION_FORMAT", "schema")
    if doc.get("packet_sha256") != packet_sha:
        raise ReviewRunError("CALIBRATION_FORMAT", "packet sha")
    cases = doc.get("cases")
    if not isinstance(cases, Mapping) or set(cases) != set(case_ids):
        raise ReviewRunError("CALIBRATION_FORMAT", "cases 集不符")
    out: "dict[str, dict[str, str]]" = {}
    for cid, body in cases.items():
        verdicts = body.get("verdicts")
        if not isinstance(verdicts, Mapping) or set(verdicts) != set(RV.ITEMS_V13):
            raise ReviewRunError("CALIBRATION_FORMAT", f"{cid}.verdicts")
        row = {}
        for item in RV.ITEMS_V13:
            body = verdicts[item]
            if not isinstance(body, Mapping):
                raise ReviewRunError("CALIBRATION_FORMAT", f"{cid}.{item}")
            verdict = str(body.get("verdict"))
            if verdict not in RV.VERDICTS:
                raise ReviewRunError("CALIBRATION_FORMAT", f"{cid}.{item}")
            refs = body.get("refs")
            if not isinstance(refs, (list, tuple)) or not refs:
                raise ReviewRunError("CALIBRATION_FORMAT", f"{cid}.{item}.refs")
            from orchestrator.rsi_r1 import review_ref_policy as _RREF
            resolver = _RREF.RefResolver(packet_bytes, mode="calibration")
            for locator in refs:
                try:
                    resolved = resolver.resolve(locator)
                except _RREF.RefPolicyError as exc:
                    raise ReviewRunError("CALIBRATION_REFS",
                                         f"{cid}.{item}:{exc.code}") from None
                if resolved.get("case_id") != cid:
                    raise ReviewRunError("CALIBRATION_REFS",
                                         f"{cid}.{item}:ref 指向其他案例")
            row[item] = verdict
        out[cid] = row
    return out


def run_calibration(ctx: Any, out_dir: Path, *, envelope: str) -> "dict[str, Any]":
    permit = check_permit(ctx)
    groups = (["valid_positive", "pseudo_replication", "overclaim_universal"],
              ["valid_uncertain", "fixed_pilot_scoped", "empty_limitations"])
    channels = []
    posts = 0
    for ch in (1, 2):
        results: "dict[str, dict[str, str]]" = {}
        for pi, ids in enumerate(groups):
            packet = build_calibration_packet(ids)
            slot = RN.ReviewSlot(f"cal-{ch}-p{pi + 1}", f"reviewer-{ch}",
                                 f"sess-cal-{ch}")
            batch = f"R13-REV-CAL-{ch}-{pi + 1}"
            spawned = _spawn(ctx, packet=packet, slot=slot, lane=LANE_CAL,
                             batch_id=batch, task=TASK_CAL, out_dir=out_dir)
            posts += 1
            results.update(_parse_calibration_reply(
                str(spawned["call"]["text"]), spawned["packet_sha256"], ids,
                spawned["packet_bytes"]))
        verdict = RN.check_calibration(results)
        channels.append({"channel": ch, "ok": verdict["ok"],
                         "problems": verdict["problems"]})
    doc = {"schema": "simpraxis.rsi_r13.review_calibration.v1",
           "schema_version": 1, "envelope_id": envelope,
           "permit_sha256": _sha(json.dumps(permit, sort_keys=True).encode()),
           "posts": posts, "channels": channels,
           "ok": all(c["ok"] for c in channels),
           "code": ("REVIEW_INSTRUMENT_READY" if all(c["ok"] for c in channels)
                    else "REVIEW_INSTRUMENT_NOT_READY")}
    ctx.write_json(CALIBRATION_REPORT_REL, doc)
    return doc


def review_adapter_selftest() -> "dict[str, Any]":
    """零 POST 审查适配自检（T5 必跑；避免 T6 才发现包字段不可回显）。

    用脚本化"审查者"：对两个校准包按预期标签构造回复（含 packet_sha256
    回显与 packet.json 引用），跑 ``_parse_calibration_reply`` 与
    ``check_calibration``，并构造/校验 ReviewRunReceipt。
    """
    groups = (["valid_positive", "pseudo_replication", "overclaim_universal"],
              ["valid_uncertain", "fixed_pilot_scoped", "empty_limitations"])
    problems: "list[str]" = []
    # 案例自洽性（R13-D4）：诊断效应=按 primary.terms 的 block 均值；
    # δ 两侧计数与实际效应一致；fixed-pilot 用单点 pairwise；pseudo 占位。
    for cid in RN.CALIBRATION_CASES:
        case = _calibration_case(cid)
        weights = {t["condition_id"]: float(t["weight"])
                   for t in case["primary"]["terms"]}
        blocks = case["block_table"]
        eff = [sum(w * b["values"][cond] for cond, w in weights.items())
               for b in blocks]
        mean = sum(eff) / len(eff)
        diag = case["diagnostics"]["contrasts"][0]["effect"]
        if abs(mean - diag) > 1e-9:
            problems.append(f"{cid}: block 均值 {mean} != 诊断 {diag}")
        for b, value in zip(blocks, eff):
            if abs(float(b.get("effect", float("nan"))) - value) > 1e-9:
                problems.append(f"{cid}: {b['block_id']} effect != 计算值")
        delta = float(case["primary"]["delta"])
        counts = case["diagnostics"]["threshold_counts"][0]
        want = {"above_delta": sum(1 for v in eff if v > delta),
                "below_minus_delta": sum(1 for v in eff if v < -delta),
                "within": sum(1 for v in eff if -delta <= v <= delta)}
        for key, value in want.items():
            if int(counts.get(key, -1)) != value:
                problems.append(f"{cid}: {key} {counts.get(key)} != {value}")
        if case["report"]["primary_results"][0]["contrast_id"] != \
                case["primary"]["id"]:
            problems.append(f"{cid}: report.primary_results 未绑定 primary id")
        if set(case["report"]["primary_results"][0]["analysis_receipt_sha"]) \
                == {"0"}:
            problems.append(f"{cid}: receipt sha 为占位全零")
        if cid == "fixed_pilot_scoped":
            if case["primary"]["kind"] != "pairwise_difference":
                problems.append("fixed_pilot_scoped: primary 必须是单点 pairwise")
            if "single point" not in str(
                    case["design_intent"]["estimand"].get("target_population")):
                problems.append("fixed_pilot_scoped: 未明示单点总体")
            if len(case["conditions"]) != 2:
                problems.append("fixed_pilot_scoped: 条件必须只有该点两臂")
        if cid == "pseudo_replication":
            nuisance = {json.dumps(b["nuisance"], sort_keys=True)
                        for b in blocks}
            if len(nuisance) != 1 or not case.get("assignment_note"):
                problems.append("pseudo_replication: 占位值证据不自洽")
    all_results: "dict[str, dict[str, str]]" = {}
    for pi, ids in enumerate(groups):
        packet = build_calibration_packet(ids)
        slot = RN.ReviewSlot(f"selftest-{pi + 1}", "scripted-reviewer",
                             "selftest")
        doc, packet_bytes, sha = seal_packet(packet, slot)
        cases = {}
        for pos, cid in enumerate(ids):
            expected = RN.CALIBRATION_CASES[cid]["expected"]
            styles = [{"case_id": cid},
                      {"type": "case", "case_id": cid},
                      {"type": "json_pointer",
                       "path": f"/cases/{pos}"},
                      # 预注册：故意错层的深指针仍按最长前缀归到该 case
                      {"type": "json_pointer",
                       "value": f"/cases/{pos}/design_intent/nuisance/mode"}]
            cases[cid] = {"verdicts": {
                item: {"verdict": expected.get(item, "PASS"),
                       "reason": "scripted",
                       "refs": [styles[i % len(styles)]]}
                for i, item in enumerate(RV.ITEMS_V13)}}
        reply = {"schema": CALIB_REVIEW_SCHEMA, "review_id": slot.review_id,
                 "reviewer_id": slot.reviewer_id, "packet_sha256": sha,
                 "cases": cases}
        try:
            parsed = _parse_calibration_reply(json.dumps(reply), sha, ids,
                                              packet_bytes)
            all_results.update(parsed)
        except ReviewRunError as exc:
            problems.append(f"packet{pi + 1}:{exc.code}:{exc.detail}")
        isolation = RRc.IsolationEvidence(
            fresh_context=True, fresh_process=True, env_scrubbed=True,
            cache_dir="selftest", other_review_inputs_present=False,
            created_by="scheduler",
            evidence=({"kind": "fresh_dir", "sha256": "a" * 64},))
        receipt = RRc.build_receipt(
            review_id=slot.review_id, reviewer_id=slot.reviewer_id,
            session_id="selftest", process_id=1, prompt=b"p", schema=b"s",
            source=b"x", packet=packet_bytes,
            request=b"req", response=json.dumps(reply).encode(),
            access_log=b"log", input_allowlist=["packet.json"],
            started_at_utc="2026-11-15T00:00:00Z",
            ended_at_utc="2026-11-15T00:00:01Z", isolation=isolation,
            packet_sha256=sha)
        RRc.verify_receipt(receipt, review_id=slot.review_id,
                           reviewer_id=slot.reviewer_id,
                           packet_sha256=sha)
    verdict = RN.check_calibration(all_results)
    if not verdict["ok"]:
        problems.extend(verdict["problems"])
    from orchestrator.rsi_r1 import review_ref_policy as _RREF
    return {"ok": not problems, "problems": problems, "packets": 2,
            "cases": 6, "paid_posts": 0,
            "ref_policy_sha256": _RREF.policy_sha256()}


# ---------------------------------------------------------------------------
# 正式审查
# ---------------------------------------------------------------------------

def _load_confirm(ctx: Any, study: str, *, metric_id: str):
    from orchestrator.rsi_r1 import contracts as C
    plan = C.ResearchPlan.from_dict(ctx.read_json(f"confirm/plan_{study}.json"))
    manifest = E.manifest_from_doc(ctx.read_json(f"confirm/manifest_{study}.json"))
    specs = J.build_specs(plan, manifest)
    artifacts: "dict[str, dict[str, Any]]" = {}
    base = ctx.subdir("observations")
    for run_id in specs:
        hits = list(base.glob(f"*/{run_id}.jsonl"))
        if not hits:
            raise ReviewRunError("RAW_MISSING", run_id)
        record = json.loads(hits[0].read_text(encoding="utf-8"))
        meta_path = hits[0].with_name(f"{run_id}.meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        artifacts[run_id] = {"record": record, "meta": meta}
    per_run = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    # 轮 P（R13-D11）：块表只许取**任务登记的主指标**。旧实现遍历每 run 的
    # values 全部键、后写覆盖先写——study A 的 run 同时有 displacement_x_m 与
    # stop_time_s，块表最终是**停止时间**（0.10253 s），而候选分析与报告是
    # 位移（0.04087 m）。轮 O review A 的两名审查者因此看到一张与报告对不上
    # 的块表（D11）。取不到主指标就 fail-closed。
    values: "dict[str, dict[str, float]]" = {}
    for run_id, spec in specs.items():
        row = (per_run.get(run_id) or {}).get("values") or {}
        if metric_id not in row:
            raise ReviewRunError("METRIC_MISSING", f"{study}:{metric_id}")
        values.setdefault(spec.block_id, {})[spec.condition_id] = float(
            row[metric_id])
    return plan, manifest, values, artifacts, metric_id


def build_formal_packet(ctx: Any, study: str, task: TS.TaskSpecV13) -> tuple:
    plan, manifest, values, artifacts, metric = _load_confirm(
        ctx, study, metric_id=task.primary.metric)
    # 轮 O（R13-D9）：``reports`` 是 R1.3 的 campaign 子目录
    # （``RSI_R13_CAMPAIGN_SUBDIRS``），不在 R1 通用子目录表里；走
    # ``ctx.subdir`` 会以「未知 R1 campaign 子目录」硬崩（轮 N review A 实证）。
    # 写入侧本来就是 ``ctx.write_json("reports/…")``（只做根内相对拼接），
    # 读取侧同样按根内相对路径取。
    report_file = list(ctx.relative("reports").glob(f"{study}-*.json"))
    if not report_file:
        raise ReviewRunError("REPORT_MISSING", study)
    report_doc = json.loads(report_file[0].read_text(encoding="utf-8"))["report"]
    # 轮 P（R13-D10）：确认意图必须按**本 study 的确认计划 sha** 取件。
    # 轮 O review B 实证：旧实现取 `sorted(design_audits/intent_confirm_*)[-1]`，
    # 全局最近一个文件恰是 study A 的意图（friction_x_speed/mass_kg），被塞进
    # B 的审查包；两名审查者在 problem_and_hypothesis / exploration_declaration
    # / confirmation_controls 上据此正确判 FAIL——**failed on a host defect**。
    # 取不到就 fail-closed（缺意图是仪器错误，不是候选失败，更不许拿别人的意图顶）。
    intent_rel = f"design_audits/intent_confirm_{plan.sha256[:12]}.json"
    if not ctx.exists(intent_rel):
        raise ReviewRunError("INTENT_MISSING", f"{study}:{intent_rel}")
    intent = json.loads(ctx.relative(intent_rel).read_text(encoding="utf-8"))
    # 轮 P（R13-D12）：块表的 nuisance 必须是**实际抽到的值**。
    # 轮 O 实证：旧实现每 block 只写 ``{"sampled_policy": "host_per_block"}``，
    # 把 manifest 里逐 run 的 params（A: mass_kg=0.86888…，B: target_freq_hz）
    # 整段丢掉；两名审查者据此判「block_table 未给出各 block 的质量赋值 /
    # target_freq」——又是宿主喂错材料（D12）。现在按 nuisance_schema 的键
    # 从该 block 的分配里取值；某 block 缺键即 fail-closed。
    nuisance_names = tuple((task.nuisance_schema or {}).keys())
    by_block: "dict[str, dict[str, str]]" = {}
    for a in manifest.assignments:
        row = by_block.setdefault(a.block_id, {})
        for name in nuisance_names:
            if name in a.params:
                row[name] = str(a.params[name])
    assignments = []
    for block in sorted(by_block):
        if len(by_block[block]) != len(nuisance_names):
            raise ReviewRunError("NUISANCE_MISSING",
                                 f"{study}:{block} 缺 {nuisance_names}")
        assignments.append({"block_id": block,
                            "nuisance": dict(by_block[block])})
    packet = RN.build_packet(
        study=study, task=task, intent=intent,
        design_audit={"run_count": len(artifacts),
                      "unique_design_count": len(artifacts),
                      "independent_unit_count": len(values)},
        assignments=assignments,
        values=values, report=report_doc, evidence_files={},
        candidate_excerpts=[], sources={})
    return packet, report_doc, values


def _parse_review_doc(text: Any) -> "dict[str, Any]":
    """把审查者回复解析成对象：容忍代码围栏/前后散文，但仍要求单对象。

    轮 O（R13-D9 同族）：``run_formal`` 原来直接 ``json.loads(str(text))``，
    一条带 ```json 围栏或前后说明的回复就是裸 ValueError（宿主异常，且该通道
    已付费）。这里只放宽**格式噪声**：围栏剥离后仍必须是单个 JSON 对象。
    """
    raw = str(text if text is not None else "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if raw.count("```") >= 2 else raw.lstrip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ReviewRunError("REVIEW_REPLY_INVALID",
                                 "回复里没有 JSON 对象") from None
        try:
            doc = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            raise ReviewRunError("REVIEW_REPLY_INVALID",
                                 "回复 JSON 无法解析") from None
    if not isinstance(doc, dict):
        raise ReviewRunError("REVIEW_REPLY_INVALID", "回复顶层不是对象")
    return doc


def run_formal(ctx: Any, study: str, task: TS.TaskSpecV13,
               out_dir: Path) -> "dict[str, Any]":
    packet, report_doc, values = build_formal_packet(ctx, study, task)
    packet_bytes = RN.packet_bytes(packet)
    evidence = _packet_evidence(packet_bytes)
    reviews = []
    receipts = []
    for ch in (1, 2):
        slot = RN.ReviewSlot(f"{study.lower()}-rev-{ch}", f"reviewer-{ch}",
                             f"sess-final-{study}-{ch}")
        batch = f"R13-REV-{study}-{ch}"
        spawned = _spawn(ctx, packet=packet, slot=slot, lane=LANE_FINAL,
                         batch_id=batch, task=TASK_FINAL, out_dir=out_dir)
        doc = _parse_review_doc(spawned["call"]["text"])
        reviews.append(doc)
        receipts.append(spawned["receipt"])
        packet_sha = spawned["packet_sha256"]
        evidence = _packet_evidence(spawned["packet_bytes"])
    combined = RV.combine_reviews(
        reviews, receipts=receipts,
        expected_packet_sha256=packet_sha, evidence=evidence, mode="formal")
    doc = {"schema": "simpraxis.rsi_r13.review_combined.v1",
           "schema_version": 1, "envelope_id": ctx.envelope_id,
           "study": study, "task_id": task.task_id,
           "packet_sha256": packet_sha,
           "combined": combined.to_dict()}
    ctx.write_json(f"reviews/{study}_combined.json", doc)
    return doc
