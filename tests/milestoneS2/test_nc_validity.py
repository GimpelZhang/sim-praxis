"""S2 NC 注册与有效性三判据（AC-04）+ 任务构建器 NC 槽。

退化/超阈/未校准各一负控；槽外提名/超上界各一负控；合法 NC 正控。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from orchestrator.hk import nc_registration as N

_REPO = Path(__file__).resolve().parents[2]


def _slot(**overrides):
    slot = {
        "schema": N.NC_SLOT_SCHEMA,
        "allowed_diff": ["sliding_friction", "torsional_friction_m"],
        "candidates": ["base", "sham_mu", "sham_tors"],
        "equivalence_metric": "displacement_x_m_negative_equivalence",
        "delta": "0.001",
        "ci_level": "0.90",
        "max_perturbation": {"sliding_friction": "0.001",
                             "torsional_friction_m": "0.02"},
        "default": {"base": "base", "sham": "sham_tors"},
        "criteria": dict(N.NC_CRITERIA),
    }
    slot.update(overrides)
    return slot


CONDITIONS = [
    {"id": "base", "params": {"sliding_friction": "0.6",
                              "torsional_friction_m": "0.01"}},
    {"id": "sham_mu", "params": {"sliding_friction": "0.6005",
                                 "torsional_friction_m": "0.01"}},
    {"id": "sham_tors", "params": {"sliding_friction": "0.6",
                                   "torsional_friction_m": "0.02"}},
]


def test_slot_valid_and_contrast_built():
    contrast = N.build_nc_contrast(_slot(), base="base", sham="sham_mu",
                                   conditions=CONDITIONS)
    assert contrast["proposition"] == "equivalent"
    assert contrast["metric"] == "displacement_x_m_negative_equivalence"
    assert contrast["delta"] == "0.001"
    weights = {t["condition_id"]: t["weight"] for t in contrast["terms"]}
    assert weights == {"base": "1", "sham_mu": "-1"}


def test_out_of_slot_rejected():
    # 提名不在候选集
    with pytest.raises(N.NCRegistrationError) as excinfo:
        N.build_nc_contrast(_slot(), base="base", sham="ghost",
                            conditions=CONDITIONS)
    assert excinfo.value.code == "NC_OUT_OF_SLOT"
    # 扰动超上界（0.01 的滑动摩擦扰动 > 0.001）
    bad = [dict(c) for c in CONDITIONS]
    bad[1] = {"id": "sham_mu", "params": {"sliding_friction": "0.61",
                                          "torsional_friction_m": "0.01"}}
    with pytest.raises(N.NCRegistrationError) as excinfo:
        N.build_nc_contrast(_slot(), base="base", sham="sham_mu",
                            conditions=bad)
    assert excinfo.value.code == "NC_OUT_OF_SLOT"
    # base == sham
    with pytest.raises(N.NCRegistrationError):
        N.build_nc_contrast(_slot(), base="base", sham="base",
                            conditions=CONDITIONS)


def test_slot_shape_negative_controls():
    with pytest.raises(N.NCRegistrationError):
        N.validate_slot({"schema": "wrong"})
    with pytest.raises(N.NCRegistrationError):
        N.validate_slot(_slot(delta="0"))
    with pytest.raises(N.NCRegistrationError):
        N.validate_slot(_slot(max_perturbation={}))
    with pytest.raises(N.NCRegistrationError):
        N.validate_slot(_slot(candidates=["base"]))


def _contrast():
    return N.build_nc_contrast(_slot(), base="base", sham="sham_mu",
                               conditions=CONDITIONS)


def test_degenerate_nc_rejected():
    """S1 的退化 sham（逐 block 恒 0、[0,0]）必须被结构拒绝。"""
    with pytest.raises(N.NCRegistrationError) as excinfo:
        N.validate_nc(contrast=_contrast(), block_values=[0.0] * 32,
                      estimate=0.0, ci_low=0.0, ci_high=0.0,
                      sensitivity={"detected": True})
    assert excinfo.value.code == "NC_DEGENERATE"


def test_above_threshold_rejected():
    with pytest.raises(N.NCRegistrationError) as excinfo:
        N.validate_nc(contrast=_contrast(),
                      block_values=[0.002] * 16 + [-0.001] * 16,
                      estimate=0.002, ci_low=0.0015, ci_high=0.0025,
                      sensitivity={"detected": True})
    assert excinfo.value.code == "NC_ABOVE_THRESHOLD"


def test_uncalibrated_rejected():
    with pytest.raises(N.NCRegistrationError) as excinfo:
        N.validate_nc(contrast=_contrast(),
                      block_values=[1e-5, -3e-6, 5e-6, -2e-6],
                      estimate=3e-6, ci_low=-4e-6, ci_high=9e-6,
                      sensitivity={"detected": False})
    assert excinfo.value.code == "NC_UNCALIBRATED"
    with pytest.raises(N.NCRegistrationError):
        N.validate_nc(contrast=_contrast(),
                      block_values=[1e-5, -3e-6], estimate=3e-6,
                      ci_low=-4e-6, ci_high=9e-6, sensitivity=None)


def test_valid_nc_passes_with_evidence():
    evidence = N.validate_nc(contrast=_contrast(),
                             block_values=[1e-5, -3e-6, 5e-6, -2e-6],
                             estimate=3e-6, ci_low=-4e-6, ci_high=9e-6,
                             sensitivity={"detected": True, "estimate_m": 0.0405})
    assert evidence["passed"] is True
    assert evidence["non_degenerate"]["max_abs_block_effect"] == pytest.approx(1e-5)
    assert evidence["sensitivity_calibrated"]["detected"] is True


# ---------------------------------------------------------------------------
# 任务构建器 NC 槽（S2 的 6 实例 + S1 旧任务不翻）
# ---------------------------------------------------------------------------

def _campaign_module():
    path = _REPO / "scripts" / "s2" / "prereg_campaign.py"
    spec = importlib.util.spec_from_file_location("s2_prereg_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_six_instances_build_with_nc_slot():
    module = _campaign_module()
    assert len(module.INSTANCES) == 6
    families = [i["family"] for i in module.INSTANCES]
    assert families.count("A_N") == 3 and families.count("B_N") == 3
    for item in module.INSTANCES:
        task = module.build_instance(item)
        slot = task.negative_control_spec
        assert slot["schema"] == N.NC_SLOT_SCHEMA
        assert slot["criteria"] == dict(N.NC_CRITERIA)
        # 每实例 4 主条件 + NC 候选（A: 2 sham；B: 1 sham）
        assert len(task.conditions) == (6 if item["family"] == "A_N" else 5)
        assert task.primary.kind == "difference_in_differences"


def test_s1_legacy_tasks_still_validate():
    """A_N 旧案不翻：S1 任务构建器的 NC 语义原样保留。"""
    from orchestrator.rsi_r1 import task_specs as TS

    a = TS.build_r1n_a()
    assert a.negative_control_spec["contrast"]["metric"] == (
        "displacement_x_m_negative_equivalence")
    b = TS.build_r1n_b()
    assert b.negative_control_spec is None
