"""P281-1 仓外信任锚测试（AC-P281-1-04/05）。

锚的全部价值在于「被改一个字节就一定看得出来」，所以这里既验自锚的**不动点**
（写 → 验 → rc=0），也逐条验**破坏面**：改锚字节、改判据数学源码、把锚放进
引擎仓内，三种都必须被点名。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_evaluator as ce

REPO_ROOT = Path(__file__).resolve().parents[2]
ANCHOR_CLI = REPO_ROOT / "scripts" / "benchmark" / "phase281_anchor.py"
SCORING = REPO_ROOT / "orchestrator" / "benchmark" / "scoring.py"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    import os
    os.chmod(root, 0o700)
    return root


def test_ac_p281_1_04_anchor_self_digest(tmp_path: Path):
    """写锚 → 复算通过；改一个字节 → 点名 ``manifest_sha256``。"""
    root = _root(tmp_path)
    manifest = anchor_mod.write_anchor(root, written_by="test")
    path = anchor_mod.anchor_path(root)
    assert path.is_file()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert anchor_mod.verify_anchor(path) == []

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert anchor_mod.self_digest(doc) == manifest.manifest_sha256

    # 改 self_digest **之外**的字段最容易漏：这里改判据快照 sha（JSON 仍合法）
    doc["criteria"]["mujoco_contact_push_v1"]["sha256"] = "0" * 64
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    problems = anchor_mod.verify_anchor(path)
    assert any("manifest_sha256" in item for item in problems), problems


def test_ac_p281_1_05_scoring_digest_guarded(tmp_path: Path):
    """``scoring.py`` 的 fixture 副本被改 → ``self_check()`` 非空且点名 scoring。"""
    root = _root(tmp_path)
    anchor_mod.write_anchor(root, written_by="test")

    tampered = tmp_path / "scoring.py"
    tampered.write_bytes(SCORING.read_bytes() + b"\n# tampered\n")

    problems = anchor_mod.verify_anchor(anchor_mod.anchor_path(root),
                                        scoring_source=tampered)
    assert any("scoring_sha256" in item for item in problems), problems

    evaluator = ce.CandidateEvaluator(
        anchor=anchor_mod.load_anchor(anchor_mod.anchor_path(root)),
        root=root, scoring_source=tampered)
    self_problems = evaluator.self_check()
    assert any("scoring_sha256" in item for item in self_problems), self_problems

    # 真件在同一个 seam 下必须仍然通过（seam 不是「一律报错」的假阳性）
    assert anchor_mod.verify_anchor(anchor_mod.anchor_path(root),
                                    scoring_source=SCORING) == []


def test_anchor_inside_repo_is_rejected(tmp_path: Path):
    """锚路径落在引擎仓内即不合格（计划 §5.9 的负例）。"""
    inside = REPO_ROOT / ".omc" / "state" / "test_anchor_manifest.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    outside_root = _root(tmp_path)
    manifest = anchor_mod.write_anchor(outside_root, written_by="test")
    try:
        inside.write_text(
            json.dumps(manifest.to_json(), ensure_ascii=False), encoding="utf-8")
        problems = anchor_mod.verify_anchor(inside)
        assert any("仓内" in item for item in problems), problems
    finally:
        inside.unlink(missing_ok=True)


def test_anchor_cli_write_then_verify_rc(tmp_path: Path):
    """CLI 面：``--write`` → 0；``--verify`` → 0；改坏 → 1。"""
    root = _root(tmp_path)
    write = subprocess.run(
        [sys.executable, str(ANCHOR_CLI), "--root", str(root), "--write"],
        capture_output=True, text=True, check=False)
    assert write.returncode == 0, write.stderr
    ok = subprocess.run(
        [sys.executable, str(ANCHOR_CLI), "--root", str(root), "--verify"],
        capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr

    path = anchor_mod.anchor_path(root)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["scoring_sha256"] = "0" * 64
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, str(ANCHOR_CLI), "--root", str(root), "--verify"],
        capture_output=True, text=True, check=False)
    assert bad.returncode == 1
    assert "manifest_sha256" in bad.stderr, bad.stderr


def test_load_anchor_refuses_tampered_self_digest(tmp_path: Path):
    """``load_anchor`` 自身也 fail-closed（不靠调用方记得先 verify）。"""
    root = _root(tmp_path)
    anchor_mod.write_anchor(root, written_by="test")
    path = anchor_mod.anchor_path(root)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["written_by"] = "someone_else"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(anchor_mod.AnchorError):
        anchor_mod.load_anchor(path)
