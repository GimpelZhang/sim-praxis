"""RSI-R0.1 仪器层单测（零付费、零容器）：动作解析 / 数值合同 / 预注册 / 判据。

覆盖 AC10（合同齐全与私有隔离）、AC12（预注册拒绝路径）、AC13（数值边界）、
AC14（严 schema 不崩溃）与 D v2 故障表的合法性。真容器执行与真实参考路径的
证据在 ``scripts/rsi_r01/reference_actor.py`` / ``controls.py`` 的 campaign
产物里，不在这里冒名顶替（本文件不产生任何 PASS 于 AC16–AC19 的证据）。
"""
from __future__ import annotations

import json
import math

import pytest

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import evaluator as EV
from orchestrator.rsi_r01 import faults as F
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r01.trial import TrialRunner, action_surface


# ---------------------------------------------------------------------------
# AC10：九实例合同与私有隔离
# ---------------------------------------------------------------------------


def test_matrix_complete_and_fault_table_valid():
    assert TC.assert_matrix_complete() == []
    assert TC.validate_fault_table() == []
    assert len(TC.INSTANCES) == 9
    for fam in ("E", "D", "R"):
        assert len(TC.by_family(fam, "dev")) == 1
        assert len(TC.by_family(fam, "validation")) == 2


def test_private_truth_absent_from_public_face():
    for inst in TC.INSTANCES:
        blob = json.dumps(inst.as_public_dict(), ensure_ascii=False)
        for secret in ("fault", "tolerance", "reference_gains", "expected",
                       "bias_x", "deadzone", "scale"):
            assert secret not in blob, f"{inst.instance_id} 公开面泄漏 {secret}"


def test_d_faults_are_three_distinct_steady_state_mechanisms():
    kinds = [i.private["fault"]["kind"] for i in TC.by_family("D")]
    assert sorted(kinds) == sorted(F.KINDS)
    for inst in TC.by_family("D"):
        fault = inst.private["fault"]
        assert F.validate_fault(fault) == fault
        inj = inst.host_injections
        if fault["kind"] == "friction_scale":
            assert inj == {}                      # 场景级，不进请求体
        else:
            assert inj, "宿主注入项不得为空"


@pytest.mark.parametrize("fault", [
    {"kind": "mass_scale", "scale": 2.0},
    {"kind": "scene_tilt", "scale": 6.0},
    {"kind": "force_scale", "scale": 0.4},
    {"kind": "external_force_bias", "bias_x": 1.0, "bias_y": 0.0},
    {"kind": "actuator_deadzone", "deadzone": 0.0},
    {"kind": "friction_scale", "scale": 99.0},
])
def test_forbidden_or_out_of_bounds_faults_rejected(fault):
    with pytest.raises(F.FaultError):
        F.validate_fault(fault)


# ---------------------------------------------------------------------------
# AC13：数值合同的边界与删失
# ---------------------------------------------------------------------------


def test_band_is_strict_on_the_boundary():
    assert EV.inside_band(0.0, 0.0) is True
    assert EV.inside_band(S.THETA_BAND, 0.0) is False      # 恰等阈值不算带内
    assert EV.inside_band(0.0, S.OMEGA_BAND) is False
    assert EV.inside_band(-S.THETA_BAND, 0.0) is False


def test_settling_first_all_inside_and_censoring():
    res = EV.settling([0.5, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 1.0, 2.0])
    assert res.index == 1 and res.time_s == 1.0 and not res.censored
    cens = EV.settling([0.0, 0.0, 0.5], [0.0, 0.0, 0.0], [0.0, 1.0, 2.0])
    assert cens.censored and cens.time_s is None
    only_last = EV.settling([0.5, 0.0], [0.0, 0.0], [0.0, 4.0])
    assert only_last.only_last_point and only_last.hold_to_end_s == 0.0
    # 中段越界但末段回升：沉降点在**最后**一个越界点之后
    late = EV.settling([0.0, 0.5, 0.0, 0.0], [0.0] * 4, [0.0, 1.0, 2.0, 3.0])
    assert late.time_s == 2.0


def test_settling_rejects_ragged_series():
    with pytest.raises(EV.EvaluationError):
        EV.settling([0.0], [0.0, 0.0], [0.0])


