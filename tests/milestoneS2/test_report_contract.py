"""S2 报告合同 v2：六段强制（AC-05）+ 缺段计 0 + 空模板拒绝。"""
from __future__ import annotations

import pytest

from orchestrator.hk import report_contract as RC

FULL = {name: f"这是 {name} 段的实质内容（足够长）" for name in RC.SECTIONS}


def test_six_sections_and_dimension_mapping():
    assert len(RC.SECTIONS) == 6
    assert set(RC.SECTION_TO_DIMENSION) == set(RC.SECTIONS)
    assert set(RC.SECTION_TO_DIMENSION.values()) == {
        "problem_hypothesis", "design_confounding", "statistics_censoring",
        "claim_strength", "limitations_next_steps", "negative_control"}


def test_complete_report_passes():
    out = RC.validate_report(FULL)
    assert set(out) == set(RC.SECTIONS)
    assert RC.missing_sections(FULL) == []


def test_missing_section_rejected_with_structure_annotation():
    partial = dict(FULL)
    del partial["limitations_next_steps"]
    with pytest.raises(RC.ReportContractError) as excinfo:
        RC.validate_report(partial)
    assert excinfo.value.code == "REPORT_INCOMPLETE"
    assert "limitations_next_steps" in excinfo.value.detail
    assert RC.missing_sections(partial) == ["limitations_next_steps"]


def test_empty_template_rejected():
    """空模板（cal-empty-template 的 S1 校准例）不得冒充已写。"""
    partial = {**FULL, "negative_control_handling": "（略）"}
    assert RC.missing_sections(partial) == ["negative_control_handling"]
    with pytest.raises(RC.ReportContractError):
        RC.validate_report(partial)


def test_dimension_floor_missing_scores_zero():
    partial = dict(FULL)
    del partial["negative_control_handling"]
    floor = RC.dimension_floor(partial)
    assert floor["negative_control"] == 0.0
    assert floor["problem_hypothesis"] == 4.0
    assert all(v == 4.0 for k, v in floor.items() if k != "negative_control")


def test_non_mapping_report_rejected():
    with pytest.raises(RC.ReportContractError) as excinfo:
        RC.validate_report(None)
    assert excinfo.value.code == "REPORT_SHAPE"
    assert RC.missing_sections(None) == list(RC.SECTIONS)


def test_build_report_roundtrip():
    doc = RC.build_report(FULL)
    assert doc["schema"] == RC.SCHEMA
    assert RC.missing_sections(doc["sections"]) == []
