#!/usr/bin/env python3
"""能力矩阵 Task7 锁内适配测试（2026-10-15 用户书面批准「[实践]证据扩展」）。

零付费、零联网：
- 真实矩阵：校验器子进程通过；sapien 已以 coverage=phase1_probed 进目录，
  6 条 [实践] fact 全部回指仓内证据索引；bullet 仍 absent 不进目录；
- 证据互斥规则：[实践]↔repo_relpath、二手等级↔kb_relpath，存在性/越界
  硬校验（check_fact_evidence 纯函数，tmp KB 注入）；
- validate() 装配：最小 tmp 矩阵证明 [实践] 路径被接受、m7 零命中守卫未削弱。
KB 只读仓一个文件都不写（tmp KB 是 tmp_path 下空 git 仓）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from memory_flywheel.scripts import validate_capability_matrix as vcm

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "memory_flywheel" / "capability_matrix.yaml"
SAPPIEN_MD = "memory_flywheel/capability_evidence/sapien_phase1_probe.md"


def _real_matrix() -> dict:
    return yaml.safe_load(MATRIX.read_text(encoding="utf-8"))


def _sapien() -> dict:
    return next(e for e in _real_matrix()["engines"] if e["slug"] == "sapien")


# ---------------------------------------------------------------------------
# 真实矩阵：校验器 / 目录 / 证据出处
# ---------------------------------------------------------------------------

def test_01_validator_subprocess_passes_real_matrix():
    proc = subprocess.run(
        [sys.executable, "-m", "memory_flywheel.scripts.validate_capability_matrix"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[matrix OK]" in proc.stdout


def test_02_sapien_promoted_practice_only_bullet_still_absent():
    from orchestrator.direction.setter import load_engine_catalog
    sap = _sapien()
    assert sap["coverage"] == "phase1_probed"
    facts = sap["facts"]
    assert len(facts) == 6
    for f in facts:
        ev = f["evidence"]
        assert ev["level"] == "[实践]"
        assert ev["repo_relpath"] == SAPPIEN_MD
        assert not ev.get("kb_relpath"), "一手证据不得伪装 KB 引用"
        assert ev["locator"].startswith("§")
    # 仓内证据索引真实存在
    assert (REPO_ROOT / SAPPIEN_MD).is_file()
    catalog = load_engine_catalog()
    assert "sapien" in catalog
    assert catalog["sapien"]["coverage"] == "phase1_probed"
    assert catalog["sapien"]["facts"] == 6
    assert "bullet" not in catalog, "KB 零命中的 bullet 必须仍被挡在目录外"


def test_03_practice_md_contains_real_artifact_anchors():
    """证据索引必须指向真实存在的一手 JSON/视频产物（防纸面证据）。"""
    runs = Path("/mnt/artifacts/simpraxis/runs/phase1/task7_track1")
    md = (REPO_ROOT / SAPPIEN_MD).read_text(encoding="utf-8")
    for rel in ("probe/maniskill_render.json",
                "probe/pushcube_forces_gpu.json",
                "run_mech_verify_sapien_1/manifest.json",
                "run_smoke_sapien_1/manifest.json"):
        assert rel in md, f"证据索引缺原始产物锚点 {rel}"
        assert (runs / rel).is_file(), f"一手产物不存在：{rel}"
    # 关键实测数值必须在索引中（防改写口径）
    for needle in ("7.6e-6", "3.36s", "0.2276", "0.721", "5.2982",
                   "get_contacts", "mu=2.0", "0 付费"):
        assert needle in md, f"证据索引缺数值/事实 {needle}"
    manifest = json.loads((runs / "run_mech_verify_sapien_1/manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["engine"] == "sapien" and manifest["env_success_end"] is True
    assert manifest["max_penetration_m"] == 7.6e-6


# ---------------------------------------------------------------------------
# check_fact_evidence：两路证据互斥 + 存在性 + 越界
# ---------------------------------------------------------------------------

def test_04_practice_level_rules(tmp_path):
    kb = tmp_path / "kb"
    (kb / "knowledge").mkdir(parents=True)
    f = vcm.check_fact_evidence
    # 正向：真实仓内证据索引
    assert f({"level": "[实践]", "repo_relpath": SAPPIEN_MD,
              "locator": "§1"}, kb_root=kb) is None
    # [实践] 禁止配 kb_relpath
    assert "禁止配 kb_relpath" in f(
        {"level": "[实践]", "repo_relpath": SAPPIEN_MD,
         "kb_relpath": "knowledge/x.md", "locator": "§1"}, kb_root=kb)
    # [实践] 缺 repo_relpath
    assert "repo_relpath" in f({"level": "[实践]", "locator": "§1"}, kb_root=kb)
    # [实践] 文件不存在
    assert "证据文件不存在" in f(
        {"level": "[实践]", "repo_relpath": "memory_flywheel/nope.md",
         "locator": "§1"}, kb_root=kb)
    # [实践] 越界（/etc/passwd 在仓外且真实存在，专测越界而非缺文件）
    assert "越出仓库根" in f(
        {"level": "[实践]", "repo_relpath": "../../../../etc/passwd",
         "locator": "§1"}, kb_root=kb)


def test_05_kb_level_rules(tmp_path):
    kb = tmp_path / "kb"
    md = kb / "knowledge" / "proj" / "bg.md"
    md.parent.mkdir(parents=True)
    md.write_text("# bg", encoding="utf-8")
    f = vcm.check_fact_evidence
    # 正向：KB 二手等级只认真实 KB 文件
    assert f({"level": "[CODE]", "kb_relpath": "knowledge/proj/bg.md",
              "locator": "L1"}, kb_root=kb) is None
    # 二手等级禁止配 repo_relpath
    assert "禁止配 repo_relpath" in f(
        {"level": "[CODE]", "kb_relpath": "knowledge/proj/bg.md",
         "repo_relpath": SAPPIEN_MD, "locator": "L1"}, kb_root=kb)
    # 缺 kb_relpath
    assert "kb_relpath" in f({"level": "[CODE]", "locator": "L1"}, kb_root=kb)
    # KB 文件不存在
    assert "证据文件不存在" in f(
        {"level": "[PAPER]", "kb_relpath": "knowledge/missing.md",
         "locator": "L1"}, kb_root=kb)


# ---------------------------------------------------------------------------
# validate() 装配：tmp KB(git) + 最小矩阵
# ---------------------------------------------------------------------------

def _git_kb(tmp_path: Path, *, sapien_mention: str = "") -> Path:
    kb = tmp_path / "kb"
    (kb / "knowledge").mkdir(parents=True)
    if sapien_mention:
        (kb / "knowledge" / "x.md").write_text(sapien_mention, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=kb, check=True)
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "kb"], cwd=kb, check=True)
    return kb


def _minimal_matrix(kb: Path, *, sapien_fact: dict, bullet_absent: bool = True,
                    mention_sapien_gap: bool = True) -> Path:
    head = subprocess.run(["git", "-C", str(kb), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    bullet = {"slug": "bullet", "display_name": "Bullet", "kind": "k",
              "coverage": "absent", "facts": [],
              "gaps": ["KB 零命中：未提及；待 Phase1 补"]}
    m = {"meta": {"kb_commit": head}, "engines": [
        {"slug": "sapien", "display_name": "SAPIEN", "kind": "k",
         "coverage": "phase1_probed", "facts": [sapien_fact], "gaps": []},
        bullet,
    ]}
    p = kb.parent / "matrix.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    return p


def test_06_validate_accepts_practice_fact_and_m7_guard_intact(tmp_path):
    kb = _git_kb(tmp_path)
    good = {"claim": "c", "evidence": {"level": "[实践]",
                                       "repo_relpath": SAPPIEN_MD,
                                       "locator": "§1"}}
    p = _minimal_matrix(kb, sapien_fact=good)
    errs = vcm.validate(p, kb)
    # 最小矩阵必然报引擎集合/覆盖度下限，但不允许出现 sapien 证据类错误
    assert not any("sapien" in e and ("实践" in e or "证据" in e) for e in errs), errs

    # m7 守卫未被削弱：absent bullet 但 KB 出现词边界 bullet → 报错
    kb2_dir = tmp_path / "kb2"
    (kb2_dir / "knowledge").mkdir(parents=True)
    (kb2_dir / "knowledge" / "y.md").write_text("bullet engine", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=kb2_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=kb2_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "kb"], cwd=kb2_dir, check=True)
    head2 = subprocess.run(["git", "-C", str(kb2_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    m2 = {"meta": {"kb_commit": head2}, "engines": [
        {"slug": "bullet", "display_name": "Bullet", "kind": "k",
         "coverage": "absent", "facts": [],
         "gaps": ["零命中：未提及；待 Phase1 补"]}]}
    p2 = tmp_path / "m2.yaml"
    p2.write_text(yaml.safe_dump(m2, allow_unicode=True), encoding="utf-8")
    errs2 = vcm.validate(p2, kb2_dir)
    assert any("bullet" in e and "命中" in e for e in errs2), errs2


def test_07_validate_rejects_practice_fact_with_kb_relpath(tmp_path):
    kb = _git_kb(tmp_path)
    (kb / "knowledge").mkdir(exist_ok=True)
    (kb / "knowledge" / "x.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], cwd=kb, check=True)
    head = subprocess.run(["git", "-C", str(kb), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    bad = {"claim": "c", "evidence": {"level": "[实践]",
                                      "repo_relpath": SAPPIEN_MD,
                                      "kb_relpath": "knowledge/x.md",
                                      "locator": "§1"}}
    m = {"meta": {"kb_commit": head}, "engines": [
        {"slug": "sapien", "display_name": "SAPIEN", "kind": "k",
         "coverage": "phase1_probed", "facts": [bad], "gaps": []}]}
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    errs = vcm.validate(p, kb)
    assert any("sapien" in e and "禁止配 kb_relpath" in e for e in errs), errs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
