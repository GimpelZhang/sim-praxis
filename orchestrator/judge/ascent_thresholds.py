"""Task2 ascent（RSI 螺旋升阶）加严阈值表（Phase_2_Plan_detailed.md L420-424）。

加严的是**验收严苛度与 DR 应力范围**，不改任何物理真值常数：

- 重算一致性公差 ×``TOLERANCE_FACTOR``（0.8）：
  SLIP_DIST_REL_TOL 0.05→0.04、VMAX_ABS_TOL 0.005→0.004、
  TILT_MAX_ABS_TOL_DEG 0.01→0.008。
- 接触力/滑移数值界 ×``ENVELOPE_BOUND_FACTOR``（0.8，更严包络）：
  接触积分力门 2.0→1.6 N、滑移速度物理界 50→40 m/s、
  名义接触力界 10000→8000 N、DR 接触力界 20000→16000 N。
- DR 应力包络对数区间向**应力端**外扩 ``DR_LOG_EXTEND``（30%）：
  μ 下界（更滑）0.05→0.035、Kp 上界（更硬）1e6→1.3e6；对数另一端按
  **几何中心不动**对称外扩——对数坐标中心 ``ln c = (ln lo + ln hi)/2``
  固定，故 ``lo'·hi' = lo·hi``：μ 上界 0.4→0.05·0.4/0.035≈0.5714，
  Kp 下界 1e4→1e4·1e6/1.3e6≈7692.3。Kd 区间计划未枚举外扩，tight 中取
  登记基线（DR_KD_LOGUNIFORM=(15.0,200.0)，同源真实 V1 summary.json）
  逐值对质后透传；漂移或缺失即 AscentThresholdError（QA-1 LOW-3 收紧，
  原「None 透传不臆造」适配被基线常量取代；锁内适配延伸，Task2 QA-1 报告列明）。

物理/预注册锚点 **FALL_Z_M / FALL_TILT_DEG / 35N×0.2s / CONTRAST_RATIO**
不进加严表，直接 re-export gait_thresholds 的同一对象（``is`` 身份不变）。
CONTRAST_RATIO=2.0 是 Q1 预注册界（硬规则 7 / 计划 §1.4）：V1 实测 0.824
的矛盾结论不得因加严而翻案。
"""
from __future__ import annotations

from typing import Mapping

from .gait_thresholds import (
    CONTACT_FORCE_MAX_N,
    CONTACT_FORCE_MAX_N_DR,
    CONTACT_FORCE_N,
    CONTRAST_RATIO,
    DR_KD_LOGUNIFORM,
    DR_KP_LOGUNIFORM,
    DR_MU_LOGUNIFORM,
    FALL_TILT_DEG,
    FALL_Z_M,
    PUSH_DUR_S,
    PUSH_FORCE_N,
    SLIP_DIST_REL_TOL,
    SLIP_V_MAX_MPS,
    TILT_MAX_ABS_TOL_DEG,
    VMAX_ABS_TOL,
)

# ---- 因子集中配置（唯一因子源；禁止在他处复制魔数） ----
TOLERANCE_FACTOR = 0.8          # 重算一致性公差 ×0.8
ENVELOPE_BOUND_FACTOR = 0.8     # 接触力/滑移数值界 ×0.8（更严）
DR_LOG_EXTEND = 0.30            # DR 对数应力区间应力端外扩 30%

# ---- 物理/预注册锚点：原样 re-export 同一对象，禁止改值 ----
__all_anchors = {
    "FALL_Z_M": FALL_Z_M,
    "FALL_TILT_DEG": FALL_TILT_DEG,
    "PUSH_FORCE_N": PUSH_FORCE_N,
    "PUSH_DUR_S": PUSH_DUR_S,
    "CONTRAST_RATIO": CONTRAST_RATIO,
}

# ---- 加严后的重算一致性公差 ----
SLIP_DIST_REL_TOL_TIGHT = SLIP_DIST_REL_TOL * TOLERANCE_FACTOR       # 0.04
VMAX_ABS_TOL_TIGHT = VMAX_ABS_TOL * TOLERANCE_FACTOR                 # 0.004
TILT_MAX_ABS_TOL_DEG_TIGHT = TILT_MAX_ABS_TOL_DEG * TOLERANCE_FACTOR  # 0.008

# ---- 加严后的接触力/滑移数值界 ----
CONTACT_FORCE_N_TIGHT = CONTACT_FORCE_N * ENVELOPE_BOUND_FACTOR             # 1.6
SLIP_V_MAX_MPS_TIGHT = SLIP_V_MAX_MPS * ENVELOPE_BOUND_FACTOR               # 40.0
CONTACT_FORCE_MAX_N_TIGHT = CONTACT_FORCE_MAX_N * ENVELOPE_BOUND_FACTOR     # 8000.0
CONTACT_FORCE_MAX_N_DR_TIGHT = CONTACT_FORCE_MAX_N_DR * ENVELOPE_BOUND_FACTOR  # 16000.0


class AscentThresholdError(RuntimeError):
    """V1 档案包络与登记基线不符：禁止在漂移基线上构造加严表（STOP 重取）。"""


