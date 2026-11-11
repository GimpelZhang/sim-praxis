"""RSI-R1 T1b 校准测试用**合成** oracle（合成 fixture，非闭源真参考）。

真 oracle 只在 campaign ``private/calibration_private/``（候选不可挂载）；
本件仅用于离线测试校准模块的接线与检查语义，不含研究结论或最优参数。
"""
from __future__ import annotations

import math

ORACLE_SCHEMA = "simpraxis.rsi_r1.calibration_oracle.v1"
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


def study_b_rmse(*, inertia_ratio: float, amplitude_rad: float, freq_hz: float,
                 tc_s: float, dt_s: float, controller: str, kp: float,
                 kd: float, duration_s: float = 4.0,
                 window_start_s: float = 0.5, window_end_s: float = 4.0,
                 refine: int = REFINE) -> dict:
    I = I0 * float(inertia_ratio)
    dt = float(dt_s)
    stride = int(round(float(tc_s) / dt))
    n = int(round(float(duration_s) / dt))
    h = dt / int(refine)
    amp, freq = float(amplitude_rad), float(freq_hz)
    q = w = tau = 0.0
    sq = count = 0
    for i in range(n):
        if i % stride == 0:
            target_now = amp * math.sin(2.0 * math.pi * freq * i * dt)
            cmd = kp * (target_now - q)
            if controller == "PD":
                cmd -= kd * w
            tau = max(-TORQUE_LIMIT, min(TORQUE_LIMIT, cmd))
        for _ in range(int(refine)):
            q, w = _zoh(q, w, tau, h, I, B_DAMPING)
        t_end = (i + 1) * dt
        if window_start_s <= t_end <= window_end_s:
            sq += (q - amp * math.sin(2.0 * math.pi * freq * t_end)) ** 2
            count += 1
    return {"normalized_rmse": math.sqrt(sq / count) / amp,
            "samples": count, "refine": int(refine),
            "reference_saturations": 0}
