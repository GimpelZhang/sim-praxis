#!/usr/bin/env python3
"""Task0b 零付费九测（Phase_2_Plan_detailed.md 附录 A，逐条同名）。

引擎/内容/数据三层分离回归：
  1-4 纯引擎仓（合成 fixture/配置/git 事实，默认裸跑必过）；
  5-8 kb_integration（SIMPRAXIS_KB_INTEGRATION=1 + 活 KB/活数据层在场才执行，
     与 paid marker 同构的自动 skip，不是 test.skip 放水；活断言一条不弱）；
  9   tmp 合成资产包 core/extended 往返 + 篡改拒绝（默认必过），另在集成
     开关下追加活资产包 verify。
全部 0 付费 0 触网；活库嵌入一律 HF_HUB_OFFLINE=1 + /mnt 缓存 mpnet。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.solutions import SolutionError, load_manifest, resolve_solution
from tests.kb_support import kb_root as live_kb_root
from tests.kb_support import require_kb_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SOLUTIONS = REPO_ROOT / "tests" / "fixtures" / "solutions"
SCRIPTS_KB = REPO_ROOT / "scripts" / "kb"
OLD_LANCEDB = Path("/mnt/artifacts/simpraxis/lancedb")
LIVE_BUNDLES = Path("/mnt/artifacts/embodied-kb/data/bundles")

# Phase 1 迁出的六个解题脚本 basename（Q2 三件 + 其余三件）
SIX_BASENAMES = (
    "maniskill_pushcube.py", "mujoco_contact_push.py", "genesis_humanoid_dr.py",
    "collect_weather_lidar.py", "optics_model.py", "analyze_q2.py",
)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"task0b_{name[:-3]}", SCRIPTS_KB / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _fixture_solutions_root(monkeypatch):
    # 默认把内容层根钉到引擎仓合成 fixture，活 KB 测试自行显式覆盖。
    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT", str(FIXTURE_SOLUTIONS))


# ---------------------------------------------------------------------------
# 1. test_resolver_stages_fixture_solution
# ---------------------------------------------------------------------------

def test_resolver_stages_fixture_solution(tmp_path):
    """合成 fixture 方案经 resolver 解析并 stage：0700/0600 权限 + C4 名校验。"""
    manifest = resolve_solution("track1_tools/01_maniskill_pushcube")
    assert manifest.solution_id == "track1_tools/01_maniskill_pushcube"
    staged = manifest.stage(tmp_path / "work")

    assert staged.entrypoint.is_file()
    mode_dir = stat.S_IMODE(staged.workdir.stat().st_mode)
    assert mode_dir == 0o700, oct(mode_dir)
    mode_entry = stat.S_IMODE(staged.entrypoint.stat().st_mode)
    assert mode_entry == 0o700, oct(mode_entry)
    for rel, p in staged.files.items():
        assert p.is_file()
        if rel != manifest.entrypoint:
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, (rel, oct(mode_entry))

    # C4：files[] 出现路径逃逸（../）必须 fail-closed；目录须保持两级结构，
    # 否则先撞 solution_id/目录名一致性检查（保留原方案层级，隔离出 C4 这一项）
    bad = tmp_path / "track1_tools" / "01_maniskill_pushcube"
    shutil.copytree(
        FIXTURE_SOLUTIONS / "track1_tools" / "01_maniskill_pushcube", bad)
    raw = yaml.safe_load((bad / "manifest.yaml").read_text(encoding="utf-8"))
    raw["files"].append({"relpath": "../evil.py",
                         "sha256": "0" * 64})
    (bad / "manifest.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(SolutionError, match="逃逸|空段|relpath"):
        load_manifest(bad)


# ---------------------------------------------------------------------------
# 2. test_resolver_sha_mismatch_fail_closed
# ---------------------------------------------------------------------------

def test_resolver_sha_mismatch_fail_closed(tmp_path, monkeypatch):
    """改 1 字节即 SolutionError：不网络兜底、不就地生成。"""
    good = FIXTURE_SOLUTIONS / "track1_tools" / "02_mujoco_contact_push"
    bad = tmp_path / "track1_tools" / "02_mujoco_contact_push"
    shutil.copytree(good, bad)
    script = bad / "mujoco_contact_push.py"
    data = bytearray(script.read_bytes())
    data[100] ^= 0x01
    script.write_bytes(bytes(data))

    def _deny_network(*_a, **_kw):  # 任何真联网尝试都视为本测失败
        raise AssertionError("sha 不符时禁止触网兜底")

    import socket
    monkeypatch.setattr(socket.socket, "connect", _deny_network)

    with pytest.raises(SolutionError, match="sha256 不符"):
        load_manifest(bad)


# ---------------------------------------------------------------------------
# 3. test_kb_mount_readonly
# ---------------------------------------------------------------------------

def test_kb_mount_readonly():
    """compose 配置级断言：宿主 solutions 只读挂到容器 /kb。

    计划附 A：「配置级断言 + 可选 smoke」——容器实跑属 track 纪律外动作，
    本测只锁配置文本/结构（:ro 是 docker 单边只读挂载语义）。
    """
    compose = yaml.safe_load(
        (REPO_ROOT / "sandbox" / "docker-compose.yml").read_text(encoding="utf-8"))
    volumes = []
    for svc in compose.get("services", {}).values():
        volumes.extend(svc.get("volumes", []))
    ro = [str(v) for v in volumes if str(v).endswith(":/kb:ro")]
    assert len(ro) == 1, volumes
    # 源含 ${KB_ROOT:-...} 默认值语法，不能按 ":" split；去挂载后缀后断言
    source = ro[0][: -len(":/kb:ro")]
    assert source.endswith("/solutions"), ro
    # 同一服务必须把方案根 env 指到 /kb
    text = (REPO_ROOT / "sandbox" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SIMPRAXIS_SOLUTIONS_ROOT" in text and "/kb" in text


# ---------------------------------------------------------------------------
# 4. test_engine_repo_has_no_question_scripts
# ---------------------------------------------------------------------------

def test_engine_repo_has_no_question_scripts():
    """引擎仓不再携带解题脚本：git ls-files 白名单外零命中；sandbox/track* 已删。"""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    # 仅允许的同名落件：合成 fixture（非真实脚本）与契约级测试文件名碰撞
    allowed_prefixes = ("tests/fixtures/solutions/",)
    allowed_exact = {"tests/milestone1/test_q2_optics_model.py"}
    violations = []
    for rel in tracked:
        base = rel.rsplit("/", 1)[-1]
        if base in SIX_BASENAMES:
            if rel in allowed_exact or rel.startswith(allowed_prefixes):
                continue
            violations.append(rel)
    assert not violations, f"引擎仓仍跟踪解题脚本：{violations}"
    assert not (REPO_ROOT / "sandbox" / "track1").exists()
    assert not (REPO_ROOT / "sandbox" / "track2").exists()


# ---------------------------------------------------------------------------
# 5. test_knowledge_hash_set_baseline（kb_integration）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_knowledge_hash_set_baseline():
    kb = require_kb_integration()
    from memory_flywheel import kb_pins
    assert kb_pins.check_layered_pins(kb) == []
    # 基线集 32 md，Task0b 时追加登记为空
    assert len(kb_pins.KNOWLEDGE_BASELINE_FILES) == 32
    assert kb_pins.KNOWLEDGE_ADDITIONS == {}
    assert kb_pins.is_descendant(kb_pins.MIGRATION_KB_SHA, kb)


# ---------------------------------------------------------------------------
# 6. test_data_symlink_ignored（kb_integration）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_data_symlink_ignored():
    kb = require_kb_integration(data=True)
    link = kb / "data"
    assert link.is_symlink(), f"{link} 必须是软链（实体在 /mnt，不入库）"
    assert os.readlink(link).rstrip("/") == "/mnt/artifacts/embodied-kb/data"
    # 彩排实证：对软链本身 check-ignore（穿越软链的 data/lancedb pathspec 会 fatal）
    ci = subprocess.run(["git", "-C", str(kb), "check-ignore", "data"],
                        capture_output=True, text=True)
    assert ci.returncode == 0, "git check-ignore data 未命中（.gitignore 须为锚定 /data）"
    tracked = subprocess.run(["git", "-C", str(kb), "ls-files", "data"],
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked == [], f"data/ 出现被跟踪路径：{tracked}"


# ---------------------------------------------------------------------------
# 7. test_solution_chunks_recall_old_tables_frozen（kb_integration）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_solution_chunks_recall_old_tables_frozen(monkeypatch):
    kb = require_kb_integration(data=True)
    # 活嵌入断网：mpnet 只走 /mnt 缓存，0 下载（计划付费点=0）
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    from memory_flywheel.scripts.ingest_solutions import (
        TABLE_NAME, SolutionIngester)

    ing = SolutionIngester(kb_root=kb)
    if TABLE_NAME not in ing.db.table_names():
        pytest.skip("solution_chunks 尚未入库（先跑 "
                    "python -m memory_flywheel.scripts.ingest_solutions）")

    before = ing.old_table_rows()
    assert before["kb_chunks"] == 1542, before
    assert before["phase1_learnings"] == 5, before

    tbl = ing.table()
    df = tbl.to_pandas()
    assert set(df["solution_id"].unique()) == {
        "track1_tools/01_maniskill_pushcube",
        "track1_tools/02_mujoco_contact_push",
        "track2_questions/01_ice_floor_humanoid_dr",
        "track2_questions/02_lidar_weather"}
    assert set(df["kind"].unique()) == {"readme", "manifest", "code"}

    # 幂等重跑（本工具设计语义）：solution_chunks 行数不翻倍，老两表冻结
    total_before = ing.count()
    ing.ingest(expect_kb_rows=1542, expect_learnings=5)
    assert ing.count() == total_before
    assert ing.old_table_rows() == before

    # Task0a 验收前置（plan L311）：Q2 README prose 经 solution_chunks 语义召回
    hits = ing.query_solutions(
        "lidar rain fog snow weather carla", top_k=ing.count())
    readmes = [h for h in hits if h["kind"] == "readme"]
    assert readmes[0]["solution_id"] == "track2_questions/02_lidar_weather"
    rank = next(i for i, h in enumerate(hits)
                if h["kind"] == "readme"
                and h["solution_id"] == "track2_questions/02_lidar_weather")
    assert rank < 15, f"Q2 README 全表排位 {rank + 1}，超出可召回口径"


# ---------------------------------------------------------------------------
# 8. test_lancedb_move_regression（kb_integration）
# ---------------------------------------------------------------------------

@pytest.mark.kb_integration
def test_lancedb_move_regression(monkeypatch):
    kb = require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    migrate = _load_script("migrate_data_layer.py")

    data_root = Path("/mnt/artifacts/embodied-kb/data")
    inventory_p = data_root / "migration_pre_inventory.json"
    manifest_p = data_root / "manifest.json"
    assert inventory_p.is_file(), "缺迁移前清单（lead 须先跑 migrate_data_layer.py --apply）"
    assert manifest_p.is_file(), "缺数据层 manifest.json"
    inventory = json.loads(inventory_p.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))

    live_uri = (kb / "data" / "lancedb").resolve()
    assert str(live_uri) == str(data_root / "lancedb")
    assert not OLD_LANCEDB.exists(), f"旧路径仍在（双写风险）：{OLD_LANCEDB}"

    # 行数：老三表在迁移清单里 solution_chunks 尚不存在（None）；老两表必须
    # 现库 == 迁移前清单 == data manifest，solution_chunks 入库不改它们。
    state = migrate.collect_state(live_uri, with_recall=True)
    assert inventory["rows"]["solution_chunks"] is None, inventory["rows"]
    for table in ("kb_chunks", "phase1_learnings"):
        assert state["rows"][table] == inventory["rows"][table] == \
            manifest["tables"][table], table
    assert state["vector_dim"] == 768

    # 3 条固定查询召回 id 与迁移前快照逐项一致
    for name, ids in inventory["recall_snapshot"].items():
        assert state["recall_snapshot"][name] == ids, name
    # solution_chunks 入库后老三表行数仍与 manifest 一致（manifest 建在入库前则
    # 只锁老两表；solution_chunks 行数以入库器自身对账为准）
    assert state["rows"]["kb_chunks"] == 1542
    assert state["rows"]["phase1_learnings"] == 5


# ---------------------------------------------------------------------------
# 9. test_bundle_verify_roundtrip
# ---------------------------------------------------------------------------

def test_bundle_verify_roundtrip(tmp_path, monkeypatch):
    """tmp 合成库 core/extended 打包-对账往返 + 篡改拒绝（0 付费，不读活数据）。"""
    import lancedb
    bk = _load_script("bundle_kb_asset.py")
    vb = _load_script("verify_bundle.py")

    uri = tmp_path / "data" / "lancedb"
    db = lancedb.connect(str(uri))
    dim = 768

    def rows(n, tag):
        return [{"id": f"{tag}{i}", "vector": [0.0] * dim} for i in range(n)]

    db.create_table("kb_chunks", data=rows(3, "k"), mode="create")
    db.create_table("phase1_learnings", data=rows(2, "l"), mode="create")
    db.create_table("solution_chunks", data=rows(1, "s"), mode="create")

    # builder 需要真实 git 仓取 kb_sha；引擎 SHA 交叉核对直接对本仓 HEAD
    kb_git = tmp_path / "kbgit"
    kb_git.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=kb_git, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x", "--allow-empty"], cwd=kb_git, check=True)
    out = tmp_path / "bundles"

    core = bk.build_bundle(tier="core", uri=uri, out_root=out,
                           kb_root=kb_git, sim_root=REPO_ROOT)
    extended = bk.build_bundle(tier="extended", uri=uri, out_root=out,
                               kb_root=kb_git, sim_root=REPO_ROOT)

    # 引擎 SHA 严格对账（builder 记录的就是当前 HEAD）；kb 交叉核对在活库测中做
    for b in (core, extended):
        assert vb.verify_bundle(b, sim_root=REPO_ROOT) == []
        m = json.loads((b / "bundle_manifest.json").read_text(encoding="utf-8"))
        assert m["schema"] == "simpraxis.bundle_manifest.v1"
        assert m["embedding_model"].endswith("paraphrase-multilingual-mpnet-base-v2")
        assert m["vector_dim"] == 768
        assert stat.S_IMODE(b.stat().st_mode) == 0o700
        assert stat.S_IMODE((b / "bundle_manifest.json").stat().st_mode) == 0o600
    cm = json.loads((core / "bundle_manifest.json").read_text(encoding="utf-8"))
    em = json.loads((extended / "bundle_manifest.json").read_text(encoding="utf-8"))
    assert sorted(cm["tables"]) == ["kb_chunks", "phase1_learnings"]
    assert sorted(em["tables"]) == [
        "kb_chunks", "phase1_learnings", "solution_chunks"]

    # 篡改 1 字节 -> sha256 不符
    victim = sorted(p for p in (extended / "tables").rglob("*") if p.is_file())[0]
    data = bytearray(victim.read_bytes())
    data[0] ^= 0xFF
    victim.write_bytes(bytes(data))
    problems = vb.verify_bundle(extended, sim_root=REPO_ROOT)
    assert any("sha256 不符" in p for p in problems), problems

    # core 表集越界（manifest 被改成 extended 表集）-> tier 不符
    mp = core / "bundle_manifest.json"
    tampered = json.loads(mp.read_text(encoding="utf-8"))
    tampered["tables"]["solution_chunks"] = {"rows": 1, "vector_dim": 768, "files": []}
    mp.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    problems = vb.verify_bundle(core, sim_root=REPO_ROOT)
    assert any("core 包表集" in p for p in problems), problems

    # 集成开关 + 活数据/活资产包都在场时，追加活 extended 包 verify（lead 实跑
    # bundle 后必过）；条件不满足时仅本块 no-op，合成往返断言始终执行、永不跳过。
    if os.environ.get("SIMPRAXIS_KB_INTEGRATION") == "1":
        kb = live_kb_root()
        live_lance = kb / "data" / "lancedb" / "kb_chunks.lance"
        live = sorted(LIVE_BUNDLES.glob("simpraxis-extended-*")) if LIVE_BUNDLES.is_dir() else []
        if live_lance.is_dir() and live:
            assert vb.verify_bundle(live[-1], kb_root=kb, sim_root=REPO_ROOT,
                                    engine_allow_ancestor=True) == []
