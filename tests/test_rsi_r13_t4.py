"""RSI-R1.3 T4：六维量表、双审合并、收据隔离与审查执行器（零 POST）。"""

from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import review_receipt as RRC
from orchestrator.rsi_r1 import review_rubric_v13 as RV
from orchestrator.rsi_r1 import review_runner as RN
from orchestrator.rsi_r1 import task_specs as TS


def _evidence():
    doc = {"blocks": [{"block_id": "b00", "effect": 0.05}],
           "report": {"limitations": ["cover box only"]}}
    data = (json.dumps(doc, sort_keys=True) + "\n").encode()
    return {"packet.json": data}


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _locator(evidence):
    return {"kind": "json_pointer", "file": "packet.json",
            "sha256": _sha(evidence["packet.json"]),
            "pointer": "/blocks/0/effect"}


def _review(evidence, review_id="rv1", reviewer="critic1", overrides=None):
    ref = _locator(evidence)
    items = {}
    for name in RV.ITEMS_V13:
        verdict = (overrides or {}).get(name, "PASS")
        items[name] = {"verdict": verdict, "reason": f"{name} reason",
                       "refs": [ref]}
    return {"schema": RV.REVIEW_SCHEMA, "review_id": review_id,
            "reviewer_id": reviewer, "packet_sha256": "f" * 64,
            "items": items}


def _isolation():
    return RRC.IsolationEvidence(
        fresh_context=True, fresh_process=True, env_scrubbed=True,
        cache_dir="/tmp/x", other_review_inputs_present=False,
        created_by="scheduler",
        evidence=({"kind": "fresh_dir", "sha256": "a" * 64},))


def _receipt(evidence, review, packet_sha="f" * 64):
    receipt = RRC.build_receipt(
        review_id=review["review_id"], reviewer_id=review["reviewer_id"],
        session_id="s1", process_id=1, prompt=b"prompt", schema=b"schema",
        source=b"source", packet=b"packet", request=b"request",
        response=b"response", access_log=b"log",
        input_allowlist=["packet.json"], started_at_utc="2026-11-15T00:00:00Z",
        ended_at_utc="2026-11-15T00:00:01Z", isolation=_isolation())
    # 单元测试用固定 packet 摘要；生产收据的 packet_sha256 由 build_receipt 实算
    receipt["packet_sha256"] = packet_sha
    return receipt


def test_review_structure_and_refs():
    ev = _evidence()
    parsed = RV.validate_review_v13(_review(ev), evidence=ev,
                                    expected_packet_sha256="f" * 64)
    assert set(parsed["items"]) == set(RV.ITEMS_V13)
    bad = _review(ev)
    bad["items"]["claim_scope"]["refs"] = []
    with pytest.raises(RV.ReviewError) as exc:
        RV.validate_review_v13(bad, evidence=ev,
                               expected_packet_sha256="f" * 64)
    assert exc.value.code == "REFS_EMPTY"


def test_combine_pass_fail_and_inconclusive():
    ev = _evidence()
    r1, r2 = _review(ev), _review(ev, "rv2", "critic2")
    receipts = [_receipt(ev, r1), _receipt(ev, r2)]
    out = RV.combine_reviews([r1, r2], receipts=receipts,
                             expected_packet_sha256="f" * 64, evidence=ev)
    assert out.verdict == "REVIEW_PASS"
    assert out.science_inconclusive is False

    fail = _review(ev, "rv2", "critic2", {"claim_scope": "FAIL"})
    out = RV.combine_reviews([r1, fail],
                             receipts=[_receipt(ev, r1), _receipt(ev, fail)],
                             expected_packet_sha256="f" * 64, evidence=ev)
    assert out.verdict == "REVIEW_FAIL"
    assert out.items["claim_scope"] == "FAIL"
    assert any("claim_scope" in r for r in out.reasons)

    unsure = _review(ev, "rv2", "critic2", {"claim_scope": "UNVERIFIABLE"})
    out = RV.combine_reviews([r1, unsure],
                             receipts=[_receipt(ev, r1), _receipt(ev, unsure)],
                             expected_packet_sha256="f" * 64, evidence=ev,
                             science_inconclusive=True)
    assert out.verdict == "REVIEW_INCONCLUSIVE"
    assert out.science_inconclusive is True
    assert out.to_dict()["third_vote"] is False


def test_two_reviews_must_be_genuinely_distinct():
    ev = _evidence()
    r1 = _review(ev)
    clone = json.loads(json.dumps(r1))
    with pytest.raises(RV.ReviewError) as exc:
        RV.combine_reviews([r1, clone],
                           receipts=[_receipt(ev, r1), _receipt(ev, clone)],
                           expected_packet_sha256="f" * 64, evidence=ev)
    assert exc.value.code == "REVIEW_NOT_INDEPENDENT"


def test_receipt_rejects_self_reported_isolation_and_visibility():
    ev = _evidence()
    review = _review(ev)
    receipt = _receipt(ev, review)
    RRC.verify_receipt(receipt, review_id="rv1", reviewer_id="critic1",
                       packet_sha256="f" * 64)
    bad = dict(receipt, other_review_visible=True)
    with pytest.raises(RRC.ReceiptError):
        RRC.verify_receipt(bad, review_id="rv1", reviewer_id="critic1",
                           packet_sha256="f" * 64)
    iso = dict(receipt["isolation"], created_by="model")
    bad2 = dict(receipt, isolation=iso)
    with pytest.raises(RRC.ReceiptError):
        RRC.verify_receipt(bad2, review_id="rv1", reviewer_id="critic1",
                           packet_sha256="f" * 64)


