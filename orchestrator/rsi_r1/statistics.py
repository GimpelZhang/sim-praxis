"""RSI-R1 T4：主估计量、bootstrap 区间与主张分类（公开方法，候选可用）。

方法在预注册前固定（Plan §4.1）：

* 抽样单位恒为 **block**（32 个确认 block 全进计算，绝不按 run/frame 抽）；
* 配对差 ``d_i = metric(treatment_i) - metric(control_i)``，逐 block 一一对应；
* 95% 主区间 / 90% 阴性等效区间，**percentile** 法，10000 次重抽样，
  ``linear`` 分位数，``float64``；
* 统计 seed 固定且独立于数据（``STATISTICS_SEED``），不随候选/工况变化。

这是有限样本近似，**不保证名义覆盖率**；覆盖与失败模式由离线合成分布测试报告
（见 ``tests/test_rsi_r1_statistics.py``），不得包装为数学保证。

独立性纪律（Plan §5.6）：judge 必须自己实现同一方法，**禁止 import 本模块**
（``judge.py`` 有独立的重抽样实现；两者数字逐位相同由测试互证）。
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from orchestrator.rsi_r1 import rubric as R

STATS_SCHEMA = "simpraxis.rsi_r1.statistics.v1"
SCHEMA_VERSION = 1

#: 固定独立统计 seed（预注册前选定：campaign 日期；不随数据/候选改变）。
STATISTICS_SEED = 20261111


class StatisticsError(RuntimeError):
    """统计输入不合规（非有限/分母空/区间非法）；带机器码。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _finite(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise StatisticsError("WRONG_TYPE", f"{where}={value!r} 是布尔，不是数")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise StatisticsError("WRONG_TYPE", f"{where}={value!r} 不是实数") from None
    if not math.isfinite(out):
        raise StatisticsError("NONFINITE", f"{where}={value!r} 非有限")
    return out


def _seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise StatisticsError("SEED_INVALID", f"seed={value!r} 必须是整数")
    seed = int(value)
    if seed < 0:
        raise StatisticsError("SEED_INVALID", f"seed={seed} 不能为负")
    return seed


def paired_diffs(pairs: Mapping[str, Sequence[Any]]) -> "dict[str, float]":
    """``block_id → (treatment, control)`` → ``block_id → 配对差``。

    键集合就是分母：调用方必须传完整 32 block，缺 block 不是"少一个样本"而是
    分母偷换（judge 侧会另行核对分配 manifest）。
    """
    if not isinstance(pairs, Mapping):
        raise StatisticsError("WRONG_TYPE", "pairs 必须是 block_id → (t, c) 映射")
    if not pairs:
        raise StatisticsError("EMPTY", "配对差为空：没有确认 block")
    out: "dict[str, float]" = {}
    for block_id in sorted(pairs):
        pair = pairs[block_id]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise StatisticsError("WRONG_TYPE",
                                  f"{block_id} 必须是 (treatment, control) 二元组")
        treatment = _finite(pair[0], where=f"{block_id}.treatment")
        control = _finite(pair[1], where=f"{block_id}.control")
        out[block_id] = treatment - control
    return out


def _values(diffs: "Mapping[str, float] | Sequence[Any]") -> "tuple[list[str], np.ndarray]":
    if isinstance(diffs, Mapping):
        ids = sorted(diffs)
        values = [_finite(diffs[k], where=f"diffs[{k}]") for k in ids]
    else:
        if isinstance(diffs, (str, bytes)):
            raise StatisticsError("WRONG_TYPE", "diffs 不能是字符串")
        seq = list(diffs)
        ids = [f"b{i:02d}" for i in range(1, len(seq) + 1)]
        values = [_finite(v, where=f"diffs[{i}]") for i, v in enumerate(seq)]
    if len(values) < 2:
        if not values:
            raise StatisticsError("EMPTY", "没有 block（分母为空）")
        raise StatisticsError("INSUFFICIENT_N",
                              f"至少需要 2 个 block，实得 {len(values)}")
    return ids, np.asarray(values, dtype=np.float64)


def _level(value: Any) -> Decimal:
    try:
        level = Decimal(str(value))
    except Exception:  # noqa: BLE001 - Decimal 的失败类型随输入而变
        raise StatisticsError("LEVEL_INVALID", f"level={value!r}") from None
    if not (Decimal("0") < level < Decimal("1")):
        raise StatisticsError("LEVEL_INVALID", f"level={level} 必须落 (0,1)")
    return level


def bootstrap_percentile_ci(values: "Sequence[Any] | Mapping[str, float]", *,
                            level: Any, seed: Any = STATISTICS_SEED
                            ) -> "tuple[float, float]":
    """percentile bootstrap 区间（block 重抽样，linear 分位，float64）。"""
    _, arr = _values(values)
    lv = _level(level)
    sd = _seed(seed)
    rng = np.random.default_rng(np.random.PCG64(sd))
    n = int(arr.shape[0])
    idx = rng.integers(0, n, size=(int(R.BOOTSTRAP_RESAMPLES), n))
    means = arr.astype(np.float64)[idx].mean(axis=1)
    alpha = float((Decimal("1") - lv) / Decimal("2"))
    lower, upper = np.quantile(means, [alpha, 1.0 - alpha],
                               method=str(R.BOOTSTRAP_QUANTILE))
    return float(lower), float(upper)


def estimate(diffs: "Mapping[str, float] | Sequence[Any]", *,
             level: Any = R.CI_PRIMARY, seed: Any = STATISTICS_SEED
             ) -> "dict[str, Any]":
    """主估计量：block 均值效应 + percentile 区间（附带方法与分母）。"""
    ids, arr = _values(diffs)
    lv = _level(level)
    sd = _seed(seed)
    lower, upper = bootstrap_percentile_ci(diffs, level=lv, seed=sd)
    effect = float(arr.mean())
    return {
        "schema": STATS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "metric_scope": "paired_block_difference",
        "n_blocks": int(arr.shape[0]),
        "block_ids": ids,
        "effect": effect,
        "lower": float(lower),
        "upper": float(upper),
        "level": str(lv),
        "method": {
            "bootstrap_resamples": int(R.BOOTSTRAP_RESAMPLES),
            "bootstrap_unit": str(R.BOOTSTRAP_UNIT),
            "quantile": str(R.BOOTSTRAP_QUANTILE),
            "dtype": str(R.BOOTSTRAP_DTYPE),
            "seed": sd,
        },
    }


def classify(proposition: str, delta: Any, lower: Any, upper: Any) -> str:
    """按量表固定表判 SUPPORTED/REFUTED/INCONCLUSIVE（δ 不许候选扩界）。"""
    try:
        d = Decimal(str(delta))
        lo = Decimal(str(lower))
        hi = Decimal(str(upper))
    except Exception:  # noqa: BLE001
        raise StatisticsError("WRONG_TYPE", "delta/lower/upper 必须可转 Decimal") from None
    try:
        return R.judge_proposition(proposition, d, lo, hi)
    except (ValueError, KeyError) as exc:
        raise StatisticsError("PROPOSITION_INVALID", str(exc)) from None


def claim_document(*, proposition: str, metric_id: str, diffs: Mapping[str, float],
                   level: Any = R.CI_PRIMARY, seed: Any = STATISTICS_SEED
                   ) -> "dict[str, Any]":
    """候选提交用的主张件：效应/区间/分类三件套，δ 取自量表目录。"""
    try:
        delta = R.metric_delta(metric_id)
    except KeyError:
        raise StatisticsError("METRIC_UNREGISTERED",
                              f"{metric_id!r} 未在量表注册") from None
    est = estimate(diffs, level=level, seed=seed)
    claim = classify(proposition, delta, est["lower"], est["upper"])
    return {
        "metric_id": metric_id,
        "proposition": proposition,
        "delta": str(delta),
        "effect": est["effect"],
        "lower": est["lower"],
        "upper": est["upper"],
        "n_blocks": est["n_blocks"],
        "level": est["level"],
        "seed": est["method"]["seed"],
        "claim": claim,
    }


def iter_block_pairs(rows: Iterable[Mapping[str, Any]], *,
                     treatment_condition: str, control_condition: str,
                     metric_id: str) -> "dict[str, tuple[Decimal, Decimal]]":
    """把逐 run 指标行整理成 ``block_id → (treatment, control)``。

    行形如 ``{"block_id", "condition_id", "metric_id", "value"}``。重复的
    (block, condition) 是分母异常，直接拒绝；缺块由调用方按分配 manifest 核对。
    """
    by_block: "dict[str, dict[str, Decimal]]" = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise StatisticsError("WRONG_TYPE", "指标行必须是对象")
        try:
            block_id = str(row["block_id"])
            condition_id = str(row["condition_id"])
            metric = str(row["metric_id"])
            value = row["value"]
        except KeyError as exc:
            raise StatisticsError("MISSING_KEY", f"指标行缺 {exc.args[0]}") from None
        if metric != metric_id:
            continue
        if condition_id not in (treatment_condition, control_condition):
            continue
        slot = by_block.setdefault(block_id, {})
        if condition_id in slot:
            raise StatisticsError(
                "DUPLICATE_BLOCK",
                f"{block_id}/{condition_id} 出现多次（分母偷换）")
        try:
            slot[condition_id] = Decimal(str(_finite(value, where="value")))
        except StatisticsError:
            raise
    pairs: "dict[str, tuple[Decimal, Decimal]]" = {}
    for block_id, slot in by_block.items():
        missing = {treatment_condition, control_condition} - set(slot)
        if missing:
            raise StatisticsError(
                "UNPAIRED_BLOCK",
                f"{block_id} 缺条件 {sorted(missing)}：配对差需要两侧都在")
        pairs[block_id] = (slot[treatment_condition], slot[control_condition])
    if not pairs:
        raise StatisticsError("EMPTY", "没有可配对的 block")
    return pairs


# ---------------------------------------------------------------------------
# 公开统计方法（§5.1「方法不是隐藏答案」）
# ---------------------------------------------------------------------------

METHOD_SCHEMA = "simpraxis.rsi_r1.statistics_method.v1"


def public_method_document() -> "dict[str, Any]":
    """公开精确估计法/随机算法/排序/分位数/容差——独立 public consumer 据此复现。

    只发布**方法**：不含任何观测值、配对差、效应或答案。document 里的每个数都
    从 rubric 常量与本模块 seed 派生，不复制字面量。
    """
    return {
        "schema": METHOD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "metric_scope": "paired_block_difference",
        "estimator": {
            "unit": "block",
            "input": "block_id → (treatment_value, control_value)",
            "per_block": "diff[block_id] = float(treatment_value) - float(control_value)",
            "effect": "effect = float(numpy.asarray(values, dtype=numpy.float64).mean())",
            "denominator": ("block_id 键集合本身即分母；缺块不是“少一个样本”而是分母"
                            "偷换，直接拒绝"),
            "min_blocks": 2,
            "requires_finite": True,
            "rejects": ["非有限值", "布尔冒充数值", "空分母", "block 少于 2"],
        },
        "sample_order": {
            "rule": "按 block_id 的字符串升序（Python sorted）排列；该行序决定重抽样数组的行号",
            "note": "键是规范化字符串 block_id；不得以插入序/字典偶然序替代",
        },
        "bootstrap": {
            "method": "percentile",
            "resamples": int(R.BOOTSTRAP_RESAMPLES),
            "unit": str(R.BOOTSTRAP_UNIT),
            "seed": int(STATISTICS_SEED),
            "rng": {
                "generator": "numpy.random.Generator",
                "bit_generator": "PCG64",
                "construction": "rng = numpy.random.default_rng(numpy.random.PCG64(seed))",
                "seed_is_fixed": True,
                "draw": "idx = rng.integers(0, n, size=(resamples, n))  # 每行一次有放回重抽样",
                "statistic": "means = values[idx].mean(axis=1)  # 行均值，即每次重抽样的效应",
            },
            "interval": {
                "quantile_method": str(R.BOOTSTRAP_QUANTILE),
                "alpha": "alpha = (1 - level) / 2（Decimal 精确计算后转 float）",
                "bounds": "numpy.quantile(means, [alpha, 1 - alpha], method=quantile_method)",
                "levels": {"primary": str(R.CI_PRIMARY), "negative": str(R.CI_NEGATIVE)},
                "level_rule": ("阴性等价结论必须用 negative 级别 90% 区间；"
                               "不得以 p>0.05 当作阴性判据"),
            },
            "dtype": str(R.BOOTSTRAP_DTYPE),
        },
        "tolerance": {
            "formula": "tolerance(ref) = abs_tol + rel_tol * abs(ref)",
            "abs_tol": str(R.CLAIM_ABS_TOL),
            "rel_tol": str(R.CLAIM_REL_TOL),
            "applies_to": ["estimate", "lower", "upper"],
            "judge_rule": "逐字段与宿主独立重算值比较；超出容差即数值不一致",
        },
        "claim_classification": {
            "scale": ("δ 只取自冻结指标目录（rubric.METRICS 的 delta）；候选不得扩界或自定界"),
            "propositions": list(R.PROPOSITIONS),
            "rules": {
                "decrease_gt": "upper < -δ → SUPPORTED；lower >= -δ → REFUTED；否则 INCONCLUSIVE",
                "increase_gt": "lower > δ → SUPPORTED；upper <= δ → REFUTED；否则 INCONCLUSIVE",
                "equivalent": ("lower > -δ 且 upper < δ → SUPPORTED；"
                               "upper <= -δ 或 lower >= δ → REFUTED；否则 INCONCLUSIVE"),
            },
            "interval_check": "lower > upper 视为区间倒置，直接拒绝",
        },
        "finite_sample_note": ("percentile bootstrap 是有限样本近似：不宣称频率派覆盖率保证，"
                               "也不因重跑而“调参救场”"),
    }
