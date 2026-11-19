"""S2 对比登记规则（AC-03）：DiD 等效可登记；阴性等效指标仍限 pairwise；旧案不改写。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_r1.contrasts import (ContrastError, ContrastV13, KIND_DID,
                                           KIND_PAIRWISE, did_terms)


def _did_equivalent(metric="displacement_x_m", prop="equivalent"):
    return ContrastV13(
        id="did_equivalence", kind=KIND_DID, metric=metric,
        proposition=prop, delta=R.metric_delta(metric),
        terms=did_terms(treatment_high="t_high", treatment_low="t_low",
                        control_high="c_high", control_low="c_low"))


def test_did_equivalence_registrable_on_primary_metric():
    """撤销「DiD 等效不可注册」规则债：主指标上的 DiD 等效可登记。"""
    contrast = _did_equivalent().validate()
    assert contrast.proposition == "equivalent"
    assert contrast.delta == Decimal("0.001")
    assert contrast.kind == KIND_DID


def test_did_equivalence_on_b_metric():
    contrast = _did_equivalent(metric="normalized_tracking_rmse").validate()
    assert contrast.delta == Decimal("0.01")


def test_negative_equivalence_metric_still_pairwise_only():
    contrast = ContrastV13(
        id="bad", kind=KIND_DID,
        metric="displacement_x_m_negative_equivalence",
        proposition="equivalent",
        delta=R.metric_delta("displacement_x_m_negative_equivalence"),
        terms=did_terms(treatment_high="a", treatment_low="b",
                        control_high="c", control_low="d"))
    with pytest.raises(ContrastError) as excinfo:
        contrast.validate()
    assert excinfo.value.code == "METRIC_INVALID"


def test_delta_still_pinned_by_rubric():
    """等效登记不放宽 δ：候选自报 δ 与量表不符即拒。"""
    contrast = ContrastV13(
        id="drift", kind=KIND_DID, metric="displacement_x_m",
        proposition="equivalent", delta=Decimal("0.5"),
        terms=did_terms(treatment_high="a", treatment_low="b",
                        control_high="c", control_low="d"))
    with pytest.raises(ContrastError) as excinfo:
        contrast.validate()
    assert excinfo.value.code == "DELTA_DRIFT"


def test_b_negative_equivalence_metric_registered():
    assert R.metric_delta("normalized_tracking_rmse_negative_equivalence") == (
        Decimal("0.01"))


def test_public_schema_documents_equivalence_registration():
    schema = TS.X.public_schema()
    assert "equivalence_registration" in schema["rules"]


def test_old_r1n_tasks_unchanged():
    """A_N 旧判件不翻：S1 任务的主对比仍是三值语义的 decrease_gt。"""
    a = TS.build_r1n_a()
    assert a.primary.proposition == "decrease_gt"
    assert a.primary.metric == "displacement_x_m"
    b = TS.build_r1n_b()
    assert b.primary.proposition == "increase_gt"
