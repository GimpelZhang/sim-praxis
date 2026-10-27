#!/usr/bin/env python3
"""Task0b 零付费九测（Phase_2_Plan_detailed.md 附录 A，逐条同名）。

引擎/内容/数据三层分离回归：
  1-4 纯引擎仓（合成 fixture/配置/git 事实，默认裸跑必过）；
  5-8 kb_integration（SIMPRAXIS_KB_INTEGRATION=1 + 活 KB/活数据层在场才执行，
     与 paid marker 同构的自动 skip，不是 test.skip 放水；活断言一条不弱）；
  9   tmp 合成资产包 core/extended 往返 + 篡改拒绝（默认必过），另在集成
     开关下追加活资产包 verify。
QA 修复轮追加 4b-4g 六个默认测试（数据链三态/strict 基线/对称文件集/
树卫生/solution_id 闸/未跟踪 md），断言一条不弱；test 7 内追加 strict
自动判定与规范方案集活断言。
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

# phase1_learnings 活表 id 基线（锁内适配（规格 §8 活表基线对账），工单
# /tmp/kb_recon_dispatch.md，2026-10-23 独立债务提交，先于 run7 特性提交；
# 2026-10-24 run7 活表对账 11→12；2026-10-27 Task7 攒批86 活表对账 12→32）：
# 活库现实 32 行 = 迁移时历史基线 5 行（2026-10-15/16，/mnt manifest 与
# migration_pre_inventory 双双冻结 5）+ Task6 run1–6 经 gate1 批准运行后诚实
# human_failure 回写 6 行（2026-10-20…22，gate_source=human_failure_agent_run）
# + Task6 run7a 回写 1 行（2026-10-23）+ Task6 升阶战役 12 条诚实 human_failure
# （2026-10-25，run=t6-genesis-ascent-20261016a）+ persist 首崩侧车 1 行
# （020327ed，2026-10-26）+ Task6 Gate2 accept 行 1（66bafb1d，rung=3，
# gate2_accepted，evidence @phase2/ 第三根）+ Task7 LIBERO Stage1 六行
# （run1/run2/run2j/run2k/侧车 五条 human_failure + 1 条 Gate2 accept
# 1b10ac8d，rung=1，2026-10-27）。32 位前缀全列钉死、只多不少。
# run7c（2026-10-24）失败文本与 run5 逐字相同，经内容寻址 id（learning_store
# _id_for 对 run_id|gate_source|rung|text|evidence_relpath 取 sha256）upsert
# 塌到既有 24681e47 行不增行；该行 created_at 被覆写 = key-collapse 尾债，另册
# 登记，引擎代码本次不动。
_PHASE1_LEARNING_IDS_BASELINE = {
    # —— 迁移时 5 行（历史基线，与两份冻结快照同源）——
    "dc0d01e7e53c",  # t7-mujoco 20261015 human_failure_gate2
    "48ec4eb4503c",  # t7-mujoco b gate2_accepted
    "1f1713ca9c9d",  # t7-maniskill gate2_accepted
    "fa7e87824e17",  # t8-genesis gate2_accepted
    "86d4198cb307",  # t8-q2carla gate2_accepted_supplementary（Task0a 4→5 授权行）
    # —— Task6 run1–6 human_failure_agent_run 诚实回写 6 行 ——
    "2db16c8babac",  # 2026-10-20
    "962f6d27a30a",  # 2026-10-21
    "d8681253ab4e",  # 2026-10-21
    "47479802882c",  # 2026-10-21
    "24681e47b2fb",  # 2026-10-22（run7c 2026-10-24 内容寻址去重塌此 id，created_at 被覆写）
    "f43d49d1bc5e",  # 2026-10-22（f43d49；run7 结局保留不塌缩）
    # —— Task6 run7a ——
    "0ef64b94244b",  # 2026-10-23 Task6 run7a（锚点 2 命中→human_failure；v1.2 协议修正前）
    # —— Task6 升阶战役 12 条诚实 human_failure（2026-10-25，run=t6-genesis-ascent-20261016a）——
    "05c0dedb670c",  # 2026-10-25T06:08:41Z
    "5f5a383247ec",  # 2026-10-25T08:20:17Z
    "34bd4d33d900",  # 2026-10-25T09:04:44Z
    "9dce693df4f7",  # 2026-10-25T09:32:55Z
    "0023944cbf8d",  # 2026-10-25T10:22:32Z
    "c3155d9ce7d9",  # 2026-10-25T11:15:45Z
    "62102153d301",  # 2026-10-25T12:01:10Z
    "c5bec0b98152",  # 2026-10-25T12:44:50Z
    "d386702b5683",  # 2026-10-25T12:59:10Z
    "f188a845609b",  # 2026-10-25T13:29:34Z
    "9e3869118aad",  # 2026-10-25T13:43:17Z
    "52d9f2c1a09c",  # 2026-10-25T16:57:17Z
    # —— persist 首崩侧车 + Task6 Gate2 accept 行（2026-10-26）——
    "020327ed97ad",  # 2026-10-26T00:51:27Z persist 首崩侧车 human_failure
    "66bafb1d8f3a",  # 2026-10-26T01:17:05Z Task6 Gate2 accept rung=3（@phase2/ 第三根）
    # —— Task7 LIBERO Stage1 战役六行（2026-10-26…27）——
    "42b7de2bebf3",  # 2026-10-26T05:00:32Z run1 5 轮帽尽 human_failure（robosuite/mujoco 墙）
    "fd59d8dfddb6",  # 2026-10-26T11:41:10Z run2 首启触批熔断 human_failure
    "344428787e7c",  # 2026-10-26T13:15:09Z run2j 5 轮帽尽 human_failure（API 名墙三坑）
    "1bf02ea873f5",  # 2026-10-27T01:53:11Z run2k 随机动作帽尽 human_failure（Gate2 reject#1 前）
    "2f9ca046843e",  # 2026-10-27T06:47:24Z persist 首崩侧车 human_failure（rungs 契约修复前）
    "1b10ac8d9d30",  # 2026-10-27T07:26:07Z Gate2 accept rung=1（核验级，Task0a 同约定）
}

def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"task0b_{name[:-3]}", SCRIPTS_KB / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _fixture_solutions_root(request, monkeypatch):
    # 合成 fixture env 只对引擎纯测（1-4、9）生效；kb_integration 活库测试必须
    # 走真实 $KB_ROOT/solutions——否则 ingester 按 env 发现合成方案，先删后写
    # 会把活 solution_chunks 替换成 fixture 块（QA 实测 82→14 写穿事故）。
    if "kb_integration" in request.keywords:
        return
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
# 4b. QA M1：内容数据链三态（实体目录/缺失/有效软链）
# ---------------------------------------------------------------------------

_PRUNE_CACHE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def _fake_content_kb(tmp_path, *, data_link_target: "Path | None" = None,
                     entity_data: bool = False, monkeypatch=None) -> Path:
    """合成内容仓：solutions=去缓存的 fixture 树；data 按三态构造。

    env 钉死 KB_ROOT 指向合成仓、清掉 SIMPRAXIS_LANCEDB_URI（否则宿主机
    00_env 注入的活库 URI 会改道默认构造），SOLUTIONS_ROOT 与合成仓对齐。
    """
    kb = tmp_path / "kb"
    shutil.copytree(FIXTURE_SOLUTIONS, kb / "solutions", ignore=_PRUNE_CACHE)
    if entity_data:
        (kb / "data" / "lancedb").mkdir(parents=True)
    elif data_link_target is not None:
        data_link_target.mkdir(parents=True, exist_ok=True)
        (kb / "data").symlink_to(data_link_target, target_is_directory=True)
    if monkeypatch is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
        monkeypatch.delenv("SIMPRAXIS_LANCEDB_URI", raising=False)
        monkeypatch.setenv("KB_ROOT", str(kb))
        monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT", str(kb / "solutions"))
    return kb


def test_content_data_link_three_states(tmp_path, monkeypatch):
    """(a) 实体 data/ (b) data 缺失：三构造器全 DataLayerError 且不新建路径；
    (c) data 为有效软链：三构造器均可构造（嵌入懒加载，0 触网）。"""
    from memory_flywheel import kb_pins
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.scripts.ingest_solutions import SolutionIngester
    from memory_flywheel.vector_engine import VectorEngine

    # (a) 实体目录 split-brain
    kb = _fake_content_kb(tmp_path, entity_data=True, monkeypatch=monkeypatch)
    ctor_label = ("VectorEngine", "LearningStore", "SolutionIngester")
    ctors = (VectorEngine, LearningStore,
             lambda: SolutionIngester(kb_root=kb))
    snapshot = sorted(p.relative_to(kb).as_posix()
                      for p in (kb / "data").rglob("*"))
    for label, ctor in zip(ctor_label, ctors):
        with pytest.raises(kb_pins.DataLayerError):
            ctor()
    assert sorted(p.relative_to(kb).as_posix()
                  for p in (kb / "data").rglob("*")) == snapshot, \
        f"{label} fail-closed 不得新建任何数据路径"

    # (b) data 缺失（缺链新克隆）
    shutil.rmtree(kb / "data")
    for ctor in ctors:
        with pytest.raises(kb_pins.DataLayerError):
            ctor()
    assert not (kb / "data").exists(), "拒绝后不得在公开内容仓实建 data/"

    # (c) 有效软链 -> 另一 tmp 实体目录：全部可构造
    shutil.rmtree(kb)
    target = tmp_path / "datalayer"
    kb = _fake_content_kb(tmp_path, data_link_target=target,
                          monkeypatch=monkeypatch)
    VectorEngine()
    LearningStore()
    SolutionIngester(kb_root=kb)
    # 库实体落在软链目标，内容仓 data 仍是软链本身（未变实体目录）
    assert kb.joinpath("data").is_symlink()
    assert target.joinpath("lancedb").is_dir()


# ---------------------------------------------------------------------------
# 4c. QA M1/M2：strict 基线缺表 fail-closed；非 strict 彩排保留 None 容忍
# ---------------------------------------------------------------------------

def test_strict_baseline_missing_tables_fail_closed(tmp_path, monkeypatch):
    """伪造 KB（data→空目标）：无基线两表时 strict ingest 抛错，不建 solution_chunks。"""
    from memory_flywheel import kb_pins
    from memory_flywheel.scripts.ingest_solutions import (
        TABLE_NAME, SolutionIngestError, SolutionIngester)

    kb = _fake_content_kb(tmp_path, data_link_target=tmp_path / "datalayer",
                          monkeypatch=monkeypatch)
    subprocess.run(["git", "init", "-q"], cwd=kb, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x", "--allow-empty"], cwd=kb, check=True)

    ing = SolutionIngester(kb_root=kb)
    assert kb_pins.is_content_data_uri(ing.uri, kb_root=kb) is True
    with pytest.raises(SolutionIngestError, match="strict 基线表缺失"):
        ing.ingest(expect_kb_rows=1542, expect_learnings=5)
    assert TABLE_NAME not in ing.db.table_names()
    assert not (tmp_path / "datalayer" / "lancedb"
                / f"{TABLE_NAME}.lance").exists()

    # 非内容层 URI（tmp 彩排库）自动 non-strict：基线表缺失被容忍，dry-run 可走通
    rehe_uri = tmp_path / "rehearsal" / "lancedb"
    rehe = SolutionIngester(kb_root=kb, uri=str(rehe_uri))
    assert kb_pins.is_content_data_uri(str(rehe_uri), kb_root=kb) is False
    out = rehe.ingest(expect_kb_rows=1542, expect_learnings=5, dry_run=True)
    assert out["dry_run"] is True


# ---------------------------------------------------------------------------
# 4d. QA L1：load_manifest 对称文件集（未登记文件/符号链接拒，缓存目录豁免）
# ---------------------------------------------------------------------------

def test_load_manifest_symmetric_file_set(tmp_path):
    src = FIXTURE_SOLUTIONS / "track1_tools" / "01_maniskill_pushcube"

    def _fresh_copy(name: str) -> Path:
        dst = tmp_path / name / "track1_tools" / "01_maniskill_pushcube"
        shutil.copytree(src, dst, ignore=_PRUNE_CACHE)
        return dst

    bad = _fresh_copy("b1")
    (bad / "notes.txt").write_text("未登记笔记", encoding="utf-8")
    with pytest.raises(SolutionError, match="未登记"):
        load_manifest(bad)

    cached = _fresh_copy("b2")
    (cached / "__pycache__").mkdir()
    (cached / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    manifest = load_manifest(cached)  # 缓存目录整段豁免
    assert manifest.solution_id == "track1_tools/01_maniskill_pushcube"

    linked = _fresh_copy("b3")
    outside = tmp_path / "outside.py"
    outside.write_text("# external", encoding="utf-8")
    (linked / "evil_link.py").symlink_to(outside)
    with pytest.raises(SolutionError, match="符号链接"):
        load_manifest(linked)


# ---------------------------------------------------------------------------
# 4e. QA L4：check_solutions 树卫生（游离目录/缓存目录报错；规范树干净）
# ---------------------------------------------------------------------------

def test_check_solutions_tree_hygiene(tmp_path):
    cs = _load_script("check_solutions.py")

    # 合成树：1 个规范方案 + 1 个缺 manifest 的游离目录 + 1 个缓存目录
    kb = tmp_path / "kb"
    sroot = kb / "solutions"
    shutil.copytree(FIXTURE_SOLUTIONS / "track1_tools" / "01_maniskill_pushcube",
                    sroot / "track1_tools" / "01_maniskill_pushcube",
                    ignore=_PRUNE_CACHE)
    (sroot / "track1_tools" / "99_stray_no_manifest").mkdir(parents=True)
    gaps = cs.check_manifest_gaps(kb)
    assert any("99_stray_no_manifest" in e for e in gaps), gaps
    (sroot / "track1_tools" / "01_maniskill_pushcube" / "__pycache__").mkdir()
    cache_errs = cs.check_cache_dirs(kb)
    assert any("__pycache__" in e for e in cache_errs), cache_errs

    # 规范 5 方案树（去缓存复制体）：两类卫生检查全空，完整 check_solutions 也全过
    clean = tmp_path / "clean"
    shutil.copytree(FIXTURE_SOLUTIONS, clean / "solutions", ignore=_PRUNE_CACHE)
    assert cs.check_manifest_gaps(clean) == []
    assert cs.check_cache_dirs(clean) == []
    assert cs.check_solutions(clean) == []

    # 物理卫生后 fixture 树本身不得有游离方案目录（缓存检查走去缓存复制体，
    # 因 gate_static 的 py_compile 可能在 fixture 旁重建 __pycache__）。
    fixture_kb = REPO_ROOT / "tests" / "fixtures"
    assert cs.check_manifest_gaps(fixture_kb) == []


# ---------------------------------------------------------------------------
# 4f. QA L3：validate_solution_id + query_solutions where 拼接前闸
# ---------------------------------------------------------------------------

def test_validate_solution_id_and_query_guard(tmp_path, monkeypatch):
    from orchestrator.solutions import validate_solution_id
    from memory_flywheel.scripts.ingest_solutions import (
        TABLE_NAME, SolutionIngester)

    good = "track2_questions/02_lidar_weather"
    assert validate_solution_id(good) == good
    for bad in ("../x", "track3_x/01_a", "track1_tools/1_x",
                "track1_tools/01_x' OR '1'='1", "", "track1_tools"):
        with pytest.raises(SolutionError):
            validate_solution_id(bad)

    kb = _fake_content_kb(tmp_path, data_link_target=tmp_path / "datalayer",
                          monkeypatch=monkeypatch)
    ing = SolutionIngester(kb_root=kb)
    # solution_chunks 尚不存在：非法 id 必须在 embed/table() 之前抛错（不触库不触网）
    with pytest.raises(SolutionError):
        ing.query_solutions("任意查询", solution_id="../../x")
    assert TABLE_NAME not in ing.db.table_names()


# ---------------------------------------------------------------------------
# 4g. QA L5：check_knowledge_hash_set 必须看见未跟踪 md
# ---------------------------------------------------------------------------

def test_knowledge_untracked_md_detected(tmp_path):
    from memory_flywheel import kb_pins

    root = tmp_path / "kbwc"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "extra_untracked.md").write_text(
        "# 未跟踪新增\n", encoding="utf-8")

    errors = kb_pins.check_knowledge_hash_set(root)
    assert any("extra_untracked.md" in e and "未被 git 跟踪" in e
               for e in errors), errors
    # 基线缺失错误照旧（32 个基线文件一个都不在）
    assert any("基线文件丢失" in e for e in errors), errors
    # 已登记基线集本身不被误报为未跟踪
    assert not any("00-index" in e and "未被 git 跟踪" in e for e in errors)


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

    # QA M1/M2：活路径 strict 自动判定必须为 True；discover id 集恰好等于规范集
    from memory_flywheel import kb_pins
    assert kb_pins.is_content_data_uri(ing.uri, kb_root=kb) is True
    discovered = {f"{d.parent.name}/{d.name}"
                  for d in ing.discover_solution_dirs()}
    assert discovered == set(kb_pins.EXPECTED_SOLUTION_IDS), discovered

    before = ing.old_table_rows()
    assert before["kb_chunks"] == 1542, before
    # 锁内适配（规格 §8 活表基线对账，工单 /tmp/kb_recon_dispatch.md）：活表
    # 现实 32 行 = 迁移时 5 + Task6 run1–6 human_failure 6 + run7a 1
    # + Task6 升阶战役 12（2026-10-25）+ persist 首崩侧车 1 + Task6 Gate2
    # accept 行 1 + Task7 战役六行（run1/run2/run2j/run2k/侧车 human_failure
    # 5 + Gate2 accept 1，2026-10-26…27）；run7c 内容寻址 upsert 塌到既有
    # 24681e47 不增行。32 个 id 全集合在 test_lancedb_move_regression 钉死。
    assert before["phase1_learnings"] == 32, before

    tbl = ing.table()
    df = tbl.to_pandas()
    assert set(df["solution_id"].unique()) == {
        "track1_tools/01_maniskill_pushcube",
        "track1_tools/02_mujoco_contact_push",
        "track1_tools/03_libero",
        "track2_questions/01_ice_floor_humanoid_dr",
        "track2_questions/02_lidar_weather"}
    assert set(df["kind"].unique()) == {"readme", "manifest", "code"}
    # 显式 kb_root 必须钉死内容层为活 KB/solutions（env 改道 fail-closed），
    # 且每方案块数等于真实内容口径（fixture 合成方案仅 3/3/3/3/5，可直接揭穿写穿）。
    assert ing.solutions_dir.resolve() == (kb / "solutions").resolve()
    assert df.groupby("solution_id").size().sort_index().to_dict() == {
        "track1_tools/01_maniskill_pushcube": 15,
        "track1_tools/02_mujoco_contact_push": 10,
        "track1_tools/03_libero": 12,
        "track2_questions/01_ice_floor_humanoid_dr": 22,
        "track2_questions/02_lidar_weather": 35}

    # 幂等重跑（本工具设计语义）：solution_chunks 行数不翻倍，老两表冻结
    total_before = ing.count()
    # 锁内适配（规格 §8 活表基线对账）：fail-closed 期望值随活表 5→11→12→32
    # （升阶战役 12 + 侧车 1 + Gate2 accept 1 + Task7 六行；run7c 内容寻址
    # 去重塌到 24681e47 不增行），守卫语义不松——老两表行数与期望不符
    # ingester 仍拒入库。
    ing.ingest(expect_kb_rows=1542, expect_learnings=32)
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

    # 行数对账 —— 锁内适配（规格 §8 活表基线对账，工单 /tmp/kb_recon_dispatch.md，
    # 2026-10-23 独立债务提交）：
    #   * kb_chunks 维持「活库 == 迁移前清单 == manifest == 1542」三重不变；
    #   * solution_chunks 在两份迁移时快照中尚不存在（None）；
    #   * phase1_learnings 的清单/manifest 是 2026-10-17 迁移时点的历史冻结证据，
    #     恒为 5（谁把快照改成活表现值谁红）；活库自 Task6 run1–6 经 gate1 批准
    #     运行后诚实回写 6 行 human_failure，不再等于快照——run7a 2026-10-23
    #     （锚点 2 命中→human_failure，v1.2 协议修正前）再回写 1 行后现实 12 行
    #     且全 id 集合钉死（_PHASE1_LEARNING_IDS_BASELINE，id 从活表同源连接读）；
    #     run7c（2026-10-24）失败文本与 run5 逐字相同，内容寻址 id upsert 塌到
    #     既有 24681e47 不增行（created_at 覆写 = key-collapse 尾债，另册登记；
    #     规格 D7 的 f43d49 旧行始终保留）。
    state = migrate.collect_state(live_uri, with_recall=True)
    assert inventory["rows"]["solution_chunks"] is None, inventory["rows"]
    assert manifest["tables"]["solution_chunks"] is None, manifest["tables"]
    assert state["rows"]["kb_chunks"] == inventory["rows"]["kb_chunks"] == \
        manifest["tables"]["kb_chunks"] == 1542
    # 历史证据冻结：迁移清单与 manifest 互证恒 5，改写历史即红
    assert inventory["rows"]["phase1_learnings"] == \
        manifest["tables"]["phase1_learnings"] == 5
    assert state["vector_dim"] == 768

    # 3 条固定查询召回 id 与迁移前快照逐项一致
    for name, ids in inventory["recall_snapshot"].items():
        assert state["recall_snapshot"][name] == ids, name
    # 活 phase1_learnings：32 行且 32 个 id（12 位前缀）集合钉死，只多不少
    # （升阶战役 12 + 侧车 1 + Task6 Gate2 accept 1 + Task7 六行，
    # 2026-10-27 攒批86 对账；run7c 内容寻址塌到 24681e47 不增行）；
    # id 从 state 同源（live_uri）连接现读，防字面量与活表各说各话。
    assert state["rows"]["phase1_learnings"] == 32
    assert len(_PHASE1_LEARNING_IDS_BASELINE) == 32
    import lancedb
    live_tbl = lancedb.connect(str(live_uri)).open_table("phase1_learnings")
    live_ids = {i[:12] for i in live_tbl.to_pandas()["id"]}
    assert live_ids == _PHASE1_LEARNING_IDS_BASELINE, live_ids


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

    # 10. 构造期内容层改道守卫（QA 实测写穿事故的固化）：显式 kb_root 与
    # SIMPRAXIS_SOLUTIONS_ROOT 不一致必须 fail-closed，且发生在任何 mkdir/写之前。
    from memory_flywheel.scripts.ingest_solutions import (
        SolutionIngestError, SolutionIngester)
    other = tmp_path / "elsewhere" / "solutions"
    other.mkdir(parents=True)
    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT", str(other))
    kb_other = tmp_path / "kbother"
    (kb_other / "solutions").mkdir(parents=True)
    uri_dst = tmp_path / "must_not_be_created" / "lancedb"
    with pytest.raises(SolutionIngestError, match="内容层指向冲突"):
        SolutionIngester(kb_root=kb_other, uri=str(uri_dst))
    assert not uri_dst.exists(), "fail-closed 不得在拒绝前创建任何数据路径"

    # 集成开关 + 活数据/活资产包都在场时，追加活 extended 包 verify（lead 实跑
    # bundle 后必过）；条件不满足时仅本块 no-op，合成往返断言始终执行、永不跳过。
    if os.environ.get("SIMPRAXIS_KB_INTEGRATION") == "1":
        kb = live_kb_root()
        live_lance = kb / "data" / "lancedb" / "kb_chunks.lance"
        live = sorted(LIVE_BUNDLES.glob("simpraxis-extended-*")) if LIVE_BUNDLES.is_dir() else []
        if live_lance.is_dir() and live:
            assert vb.verify_bundle(live[-1], kb_root=kb, sim_root=REPO_ROOT,
                                    engine_allow_ancestor=True) == []
