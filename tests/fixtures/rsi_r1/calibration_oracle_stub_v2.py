"""RSI-R1 T1b 校准 v2 测试用**合成** oracle（合成 fixture，非闭源真参考）。

与 v1 stub（``calibration_oracle_stub.py``, schema ``…v1``, ``REFINE=4``）的区别只在
schema 与新增的 ``study_b_trace``；``study_b_rmse`` 的动力学与求和顺序逐字相同，
因此 ``calibration.assert_oracle_v2_equivalence`` 在两件上都成立（Δ=0.0）。
本件仅用于离线测试 v2 接线与检查语义，不含研究结论或最优参数。
"""
from __future__ import annotations

import math

ORACLE_SCHEMA = "simpraxis.rsi_r1.calibration_oracle.v2"
REFINE = 4
G = 9.81
I0 = 0.05
B_DAMPING = 0.05
TORQUE_LIMIT = 2.0


def study_a_oracle(*, mu: float, mass_kg: float, v0: float,
                   duration_s: float = 4.0) -> dict:
    a = float(mu) * G
    t_stop = float(v0) / a
    if t_stop <= duration_s:
        return {"displacement_m": v0 * v0 / (2.0 * a),
                "stop_time_s": t_stop, "censored": False}
    return {"displacement_m": v0 * duration_s - 0.5 * a * duration_s ** 2,
            "stop_time_s": None, "censored": True}


def _zoh(q, w, tau, h, I, b):
    w_new = (I * w + h * tau) / (I + h * b)
    return q + h * w_new, w_new


def _simulate(*, inertia_ratio: float, amplitude_rad: float, freq_hz: float,
              tc_s: float, dt_s: float, controller: str, kp: float,
              kd: float, duration_s: float = 4.0, refine: int = REFINE):
    I = I0 * float(inertia_ratio)
    dt = float(dt_s)
    stride = int(round(float(tc_s) / dt))
    n = int(round(float(duration_s) / dt))
    h = dt / int(refine)
    amp, freq = float(amplitude_rad), float(freq_hz)
    q = w = tau = 0.0
    saturations = updates = 0
    trace = [0.0] * n
    for i in range(n):
        if i % stride == 0:
            target_now = amp * math.sin(2.0 * math.pi * freq * i * dt)
            cmd = kp * (target_now - q)
            if controller == "PD":
                cmd -= kd * w
            if abs(cmd) > TORQUE_LIMIT:
                saturations += 1
            tau = max(-TORQUE_LIMIT, min(TORQUE_LIMIT, cmd))
            updates += 1
        for _ in range(int(refine)):
            q, w = _zoh(q, w, tau, h, I, B_DAMPING)
        trace[i] = q
    return trace, saturations, dt, updates


def study_b_rmse(*, inertia_ratio: float, amplitude_rad: float, freq_hz: float,
                 tc_s: float, dt_s: float, controller: str, kp: float,
                 kd: float, duration_s: float = 4.0,
                 window_start_s: float = 0.5, window_end_s: float = 4.0,
                 refine: int = REFINE) -> dict:
    amp = float(amplitude_rad)
    trace, saturations, dt, _updates = _simulate(
        inertia_ratio=inertia_ratio, amplitude_rad=amp, freq_hz=freq_hz,
        tc_s=tc_s, dt_s=dt_s, controller=controller, kp=kp, kd=kd,
        duration_s=duration_s, refine=refine)
    sq = 0.0
    count = 0
    for i, q in enumerate(trace):
        t_end = (i + 1) * dt
        if window_start_s <= t_end <= window_end_s:
            sq += (q - amp * math.sin(2.0 * math.pi * float(freq_hz) * t_end)) ** 2
            count += 1
    return {"normalized_rmse": math.sqrt(sq / count) / amp,
            "samples": count, "refine": int(refine),
            "reference_saturations": saturations}


def study_b_trace(*, inertia_ratio: float, amplitude_rad: float, freq_hz: float,
                  tc_s: float, dt_s: float, controller: str, kp: float,
                  kd: float, duration_s: float = 4.0,
                  refine: int = REFINE) -> dict:
    trace, saturations, dt, updates = _simulate(
        inertia_ratio=inertia_ratio, amplitude_rad=amplitude_rad, freq_hz=freq_hz,
        tc_s=tc_s, dt_s=dt_s, controller=controller, kp=kp, kd=kd,
        duration_s=duration_s, refine=refine)
    return {"q": trace, "time_s": [(i + 1) * dt for i in range(len(trace))],
            "reference_saturations": saturations, "updates": updates,
            "dt_s": dt}
