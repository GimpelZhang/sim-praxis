"""RSI-R0 评测器契约测试：分层复算、伪造拒绝、研究判据。

全部离线：``replay`` 是测试注入的纯函数，不启容器、不联网、不触账本。
"""
from __future__ import annotations

import pytest

from orchestrator.rsi_r0.contracts import (
    Layer,
    LayerStatus,
    RESEARCH_CRITERIA,
    ScientificVerdict,
    ExecutionVerdict,
)
from orchestrator.rsi_r0.evaluator import (
    AnswerError,
    EvaluationInput,
    diagnose_from_runs,
    evaluate,
    research_from_runs,
    series_metrics,
)

SCENE = "a" * 64


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------
def _e_task(steps_max=4000):
    return {
        "instance_id": "E-v1", "family": "E", "role": "validation",
        "scene_xml_sha256": SCENE, "steps_max": steps_max,
        "scene_params": {"timestep": 0.002},
        "interface": {"kind": "push_control", "keys": ["kp", "kd", "max_force"]},
        "goal": {"type": "reach_and_settle", "target": [0.6, 0.0]},
    }


def _d_task():
    return {
        "instance_id": "D-v1", "family": "D", "role": "validation",
        "scene_xml_sha256": SCENE, "steps_max": 3000,
        "scene_params": {"timestep": 0.001},
        "interface": {"kind": "push_control", "keys": ["kp", "kd", "max_force"]},
        "goal": {"type": "reach_and_settle", "target": [0.6, 0.0]},
    }


def _r_task():
    return {
        "instance_id": "R-v1", "family": "R", "role": "validation",
        "scene_xml_sha256": SCENE, "steps_max": 4000,
        "scene_params": {"timestep": 0.001},
        "interface": {"kind": "free_run", "keys": ["initial_angle"]},
        "goal": {"type": "controlled_experiment"},
    }


def _call(run_id, params, events, scene=SCENE):
    return {"run_id": run_id, "params": params, "events_sha256": events,
            "scene_sha256": scene, "steps_run": int(params["steps"])}


def _accel_series(a, dt, n):
    """v(t)=a·t 的直线序列，fit_slope 会精确返回 a。"""
    return [[i, 0.0, 0.0, a * dt * i, 0.0] for i in range(n)]


def _settle_series(settle_steps, n, dt=0.001, amp=0.5):
    return [[i, amp, amp] for i in range(settle_steps)] + \
           [[i, 0.0, 0.0] for i in range(settle_steps, n)]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_series_metrics_reads_the_last_row():
    m = series_metrics([[0, 0.0, 0.0, 0.0, 0.0], [1, 0.3, 0.4, 0.5, 0.0]], [0.0, 0.0])
    assert m["distance"] == pytest.approx(0.5)
    assert m["final_speed"] == pytest.approx(0.5)


def test_diagnose_needs_two_forces_per_direction():
    with pytest.raises(AnswerError):
        diagnose_from_runs([{"params": {"target_x": 30.0, "max_force": 13.0},
                             "series": _accel_series(1.0, 0.001, 50)}],
                           nominal={"mass": 1.0, "friction": 0.25, "g": 9.81},
                           rules={"asymmetry_min": 0.5, "deviation_min": 0.15},
                           dt=0.001)


def test_research_needs_two_levels_and_two_angles():
    with pytest.raises(AnswerError):
        research_from_runs(
            [{"params": {"damping": 0.3, "initial_angle": 0.9},
              "angle_series": _settle_series(100, 200)}],
            dt=0.001, priv={"equivalence_bound": 0.05})


# ---------------------------------------------------------------------------
# E 族
# ---------------------------------------------------------------------------
def _e_input(answer, *, series=None, calls=None, tolerance=None):
    series = series or [[0, 0.0, 0.0, 0.0, 0.0], [10, 0.599, 0.0, 0.001, 0.0]]
    calls = calls if calls is not None else [
        _call("run1", {"kp": 40.0, "kd": 4.0, "max_force": 12.0, "steps": 10}, "ev1")]
    return EvaluationInput(
        task=_e_task(), answer=answer, calls=calls,
        replay=lambda _p: {"events_sha256": "ev1", "series": series},
        truth={}, priv_rules={"tolerance": tolerance or
                              {"distance": 0.06, "final_speed": 0.02}})


def test_e_happy_path_passes_every_applicable_layer():
    res = evaluate(_e_input({"family": "E", "run_id": "run1",
                             "claim": {"distance": 0.001, "final_speed": 0.001}}))
    assert [res.layers[L].status for L in
            (Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4)] == [LayerStatus.PASS] * 5
    assert res.layers[Layer.L5].status is LayerStatus.NOT_APPLICABLE
    assert res.execution is ExecutionVerdict.COMPLETE
    assert res.scientific is ScientificVerdict.NOT_APPLICABLE


