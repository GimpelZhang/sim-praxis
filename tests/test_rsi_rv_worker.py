"""RV-03/RV-04：实际 worker 的页读取与隔离探针（同一 executable/image/mount）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_worker as W
from tests.fixtures import rv_synthetic as RS


def _public(tmp_path: Path) -> "tuple[Path, dict]":
    _, source_map = RS.build_rv_source_roots(tmp_path)
    manifest = RV.build_source_manifest(source_map)
    root = tmp_path / "round"
    RV.build_public_package(root, manifest)
    return root, manifest


def test_policy_is_cpu_only_and_pinned():
    policy = W.WorkerPolicy()
    doc = policy.to_dict()
    assert doc["image"].startswith("sim-praxis/sandbox@sha256:")
    assert "--network" in doc["argv_prefix"]
    assert "none" in doc["argv_prefix"]
    assert "--read-only" in doc["argv_prefix"]
    assert "--cap-drop" in doc["argv_prefix"]
    assert "ALL" in doc["argv_prefix"]
    assert "docker.sock" not in str(doc)
    assert "/var/run/docker.sock" not in str(doc)
    # reader 只允许“用 socket 证明网络不可达”，不得有执行外部命令的面。
    assert "subprocess" not in W.READER_SOURCE
    assert "os.system" not in W.READER_SOURCE
    assert "ctypes" not in W.READER_SOURCE


def test_reader_source_sha_is_the_policy_sha():
    policy = W.WorkerPolicy()
    assert policy.reader_sha256 == W.READER_SHA256
    assert W.READER_SHA256 == RV.sha_bytes(W.READER_SOURCE.encode("utf-8"))


def test_container_read_page_matches_g2_semantics(tmp_path):
    root, manifest = _public(tmp_path)
    data = (root / "public" / "A" / "analysis_output.json").read_bytes()
    sha = RV.sha_bytes(data)
    reader = W.ContainerReader(root=root, study="A",
                               policy=W.WorkerPolicy(),
                               publication=root / "public" / "A")
    registry = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json",
        "relative_path": "analysis/A/output.json", "sha256": sha,
        "bytes": len(data), "access_mode": "read_result_artifact"}])
    reference = G2.read_page(registry=registry,
                             read_bytes=lambda rel: data,
                             artifact_id="analysis/output.json",
                             output_sha256=sha, cursor=0, page_bytes=8192)
    page = reader.read_page(artifact_id="analysis/output.json",
                            relative_path="public/A/analysis_output.json",
                            expected_sha256=sha, cursor=0, page_bytes=8192)
    for key in ("artifact_sha256", "offset", "n_bytes", "total_bytes",
                "page_sha256", "encoding", "data", "next_cursor"):
        assert page[key] == reference[key], key
    evidence = reader.evidence()
    assert evidence["n_reads"] == 1
    record = evidence["records"][0]
    assert record["container_id"]
    assert record["host_pid"] > 0
    assert record["rc"] == 0
    assert record["stdout_sha256"]


def test_container_read_rejects_wrong_sha(tmp_path):
    root, _ = _public(tmp_path)
    reader = W.ContainerReader(root=root, study="A",
                               policy=W.WorkerPolicy(),
                               publication=root / "public" / "A")
    with pytest.raises(W.WorkerError) as exc:
        reader.read_page(artifact_id="analysis/output.json",
                         relative_path="public/A/analysis_output.json",
                         expected_sha256="0" * 64, cursor=0, page_bytes=4096)
    assert exc.value.code == "WORKER_REJECTED"


def test_isolation_probe_and_negative_control(tmp_path):
    root, _ = _public(tmp_path)
    report = W.run_isolation(root, policy=W.WorkerPolicy())
    assert report["ok"] is True
    candidate = report["summary"]["candidate"]
    assert candidate["forbidden_read_denied"] is True
    assert candidate["host_private_denied"] is True
    assert candidate["write_data_denied"] is True
    assert candidate["write_out_allowed"] is True
    assert candidate["network_denied"] is True
    assert candidate["canary_not_leaked"] is True
    assert candidate["non_root"] is True
    assert report["summary"]["negative_control"]["negative_control_detected"] is True
