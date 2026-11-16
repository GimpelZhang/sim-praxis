"""RSI-G2.1 容量：16 KiB/token/页/artifact 边界与批准 tokenizer（零 POST）。"""
from __future__ import annotations

from pathlib import Path

from orchestrator.rsi_r1 import g2_cli as CLI
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g21_bridge as G21
from tests.fixtures import g21_synthetic as SYN


def _prepared(tmp_path: Path) -> Path:
    doc = SYN.replay_manifest_doc(tmp_path)
    manifest = tmp_path / "replay_manifest.json"
    import json
    manifest.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    root = tmp_path / "diag"
    assert CLI.main(["bridge-init", "--root", str(root),
                     "--inputs", str(manifest)]) == 0
    return root


def test_tokenizer_contract_is_the_approved_artifact(tmp_path):
    root = _prepared(tmp_path)
    contract = G21.tokenizer_contract(root)
    assert contract["status"] == "TOKENIZER_READY"
    doc = contract["contract"]
    assert doc["supplier_token_count"] == "UNKNOWN_NOT_CLAIMED"
    assert doc["sha256"] == doc["expected_sha256"]
    assert doc["approval_ref"].startswith("orchestrator/rsi_r1/review_capacity")


def test_capacity_matrix_all_fixtures(tmp_path):
    root = _prepared(tmp_path)
    manifest = G21.load_manifest(root / "freeze" / "replay_manifest.json")
    matrix = G21.capacity_matrix(
        G2.load_scope_contract(root / "contract" / "scope_A.json"))
    G21.write_json_private(root / "freeze" / "capacity_matrix.json", matrix)
    report = G21.run_capacity(root, manifest,
                              tokenizer=G21.tokenizer_contract(
                                  root)["tokenizer"])
    assert report["ok"] is True, [row for row in report["results"]
                                  if not row["ok"]]
    assert report["n_ok"] == report["n_total"] >= 11
    rows = {row["fixture_id"]: row for row in report["results"]}
    # 真实 16 KiB 边界：≤ 帽 正常，+1 条对比在 sender 前阻断
    assert rows["envelope_16384_ok"]["envelope_bytes"] <= G2.ENVELOPE_MAX_BYTES
    assert rows["envelope_16385_block"]["envelope_bytes"] > G2.ENVELOPE_MAX_BYTES
    assert rows["envelope_16385_block"]["outcome"] == "BLOCK_BEFORE_SEND"
    assert rows["page_8192_ok"]["outcome"] == "NORMAL_SUCCESS"
    assert rows["page_8193_block"]["outcome"] == "BLOCK_BEFORE_SEND"
    assert rows["artifact_over_8mib_block"]["outcome"] == "BLOCK_BEFORE_SEND"
    assert rows["legacy_4096_boundary"]["big_bytes"] <= 4096
    assert any(row["outcome"] == "NORMAL_SUCCESS" for row in report["results"])


def test_capacity_requires_frozen_matrix(tmp_path):
    root = _prepared(tmp_path)
    manifest = G21.load_manifest(root / "freeze" / "replay_manifest.json")
    try:
        G21.run_capacity(root, manifest, tokenizer=None)
    except G21.G21Error as exc:
        assert exc.code == "CAPACITY_MATRIX_MISSING"
    else:  # pragma: no cover - 冻结矩阵必须已写
        raise AssertionError("缺冻结矩阵时必须阻断")


def test_capacity_matrix_entries_carry_frozen_limits(tmp_path):
    root = _prepared(tmp_path)
    matrix = G21.capacity_matrix(
        G2.load_scope_contract(root / "contract" / "scope_A.json"))
    for entry in matrix["entries"]:
        assert entry["required_ids"]
        assert entry["envelope_byte_limit"] == G2.ENVELOPE_MAX_BYTES
        assert entry["request_token_limit"] > 0
        assert entry["expected_outcome"] in (
            "NORMAL_SUCCESS", "BLOCK_BEFORE_SEND", "SOURCE_OR_SUCCESS")