def test_e_rejects_a_claim_that_disagrees_with_replay():
    res = evaluate(_e_input({"family": "E", "run_id": "run1",
                             "claim": {"distance": 0.0001, "final_speed": 0.001}}))
    assert res.layers[Layer.L4].status is LayerStatus.FAIL
    assert res.layers[Layer.L4].reason_code == "CLAIM_MISMATCH"
    assert res.execution is ExecutionVerdict.FAILED


def test_e_rejects_target_not_met():
    far = [[0, 0.0, 0.0, 0.0, 0.0], [10, 0.30, 0.0, 0.001, 0.0]]
    m = series_metrics(far, [0.6, 0.0])
    res = evaluate(_e_input({"family": "E", "run_id": "run1",
                             "claim": {"distance": m["distance"],
                                       "final_speed": m["final_speed"]}},
                            series=far))
    assert res.layers[Layer.L4].reason_code == "TARGET_NOT_MET"


def test_unknown_run_id_fails_at_l0_and_blocks_lower_layers():
    res = evaluate(_e_input({"family": "E", "run_id": "nope",
                             "claim": {"distance": 0.0, "final_speed": 0.0}}))
    assert res.layers[Layer.L0].status is LayerStatus.FAIL
    for L in (Layer.L1, Layer.L2, Layer.L3, Layer.L4):
        assert res.layers[L].status is LayerStatus.NOT_REACHED


def test_replay_mismatch_is_fabrication():
    inp = _e_input({"family": "E", "run_id": "run1",
                    "claim": {"distance": 0.001, "final_speed": 0.001}})
    inp = EvaluationInput(**{**inp.__dict__,
                             "replay": lambda _p: {"events_sha256": "OTHER"}})
    res = evaluate(inp)
    assert res.layers[Layer.L1].status is LayerStatus.FAIL
    assert res.layers[Layer.L1].reason_code == "REPLAY_MISMATCH"
    assert res.layers[Layer.L4].status is LayerStatus.NOT_REACHED


def test_scene_and_step_tampering_are_caught():
    bad_scene = [_call("run1", {"kp": 1.0, "steps": 10}, "ev1", scene="b" * 64)]
    res = evaluate(_e_input({"family": "E", "run_id": "run1",
                             "claim": {"distance": 0.001, "final_speed": 0.001}},
                            calls=bad_scene))
    assert res.layers[Layer.L2].reason_code == "SCENE_MISMATCH"

    bad_steps = [_call("run1", {"kp": 1.0, "steps": 10}, "ev1")]
    bad_steps[0]["steps_run"] = 9
    res = evaluate(_e_input({"family": "E", "run_id": "run1",
                             "claim": {"distance": 0.001, "final_speed": 0.001}},
                            calls=bad_steps))
    assert res.layers[Layer.L3].reason_code == "STEPS_MISMATCH"


# ---------------------------------------------------------------------------
# D 族
# ---------------------------------------------------------------------------
def _d_probe_runs(dt=0.001, A=1.0, B=4.905):
    runs = []
    for direction in (+1, -1):
        for force in (13.0, 16.0):
            a = A * force - B
            params = {"target_x": direction * 30.0, "max_force": force,
                      "kp": 200.0, "kd": 0.0, "steps": 200}
            runs.append({"run_id": f"r{direction}{force}", "params": params,
                         "events_sha256": f"ev{direction}{force}",
                         "scene_sha256": SCENE, "steps_run": 200,
                         "series": _accel_series(a, dt, 200)})
    return runs


def _d_input(answer, runs=None):
    runs = runs or _d_probe_runs()
    calls = [{k: v for k, v in r.items() if k != "series"} for r in runs]
    return EvaluationInput(
        task=_d_task(), answer=answer, calls=calls,
        replay=lambda c: next(r for r in runs if r["run_id"] == c["run_id"]),
        truth={"fault": {"kind": "friction_scale", "scale": 2.0},
               "nominal": {"mass": 1.0, "friction": 0.25, "g": 9.81},
               "classify": {"asymmetry_min": 0.5, "deviation_min": 0.15}},
        priv_rules={"tolerance": {"scale_rel": 0.2}})


def test_d_correct_hypothesis_passes():
    runs = _d_probe_runs()
    answer = {"family": "D", "run_ids": [r["run_id"] for r in runs],
              "hypothesis": {"kind": "friction_scale", "scale": 2.0}}
    res = evaluate(_d_input(answer, runs))
    assert res.layers[Layer.L4].status is LayerStatus.PASS
    assert res.layers[Layer.L4].reason_code == "DIAGNOSIS_CORRECT"


