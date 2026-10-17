"""Task2 ascent 的形式化 Pareto 支配判定（Phase_2_Plan_detailed.md L406-414）。

只做**逐指标 Pareto 支配**，不做加权总分、不做标量化（AST 测试
test_pareto_no_weighted_total 钉死：本模块禁止出现权重/总分概念）。

指标方向（显式方向表，唯一真值；+1 越大越好，-1 越小越好）：

    success_rate            +1   （越大越好，ε 为绝对 0）
    slip_dist_mean_m        -1
    postpush_vmax_mean_mps  -1
    tilt_max_deg_max        -1
    solve_wallclock_s       -1
    vram_peak_gb            -1

容差 ε：success_rate 绝对 0（任何提升都算数）；其余指标相对 V1 基准值
5%（``eps = 0.05 * |v1|``；V1 值为 0 时 ε=0，严格比较）。容差带**单向
锚定 V1**（第一参数，冻结档案）：verdict 是 V2 相对 V1 的**有序关系**，
调用必须 ``pareto_verdict(v1=档案, v2=候选)``，**禁止反序比较**——互换
角色会以新第一参数重锚容差带，不承诺角色互换镜像（QA-1 LOW-1 修正；
v2_dominates 与 dominated_by_v1 的互补性只在同一次判定、同一 V1 锚点
下成立）。

三判（verdict）：

- ``v2_dominates``：每个指标 V2 都不差于 V1（ε 内视为持平），且至少一个
  指标严格优于 V1（超出 ε）。
- ``dominated_by_v1``：镜像——V2 无严格优于项，且至少一个指标严格劣于。
- ``non_dominated``：其余一切情形（互有胜负，或全部 ε 内持平）。

非法输入一律 fail-closed（ParetoInputError）：缺指标/多余指标/非有限值
（NaN/inf）/布尔冒充数值/方向表与规范常量冲突。判定只用**重算后的汇总
值**；脚本自报 outcome 不得进入比较（由调用方保证口径）。
"""
from __future__ import annotations

import math
from typing import Mapping

# 显式方向表：+1 越大越好，-1 越小越好。
METRIC_DIRECTIONS: "dict[str, int]" = {
    "success_rate": 1,
    "slip_dist_mean_m": -1,
    "postpush_vmax_mean_mps": -1,
    "tilt_max_deg_max": -1,
    "solve_wallclock_s": -1,
    "vram_peak_gb": -1,
}

# 容差表：(模式, 参数)。success_rate 绝对 0；其余相对 V1 基准 5%。
_EPS_ABSOLUTE = "absolute"
_EPS_RELATIVE = "relative"
METRIC_EPSILON: "dict[str, tuple[str, float]]" = {
    "success_rate": (_EPS_ABSOLUTE, 0.0),
    "slip_dist_mean_m": (_EPS_RELATIVE, 0.05),
    "postpush_vmax_mean_mps": (_EPS_RELATIVE, 0.05),
    "tilt_max_deg_max": (_EPS_RELATIVE, 0.05),
    "solve_wallclock_s": (_EPS_RELATIVE, 0.05),
    "vram_peak_gb": (_EPS_RELATIVE, 0.05),
}

V2_DOMINATES = "v2_dominates"
DOMINATED_BY_V1 = "dominated_by_v1"
NON_DOMINATED = "non_dominated"
_VERDICTS = frozenset({V2_DOMINATES, DOMINATED_BY_V1, NON_DOMINATED})


class ParetoInputError(ValueError):
    """Pareto 输入非法：缺件/非有限/方向冲突——fail-closed，不猜测。"""


