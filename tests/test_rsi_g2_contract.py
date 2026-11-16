"""RSI-G2 最小交付合同：原子性、容量、规范字节与只读分页（零 POST）。"""
from __future__ import annotations

import json

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import g2_contract as G2

PLAN = "d" * 64
RECEIPT = "c" * 64
OUTPUT = "b" * 64


def contract(**kw):
    doc = {
        "schema": G2.SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
        "contract_id": "c1", "task_id": "t1", "task_spec_sha256": "a" * 64,
        "hypothesis_id": "h1", "estimand_id": "e1", "metric_id": "m1",
        "proposition": "increase_gt", "delta": 0.001, "unit": "m",
        "target_population": "registered_distribution", "aggregation": "mean",
        "weighting": "equal", "block_to_contrast_formula": "mean",
        "formula_notes": "等权 mean", "censoring_rule": "dropped 不补值",
        "coverage": "observed_blocks", "interval_level": 0.9,
        "allowed_semantic_labels": list(G2.SEMANTIC_LABELS),
    }
    doc.update(kw)
    return G2.ScopeContractV1.from_dict(doc)


def envelope(**kw):
    args = dict(
        study_id="A", phase="confirm", plan_sha256=PLAN,
        analysis_receipt_sha256=RECEIPT, analysis_output_sha256=OUTPUT,
        scope_contract=contract(), required_contrast_ids=["k1"],
        contrasts=[{"contrast_id": "k1", "metric_id": "m1",
                    "proposition": "increase_gt", "delta": 0.001,
                    "estimate": 0.05, "lower": 0.01, "upper": 0.09,
                    "level": 0.9, "science_status": "SUPPORTED",
                    "n_blocks": 4, "dropped": {"count": 0, "ids": [],
                                               "ids_sha256": ""}}],
        primary_contrast_id="k1", denominator=4,
        dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics={"status": "OBSERVED"},
        artifacts=[{"artifact_id": "analysis/output.json",
                    "relative_path": "analysis/output.json",
                    "sha256": OUTPUT, "bytes": 10,
                    "access_mode": "read_result_artifact"}],
        evidence=[{"artifact_id": "analysis/output.json",
                   "json_pointer": "/contrasts/0/estimate",
                   "field_sha256": G2.field_sha(0.05)}],
        summary={"n": 1})
    args.update(kw)
    return G2.build_envelope(**args)


def _rehash(doc):
    doc = dict(doc)
    doc["summary_sha256"] = G2.field_sha(doc.get("summary") or {})
    doc["envelope_sha256"] = G2.content_sha(doc, exclude=("envelope_sha256",))
    return doc


def test_required_key_sets_agree_with_transport():
    assert tuple(G2.ENVELOPE_REQUIRED) == tuple(T.G2_ENVELOPE_REQUIRED)
    assert T.POLICY_G2_ATOMIC == G2.POLICY_G2_ATOMIC


def test_envelope_roundtrip_and_verify():
    env = envelope()
    report = G2.verify_envelope(env, scope_contract=contract())
    assert report["ok"] and report["n_contrasts"] == 1


def test_missing_primary_is_atomic_failure_not_partial():
    env = envelope()
    broken = {k: v for k, v in env.items() if k != "primary"}
    with pytest.raises(G2.G2Error) as exc:
        G2.verify_envelope(broken)
    assert exc.value.code == "ENVELOPE_INVALID"
    with pytest.raises(T.ResultViewIncomplete):
        T.g2_envelope_bytes(broken)


def test_missing_required_contrast_is_refused():
    env = envelope()
    bad = dict(env, required_contrast_ids=["k1", "k2"])
    with pytest.raises(G2.G2Error) as exc:
        G2.verify_envelope(bad)
    assert exc.value.code == "RESULT_VIEW_INCOMPLETE"


def test_summary_is_droppable_primary_is_not():
    env = envelope()
    no_summary = {k: v for k, v in env.items() if k != "summary"}
    no_summary = _rehash(no_summary)
    assert G2.verify_envelope(no_summary)["ok"]
    assert "primary" in no_summary


