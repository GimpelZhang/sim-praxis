"""RSI-R1 T4：judge 三层判决（AC21–AC23 的证据面）。

全部离线：合成 raw（不是物理仿真），宿主分配件由 ``allocate_confirm`` 生成。
"""
from __future__ import annotations

import copy
import math

import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import statistics as ST
from tests import rsi_r1_helpers as H

KEEPER = "b" * 64


# ---------------------------------------------------------------------------
# 合成 raw
# ---------------------------------------------------------------------------

def _plan(study: str, **kw) -> C.ResearchPlan:
    return H.doc_of(H.plan_doc(study, **kw))


def _a_record(spec: E.ExperimentSpec, *, displacement: float) -> dict:
    times = [i * 0.001 for i in range(6)]
    return {
        "time_s": times,
        "qpos": [[displacement if i == len(times) - 1 else 0.0, 0.0] for i in range(len(times))],
        "qvel": [[0.5, 0.0] for _ in times],
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def _b_record(spec: E.ExperimentSpec, *, rmse: float) -> dict:
    amp = float(spec.physical_params["target_amplitude_rad"])
    times = [i * 0.5 for i in range(9)]          # 0.0 … 4.0，窗口内 8 点
    return {
        "time_s": times,
        "qpos": [[rmse * amp, 0.0] for _ in times],
        "target_q": [[0.0, 0.0] for _ in times],
        "instability_events": [],
        "run_id": spec.run_id, "spec_hash": spec.spec_hash,
        "block_id": spec.block_id,
    }


def _study_a_artifacts(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                       *, diffs: "dict[str, float] | None" = None
                       ) -> "dict[str, dict]":
    """``base`` 位移 0；``low_mu`` 位移 = ``diffs[block]``（缺省 −0.01）。

    ``sham`` 与 ``base`` 位移相同（阴性控制真等效）。
    """
    specs = J.build_specs(plan, manifest)
    out = {}
    for run_id, spec in specs.items():
        per_block = (diffs or {}).get(spec.block_id, -0.01)
        if spec.condition_id == "low_mu":
            disp = -per_block          # 配对差 = base − low_mu = per_block
        else:                          # base / sham / heavy：位移 0
            disp = 0.0
        out[run_id] = {"record": _a_record(spec, displacement=disp),
                       "meta": {"run_id": run_id, "spec_hash": spec.spec_hash,
                                "condition_id": spec.condition_id,
                                "initial_x_m": 0.0}}
    return out


def _study_b_artifacts(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                       *, effect: float = 0.02) -> "dict[str, dict]":
    specs = J.build_specs(plan, manifest)
    out = {}
    for run_id, spec in specs.items():
        rmse = 1.0 + (effect if spec.condition_id == "base" else 0.0)
        out[run_id] = {"record": _b_record(spec, rmse=rmse),
                       "meta": {"run_id": run_id, "spec_hash": spec.spec_hash,
                                "condition_id": spec.condition_id}}
    return out


def _claim(hypothesis_id: str, proposition: str, metric_id: str, delta: str,
           estimate: float, lower: float, upper: float) -> dict:
    return {"hypothesis_id": hypothesis_id, "proposition": proposition,
            "metric_id": metric_id, "delta": delta, "estimate": estimate,
            "lower": lower, "upper": upper}


def _submission(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                report: dict) -> dict:
    """从 judge 自己的分层结果构造一份"完美候选"提交（测 MATCH 面）。"""
    by_contrast = {c["id"]: c for c in report["layers"]["scientific"]}
    hyp = {h.id: h for h in plan.hypotheses}
    claims = []
    for hid, h in sorted(hyp.items()):
        c = by_contrast[h.contrast_id]
        claims.append(_claim(hid, c["proposition"], c["metric_id"], c["delta"],
                             c["effect"], c["lower"], c["upper"]))
    return {"schema": J.SUBMISSION_SCHEMA, "study_id": plan.study_id,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": plan.primary.id, "claims": claims}


# ---------------------------------------------------------------------------
# A：三层同过
# ---------------------------------------------------------------------------

def test_study_a_judge_ok_with_matching_submission() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    # 没有提交件：结构层不过，但科学层的命题状态照样独立给出
    assert report["layers"]["schema_accepted"] is False
    assert report["layers"]["evidence_valid"] is True
    assert report["layers"]["claim_status"] == "MISSING"
    assert report["verdict"] == "JUDGE_REJECT"

    primary = [c for c in report["layers"]["scientific"] if c["id"] == "primary"][0]
    assert primary["n_blocks"] == 32
    assert primary["lower"] == pytest.approx(-0.01)
    assert primary["upper"] == pytest.approx(-0.01)
    assert primary["status"] == "SUPPORTED"
    negative = [c for c in report["layers"]["scientific"] if c["id"] == "negative"][0]
    assert negative["status"] == "SUPPORTED"       # 差 0 的等效

    submission = _submission(plan, manifest, report)
    report2 = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                            submission=submission)
    assert report2["layers"]["schema_accepted"] is True
    assert report2["layers"]["claim_status"] == "MATCH"
    assert report2["verdict"] == "JUDGE_OK"


def test_study_b_judge_computes_rmse_independently() -> None:
    plan = _plan("B")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_b_artifacts(plan, manifest, effect=0.02)
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    primary = [c for c in report["layers"]["scientific"] if c["id"] == "primary"][0]
    assert primary["n_blocks"] == 32
    assert primary["lower"] == pytest.approx(0.02)
    assert primary["upper"] == pytest.approx(0.02)
    assert primary["status"] == "SUPPORTED"        # increase_gt, δ=0.01


def test_judge_bootstrap_matches_public_statistics_bitwise() -> None:
    """judge 的独立重抽样必须与公开统计逐位一致（否则候选与判者两套数）。"""
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    diffs = {f"b{i:02d}": -0.01 + 0.002 * math.sin(i) for i in range(1, 33)}
    artifacts = _study_a_artifacts(plan, manifest, diffs=diffs)
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    primary = [c for c in report["layers"]["scientific"] if c["id"] == "primary"][0]
    est = ST.estimate({k: diffs[k] for k in sorted(diffs)})
    assert primary["lower"] == est["lower"]
    assert primary["upper"] == est["upper"]
    assert primary["effect"] == est["effect"]


# ---------------------------------------------------------------------------
# 分母与识别（证据层）
# ---------------------------------------------------------------------------

def test_missing_run_rejects_without_raising() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    victim = sorted(artifacts)[0]
    del artifacts[victim]
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    assert report["layers"]["evidence_valid"] is False
    assert report["verdict"] == "JUDGE_REJECT"
    runset = [c for c in report["checks"] if c["id"] == "J-RUN-SET"][0]
    assert runset["ok"] is False


def test_denominator_shrink_is_caught() -> None:
    """少一个 block 的 manifest（不是少一个样本，是分母偷换）必须证据层失败。"""
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    trimmed = E.ConfirmManifest(
        study_id=manifest.study_id, plan_sha256=manifest.plan_sha256,
        keeper_seed_sha256=manifest.keeper_seed_sha256,
        block_count=manifest.block_count, varying=manifest.varying,
        assignments=tuple(a for a in manifest.assignments
                          if not (a.block_id == "b07" and a.condition_id == "base")))
    artifacts = _study_a_artifacts(plan, trimmed)
    report = J.judge_study(plan=plan, manifest=trimmed, artifacts=artifacts)
    assert report["layers"]["evidence_valid"] is False
    ids = {c["id"]: c for c in report["checks"]}
    assert ids["J-DENOMINATOR"]["ok"] is False
    assert ids["J-32-BLOCKS"]["ok"] is False


def test_block_pairing_break_is_caught() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    broken = E.ConfirmManifest(
        study_id=manifest.study_id, plan_sha256=manifest.plan_sha256,
        keeper_seed_sha256=manifest.keeper_seed_sha256,
        block_count=manifest.block_count, varying=manifest.varying,
        assignments=tuple(
            E.ConfirmAssignment(a.condition_id, a.block_id,
                                {**a.params, "mass_kg": "0.8"}
                                if (a.condition_id == "sham" and a.block_id == "b03")
                                else a.params)
            for a in manifest.assignments))
    artifacts = _study_a_artifacts(plan, broken)
    report = J.judge_study(plan=plan, manifest=broken, artifacts=artifacts)
    pair = [c for c in report["checks"] if c["id"] == "J-BLOCK-PAIRING"][0]
    assert pair["ok"] is False and "b03" in pair["detail"]


def test_two_factor_contrast_is_confounded() -> None:
    doc = H.plan_doc("A")
    bad = copy.deepcopy(doc["conditions"])
    for c in bad:
        if c["id"] == "low_mu":
            c["params"]["torsional_friction_m"] = "0.02"   # 第二个干预
    plan = H.doc_of(H.plan_doc("A", conditions=bad))
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    ident = [c for c in report["checks"] if c["id"] == "J-IDENTIFICATION"][0]
    assert ident["ok"] is False
    assert "primary" in ident["detail"]
    assert report["verdict"] == "JUDGE_REJECT"


def test_negative_control_must_use_registered_param() -> None:
    doc = H.plan_doc("A")
    bad = copy.deepcopy(doc["conditions"])
    for c in bad:
        if c["id"] == "sham":
            c["params"]["sliding_friction"] = "0.3"    # 阴性控制必须只动登记参数
    plan = H.doc_of(H.plan_doc("A", conditions=bad))
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    ident = [c for c in report["checks"] if c["id"] == "J-IDENTIFICATION"][0]
    assert ident["ok"] is False
    assert "negative" in ident["detail"]


# ---------------------------------------------------------------------------
# 候选提交面（不得替候选过关）
# ---------------------------------------------------------------------------

def test_condition_swap_in_meta_is_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    victim = sorted(artifacts)[0]
    meta = dict(artifacts[victim]["meta"])
    meta["condition_id"] = "sham" if meta["condition_id"] != "sham" else "base"
    artifacts[victim] = {**artifacts[victim], "meta": meta}
    with pytest.raises(J.JudgeError) as exc:
        J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    assert exc.value.code == "CONDITION_SWAP"


def test_spec_hash_tamper_is_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    victim = sorted(artifacts)[0]
    rec = dict(artifacts[victim]["record"])
    rec["spec_hash"] = "0" * 64
    artifacts[victim] = {**artifacts[victim], "record": rec}
    with pytest.raises(J.JudgeError) as exc:
        J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    assert exc.value.code == "SPEC_MISMATCH"


def test_claim_numbers_are_cross_checked_not_trusted() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    base = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    submission = _submission(plan, manifest, base)
    submission["claims"][0]["lower"] += 1e-3        # 谎报下界
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                           submission=submission)
    assert report["layers"]["claim_status"] == "CLAIM_INVALID"
    assert report["verdict"] == "JUDGE_REJECT"
    # 科学层仍用 judge 自己的数，不受候选谎报影响
    primary = [c for c in report["layers"]["scientific"] if c["id"] == "primary"][0]
    assert primary["status"] == "SUPPORTED"


