"""RSI-G2 scope/evaluator：轴分离、CASE_INVALID 与 science 不晋升（零 POST）。"""
from __future__ import annotations

import copy

import pytest

from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g2_validation as GV


def _cases():
    return {c["case_id"]: c for c in GV.build_cases()}


def test_all_fixtures_score_ok_with_contract_derived_claims():
    for case in GV.build_cases():
        result = GV.score_case(case, GV.expected_claim(case))
        assert result["classification"] == "OK", (case["case_id"], result)


def test_batch_scoring_is_not_vacuous():
    doc = GV.build_case_file(
        claims={c["case_id"]: GV.expected_claim(c)
                for c in GV.build_cases()})
    report = GV.score_batch(doc)
    assert report["ok"] and report["n_cases"] == 6
    empty = GV.score_batch({"cases": GV.build_cases(), "claims": {}})
    assert not empty["ok"]
    assert {r["classification"] for r in empty["results"]} == {"CLAIM_MISSING"}


def test_scope_error_with_correct_label_is_not_semantic_gap():
    case = _cases()["g2c04_fixed_point"]
    claim = GV.expected_claim(case)
    claim["target_population"] = "registered_distribution"
    out = GV.score_case(case, claim)
    assert out["classification"] == "SCOPE_CONTRACT_MISMATCH"
    assert out["semantic_label_valid"] is True
    assert out["semantic_match"] is True
    assert out["scope_valid"] is False


def test_semantic_error_with_correct_scope_is_separate():
    case = _cases()["g2c01_supported_mean"]
    claim = GV.expected_claim(case)
    claim["semantic_label"] = "REFUTED"
    out = GV.score_case(case, claim)
    assert out["classification"] == "SEMANTIC_MISMATCH"
    assert out["scope_valid"] is True
    assert out["semantic_label_valid"] is True
    assert out["semantic_match"] is False


def test_tuple_fields_are_compared_individually():
    case = _cases()["g2c01_supported_mean"]
    for field, value in (("hypothesis_id", "h2"), ("estimand_id", "e2"),
                         ("metric_id", "m2"), ("proposition", "decrease_lt"),
                         ("delta", 0.5), ("unit", "cm")):
        claim = GV.expected_claim(case)
        claim[field] = value
        out = GV.score_case(case, claim)
        assert out["classification"] == "TUPLE_MISMATCH", field
        assert any(field in code for code in out["codes"]), (field, out["codes"])


def test_numeric_mismatch_is_mechanical():
    case = _cases()["g2c01_supported_mean"]
    claim = GV.expected_claim(case)
    claim["estimate"] = 0.0476
    out = GV.score_case(case, claim)
    assert out["classification"] == "NUMERIC_MISMATCH"
    assert out["numeric_valid"] is False


def test_missing_block_formula_makes_case_invalid():
    case = _cases()["g2c01_supported_mean"]
    broken = copy.deepcopy(case)
    broken["public"]["scope_contract"]["block_to_contrast_formula"] = ""
    problems = GV.validate_case(broken)
    assert any("block_to_contrast_formula" in p for p in problems)
    out = GV.score_case(broken, GV.expected_claim(case))
    assert out["classification"] == "CASE_INVALID"


def test_expected_contract_drift_is_fail_closed():
    case = _cases()["g2c02_refuted"]
    drift = copy.deepcopy(case)
    drift["public"]["scope_contract"]["delta"] = 0.5
    problems = GV.validate_case(drift)
    assert any("contract sha" in p for p in problems), problems


def test_coverage_mismatch_is_its_own_axis():
    case = _cases()["g2c01_supported_mean"]
    claim = GV.expected_claim(case)
    claim["covered_block_ids"] = ["b0"]
    out = GV.score_case(case, claim)
    assert out["classification"] == "EVIDENCE_COVERAGE_MISMATCH"
    assert out["scope_valid"] is True
    assert out["evidence_coverage_valid"] is False


def test_science_status_is_never_promoted_by_match():
    refuted = GV.score_case(_cases()["g2c02_refuted"],
                            GV.expected_claim(_cases()["g2c02_refuted"]))
    assert refuted["science_status"] == "REFUTED"
    inconsistent = GV.score_case(
        _cases()["g2c06_inconsistent"],
        GV.expected_claim(_cases()["g2c06_inconsistent"]))
    assert inconsistent["classification"] == "OK"
    assert inconsistent["science_status"] == "NOT_MEASURED"
    assert inconsistent["science_assessment"]["science_status"] == "NOT_MEASURED"


def test_claim_missing_fields_is_mechanical_not_semantic():
    case = _cases()["g2c01_supported_mean"]
    out = GV.score_case(case, {"hypothesis_id": "h_interaction"})
    assert out["classification"] == "MECHANICAL_FAIL"
    assert out["codes"][0].startswith("CLAIM_MISSING")


def test_recompute_formula_is_public_and_independent():
    assert GV.recompute([0.06, 0.04, 0.0005, 0.09], "mean") == pytest.approx(
        0.047625)
    assert GV.recompute([1.0, 2.0, 3.0], "median") == 2.0
    assert GV.recompute([1.0, 2.0, 3.0, 4.0], "paired_mean") == 2.5
    with pytest.raises(G2.G2Error):
        GV.recompute([1.0], "other")


def test_schema_document_matches_enums():
    schema = GV.public_schema()
    assert set(schema["enums"]["target_population"]) == set(G2.TARGET_POPULATIONS)
    assert set(schema["enums"]["aggregation"]) == set(G2.AGGREGATIONS)
    assert schema["rules"]["scope_vs_semantic"].startswith("scope 错")
