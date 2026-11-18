"""S0-8：ProductBundle v1（product tier）+ 防泄两负控 + core/extended 行为不变。"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_KB = REPO_ROOT / "scripts" / "kb"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"s0_{name[:-3]}", SCRIPTS_KB / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import lancedb

    bk = _load("bundle_kb_asset.py")
    vb = _load("verify_bundle.py")
    monkeypatch.setattr(bk, "select_embedding", lambda: {"model_name": "test-model",
                                                         "dim": 8})
    uri = tmp_path / "data" / "lancedb"
    db = lancedb.connect(str(uri))

    def rows(tag, n=2):
        return [{"id": f"{tag}{i}", "vector": [0.0] * 8} for i in range(n)]

    db.create_table("kb_chunks", data=rows("k", 3), mode="create")
    db.create_table("phase1_learnings", data=rows("l", 2), mode="create")

    kb = tmp_path / "kbgit"
    kb.mkdir()
    (kb / "knowledge").mkdir()
    (kb / "knowledge" / "guide.md").write_text("# 公开知识\n", encoding="utf-8")
    (kb / "solutions" / "track1_tools").mkdir(parents=True)
    (kb / "solutions" / "track1_tools" / "run.py").write_text("print('hi')\n",
                                                              encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=kb, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], cwd=kb, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "kb"], cwd=kb, check=True)

    closed = tmp_path / "closed"
    (closed / "research_memory").mkdir(parents=True)
    (closed / "research_memory" / "rm.jsonl").write_text('{"lesson_id":"L1"}\n',
                                                         encoding="utf-8")
    (closed / "archives").mkdir()
    (closed / "promoted_params").mkdir()

    out = tmp_path / "bundles"
    return {"bk": bk, "vb": vb, "uri": uri, "kb": kb, "closed": closed, "out": out,
            "tmp": tmp_path}


def _build(world, tier):
    return world["bk"].build_bundle(tier=tier, uri=world["uri"],
                                    out_root=world["out"], kb_root=world["kb"],
                                    sim_root=REPO_ROOT,
                                    closed_root=world["closed"])


def test_product_bundle_roundtrip(world):
    bundle = _build(world, "product")
    manifest = json.loads((bundle / "bundle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tier"] == "product"
    assert sorted(manifest["closed_tiers"]) == ["archives", "promoted_params",
                                                "research_memory"]
    assert sorted(manifest["content"]) == ["knowledge", "solutions"]
    assert len(manifest["closed_tiers"]["research_memory"]["files"]) == 1
    assert manifest["closed_tiers"]["archives"]["files"] == []
    assert world["vb"].verify_bundle(bundle, sim_root=REPO_ROOT) == []


def test_core_and_extended_manifests_unchanged(world):
    for tier in ("core", "extended"):
        bundle = _build(world, tier)
        manifest = json.loads((bundle / "bundle_manifest.json").read_text(
            encoding="utf-8"))
        for new_key in ("closed_tiers", "content", "closed_root"):
            assert new_key not in manifest
        assert world["vb"].verify_bundle(bundle, sim_root=REPO_ROOT) == []


def test_content_leak_by_path_is_refused(world):
    leak_dir = world["kb"] / "knowledge" / "research_memory"
    leak_dir.mkdir()
    (leak_dir / "secret.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    with pytest.raises(world["bk"].BundleError) as excinfo:
        _build(world, "product")
    assert "闭源 tier 名" in str(excinfo.value) or "闭源 tier 路径" in str(excinfo.value)


def test_content_leak_by_identical_bytes_is_refused(world):
    # 换路径但字节与闭源文件完全相同 ⇒ sha256 撞库拒绝
    shutil.copyfile(world["closed"] / "research_memory" / "rm.jsonl",
                    world["kb"] / "knowledge" / "copied.jsonl")
    with pytest.raises(world["bk"].BundleError) as excinfo:
        _build(world, "product")
    assert "sha256 相同" in str(excinfo.value)


def test_closed_tier_tamper_detected(world):
    bundle = _build(world, "product")
    victim = bundle / "closed" / "research_memory" / "rm.jsonl"
    data = bytearray(victim.read_bytes())
    data[0] ^= 0xFF
    victim.write_bytes(bytes(data))
    problems = world["vb"].verify_bundle(bundle, sim_root=REPO_ROOT)
    assert any("sha256 不符" in p for p in problems), problems


def test_push_surface_scan_negative_control(world, tmp_path):
    ps = _load("check_push_surface.py")
    bundle = _build(world, "product")
    index = ps.closed_index(bundle)
    assert index

    clean = tmp_path / "staging_clean"
    clean.mkdir()
    (clean / "README.md").write_text("public\n", encoding="utf-8")
    assert ps.scan(clean, index) == []

    dirty = tmp_path / "staging_dirty"
    (dirty / "deep").mkdir(parents=True)
    shutil.copyfile(bundle / "closed" / "research_memory" / "rm.jsonl",
                    dirty / "deep" / "innocent.jsonl")
    leaked = ps.scan(dirty, index)
    assert leaked and leaked[0]["reasons"] == ["closed-bytes-sha256"]

    path_leak = tmp_path / "staging_path"
    (path_leak / "archives").mkdir(parents=True)
    (path_leak / "archives" / "note.txt").write_text("x\n", encoding="utf-8")
    assert ps.scan(path_leak, index)[0]["reasons"] == ["closed-tier-path"]
