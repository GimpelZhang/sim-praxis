"""S0-6c：receipt ↔ manifest 互证（一正一负）+ ResearchMemory append-only/哈希链。"""
from __future__ import annotations

import hashlib
import json

import pytest

from orchestrator.rsi_bridge.memory_store import ResearchMemory, ResearchMemoryError
from orchestrator.rsi_bridge.receipts import (ReceiptMismatch, crosscheck_receipt,
                                              load_manifest_files)


def _manifest(tmp_path):
    body = b"run-1"
    digest = hashlib.sha256(body).hexdigest()
    document = {
        "schema": "simpraxis.bundle_manifest.v1",
        "tables": {"kb_chunks": {"rows": 1, "files": [
            {"relpath": "abc.lance", "size": len(body), "sha256": digest}]}},
    }
    path = tmp_path / "bundle_manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, digest


def _receipt(digest):
    return {"schema": "physicalrsi.experiment-receipt/v2",
            "files": {"tables/kb_chunks/abc.lance": digest}}


def test_crosscheck_passes_on_identical_hashes(tmp_path):
    path, digest = _manifest(tmp_path)
    index = load_manifest_files(path)
    matched = crosscheck_receipt(_receipt(digest), index)
    assert matched["count"] == 1


def test_crosscheck_fails_on_one_byte_change(tmp_path):
    path, digest = _manifest(tmp_path)
    index = load_manifest_files(path)
    tampered = _receipt(digest[:-1] + ("0" if digest[-1] != "0" else "1"))
    with pytest.raises(ReceiptMismatch):
        crosscheck_receipt(tampered, index)
    with pytest.raises(ReceiptMismatch):
        crosscheck_receipt({"schema": "v1", "files": {"x": digest}}, index)


def test_memory_append_only_hash_chain(tmp_path):
    memory = ResearchMemory(tmp_path / "research_memory", journal="rm")
    first = memory.append(lesson_id="L1", lesson="先冻结再复跑",
                          evidence=["receipt:abc"], counterexample="R1-N 冻结后改方法")
    second = memory.append(lesson_id="L2", lesson="打回必须带意见",
                           evidence=["gate2:def"], counterexample="空意见打回")
    assert second["prev_sha256"] == first["sha256"]
    with pytest.raises(ResearchMemoryError):
        memory.append(lesson_id="L1", lesson="覆写", evidence=["x"], counterexample="y")
    with pytest.raises(ResearchMemoryError):
        memory.append(lesson_id="L3", lesson="无证据", evidence=[], counterexample="y")
    with pytest.raises(ResearchMemoryError):
        memory.append(lesson_id="L3", lesson="无反例", evidence=["x"], counterexample=" ")
    with pytest.raises(ResearchMemoryError):
        memory.rewrite_forbidden("L1", "silently changed")


def test_memory_chain_detects_tampering(tmp_path):
    root = tmp_path / "research_memory"
    memory = ResearchMemory(root, journal="rm")
    memory.append(lesson_id="L1", lesson="原始教训",
                  evidence=["e"], counterexample="c")
    path = root / "rm.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["lesson"] = "被改写"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with pytest.raises(ResearchMemoryError):
        ResearchMemory(root, journal="rm")


def test_memory_reload_roundtrip(tmp_path):
    root = tmp_path / "research_memory"
    memory = ResearchMemory(root, journal="rm")
    memory.append(lesson_id="L1", lesson="x", evidence=["e"], counterexample="c")
    again = ResearchMemory(root, journal="rm")
    assert [entry["lesson_id"] for entry in again.entries] == ["L1"]
    export = again.export(tmp_path / "export.jsonl")
    assert export.read_text(encoding="utf-8").count("\n") == 1