def test_primary_tamper_breaks_envelope_sha():
    env = envelope()
    env["primary"]["estimate"] = 0.06
    with pytest.raises(G2.G2Error) as exc:
        G2.verify_envelope(env)
    assert exc.value.code == "ENVELOPE_INVALID"


def test_oversize_envelope_is_blocked_before_send():
    big = envelope()
    big["pad"] = "x" * 17000
    with pytest.raises(T.ResultViewIncomplete) as exc:
        T.g2_envelope_bytes(big)
    assert "16384" in exc.value.detail


def test_boundary_16384_accepted_16385_rejected():
    env = envelope()
    base = len(T.g2_envelope_bytes(env))
    room = T.G2_ENVELOPE_MAX_BYTES - base - 20
    fitted = {k: v for k, v in env.items()}
    fitted["pad"] = "x" * max(0, room)
    assert len(T.g2_envelope_bytes(fitted)) <= T.G2_ENVELOPE_MAX_BYTES
    fitted["pad"] = "x" * (room + 40)
    with pytest.raises(T.ResultViewIncomplete):
        T.g2_envelope_bytes(fitted)


def test_strict_json_rejects_duplicate_keys_and_nan():
    with pytest.raises(G2.G2Error) as exc:
        G2.parse_json_strict('{"a":1,"a":2}')
    assert exc.value.code == "DUPLICATE_KEY"
    with pytest.raises(G2.G2Error) as exc:
        G2.parse_json_strict('{"a":NaN}')
    assert exc.value.code == "NAN_OR_INF"


def test_scope_contract_requires_public_enums_and_formula():
    with pytest.raises(G2.G2Error) as exc:
        contract(block_to_contrast_formula="")
    assert exc.value.code == "SCOPE_CONTRACT_UNDEFINED"
    with pytest.raises(G2.G2Error) as exc:
        contract(aggregation="median", block_to_contrast_formula="mean")
    assert exc.value.code == "SCOPE_CONTRACT_UNDEFINED"
    with pytest.raises(G2.G2Error) as exc:
        contract(target_population="population")
    assert exc.value.code == "SCOPE_CONTRACT_UNDEFINED"
    with pytest.raises(G2.G2Error) as exc:
        contract(coverage="all_blocks")
    assert exc.value.code == "SCOPE_CONTRACT_UNDEFINED"


def test_scope_contract_content_sha_excludes_own_hash_only():
    doc = contract().to_dict()
    assert contract().sha256 == G2.content_sha(
        {k: v for k, v in doc.items() if k != "contract_sha256"})


def test_read_page_multipage_and_errors():
    data = ("0123456789" * 2000).encode()          # 20 KB
    digest = G2.sha_bytes(data)
    reg = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json",
        "relative_path": "analysis/output.json", "sha256": digest,
        "bytes": len(data), "access_mode": "read_result_artifact"}])
    pages, cursor = [], 0
    while cursor is not None:
        page = G2.read_page(registry=reg, read_bytes=lambda rel: data,
                            artifact_id="analysis/output.json",
                            output_sha256=digest, cursor=cursor,
                            page_bytes=8192)
        pages.append(page["data"])
        cursor = page["next_cursor"]
    assert b"".join(p.encode() for p in pages) == data
    assert len(pages) >= 3
    for kwargs, code in (
            ({"cursor": -1}, "CURSOR_INVALID"),
            ({"cursor": len(data) + 1}, "CURSOR_INVALID"),
            ({"page_bytes": 9000}, "PAGE_TOO_LARGE"),
            ({"output_sha256": "0" * 64}, "ARTIFACT_SHA_MISMATCH"),
            ({"artifact_id": "analysis/none.json"}, "ARTIFACT_MISSING")):
        base = dict(registry=reg, read_bytes=lambda rel: data,
                    artifact_id="analysis/output.json",
                    output_sha256=digest, cursor=0, page_bytes=8192)
        base.update(kwargs)
        with pytest.raises(G2.G2Error) as exc:
            G2.read_page(**base)
        assert exc.value.code == code


