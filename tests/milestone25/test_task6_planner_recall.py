#!/usr/bin/env python3
"""Phase 2.5 Task6 — Gate1 全量知识四路召回与规划题面升级（全部离线 0 付费）。

覆盖验收（计划 §4 AC-T6-1/2/5/6/7/8；AC-T6-4 的机判在
scripts/phase25/eval_planner_recall.py；mpnet/768 负结论的双轨对账在本文件
尾部 kb_integration 测试——mpnet 轨活库重算、qwen 轨几何无关不变量，
mpnet 历史值另在 iter1/iter2 报告文件证据链永久封存，见 fcb3fcd6 §3.4
缺口双轨适配，2026-11-01 用户当日裁决）：

  A 查询构造器/同义词典：零命中逐字节旧 hint、ASCII 词边界（ACT 不命中
    react/action）、CJK 子串、档级主题句只进 kb 路、词典校验 fail-closed、
    r2 项目识别在未扩展 base 上做（不级联）；
  B 四路编排：kb 双 namespace 独立 top_k + r2 项目作用域 lane、solutions
    全局+方案 lane 与缺表容错、learning 主库+额外库合并缺表 []、距离合并
    去重截断、浅拷贝不改原行、采纳遥测透传单一写入方；
  C 三段注入：三段头/空段不渲染/[Si] kb+solution 连续编号/逐字代码窗/
    evidence_refs 跨两路可解析/policy_closed_loop 要求句；
  D setter 回归：planner_recall=None 时消息逐字节走旧 build_first_messages、
    每候选轮恰好 1 次 macro 调用、0 采纳行；planner 模式三段 + 七字段遥测；
  E 遥测/闭环机判：六字段旧行/七字段新行、row_source_route 四形、
    iter_adoption_rows 损坏 fail-closed、audit_adoption_rows hermetic 三形；
  F ingest_solutions --expect-learnings 必填（argparse rc=2）；
  G gold@8 机判脚本：fixture 冻结契约 + load_cases 校验 + rc 0/3/2。

纪律：合成件 + 断网 env（mpnet 走 /mnt 本地缓存）；不写任何活库。
活库不变量（1542/44/94/1）、AC-T6-3 12 块覆盖与 AC-T6-4 负结论重算均打
kb_integration，裸跑自动 skip。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from memory_flywheel.recall import (
    LOG_FIELDS,
    LOG_FIELDS_V2,
    SOURCE_ROUTE_KB,
    SOURCE_ROUTE_LEARNINGS,
    SOURCE_ROUTE_SOLUTIONS,
    SOURCE_ROUTE_UNKNOWN,
    ProductionRecall,
    count_adoptions,
    iter_adoption_rows,
    prompt_embedded_text,
    row_source_route,
)
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.direction.planner_recall import (
    KB_PROJECT_TAGS,
    R3_RULES_VERSION,
    SYNONYMS_PATH,
    TIER_POLICY_CLOSED_LOOP,
    TIER_PROBE,
    PlannerRecall,
    _scoped_kb_projects,
    _scoped_solution_ids,
    build_planner_hint,
    build_route_hints,
    detect_projects,
    legacy_direction_hint,
    load_synonyms,
    matched_projects,
)
from orchestrator.direction.setter import (
    DirectionError,
    DirectionSetter,
    PLANNER_SECTION_KB,
    PLANNER_SECTION_LEARNINGS,
    PLANNER_SECTION_SOLUTIONS,
    _POLICY_REQUIREMENT,
    _planner_sections,
    _planner_track1_user,
    build_first_messages,
    build_planner_first_messages,
    load_engine_catalog,
    validate_proposals,
)
from orchestrator.solutions import validate_solution_id
from memory_flywheel.embedding_selector import select_embedding
from scripts.kb import gen_post_rebuild_snapshots as gps
from tests.kb_support import require_kb_integration

_MPNET_MODEL_SUFFIX = "paraphrase-multilingual-mpnet-base-v2"

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (REPO_ROOT / "tests" / "milestone25" / "fixtures"
           / "planner_golden_queries.json")

LIBERO_SOLUTION_ID = "track1_tools/03_libero"
LIBERO_12_PREFIXES = {
    "8fc302da5604", "7f9bdabc4e72", "947d4633823c", "c7d2a7a8ba7a",
    "50405f5cb4ec", "9a15e94c83e7", "cb1f008d670f", "ae0146acc531",
    "ca8c9e45328b", "2e7d3ac99db9", "c685ddb05301", "77444cbfec27"}


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网：fastembed 任何下载尝试都应直接失败（mpnet 已在本地缓存）。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


def _load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# A) 查询构造器 / 同义词典（AC-T6-2）
# ---------------------------------------------------------------------------

def test_zero_hit_hint_byte_identical_to_setter_query():
    """无项目别名时三路 hint 必须与 setter 现行查询串逐字节一致（golden）。"""
    name = "自研多体动力学内核 Kinematica-X1"
    base_text = f"{name} 架构 物理引擎 特性 闭环仿真"
    assert detect_projects(base_text) == []  # 前置：该题面确实零命中
    payload1 = {"name": name, "urls": []}
    assert legacy_direction_hint("track1", payload1) == base_text
    hints = build_route_hints("track1", payload1, tier=TIER_PROBE)
    assert hints == {"kb": base_text, "solutions": base_text,
                     "learnings": base_text}

    question = "为什么冰面摩擦系数下降后 DR 对照的奖励比反而小于界值？"
    assert detect_projects(question) == []
    assert legacy_direction_hint("track2", {"question": question}) == question
    hints2 = build_route_hints("track2", {"question": question},
                               tier=TIER_PROBE)
    assert set(hints2) == {"kb", "solutions", "learnings"}
    assert all(v == question for v in hints2.values())


def test_alias_ascii_word_boundaries_and_cjk_substring():
    def ids(text):
        return [p["id"] for p in detect_projects(text)]

    # ACT 只匹配大写专有形态：react/transact/action 都不得命中。
    assert "act" in ids("用 ACT 做动作分块模仿学习")
    assert "act" not in ids("react hooks 与 transact 事务处理")
    assert "act" not in ids("define the action space of the robot arm")
    # 显式登记的大小写形态与 CJK 子串正常命中。
    assert "act" in ids("Action Chunking Transformer 训练")
    assert "act" in ids("动作分块策略的分块预测")
    # CARLA 数字紧邻不命中（CARLA2 是另一个词），连字符边界命中。
    assert "carla" not in ids("CARLA2 仿真器评测")
    assert "carla" in ids("CARLA-2 激光雷达天气矩阵")
    # LIBERO 别名族（BDDL/03_libero/openpi PiPolicy）。
    assert "libero" in ids("BDDL 目标谓词 resolve_target")
    assert "libero" in ids("方案 03_libero 的 stage1 最小闭环")
    assert "openpi" in ids("PiPolicy 动作接口 checkpoint")
    assert "openvla" in ids("OpenVLA-7B 末端收敛微调")


def test_detect_projects_order_and_dedup():
    ids = [p["id"] for p in detect_projects("LIBERO 与 Genesis 流体里的 robosuite 栈")]
    # LIBERO 扩展词不参与识别（见 matched_projects 级联测试）；此处顺序按词典。
    assert ids[0] == "libero"
    assert ids[-1] == "genesis"
    assert len(ids) == len(set(ids))


def test_matched_projects_detects_on_unexpanded_base_no_cascade():
    """LIBERO 扩展串自带 robosuite/MuJoCo/BDDL：作用域项目识别只看 base。"""
    projs = matched_projects(
        "track1", {"name": "LIBERO 机械臂操作 benchmark", "urls": []})
    assert [p["id"] for p in projs] == ["libero"]
    assert _scoped_kb_projects(projs) == ["lw_benchhub"]
    assert _scoped_solution_ids(projs) == [LIBERO_SOLUTION_ID]
    # build_planner_hint 追加 libero 扩展词（其字面含 robosuite/MuJoCo/BDDL）；
    # 作用域探针的项目集只从 base 识别（matched_projects 已证），扩展串上的
    # 二次识别允许级联——它不参与任何探针决策。
    hint = build_planner_hint(
        "track1", {"name": "LIBERO 机械臂操作 benchmark", "urls": []})
    assert "robosuite" in hint and "BDDL" in hint  # libero 扩展词在场


def test_policy_tier_theme_only_enters_kb_hint():
    question = "闭环操作任务的策略接口如何逐 Stage 切分？"
    assert detect_projects(question) == []
    hints = build_route_hints("track2", {"question": question},
                              tier=TIER_POLICY_CLOSED_LOOP)
    assert "policy_source" in hints["kb"]
    assert "scripted_servo" in hints["kb"]
    assert "official_demo_policy" in hints["kb"]
    assert "opensource_vla" in hints["kb"]
    # solutions/learnings 两路恒走 probe 串（泛化主题句不稀释代码/战役切块）。
    assert hints["solutions"] == hints["learnings"] == question
    with pytest.raises(ValueError):
        build_route_hints("track2", {"question": question}, tier="bogus")


def _synonym_doc(projects, *, probe="", policy=None):
    doc = {"schema": "simpraxis.planner_synonyms.v1",
           "projects": projects,
           "tiers": {"probe": probe}}
    if policy is not None:
        doc["tiers"][TIER_POLICY_CLOSED_LOOP] = policy
    return doc


def _write_synonyms(tmp_path, doc):
    import yaml
    p = tmp_path / "synonyms.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    load_synonyms.cache_clear()
    return str(p)


def test_load_synonyms_validation_fail_closed(tmp_path):
    good = {"id": "p", "aliases": ["PROJ"], "expansion": "PROJ 扩展词"}
    # 白名单外 project 标签拒绝（防数据文件写出无意义 WHERE）。
    p = _write_synonyms(tmp_path, _synonym_doc(
        [{**good, "kb_projects": ["not_a_tag"]}]))
    with pytest.raises(ValueError, match="kb_projects"):
        load_synonyms(p)
    # kb_projects 非列表 / solution_id 非空串同样 fail-closed。
    p = _write_synonyms(tmp_path, _synonym_doc(
        [{**good, "kb_projects": "lw_benchhub"}]))
    with pytest.raises(ValueError):
        load_synonyms(p)
    p = _write_synonyms(tmp_path, _synonym_doc(
        [{**good, "solution_id": 123}]))
    with pytest.raises(ValueError, match="solution_id"):
        load_synonyms(p)
    # schema / 空 projects / 缺 probe 档拒绝。
    p = _write_synonyms(tmp_path, {**_synonym_doc([good]), "schema": "x"})
    with pytest.raises(ValueError, match="schema"):
        load_synonyms(p)
    p = _write_synonyms(tmp_path, _synonym_doc([]))
    with pytest.raises(ValueError):
        load_synonyms(p)


def test_production_synonyms_file_contract():
    data = load_synonyms(str(SYNONYMS_PATH))
    ids = {p["id"] for p in data["projects"]}
    # 计划点名的 10 个项目族必须在典。
    assert {"libero", "robosuite", "mujoco", "vla", "openvla", "openpi",
            "octo", "robomimic", "act", "diffusion_policy"} <= ids
    # r2 取值纪律：kb_projects 全在活库标签白名单（加载器已强校验），
    # solution_id 全部通过 C4 路径校验（不触库，只验字符集形态）。
    for proj in data["projects"]:
        for tag in proj.get("kb_projects", []):
            assert tag in KB_PROJECT_TAGS
        sid = proj.get("solution_id")
        if sid:
            assert validate_solution_id(sid) == sid
    assert data["tiers"][TIER_PROBE] == ""
    assert "7-DOF delta EEF" in data["tiers"][TIER_POLICY_CLOSED_LOOP]


# ---------------------------------------------------------------------------
# B) 四路召回编排（AC-T6-1，合成件）
# ---------------------------------------------------------------------------

def _kb_row(rid, distance, *, text=None, relpath="knowledge/x.md",
            section="s1", doc_type="guide", project="global"):
    return {"id": rid, "text": text or f"KB 正文 {rid}。",
            "kb_relpath": relpath, "section_path": section,
            "doc_type": doc_type, "project": project,
            "namespace": "capabilities", "_distance": distance}


def _sol_row(rid, distance, *, solution_id=LIBERO_SOLUTION_ID,
             relpath=None, kind="code", chunk_index=1, text=None):
    return {"id": rid, "text": text or f"solution code {rid}\nprint({rid!r})",
            "relpath": relpath or f"solutions/{solution_id}/scripts/win.py",
            "kind": kind, "chunk_index": chunk_index,
            "solution_id": solution_id, "_distance": distance}


class FakeVectorEngine:
    """记录调用参数、回放合成命中；不做任何 embed（0 付费、0 下载）。"""

    def __init__(self, *, cap=(), tb=(), scoped_kb=None, solutions=(),
                 scoped_sol=None, missing_solutions=False):
        self.cap = list(cap)
        self.tb = list(tb)
        self.scoped_kb = scoped_kb or {}
        self.solutions = list(solutions)
        self.scoped_sol = scoped_sol or {}
        self.missing_solutions = missing_solutions
        self.kb_calls = []
        self.sol_calls = []

    def query_kb(self, query, *, namespace=None, project=None,
                 doc_types=None, top_k=5, include_index=False):
        self.kb_calls.append({"query": query, "namespace": namespace,
                              "project": project, "top_k": top_k})
        if project is not None:
            return [dict(r) for r in self.scoped_kb.get(project, [])]
        if namespace == "capabilities":
            return [dict(r) for r in self.cap]
        if namespace == "troubleshooting":
            return [dict(r) for r in self.tb]
        raise AssertionError(f"未预期的 query_kb 形态 ns={namespace}")

    def query_solutions(self, query, *, top_k=8, solution_id=None):
        self.sol_calls.append({"query": query, "solution_id": solution_id,
                               "top_k": top_k})
        if self.missing_solutions:
            raise RuntimeError(
                "表 solution_chunks 不存在（先运行 ingest_solutions）")
        if solution_id is not None:
            return [dict(r) for r in self.scoped_sol.get(solution_id, [])]
        return [dict(r) for r in self.solutions]


class FakeLearningStore:
    def __init__(self, name, rows=(), *, exists=True):
        self.name = name
        self._rows = list(rows)
        self._exists = exists
        self.queries = []

    def table_exists(self):
        return self._exists

    def table(self, create=False):
        class _T:
            pass
        t = _T()
        t.name = self.name
        return t

    def query(self, hint, top_k=5):
        self.queries.append((hint, top_k))
        return [dict(r) for r in self._rows[:top_k]]


def _lr_row(rid, *, table=None, text=None, evidence="@runs/phase1/x.json",
            run_id="r1", gate_source="human_failure_agent_run", rung=0):
    return {"id": rid, "text": text or f"学习行 {rid}：真实避坑叙述。",
            "evidence_relpath": evidence, "run_id": run_id,
            "gate_source": gate_source, "rung": rung}


def _planner(ve, main_store, extra=(), **kw):
    prod = ProductionRecall(main_store, ve,
                            extra_learning_stores=tuple(extra))
    # 本文件的断言全部是 r2/r3 语义锚点；r4 默认上线后显式钉 r3，
    # 断言内容一字不改（task6_r4_prereg.md §10）。
    kw.setdefault("rules_version", "r3")
    return PlannerRecall(prod, **kw), prod


def test_kb_dual_namespace_independent_topk_distance_merge_dedup():
    ve = FakeVectorEngine(cap=[_kb_row("ka", 0.10), _kb_row("kb", 0.40)],
                          tb=[_kb_row("kb", 0.20, section="t"),
                              _kb_row("kc", 0.50, section="t")])
    main = FakeLearningStore("phase1_learnings")
    planner, _ = _planner(ve, main)
    ret = planner.retrieve("track1",
                           {"name": "自研内核 Kinematica-Z9", "urls": []},
                           tier=TIER_PROBE, site="direction")
    # 双 namespace 各发一条、独立 top_k；无项目命中 → 无 scoped lane。
    assert {c["namespace"] for c in ve.kb_calls} == \
        {"capabilities", "troubleshooting"}
    assert all(c["project"] is None for c in ve.kb_calls)
    assert all(c["top_k"] == 8 for c in ve.kb_calls)
    # 距离合并：ka(.10) < kb(.20 tb) 去重一次 < kc(.50)。
    assert [r["id"] for r in ret["kb_hits"]] == ["ka", "kb", "kc"]
    merged_tb = next(r for r in ret["kb_hits"] if r["id"] == "kb")
    assert merged_tb["section_path"] == "t"  # 留下的是更近的 tb 副本
    assert len(ret["kb_capabilities_hits"]) == 2
    assert len(ret["kb_troubleshooting_hits"]) == 2
    assert all(r["source_route"] == SOURCE_ROUTE_KB for r in ret["kb_hits"])
    assert all(r["relpath"] == r["kb_relpath"] for r in ret["kb_hits"])
    # 浅拷贝：FakeVectorEngine 回放的原行不被写上 source_route。
    assert all("source_route" not in r for r in ve.cap + ve.tb)


def test_r2_project_scoped_lanes_fire_and_can_seat_close_rows():
    # 全局 lane 距离很差；LIBERO 命中 → lw_benchhub 作用域 lane 近距离行必须能入座。
    ve = FakeVectorEngine(
        cap=[_kb_row("g1", 9.0, project="global")], tb=[],
        scoped_kb={"lw_benchhub": [
            _kb_row("scoped-1", 0.30, project="lw_benchhub")]})
    planner, _ = _planner(ve, FakeLearningStore("phase1_learnings"))
    ret = planner.retrieve("track1",
                           {"name": "LIBERO stage1 机械臂任务", "urls": []},
                           site="direction")
    scoped_calls = [c for c in ve.kb_calls if c["project"] is not None]
    assert [c["project"] for c in scoped_calls] == ["lw_benchhub"]
    assert scoped_calls[0]["namespace"] is None  # 跨两 namespace
    assert ret["kb_hits"][0]["id"] == "scoped-1"
    assert ret["kb_hits"][0]["scoped_project"] == "lw_benchhub"
    assert "scoped_project" not in ret["kb_hits"][-1]


def test_solution_route_global_plus_solution_id_lanes_and_annotation():
    ve = FakeVectorEngine(
        solutions=[_sol_row("sol-a", 0.50)],
        scoped_sol={LIBERO_SOLUTION_ID: [_sol_row("sol-l", 0.10)]})
    planner, _ = _planner(ve, FakeLearningStore("phase1_learnings"))
    ret = planner.retrieve("track1",
                           {"name": "LIBERO servo 七维动作", "urls": []},
                           site="direction")
    assert [c["solution_id"] for c in ve.sol_calls] == [None, LIBERO_SOLUTION_ID]
    assert [r["id"] for r in ret["solution_hits"]] == ["sol-l", "sol-a"]
    top = ret["solution_hits"][0]
    assert top["source_route"] == SOURCE_ROUTE_SOLUTIONS
    assert top["scoped_solution_id"] == LIBERO_SOLUTION_ID
    assert top["relpath"].startswith("solutions/")
    assert all("source_route" not in r for r in ve.solutions)


def test_solution_missing_table_fail_closed_and_tolerated():
    ve = FakeVectorEngine(cap=[_kb_row("ka", 0.1)], missing_solutions=True)
    planner, _ = _planner(ve, FakeLearningStore("phase1_learnings"))
    with pytest.raises(RuntimeError, match="solution_chunks"):
        planner.retrieve("track1",
                         {"name": "自研内核 Kinematica-Z9", "urls": []},
                         site="direction")
    ret = planner.retrieve("track1",
                           {"name": "自研内核 Kinematica-Z9", "urls": []},
                           site="direction",
                           allow_missing_solutions=True)
    assert ret["solution_hits"] == []
    assert [r["id"] for r in ret["kb_hits"]] == ["ka"]  # 其余路不受影响


def test_learning_route_merges_stores_missing_table_empty_and_dedup():
    main_rows = [_lr_row("L1")]
    extra_ok = FakeLearningStore(
        "phase2_learnings", [_lr_row("L1"), _lr_row("L2", rung=3)])
    extra_missing = FakeLearningStore("ghost_table", exists=False)
    ve = FakeVectorEngine(cap=[_kb_row("ka", 0.1)])
    planner, _ = _planner(
        ve, FakeLearningStore("phase1_learnings", main_rows),
        extra=(extra_ok, extra_missing))
    ret = planner.retrieve("track1",
                           {"name": "自研内核 Kinematica-Z9", "urls": []},
                           site="direction")
    hits = ret["learning_hits"]
    assert [r["id"] for r in hits] == ["L1", "L2"]  # 主库优先，跨库 id 去重
    labels = {r["id"]: r["learning_table"] for r in hits}
    assert labels == {"L1": "phase1_learnings", "L2": "phase2_learnings"}
    assert all(r["source_route"] == SOURCE_ROUTE_LEARNINGS for r in hits)
    assert all(r["relpath"] == r["evidence_relpath"] for r in hits)
    # 缺表 store 不发 query（table_exists 闸在 query 之前）。
    assert extra_missing.queries == []


def test_merge_lanes_distance_tiebreak_dedup_truncate():
    rows = [
        [{"id": "a", "_distance": 0.5}, {"id": "b", "_distance": 0.2}],
        [{"id": "b", "_distance": 0.2}, {"id": "c"}],  # 重复 id + 缺距离
        [{"id": "d", "_distance": None}, {"id": "a2", "_distance": 0.5}],
    ]
    out = PlannerRecall._merge_lanes(rows, top_k=3)
    # b(.2) < a(.5) 与 a2(.5) 同距按 id 词典序 tiebreak；缺距离排末尾；截 3。
    assert [r["id"] for r in out] == ["b", "a", "a2"]
    assert PlannerRecall._merge_lanes([], top_k=3) == []


def test_retrieve_hints_and_topk_validation():
    ve = FakeVectorEngine(cap=[_kb_row("ka", 0.1)])
    planner, _ = _planner(ve, FakeLearningStore("phase1_learnings"))
    with pytest.raises(ValueError, match="site"):
        planner.retrieve_with_hints(
            {"kb": "q", "solutions": "q", "learnings": "q"}, site="bogon")
    with pytest.raises(ValueError, match="hints"):
        planner.retrieve_with_hints(
            {"kb": "  ", "solutions": "q", "learnings": "q"},
            site="direction")
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            PlannerRecall(ProductionRecall(
                FakeLearningStore("phase1_learnings"), ve), kb_top_k=bad,
                rules_version="r3")


def test_record_adoption_delegates_single_writer(tmp_path):
    ve = FakeVectorEngine(cap=[_kb_row("ka", 0.1)])
    planner, prod = _planner(ve, FakeLearningStore("phase1_learnings"))
    assert planner.vector_engine is ve and planner.prod is prod
    log = tmp_path / "recall" / "production_recall.jsonl"
    text = "KB 正文 ka。"
    prompt = json.dumps([{"role": "user", "content": f"前缀\n{text}\n后缀"}],
                        ensure_ascii=False)
    # 未知路名写入侧 fail-closed（前向兼容只在读侧）。
    with pytest.raises(ValueError, match="source_route"):
        planner.record_adoption(
            thread_id="t6herm01", site="direction", learning_id="ka",
            prompt_text=prompt, learning_text=prompt_embedded_text(text),
            source_route="future_route", path=log)
    assert not log.exists()
    # 七字段新行。
    assert planner.record_adoption(
        thread_id="t6herm01", site="direction", learning_id="ka",
        prompt_text=prompt, learning_text=prompt_embedded_text(text),
        source_route=SOURCE_ROUTE_KB, path=log) is True
    # 旧调用点（source_route 缺省）仍是恰好六字段。
    assert planner.record_adoption(
        thread_id="t6herm02", site="direction", learning_id="ka",
        prompt_text=prompt, learning_text=prompt_embedded_text(text),
        path=log) is True
    rows = iter_adoption_rows(log)
    assert tuple(rows[0].keys()) == LOG_FIELDS_V2
    assert tuple(rows[1].keys()) == LOG_FIELDS
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# C) 三段 prompt 注入（AC-T6-5）
# ---------------------------------------------------------------------------

def _kb_hit(rid, *, text=None):
    return {"id": rid, "text": text or f"KB 正文 {rid} 多行\n第二行 {rid}。",
            "kb_relpath": f"knowledge/{rid}.md", "section_path": f"§{rid}",
            "doc_type": "background"}


def _sol_hit(rid, *, chunk_index=1, text=None):
    return {"id": rid,
            "text": text or f"import libero as {rid}\nprint({rid!r})  # 代码窗",
            "relpath": f"solutions/track1_tools/03_libero/scripts/{rid}.py",
            "kind": "code", "chunk_index": chunk_index,
            "solution_id": LIBERO_SOLUTION_ID}


def _lr_hit(rid):
    return {"id": rid, "text": f"避坑叙述 {rid}：EGL 与 setrlimit。",
            "run_id": f"run-{rid}", "gate_source": "human_failure_agent_run",
            "rung": 0}


def test_three_sections_headers_empty_omission_contiguous_ids():
    sections, n = _planner_sections([_kb_hit("k1"), _kb_hit("k2")],
                                    [_sol_hit("s1", chunk_index=1)],
                                    [_lr_hit("l1")])
    assert PLANNER_SECTION_KB in sections
    assert PLANNER_SECTION_SOLUTIONS in sections
    assert PLANNER_SECTION_LEARNINGS in sections
    for marker in ("[S1]", "[S2]", "[S3]", "[L1 "):
        assert marker in sections
    assert "[S4]" not in sections and "[L2]" not in sections
    assert n == 3  # kb+solution 可引用总数；learning 不占 [Si]

    empty_sections, n0 = _planner_sections([_kb_hit("k1")], [], [])
    assert PLANNER_SECTION_KB in empty_sections
    assert PLANNER_SECTION_SOLUTIONS not in empty_sections
    assert PLANNER_SECTION_LEARNINGS not in empty_sections
    assert n0 == 1


def test_section_texts_verbatim_and_locators():
    kb = _kb_hit("k1")
    sol = _sol_hit("s1", chunk_index=7)
    lr = _lr_hit("l1")
    sections, _ = _planner_sections([kb], [sol], [lr])
    # kb/sol 文本逐字（代码窗换行保留，不压行）。
    assert kb["text"] in sections
    assert sol["text"] in sections
    assert lr["text"] in sections
    assert f"[来源: kb {kb['kb_relpath']}#{kb['section_path']}]" in sections
    assert (f"[来源: solutions {sol['relpath']}#code/7]" in sections)
    assert f"(solution_id={LIBERO_SOLUTION_ID})" in sections
    # planner 路命中行带 learning_table 标签（由召回编排统一加注）。
    lr_tagged = dict(lr)
    lr_tagged["learning_table"] = "phase1_learnings"
    sections_lr, _ = _planner_sections([], [], [lr_tagged])
    assert "learning_table=phase1_learnings" in sections_lr
    assert "learning_id=l1" in sections and "run_id=run-l1" in sections


def test_evidence_refs_resolve_across_unified_kb_solution_hits():
    catalog = load_engine_catalog()
    slug = sorted(catalog)[0]
    cite_hits = [_kb_hit("k1"), _kb_hit("k2"),
                 _sol_hit("s1"), _sol_hit("s2", chunk_index=2)]

    def proposal(refs, idx):
        return {"name": f"候选方案{chr(65 + idx)}", "sim_engine": slug,
                "first_principles": "第一性原理推导 [S3]。",
                "key_params": "时间步长 0.5ms",
                "controls_vs_experiment": "Baseline vs Ours 对照",
                "evidence_refs": refs, "risk": "接触穿透",
                "expected_artifacts": ["probe.mp4", "metrics.json"]}

    obj = {"question": "合成题", "proposals": [proposal(["S3"], 0),
                                               proposal(["S1", "S4"], 1)]}
    props, errors = validate_proposals(obj, hits=cite_hits, catalog=catalog)
    # S3 解析到 solution 行（kb+solution 连续编号），双候选全部通过。
    assert errors == [] and len(props) == 2
    assert props[0]["evidence_refs"] == ["S3"]

    obj_bad = {"question": "合成题",
               "proposals": [proposal(["S1"], 0), proposal(["S5"], 1)]}
    _, errors_bad = validate_proposals(obj_bad, hits=cite_hits, catalog=catalog)
    # 越界引用的报错固定口径：[S1]..[S{n}]（n=4），不含 S5。
    assert any("evidence_refs 非法" in e and "[S4]" in e
               and "S5" not in e for e in errors_bad)


def test_policy_requirement_markers_and_track1_task_prose():
    for marker in ("动作从哪来", "policy_source", "scripted_servo",
                   "official_demo_policy", "opensource_vla", "training",
                   "7-DOF delta EEF", "Stage 0..3"):
        assert marker in _POLICY_REQUIREMENT
    payload = {"name": "LIBERO 机械臂", "urls": ["http://example/a"]}
    probe = _planner_track1_user(payload, "【基线理论先验(kb)】\nx",
                                 tier=TIER_PROBE)
    policy = _planner_track1_user(payload, "【基线理论先验(kb)】\nx",
                                  tier=TIER_POLICY_CLOSED_LOOP)
    assert _POLICY_REQUIREMENT not in probe
    assert _POLICY_REQUIREMENT in policy
    # probe 档任务散文与旧 _track1_user 同源逐字。
    old = build_first_messages(
        track="track1", payload=payload, hits=[_kb_hit("k1")],
        catalog=load_engine_catalog())[1]["content"]
    new = build_planner_first_messages(
        track="track1", payload=payload, kb_hits=[_kb_hit("k1")],
        solution_hits=[], learning_hits=[],
        catalog=load_engine_catalog(), tier=TIER_PROBE)[0][1]["content"]
    # spine 之前（标题/工具/任务段/canonical spine）逐字节相同。
    assert old.split("【知识库证据】")[0] == \
        new.split(PLANNER_SECTION_KB)[0]


# ---------------------------------------------------------------------------
# D) setter 新旧双支路（AC-T6-6）
# ---------------------------------------------------------------------------

class ScriptedMacroGateway:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def model_id(self, stage):
        return "fixture-macro-redacted-0001"

    def call(self, stage, messages, *, max_tokens, temperature, timeout=None):
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens, "temperature": temperature})
        return self.raw, (100, 200)


def _valid_proposals_raw():
    catalog = load_engine_catalog()
    slugs = sorted(catalog)[:2]
    return json.dumps({
        "question": "合成题目",
        "proposals": [{
            "name": f"方案{chr(65 + i)}", "sim_engine": slug,
            "first_principles": f"第一性原理接触模型推导 [S1]（候选 {i+1}）。",
            "key_params": "时间步长 0.5ms；摩擦系数 0.3",
            "controls_vs_experiment": "Baseline vs Ours 参数鲁棒性对照",
            "evidence_refs": ["S1"],
            "risk": "接触穿透；最小闭环探针验证",
            "expected_artifacts": [f"probe_{i}.mp4", f"metrics_{i}.json"]}
            for i, slug in enumerate(slugs)]}, ensure_ascii=False)


def _evidence_noop(stem, kind, payload):
    return f"phase1/task2_direction/{stem}.{kind}.json"


def test_setter_legacy_path_byte_identical_single_call_no_adoption(tmp_path):
    hits = [_kb_hit("k1"), _kb_hit("k2")]

    class LegacyVE:
        def __init__(self):
            self.queries = []

        def query_kb(self, query, *, namespace, top_k):
            self.queries.append(
                {"query": query, "namespace": namespace, "top_k": top_k})
            return [dict(h) for h in hits]

    ve = LegacyVE()
    gw = ScriptedMacroGateway(_valid_proposals_raw())
    setter = DirectionSetter(gw, ve, evidence_writer=_evidence_noop,
                             call_cache=_cache(tmp_path))
    tool = "自研多体动力学内核 Kinematica-Legacy"
    payload = {"name": tool, "urls": []}
    result = setter.run_direction(
        track="track1", payload=payload, thread_id="t6legacy1",
        batch_id="t6-herm-batch")

    # 每候选轮恰好 1 次 macro 调用；查询串逐字节旧字面量。
    assert len(gw.calls) == 1
    assert ve.queries == [{
        "query": f"{tool} 架构 物理引擎 特性 闭环仿真",
        "namespace": "capabilities", "top_k": 8}]
    # 消息逐字节等于旧 builder（三段函数完全不参与）。
    expected = build_first_messages(
        track="track1", payload=payload, hits=hits,
        catalog=setter.catalog)
    assert gw.calls[0]["messages"] == expected
    assert "【方案实录" not in gw.calls[0]["messages"][1]["content"]
    assert len(result["proposals"]) == 2
    # 旧路径没有任何采纳行（recall/planner 均未注入）。
    assert count_adoptions() == 0


def _cache(tmp_path):
    from orchestrator.common.callcache import CallReplay
    return CallReplay(tmp_path / ".call_cache")


def test_setter_planner_mode_three_sections_and_route_telemetry(tmp_path):
    kb = [_kb_hit("k1"), _kb_hit("k2")]
    sol = [_sol_hit("s1", chunk_index=1)]
    lr = [_lr_hit("l1")]
    ve = FakeVectorEngine(cap=kb, solutions=sol)
    main_store = FakeLearningStore("phase1_learnings", lr)
    planner, _ = _planner(ve, main_store)
    gw = ScriptedMacroGateway(_valid_proposals_raw())
    setter = DirectionSetter(gw, ve, planner_recall=planner,
                             evidence_writer=_evidence_noop,
                             call_cache=_cache(tmp_path))
    result = setter.run_direction(
        track="track1",
        payload={"name": "LIBERO 机械臂操作", "urls": []},
        thread_id="t6planner1", batch_id="t6-herm-batch")
    assert len(gw.calls) == 1
    user = gw.calls[0]["messages"][1]["content"]
    assert PLANNER_SECTION_KB in user
    assert PLANNER_SECTION_SOLUTIONS in user
    assert PLANNER_SECTION_LEARNINGS in user
    assert all(h["text"] in user for h in kb + sol + lr)
    # kb_sources 带来源路标签；结果件校验按 kb+sol 四路总数（S1..S3）。
    routes = {s["source_route"] for s in result["kb_sources"]}
    assert routes == {SOURCE_ROUTE_KB, SOURCE_ROUTE_SOLUTIONS}
    # 逐字渲染的 4 行 → 四条七字段采纳行，三路齐全。
    rows = iter_adoption_rows()
    assert len(rows) == 4
    by_route = {}
    for r in rows:
        assert tuple(r.keys()) == LOG_FIELDS_V2
        by_route.setdefault(r["source_route"], []).append(r["learning_id"])
    assert set(by_route) == {SOURCE_ROUTE_KB, SOURCE_ROUTE_SOLUTIONS,
                             SOURCE_ROUTE_LEARNINGS}
    assert len(by_route[SOURCE_ROUTE_KB]) == 2
    # 旧字段六行契约的前六键仍在。
    assert all(set(r) >= set(LOG_FIELDS) for r in rows)


def test_setter_planner_invalid_tier_rejects_before_call(tmp_path):
    ve = FakeVectorEngine(cap=[_kb_hit("k1")])
    planner, _ = _planner(ve, FakeLearningStore("phase1_learnings"))
    gw = ScriptedMacroGateway(_valid_proposals_raw())
    setter = DirectionSetter(gw, ve, planner_recall=planner,
                             evidence_writer=_evidence_noop,
                             call_cache=_cache(tmp_path))
    with pytest.raises(DirectionError, match="planner_tier"):
        setter.run_direction(
            track="track1",
            payload={"name": "自研内核 Kinematica-Q", "urls": []},
            thread_id="t6badtier", batch_id="t6-herm-batch",
            planner_tier="not_a_tier")
    assert gw.calls == []


# ---------------------------------------------------------------------------
# E) 采纳遥测/闭环机判读侧（AC-T6-8）
# ---------------------------------------------------------------------------

def _log_row(**over):
    row = {"ts": utc_now_iso_z(), "thread_id": "t6herm-telemetry",
           "site": "direction", "learning_id": "id-1",
           "prompt_sha256": "a" * 64,
           "adopted_text_sha256": "b" * 64}
    row.update(over)
    return row


def test_row_source_route_four_forms():
    assert row_source_route(_log_row()) == SOURCE_ROUTE_UNKNOWN        # 缺字段
    assert row_source_route(_log_row(source_route="")) == SOURCE_ROUTE_UNKNOWN
    assert row_source_route(_log_row(source_route="  ")) == SOURCE_ROUTE_UNKNOWN
    assert row_source_route(
        _log_row(source_route="future_route")) == SOURCE_ROUTE_UNKNOWN
    assert row_source_route(
        _log_row(source_route=SOURCE_ROUTE_KB)) == SOURCE_ROUTE_KB


def test_iter_adoption_rows_six_and_seven_field_and_corrupt(tmp_path):
    log = tmp_path / "production_recall.jsonl"
    good = "\n".join(json.dumps(r, ensure_ascii=False)
                     for r in (_log_row(), _log_row(source_route="kb")))
    log.write_text(good + "\n", encoding="utf-8")
    rows = iter_adoption_rows(log)
    assert [tuple(r.keys()) for r in rows] == [LOG_FIELDS, LOG_FIELDS_V2]
    assert count_adoptions(log) == 2

    bad_log = tmp_path / "bad1.jsonl"
    bad_log.write_text(good + "\n" + "{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        iter_adoption_rows(bad_log)

    extra_key = _log_row(source_route="kb", unexpected=1)
    bad_log2 = tmp_path / "bad2.jsonl"
    bad_log2.write_text(json.dumps(extra_key) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        iter_adoption_rows(bad_log2)

    bad_ts = _log_row(ts="2026-10-31T08:00:00+0800")  # 非严格 Z
    bad_log3 = tmp_path / "bad3.jsonl"
    bad_log3.write_text(json.dumps(bad_ts) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        iter_adoption_rows(bad_log3)


def test_audit_adoption_rows_hermetic_forms_and_ghosts():
    closure = _load_module(
        "t6_check_recall_closure",
        "scripts/verification/check_recall_closure.py")
    id_sets = {"kb": {"kb-1"}, "solutions": {"sol-1"},
               "learnings": {"lr-1", "legacy-1"}}
    rows = [
        _log_row(learning_id="legacy-1"),                       # 旧行缺字段
        _log_row(learning_id="lr-1", source_route=""),          # 空字段
        _log_row(learning_id="x", source_route="future_x"),     # 未知合法值
        _log_row(learning_id="kb-1", source_route="kb"),
        _log_row(learning_id="sol-1", source_route="solutions"),
        _log_row(learning_id="lr-1", source_route="learnings"),
        _log_row(learning_id="ghost-kb", source_route="kb"),   # ghost
        _log_row(learning_id="legacy-1"),                       # 旧行 id 在学习并集
    ]
    audit = closure.audit_adoption_rows(rows, id_sets=id_sets)
    assert audit["n"] == 8
    assert audit["form_detail"] == {
        "missing_route": 2, "empty_route": 1, "unknown_value_route": 1}
    assert audit["unknown_values"] == {"future_x": 1}
    by = audit["by_route"]
    assert by["kb"] == 2 and by["solutions"] == 1 \
        and by["learnings"] == 1 and by["unknown"] == 4
    ghosts = {(g["route"], g["learning_id"]) for g in audit["ghost"]}
    assert ghosts == {("kb", "ghost-kb"), ("unknown", "x")}
    with pytest.raises(ValueError, match="id_sets"):
        closure.audit_adoption_rows(rows, id_sets={"kb": set()})


# ---------------------------------------------------------------------------
# F) ingest_solutions --expect-learnings 必填（AC-T6-7）
# ---------------------------------------------------------------------------

def test_ingest_solutions_expect_learnings_required(tmp_path):
    env = dict(os.environ)
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "SIMPRAXIS_PHASE2_ROOT": str(tmp_path / "p2")})
    proc = subprocess.run(
        [sys.executable, "-m", "memory_flywheel.scripts.ingest_solutions"],
        capture_output=True, text=True, timeout=180, env=env, cwd=REPO_ROOT)
    # argparse required：缺参在任何 lancedb 连接之前退出码 2。
    assert proc.returncode == 2, proc.stderr
    assert "--expect-learnings" in proc.stderr


# ---------------------------------------------------------------------------
# G) gold@8 机判脚本冻结契约（AC-T6-4 机判本身，0 付费）
# ---------------------------------------------------------------------------

def test_golden_fixture_frozen_contract():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == "simpraxis.phase25.planner_golden.v1"
    assert data["frozen_at"] == "2026-10-31"
    assert data["thresholds"] == {
        "overall": 0.80, "solution_route": 0.75, "learning_route": 0.75}
    assert data["allowed_repair"]["max_iterations_after_first"] == 1
    assert set(data["allowed_repair"]["changeable_files"]) == {
        "orchestrator/direction/synonyms.yaml",
        "orchestrator/direction/planner_recall.py"}
    scope = data["allowed_repair"]["scope"]
    for forbidden in ("改 case 标签", "阈值", "gold id"):
        assert forbidden in scope
    cases = data["cases"]
    assert len(cases) >= 25 and len({c["id"] for c in cases}) == len(cases)
    from collections import Counter
    counts = Counter(c["route"] for c in cases)
    assert counts == {"solutions": 18, "kb": 17, "learnings": 12}
    for c in cases:
        assert c["track"] in ("track1", "track2")
        assert c["tier"] in (TIER_PROBE, TIER_POLICY_CLOSED_LOOP)
        assert "gold_id" in c or "gold_text" in c
        if "gold_id" in c:
            assert len(c["gold_id"]) == 32
            int(c["gold_id"], 16)
    assert sum("hard_negative" in c for c in cases) >= 10


class _ScriptedPlanner:
    """eval.evaluate 的生产同构替身：按 case 是否在 hit_set 给 top1 命中。"""

    def __init__(self, cases, hit_ids):
        self.cases = cases
        self.hit_ids = set(hit_ids)

    def retrieve(self, track, payload, *, tier, site):
        q = payload.get("question") or payload.get("name")
        case = next(c for c in self.cases if c["query"] == q)
        key = {"kb": "kb_hits", "solutions": "solution_hits",
               "learnings": "learning_hits"}[case["route"]]
        # 真实 fixture 的 learnings 路多为 gold_text 指纹：替身同时给 id/text，
        # evaluate 的 gold_id 与 gold_text 两判据都能中。
        if case["id"] in self.hit_ids:
            row = [{"id": case.get("gold_id", f"synthetic-{case['id']}"),
                    "text": case.get("gold_text", "x")}]
        else:
            row = []
        return {"hints": {}, "kb_hits": row if key == "kb_hits" else [],
                "kb_capabilities_hits": [], "kb_troubleshooting_hits": [],
                "solution_hits": row if key == "solution_hits" else [],
                "learning_hits": row if key == "learning_hits" else []}


def _eval_mod():
    return _load_module("t6_eval_planner_recall",
                        "scripts/phase25/eval_planner_recall.py")


def _build_r3_planner(mod):
    """按判分器同一装配本地构造 **r3** 生产 planner（Stage B 暴露的锚点缺口）。

    `mod.build_planner()` 是冻结件（sha `e73fe8b8…`）且不透传 `rules_version`
    ——这是预注册 §6.3 修订 A2 明写的事实（`--rules-version` 只是报告标签）。
    Stage B 把 `PlannerRecall` 的构造函数默认翻成 `r4` 后，任何经
    `build_planner()` 装配的测试都会拿到 r4；本测断言的是 **r3 语义**
    （逐案复现冻结 iter3 `f1c0e7f4…`），所以必须显式钉 `rules_version="r3"`。
    装配逐字同 `build_planner()`（同一 VectorEngine + phase1 主库 + phase2
    额外库 + 同一 solution 表存在性前置），只多一个版本参数。
    """
    ve = mod.VectorEngine()
    ls = mod.LearningStore()
    es = mod.EvolutionStore()
    assert ve.solution_table_exists()
    assert ls.table_exists() and es.table_exists()
    prod = mod.ProductionRecall(ls, ve, extra_learning_stores=(es,))
    return mod.PlannerRecall(prod, rules_version="r3")


def test_eval_load_cases_rejects_bad_fixtures(tmp_path):
    mod = _eval_mod()
    good = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert mod.load_cases(FIXTURE)["schema"] == good["schema"]

    def reject(doc, fragment):
        p = tmp_path / f"bad-{fragment}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError):
            mod.load_cases(p)

    reject({**good, "schema": "x"}, "schema")
    short = {**good, "cases": good["cases"][:10]}
    reject(short, "toofew")
    bad_route = json.loads(json.dumps(good))
    bad_route["cases"][0]["route"] = "bogus"
    reject(bad_route, "route")
    no_gold = json.loads(json.dumps(good))
    for k in ("gold_id", "gold_text"):
        no_gold["cases"][0].pop(k, None)
    reject(no_gold, "nogold")
    no_th = {**good, "thresholds": {"overall": 0.8}}
    reject(no_th, "threshold")


def test_eval_rc0_pass_and_rc3_fail_and_rc2_setup(tmp_path, monkeypatch):
    mod = _eval_mod()
    good = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # 直接用冻结 47 case：三路 17/18/12 均非空，全命中时三个比值恒为 1.0。
    fixture = tmp_path / "gold47.json"
    fixture.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "report.json"

    # 全命中 → rc 0。
    monkeypatch.setattr(mod, "build_planner",
                        lambda: _ScriptedPlanner(good["cases"],
                                                 {c["id"] for c in good["cases"]}))
    assert mod.run(["--fixture", str(fixture), "--json-out", str(out),
                    "--iteration", "1"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["metrics"]["all_pass"] is True
    assert stat.S_IMODE(out.stat().st_mode) == 0o600

    # 全不命中 → rc 3（判据性失败，STOP），阈值/标签不动。
    monkeypatch.setattr(mod, "build_planner",
                        lambda: _ScriptedPlanner(good["cases"], set()))
    out3 = tmp_path / "report3.json"
    assert mod.run(["--fixture", str(fixture), "--json-out", str(out3),
                    "--iteration", "2"]) == 3
    report3 = json.loads(out3.read_text(encoding="utf-8"))
    assert report3["metrics"]["overall"]["gold_at_8"] == 0.0
    assert report3["metrics"]["all_pass"] is False

    # 装配前失败 → rc 2（fixture 不存在）。
    assert mod.run(["--fixture", str(tmp_path / "missing.json"),
                    "--json-out", str(tmp_path / "x.json")]) == 2


# ---------------------------------------------------------------------------
# H) 活库测试（kb_integration：SIMPRAXIS_KB_INTEGRATION=1 才跑，只读 0 付费）
# ---------------------------------------------------------------------------

LIBERO_VLA_QUERIES = [
    "LIBERO stage1 setrlimit RLIMIT_NOFILE MUJOCO_GL egl OffScreenRenderEnv import",
    "LIBERO servo 七维动作 np.zeros(7) a[:3] 平移 a[5] yaw a[6] 夹爪 clip",
    "LIBERO 24 角度 clearance 扫描 pick_offset_angle run_episode 单控制环",
    "LIBERO bddl_file_name 大写谓词 resolve_target 区域 plate ramekin 均值",
    "LIBERO finger_joint pad tip geom_xpos finger_axis_err 指轴角",
    "LIBERO align_yaw 符号反转 restore set_init_state attempt_grasp 闭爪抬升",
    "LIBERO held rise 0.05 trail 拖尾 分轴运输 反应式下放 check_success",
    "LIBERO get_benchmark libero_spatial OffScreenRenderEnv TypeError horizon",
    "LIBERO results judge libero_official telemetry episode manifest",
    "LIBERO incomplete suite sys.exit cover imageio 收尾",
    "VLA LIBERO policy action 7-DOF delta EEF gripper 归一化 Stage 切分",
    "OpenVLA LIBERO 动作接口 夹爪 归一化 action space 闭环",
]

# AC-T6-4 r1/r2 时代 mpnet/768 封存负结论（iter1/iter2 同数：35/47、
# 12 miss；r2 作用域 lane 零入座，生产并集 3/12；攢批119 9c894eb）。
# 攢批121 b5444be r3 作用域保底配额落地后活路径值前进（gold 38/0.8085、
# 生产并集 8/12，r2 三块零丢失、单调 +5），但 121 收口只跑 hermetic，
# kb_integration 活门自 119 起未再执行；攢批126 双轨改造在 qwen 活轨落盘，
# mpnet 分支直到 2026-11-01 回滚后首次活跑才暴露这组常量已不能在活路径复现。
# 锁内适配（攢批128，fcb3fcd6 §3.4 缺口双轨适配，2026-11-01 用户当日裁决
# 授权双轨）：FROZEN_MISSES（r1/r2 12 miss）随 iter1/iter2 报告文件证据链
# 永久对账（见 test_live_iter_reports_frozen_and_chained）；PROD_UNION_3 在
# mpnet 轨保留为「r3 保底席只增不丢」的单调包含常量（不回改、不假装活可复现）；
# r3 活值一律锚机产报告/快照（iter3 f1c0e7f4、task6_r3_mpnet_prod_union
# _snapshot.json 攢批128 补注册），零手写几何第二事实源；qwen 分支一字不动，
# qwen 活证据仅存窗口门日志与 iter4 报告（预注册 §8 禁回填）。
FROZEN_MISSES = {
    "kb_check_success", "kb_execmode", "kb_16d_layout", "kb_pipolicy",
    "sol_win1_env", "sol_win2_servo", "sol_win3_clearance",
    "sol_win4_region", "sol_win5_finger", "sol_win6_align", "sol_win10_exit",
    "l_fluid_wall"}
PROD_UNION_3 = {"2e7d3ac99db9", "7f9bdabc4e72", "8fc302da5604"}


def _embed_track() -> str:
    """按活 config 模型全名选轨（不认模型即硬红，不 skip 不猜）。

    锁内适配（Phase 2.5 换嵌入，冻结预注册 fcb3fcd6 §3.4 覆盖缺口，
    2026-11-01 用户当日裁决双轨适配）：范式同
    tests/milestone1/test_learning_isolation.py 的 _embed_track()。
    """
    model = str(select_embedding().get("model_name", ""))
    if model.endswith(_MPNET_MODEL_SUFFIX):
        return "mpnet768"
    if model == gps.QWEN_MODEL:
        return "qwen1024"
    raise AssertionError(
        f"双轨测试只认 mpnet/Qwen3，活 config model_name={model!r} 未识别")


def _live_route_id_sets() -> dict:
    """活库四表真实 id 集（32 位全文，非 12 位前缀）；几何无关内容身份用。"""
    import lancedb
    from memory_flywheel import kb_pins
    db = lancedb.connect(kb_pins.default_lancedb_uri())

    def _ids(name: str) -> set[str]:
        rows = (db.open_table(name).search(None)
                .select(["id"]).limit(1_000_000).to_list())
        return {str(r["id"]) for r in rows}

    return {"kb": _ids("kb_chunks"),
            "solutions": _ids("solution_chunks"),
            "learnings": _ids("phase1_learnings") | _ids("phase2_learnings")}


# 攢批128：r3/mpnet 生产并集机产快照（scripts/phase25/gen_r3_prod_union_snapshot.py
# 同口径生成；r3 时代漏采、回滚后补注册，两独立 lane -m 重跑 --out /tmp cmp）。
R3_PROD_UNION_SNAPSHOT = (REPO_ROOT / "scripts" / "phase25" / "snapshots"
                          / "task6_r3_mpnet_prod_union_snapshot.json")
# verifier MINOR-1（攢批128）：注册件自身防篡改对位（两 lane 第五/六次重跑 cmp 同值）。
_R3_PROD_UNION_SNAPSHOT_SHA256 = (
    "53c043dc202c7de8c611fb523ee842546b47f9d5cbbbaf72633064fcbf98cc7b")
_ITER3_REPORT_SHA256 = (
    "f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307")


def _load_r3_prod_union_snapshot() -> dict:
    raw = R3_PROD_UNION_SNAPSHOT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _R3_PROD_UNION_SNAPSHOT_SHA256
    return json.loads(raw.decode("utf-8"))


def _live_table_id_shas() -> dict:
    """活四表 sorted(id) 换行 join sha256（同 gen_prewindow 与并集快照口径）。"""
    import lancedb
    from memory_flywheel import kb_pins
    db = lancedb.connect(kb_pins.default_lancedb_uri())
    out = {}
    for name in ("kb_chunks", "solution_chunks", "phase1_learnings",
                 "phase2_learnings"):
        rows = (db.open_table(name).search(None)
                .select(["id"]).limit(1_000_000).to_list())
        ids = sorted(str(r["id"]) for r in rows)
        out[name] = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return out


def _load_frozen_iter3_report() -> dict:
    """iter3 r3 机产报告（唯一一份，0600，sha 钉死 f1c0e7f4）。"""
    from orchestrator.common.runtime_paths import phase25_root
    rdir = phase25_root() / "task6_recall"
    r3 = sorted(rdir.glob("planner_gold_iter3_*.json"))
    assert len(r3) == 1, f"expect exactly one iter3 report, got {r3}"
    p = r3[0]
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert hashlib.sha256(p.read_bytes()).hexdigest() == _ITER3_REPORT_SHA256
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.kb_integration
def test_live_four_table_invariants_1542_44_94_1(monkeypatch):
    require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    import lancedb
    from memory_flywheel import kb_pins
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.phase2.evolution_store import EvolutionStore
    from memory_flywheel.vector_engine import VectorEngine

    db = lancedb.connect(kb_pins.default_lancedb_uri())
    assert db.open_table("kb_chunks").count_rows() == 1542
    assert db.open_table("solution_chunks").count_rows() == 94
    assert LearningStore().count() == 44
    assert EvolutionStore().count() == 1
    # 活库只读红线：四路召回全程不建表、不写块。
    ve = VectorEngine()
    assert ve.solution_table_exists() is True


@pytest.mark.kb_integration
def test_live_task6_3_libero_scoped_seam_12_prod_union_3(monkeypatch):
    """AC-T6-3 双轨：作用域缝并集 12/12 两轨皆活（几何无关内容覆盖）；
    mpnet 轨生产距离合并活值锚攢批128 机产快照（r3 规则 8/12；r1/r2 的
    3/12 保留为 PROD_UNION_3 单调包含常量），qwen 轨只保留几何无关真断言
    （fcb3fcd6 §3.4 缺口双轨适配，2026-11-01 用户当日裁决；qwen 入座活值
    禁人手填，归 iter4/门日志）。"""
    require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.phase2.evolution_store import EvolutionStore
    from memory_flywheel.vector_engine import VectorEngine
    from orchestrator.direction.planner_recall import PlannerRecall

    ve = VectorEngine()
    union_seam: set[str] = set()
    for q in LIBERO_VLA_QUERIES:
        for r in ve.query_solutions(
                q, top_k=8, solution_id=LIBERO_SOLUTION_ID):
            union_seam.add(str(r["id"])[:12])
    assert union_seam == LIBERO_12_PREFIXES  # 作用域缝 12/12 全覆盖

    planner = PlannerRecall(ProductionRecall(
        LearningStore(), ve, extra_learning_stores=(EvolutionStore(),)),
        rules_version="r3")   # 历史锚点钉子（task6_r4_prereg.md §10）
    union_prod: set[str] = set()
    live_per_query: dict[str, list[str]] = {}
    for q in LIBERO_VLA_QUERIES:
        ret = planner.retrieve("track1", {"name": q, "urls": []},
                               tier=TIER_PROBE, site="direction")
        hits8 = ret["solution_hits"][:8]
        live_per_query[q] = [str(r["id"]) for r in hits8]
        union_prod |= {str(r["id"])[:12] for r in hits8}
    track = _embed_track()
    if track == "mpnet768":
        # r3（攢批121 b5444be）作用域保底配额后生产合并入座 8/12：r1/r2 封存
        # 三席（PROD_UNION_3，攢批119）零丢失、保底席单调 +5。r3 时代该活值
        # 漏采（121 收口仅 hermetic），攢批128 在回滚保真的 mpnet+r3 活库
        # 补注册机产快照；以下等式全部锚机产件，零手写几何第二事实源。
        snap = _load_r3_prod_union_snapshot()
        head = snap["header"]
        assert head["config_model"].endswith(_MPNET_MODEL_SUFFIX)
        assert head["config_dim"] == 768
        assert head["rules_version"] == R3_RULES_VERSION
        assert head["solution_id"] == LIBERO_SOLUTION_ID
        assert head["top_k"] == 8
        assert snap["source_queries"] == LIBERO_VLA_QUERIES
        assert head["table_rows"] == {
            "kb_chunks": 1542, "solution_chunks": 94,
            "phase1_learnings": 44, "phase2_learnings": 1}
        assert head["table_id_set_sha256"] == _live_table_id_shas()
        assert set(snap["scoped_seam_prefixes"]) == LIBERO_12_PREFIXES
        assert {x["query"] for x in snap["prod_union"]["per_query"]} \
            == set(LIBERO_VLA_QUERIES)
        snap_per_query = {x["query"]: x["solution_hit_ids"]
                          for x in snap["prod_union"]["per_query"]}
        assert live_per_query == snap_per_query  # 全 32 位 id 有序逐字
        assert union_prod == set(
            snap["prod_union"]["union_solution_prefixes_all"])
        inter = union_prod & LIBERO_12_PREFIXES
        assert inter == set(
            snap["prod_union"]["intersection_scoped12_prefixes"])
        # r1/r2 历史封存：r3 保底席只增不丢，原三席必须仍在座。
        assert PROD_UNION_3 <= inter
    else:
        # qwen 轨：几何相关等式随换型退役（活证据归 iter4/门日志，禁回填，
        # fcb3fcd6 §8）；以下均为几何无关真断言（非 X∩S⊆S 式恒真式）：
        # 端到端真有 solution 入座、每个入座 id 在活 solution_chunks 真实
        # 存在（内容身份）、每问至多 8 块的基数界。
        live_solution_prefixes = {
            x[:12] for x in _live_route_id_sets()["solutions"]}
        assert union_prod  # 非空＝端到端真返回
        assert union_prod <= live_solution_prefixes
        assert len(union_prod) <= len(LIBERO_VLA_QUERIES) * 8


def _assert_gold_eval_geometry_free(data: dict, results: list, metrics: dict):
    """qwen 轨几何无关不变量（零 qwen 几何活值；fcb3fcd6 §3.4 缺口双轨适配）。

    端到端实跑 evaluate/summarize 后，只断言与嵌入几何无关的事实：fixture
    组成数、阈值单一源、计数/比率/合取内部自洽、miss⊆案集、每条 top8 id 都是
    该路由活表真实行（跨表污染检测）、rank/matched_by 自洽。qwen 下几何数值
    一律不 assert——Stage A 活证据归窗口门日志与 iter4 绑定报告。
    """
    cases = data["cases"]
    th = data["thresholds"]
    case_ids = {c["id"] for c in cases}
    route_totals = {"kb": 0, "solutions": 0, "learnings": 0}
    for c in cases:
        route_totals[c["route"]] += 1
    # 组成事实（冻结 fixture 构成，几何无关）：47 案 17/18/12。
    assert route_totals == {"kb": 17, "solutions": 18, "learnings": 12}
    assert len(cases) == metrics["n_cases"] == 47
    assert len(results) == 47

    blocks = [("overall", None), ("solution_route", "solutions"),
              ("learning_route", "learnings")]
    for block_name, route in blocks:
        blk = metrics[block_name]
        total = (47 if route is None else route_totals[route])
        assert blk["total"] == total
        assert 0 <= blk["hits"] <= total
        assert blk["gold_at_8"] == (round(blk["hits"] / total, 4) if total else 0.0)
        assert blk["threshold"] == th[block_name]
        assert blk["pass"] == (blk["gold_at_8"] >= blk["threshold"])
        counted = sum(1 for r in results
                      if (route is None or r["route"] == route) and r["hit"])
        assert blk["hits"] == counted
    assert metrics["kb_route_observed"]["total"] == route_totals["kb"]
    assert metrics["kb_route_observed"]["hits"] == sum(
        1 for r in results if r["route"] == "kb" and r["hit"])
    assert metrics["all_pass"] == (metrics["overall"]["pass"]
                                   and metrics["solution_route"]["pass"]
                                   and metrics["learning_route"]["pass"])
    assert len(metrics["misses"]) == (metrics["overall"]["total"]
                                      - metrics["overall"]["hits"])
    assert {m["id"] for m in metrics["misses"]} <= case_ids

    live_ids = _live_route_id_sets()
    for r in results:
        assert r["id"] in case_ids
        assert len(r["top8_ids"]) <= 8
        assert set(r["top8_ids"]) <= live_ids[r["route"]]
        if r["hit"]:
            assert isinstance(r["rank"], int) and 1 <= r["rank"] <= 8
            assert r["matched_by"] in ("gold_id", "gold_text")
        else:
            assert r["rank"] is None and r["matched_by"] is None


@pytest.mark.kb_integration
def test_live_gold_eval_frozen_negative_recompute(monkeypatch):
    """AC-T6-4 双轨：mpnet 轨活库重算须逐案逐字等于 iter3 r3 机产报告
    （f1c0e7f4；攢批119 的 35/0.7447 是 r1/r2 负结论，只随 iter1/iter2 报告
    文件证据链对账，见 test_live_iter_reports_frozen_and_chained——攢批128
    修正：126 曾误把 r1/r2 常量钉在 r3 活路径，回滚后首跑曝光）；
    qwen 轨端到端实跑但只断言几何无关不变量（fcb3fcd6 §3.4 缺口双轨适配，
    2026-11-01 用户当日裁决；qwen 几何活值禁人手填，活证据归 iter4）。"""
    require_kb_integration(data=True)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")
    track = _embed_track()
    mod = _eval_mod()
    data = mod.load_cases(FIXTURE)
    planner = _build_r3_planner(mod)
    results = mod.evaluate(planner, data["cases"])
    metrics = mod.summarize(data, results)
    if track == "mpnet768":
        d3 = _load_frozen_iter3_report()
        assert d3["iteration"] == 3 and d3["rules_version"] == "r3"
        m3 = d3["metrics"]
        assert metrics["overall"] == m3["overall"]
        assert metrics["solution_route"] == m3["solution_route"]
        assert metrics["learning_route"] == m3["learning_route"]
        assert metrics["kb_route_observed"] == m3["kb_route_observed"]
        assert metrics["all_pass"] is m3["all_pass"]
        assert {m["id"] for m in metrics["misses"]} == {
            m["id"] for m in m3["misses"]}
        # 47 案逐案活算重放：hit/rank/matched_by/top8_ids 有序全等——
        # 回滚保真度的最强机判（非仅聚合数相等）。
        frozen_cases = {c["id"]: c for c in d3["cases"]}
        assert set(frozen_cases) == {r["id"] for r in results}
        for r in results:
            f = frozen_cases[r["id"]]
            assert r["hit"] is f["hit"]
            assert r["rank"] == f["rank"]
            assert r["matched_by"] == f["matched_by"]
            assert r["top8_ids"] == f["top8_ids"]
    else:
        _assert_gold_eval_geometry_free(data, results, metrics)


@pytest.mark.kb_integration
def test_live_iter_reports_frozen_and_chained():
    """iter1/iter2 报告留痕：r1/r2 规则 mpnet/768 封存负结论（35/47、12 miss）
    的永久证据链。

    锁内适配（fcb3fcd6 §3.4 缺口双轨适配，2026-11-01 用户当日裁决；
    攢批128 补正锚点错配）：r1/r2 的 35/47、12 miss 从「对活库重算」改为
    「对 iter1/iter2 不可变机产报告逐字对账」——锚定 sha 的物证反篡改强度
    只增不减，且与活库几何解耦。活路径自攢批121 起为 r3（38/0.8085），
    其活等式在 test_live_gold_eval_frozen_negative_recompute 锚 iter3 报告
    f1c0e7f4、在 seam 测试锚 r3 机产快照；r2 生产并集 PROD_UNION_3 则保留为
    r3 单调包含常量。qwen 活证据归 iter4，本测不 glob。
    """
    require_kb_integration(data=True)
    from orchestrator.common.runtime_paths import phase25_root
    rdir = phase25_root() / "task6_recall"
    r1 = sorted(rdir.glob("planner_gold_iter1_*.json"))
    r2 = sorted(rdir.glob("planner_gold_iter2_*.json"))
    assert len(r1) == 1 and len(r2) == 1
    for p in (*r1, *r2):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    d1 = json.loads(r1[0].read_text(encoding="utf-8"))
    d2 = json.loads(r2[0].read_text(encoding="utf-8"))
    assert d1["iteration"] == 1 and d1["rules_version"] == "r1"
    assert d2["iteration"] == 2 and d2["rules_version"] == "r2"
    assert [i["iteration"] for i in d2["iterations"]] == [1, 2]
    for d in (d1, d2):
        m = d["metrics"]
        assert m["overall"] == {
            "hits": 35, "total": 47, "gold_at_8": 0.7447,
            "threshold": 0.80, "pass": False}
        assert m["solution_route"] == {
            "hits": 11, "total": 18, "gold_at_8": 0.6111,
            "threshold": 0.75, "pass": False}
        assert m["learning_route"] == {
            "hits": 11, "total": 12, "gold_at_8": 0.9167,
            "threshold": 0.75, "pass": True}
        assert m["kb_route_observed"] == {"total": 17, "hits": 13}
        assert m["all_pass"] is False
        assert {x["id"] for x in m["misses"]} == FROZEN_MISSES