def test_isolation_rejects_contaminated_inputs():
    slot = RN.ReviewSlot("rv2", "critic2", "s2")
    with pytest.raises(RN.ReviewRunnerError) as exc:
        RN.prepare_isolation(slot=slot, other_review_files=["rv1.json"])
    assert exc.value.code == "ISOLATION_CONTAMINATED"


def test_packet_is_self_contained_and_has_no_machine_verdict():
    task = TS.build_new_a()
    values = {f"b{i:02d}": {c.id: 1.0 + j * 0.1
                            for j, c in enumerate(task.conditions)}
              for i in range(2)}
    assignments = [{"block_id": f"b{i:02d}", "nuisance": {"mass_kg": "0.5"}}
                   for i in range(2)]
    ev = _evidence()
    packet = RN.build_packet(
        study="A", task=task,
        intent={"phase_role": "confirmation"}, design_audit={"run_count": 10},
        assignments=assignments, values=values,
        report={"limitations": ["box only"]}, evidence_files=ev,
        candidate_excerpts=[{"file": "packet.json", "pointer": "/report",
                             "sha256": _sha(ev["packet.json"])}],
        sources={"design.py": b"x" * 10})
    body = json.dumps(packet, ensure_ascii=False)
    assert "should PASS" not in body and "should FAIL" not in body
    assert len(packet["block_table"]) == 2
    assert all(row["nuisance"] for row in packet["block_table"])
    assert packet["primary_recomputation"]["n_blocks"] == 2


def test_packet_rejects_partial_block_table():
    task = TS.build_new_a()
    values = {"b00": {c.id: 1.0 for c in task.conditions}}
    with pytest.raises(RN.ReviewRunnerError) as exc:
        RN.build_packet(study="A", task=task, intent={}, design_audit={},
                        assignments=[{"block_id": "b00"},
                                     {"block_id": "b01"}],
                        values=values, report={}, evidence_files={},
                        candidate_excerpts=[], sources={})
    assert exc.value.code == "PACKET_INCOMPLETE"


def test_calibration_requires_critical_failures():
    ok = {
        "valid_positive": {n: "PASS" for n in RV.ITEMS_V13},
        "pseudo_replication": {n: "PASS" for n in RV.ITEMS_V13} | {
            "exploration_declaration": "FAIL"},
        "overclaim_universal": {n: "PASS" for n in RV.ITEMS_V13} | {
            "claim_scope": "FAIL"},
        "valid_uncertain": {n: "PASS" for n in RV.ITEMS_V13},
        "fixed_pilot_scoped": {n: "PASS" for n in RV.ITEMS_V13},
        "empty_limitations": {n: "PASS" for n in RV.ITEMS_V13} | {
            "result_driven_limitations_next_steps": "FAIL"},
    }
    assert RN.check_calibration(ok)["code"] == "REVIEW_INSTRUMENT_READY"
    bad = json.loads(json.dumps(ok))
    bad["pseudo_replication"]["exploration_declaration"] = "PASS"
    out = RN.check_calibration(bad)
    assert out["code"] == "REVIEW_INSTRUMENT_NOT_READY"
    assert not out["ok"]


def test_calibration_packet_has_no_labels():
    packet = RN.calibration_packet(["valid_positive", "pseudo_replication",
                                    "overclaim_universal"])
    body = json.dumps(packet, ensure_ascii=False)
    assert "expected" in body
    assert packet["cases"][0]["expected_verdicts"] is None


def test_runner_single_packet_parse_and_receipt():
    task = TS.build_new_a()
    values = {"b00": {c.id: 1.0 for c in task.conditions},
              "b01": {c.id: 1.2 for c in task.conditions}}
    ev = _evidence()
    packet = RN.build_packet(
        study="A", task=task, intent={}, design_audit={"run_count": 2},
        assignments=[{"block_id": "b00"}, {"block_id": "b01"}],
        values=values, report={}, evidence_files=ev,
        candidate_excerpts=[], sources={})
    ref = _locator(ev)
    reply = {
        "schema": RV.REVIEW_SCHEMA, "review_id": "rv1", "reviewer_id": "critic1",
        "packet_sha256": RN.packet_sha256(packet),
        "items": {n: {"verdict": "PASS", "reason": "ok", "refs": [ref]}
                  for n in RV.ITEMS_V13},
    }
    slot = RN.ReviewSlot("rv1", "critic1", "s1")
    _, isolation = RN.prepare_isolation(slot=slot)
    out = RN.run_review(packet=packet, slot=slot,
                        sender=lambda system, user: json.dumps(reply),
                        evidence_files=ev, prompt=b"p", schema=b"s",
                        source=b"x", isolation=isolation)
    assert out["review"]["reviewer_id"] == "critic1"
    RRC.verify_receipt(out["receipt"], review_id="rv1", reviewer_id="critic1",
                       packet_sha256=RN.packet_sha256(packet))
    with pytest.raises(RN.ReviewRunnerError) as exc:
        RN.run_review(packet=packet, slot=slot, sender=lambda s, u: "",
                      evidence_files=ev, prompt=b"p", schema=b"s",
                      source=b"x", isolation=isolation)
    assert exc.value.code == "REVIEW_EMPTY_RESPONSE"


def test_review_schema_is_new_family():
    assert RV.VERDICT_SCHEMA == P.REVIEW_VERDICT_SCHEMA_V13
    assert RRC.RECEIPT_SCHEMA == P.REVIEW_RECEIPT_SCHEMA_V13
    assert RN.PACKET_SCHEMA == P.REVIEW_PACKET_SCHEMA_V13
    assert len(RN.CALIBRATION_CASES) == 6
