"""RSI-G1 旁路结案/决策：AND 聚合、三字段分离、多轴矩阵（零 POST）。"""
from __future__ import annotations

from orchestrator.rsi_r1 import g1_decision as GD


def _study(study, claim, direction, review):
    return {"study": study, "claim_status": claim,
            "scientific_direction": direction, "review_verdict": review}


def test_sidecar_ands_studies_and_keeps_three_fields():
    doc = GD.build_sidecar_closeout([
        _study("A", "CLAIM_INVALID", "VALID_FAIL", "REVIEW_FAIL"),
        _study("B", "MATCH", "REFUTED", "REVIEW_PASS")])
    assert doc["claim_tuple"] == ["CLAIM_INVALID", "MATCH"]
    assert doc["scientific_direction"] == ["VALID_FAIL", "REFUTED"]
    assert doc["review_verdict"] == ["REVIEW_FAIL", "REVIEW_PASS"]
    assert doc["and_aggregation"]["claims_all_match"] is False
    assert doc["and_aggregation"]["combined"] is False


def test_refuted_and_inconclusive_are_not_relabelled():
    """科学方向 REFUTED/INCONCLUSIVE 原样保留，不因非 SUPPORTED 改判。"""
    doc = GD.build_sidecar_closeout([
        _study("A", "MATCH", "REFUTED", "REVIEW_PASS"),
        _study("B", "MATCH", "INCONCLUSIVE", "REVIEW_PASS")])
    assert doc["scientific_direction"] == ["REFUTED", "INCONCLUSIVE"]
    assert doc["and_aggregation"]["combined"] is True


def test_empty_study_set_is_not_vacuously_true():
    doc = GD.build_sidecar_closeout([])
    assert doc["and_aggregation"]["combined"] is False
    assert doc["and_aggregation"]["claims_all_match"] is False


def test_three_axis_matrix_reports_evidence_not_single_label():
    traces = [{"study": "A", "gaps": ["request_serialized"],
               "suspected": "RETURN_VIEW", "attribution_level": "RECONSTRUCTED"},
              {"study": "B", "gaps": ["request_serialized"],
               "suspected": "RETURN_VIEW", "attribution_level": "RECONSTRUCTED"}]
    audits = [{"ok": False, "codes": ["PACKET_INTENT_STUDY"]}]
    cases = [{"case_id": "c", "ok": True}]
    doc = GD.aggregate_diagnostics(diagnostic_id="g1t", traces=traces,
                                   packet_audits=audits, compiler_cases=cases)
    axes = doc["axes"]
    assert axes["history_observability"] == "PARTIAL"
    assert axes["delivery_integrity"] == "UNKNOWN"
    assert axes["packet_integrity"] == "FAIL"
    assert axes["compiler_fixture_status"] == "PASS"
    assert axes["model_comparison"] == "NOT_RUN"
    classes = {row["class"]: row["applies"] for row in doc["classification"]}
    assert classes["INSTRUMENT"] is True
    assert classes["DELIVERY_SUSPECTED"] is True
    assert classes["CANDIDATE"] is False
    assert classes["UNOBSERVABLE"] is True


def test_delivery_confirmed_only_with_observed_chain():
    traces = [{"study": "A", "gaps": [], "suspected": None,
               "attribution_level": "OBSERVED"}]
    doc = GD.aggregate_diagnostics(diagnostic_id="g1t", traces=traces,
                                   packet_audits=[{"ok": True, "codes": []}],
                                   compiler_cases=[{"case_id": "c", "ok": True}])
    assert doc["axes"]["delivery_integrity"] == "CONFIRMED"
    assert doc["axes"]["history_observability"] == "COMPLETE"