def test_d_wrong_kind_is_rejected():
    runs = _d_probe_runs()
    answer = {"family": "D", "run_ids": [r["run_id"] for r in runs],
              "hypothesis": {"kind": "scene_tilt", "scale": 2.0}}
    res = evaluate(_d_input(answer, runs))
    assert res.layers[Layer.L4].status is LayerStatus.FAIL
    assert res.layers[Layer.L4].reason_code == "WRONG_KIND"


def test_d_scale_outside_tolerance_is_rejected():
    runs = _d_probe_runs(A=1.0, B=4.905 * 1.6)   # 复算得 1.6，真值 2.0 ⇒ 20% 外
    answer = {"family": "D", "run_ids": [r["run_id"] for r in runs],
              "hypothesis": {"kind": "friction_scale", "scale": 1.6}}
    res = evaluate(_d_input(answer, runs))
    assert res.layers[Layer.L4].reason_code == "SCALE_OUT_OF_TOLERANCE"


# ---------------------------------------------------------------------------
# R 族
# ---------------------------------------------------------------------------
def _r_runs():
    runs = []
    for damping, settle in ((0.3, 2000), (0.6, 1000)):
        for angle in (0.9, -0.9):
            rid = f"r{damping}{angle}"
            params = {"damping": damping, "initial_angle": angle, "steps": 2000}
            runs.append({"run_id": rid, "params": params, "events_sha256": rid,
                         "scene_sha256": SCENE, "steps_run": 2000,
                         "angle_series": _settle_series(settle, 3000)})
    return runs


def _r_input(answer, runs=None, prereg="planhash"):
    runs = runs or _r_runs()
    calls = [{k: v for k, v in r.items() if k != "angle_series"} for r in runs]
    return EvaluationInput(
        task=_r_task(), answer=answer, calls=calls,
        replay=lambda c: next(r for r in runs if r["run_id"] == c["run_id"]),
        truth={"expected": "SUPPORTED"},
        priv_rules={"equivalence_bound": 0.05, "support_threshold": 0.15},
        prereg_sha256=prereg)


def _r_answer(**over):
    base = {"family": "R", "run_ids": [r["run_id"] for r in _r_runs()],
            "verdict": "SUPPORTED", "effect": 0.5,
            "criteria": {c: "evidence" for c in RESEARCH_CRITERIA},
            "scope": "无接触、有限步长 0–2 s 的范围内成立"}
    base.update(over)
    return base


def test_r_happy_path_reaches_l5_pass():
    res = evaluate(_r_input(_r_answer()))
    assert res.layers[Layer.L4].status is LayerStatus.PASS
    assert res.layers[Layer.L5].status is LayerStatus.PASS, res.layers[Layer.L5]
    assert res.scientific is ScientificVerdict.SUPPORTED


def test_r_missing_criterion_is_rejected_at_l0():
    criteria = {c: "evidence" for c in RESEARCH_CRITERIA if c != "C7_scope_statement"}
    res = evaluate(_r_input(_r_answer(criteria=criteria)))
    assert res.layers[Layer.L0].status is LayerStatus.FAIL


def test_r_wrong_verdict_fails_l5():
    res = evaluate(_r_input(_r_answer(verdict="REFUTED")))
    assert res.layers[Layer.L5].status is LayerStatus.FAIL
    assert res.scientific is ScientificVerdict.INCONCLUSIVE


def test_r_missing_prereg_fails_c1():
    res = evaluate(_r_input(_r_answer(), prereg=None))
    assert res.layers[Layer.L5].status is LayerStatus.FAIL
    assert res.layers[Layer.L5].reason_code.startswith("C1")


def test_r_inconclusive_without_diagnostics_is_rejected():
    runs = []
    for damping, settle in ((0.3, 2000), (0.6, 1990)):
        for angle in (0.9, -0.9):
            params = {"damping": damping, "initial_angle": angle, "steps": 2000}
            runs.append({"run_id": f"{damping}{angle}", "params": params,
                         "events_sha256": f"{damping}{angle}", "scene_sha256": SCENE,
                         "steps_run": 2000,
                         "angle_series": _settle_series(settle, 3000)})
    answer = _r_answer(run_ids=[r["run_id"] for r in runs],
                       verdict="INCONCLUSIVE_EQUIVALENT", effect=0.005)
    inp = _r_input(answer, runs)
    inp = EvaluationInput(**{**inp.__dict__,
                             "truth": {"expected": "INCONCLUSIVE_EQUIVALENT"}})
    res = evaluate(inp)
    assert res.layers[Layer.L5].status is LayerStatus.FAIL
    assert any("C8" in e for e in res.layers[Layer.L5].evidence)