def test_effect_null_reasons_and_mean_partial_censoring():
    assert EV.effect(None, 1.0)["reason"] == "censored_or_missing"
    assert EV.effect(0.0, 1.0)["reason"] == "t_low_zero"
    assert EV.effect(1.0, 2.0)["value"] == -1.0
    part = EV.mean_effect({"plus": {"value": 1.0}, "minus": {"value": None}})
    assert part["value"] is None and part["reason"] == "partial_censored"
    assert part["missing_angles"] == ["minus"]
    full = EV.mean_effect({"plus": {"value": 1.0}, "minus": {"value": 3.0}})
    assert full["value"] == 2.0


def test_claim_boundaries_are_inclusive_at_delta():
    assert EV.claim_of(None, 1) == (S.ClaimStatus.INCONCLUSIVE, None)
    assert EV.claim_of(S.DELTA, 1) == (S.ClaimStatus.SUPPORTED, S.DELTA)
    assert EV.claim_of(-S.DELTA, 1) == (S.ClaimStatus.REFUTED, -S.DELTA)
    assert EV.claim_of(S.DELTA, -1) == (S.ClaimStatus.REFUTED, -S.DELTA)
    status, z = EV.claim_of(0.0, 1)
    assert status == S.ClaimStatus.INCONCLUSIVE and z == 0.0
    with pytest.raises(EV.EvaluationError):
        EV.claim_of(0.5, 0)


# ---------------------------------------------------------------------------
# AC12：预注册结构（缺字段 / h / delta / 四格集合相等）
# ---------------------------------------------------------------------------


def _r_prereg(**over):
    plan = {
        "question": "q", "h": 1,
        "cells": [{"damping": c["damping"], "theta0": c["theta0"]}
                  for c in TC.get("R-dev").r_cells],
        "metric": "settling_time_s", "formula_version": "v", "delta": S.DELTA,
        "censoring_rule": "r", "analysis_procedure": "p", "stop_rule": "s",
    }
    plan.update(over)
    return plan


def test_prereg_accepts_frozen_matrix():
    cells = TC.prereg_expected_cells("R-dev")
    assert EV.check_prereg(_r_prereg(), expected_cells=cells) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.pop("delta"), "delta"),
    (lambda p: p.update(h=0), "h"),
    (lambda p: p.update(delta=0.2), "delta"),
    (lambda p: p.update(cells=[]), "cells"),
    (lambda p: p.update(cells=p["cells"][:3]), "4 格"),
    (lambda p: p.update(cells=p["cells"] * 2), "重复"),
    (lambda p: p["cells"][0].update(damping=99.0), "集合不符"),
])
def test_prereg_rejections_are_specific(mutate, needle):
    plan = _r_prereg()
    mutate(plan)
    problems = EV.check_prereg(plan, expected_cells=TC.prereg_expected_cells("R-dev"))
    assert problems, "该畸形预注册必须被拒"
    assert any(needle in p for p in problems), problems


def test_prereg_engineering_family_needs_nonempty_cells():
    assert EV.check_prereg(_r_prereg(cells=[{"kp": 1.0}])) == []
    assert EV.check_prereg(_r_prereg(cells=[])) != []


# ---------------------------------------------------------------------------
# AC14：严 schema 解析不抛异常
# ---------------------------------------------------------------------------


def _surface(instance_id: str) -> AC.ActionSurface:
    return action_surface(TC.get(instance_id))


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    "null",
    '{"schema": "wrong", "action": "run_experiment", "params": {}}',
    '{"schema": "%s", "action": "nope"}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment"}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": []}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": {"damping": null,'
    ' "initial_angle": 0.1, "steps": 10}}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": {"damping": "0.1",'
    ' "initial_angle": 0.1, "steps": 10}}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": {"damping": NaN,'
    ' "initial_angle": 0.1, "steps": 10}}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": {"damping": 0.1,'
    ' "initial_angle": 0.1, "steps": 100000}}' % S.SCHEMA_ACTION,
    '{"schema": "%s", "action": "run_experiment", "params": {"damping": 0.1,'
    ' "initial_angle": 0.1, "steps": 10, "fault": 1}}' % S.SCHEMA_ACTION,
])
def test_parse_action_returns_typed_failure(text):
    result = AC.parse_action(text, _surface("R-dev"))
    assert isinstance(result, AC.ActionFailure)
    assert result.code and isinstance(result.message, str)


def test_parse_action_accepts_valid_run_and_fills_fixed_dt():
    text = json.dumps({"schema": S.SCHEMA_ACTION, "action": "run_experiment",
                       "params": {"damping": 0.2, "initial_angle": 0.5,
                                  "steps": 2000}})
    result = AC.parse_action(text, _surface("R-dev"))
    assert isinstance(result, AC.RunExperiment)
    assert result.params["timestep"] == S.DT_S       # 固定值由宿主补，不由候选给