def test_read_page_refuses_private_and_traversal_paths():
    for rel, code in (("private_expected/x.json", "ARTIFACT_FORBIDDEN"),
                      ("judge/x.json", "ARTIFACT_FORBIDDEN"),
                      ("analysis/../../etc/passwd", "ARTIFACT_PATH")):
        with pytest.raises(G2.G2Error) as exc:
            G2.ArtifactRegistry([{"artifact_id": "x", "relative_path": rel,
                                  "sha256": "a" * 64, "bytes": 1,
                                  "access_mode": "read_result_artifact"}])
        assert exc.value.code == code


def test_read_page_enforces_artifact_size_cap():
    with pytest.raises(G2.G2Error) as exc:
        G2.ArtifactRegistry([{"artifact_id": "x",
                              "relative_path": "analysis/x.json",
                              "sha256": "a" * 64,
                              "bytes": G2.ARTIFACT_MAX_BYTES + 1,
                              "access_mode": "read_result_artifact"}])
    assert exc.value.code == "ARTIFACT_PATH"


def test_read_page_refuses_cursor_inside_multibyte_char():
    data = "结果".encode("utf-8") * 10          # 每字 3 字节
    digest = G2.sha_bytes(data)
    reg = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json",
        "relative_path": "analysis/output.json", "sha256": digest,
        "bytes": len(data), "access_mode": "read_result_artifact"}])
    with pytest.raises(G2.G2Error) as exc:
        G2.read_page(registry=reg, read_bytes=lambda rel: data,
                     artifact_id="analysis/output.json",
                     output_sha256=digest, cursor=1, page_bytes=8)
    assert exc.value.code == "CURSOR_INVALID"
    page = G2.read_page(registry=reg, read_bytes=lambda rel: data,
                        artifact_id="analysis/output.json",
                        output_sha256=digest, cursor=3, page_bytes=8)
    assert page["data"].encode("utf-8") == data[3:9]
    assert page["next_cursor"] == 9


def test_contract_snapshot_verifies_and_detects_drift():
    snapshot = G2.build_contract_snapshot(
        policy_id=T.POLICY_G2_ATOMIC, scope_contract=contract(),
        schema_files={"schema": __file__}, required_contrast_ids=["k1"],
        source_commit="abc", implementation_files=[__file__],
        tokenizer_id="local", tokenizer_sha256="f" * 64)
    assert G2.verify_contract_snapshot(snapshot)["ok"]
    snapshot["limits"]["envelope_max_bytes"] = 4096
    snapshot["contract_sha256"] = G2.content_sha(
        snapshot, exclude=("contract_sha256",))
    with pytest.raises(G2.G2Error):
        G2.verify_contract_snapshot(snapshot)


def test_envelope_from_output_uses_candidate_values_only():
    output = {"contrasts": [{"contrast_id": "k1", "metric_id": "m1",
                             "proposition": "increase_gt",
                             "estimate": -0.0042, "lower": -0.0151,
                             "upper": 0.0063, "status": "REFUTED",
                             "n_blocks": 2}]}
    env = G2.envelope_from_output(
        study_id="A", phase="confirm", plan_sha256=PLAN, output=output,
        output_bytes=json.dumps(output).encode(), receipt_bytes=b"{}",
        scope_contract=contract(), required_contrast_ids=["k1"],
        primary_contrast_id="k1")
    assert env["primary"]["estimate"] == pytest.approx(-0.0042)
    assert env["contrasts"][0]["science_status"] == "REFUTED"
    G2.verify_envelope(env, scope_contract=contract())


def test_envelope_from_output_refuses_missing_required_contrast():
    with pytest.raises(G2.G2Error) as exc:
        G2.envelope_from_output(
            study_id="A", phase="confirm", plan_sha256=PLAN,
            output={"contrasts": []}, output_bytes=b"{}", receipt_bytes=b"{}",
            scope_contract=contract(), required_contrast_ids=["k1"],
            primary_contrast_id="k1")
    assert exc.value.code == "RESULT_VIEW_INCOMPLETE"
