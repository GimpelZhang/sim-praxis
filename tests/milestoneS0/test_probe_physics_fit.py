"""S0-9 A4 物理一致性判据：恒速伪造必须拒、恒力轨迹必须过（评审 M1 回归）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "s0" / "probe_four_exits.py"


def _module():
    spec = importlib.util.spec_from_file_location("s0_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constant_velocity_forgery_is_rejected():
    probe = _module()
    ts = [i * 0.02 for i in range(50)]
    linear = [0.5 * t for t in ts]
    ok, detail = probe.physics_fit(ts, linear)
    assert ok is False
    assert abs(detail["accel_m_s2"]) < 0.1  # 噪声量级的"加速度"不得放行


def test_constant_force_trajectory_passes_with_analytic_acceleration():
    probe = _module()
    ts = [i * 0.02 for i in range(50)]
    # F=2N, m=1kg, mu=0.1 → a = 2 - 0.98 = 1.02 m/s²（解析值）
    quad = [0.5 * 1.02 * t * t for t in ts]
    ok, detail = probe.physics_fit(ts, quad)
    assert ok is True
    assert abs(detail["accel_m_s2"] - 1.02) < 1e-6
    assert detail["r2"] >= 0.999


def test_insufficient_rows_fail_closed():
    probe = _module()
    ok, detail = probe.physics_fit([0.0], [0.0])
    assert ok is False and detail["reason"] == "insufficient_rows"