def test_parse_action_tolerates_one_markdown_fence():
    body = {"schema": S.SCHEMA_ACTION, "action": "submit", "claim": "SUPPORTED",
            "run_ids": ["run_x"], "analysis_ids": ["an_1"]}
    fenced = "```json\n" + json.dumps(body) + "\n```"
    result = AC.parse_action(fenced, _surface("R-dev"))
    assert isinstance(result, AC.Submit)


# ---------------------------------------------------------------------------
# 结论一致性（AC18「错误结论」类负控的宿主侧机制）
# ---------------------------------------------------------------------------


def _bare_runner(tmp_path) -> TrialRunner:
    return TrialRunner(campaign_root=tmp_path, instance_id="R-dev", trial_id="t")


def test_submission_check_requires_analysis_and_matching_claim(tmp_path):
    runner = _bare_runner(tmp_path)
    ok, reason = runner._submission_check(S.ClaimStatus.SUPPORTED)
    assert (ok, reason) == (False, "no_submission")
    runner.submissions.append({"claim": "SUPPORTED", "analysis_ids": []})
    ok, reason = runner._submission_check(S.ClaimStatus.SUPPORTED)
    assert (ok, reason) == (False, "no_analysis_referenced")
    runner.submissions.append({"claim": "REFUTED", "analysis_ids": ["an_1"]})
    ok, reason = runner._submission_check(S.ClaimStatus.SUPPORTED)
    assert not ok and reason.startswith("claim_mismatch:")
    runner.submissions.append({"claim": "SUPPORTED", "analysis_ids": ["an_1"]})
    assert runner._submission_check(S.ClaimStatus.SUPPORTED) == (True, "")


def test_engineering_judgement_uses_last_trusted_run_only():
    runs = [{"run_id": "a", "distance": 0.9, "final_speed": 0.5},
            {"run_id": "b", "distance": 0.01, "final_speed": 0.001},
            {"run_id": "c", "distance": 0.5, "final_speed": 0.001}]
    verdict = EV.evaluate_engineering(runs=runs,
                                      tolerance={"distance": 0.06,
                                                 "final_speed": 0.02})
    assert verdict["status"] == "FAIL" and verdict["run_id"] == "c"
    assert EV.evaluate_engineering(runs=[], tolerance={})["status"] == "FAIL"


def test_submit_checks_analysis_ids_against_real_analysis_events(tmp_path):
    """回归：``_submit`` 必须读 ``AnalysisEvent`` 属性（曾按 dict 取键而崩）。"""
    from orchestrator.rsi_r01.analysis_runner import AnalysisEvent
    from orchestrator.rsi_r01.trial import RunRecord, TrialRejected

    runner = _bare_runner(tmp_path)
    runner.runs.append(RunRecord(run_id="run_1", cell=None, params={},
                                 manifest={}, summary={}))
    event = AnalysisEvent(analysis_id="an_1", source_sha256="0" * 64,
                          run_ids=["run_1"], exit_code=0, timed_out=False,
                          stream_bytes=0, stream_over_cap=False,
                          stdout_preview="", stderr_preview="")
    runner.analyses.events.append(event)
    ok = AC.parse_action(json.dumps({"schema": S.SCHEMA_ACTION, "action": "submit",
                                     "claim": "SUPPORTED",
                                     "run_ids": ["run_1"],
                                     "analysis_ids": ["an_1"]}),
                         runner.surface)
    assert isinstance(ok, AC.Submit)
    out = runner._submit(ok)
    assert out["ok"] is True and runner.submissions[-1]["analysis_ids"] == ["an_1"]
    bad = AC.parse_action(json.dumps({"schema": S.SCHEMA_ACTION, "action": "submit",
                                      "claim": "SUPPORTED",
                                      "run_ids": ["run_1"],
                                      "analysis_ids": ["an_x"]}),
                          runner.surface)
    with pytest.raises(TrialRejected):
        runner._submit(bad)


def test_measurement_contract_constants_are_self_consistent():
    doc = TC.measurement_contract()
    assert doc["dt_s"] == S.DT_S and doc["expected_rows"] == S.EXPECTED_ROWS
    assert math.isclose(doc["t_end_s"], S.N_STEPS * S.DT_S, rel_tol=0, abs_tol=1e-12)
