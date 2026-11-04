"""Phase 2.7 Task 2 机判：可执行技能库与装配引擎（Plan §T2.6 / AC-T2-1..6）。

判据只读**清单 + 磁盘 + git + 活表**，不 import 技能代码（引擎侧红线，
与 AC-T2-4 同一条纪律）。所有契约级检查（sha256 / 篡改 / 未登记名 / entry
与源码一致 / 暂存权限）都在默认套件里跑；只有需要活库的两项（活表 evidence
前缀、四表行数）打 ``kb_integration``，收尾显式跑。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator.agentic.tools.run import build_docker_create_argv
from orchestrator.common.runtime_paths import phase27_root
from orchestrator.skills.registry import (
    SKILLS_SCHEMA_ID,
    PACKAGE_NAME,
    SkillEntry,
    SkillsError,
    env_spec_skills_block,
    get_skill,
    list_skills,
    load_skills_manifest,
    manifest_sha12,
    skills_root,
    staged_path_for,
)
from scripts.phase27 import stage_skills

from tests.kb_support import kb_root as _kb_root, require_kb_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = phase27_root() / "task2_skills"
TWO_EXCLUSIONS = '-m "not paid and not kb_integration"'

# Task 2 首灌四件 + Task 4 依 BR-4/D4-4 追加的 wall_b.instruments（KB cd5cdad）。
# 断言形态不变：仍是「清单逐名与磁盘逐件 sha 双向相等」，追加只改基线不改口径
# （Complete §5.0b #14：Task 4 登记第五件技能后，Task 2 的两条钉死基线随之刷新）。
# Task 6（2026-11-04，KB b5de4bd 回收 04_libero_vla）再追加第六件
# libero.vla_episodes——同样是「刷新基线、不放水」：全等断言形态不变。
EXPECTED_SKILLS = (
    "common.math_3d",
    "libero.env_boot",
    "libero.vla_episodes",
    "maniskill.pushcube",
    "mujoco.contact_push",
    "wall_b.instruments",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=False)


# --------------------------------------------------------- AC-T2-1 契约


def test_ac_t2_1_manifest_fail_closed(tmp_path, monkeypatch):
    """manifest 逐件 sha 与磁盘相等；篡改/未登记名/伪造 entry 一律 SkillsError。"""
    root = skills_root()
    manifest = load_skills_manifest(root, verify_provenance_=True)
    assert manifest["schema"] == SKILLS_SCHEMA_ID
    assert [s["name"] for s in manifest["skills"]] == list(EXPECTED_SKILLS)

    # ① 逐件 sha256 与磁盘相等（load 已经复算过，这里再独立复核一遍）
    for item in manifest["skills"] + manifest["packages"]:
        assert _sha(root.parent / item["relpath"]) == item["sha256"], item["relpath"]

    # ② provenance 逐件可核：extracted 的 source 必须在 kb_commit 里真实存在
    for item in manifest["skills"]:
        prov = item["provenance"]
        if prov["kind"] != "extracted":
            assert prov["source"] is None
            continue
        rc = _git("cat-file", "-e", f"{prov['kb_commit']}:{prov['source']}",
                  cwd=root.parent)
        assert rc.returncode == 0, (prov, rc.stderr)
        assert prov["evidence"].startswith("phase1_learnings:")

    # ③ 篡改任一字节 → SkillsError（复制到 tmp，不动活仓）
    tampered = tmp_path / "kb"
    shutil.copytree(root, tampered / "skills")
    monkeypatch.delenv("SIMPRAXIS_SKILLS_ROOT", raising=False)
    target = tampered / "skills/common/math_3d.py"
    target.write_bytes(target.read_bytes() + b"\n# tampered\n")
    with pytest.raises(SkillsError, match="sha256 不符"):
        load_skills_manifest(tampered / "skills")

    # ④ entry 与源码不符 → SkillsError（非放水的「清单自述」检查）
    manifest_path = tampered / "skills/manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["skills"][0]["entry"] = ["no_such_function"]
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True),
                             encoding="utf-8")
    # 先把被篡改的技能文件还原，确保触发的是 entry 而不是 sha
    target.write_bytes((root.parent / "skills/common/math_3d.py").read_bytes())
    with pytest.raises(SkillsError):
        load_skills_manifest(tampered / "skills")

    # ⑤ 未登记名 → SkillsError（不返回 None）
    with pytest.raises(SkillsError, match="未登记"):
        get_skill("common.not_a_skill")
    names = [e.name for e in list_skills(root)]
    assert names == sorted(EXPECTED_SKILLS)


@pytest.mark.kb_integration
def test_ac_t2_1b_evidence_prefix_hits_live_table(monkeypatch):
    """evidence 的 phase1_learnings 前缀必须在**活表**命中恰好 1 行（0/≥2 即错）。"""
    require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    import lancedb

    from memory_flywheel import kb_pins

    db = lancedb.connect(kb_pins.default_lancedb_uri())
    ids = [row["id"] for row in
           db.open_table("phase1_learnings").to_arrow().to_pylist()]
    manifest = load_skills_manifest(skills_root())
    checked = 0
    for item in manifest["skills"]:
        evidence = item["provenance"]["evidence"]
        if not evidence.startswith("phase1_learnings:"):
            continue
        prefix = evidence.split(":", 1)[1]
        hits = [i for i in ids if i.startswith(prefix)]
        if len(hits) != 1:
            raise SkillsError(
                f"{item['name']} 的 evidence 前缀 {prefix!r} 在活表命中 "
                f"{len(hits)} 行（必须恰好 1 行）")
        assert len(hits[0]) == 32, hits[0]
        checked += 1
    assert checked == 3, "三个 extracted 技能都必须钉活表 evidence"


# ------------------------------------------------------------ AC-T2-2


def test_ac_t2_2_each_skill_has_tests():
    """一技能一测试文件（不许巨型聚合测试），且 KB 侧单测 100% 通过。"""
    manifest = load_skills_manifest(skills_root())
    skills = manifest["skills"]
    tests = [s["tests"] for s in skills]
    assert len(tests) == len(set(tests)) == len(skills), "测试文件必须一技能一个且互异"
    for item in skills:
        test_path = skills_root().parent / item["tests"]
        assert test_path.is_file(), item["tests"]
        text = test_path.read_text(encoding="utf-8")
        stem = Path(item["relpath"]).stem
        assert f"from {stem} import" in text, (
            f"{item['tests']} 必须直接 import {stem}（不能只测别的）")
        assert _sha(test_path) == item["tests_sha256"]

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(skills_root()), "-q"],
        capture_output=True, text=True, cwd=str(_kb_root() if _kb_root().is_dir()
                                                else skills_root().parent),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "failed" not in proc.stdout.splitlines()[-1]


# ------------------------------------------------------------ AC-T2-3


def test_ac_t2_3_stage_readonly_copy(tmp_path, monkeypatch):
    """staged 副本逐件 sha 与清单相等；目录 0700、文件 0600、无符号链接。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE27_ROOT", str(tmp_path / "p27"))
    monkeypatch.delenv("SIMPRAXIS_SKILLS_ROOT", raising=False)
    report = stage_skills.stage(tmp_path / "evidence")
    stage_root = Path(report["stage_root"])
    assert stage_root.parent.name == "skills_stage"
    assert stage_root.name == manifest_sha12()
    assert (stage_root / PACKAGE_NAME).is_dir()

    manifest = load_skills_manifest(skills_root())
    expected = {staged_path_for(i["relpath"], stage_root): i["sha256"]
                for i in manifest["skills"] + manifest["packages"]}
    assert len(expected) == len(manifest["skills"]) + len(manifest["packages"])
    for path, want in expected.items():
        assert path.is_file(), path
        assert _sha(path) == want
    # 落盘报告与磁盘一致（键必须用相对路径：五个 __init__.py 同名不同件）
    report_rows = {r["staged"]: r["sha256"] for r in report["files"]}
    assert len(report_rows) == len(report["files"]) == len(expected)
    for path, want in expected.items():
        assert report_rows[str(path.relative_to(stage_root))] == want

    for entry in stage_root.rglob("*"):
        assert not entry.is_symlink(), f"staged 树含符号链接：{entry}"
        mode = entry.stat().st_mode
        if stat.S_ISDIR(mode):
            assert stat.S_IMODE(mode) == 0o700, oct(stat.S_IMODE(mode))
        else:
            assert stat.S_IMODE(mode) == 0o600, oct(stat.S_IMODE(mode))
    assert stat.S_IMODE(stage_root.stat().st_mode) == 0o700

    # run.py 的 /skills 挂载白名单必须接受这个暂存目录，并生成 :ro 挂载
    argv = build_docker_create_argv(
        image="sim-praxis/x@sha256:" + "0" * 64, argv=["true"],
        workdir=tmp_path, extra_ro_mounts=((str(stage_root), "/skills"),))
    joined = " ".join(argv)
    assert f"-v {stage_root}:/skills:ro" in joined

    # 重入幂等：再跑一次不报错、sha 不变
    again = stage_skills.stage(None)
    assert again["stage_root"] == str(stage_root)
    assert {r["sha256"] for r in again["files"]} == set(expected.values())


