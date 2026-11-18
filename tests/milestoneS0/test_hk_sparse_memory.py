"""S0-4a：m_t 白名单、归档清零、闭源路径与「不入 KB」结构断言。"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.hk import sparse_memory
from orchestrator.hk.sparse_memory import SparseMemory, SparseMemoryError

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_whitelist_rejects_unknown_key():
    memory = SparseMemory()
    with pytest.raises(SparseMemoryError) as excinfo:
        memory.put("workspace_paths", ["/tmp/x"])
    assert "白名单" in str(excinfo.value)
    memory.put("api_paths", {"mujoco": "mujoco.MjModel.from_xml_path"})
    assert memory.get("api_paths")["mujoco"].startswith("mujoco.")


def test_bytes_values_rejected_in_favour_of_blob_refs():
    memory = SparseMemory()
    with pytest.raises(SparseMemoryError):
        memory.put("defects", b"\x00binary")


def test_archive_clears_memory_and_writes_private_files(tmp_path, monkeypatch):
    closed = tmp_path / "closed"
    monkeypatch.setenv("SIMPRAXIS_S0_CLOSED_ROOT", str(closed))
    memory = SparseMemory(run_dir=tmp_path / "run")
    memory.put("fingerprints", {"mujoco": "3.15.0"})
    memory.put("block_effects", {"b01": 0.01})
    target = memory.archive()
    assert target.exists()
    assert target.parent.is_dir()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert len(memory) == 0  # 归档即清零
    assert memory.archived_to == target
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema"] == "simpraxis.s0.sparse_state.v1"
    assert document["state"]["fingerprints"]["mujoco"] == "3.15.0"
    # 归档在闭源数据层路径约定下
    assert str(closed) in str(target)


def test_archive_default_root_is_closed_data_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_S0_CLOSED_ROOT", str(tmp_path / "closed"))
    memory = SparseMemory()
    memory.put("api_paths", {"x": "y"})
    target = memory.archive()
    assert target.parent.name.startswith("run_")
    assert (tmp_path / "closed" / "m_t") in target.parents


def test_save_and_load_roundtrip_and_tamper_detection(tmp_path):
    memory = SparseMemory()
    memory.put("defects", {"d1": "non-manifold"})
    path = memory.save(tmp_path / "sparse_state.json")
    again = SparseMemory.load(path)
    assert again.get("defects") == {"d1": "non-manifold"}
    document = json.loads(path.read_text(encoding="utf-8"))
    document["state"]["defects"]["d1"] = "silently-fixed"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SparseMemoryError):
        SparseMemory.load(path)


def test_sparse_memory_never_touches_kb_tables():
    import ast

    source = (REPO_ROOT / "orchestrator" / "hk" / "sparse_memory.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("lancedb", "memory_flywheel"):
        assert forbidden not in imported, imported
    # 默认根落在闭源数据层（不是 KB 数据层）
    assert str(sparse_memory.closed_root()).startswith(
        os.environ.get("SIMPRAXIS_S0_CLOSED_ROOT", "/mnt/artifacts/simpraxis/s0_closed"))
