"""RSI-R1 T4：统计估计量与主张分类（AC19/AC20 的理由面）。

全部离线：合成分布，不触网、不启容器。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import statistics as ST


# ---------------------------------------------------------------------------
# 手算 golden
# ---------------------------------------------------------------------------

def test_two_point_sample_has_enumerable_percentile_ci() -> None:
    """[0, 1] 的 block 重抽样均值只取 0/0.5/1（概率 1/4、1/2、1/4）：
    2.5% 与 97.5% 分位必为 0 与 1（linear 分位不越过端点）。"""
    lower, upper = ST.bootstrap_percentile_ci([0.0, 1.0], level="0.95")
    assert lower == 0.0 and upper == 1.0
    est = ST.estimate([0.0, 1.0])
    assert est["n_blocks"] == 2 and est["effect"] == 0.5
    assert (est["lower"], est["upper"]) == (0.0, 1.0)
    assert est["method"] == {
        "bootstrap_resamples": 10_000, "bootstrap_unit": "block",
        "quantile": "linear", "dtype": "float64", "seed": ST.STATISTICS_SEED,
    }


def test_constant_vector_ci_is_degenerate() -> None:
    """零差向量/常向量区间塌成一点：这是确定性不变性，不是"现实不确定性为 0"。"""
    diffs = {f"b{i:02d}": 0.0 for i in range(1, 33)}
    est = ST.estimate(diffs, level=R.CI_NEGATIVE)
    assert est["effect"] == 0.0 and est["lower"] == 0.0 and est["upper"] == 0.0
    claim = ST.claim_document(
        proposition="equivalent", metric_id="displacement_x_m_negative_equivalence",
        diffs=diffs, level=R.CI_NEGATIVE)
    assert claim["claim"] == "SUPPORTED"
    assert claim["delta"] == str(R.metric_delta(
        "displacement_x_m_negative_equivalence"))


def test_estimate_is_deterministic_and_block_ordered() -> None:
    rng = np.random.default_rng(7)
    diffs = {f"b{i:02d}": float(v) for i, v in enumerate(rng.normal(0.2, 1.0, 32), 1)}
    a = ST.estimate(diffs)
    b = ST.estimate(dict(reversed(list(diffs.items()))))
    assert a == b, "排序不应影响结果"
    assert a["block_ids"] == sorted(diffs)
    assert a["n_blocks"] == 32
    assert a["method"]["seed"] == ST.STATISTICS_SEED


def test_paired_diffs_are_elementwise_treatment_minus_control() -> None:
    out = ST.paired_diffs({"b01": [1.5, 1.0], "b02": [0.25, 0.5]})
    assert out == {"b01": 0.5, "b02": -0.25}
    assert isinstance(out["b01"], float)


# ---------------------------------------------------------------------------
# 输入失败模式
# ---------------------------------------------------------------------------

def test_nan_and_infinite_are_rejected() -> None:
    with pytest.raises(ST.StatisticsError) as exc:
        ST.bootstrap_percentile_ci([0.0, math.nan, 1.0], level="0.95")
    assert exc.value.code == "NONFINITE"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.estimate([0.0, math.inf])
    assert exc.value.code == "NONFINITE"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.paired_diffs({"b01": (True, 0.0)})
    assert exc.value.code == "WRONG_TYPE"


def test_insufficient_blocks_and_bad_level_are_rejected() -> None:
    with pytest.raises(ST.StatisticsError) as exc:
        ST.estimate([0.5])
    assert exc.value.code == "INSUFFICIENT_N"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.estimate([0.1, 0.2], level="1.5")
    assert exc.value.code == "LEVEL_INVALID"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.estimate({}, level="0.95")
    assert exc.value.code == "EMPTY"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.bootstrap_percentile_ci([0.1, 0.2], level="0.95", seed=-1)
    assert exc.value.code == "SEED_INVALID"


def test_block_pair_rows_reject_duplicates_and_unpaired() -> None:
    rows = [
        {"block_id": "b01", "condition_id": "t", "metric_id": "m", "value": 1.0},
        {"block_id": "b01", "condition_id": "c", "metric_id": "m", "value": 0.5},
    ]
    pairs = ST.iter_block_pairs(rows, treatment_condition="t",
                                control_condition="c", metric_id="m")
    assert pairs == {"b01": (1.0, 0.5)}
    with pytest.raises(ST.StatisticsError) as exc:
        ST.iter_block_pairs(rows + [rows[0]], treatment_condition="t",
                            control_condition="c", metric_id="m")
    assert exc.value.code == "DUPLICATE_BLOCK"
    with pytest.raises(ST.StatisticsError) as exc:
        ST.iter_block_pairs(rows[:1], treatment_condition="t",
                            control_condition="c", metric_id="m")
    assert exc.value.code == "UNPAIRED_BLOCK"


# ---------------------------------------------------------------------------
# 主张语义与 δ 边界（AC20）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prop,lower,upper,expected", [
    ("decrease_gt", -0.02, -0.011, "SUPPORTED"),
    ("decrease_gt", -0.02, -0.010, "INCONCLUSIVE"),   # U 恰在 -δ：不算"
    ("decrease_gt", -0.010, 0.0, "REFUTED"),          # L ≥ -δ
    ("increase_gt", 0.011, 0.02, "SUPPORTED"),
    ("increase_gt", -0.01, 0.010, "REFUTED"),         # U ≤ δ
    ("increase_gt", -0.01, 0.012, "INCONCLUSIVE"),
    ("equivalent", -0.0099, 0.0099, "SUPPORTED"),
    ("equivalent", -0.03, -0.011, "REFUTED"),         # U ≤ -δ
    ("equivalent", 0.011, 0.03, "REFUTED"),           # L ≥ δ
    ("equivalent", -0.02, 0.0, "INCONCLUSIVE"),       # 只是偏在负侧
    ("equivalent", -0.01, 0.01, "INCONCLUSIVE"),      # 恰在界上，不是阴性
])
def test_claim_boundaries_are_exact(prop, lower, upper, expected) -> None:
    assert ST.classify(prop, "0.01", lower, upper) == expected


def test_p_value_above_005_is_not_a_negative() -> None:
    """宽区间跨出 ±δ 只是 INCONCLUSIVE；阴性要求 90% 区间严格落 ±δ 内。"""
    spread = [(-1) ** i * (0.002 + 0.0009 * i) for i in range(32)]
    wide = ST.claim_document(
        proposition="equivalent", metric_id="displacement_x_m_negative_equivalence",
        diffs={f"b{i:02d}": v for i, v in enumerate(spread, 1)}, level=R.CI_NEGATIVE)
    assert wide["claim"] == "INCONCLUSIVE", wide
    tight = ST.claim_document(
        proposition="equivalent", metric_id="displacement_x_m_negative_equivalence",
        diffs={f"b{i:02d}": 0.0002 for i in range(1, 33)}, level=R.CI_NEGATIVE)
    assert tight["claim"] == "SUPPORTED"


def test_claim_document_rejects_unregistered_metric() -> None:
    with pytest.raises(ST.StatisticsError) as exc:
        ST.claim_document(proposition="decrease_gt", metric_id="made_up",
                          diffs={"b01": 0.0, "b02": 1.0})
    assert exc.value.code == "METRIC_UNREGISTERED"


# ---------------------------------------------------------------------------
# 有限样本覆盖与失败模式（Plan §4.1 要求报告，不包装为数学保证）
# ---------------------------------------------------------------------------

def _coverage(dist_fn, *, reps: int, sweep: str) -> float:
    hits = 0
    for i in range(reps):
        rng = np.random.default_rng(10_000 + i)
        sample = dist_fn(rng)
        lower, upper = ST.bootstrap_percentile_ci(sample, level="0.95")
        hits += int(lower <= 0.0 <= upper)
    return hits / reps


def test_coverage_on_symmetric_and_skewed_synthetics() -> None:
    """报告型测试：正态 n=32/64 与右偏 n=32 的经验覆盖率。

    区间是 percentile bootstrap（有限样本近似），这里只要求**落在可解释带内**：
    正态 ≥0.90（名义 0.95 的小样本欠覆盖）、偏斜 ≥0.85；上限 0.995 防"永远
    覆盖"的退化实现。真值与带宽见断言消息，便于台账引用。
    """
    normal32 = _coverage(lambda r: r.normal(0.0, 1.0, 32), reps=200, sweep="n32")
    normal64 = _coverage(lambda r: r.normal(0.0, 1.0, 64), reps=200, sweep="n64")
    skew32 = _coverage(lambda r: r.exponential(1.0, 32) - 1.0, reps=200, sweep="skew")
    assert 0.90 <= normal32 <= 0.995, f"normal n=32 coverage={normal32}"
    assert 0.90 <= normal64 <= 0.995, f"normal n=64 coverage={normal64}"
    assert 0.85 <= skew32 <= 0.995, f"skew n=32 coverage={skew32}"
    print(f"[coverage] normal32={normal32:.3f} normal64={normal64:.3f} "
          f"skew32={skew32:.3f}")
