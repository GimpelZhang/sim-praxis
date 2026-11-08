"""RSI-R0 参考分析：判据的**纯函数**（无 IO、无容器、无随机），单测直接命中。

这些函数是"参考怎么读数据"的唯一真值。它们被
:mod:`scripts.rsi_r0.t2_reference`（实跑）与 :mod:`tests.test_rsi_r0_t2`（判据
回归）共用，因此放在引擎包里而不是脚本里。

三条判据都**事前预注册**，不许看完数据再调：

* 执行（E）：``distance`` / ``final_speed`` 落在私有容差内；
* 诊断（D）：先看双向非对称（倾斜），再看斜率（执行增益），最后看截距（摩擦）；
* 研究（R）：方向一致且效应 ≥ 支持阈 ⇒ SUPPORTED；|效应| ≤ 等效界 ⇒
  INCONCLUSIVE_EQUIVALENT（**诚实阴性**）；两者之间 ⇒ INCONCLUSIVE。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

G = 9.81

#: R 族沉降判据：角度与角速度**连续**保持在容差内的第一个时刻（秒）。
SETTLE_ANGLE_TOL = 0.05
SETTLE_VEL_TOL = 0.05


def fit_slope(series: Sequence[Sequence[float]], dt: float) -> tuple[float, float]:
    """对 ``v(t)`` 做最小二乘直线拟合，返回 ``(a, v0)``。

    端点差分（``v_end / T``）会把起步的静摩擦瞬态与末段接触软化一起算进去，
    2026-11-08 实测 A 的误差到 ±15%；换整条速度序列拟合后降到 ≤5%。
    """
    n = len(series)
    if n < 3:
        raise ValueError("序列太短，无法拟合加速度")
    ts = [i * dt for i in range(n)]
    vs = [math.hypot(row[3], row[4]) for row in series]
    mt = sum(ts) / n
    mv = sum(vs) / n
    den = sum((t - mt) ** 2 for t in ts)
    if den == 0:
        raise ValueError("时间轴退化，无法拟合")
    slope = sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / den
    return slope, mv - slope * mt


def classify_fault(a_pos: float, b_pos: float, a_neg: float, b_neg: float,
                   nominal: Mapping[str, float], rules: Mapping[str, float]) -> dict[str, Any]:
    """把 ``a(F)=A·F−B`` 的双向拟合结果归类为三种故障之一。

    物理依据（m 为公开标称质量、μ 为公开标称摩擦、g=9.81）：

    * 摩擦放大 r ⇒ ``A=1/m`` 不变、``B=r·μ·g``，**两个方向相同**；
    * 执行增益 s ⇒ ``A=s/m`` 变、``B=μ·g`` 不变，两个方向相同；
    * 地面倾斜 θ ⇒ ``A=1/m`` 不变，但 ``B±=μ·g·cosθ ∓ g·sinθ``，**两个方向相反地偏**。

    所以判据顺序是：先看非对称（倾斜），再看斜率（执行增益），最后看截距（摩擦）。
    质量缩放与执行增益在纯惯性观测里只以 ``s/m`` 的形式出现，**物理上不可辨识**，
    因此不在故障集合里（2026-11-08 设计修正）。
    """
    a0 = 1.0 / float(nominal["mass"])
    b0 = float(nominal["friction"]) * float(nominal["g"])
    a_mean = 0.5 * (a_pos + a_neg)
    b_mean = 0.5 * (b_pos + b_neg)
    asymmetry = abs(b_neg - b_pos)
    if asymmetry > rules["asymmetry_min"]:
        kind = "scene_tilt"
        scale = math.degrees(math.asin(max(-1.0, min(1.0, asymmetry / (2 * G)))))
    elif abs(a_mean - a0) / a0 > rules["deviation_min"]:
        kind = "force_scale"
        scale = a_mean * float(nominal["mass"])
    elif abs(b_mean - b0) / b0 > rules["deviation_min"]:
        kind = "friction_scale"
        scale = b_mean / b0
    else:
        kind, scale = "none", 1.0
    return {"kind": kind, "scale": scale, "a_mean": a_mean, "b_mean": b_mean,
            "asymmetry": asymmetry, "nominal_A": a0, "nominal_B": b0}


def settling_time(result: Mapping[str, Any], dt: float) -> dict[str, Any]:
    """``|angle|`` 与 ``|vel|`` **连续**落在容差内的第一个时刻；全程未落定即删失。"""
    series = result["angle_series"]
    last_bad = -1
    for idx, (_step, ang, vel) in enumerate(series):
        if abs(ang) >= SETTLE_ANGLE_TOL or abs(vel) >= SETTLE_VEL_TOL:
            last_bad = idx
    if last_bad >= len(series) - 1:
        return {"settle_s": None, "censored": True, "steps": len(series)}
    return {"settle_s": round((last_bad + 1) * dt, 6), "censored": False,
            "steps": len(series)}


def research_verdict(effects: Mapping[str, float | None], priv: Mapping[str, Any]) -> str:
    """预注册判决：方向一致 + 效应 ≥ 支持阈 ⇒ SUPPORTED；|效应| ≤ 等效界 ⇒
    INCONCLUSIVE_EQUIVALENT（诚实阴性，不是"支持"）；有删失 ⇒ CENSORED。"""
    valid = [v for v in effects.values() if v is not None]
    if not valid or len(valid) != len(effects):
        return "CENSORED"
    consistent = all(v > 0 for v in valid) or all(v < 0 for v in valid)
    mean_effect = sum(valid) / len(valid)
    threshold = float(priv.get("support_threshold", 0.15))
    bound = float(priv["equivalence_bound"])
    if not consistent:
        # 方向不一致时**不许**判等效：等效声明要求两组的位移同向且都贴零。
        return "INCONCLUSIVE"
    if mean_effect >= threshold:
        return "SUPPORTED"
    if abs(mean_effect) <= bound:
        return "INCONCLUSIVE_EQUIVALENT"
    return "INCONCLUSIVE"
