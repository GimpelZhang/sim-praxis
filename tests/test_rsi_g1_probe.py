"""RSI-G1 B 层：预注册、评分、双轴判定与独立参照（零网络）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g1_probe as P
from tests import g1_oracle_reference as ORACLE


def _spec(tmp_path: Path) -> "dict":
    return P.build_prereg(
        diagnostic_id="g1t",
        ledger_path=tmp_path / "q" / "ledger.jsonl",
        stage_models_path=Path("orchestrator/gateway/stage_models.yaml"),
        model_id="test-model", input_manifest_sha="0" * 64)


def _good_reply(case: dict, interface: str) -> str:
    contrasts = case["analysis"]["doc"]["contrasts"]
    idx = next(i for i, c in enumerate(contrasts)
               if c["contrast_id"] == case["expected"]["contrast_id"])
    node = contrasts[idx]
    claim = {"hypothesis_id": "h", "proposition": node["proposition"],
             "metric_id": node["metric_id"], "delta": float(node["delta"]),
             "scope": case["expected"]["scope"],
             "semantic": case["expected"]["semantic"]}
    if interface == "verbatim":
        claim.update({k: node[k] for k in ("estimate", "lower", "upper")})
    return json.dumps({"claim": claim,
                       "result_ref": {"artifact_id": "analysis/output.json",
                                      "json_pointer": f"/contrasts/{idx}"}})


def test_request_budget_covers_thinking_tokens():
    """G1-D1：输出帽必须 ≥ 8192（thinking 型模型会把思考算进 max_tokens）。"""
    case = P.build_cases()[0]
    for iface in P.INTERFACES:
        request = P.build_request(case, iface)
        assert request.max_tokens >= 8192


def test_prereg_is_24_slots_balanced_and_frozen(tmp_path):
    spec = _spec(tmp_path)
    slots = spec["slots"]
    assert len(slots) == 24
    combos = {(s["case_id"], s["interface"], s["replicate"]) for s in slots}
    assert len(combos) == 24
    assert {s["interface"] for s in slots} == {"verbatim", "by_ref"}
    assert all(s["cache_policy"] == "off" and s["max_attempts"] == 1
               for s in slots)
    assert len({s["slot_id"] for s in slots}) == 24
    assert all(len(s["stage_config_sha"]) == 64 for s in slots)


def test_score_slot_matches_independent_oracle_for_all_cases(tmp_path):
    for case in P.build_cases():
        for iface in P.INTERFACES:
            doc = json.loads(_good_reply(case, iface))
            mine = P.score_slot(case, iface, json.dumps(doc))
            ref = ORACLE.judge(case, iface, doc)
            assert mine["mechanical_ok"] == ref["mechanical_ok"], (case["case_id"], iface)
            assert mine["semantic_ok"] == ref["semantic_ok"], (case["case_id"], iface)
            assert mine["mechanical_ok"] and mine["semantic_ok"]


def test_verbatim_wrong_numbers_fail_but_by_ref_passes():
    case = P.build_cases()[0]
    doc = json.loads(_good_reply(case, "verbatim"))
    doc["claim"]["estimate"] = 0.0501
    bad = P.score_slot(case, "verbatim", json.dumps(doc))
    assert bad["mechanical_ok"] is False and bad["numeric_ok"] is False
    by_ref = json.loads(_good_reply(case, "by_ref"))
    by_ref["claim"]["estimate"] = 0.0501   # by_ref 不抄数值：额外字段不影响
    assert P.score_slot(case, "by_ref", json.dumps(by_ref))["mechanical_ok"]


def test_semantic_mismatch_is_recorded_not_mechanical_failure():
    case = P.build_cases()[0]
    doc = json.loads(_good_reply(case, "by_ref"))
    doc["claim"]["scope"] = "all_blocks"
    score = P.score_slot(case, "by_ref", json.dumps(doc))
    assert score["mechanical_ok"] is True
    assert score["semantic_ok"] is False


def test_fenced_reply_and_missing_fields():
    case = P.build_cases()[0]
    fenced = "```json\n" + _good_reply(case, "by_ref") + "\n```"
    assert P.score_slot(case, "by_ref", fenced)["mechanical_ok"]
    doc = json.loads(_good_reply(case, "verbatim"))
    doc["claim"].pop("upper")
    assert P.score_slot(case, "verbatim", json.dumps(doc))["error"].startswith("MISSING")


def _records_for(spec, *, fail_iface=None, fail_case=None):
    cases = {c["case_id"]: c for c in P.build_cases()}
    out = []
    for slot in spec["slots"]:
        case = cases[slot["case_id"]]
        score = P.score_slot(case, slot["interface"],
                             _good_reply(case, slot["interface"]))
        if slot["interface"] == fail_iface or slot["case_id"] == fail_case:
            score = dict(score, mechanical_ok=False)
        out.append({"slot_id": slot["slot_id"], "case_id": slot["case_id"],
                    "interface": slot["interface"],
                    "replicate": slot["replicate"], "status": "CALLED_OK",
                    "request_sha256": "a" * 64, "ledger_seq": 1,
                    "score": score})
    return out


def test_decide_branches(tmp_path):
    spec = _spec(tmp_path)
    r = P.decide(spec, _records_for(spec))
    assert r["overall"] == "DIAGNOSTIC_COMPLETE"
    assert r["axes"]["delivery"] == "NO_DELIVERY_BOTTLENECK_OBSERVED"
    assert r["axes"]["semantic"] == "NO_REPEAT_GAP_OBSERVED"

    r = P.decide(spec, _records_for(spec, fail_iface="by_ref"))
    assert r["axes"]["delivery"] == "REFERENCE_INTERFACE_NOT_READY"

    r = P.decide(spec, _records_for(spec, fail_iface="verbatim"))
    assert r["axes"]["delivery"] == "DELIVERY_LEVER_SUPPORTED_LOCAL"

    rows = _records_for(spec)
    rows[0] = dict(rows[0], status="NOT_CALLED")
    r = P.decide(spec, rows)
    assert r["overall"] == "DIAGNOSTIC_INCONCLUSIVE"
    assert r["axes"]["delivery"] == "NOT_MEASURED"

    r = P.decide(spec, _records_for(spec), ["S01:ATTEMPTS"])
    assert r["overall"] == "DIAGNOSTIC_INCONCLUSIVE"


def test_semantic_gap_detected_when_both_arms_repeat_wrong(tmp_path):
    spec = _spec(tmp_path)
    rows = _records_for(spec)
    for row in rows:
        if row["case_id"] == "C4":
            row["score"] = dict(row["score"], semantic_ok=False)
    r = P.decide(spec, rows)
    assert r["axes"]["semantic"] == "SEMANTIC_GAP_SUSPECTED"


def test_run_probe_stops_on_duplicate_slot(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "g1"
    (root / "probe" / "approvals").mkdir(parents=True)
    approval = {"diagnostic_id": "g1t", "approved_by": "test",
                "max_posts": 24, "verbatim": "test"}
    P.write_approval(root, approval)
    ledger = P.G1Ledger(root)
    ledger._append({"event": "call", "seq": 1, "batch_id": "G1B-S01",
                    "ts_iso": "2026-11-15T00:00:00Z", "ok": True})
    calls = []

    def sender(request, *, slot_id):
        calls.append(slot_id)
        return {"text": "{}", "request_sha256": "0" * 64,
                "response_id": "x", "attempts_http": 1, "ledger_seq": 1,
                "n_bytes": 1}

    report = P.run_probe(root=root, prereg=spec, sender=sender,
                         approval_path=root / "probe" / "approvals" / "G1_DIAGNOSTIC_B.json",
                         ledger_path=root / "quota" / "ledger.jsonl")
    assert report["overall"] == "DIAGNOSTIC_INCONCLUSIVE"
    assert any("DUPLICATE" in i for i in report["invariants"])
    assert len(calls) == 23
