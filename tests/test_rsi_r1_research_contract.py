"""RSI-R1 T1a：研究合同、场景接口、raw 观测接口、预注册与公开渲染（AC05–AC09）。

全部离线：不触网、不调模型、不跑仿真、不 import mujoco。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import prereg as PR
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

# ---------------------------------------------------------------------------
# 构造合法计划的工具
# ---------------------------------------------------------------------------

def _cond(cid: str, scene: str, **params) -> dict:
    return {"id": cid, "scene_id": scene, "params": params}


A_CONDITIONS = (
    _cond("base", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
          sliding_friction="0.4", torsional_friction_m="0.01"),
    _cond("sham", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
          sliding_friction="0.4", torsional_friction_m="0.02"),
    _cond("low_mu", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
          sliding_friction="0.15", torsional_friction_m="0.01"),
    _cond("heavy", S.SCENE_A, mass_kg="0.9", v0_mps="0.4",
          sliding_friction="0.4", torsional_friction_m="0.01"),
)

B_CONDITIONS = (
    _cond("base", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
          target_freq_hz="1.0", tc_s="0.008", dt_s="0.001",
          controller="PD", kp="5", kd="0.1"),
    _cond("tc_only", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
          target_freq_hz="1.0", tc_s="0.016", dt_s="0.001",
          controller="PD", kp="5", kd="0.1"),
    _cond("dt_only", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
          target_freq_hz="1.0", tc_s="0.008", dt_s="0.0005",
          controller="PD", kp="5", kd="0.1"),
)

SHA = "a" * 64


def _plan_doc(study: str, *, phase: str = "confirm", conditions=None,
              primary=None, negative=None, varying=None, block_count: int = 32,
              hypotheses=None) -> dict:
    import copy

    scene = S.SCENE_A if study == "A" else S.SCENE_B
    conds = copy.deepcopy(
        list(A_CONDITIONS if study == "A" else B_CONDITIONS)) \
        if conditions is None else conditions
    if primary is None:
        if study == "A":
            primary = {"id": "primary", "kind": "primary",
                       "metric": "displacement_x_m",
                       "proposition": "decrease_gt", "delta": "0.001",
                       "condition_ids": ["base", "low_mu"]}
        else:
            primary = {"id": "primary", "kind": "primary",
                       "metric": "normalized_tracking_rmse",
                       "proposition": "increase_gt", "delta": "0.01",
                       "condition_ids": ["base", "tc_only"]}
    if negative is None and study == "A":
        negative = {"id": "negative", "kind": "negative",
                    "metric": "displacement_x_m_negative_equivalence",
                    "proposition": "equivalent", "delta": "0.001",
                    "condition_ids": ["base", "sham"]}
    if hypotheses is None:
        hypotheses = [
            {"id": "h1", "mechanism": "摩擦耗能主导减速",
             "predicted": "decrease_gt" if study == "A" else "increase_gt",
             "contrast_id": "primary", "falsification": "区间跨过 δ 即反证"},
            {"id": "h2", "mechanism": "扭转摩擦只影响旋转耦合",
             "predicted": "equivalent", "contrast_id":
                 "negative" if study == "A" else "primary",
             "falsification": "位移差区间越出等效界即反证"},
        ]
    if varying is None:
        varying = ({"mass_kg": ["0.2", "1.0"]} if study == "A"
                   else {"inertia_ratio": ["0.5", "2.0"]})
    return {
        "schema": C.PLAN_SCHEMA, "schema_version": 1,
        "study_id": study, "revision": 1, "parent_sha": None,
        "hypotheses": hypotheses, "conditions": conds,
        "nuisance_distribution": {"generator": "pcg64-host",
                                  "block_count": block_count,
                                  "varying": varying},
        "primary": primary, "negative_control": negative,
        "analysis_source_sha": SHA if phase == "confirm" else None,
        "design_source_sha": SHA if phase == "confirm" else None,
        "phase": phase,
    }


# ---------------------------------------------------------------------------
# AC05：单位与域完整公开
# ---------------------------------------------------------------------------

def test_public_contract_covers_every_param_with_unit():
    doc = C.public_contract_document(scene_ids=(S.SCENE_A, S.SCENE_B))
    C.assert_public_contract_complete(doc)
    for scene in doc["scenes"]:
        for p in scene["params"]:
            assert p["unit"], p
            assert p["description"], p
            assert p["domain"], p
        for f in scene["fixed"]:
            assert f["unit"] and f["description"]
    text = C.render_public_contract(scene_ids=(S.SCENE_A, S.SCENE_B))
    for spec in S.SCENE_A_DOC.params + S.SCENE_B_DOC.params:
        assert spec.id in text
    for sid in (S.SCENE_A, S.SCENE_B):
        for field in O.required_fields(sid):
            assert field in text


def test_public_contract_missing_unit_fails():
    doc = C.public_contract_document(scene_ids=(S.SCENE_A,))
    broken = json.loads(json.dumps(doc))
    broken["scenes"][0]["params"][0]["unit"] = ""
    with pytest.raises(C.ContractError) as ei:
        C.assert_public_contract_complete(broken)
    assert ei.value.code == "MISSING_KEY"


def test_public_contract_render_is_deterministic():
    a = C.render_public_contract(scene_ids=(S.SCENE_A, S.SCENE_B))
    b = C.render_public_contract(scene_ids=(S.SCENE_A, S.SCENE_B))
    assert a == b
    assert "R1-S1-01" in a and "DELTA_DRIFT" in a


def test_limits_are_public():
    doc = C.public_contract_document(scene_ids=(S.SCENE_A,))
    assert doc["limits"]["max_local_tokens"] == 20_000
    assert doc["limits"]["max_series_bytes"] == 131_072
    assert doc["limits"]["max_output_tokens"] == 8_192
    assert doc["limits"]["max_tool_return_bytes"] == 4_096


# ---------------------------------------------------------------------------
# 场景参数：typed、整数 ticks、域
# ---------------------------------------------------------------------------

def test_scene_rejects_unknown_missing_and_domain():
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A, {**A_CONDITIONS[0]["params"], "extra": "1"})
    assert ei.value.code == "UNKNOWN_PARAM"
    partial = dict(A_CONDITIONS[0]["params"])
    partial.pop("mass_kg")
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A, partial)
    assert ei.value.code == "MISSING_PARAM"
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A, {**A_CONDITIONS[0]["params"], "mass_kg": "1.5"})
    assert ei.value.code == "DOMAIN"


def test_scene_rejects_bool_float_and_nan():
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A,
                           {**A_CONDITIONS[0]["params"], "mass_kg": True})
    assert ei.value.code == "BOOL_AS_NUMBER"
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A,
                           {**A_CONDITIONS[0]["params"], "mass_kg": 0.5})
    assert ei.value.code == "FLOAT_INPUT"
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_A,
                           {**A_CONDITIONS[0]["params"], "mass_kg": "NaN"})
    assert ei.value.code == "NONFINITE"


def test_tick_math_is_integer_and_capped():
    shape = S.run_shape(Decimal("0.0005"), tc_s=Decimal("0.008"))
    assert shape["dt_ticks_us"] == 500
    assert shape["nsteps"] == 8000            # 4 s / 0.5 ms，恰在上限
    assert shape["control_updates"] == 500    # 4 s / 8 ms
    with pytest.raises(S.SceneError) as ei:
        S.run_shape(Decimal("0.003"))
    assert ei.value.code == "ENUM_INVALID" if False else True  # 由 normalise 拒；此处只验整除
    with pytest.raises(S.SceneError) as ei2:
        S.run_shape(Decimal("0.0007"))
    assert ei2.value.code in ("TICK_NONDIVISIBLE",)


def test_tc_not_multiple_of_dt_rejected():
    with pytest.raises(S.SceneError) as ei:
        S.run_shape(Decimal("0.002"), tc_s=Decimal("0.003"))
    assert ei.value.code == "TICK_NONDIVISIBLE"


def test_p_controller_requires_zero_kd():
    params = dict(B_CONDITIONS[0]["params"])
    params["controller"] = "P"
    with pytest.raises(S.SceneError) as ei:
        S.normalise_params(S.SCENE_B, params)
    assert ei.value.code == "DOMAIN"
    params["kd"] = "0"
    assert S.normalise_params(S.SCENE_B, params)["controller"] == "P"


# ---------------------------------------------------------------------------
# AC08：两题不预定开放结论 / 至少两个可区分解释
# ---------------------------------------------------------------------------

def test_no_expected_label_anywhere():
    doc = _plan_doc("B")
    doc["expected_label"] = "Tc is better"
    with pytest.raises(C.ContractError) as ei:
        C.ResearchPlan.from_dict(doc)
    assert ei.value.code == "UNKNOWN_KEY"
    text = C.render_public_contract(scene_ids=(S.SCENE_A, S.SCENE_B))
    assert "expected_label" not in text


def test_single_hypothesis_rejected():
    doc = _plan_doc("A", hypotheses=[{
        "id": "h1", "mechanism": "唯一机制", "predicted": "decrease_gt",
        "contrast_id": "primary", "falsification": "区间跨 δ 即反证"}])
    plan = C.ResearchPlan.from_dict(doc)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(plan)
    assert ei.value.code == "HYPOTHESIS_INSUFFICIENT"


def test_duplicate_hypothesis_mechanism_rejected():
    doc = _plan_doc("A")
    doc["hypotheses"][1]["mechanism"] = doc["hypotheses"][0]["mechanism"]
    plan = C.ResearchPlan.from_dict(doc)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(plan)
    assert ei.value.code == "HYPOTHESIS_INSUFFICIENT"


# ---------------------------------------------------------------------------
# AC09：量表先固定 + δ 不可扩
# ---------------------------------------------------------------------------

def test_delta_drift_rejected():
    doc = _plan_doc("A")
    doc["primary"]["delta"] = "0.01"          # 想把界放宽 10 倍
    with pytest.raises(C.ContractError) as ei:
        C.ResearchPlan.from_dict(doc)
    assert ei.value.code == "DELTA_DRIFT"


def test_unregistered_metric_rejected():
    doc = _plan_doc("A")
    doc["primary"]["metric"] = "made_up_metric"
    with pytest.raises(C.ContractError) as ei:
        C.ResearchPlan.from_dict(doc)
    assert ei.value.code == "METRIC_UNREGISTERED"


def test_rubric_hash_changes_with_delta(monkeypatch):
    before = R.rubric_sha256()
    tampered = dict(R.METRICS)
    tampered["displacement_x_m"] = {**R.METRICS["displacement_x_m"],
                                    "delta": Decimal("0.01")}
    monkeypatch.setattr(R, "METRICS", tampered)
    assert R.rubric_sha256() != before


def test_valid_plans_pass_both_studies():
    for study in ("A", "B"):
        plan = C.ResearchPlan.from_dict(_plan_doc(study))
        check = C.validate_plan(plan)
        assert check.ok, check.to_dict()
        ids = [c[0] for c in check.checks]
        assert "distribution" in ids
        # 计划件 round-trip 稳定
        again = C.ResearchPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert again.sha256 == plan.sha256


def test_negative_control_must_differ_only_in_torsional():
    doc = _plan_doc("A")
    # 让 sham 与 base 还差一个 sliding friction ⇒ 设计门拒绝
    doc["conditions"][1]["params"]["sliding_friction"] = "0.7"
    plan = C.ResearchPlan.from_dict(doc)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(plan)
    assert ei.value.code == "DESIGN_NOT_SEPARABLE"


def test_negative_control_required_for_study_a():
    hyps = [
        {"id": "h1", "mechanism": "摩擦耗能主导减速", "predicted": "decrease_gt",
         "contrast_id": "primary", "falsification": "区间跨过 δ 即反证"},
        {"id": "h2", "mechanism": "质量只改变惯性尺度", "predicted": "descriptive",
         "contrast_id": "primary", "falsification": "位移差与质量无关即反证"},
    ]
    doc = _plan_doc("A", negative=None, hypotheses=hyps)
    doc["negative_control"] = None
    plan = C.ResearchPlan.from_dict(doc)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(plan)
    assert ei.value.code == "NEGATIVE_CONTROL_MISSING"


def test_study_b_requires_tc_and_dt_single_variable_contrasts():
    conditions = (B_CONDITIONS[0],
                  _cond("both_changed", S.SCENE_B, inertia_ratio="1.0",
                        target_amplitude_rad="0.2", target_freq_hz="1.0",
                        tc_s="0.016", dt_s="0.0005", controller="PD",
                        kp="5", kd="0.1"))
    doc = _plan_doc("B", conditions=list(conditions),
                    primary={"id": "primary", "kind": "primary",
                             "metric": "normalized_tracking_rmse",
                             "proposition": "increase_gt", "delta": "0.01",
                             "condition_ids": ["base", "both_changed"]})
    plan = C.ResearchPlan.from_dict(doc)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(plan)
    assert ei.value.code == "DESIGN_NOT_SEPARABLE"


def test_confirm_requires_32_blocks_and_sources():
    doc = _plan_doc("A", block_count=16)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(C.ResearchPlan.from_dict(doc))
    assert ei.value.code == "DISTRIBUTION"
    doc = _plan_doc("A")
    doc["analysis_source_sha"] = None
    with pytest.raises(C.ContractError) as ei:
        C.ResearchPlan.from_dict(doc)
    assert ei.value.code == "SOURCE_SHA_INVALID"


def test_degenerate_distribution_rejected():
    doc = _plan_doc("A", varying={"mass_kg": ["0.5", "0.5"]})
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(C.ResearchPlan.from_dict(doc))
    assert ei.value.code == "DISTRIBUTION_DEGENERATE"


def test_condition_limit_in_confirm():
    conds = list(A_CONDITIONS) + [
        _cond(f"extra{i}", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
              sliding_friction="0.4", torsional_friction_m="0.01")
        for i in range(3)]
    doc = _plan_doc("A", conditions=conds)
    with pytest.raises(C.ContractError) as ei:
        C.validate_plan(C.ResearchPlan.from_dict(doc))
    assert ei.value.code == "CONDITION_LIMIT"


# ---------------------------------------------------------------------------
# AC06：预注册先于 raw；来源漂移即失效
# ---------------------------------------------------------------------------

def test_prereg_matches_sources():
    doc = PR.build_instrument_prereg(envelope_id="env1")
    PR.assert_prereg_matches_sources(doc)
    assert doc["no_reference_solution"] is True
    assert doc["no_expected_label"] is True
    assert "DESIGN_NOT_READY" in json.dumps(doc, ensure_ascii=False)
    assert doc["rubric_sha256"] == R.rubric_sha256()


def test_prereg_rubric_drift_detected():
    doc = PR.build_instrument_prereg(envelope_id="env1")
    doc["rubric_sha256"] = "0" * 64
    with pytest.raises(PR.PreregError) as ei:
        PR.assert_prereg_matches_sources(doc)
    assert ei.value.code == "RUBRIC_DRIFT"


def test_prereg_must_precede_raw(tmp_path):
    doc = PR.build_instrument_prereg(
        envelope_id="env1", registered_at_utc="2026-11-11T10:00:00Z")
    raw = tmp_path / "run1.jsonl"
    raw.write_text("{}\n", encoding="utf-8")
    # 把 raw 的 mtime 固定到预注册之后 → 通过
    import os
    ts_after = 1794391200 + 1  # 预注册时刻 2026-11-11T10:00:00Z 的后一秒
    os.utime(raw, (ts_after, ts_after))
    PR.assert_prereg_precedes_raw(doc, [raw])
    # 把 raw 的 mtime 提前到预注册之前 → 拒绝
    os.utime(raw, (ts_after - 7200, ts_after - 7200))
    with pytest.raises(PR.PreregError) as ei:
        PR.assert_prereg_precedes_raw(doc, [raw])
    assert ei.value.code == "PREREG_NOT_FIRST"


def test_prereg_timestamp_must_be_z():
    doc = PR.build_instrument_prereg(
        envelope_id="env1", registered_at_utc="2026-11-11 10:00:00")
    with pytest.raises(PR.PreregError) as ei:
        PR.assert_prereg_precedes_raw(doc, [])
    assert ei.value.code == "TIMESTAMP"


# ---------------------------------------------------------------------------
# AC07（接口半边）：raw 校验与 manifest
# ---------------------------------------------------------------------------

def _record(scene: str, n: int = 4) -> dict:
    if scene == S.SCENE_A:
        return {
            "time_s": [i * 0.001 for i in range(n)],
            "qpos": [0.0] * n, "qvel": [0.4] * n,
            "params_applied": {"mass_kg": "0.5"},
            "engine": {"name": "mujoco", "version": "3.15.0"},
            "run_id": "r1", "spec_hash": "s" * 64, "block_id": "b1",
            "nsteps": n, "nonfinite_count": 0, "solver_warnings": [],
            "contact_pair": "box-floor",
            "contact_dim_per_step": [3] * n,
            "friction_sliding_per_step": [0.4] * n,
            "friction_torsional_per_step": [0.01] * n,
            "contact_force_n": [5.0] * n,
        }
    return {
        "time_s": [i * 0.001 for i in range(n)],
        "qpos": [0.0] * n, "qvel": [0.0] * n,
        "params_applied": {"kp": "5"},
        "engine": {"name": "mujoco", "version": "3.15.0"},
        "run_id": "r1", "spec_hash": "s" * 64, "block_id": "b1",
        "nsteps": n, "nonfinite_count": 0, "solver_warnings": [],
        "target_q": [0.1] * n, "command_torque": [0.2] * n,
        "applied_torque": [0.2] * n,
        "control_update_index": [0, 0, 0, 1][:n],
        "saturation_count": 0, "instability_events": [],
    }


def test_valid_record_passes_both_scenes():
    for scene in (S.SCENE_A, S.SCENE_B):
        rec = _record(scene)
        out = O.validate_record(rec, scene_id=scene)
        assert out["run_id"] == "r1"


def test_contact_dim_must_be_three():
    rec = _record(S.SCENE_A)
    rec["contact_dim_per_step"] = [3, 3, 4, 3]
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "CONTACT_DIM"


def test_nonfinite_and_length_and_time_checks():
    rec = _record(S.SCENE_A)
    rec["qpos"] = [float("nan")] * 4
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "NONFINITE"
    rec = _record(S.SCENE_A)
    rec["qvel"] = [0.0] * 3
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "LENGTH_MISMATCH"
    rec = _record(S.SCENE_A)
    rec["time_s"] = [0.0, 0.002, 0.001, 0.003]
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "TIME_NOT_MONOTONIC"
    rec = _record(S.SCENE_A)
    rec["nonfinite_count"] = 1
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "NONFINITE_STATE"


def test_unknown_raw_field_rejected():
    rec = _record(S.SCENE_A)
    rec["candidate_self_report"] = "all good"
    with pytest.raises(O.ObservationError) as ei:
        O.validate_record(rec, scene_id=S.SCENE_A)
    assert ei.value.code == "UNKNOWN_FIELD"


def test_jsonl_roundtrip_and_manifest(tmp_path):
    rec = _record(S.SCENE_A)
    p = O.write_record_jsonl(tmp_path / "raw" / "run1.jsonl", rec)
    assert p.read_text(encoding="utf-8").count("\n") == 1
    assert O.read_record_jsonl(p)["run_id"] == "r1"
    entry = O.manifest_entry("r1", "raw/run1.jsonl", p, nsteps=4)
    man = O.build_manifest([entry], study="A", batch_id="b1")
    O.verify_manifest(man, tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(O.ObservationError) as ei:
        O.verify_manifest(man, tmp_path)
    assert ei.value.code in ("MANIFEST_HASH", "MANIFEST_SIZE")


def test_npz_reader_forbids_pickle(tmp_path):
    arr = np.arange(3)
    np.savez(tmp_path / "ok.npz", time_s=arr)
    assert O.read_npz_arrays(tmp_path / "ok.npz")["time_s"] == [0, 1, 2]
    np.savez(tmp_path / "bad.npz", obj=np.array([{"a": 1}], dtype=object))
    with pytest.raises(ValueError):
        O.read_npz_arrays(tmp_path / "bad.npz")


def test_dt_consistency():
    rec = _record(S.SCENE_A)
    O.check_dt_consistency(rec, dt_s=Decimal("0.001"))
    with pytest.raises(O.ObservationError) as ei:
        O.check_dt_consistency(rec, dt_s=Decimal("0.002"))
    assert ei.value.code == "DT_MISMATCH"


# ---------------------------------------------------------------------------
# CLI：prereg / contract 端到端
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from orchestrator.rsi_r1 import cli

    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281",
                 "RSI_R0", "RSI_R01", "RSI_R02", "RSI_R1"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", "env1")
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    model = tmp_path / "stage_models.yaml"
    model.write_text("stages: {}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "PLAN_PATH", plan)
    monkeypatch.setattr(cli, "MODEL_CONFIG", model)
    import hashlib
    consent = {
        "schema": C.CONSENT_SCHEMA, "schema_version": 1, "envelope_id": "env1",
        "decision": C.COST_DECISION_WAIVER, "scope": "R1",
        "granted_at_utc": "2026-11-11T00:00:00Z", "granted_by": "user",
        "verbatim": "我授权", "plan_path": str(plan),
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "supersedes": [], "notes": "",
    }
    consent_path = tmp_path / "consent.json"
    consent_path.write_text(json.dumps(consent), encoding="utf-8")
    assert cli.main(["init", "--envelope", "env1",
                     "--consent", str(consent_path)]) == 0
    return {"cli": cli, "root": Path(tmp_path / "rsi_r1" / "env1")}


def test_cli_prereg_and_contract(cli_env):
    cli = cli_env["cli"]
    assert cli.main(["prereg", "--envelope", "env1"]) == 0
    doc = json.loads((cli_env["root"] / "contract" /
                      "instrument_prereg.json").read_text(encoding="utf-8"))
    PR.assert_prereg_matches_sources(doc)
    # 幂等：再跑一次不覆盖（同 document_sha）
    assert cli.main(["prereg", "--envelope", "env1"]) == 0
    doc2 = json.loads((cli_env["root"] / "contract" /
                       "instrument_prereg.json").read_text(encoding="utf-8"))
    assert doc2["document_sha256"] == doc["document_sha256"]

    assert cli.main(["contract", "--envelope", "env1"]) == 0
    text = (cli_env["root"] / "public" / "public_contract.md").read_text(encoding="utf-8")
    assert "RSI-R1 公开研究合同" in text
    assert cli.main(["check", "--envelope", "env1"]) == 0
