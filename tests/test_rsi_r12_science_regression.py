"""RSI-R1.2 T4：科学界回归（AC42）。

冻结边界逐值不变：A 阴性等效界 ±0.001 m / 90% 区间；B normalized RMSE δ=0.01；
阴性 INCONCLUSIVE 不满足阴性门；无配对样本不算阴性（n_blocks=0 且 INCONCLUSIVE）。
本文件只读 rubric 常量与 judge 分类，不改界、不换 seed。
"""
from __future__ import annotations

from decimal import Decimal

from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import rubric as R
from tests import rsi_r1_helpers as H


def _plan(study: str):
    return H.doc_of(H.plan_doc(study))


def _contrast(plan, cid: str):
    return {c.id: c for c in J._contrasts(plan)}[cid]


def _per_run(plan, contrast, treatment: float, control: float, *,
             blocks: int = 32) -> "dict[str, dict]":
    t_cond, c_cond = contrast.condition_ids
    out: "dict[str, dict]" = {}
    for i in range(blocks):
        block = f"b{i:02d}"
        out[f"{block}-t"] = {"condition_id": t_cond, "block_id": block,
                             "values": {"displacement_x_m": treatment,
                                        "normalized_tracking_rmse": treatment}}
        out[f"{block}-c"] = {"condition_id": c_cond, "block_id": block,
                             "values": {"displacement_x_m": control,
                                        "normalized_tracking_rmse": control}}
    return out


def test_frozen_scientific_bounds_unchanged() -> None:
    assert R.CI_PRIMARY == Decimal("0.95")
    assert R.CI_NEGATIVE == Decimal("0.90")
    assert R.metric_delta("displacement_x_m") == Decimal("0.001")
    assert R.metric_delta("displacement_x_m_negative_equivalence") == \
        Decimal("0.001")
    assert R.metric_delta("normalized_tracking_rmse") == Decimal("0.01")
    assert R.NEGATIVE_CONTROL_METRIC == \
        "displacement_x_m_negative_equivalence"
    neg = R.METRICS["displacement_x_m_negative_equivalence"]
    assert neg["censoring"] == "none" and neg["study"] == "A"


def test_a_negative_true_equivalence_passes_with_zero_diff() -> None:
    plan = _plan("A")
    contrast = _contrast(plan, "negative")
    checks: "list[dict]" = []
    ref = J._judge_contrast(plan, _per_run(plan, contrast, 0.10, 0.10), contrast,
                            checks)
    assert ref["status"] == "SUPPORTED"
    assert ref["n_blocks"] == 32
    assert ref["level"] == "0.90"
    assert abs(ref["effect"]) < 1e-12


def test_a_negative_real_difference_is_not_equivalence() -> None:
    plan = _plan("A")
    contrast = _contrast(plan, "negative")
    ref = J._judge_contrast(plan, _per_run(plan, contrast, 0.05, 0.04),
                            contrast, [])
    assert ref["status"] != "SUPPORTED"          # 阴性门不满足
    assert ref["effect"] > 0.001


def test_a_negative_no_samples_is_not_equivalence() -> None:
    plan = _plan("A")
    contrast = _contrast(plan, "negative")
    ref = J._judge_contrast(plan, {}, contrast, [])
    assert ref["status"] == "INCONCLUSIVE"
    assert ref["n_blocks"] == 0
    assert ref["error"] == "NO_PAIRED_BLOCKS"


def test_b_reasonable_inconclusive_stays_inconclusive() -> None:
    plan = _plan("B")
    contrast = _contrast(plan, "primary")
    # 差值围绕 δ=0.01 抖动，区间必然跨界 → INCONCLUSIVE（不是 REFUTED）
    import math
    t_cond, c_cond = contrast.condition_ids
    per_run: "dict[str, dict]" = {}
    for i in range(32):
        block = f"b{i:02d}"
        noise = 0.01 * math.sin(i)       # [−0.01, 0.01]
        per_run[f"{block}-t"] = {
            "condition_id": t_cond, "block_id": block,
            "values": {"normalized_tracking_rmse": 1.0 + 0.01 + noise,
                       "displacement_x_m": 0.0}}
        per_run[f"{block}-c"] = {
            "condition_id": c_cond, "block_id": block,
            "values": {"normalized_tracking_rmse": 1.0,
                       "displacement_x_m": 0.0}}
    ref = J._judge_contrast(plan, per_run, contrast, [])
    assert ref["status"] == "INCONCLUSIVE"
    assert ref["n_blocks"] == 32
    assert ref["lower"] <= 0.01 <= ref["upper"]


def test_b_no_samples_not_supported() -> None:
    plan = _plan("B")
    contrast = _contrast(plan, "primary")
    ref = J._judge_contrast(plan, {}, contrast, [])
    assert ref["status"] == "INCONCLUSIVE"
    assert ref["n_blocks"] == 0
