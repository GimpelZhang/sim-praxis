"""RSI-R1.2 T4：候选分析有效性三门（AC37–AC41、AC51、AC52 的单元对抗面）。

链路只到 judge/凭据层：``analysis_receipt`` 校验来源/视图/执行 → ``parse_output``
校验结构与 run 身份 → ``judge_study_r12`` 从可信 raw 独立重算并逐字段比较。
合成 raw 只用于单元对抗；真实物理正控由 T5 生产 e2e 提供（Plan §5.1 末段）。

八组固定负控 ①–⑧ 必须全部拒绝，正控必须通过。
"""
from __future__ import annotations

import copy
import json

import pytest

from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import sandbox as SB
from tests import rsi_r1_helpers as H

KEEPER = "b" * 64
SOURCE_SHA = "c" * 64
REPLY_ID = "reply-1"
ACTION_ID = "action-1"
EXEC_ID = "exec-1"


# ---------------------------------------------------------------------------
# 合成确认批与宿主独立重算
# ---------------------------------------------------------------------------

def _plan() -> C.ResearchPlan:
    return H.doc_of(H.plan_doc("A"))


def _a_record(spec: E.ExperimentSpec, *, displacement: float) -> dict:
    times = [i * 0.001 for i in range(6)]
    return {
        "time_s": times,
        "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0]
                 for i in range(len(times))],
        "qvel": [[0.5, 0.0] for _ in times],
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def _artifacts(plan: C.ResearchPlan,
               manifest: E.ConfirmManifest) -> "dict[str, dict]":
    out = {}
    for run_id, spec in J.build_specs(plan, manifest).items():
        disp = -0.01 if spec.condition_id == "low_mu" else 0.0
        out[run_id] = {"record": _a_record(spec, displacement=disp),
                       "meta": {"run_id": run_id, "spec_hash": spec.spec_hash,
                                "condition_id": spec.condition_id,
                                "initial_x_m": 0.0}}
    return out


def _host(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
          artifacts: "dict[str, dict]") -> "dict[str, dict]":
    specs = J.build_specs(plan, manifest)
    per_run = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    return {c.id: J._judge_contrast(plan, per_run, c, [])
            for c in J._contrasts(plan)}


def _output_doc(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                host: "dict[str, dict]") -> "dict":
    specs = J.build_specs(plan, manifest)
    contrasts = []
    for cid, ref in sorted(host.items()):
        contrasts.append({
            "contrast_id": cid, "metric_id": ref["metric_id"],
            "proposition": ref["proposition"], "estimate": ref["effect"],
            "lower": ref["lower"], "upper": ref["upper"],
            "status": ref["status"], "n_blocks": ref["n_blocks"],
        })
    return {"schema": AR.OUTPUT_SCHEMA, "run_ids": sorted(specs),
            "contrasts": contrasts}