# ------------------------------------------------------------ AC-T2-4


def _imports(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
    return out


def test_ac_t2_4_engine_never_imports_skills():
    """引擎仓任何地方都不得 import 内容层技能包（AST 扫描；registry 只读清单）。"""
    forbidden = ("skills", "simpraxis_skills")
    offenders = []
    for base in (REPO_ROOT / "orchestrator", REPO_ROOT / "scripts",
                 REPO_ROOT / "sandbox"):
        for path in base.rglob("*.py"):
            for lineno, module in _imports(path):
                head = module.split(".")[0]
                if head == "simpraxis_skills" or module in forbidden:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} "
                                     f"import {module}")
    assert not offenders, f"引擎层不得 import 技能代码：{offenders}"

    # registry.py 只读清单：源码里不得出现执行/导入内容层代码的手段
    src = (REPO_ROOT / "orchestrator/skills/registry.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name not in ("import_module", "exec", "eval", "__import__"), \
                f"registry 不得动态执行/导入内容层代码：{name}"
    # 技能模块 docstring 用 ast 解析（不执行），磁盘只经 yaml/hashlib 读
    assert "ast.parse" in src
    assert "subprocess" in src          # 仅用于 provenance 的 git cat-file


# ------------------------------------------------------------ AC-T2-5


@pytest.mark.kb_integration
def test_ac_t2_5_four_tables_unchanged(monkeypatch):
    """本 Task 不写任何表：四表行数恒 1542 / 44 / 106 / 1。"""
    require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    import lancedb

    from memory_flywheel import kb_pins
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.phase2.evolution_store import EvolutionStore

    db = lancedb.connect(kb_pins.default_lancedb_uri())
    assert db.open_table("kb_chunks").count_rows() == 1542
    # 锁内适配（Task 6 回收窗口 2026-11-04）：Task 2 自身的「不写表」语义不变
    # ——本测仍只读；94→106 是 Task 6 回收 04_libero_vla（12 块）后的活表基线
    # 纯事实前进。断言形态不变（仍是精确等值，不是 >=）。
    assert db.open_table("solution_chunks").count_rows() == 106
    assert LearningStore().count() == 44
    assert EvolutionStore().count() == 1


# ------------------------------------------------------------ AC-T2-6


def test_ac_t2_6_no_auto_push():
    """KB 侧的本次提交**未推送**：origin/main 上没有 skills/，且本地领先。"""
    kb = _kb_root()
    assert kb.is_dir(), f"KB 仓不存在：{kb}"
    # ① 基线：HEAD 必须是迁移提交 c5712e9f 的后代
    base = "c5712e9f73fc17cd2649cae359aad216e8f3bd07"
    assert _git("merge-base", "--is-ancestor", base, "HEAD", cwd=kb).returncode == 0
    # ② origin/main 上不存在技能清单（=从未 push 过本 Task 的产物）
    assert _git("cat-file", "-e", "origin/main:skills/manifest.yaml",
                cwd=kb).returncode != 0, "skills/ 已被推到 origin/main（违反 STOP-4）"
    # ③ 本地确有一个未推送提交引入了 skills/manifest.yaml
    revs = _git("rev-list", "origin/main..HEAD", cwd=kb).stdout.split()
    assert revs, "本地没有未推送提交（KB 侧尚未提交？）"
    added = _git("log", "--format=%H", "--name-status", "origin/main..HEAD",
                 cwd=kb).stdout
    assert "A\tskills/manifest.yaml" in added
    # ④ 工作树干净（未提交的临时改动不许留在 KB 里）
    assert _git("status", "--porcelain", cwd=kb).stdout.strip() == ""


# ---------------------------------------------------- 附加机判（非 AC）


def test_env_spec_skills_block_is_deterministic_and_complete():
    block = env_spec_skills_block()
    assert block == env_spec_skills_block()
    for name in EXPECTED_SKILLS:
        assert name in block
    for entry in list_skills():
        assert isinstance(entry, SkillEntry)
        assert entry.staged_path is None
        domain, _, _ = entry.name.partition(".")
        assert entry.relpath.startswith(f"skills/{domain}/")
        assert entry.doc and "\n" not in entry.doc
        assert entry.requires.get("numpy") == "==1.26.4"


def test_task2_evidence_layout():
    """T2.7 证据树：五件在位且可解析。"""
    expected = ("manifest_snapshot.yaml", "stage_report.json", "tests_math_3d.log",
                "registry_probe.json", "kb_commit.txt")
    for name in expected:
        assert (EVIDENCE / name).is_file(), f"缺证据件：{EVIDENCE / name}"
    snapshot = yaml.safe_load((EVIDENCE / "manifest_snapshot.yaml").read_text(
        encoding="utf-8"))
    assert snapshot["schema"] == SKILLS_SCHEMA_ID
    for item in snapshot["skills"]:
        assert _sha(skills_root().parent / item["relpath"]) == item["sha256"], (
            f"{item['name']} 的 sha 与证据快照不符（技能被改但未更新证据）")
    report = json.loads((EVIDENCE / "stage_report.json").read_text(encoding="utf-8"))
    assert report["schema"] == "simpraxis.skills_stage_report.v1"
    assert report["manifest_sha12"] == manifest_sha12()
    probe = json.loads((EVIDENCE / "registry_probe.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in probe["skills"]] == list(EXPECTED_SKILLS)
    kb_commit = (EVIDENCE / "kb_commit.txt").read_text(encoding="utf-8").strip()
    assert len(kb_commit) == 40
    assert _git("cat-file", "-e", f"{kb_commit}^{{commit}}",
                cwd=skills_root().parent).returncode == 0
    assert kb_commit in _git("rev-list", "origin/main..HEAD",
                             cwd=skills_root().parent).stdout
    # 单真值：收尾命令仍然只有两排除项（不得为本 Task 新增第三项）
    assert TWO_EXCLUSIONS == '-m "not paid and not kb_integration"'
