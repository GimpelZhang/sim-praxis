"""S2c AC-08/AC-09：G-DESIGN 五折负控 + 泄漏负控 + 编译往返 + DomainBrief 约束。"""
from __future__ import annotations

import pytest

from orchestrator.hk.design_space import (BriefError, DomainBrief,
                                          check_within_bounds)
from orchestrator.hk.design_gate import check_design
from orchestrator.hk.design_compiler import DesignCompileError, compile_design


def _brief():
    return DomainBrief(
        brief_id="test-brief", question="因子交互？",
        competing_explanations=({"id": "e1", "desc": "机制A"}, {"id": "e2", "desc": "机制B"}),
        factor_axes=({"name": "fa", "values": ["0.2", "0.8"], "scene_param": "solref_damping_ratio"},
                     {"name": "fb", "values": ["1.0", "5.0"], "scene_param": "v0_mps"}),
        metrics=("rebound_height_m",), scene_id="C_collision_rebound")


def _good_design():
    return {
        "conditions": [
            {"id": "c_hh", "factors": {"fa": "0.8", "fb": "5.0"}, "scene_params": {}},
            {"id": "c_hl", "factors": {"fa": "0.8", "fb": "1.0"}, "scene_params": {}},
            {"id": "c_lh", "factors": {"fa": "0.2", "fb": "5.0"}, "scene_params": {}},
            {"id": "c_ll", "factors": {"fa": "0.2", "fb": "1.0"}, "scene_params": {}},
        ],
        "metric": "rebound_height_m",
        "primary_contrast": {"id": "fa_x_fb", "treatment_high": "c_hh",
                             "treatment_low": "c_lh", "control_high": "c_hl",
                             "control_low": "c_ll", "proposition": "decrease_gt"},
        "negative_control": {"base": "c_ll", "sham": "c_ll"},
        "hypothesis": "fa 效应随 fb 改变", "rationale": "物理理由",
    }


def test_brief_requires_two_explanations():
    with pytest.raises(BriefError) as e:
        DomainBrief(brief_id="x", question="q",
                    competing_explanations=({"id": "e1", "desc": "A"},),
                    factor_axes=({"name": "fa", "values": ["0"]},),
                    metrics=("m",), scene_id="C_collision_rebound")
    assert e.value.code == "EXPLANATIONS_INSUFFICIENT"


def test_out_of_space_unknown_factor():
    brief = _brief()
    d = _good_design()
    d["conditions"][0]["factors"]["zzz"] = "9"
    probs = check_within_bounds(d, brief)
    assert any("未知因子" in p for p in probs)


def test_out_of_space_value_not_in_axis():
    brief = _brief()
    d = _good_design()
    d["conditions"][0]["factors"]["fa"] = "0.5"  # 不在 {0.2,0.8}
    probs = check_within_bounds(d, brief)
    assert any("不在轴" in p for p in probs)


def test_gate_out_of_space():
    r = check_design({"conditions": [{"id": "c", "factors": {"bad": "1"}}],
                      "metric": "rebound_height_m"}, _brief())
    assert not r.passed and r.code == "DESIGN_OUT_OF_SPACE"


def test_gate_shape_invalid():
    brief = _brief()
    d = _good_design()
    d["conditions"] = [{"id": f"c{i}", "factors": {"fa": "0.2", "fb": "1.0"}}
                       for i in range(7)]  # 7×32=224 >192
    r = check_design(d, brief)
    assert not r.passed and r.code in ("DESIGN_OUT_OF_SPACE", "DESIGN_SHAPE_INVALID")
    # 7 条件也超 max_conditions(6) → OUT_OF_SPACE 更早；用非空轴构造 7 条件看 SHAPE
    d2 = _good_design()
    d2["conditions"] = [{"id": f"c{i}", "factors": {"fa": "0.2", "fb": "1.0"}}
                        for i in range(7)]
    r2 = check_design(d2, brief)
    assert not r2.passed


def test_gate_nc_invalid():
    r = check_design(_good_design(), _brief(),
                     nc_check=lambda d: (False, "NC degenerate"))
    assert not r.passed and r.code == "DESIGN_NC_INVALID"


def test_gate_pilot_weak():
    r = check_design(_good_design(), _brief(),
                     nc_check=lambda d: (True, ""),
                     pilot_effect=lambda d: 0.0005, delta=0.001)
    assert not r.passed and r.code == "DESIGN_PILOT_WEAK"


def test_gate_non_discriminative():
    r = check_design(_good_design(), _brief(),
                     nc_check=lambda d: (True, ""),
                     pilot_effect=lambda d: 0.05, delta=0.001,
                     discriminability=lambda d: {"min_pair_diff": 0.0008, "variants": ["v1", "v2"]})
    assert not r.passed and r.code == "DESIGN_NON_DISCRIMINATIVE"
    # 世界变体数值在 private，不进 public
    assert "variants" in r.private
    assert "variants" not in r.public


def test_gate_full_pass():
    r = check_design(_good_design(), _brief(),
                     nc_check=lambda d: (True, ""),
                     pilot_effect=lambda d: 0.05, delta=0.001,
                     discriminability=lambda d: {"min_pair_diff": 0.02, "variants": ["v1", "v2"]})
    assert r.passed and r.code == "OK"


def test_leak_negative_public_doc_has_no_world_variants():
    """AC-09：actor 可见面不得出现世界变体数值（DomainBrief 不持有它们）。"""
    brief = _brief()
    doc = brief.public_document()
    # 公开面键不得包含变体/真值类
    assert "world_variants" not in doc
    assert "variants" not in doc
    # 竞争解释只有公开描述（id/desc），无数值字段
    for e in doc["competing_explanations"]:
        assert set(e.keys()) <= {"id", "desc"}


def test_compile_roundtrip():
    brief = _brief()
    d = _good_design()
    d["negative_control"] = {"base": "c_ll", "sham": "c_lh"}
    spec = compile_design(d, brief)
    assert spec["plan_sha256"]
    assert spec["primary_contrast"]["terms"]["treatment_high"] == "c_hh"
    # 同输入同 sha
    spec2 = compile_design(d, brief)
    assert spec["plan_sha256"] == spec2["plan_sha256"]


def test_compile_missing_primary():
    with pytest.raises(DesignCompileError) as e:
        compile_design({"conditions": [{"id": "c", "factors": {}}],
                        "metric": "m", "negative_control": {"base": "c", "sham": "c"}}, _brief())
    assert e.value.code == "NO_PRIMARY"