def _submission(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                host: "dict[str, dict]") -> "dict":
    hyp = {h.id: h for h in plan.hypotheses}
    claims = []
    for hid, h in sorted(hyp.items()):
        ref = host[h.contrast_id]
        try:
            delta = str(R.metric_delta(ref["metric_id"]))
        except KeyError:
            delta = "0.001"
        claims.append({"hypothesis_id": hid, "proposition": ref["proposition"],
                       "metric_id": ref["metric_id"], "delta": delta,
                       "estimate": ref["effect"], "lower": ref["lower"],
                       "upper": ref["upper"]})
    return {"schema": J.SUBMISSION_SCHEMA, "study_id": plan.study_id,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": plan.primary.id, "claims": claims}


def _inputs(plan: C.ResearchPlan, manifest: E.ConfirmManifest) -> "list[dict]":
    return [{"run_id": rid,
             "relative_path": f"observations/confirm_A/{rid}.jsonl",
             "sha256": "1" * 64}
            for rid in sorted(J.build_specs(plan, manifest))]


def _receipt(doc: dict, inputs: "list[dict]", *, plan: C.ResearchPlan,
             manifest: E.ConfirmManifest, output_bytes: bytes,
             producer: str = AR.PRODUCER_CANDIDATE, phase: str = "confirm",
             exit_code: int = 0, timed_out: bool = False,
             source_sha: str = SOURCE_SHA) -> "dict":
    return AR.build_receipt(
        profile_id="rsi-r1.2", study_id=plan.study_id, phase=phase,
        plan_sha256=plan.sha256, confirm_manifest_sha256=manifest.sha256,
        source_sha256=source_sha, registered_reply_id=REPLY_ID,
        registered_action_id=ACTION_ID, producer=producer, inputs=inputs,
        execution_id=EXEC_ID, sandbox_status="ok", exit_code=exit_code,
        timed_out=timed_out, output_bytes=output_bytes, output_doc=doc,
        validity=True)



def _expect(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
            inputs: "list[dict]", output_bytes: bytes, *,
            source_file_sha: str = SOURCE_SHA, producer: str = "candidate",
            phase: str = "confirm") -> AR.ReceiptExpectation:
    return AR.ReceiptExpectation(
        study_id=plan.study_id, phase=phase, plan_sha256=plan.sha256,
        source_sha256=SOURCE_SHA, source_file_sha256=source_file_sha,
        producer=producer, registered_reply_id=REPLY_ID,
        registered_action_id=ACTION_ID,
        confirm_manifest_sha256=manifest.sha256,
        allowed_run_ids=tuple(sorted(J.build_specs(plan, manifest))),
        expected_inputs=tuple(inputs), output_bytes=output_bytes,
        execution_id=EXEC_ID)


def _gate(plan, manifest, artifacts, doc, *, receipt=None, expect=None,
          submission=None, expect_ok: "bool | None" = None) -> "dict":
    """跑完整三门：凭据 → 结构 → 数值/claim 判决。"""
    output_bytes = json.dumps(doc, sort_keys=True).encode("utf-8")
    inputs = _inputs(plan, manifest)
    rcpt = receipt if receipt is not None else _receipt(
        doc, inputs, plan=plan, manifest=manifest, output_bytes=output_bytes)
    exp = expect if expect is not None else _expect(
        plan, manifest, inputs, output_bytes)
    ok, reasons = AR.validate_receipt(rcpt, exp)
    if expect_ok is not None:
        assert ok is expect_ok, (ok, reasons)
    output = None
    output_error = ""
    try:
        output = AR.parse_output(
            doc, expected_run_ids=sorted(J.build_specs(plan, manifest)),
            expected_contrasts=[c.id for c in J._contrasts(plan)])
    except AR.ReceiptError as exc:
        output_error = exc.code
    return J.judge_study_r12(
        plan=plan, manifest=manifest, artifacts=artifacts,
        submission=(submission if submission is not None
                    else _submission(plan, manifest, _host(plan, manifest,
                                                           artifacts))),
        receipt_ok=ok, receipt_reasons=reasons, output=output,
        output_error=output_error)


# ---------------------------------------------------------------------------
# 正控：合法程序 + 正确数值 + 一致 claim 必须通过
# ---------------------------------------------------------------------------

def test_positive_control_full_gate_passes() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    doc = _output_doc(plan, manifest, _host(plan, manifest, artifacts))
    report = _gate(plan, manifest, artifacts, doc)
    assert report["verdict"] == "JUDGE_OK", report["reason_codes"]
    assert report["layers"]["receipt_valid"] is True
    assert report["layers"]["numerics_valid"] is True
    assert report["layers"]["claim_status"] == "MATCH"


# ---------------------------------------------------------------------------
# ① 崩溃/超时 + 外部正确 claim：宿主不得替其过关
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
def test_control_1_crash_or_timeout_with_correct_claim_rejected(
        exit_code: int, timed_out: bool) -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    doc = _output_doc(plan, manifest, host)          # 数值"恰好正确"
    output_bytes = json.dumps(doc, sort_keys=True).encode("utf-8")
    inputs = _inputs(plan, manifest)
    rcpt = _receipt(doc, inputs, plan=plan, manifest=manifest,
                    output_bytes=output_bytes, exit_code=exit_code,
                    timed_out=timed_out)
    exp = _expect(plan, manifest, inputs, output_bytes)
    ok, reasons = AR.validate_receipt(rcpt, exp)
    assert ok is False
    wanted = "EXIT_CODE_MISMATCH" if exit_code else "TIMEOUT_MISMATCH"
    assert wanted in reasons
    report = J.judge_study_r12(
        plan=plan, manifest=manifest, artifacts=artifacts,
        submission=_submission(plan, manifest, host),
        receipt_ok=ok, receipt_reasons=reasons, output=None,
        output_error="ANALYSIS_EXIT_NONZERO")
    assert report["verdict"] == "JUDGE_REJECT"
    assert "INVALID_ANALYSIS" in report["reason_codes"]


# ---------------------------------------------------------------------------
# ② 0 exit 但空对象 / 只有 n_runs：结构拒绝
# ---------------------------------------------------------------------------

def test_control_2_exit0_empty_or_only_n_runs_rejected() -> None:
    plan = _plan()
    run_ids = sorted(J.build_specs(plan, E.allocate_confirm(plan, KEEPER)))
    contrasts = [c.id for c in J._contrasts(plan)]
    with pytest.raises(AR.ReceiptError) as exc:
        AR.parse_output({}, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc.value.code == "OUTPUT_SCHEMA_MISMATCH"
    only_runs = {"schema": AR.OUTPUT_SCHEMA, "run_ids": run_ids}
    with pytest.raises(AR.ReceiptError) as exc2:
        AR.parse_output(only_runs, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc2.value.code == "OUTPUT_CONTRASTS_MISSING"


# ---------------------------------------------------------------------------
# ③ 错误候选数值 + 正确 claim：claim 不能救数值
# ---------------------------------------------------------------------------

def test_control_3_wrong_numbers_with_correct_claim_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    doc = _output_doc(plan, manifest, host)
    doc["contrasts"][0]["estimate"] = float(doc["contrasts"][0]["estimate"]) + 0.05
    report = _gate(plan, manifest, artifacts, doc,
                   submission=_submission(plan, manifest, host))
    assert report["verdict"] == "JUDGE_REJECT"
    assert "INVALID_NUMERICS" in report["reason_codes"]
    assert report["layers"]["numerics_valid"] is False
    # claim 与宿主正确值一致，但必须与**候选自己的数值**一致——两条都不满足
    # 也照样不救：数值门先拒，claim 门独立再拒。
    assert report["layers"]["claim_status"] in ("MATCH", "CLAIM_INVALID")


def test_control_3b_ci_out_of_tolerance_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    doc = _output_doc(plan, manifest, host)
    ref = doc["contrasts"][0]
    ref["lower"] = float(ref["lower"]) + 1e-3
    report = _gate(plan, manifest, artifacts, doc)
    assert report["verdict"] == "JUDGE_REJECT"
    assert "INVALID_NUMERICS" in report["reason_codes"]


# ---------------------------------------------------------------------------
# ④ 同数量错集合 / 增 / 漏 / 重复：身份拒绝
# ---------------------------------------------------------------------------

def test_control_4_wrong_run_set_of_same_size_rejected() -> None:
    plan = _plan()
    run_ids = sorted(J.build_specs(plan, E.allocate_confirm(plan, KEEPER)))
    contrasts = [c.id for c in J._contrasts(plan)]
    swapped = run_ids[:-1] + ["run_not_in_manifest"]
    doc = {"schema": AR.OUTPUT_SCHEMA, "run_ids": swapped, "contrasts": []}
    with pytest.raises(AR.ReceiptError) as exc:
        AR.parse_output(doc, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc.value.code in ("OUTPUT_RUN_IDS_INCOMPLETE",)


def test_control_4b_extra_and_duplicate_run_ids_rejected() -> None:
    plan = _plan()
    run_ids = sorted(J.build_specs(plan, E.allocate_confirm(plan, KEEPER)))
    contrasts = [c.id for c in J._contrasts(plan)]
    extra = {"schema": AR.OUTPUT_SCHEMA, "run_ids": run_ids + ["ghost"],
             "contrasts": []}
    with pytest.raises(AR.ReceiptError) as exc:
        AR.parse_output(extra, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc.value.code == "OUTPUT_RUN_IDS_EXTRA"
    dup = {"schema": AR.OUTPUT_SCHEMA,
           "run_ids": run_ids[:-1] + [run_ids[0]], "contrasts": []}
    with pytest.raises(AR.ReceiptError) as exc2:
        AR.parse_output(dup, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc2.value.code == "OUTPUT_RUN_IDS_DUPLICATE"


def test_control_4c_missing_and_unknown_contrast_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    doc = _output_doc(plan, manifest, host)
    run_ids = sorted(J.build_specs(plan, manifest))
    contrasts = [c.id for c in J._contrasts(plan)]
    dropped = copy.deepcopy(doc)
    dropped["contrasts"] = dropped["contrasts"][:-1]
    with pytest.raises(AR.ReceiptError) as exc:
        AR.parse_output(dropped, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc.value.code == "OUTPUT_CONTRAST_INCOMPLETE"
    unknown = copy.deepcopy(doc)
    unknown["contrasts"][0]["contrast_id"] = "not-a-contrast"
    with pytest.raises(AR.ReceiptError) as exc2:
        AR.parse_output(unknown, expected_run_ids=run_ids,
                        expected_contrasts=contrasts)
    assert exc2.value.code == "OUTPUT_CONTRAST_UNKNOWN"


# ---------------------------------------------------------------------------
# ⑤ 其他 study / 探索 / 旧 confirm 替代：身份与视图拒绝
# ---------------------------------------------------------------------------

def test_control_5_other_study_or_old_manifest_or_explore_phase_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    plan_b = H.doc_of(H.plan_doc("B"))
    manifest_b = E.allocate_confirm(plan_b, KEEPER)
    artifacts = _artifacts(plan, manifest)
    doc = _output_doc(plan, manifest, _host(plan, manifest, artifacts))
    output_bytes = json.dumps(doc, sort_keys=True).encode("utf-8")
    inputs = _inputs(plan, manifest)
    # 其他 study 的凭据
    other = _receipt(doc, _inputs(plan_b, manifest_b), plan=plan_b,
                     manifest=manifest_b, output_bytes=output_bytes)
    ok, reasons = AR.validate_receipt(
        other, _expect(plan, manifest, inputs, output_bytes))
    assert ok is False and "STUDY_MISMATCH" in reasons
    # 旧 confirm manifest 的凭据
    old = _receipt(doc, inputs, plan=plan,
                   manifest=E.allocate_confirm(plan, "d" * 64),
                   output_bytes=output_bytes)
    ok2, reasons2 = AR.validate_receipt(
        old, _expect(plan, manifest, inputs, output_bytes))
    assert ok2 is False and "MANIFEST_SHA_MISMATCH" in reasons2
    # 探索期凭据冒充确认期
    explore = _receipt(doc, inputs, plan=plan, manifest=manifest,
                       output_bytes=output_bytes, phase="explore")
    ok3, reasons3 = AR.validate_receipt(
        explore, _expect(plan, manifest, inputs, output_bytes))
    assert ok3 is False and "PHASE_MISMATCH" in reasons3


# ---------------------------------------------------------------------------
# ⑥ action / plan / 源码字节任一不一致：绑定拒绝
# ---------------------------------------------------------------------------

def test_control_6_action_plan_source_binding_mismatch_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    doc = _output_doc(plan, manifest, _host(plan, manifest, artifacts))
    output_bytes = json.dumps(doc, sort_keys=True).encode("utf-8")
    inputs = _inputs(plan, manifest)
    rcpt = _receipt(doc, inputs, plan=plan, manifest=manifest,
                    output_bytes=output_bytes)
    exp = _expect(plan, manifest, inputs, output_bytes)
    # action 绑定被换
    tampered = copy.deepcopy(rcpt)
    tampered["registered_action_id"] = "action-other"
    ok, reasons = AR.validate_receipt(tampered, exp)
    assert ok is False and "ACTION_BINDING_MISMATCH" in reasons
    # plan 引用被换
    tampered2 = copy.deepcopy(rcpt)
    tampered2["plan_sha256"] = "e" * 64
    ok2, reasons2 = AR.validate_receipt(tampered2, exp)
    assert ok2 is False and "PLAN_SHA_MISMATCH" in reasons2
    # 实际源码字节漂移（文件 sha ≠ 冻结 sha）
    ok3, reasons3 = AR.validate_receipt(
        rcpt, _expect(plan, manifest, inputs, output_bytes,
                      source_file_sha="f" * 64))
    assert ok3 is False and "SOURCE_FILE_DRIFT" in reasons3


# ---------------------------------------------------------------------------
# ⑦ 完全跳过 analysis 直接复制参考值：无产物即失效
# ---------------------------------------------------------------------------

def test_control_7_skip_analysis_copy_reference_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    report = J.judge_study_r12(
        plan=plan, manifest=manifest, artifacts=artifacts,
        submission=_submission(plan, manifest, host),      # claim 与宿主逐字一致
        receipt_ok=False, receipt_reasons=["ANALYSIS_OUTPUT_MISSING"],
        output=None, output_error="ANALYSIS_OUTPUT_MISSING")
    assert report["verdict"] == "JUDGE_REJECT"
    assert "INVALID_ANALYSIS" in report["reason_codes"]
    assert report["layers"]["receipt_valid"] is False


# ---------------------------------------------------------------------------
# ⑧ 宿主产物冒充 candidate：producer 绑定拒绝
# ---------------------------------------------------------------------------

def test_control_8_host_artifact_posing_as_candidate_rejected() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    doc = _output_doc(plan, manifest, _host(plan, manifest, artifacts))
    output_bytes = json.dumps(doc, sort_keys=True).encode("utf-8")
    inputs = _inputs(plan, manifest)
    rcpt = _receipt(doc, inputs, plan=plan, manifest=manifest,
                    output_bytes=output_bytes,
                    producer=AR.PRODUCER_MAINTAINER)
    ok, reasons = AR.validate_receipt(
        rcpt, _expect(plan, manifest, inputs, output_bytes))
    assert ok is False and "PRODUCER_MISMATCH" in reasons


# ---------------------------------------------------------------------------
# AC41：宿主正确值不得进入公开结果/异常文本
# ---------------------------------------------------------------------------

def test_host_numbers_never_leak_into_report() -> None:
    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _artifacts(plan, manifest)
    host = _host(plan, manifest, artifacts)
    doc = _output_doc(plan, manifest, host)
    doc["contrasts"][0]["estimate"] = 9.87654321
    report = _gate(plan, manifest, artifacts, doc,
                   submission=_submission(plan, manifest, host))
    text = json.dumps(report, sort_keys=True)
    for ref in host.values():
        for field in ("effect", "lower", "upper"):
            value = float(ref[field])
            assert repr(value) not in text
            assert str(value) not in text
    # 报告只报字段名/类别
    assert any("output." in code for code in report["reason_codes"])


# ---------------------------------------------------------------------------
# AC52：phase 由宿主构造；候选不能自报扩权
# ---------------------------------------------------------------------------

def test_analysis_context_cannot_be_widened_by_phase_label() -> None:
    entries = [SB.ViewEntry(relpath=f"observations/b/{i:03d}.jsonl",
                            sha256="0" * 64) for i in range(65)]
    allowed = tuple(f"{i:03d}" for i in range(65))
    explore = SB.AnalysisContext(phase="explore", max_runs=64,
                                 allowed_run_ids=allowed)
    with pytest.raises(SB.SandboxError) as exc:
        explore.check(entries)
    assert exc.value.code == "ANALYSIS_SCOPE"
    confirm = SB.AnalysisContext(phase="confirm", max_runs=192,
                                 allowed_run_ids=allowed)
    confirm.check(entries)                     # 192 帽不截断成 64


# ---------------------------------------------------------------------------
# T5 回归（drill16）：凭据绑定面 = 沙盒原始字节，不是规范化归档件
# ---------------------------------------------------------------------------

def test_confirm_pointer_binds_raw_bytes_not_normalized_doc(tmp_path) -> None:
    """``submit_claim`` 必须按分析指针读**原始输出字节**校验凭据。

    drill16 suite1 的唯一失败点：``AR.build_receipt`` 的 ``output_sha256`` 绑
    沙盒原始字节，旧 ``submit_claim`` 却拿 ``analysis/`` 下的规范化文档（宿主
    重排 contrasts、缩进不同）去校验 ⇒ 恒 ``OUTPUT_SHA_MISMATCH`` ⇒ 两层
    R12-RECEIPT 判 false ⇒ study judge ``INVALID_ANALYSIS``。
    """
    from orchestrator.rsi_r1 import assembly as A

    plan = _plan()
    manifest = E.allocate_confirm(plan, KEEPER)
    inputs = _inputs(plan, manifest)
    raw_doc = {"schema": AR.OUTPUT_SCHEMA,
               "run_ids": sorted(J.build_specs(plan, manifest)),
               "contrasts": []}
    raw = json.dumps(raw_doc, separators=(",", ":")).encode("utf-8")
    norm = (json.dumps(raw_doc, sort_keys=True, indent=1) + "\n").encode("utf-8")
    assert raw != norm                     # 两种序列化逐字节不同

    root = tmp_path / "root"
    H.make_dirs(root)
    ctx = H.make_ctx(root)
    (root / "private" / "analysis").mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "analysis").mkdir(exist_ok=True, mode=0o700)
    (root / "private" / "analysis" / "A-confirm.raw.json").write_bytes(raw)
    (root / "analysis" / "confirm.json").write_bytes(norm)
    sha = AR.sha256_bytes(raw)
    ctx.write_json("private/analysis/A-confirm.json", {
        "schema": "simpraxis.rsi_r1.analysis_pointer.v1", "schema_version": 1,
        "study_id": "A", "phase": "confirm",
        "relpath": "analysis/confirm.json",
        "raw_relpath": "private/analysis/A-confirm.raw.json",
        "sha256": sha, "raw_sha256": sha})

    data, error = A.read_confirm_output(ctx, "A")
    assert (error, data) == ("", raw)

    # 凭据绑原始字节：指针读出的字节校验必过；归档件必挂（旧缺陷形态）
    receipt = _receipt(raw_doc, inputs, plan=plan, manifest=manifest,
                       output_bytes=raw)
    ok, reasons = AR.validate_receipt(
        receipt, _expect(plan, manifest, inputs, data))
    assert ok, reasons
    ok_norm, reasons_norm = AR.validate_receipt(
        receipt, _expect(plan, manifest, inputs, norm))
    assert not ok_norm and "OUTPUT_SHA_MISMATCH" in reasons_norm

    # 私有区被改：按漂移 fail-closed，不回落归档件凑数
    (root / "private" / "analysis" / "A-confirm.raw.json").write_bytes(raw + b" ")
    drift, err = A.read_confirm_output(ctx, "A")
    assert drift == b"" and err == "ANALYSIS_OUTPUT_DRIFT"


# ---------------------------------------------------------------------------
# D-1 修复（2026-11-13）：模板 → 门的往返正控
# ---------------------------------------------------------------------------

def _template_view(tmp_path):
    """合成一个最小分析视图：plan + 四个 run（两对比 × 6 block）。"""
    import subprocess
    import sys

    from orchestrator.rsi_r1 import protocol as PT
    from orchestrator.rsi_r1 import statistics as STAT

    view = tmp_path / "view"
    out = tmp_path / "out"
    view.mkdir()
    out.mkdir()
    plan = {
        "primary": {"id": "c-main", "kind": "primary",
                    "metric": "displacement_x_m", "proposition": "increase_gt",
                    "delta": "0.01", "condition_ids": ["handling", "control"]},
        "negative_control": {"id": "c-neg", "kind": "negative",
                             "metric": "displacement_x_m",
                             "proposition": "equivalent", "delta": "0.05",
                             "condition_ids": ["noise_a", "noise_b"]},
    }
    (view / "plan-abc123.json").write_text(json.dumps(plan), encoding="utf-8")
    conds = {"handling": 0.05, "control": 0.0, "noise_a": 0.0, "noise_b": 0.002}
    run_ids = []
    diffs = {"c-main": {}, "c-neg": {}}
    for block in range(6):
        for cond, offset in conds.items():
            run_id = f"run-{cond}-{block:02d}"
            run_ids.append(run_id)
            value = 0.10 + offset + 0.001 * block
            rec = {"run_id": run_id, "block_id": f"blk-{block:02d}",
                   "qpos": [[0.0, 0.0], [value, 0.0]]}
            (view / f"{run_id}.jsonl").write_text(
                json.dumps(rec) + "\n", encoding="utf-8")
            (view / f"{run_id}.meta.json").write_text(
                json.dumps({"run_id": run_id, "condition_id": cond,
                            "initial_x_m": 0.0}),
                encoding="utf-8")
            if cond == "handling":
                diffs["c-main"].setdefault(f"blk-{block:02d}", [0.0, 0.0])[0] = value
            elif cond == "control":
                diffs["c-main"].setdefault(f"blk-{block:02d}", [0.0, 0.0])[1] = value
            elif cond == "noise_a":
                diffs["c-neg"].setdefault(f"blk-{block:02d}", [0.0, 0.0])[0] = value
            else:
                diffs["c-neg"].setdefault(f"blk-{block:02d}", [0.0, 0.0])[1] = value

    script = tmp_path / "analysis.py"
    script.write_text(PT.ANALYSIS_TEMPLATE, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(view), str(out)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((out / "analysis.json").read_text(encoding="utf-8"))

    expected = {}
    for cid, level in (("c-main", "0.95"), ("c-neg", "0.90")):
        pairs = {b: tuple(v) for b, v in diffs[cid].items()}
        expected[cid] = STAT.estimate(
            STAT.paired_diffs(pairs), level=level)
    return doc, sorted(run_ids), expected


def test_analysis_template_passes_host_gate(tmp_path):
    """模板照抄必须过门，且数值与宿主公开估计量逐位相同（D-1 回归）。"""
    from orchestrator.rsi_r1 import protocol as PT

    assert AR.OUTPUT_SCHEMA in PT.ANALYSIS_TEMPLATE
    assert "protocol_analysis.v1" not in PT.ANALYSIS_TEMPLATE
    doc, run_ids, expected = _template_view(tmp_path)
    parsed = AR.parse_output(doc, expected_run_ids=run_ids,
                             expected_contrasts=["c-main", "c-neg"])
    assert parsed["run_ids"] == run_ids
    assert set(parsed["contrasts"]) == {"c-main", "c-neg"}
    for cid, want in expected.items():
        got = parsed["contrasts"][cid]
        assert got["estimate"] == want["effect"]
        assert got["lower"] == want["lower"]
        assert got["upper"] == want["upper"]
        assert got["n_blocks"] == want["n_blocks"]


def test_prompt_names_output_schema_not_contract_schema():
    """D-2 回归（2026-11-13 实测）：prompt 只能指向 output_schema。"""
    from orchestrator.rsi_r1 import protocol as PT

    block = PT._SOURCE_TEMPLATES_BLOCK
    assert "analysis_output.output_schema" in block
    # 旧误导措辞（把合同自身 schema 当输出 schema 指给候选）不得复现
    assert "（= 公开合同 `analysis_output.schema`" not in block
    assert AR.OUTPUT_SCHEMA in block