def test_missing_claim_for_registered_contrast_is_invalid() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    base = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    submission = _submission(plan, manifest, base)
    submission["claims"] = [c for c in submission["claims"] if c["hypothesis_id"] == "h1"]
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                           submission=submission)
    assert report["layers"]["claim_status"] == "CLAIM_INVALID"
    assert any("negative" in m for m in report["claim_mismatches"])


def test_primary_contrast_drift_is_schema_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    base = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    submission = _submission(plan, manifest, base)
    submission["primary_contrast_id"] = "negative"
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                           submission=submission)
    assert report["layers"]["schema_accepted"] is False
    assert report["verdict"] == "JUDGE_REJECT"


def test_unknown_claim_keys_are_schema_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    base = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    submission = _submission(plan, manifest, base)
    submission["claims"][0]["p_value"] = 0.01
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                           submission=submission)
    assert report["layers"]["schema_accepted"] is False


def test_delta_drift_in_submission_is_schema_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    base = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    submission = _submission(plan, manifest, base)
    submission["claims"][0]["delta"] = "0.0001"
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts,
                           submission=submission)
    assert report["layers"]["schema_accepted"] is False


def test_foreign_run_id_is_rejected() -> None:
    plan = _plan("A")
    manifest = E.allocate_confirm(plan, KEEPER)
    artifacts = _study_a_artifacts(plan, manifest)
    artifacts["run-ffffffffffffffffffff"] = {"record": {}, "meta": {}}
    report = J.judge_study(plan=plan, manifest=manifest, artifacts=artifacts)
    assert report["layers"]["evidence_valid"] is False
    assert report["verdict"] == "JUDGE_REJECT"
    # 直接入口也不吃外来 run
    with pytest.raises(J.JudgeError) as exc:
        J.recompute_metrics(plan=plan, artifacts=artifacts,
                            specs=J.build_specs(plan, manifest))
    assert exc.value.code == "EXTRA_RUN"
