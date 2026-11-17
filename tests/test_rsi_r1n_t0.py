"""RSI-R1-N T0/T1 零 POST 单元：profile/账本/路径、两题 builder、五轴结案。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import closeout_r1n as CO
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import resource_plan as RPL
from orchestrator.rsi_r1 import task_specs as TS


def test_r1n_profile_numbers_and_flags():
    prof = P.profile_by_id(P.PROFILE_R1N)
    assert prof.is_r1n and not prof.is_r13 and not prof.is_r12
    assert prof.is_r13_family  # 仪器面接线（design_intent/审查容量/R13 brief）
    assert prof.namespace == 2907
    assert prof.consent_scope == "R1-N"
    assert prof.hard_cap == 132 == prof.normal_budget
    assert dict(prof.lane_post_caps) == {
        "probe": 12, "practice": 16, "study_A": 48, "study_B": 48,
        "review_calibration": 4, "review_final": 4}
    assert prof.family_cap == 132 and prof.family_history_posts == 0
    assert prof.data_root_name == "rsi_r1_n"
    assert prof.closure_groups_key == "r1n"
    P.assert_r1n_table_consistent()
    assert FAM.family_spec_for(prof).profile_id == P.PROFILE_R1N


def test_r1n_namespace_is_isolated_and_numbered(tmp_path, monkeypatch):
    from orchestrator.gateway import quota_ledger as QL
    monkeypatch.setenv("SIMPRAXIS_RSI_R1N_ROOT", str(tmp_path / "roots"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1N_CAMPAIGN", "r1n-test")
    ledger = QL.QuotaLedger.rsi_r1n()
    assert ledger.ns.phase == 2907 and ledger.ns.hard_cap == 132
    assert QL.RSI_R1N_TASK_CAPS == P.RSI_R1N_TASK_CAPS
    assert QL.LEDGER_RSI_R1N not in (2900, 2901, 2902, 2903, 2904, 2905, 2906)


def test_r1n_tasks_shape_and_policy():
    a, b = TS.build_r1n_a(), TS.build_r1n_b()
    # A_N：5 条件（2×2 + sham），block 32 → 160 run
    assert len(a.conditions) == 5
    assert int(a.fixed["confirm_blocks"]) == 32
    assert a.primary.kind == "difference_in_differences"
    assert a.primary.metric == "displacement_x_m"
    assert a.intervention_signature() == ("mass_kg", "sliding_friction",
                                          "torsional_friction_m")
    nc = a.negative_control_spec["contrast"]
    assert nc["id"] == "negative_torsional"
    assert nc["kind"] == "pairwise_difference"
    assert nc["metric"] == "displacement_x_m_negative_equivalence"
    assert nc["proposition"] == "equivalent"
    # sham 只差 torsional（克隆 cell μ=0.4, m=1.0）
    by_id = {c.id: c for c in a.conditions}
    base, sham = by_id["hi_mu_hi_mass"], by_id["sham"]
    diff = sorted(k for k in base.params if base.params[k] != sham.params[k])
    assert diff == ["torsional_friction_m"]
    assert base.params["sliding_friction"] == "0.4"
    assert base.params["mass_kg"] == "1.0"
    # B_N：4 条件（2×2），128 run，无 NC，Tc/dt 整除
    assert len(b.conditions) == 4
    assert int(b.fixed["confirm_blocks"]) == 32
    assert b.negative_control_spec is None
    assert b.intervention_signature() == ("kp", "tc_s")
    from decimal import Decimal
    for c in b.conditions:
        ratio = Decimal(c.params["tc_s"]) / Decimal(c.params["dt_s"])
        assert ratio == ratio.to_integral_value()


def test_r1n_near_duplicate_gate_against_all_old_tasks():
    rows = TS.assert_r1n_not_near_duplicate()
    assert len(rows) == 4 and all(r["ok"] for r in rows)
    assert {r["new"] for r in rows} == {TS.R1N_A_TASK_ID, TS.R1N_B_TASK_ID}


def test_did_equivalence_is_not_registrable():
    """T0 核验：DiD + equivalent 不可注册（只用 pairwise 阴性指标可等效）。"""
    from orchestrator.rsi_r1 import contrasts as X
    from orchestrator.rsi_r1 import rubric as R
    a = TS.build_r1n_a()
    doc = a.primary.to_dict()
    doc["proposition"] = "equivalent"
    with pytest.raises(X.ContrastError) as exc:
        X.ContrastV13.from_dict(doc).validate()
    assert exc.value.code == "METRIC_INVALID"
    assert R.metric_delta("displacement_x_m") == R.metric_delta(
        "displacement_x_m_negative_equivalence")


def test_r1n_taskset_manifest_and_resource_plan(tmp_path):
    a, b = TS.build_r1n_a(), TS.build_r1n_b()
    plan = RPL.build_plan((a, b))
    doc = TS.build_taskset_r1n(
        a_commitment="a" * 64, b_commitment="b" * 64,
        a_resource_sha=plan.task_sha256(TS.R1N_A_TASK_ID),
        b_resource_sha=plan.task_sha256(TS.R1N_B_TASK_ID),
        difference_table_sha="d" * 64)
    assert doc["profile_id"] == P.PROFILE_R1N
    assert doc["holdout_task_ids"] == [TS.R1N_A_TASK_ID, TS.R1N_B_TASK_ID]
    assert len(doc["tasks"]) == 6
    assert plan.total_steps > 0 and plan.hard_cap_steps >= plan.total_steps


def _study(study: str, task_id: str, *, state: str = "VERIFIED",
           verdict: str = "JUDGE_OK", claim: str = "MATCH",
           evidence: bool = True, nc: "str | None" = "SUPPORTED") -> dict:
    judge: "dict" = {"verdict": verdict, "claim_status": claim,
                     "evidence_valid": evidence}
    if study == "A" and nc:
        judge["negative_control"] = {
            "contrast_id": "negative_torsional", "status": nc,
            "source": "host_recomputed_from_raw"}
    return {"study": study, "task_id": task_id, "status": "COMPLETED",
            "study_state": state, "judge": judge}


def _analysis(task_id: str, contrasts) -> dict:
    return {"task_id": task_id,
            "contrasts": [{"contrast_id": cid, "status": status}
                          for cid, status in contrasts]}


def _closeout(tmp_path: Path, **overrides):
    taskset = tmp_path / "taskset"
    taskset.mkdir()
    (taskset / "taskset_manifest.json").write_text(json.dumps({
        "holdout_task_ids": [TS.R1N_A_TASK_ID, TS.R1N_B_TASK_ID]}))
    (taskset / "resource_plan.json").write_text(json.dumps({
        "total_steps": 100, "hard_cap_steps": 120}))
    singles = {
        "calibration": {"ok": True, "code": "REVIEW_CALIBRATED"},
        "t5": {"all_ok": True},
        "human": {"complete": True, "rescue_orders": [], "n_events": 0},
    }
    singles.update({k: v for k, v in overrides.items() if not isinstance(v, list)})
    lists = {
        "studies": [_study("A", TS.R1N_A_TASK_ID),
                    _study("B", TS.R1N_B_TASK_ID)],
        "reviews": [{"verdict": "REVIEW_PASS"}, {"verdict": "REVIEW_PASS"},
                    {"verdict": "REVIEW_PASS"}, {"verdict": "REVIEW_PASS"}],
        "replays": [{"study": "A", "ok": True, "identical": True,
                     "analysis_identical": True},
                    {"study": "B", "ok": True, "identical": True,
                     "analysis_identical": True}],
        "analyses": [
            _analysis(TS.R1N_A_TASK_ID,
                      [("friction_x_mass", "SUPPORTED"),
                       ("negative_torsional", "SUPPORTED")]),
            _analysis(TS.R1N_B_TASK_ID, [("kp_x_tc", "INCONCLUSIVE")])],
    }
    lists.update({k: v for k, v in overrides.items() if isinstance(v, list)})
    paths = {}
    for key, doc in singles.items():
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(doc))
        paths[key] = path
    for key, docs in lists.items():
        made = []
        for index, doc in enumerate(docs):
            path = tmp_path / f"{key}_{index}.json"
            path.write_text(json.dumps(doc))
            made.append(path)
        paths[key] = made
    return CO.build_closeout_r1n(
        campaign="r1n-test", taskset_dir=taskset,
        t5_summary=paths["t5"], calibration_report=paths["calibration"],
        study_reports=paths["studies"], review_reports=paths["reviews"],
        replay_reports=paths["replays"], analyses=paths["analyses"],
        human_events=paths["human"],
        primary_contrast_ids={"A": "friction_x_mass", "B": "kp_x_tc"},
        negative_contrast_id="negative_torsional")


def test_closeout_all_conditions_established(tmp_path):
    doc = _closeout(tmp_path)
    assert doc["axes"]["r1"] == CO.R1_ESTABLISHED
    assert doc["axes"]["study_execution"] == CO.STUDY_COMPLETED
    assert doc["axes"]["claim"] == CO.CLAIM_SUPPORTED
    assert all(doc["facts"]["r1_conditions"].values())
    # B_N 的科学方向独立记录（INCONCLUSIVE 是合法结论，不入 r1 gate）
    b = [s for s in doc["facts"]["studies"] if s["study"] == "B"][0]
    assert b["science_direction"] == "INCONCLUSIVE"
    assert doc["facts"]["claim_scope"] == CO.CLAIM_SCOPE


def test_closeout_negative_control_refuted_blocks_r1(tmp_path):
    doc = _closeout(tmp_path,
                    studies=[_study("A", TS.R1N_A_TASK_ID, nc="REFUTED"),
                             _study("B", TS.R1N_B_TASK_ID)],
                    analyses=[
        _analysis(TS.R1N_A_TASK_ID,
                  [("friction_x_mass", "SUPPORTED"),
                   ("negative_torsional", "REFUTED")]),
        _analysis(TS.R1N_B_TASK_ID, [("kp_x_tc", "INCONCLUSIVE")])])
    assert doc["facts"]["r1_conditions"]["effective_negative"] is False
    assert doc["axes"]["r1"] == CO.R1_NOT_ESTABLISHED


def test_closeout_rescue_maps_to_assisted_not_blocked(tmp_path):
    doc = _closeout(tmp_path, human={
        "complete": True, "n_events": 1,
        "rescue_orders": [{"event_id": "h1", "success": True}],
        "rescue_success_count": 1})
    assert doc["axes"]["study_execution"] == CO.STUDY_ASSISTED
    assert doc["axes"]["r1"] == CO.R1_NOT_ESTABLISHED


def test_closeout_missing_replay_is_incomplete_coverage(tmp_path):
    doc = _closeout(tmp_path, replays=[])
    assert doc["axes"]["r1"] == CO.R1_INCOMPLETE


def test_closeout_claim_match_is_not_science_support(tmp_path):
    # claim 轴 SUPPORTED 但科学方向 INCONCLUSIVE：两轴必须分开记录
    doc = _closeout(tmp_path)
    a = [s for s in doc["facts"]["studies"] if s["study"] == "A"][0]
    assert doc["axes"]["claim"] == CO.CLAIM_SUPPORTED
    assert a["science_direction"] == "SUPPORTED"  # A_N 主对比登记方向
    assert doc["facts"]["claim_scope"] == "two_case_feasibility"