def _validate_tables(directions: "Mapping[str, int]",
                     epsilons: "Mapping[str, tuple[str, float]]") -> None:
    if set(directions) != set(METRIC_DIRECTIONS):
        raise ParetoInputError(
            f"方向表指标集必须恰为 {sorted(METRIC_DIRECTIONS)}，"
            f"收到 {sorted(directions)}")
    if set(epsilons) != set(METRIC_EPSILON):
        raise ParetoInputError(
            f"容差表指标集必须恰为 {sorted(METRIC_EPSILON)}，"
            f"收到 {sorted(epsilons)}")
    for metric, sign in directions.items():
        if sign not in (1, -1):
            raise ParetoInputError(f"方向只允许 +1/-1：{metric}={sign!r}")
        # 方向冲突：调用方传入的方向与规范常量不一致即拒绝。
        if sign != METRIC_DIRECTIONS[metric]:
            raise ParetoInputError(
                f"指标 {metric} 方向 {sign} 与规范方向 "
                f"{METRIC_DIRECTIONS[metric]} 冲突")
    for metric, (mode, value) in epsilons.items():
        # 容差表允许调用方注入同形表（默认即规范表 METRIC_EPSILON）；方向表
        # 才禁止与规范冲突。模式/取值仍严格校验（非法即 fail-closed）。
        if mode not in (_EPS_ABSOLUTE, _EPS_RELATIVE):
            raise ParetoInputError(f"容差模式非法：{metric}={mode!r}")
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(float(value)) or value < 0:
            raise ParetoInputError(f"容差必须为有限非负数：{metric}={value!r}")


def _validate_vector(name: str, vector: "Mapping[str, float]",
                     metrics: "set[str]") -> None:
    if not isinstance(vector, Mapping):
        raise ParetoInputError(f"{name} 必须是映射，收到 {type(vector).__name__}")
    if set(vector) != metrics:
        missing = sorted(metrics - set(vector))
        extra = sorted(set(vector) - metrics)
        raise ParetoInputError(
            f"{name} 指标集不符（缺 {missing}，多余 {extra}）；"
            "缺指标/多指标一律 fail-closed")
    for metric, value in vector.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParetoInputError(
                f"{name}[{metric}] 必须是实数，收到 {type(value).__name__}")
        if not math.isfinite(float(value)):
            raise ParetoInputError(
                f"{name}[{metric}]={value!r} 非有限值（NaN/inf），fail-closed")


def _epsilon(metric: str, v1_value: float,
             epsilons: "Mapping[str, tuple[str, float]]") -> float:
    mode, value = epsilons[metric]
    if mode == _EPS_ABSOLUTE:
        return float(value)
    return float(value) * abs(v1_value)


def pareto_verdict(v1_metrics: "Mapping[str, float]",
                   v2_metrics: "Mapping[str, float]", *,
                   directions: "Mapping[str, int] | None" = None,
                   epsilons: "Mapping[str, tuple[str, float]] | None" = None
                   ) -> str:
    """比较 V2（ascent 提案）相对 V1（档案基线）的 Pareto 支配关系。

    返回 ``v2_dominates`` / ``dominated_by_v1`` / ``non_dominated`` 之一。
    directions 与规范方向冲突即 ParetoInputError（方向表不允许私改）；
    epsilons 允许注入同形容差表（默认即 METRIC_EPSILON），模式/取值非法
    仍 fail-closed。判定输入只允许重算汇总值，调用方不得混入脚本自报。
    """
    dirs = METRIC_DIRECTIONS if directions is None else directions
    eps = METRIC_EPSILON if epsilons is None else epsilons
    _validate_tables(dirs, eps)
    metrics = set(METRIC_DIRECTIONS)
    _validate_vector("v1_metrics", v1_metrics, metrics)
    _validate_vector("v2_metrics", v2_metrics, metrics)

    better = worse = False
    for metric in METRIC_DIRECTIONS:  # 固定顺序，结论可复现
        v1 = float(v1_metrics[metric])
        v2 = float(v2_metrics[metric])
        signed_gain = dirs[metric] * (v2 - v1)  # >0: V2 更优
        band = _epsilon(metric, v1, eps)
        if signed_gain > band:
            better = True
        elif signed_gain < -band:
            worse = True
        # |signed_gain| <= band：ε 内持平，不计胜负。

    if better and not worse:
        return V2_DOMINATES
    if worse and not better:
        return DOMINATED_BY_V1
    return NON_DOMINATED  # 互有胜负，或全部 ε 内持平（无严格胜项）
