"""S0-4b：ParameterSpace 六字段 + 科学判据字段结构性不可达（负控最重要）。"""
from __future__ import annotations

import pytest

from orchestrator.hk.params import (FIELDS, INTEGER_FIELDS, SPACE,
                                    ParameterProposalError, validate_proposal)


def test_six_fields_declared_with_design_bounds():
    assert set(FIELDS) == {
        "exploration.max_batches",
        "exploration.runs_per_condition",
        "analysis.bootstrap_resamples",
        "tolerance.settle_time_abs_s",
        "guard.noprog_rounds",
        "timing.analysis_timeout_s",
    }
    expected_bounds = {
        "exploration.max_batches": (1.0, 8.0),
        "exploration.runs_per_condition": (8.0, 64.0),
        "analysis.bootstrap_resamples": (1000.0, 100000.0),
        "tolerance.settle_time_abs_s": (0.005, 0.05),
        "guard.noprog_rounds": (1.0, 4.0),
        "timing.analysis_timeout_s": (30.0, 600.0),
    }
    for name, (lower, upper) in expected_bounds.items():
        assert (FIELDS[name].lower, FIELDS[name].upper) == (lower, upper)
    assert len(SPACE.identity()["fields"]) == 6


def test_proposal_accepts_declared_numeric_fields():
    accepted = validate_proposal({"exploration.max_batches": 4,
                                  "tolerance.settle_time_abs_s": 0.02})
    assert accepted == {"exploration.max_batches": 4.0,
                        "tolerance.settle_time_abs_s": 0.02}


@pytest.mark.parametrize("field", ["delta_m", "alpha", "confidence_level"])
def test_scientific_criteria_fields_are_unknown_field(field):
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({field: 0.01})
    assert excinfo.value.code == "UNKNOWN_FIELD"


def test_judgement_related_names_also_rejected():
    for field in ("analysis.delta", "analysis.alpha", "claim.direction", "block.count",
                  "contrast.definition"):
        with pytest.raises(ParameterProposalError) as excinfo:
            validate_proposal({field: 1})
        assert excinfo.value.code == "UNKNOWN_FIELD"


def test_out_of_bounds_and_off_grid_rejected():
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({"exploration.max_batches": 9})
    assert excinfo.value.code == "OUT_OF_BOUNDS"
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({"tolerance.settle_time_abs_s": 0.0075})
    assert excinfo.value.code == "OFF_GRID"
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({"guard.noprog_rounds": 2.5})
    assert excinfo.value.code == "OFF_GRID"


def test_non_numeric_and_empty_rejected():
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({"exploration.max_batches": "4"})
    assert excinfo.value.code == "NON_NUMERIC"
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({})
    assert excinfo.value.code == "EMPTY_PROPOSAL"
    with pytest.raises(ParameterProposalError) as excinfo:
        validate_proposal({"exploration.max_batches": True})
    assert excinfo.value.code == "NON_NUMERIC"


def test_integer_fields_cover_counts_and_timeouts():
    assert "exploration.max_batches" in INTEGER_FIELDS
    assert "tolerance.settle_time_abs_s" not in INTEGER_FIELDS
