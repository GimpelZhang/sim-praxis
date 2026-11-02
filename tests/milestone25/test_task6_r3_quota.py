#!/usr/bin/env python3
"""Phase 2.5 Task6 r3 — 作用域保底配额合并 hermetic 测试（纯合成，0 付费）。

实现合同：scripts/phase25/task6_r3_prereg.md §5.1（保底席只给 S-only 行、
q=floor(top_k/2)）、§5.5 强制用例①–⑥与测试污染封口、§5.6 NOTES 1–4。

纪律：全部 id/文本/查询串均为本文件自造合成件（syn_*/SYNX）；不 import、
不读取任何冻结标注集或历史评测报告；不触活库、不联网；本文件
不含 kb_integration 用例（活库证据只来自后续冻结评测报告）。
"""
from __future__ import annotations

import pytest
import yaml

from memory_flywheel.recall import (
    SOURCE_ROUTE_KB,
    SOURCE_ROUTE_SOLUTIONS,
)
from orchestrator.direction import planner_recall as pr
from orchestrator.direction.planner_recall import (
    R3_RULES_VERSION,
    PlannerRecall,
    _annotate,
    build_route_hints,
    matched_projects,
)

K = 8
Q = K // 2

SYN_SOLUTION_ID = "syn_sol_1"
SYN_KB_TAG_1 = "syn_tag_p1"
SYN_KB_TAG_2 = "syn_tag_p2"


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """断网三开关：任何 embedding 下载尝试都应直接失败（本件零 embed）。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")


# ---------------------------------------------------------------------------
# 自含合成件：最小 fake engine（仅 query_kb/query_solutions）+ 空学习库
# ---------------------------------------------------------------------------

def _raw(rid, distance, **extra):
    row = {"id": rid, "text": f"syn body {rid} 9137", "_distance": distance}
    row.update(extra)
    return row


def _kb_raw(rid, distance, *, project="syn_global"):
    return {"id": rid, "text": f"syn kb body {rid} 2051",
            "kb_relpath": f"syn_kb/{rid}.md", "section_path": "syn/s1",
            "doc_type": "guide", "project": project,
            "namespace": "capabilities", "_distance": distance}


def _sol_raw(rid, distance, *, solution_id=SYN_SOLUTION_ID):
    return {"id": rid, "text": f"syn sol body {rid}\nprint({rid!r})",
            "relpath": f"syn_repo/{rid}.py", "kind": "code",
            "chunk_index": 1, "solution_id": solution_id,
            "_distance": distance}


class _FakeEngine:
    """记录调用参数、回放合成命中；只实现 query_kb/query_solutions。"""

    def __init__(self, *, cap=(), tb=(), scoped_kb=None,
                 solutions=(), scoped_sol=None):
        self._cap = list(cap)
        self._tb = list(tb)
        self._scoped_kb = scoped_kb or {}
        self._solutions = list(solutions)
        self._scoped_sol = scoped_sol or {}
        self.kb_calls = []
        self.sol_calls = []

    def query_kb(self, query, *, namespace=None, project=None,
                 top_k=K, **kwargs):
        self.kb_calls.append({"query": query, "namespace": namespace,
                              "project": project, "top_k": top_k})
        if project is not None:
            return [dict(r) for r in self._scoped_kb.get(project, ())]
        if namespace == "capabilities":
            return [dict(r) for r in self._cap]
        if namespace == "troubleshooting":
            return [dict(r) for r in self._tb]
        raise AssertionError(f"unexpected kb probe namespace={namespace!r}")

    def query_solutions(self, query, *, top_k=K, solution_id=None):
        self.sol_calls.append({"query": query, "solution_id": solution_id,
                               "top_k": top_k})
        if solution_id is not None:
            return [dict(r) for r in self._scoped_sol.get(solution_id, ())]
        return [dict(r) for r in self._solutions]


class _MissingLearningStore:
    """空学习库（缺表语义）：learning 路恒 []，r3 不引入学习配额。"""

    def table_exists(self):
        return False

    def query(self, hint, top_k=K):
        return []


class _FakeProd:
    """PlannerRecall 构造所需的最小 ProductionRecall 形状（只持句柄）。"""

    def __init__(self, engine):
        self.vector_engine = engine
        self.learning_store = _MissingLearningStore()
        self.extra_learning_stores = ()


def _planner(engine, **kwargs):
    # 本文件全部是 r3 保底配额语义锚点；r4 默认上线后显式钉 r3
    # （task6_r4_prereg.md §10，断言一字不改）。
    kwargs.setdefault("rules_version", "r3")
    return PlannerRecall(_FakeProd(engine), **kwargs)


# ---------------------------------------------------------------------------
# §5.5 强制用例①：G∩S 双属行即使排在 S 序最前也不占保底席
# ---------------------------------------------------------------------------

def test_case1_dual_membership_row_never_takes_reserved_seat():
    # X 同时在 G/S 且在 S 距离序第 1；q=4 个保底席必须全是 S-only 行；
    # X 仅在开放段被 G 距离序带入（第 5 位），且保持全局注解形态。
    x_global = _raw("x_dual", 0.05, shape="syn_global")
    global_lanes = [[x_global] + [
        _raw(f"g{i}", 0.10 + 0.10 * i, shape="syn_global")
        for i in range(1, 9)]]
    x_scoped = _raw("x_dual", 0.05, shape="syn_global",
                    scoped_project=SYN_KB_TAG_1)
    scoped_lanes = [[x_scoped] + [
        _raw(f"s_only_{c}", 0.20 + 0.01 * i, scoped_project=SYN_KB_TAG_1)
        for i, c in enumerate("abcd")]]

    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, K)
    ids = [r["id"] for r in out]
    assert ids == ["s_only_a", "s_only_b", "s_only_c", "s_only_d",
                   "x_dual", "g1", "g2", "g3"]
    # 前 q 行全是 S-only 且带来源作用域键。
    assert all(r.get("scoped_project") == SYN_KB_TAG_1 for r in out[:Q])
    # X 来自全局 lane：同一对象、全局注解形态（NOTES-2），全表仅出现一次。
    assert out[4] is x_global
    assert "scoped_project" not in out[4]
    assert ids.count("x_dual") == 1


# ---------------------------------------------------------------------------
# §5.5 强制用例②：S-only 不足 q 时保底少席、开放段按 G 距离序回填至 8
# ---------------------------------------------------------------------------

def test_case2_reserved_underfilled_backfilled_from_global_order():
    # 双属行 X 在 S 中距离最近但不占保底；S-only 仅 2 个（< q=4）。
    global_lanes = [[_raw("x_dual", 0.05, shape="syn_global")] + [
        _raw(f"g{i}", 0.30 + 0.10 * i, shape="syn_global")
        for i in range(1, 8)]]
    scoped_lanes = [[
        _raw("x_dual", 0.02, shape="syn_global",
             scoped_project=SYN_KB_TAG_1),
        _raw("s_only_a", 0.15, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_b", 0.16, scoped_project=SYN_KB_TAG_1),
    ]]

    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, K)
    assert [r["id"] for r in out] == [
        "s_only_a", "s_only_b", "x_dual", "g1", "g2", "g3", "g4", "g5"]
    assert len(out) == K  # 开放段补齐到 8
    assert all("scoped_project" not in r for r in out[2:])


# ---------------------------------------------------------------------------
# §5.5 强制用例③：projects 为空时输出 == r2 _merge_lanes（kb/solutions 两路）
# ---------------------------------------------------------------------------

def test_case3_empty_projects_equals_r2_merge_on_both_routes():
    cap = [_kb_raw("g_cap1", 0.10), _kb_raw("g_cap2", 0.60)]
    tb = [_kb_raw("g_tb1", 0.20)]
    sols = [_sol_raw("gs1", 0.30), _sol_raw("gs2", 0.70)]
    engine = _FakeEngine(cap=cap, tb=tb, solutions=sols)
    planner = _planner(engine)

    # kb 路：None 与 [] 两种空形态都不发作用域探针，合并结果逐 dict 等于 r2。
    for empty in (None, []):
        merged, cap_hits, tb_hits = planner._kb_route("syn query 4102", empty)
        assert merged == PlannerRecall._merge_lanes([cap_hits, tb_hits], K)
    assert all(c["project"] is None for c in engine.kb_calls)
    assert all(c["top_k"] == K for c in engine.kb_calls)
    assert len(engine.kb_calls) == 4  # 两次调用各双 namespace，零 scoped

    # solutions 路：用与生产完全相同的注解形态复算 r2 全局 lane。
    for empty in (None, []):
        got = planner._solution_route("syn query 4102", empty)
        global_lane = [[
            _annotate(r, route=SOURCE_ROUTE_SOLUTIONS,
                      relpath=r.get("relpath"))
            for r in sols]]
        assert got == PlannerRecall._merge_lanes(global_lane, K)
        assert [c["solution_id"] for c in engine.sol_calls] == [None]
        assert all(c["top_k"] == K for c in engine.sol_calls)
        engine.sol_calls.clear()

    # 纯函数层退化等式（kb 风格双 lane + 空 scoped 的两种空形态）。
    lanes = [[_kb_raw("a", 0.2)], [_kb_raw("b", 0.1)]]
    assert PlannerRecall._merge_with_scoped_quota(lanes, [], K) \
        == PlannerRecall._merge_lanes(lanes, K)
    assert PlannerRecall._merge_with_scoped_quota(lanes, [[]], K) \
        == PlannerRecall._merge_lanes(lanes, K)
    # 路由常量形态自证：kb 路全局注解不带 scoped_project 键。
    assert all("scoped_project" not in r
               for r in planner._kb_route("syn query 4102", None)[0])


# ---------------------------------------------------------------------------
# §5.5 强制用例④：按 id 去重（两条 scoped 探针同 id + G∩S 双属）
# ---------------------------------------------------------------------------

def test_case4_dedup_by_id_across_scoped_probes_and_dual_membership():
    global_lanes = [[
        _raw("s_dual", 0.08, shape="syn_global"),
        _raw("g1", 0.30, shape="syn_global"),
        _raw("g2", 0.31, shape="syn_global"),
        _raw("g3", 0.32, shape="syn_global"),
    ]]
    scoped_lanes = [
        [_raw("s_dual", 0.05, shape="syn_global",
              scoped_project=SYN_KB_TAG_1)],
        [_raw("s_dual", 0.05, shape="syn_global",
              scoped_project=SYN_KB_TAG_2),
         _raw("s_only_a", 0.20, scoped_project=SYN_KB_TAG_2)],
    ]

    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, K)
    ids = [r["id"] for r in out]
    assert len(ids) == len(set(ids))  # 无重复占席
    assert ids == ["s_only_a", "s_dual", "g1", "g2", "g3"]  # 实际并集 5 行
    # s_dual 是 G∩S：不进保底段，仅以全局形态出现一次。
    assert "scoped_project" not in out[1]
    # s_only_a 唯一副本保留其作用域注解。
    assert out[0]["scoped_project"] == SYN_KB_TAG_2


# ---------------------------------------------------------------------------
# §5.5 强制用例⑤：段顺序——前 |R| 行恰为保底段，其后为 G 距离序开放段
# ---------------------------------------------------------------------------

def test_case5_segment_order_reserved_then_open_distance_sorted():
    global_rows = [_raw(f"g{i}", 0.40 + 0.01 * i, shape="syn_global")
                   for i in range(1, 9)]
    scoped = [
        _raw("s_only_f", 0.90, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_b", 0.30, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_a", 0.30, scoped_project=SYN_KB_TAG_2),  # 同距 str(id)
        _raw("s_only_c", 0.31, scoped_project=SYN_KB_TAG_2),
        _raw("s_only_d", 0.32, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_e", 0.33, scoped_project=SYN_KB_TAG_2),
    ]

    out = PlannerRecall._merge_with_scoped_quota(
        [global_rows], [scoped], K)
    ids = [r["id"] for r in out]
    # 保底段：S-only 距离序前 q=4（0.30 同距按 str(id)，a 在 b 前）。
    assert ids[:Q] == ["s_only_a", "s_only_b", "s_only_c", "s_only_d"]
    # 开放段：全部来自 G，严格按 G 的 (_distance, str(id)) 序取满剩余 4 席。
    suffix = out[Q:]
    assert {r["id"] for r in suffix} <= {f"g{i}" for i in range(1, 9)}
    g_sorted = PlannerRecall._sort_dedup_lanes([global_rows])
    assert [r["id"] for r in suffix] == [r["id"] for r in g_sorted[:Q]]
    assert all("scoped_project" not in r for r in suffix)


# ---------------------------------------------------------------------------
# §5.5 强制用例⑥：生产同构——retrieve() 与 retrieve_with_hints() 三路逐行相等
# ---------------------------------------------------------------------------

def test_case6_retrieve_isomorphic_with_retrieve_with_hints(
        tmp_path, monkeypatch):
    # 合成同义词典（走 load_synonyms 校验；只声明 solution_id，不声明
    # kb_projects——solution 作用域探针即足以证明非空 projects 走配额路径）。
    syn_doc = {
        "schema": "simpraxis.planner_synonyms.v1",
        "projects": [{
            "id": "syn_proj",
            "aliases": ["SYNX"],
            "expansion": "syn expansion 合成扩展词 7731",
            "solution_id": SYN_SOLUTION_ID,
        }],
        "tiers": {"probe": ""},
    }
    syn_path = tmp_path / "syn_synonyms.yaml"
    syn_path.write_text(
        yaml.safe_dump(syn_doc, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(pr, "SYNONYMS_PATH", syn_path)  # retrieve() 读同一件

    engine = _FakeEngine(
        cap=[_kb_raw("g_cap1", 0.40), _kb_raw("g_cap2", 0.41)],
        tb=[_kb_raw("g_tb1", 0.42)],
        solutions=[_sol_raw("gs1", 0.50), _sol_raw("gs2", 0.51)],
        scoped_sol={SYN_SOLUTION_ID: [
            _sol_raw("s_only_sol", 0.12, solution_id=SYN_SOLUTION_ID)]})
    planner = _planner(engine)

    payload = {"question": "SYNX 合成问句 delta-2051"}
    # 前置：合成项目在未扩展 base 上命中（matched_projects 非空）。
    projects = matched_projects("track2", payload, data=syn_doc)
    assert [p["id"] for p in projects] == ["syn_proj"]
    hints = build_route_hints("track2", payload, data=syn_doc)

    via_hints = planner.retrieve_with_hints(
        hints, matched_projects=projects)
    via_retrieve = planner.retrieve("track2", payload)  # 生产主入口

    assert via_retrieve["hints"] == via_hints["hints"]
    for key in ("kb_hits", "kb_capabilities_hits",
                "kb_troubleshooting_hits", "solution_hits", "learning_hits"):
        assert via_retrieve[key] == via_hints[key], key
    # 两次调用探针形态一致：solution 路各一次全局 + 一次作用域（无 eval 分支）。
    assert [c["solution_id"] for c in engine.sol_calls] == \
        [None, SYN_SOLUTION_ID, None, SYN_SOLUTION_ID]
    assert all(c["top_k"] == K for c in engine.sol_calls)
    # 非空 projects 确实走了 r3 配额：S-only 方案行保底前置。
    sol_hits = via_retrieve["solution_hits"]
    assert sol_hits[0]["id"] == "s_only_sol"
    assert sol_hits[0]["scoped_solution_id"] == SYN_SOLUTION_ID


# ---------------------------------------------------------------------------
# 边界用例：规则版本常量 / 缺距离排末尾 / 奇数 k 取 floor / 候选不足 8
# ---------------------------------------------------------------------------

def test_r3_rules_version_constant_frozen():
    assert R3_RULES_VERSION == "r3"


def test_boundary_missing_distance_sorts_last_in_both_segments():
    global_lanes = [[
        _raw("g1", 0.10, shape="syn_global"),
        _raw("g2", None, shape="syn_global"),
    ]]
    scoped_lanes = [[
        _raw("s_only_a", None, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_b", 0.90, scoped_project=SYN_KB_TAG_1),
    ]]
    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, K)
    # 保底段内有距离者在前；开放段内缺距离的 g2 排末尾（inf 单一源）。
    assert [r["id"] for r in out] == [
        "s_only_b", "s_only_a", "g1", "g2"]


def test_boundary_odd_top_k_uses_floor_half_quota():
    global_lanes = [[_raw(f"g{i}", 0.50 + 0.01 * i, shape="syn_global")
                     for i in range(1, 8)]]
    scoped_lanes = [[_raw(f"s_only_{c}", 0.10 + 0.01 * i,
                          scoped_project=SYN_KB_TAG_1)
                     for i, c in enumerate("abc")]]
    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, 5)  # k=5 → q=2
    assert [r["id"] for r in out] == [
        "s_only_a", "s_only_b", "g1", "g2", "g3"]
    assert len(out) == 5


def test_boundary_fewer_than_top_k_returns_actual_union_no_padding():
    # G 仅 3 行、S-only 2 行（无交集）：并集 5 行全部返回，不填充不报错。
    global_lanes = [[_raw(f"g{i}", 0.40 + 0.10 * i, shape="syn_global")
                     for i in range(1, 4)]]
    scoped_lanes = [[
        _raw("s_only_a", 0.10, scoped_project=SYN_KB_TAG_1),
        _raw("s_only_b", 0.20, scoped_project=SYN_KB_TAG_1),
    ]]
    out = PlannerRecall._merge_with_scoped_quota(
        global_lanes, scoped_lanes, K)
    assert [r["id"] for r in out] == [
        "s_only_a", "s_only_b", "g1", "g2", "g3"]
    assert len(out) == 5
    # 空输入边界：两路皆空返回 []。
    assert PlannerRecall._merge_with_scoped_quota([], [], K) == []
