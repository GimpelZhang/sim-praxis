"""RSI-R1 指标（``metrics.py``）离线测试：只喂合成 raw，不触网、不跑容器。"""
from __future__ import annotations

import math

import pytest

from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import rubric as R

DT = 0.0005


def _a_record(*, n=8000, vx=None, x=None):
    times = [(i + 1) * DT for i in range(n)]
    if x is None:
        x = [0.4 * times[i] for i in range(n)]
    if vx is None:
        vx = [0.4 for _ in range(n)]
    return {
        "time_s": times,
        "qpos": [[x[i], 0.0, 0.05, 1.0, 0.0, 0.0, 0.0] for i in range(n)],
        "qvel": [[vx[i], 0.0, 0.0, 0.0, 0.0, 0.0] for i in range(n)],
    }


def test_study_metrics_to_dict_is_string_and_sorted():
    m = M.StudyMetrics(study_id="A", values={M.STOP_TIME: 0.1, M.DISPLACEMENT: 0.2},
                       censored={M.STOP_TIME: False})
    doc = m.to_dict()
    assert doc["values"] == {M.DISPLACEMENT: "0.2", M.STOP_TIME: "0.1"}
    assert doc["censored"] == {M.STOP_TIME: False}
    assert all(isinstance(v, str) for v in doc["values"].values())


def test_study_a_displacement_from_host_arrays():
    n = 100
    x = [0.9 - 0.004 * i for i in range(n)]
    rec = _a_record(n=n, x=x)
    m = M.compute_study_a(rec, initial_x_m=0.9)
    assert m.values[M.DISPLACEMENT] == M._dec(x[-1] - 0.9)
    # vx 恒 0.4（未停）→ 右删失，不得当 0
    assert m.censored[M.STOP_TIME] is True
    assert M.STOP_TIME not in m.values


def test_study_a_stop_time_exact_when_held():
    # rubric 的保持窗是 0.2 s（=400 步 @ dt 0.0005），样本必须够长才能证实
    n = 600
    vx = [0.4] * 100 + [0.0] * 500
    rec = _a_record(n=n, vx=vx)
    m = M.compute_study_a(rec, initial_x_m=0.0)
    assert m.censored[M.STOP_TIME] is False
    assert m.values[M.STOP_TIME] == M._dec(101 * DT)


def test_study_a_stop_time_censored_when_hold_window_truncated():
    n = 600
    # 直到最后 hold 窗内才进入静止：不足以证实保持时长 → 删失
    vx = [0.4] * 550 + [0.0] * 50
    rec = _a_record(n=n, vx=vx)
    m = M.compute_study_a(rec, initial_x_m=0.0)
    assert m.censored[M.STOP_TIME] is True


def test_study_a_rejects_non_uniform_dt():
    rec = _a_record(n=100)
    rec["time_s"][50] += 1e-3
    with pytest.raises(M.MetricError) as exc:
        M.compute_study_a(rec, initial_x_m=0.0)
    assert exc.value.code == "DT_MISMATCH"


def test_study_a_scalar_series_supported():
    n = 50
    rec = {"time_s": [(i + 1) * DT for i in range(n)],
           "qpos": [0.1 * i for i in range(n)],
           "qvel": [0.1 for _ in range(n)]}
    m = M.compute_study_a(rec, initial_x_m=0.0)
    assert m.values[M.DISPLACEMENT] == M._dec(0.1 * (n - 1))


def _b_record(*, n=4000, dt=0.001, q=None, target=None, events=None):
    times = [(i + 1) * dt for i in range(n)]
    if q is None:
        q = [0.0] * n
    if target is None:
        target = [0.0] * n
    rec = {
        "time_s": times,
        "qpos": [[q[i]] for i in range(n)],
        "qvel": [[0.0] for _ in range(n)],
        "target_q": target,
        "instability_events": list(events or []),
    }
    return rec


def test_study_b_zero_error_and_window_only():
    n = 4000
    target = [0.2] * n
    q = list(target)
    # 窗口外的巨大误差不得影响指标
    q[0] = 3.0
    rec = _b_record(n=n, q=q, target=target)
    m = M.compute_study_b(rec, amplitude_rad=0.2)
    assert m.values[M.TRACKING_RMSE] == M._dec(0.0)
    assert m.details["window_samples"] > 3000


def test_study_b_normalized_rmse_value():
    n = 4000
    off = 0.02
    target = [0.0] * n
    q = [off] * n
    rec = _b_record(n=n, q=q, target=target)
    m = M.compute_study_b(rec, amplitude_rad=0.2)
    assert float(m.values[M.TRACKING_RMSE]) == pytest.approx(off / 0.2, rel=1e-12)


def test_study_b_instability_means_loss_cap():
    rec = _b_record(n=10, events=[{"time_s": 0.01, "reason": "ABS_Q_GT_4"}])
    m = M.compute_study_b(rec, amplitude_rad=0.2)
    assert m.values[M.TRACKING_RMSE] == R.LOSS_CAP
    assert m.details["loss_applied"] is True


def test_study_b_rejects_bad_amplitude():
    rec = _b_record(n=10)
    for bad in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(M.MetricError) as exc:
            M.compute_study_b(rec, amplitude_rad=bad)
        assert exc.value.code == "PARAM_INVALID"


def test_metrics_for_dispatch_rejects_cross_study_kwargs():
    rec = _a_record(n=10)
    with pytest.raises(M.MetricError) as exc:
        M.metrics_for(rec, study_id="A", amplitude_rad=0.2)
    assert exc.value.code == "PARAM_INVALID"
    with pytest.raises(M.MetricError) as exc:
        M.metrics_for(rec, study_id="B", initial_x_m=0.0)
    assert exc.value.code == "PARAM_INVALID"
    with pytest.raises(M.MetricError) as exc:
        M.metrics_for(rec, study_id="C", initial_x_m=0.0)
    assert exc.value.code == "STUDY_UNKNOWN"