def extend_log_bounds(lower: float, upper: float, *, stress_end: str,
                      extend: float = DR_LOG_EXTEND) -> tuple[float, float]:
    """对数区间向应力端线性外扩 ``extend``（30%），另一端按对数中心对称。

    应力端取法（计划 L422）：
    - stress_end="low"（μ：更滑更难）：``lo' = lo·(1-extend)``；
    - stress_end="high"（Kp：更硬更冲击）：``hi' = hi·(1+extend)``。

    对数对称端：几何中心 ``c²=lo·hi`` 不动（``ln c = (ln lo+ln hi)/2``），
    故另一端 ``x' = lo·hi / stress'``。返回 (new_lower, new_upper)。
    """
    if not (0.0 < lower < upper):
        raise AscentThresholdError(f"非法对数区间：({lower!r}, {upper!r})")
    if not 0.0 < extend < 1.0:
        raise AscentThresholdError(f"非法外扩比例：{extend!r}")
    if stress_end == "low":
        new_low = lower * (1.0 - extend)
        new_high = lower * upper / new_low
    elif stress_end == "high":
        new_high = upper * (1.0 + extend)
        new_low = lower * upper / new_high
    else:
        raise AscentThresholdError(
            f"stress_end 只允许 'low'|'high'，收到 {stress_end!r}")
    return new_low, new_high


DR_MU_LOGUNIFORM_TIGHT = extend_log_bounds(*DR_MU_LOGUNIFORM, stress_end="low")
DR_KP_LOGUNIFORM_TIGHT = extend_log_bounds(*DR_KP_LOGUNIFORM, stress_end="high")


def _pair(value: object) -> "tuple[float, float] | None":
    if isinstance(value, (list, tuple)) and len(value) == 2 \
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in value):
        return float(value[0]), float(value[1])
    return None


def build_targets(dr_envelope: "Mapping | None") -> dict:
    """由 V1 summary 的 dr_envelope 构造 ascent_targets（classify_run 调用）。

    登记的 μ/Kp/Kd 基线必须与 gait_thresholds 常量逐值相等（档案漂移即
    AscentThresholdError，计划 STOP：重取档案，禁止改表迁就）。Kd 不外扩；
    tight 中取登记基线逐值对质后透传（QA-1 LOW-3：缺失同样拒收，不再 None
    透传）。
    """
    if not isinstance(dr_envelope, Mapping):
        raise AscentThresholdError(
            f"V1 档案缺 dr_envelope 映射：{type(dr_envelope).__name__}")
    mu = _pair(dr_envelope.get("mu_loguniform"))
    kp = _pair(dr_envelope.get("Kp_loguniform"))
    if mu is None or mu != DR_MU_LOGUNIFORM:
        raise AscentThresholdError(
            f"V1 μ 包络 {mu} 与登记基线 {DR_MU_LOGUNIFORM} 不符；STOP 重取档案")
    if kp is None or kp != DR_KP_LOGUNIFORM:
        raise AscentThresholdError(
            f"V1 Kp 包络 {kp} 与登记基线 {DR_KP_LOGUNIFORM} 不符；STOP 重取档案")
    # QA-1 LOW-3（锁内适配延伸）：Kd 虽不外扩，也必须与登记基线逐值对质，
    # 缺失/漂移零报错进入 tight 表会让加严 DR 覆盖在未知阻尼区间上执行。
    kd = _pair(dr_envelope.get("Kd_loguniform"))
    if kd is None or kd != DR_KD_LOGUNIFORM:
        raise AscentThresholdError(
            f"V1 Kd 包络 {kd} 与登记基线 {DR_KD_LOGUNIFORM} 不符（含缺失）；"
            "STOP 重取档案")
    return {
        "factors": {
            "recompute_tolerance": TOLERANCE_FACTOR,
            "contact_slip_bound": ENVELOPE_BOUND_FACTOR,
            "dr_log_extend": DR_LOG_EXTEND,
        },
        "recompute_tolerances": {
            "slip_dist_rel_tol": SLIP_DIST_REL_TOL_TIGHT,
            "vmax_abs_tol": VMAX_ABS_TOL_TIGHT,
            "tilt_max_abs_tol_deg": TILT_MAX_ABS_TOL_DEG_TIGHT,
        },
        "physical_bounds": {
            "contact_force_n": CONTACT_FORCE_N_TIGHT,
            "slip_v_max_mps": SLIP_V_MAX_MPS_TIGHT,
            "contact_force_max_n": CONTACT_FORCE_MAX_N_TIGHT,
            "contact_force_max_n_dr": CONTACT_FORCE_MAX_N_DR_TIGHT,
        },
        "anchors_unchanged": dict(__all_anchors),
        "dr_envelope_v1": {
            "mu_loguniform": list(mu),
            "Kp_loguniform": list(kp),
            # Kd 不外扩：登记基线逐值对质通过后原值透传。
            "Kd_loguniform": list(kd),
        },
        "dr_envelope_tight": {
            "mu_loguniform": list(DR_MU_LOGUNIFORM_TIGHT),
            "Kp_loguniform": list(DR_KP_LOGUNIFORM_TIGHT),
            "Kd_loguniform": list(kd),
        },
    }


def tight_profile() -> "dict[str, float]":
    """gait_rungs 加严 profile（键集由 gait_rungs.PROFILE_KEYS 钉死）。

    Task6 V2 batch 以本 profile 独立重算 Rung1–3（V1 档案仍用默认 profile
    在 V1 阈值下重算；两组数字口径分离，Pareto 只比六项汇总值）。锚点
    （FALL_Z_M/FALL_TILT_DEG/35N×0.2s/CONTRAST_RATIO）不是 profile 键，
    消费侧恒走 gait_thresholds 常量，任何加严都改不了它们。
    """
    return {
        "slip_dist_rel_tol": SLIP_DIST_REL_TOL_TIGHT,
        "vmax_abs_tol": VMAX_ABS_TOL_TIGHT,
        "tilt_max_abs_tol_deg": TILT_MAX_ABS_TOL_DEG_TIGHT,
        "contact_force_n": CONTACT_FORCE_N_TIGHT,
        "slip_v_max_mps": SLIP_V_MAX_MPS_TIGHT,
        "contact_force_max_n": CONTACT_FORCE_MAX_N_TIGHT,
        "contact_force_max_n_dr": CONTACT_FORCE_MAX_N_DR_TIGHT,
    }
