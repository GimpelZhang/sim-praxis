#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 词法通道与秩融合（全部离线、0 付费、合成件）。

预注册 ``scripts/phase25/task6_r4_prereg.md`` §3/§4.5 的 hermetic 验收：

  A 术语抽取（§3.1）：ASCII 大小写敏感 + 长度 ≥4、下划线起首、CJK 4-gram
    滑窗与短段不产术语、去重且字典序；
  B BM25（§3.2）：n/df/avgdl、idf 公式（闭式手推）、长度归一化方向
    （短行胜出）、tf 子串计数、零分行不入结果、等分按 ``str(id)`` 升序；
  C RRF（§3.3）：k=60 等权、名次从 1 起、平局按 ``str(id)``、参数校验；
  D 退化等价（§3.3 硬性质）：词法侧空 ⇒ 各路输出与 r3 **逐 dict 相等**；
  E 生产接线（§3.3/§3.4）：词法查询串取**未扩展 base hint**（扩展词不得
    捞行）、词法独有行补 source_route/relpath/lexical_rank、稠密行注解不变、
    learning 路第 9 行（phase2 演化志）尾席保持且不重复；
  F 缓存与失效（§3.6）：同一实例复用索引，行数变化即重建、重建后可查到新行；
  G 版本契约（§3.7）：常量冻结、非法版本 fail-closed、r3 分支绝不触
    count_rows/scan_rows（历史锚点零扰动）。

纪律：不读活库、不载嵌入模型、不触网；数值断言来自 §3 规格的独立推导
（闭式公式 + 小语料手算），不复用 r3 期望值。
"""
from __future__ import annotations

import math

import pytest

from memory_flywheel.recall import ProductionRecall
from orchestrator.direction.lexical_index import (
    BM25_B,
    BM25_K1,
    CJK_NGRAM,
    DEFAULT_TOP_K,
    RRF_K,
    Bm25Index,
    rrf_fuse,
    tokenize_terms,
)
from orchestrator.direction.planner_recall import (
    DEFAULT_RULES_VERSION,
    R3_RULES_VERSION,
    R4_RULES_VERSION,
    SUPPORTED_RULES_VERSIONS,
    PlannerRecall,
)

IDF_ALPHA = math.log(1.0 + (2 - 2 + 0.5) / (2 + 0.5))     # N=2, df=2 → ln(1.2)


# ---------------------------------------------------------------- fakes

class _Table:
    def __init__(self, name):
        self.name = name


class _FakeStore:
    """最小 LearningStore 形状：query/scan_rows/count/table_exists/table。"""

    def __init__(self, name, rows=(), *, exists=True):
        self.name = name
        self.rows = [dict(r) for r in rows]
        self.exists = exists
        self.scans = 0
        self.queries = []

    def table_exists(self):
        return self.exists

    def table(self, create=False):
        return _Table(self.name)

    def count(self):
        return len(self.rows) if self.exists else 0

    def query(self, hint, top_k=5):
        self.queries.append((hint, top_k))
        if not self.exists:
            return []
        return [dict(r) for r in self.rows[:top_k]]

    def scan_rows(self, columns, *, limit=10 ** 6):
        self.scans += 1
        if not self.exists:
            return []
        return [{c: r.get(c) for c in columns} for r in self.rows]


class _FakeEngine:
    """最小 VectorEngine 形状：稠密查询 + 只读扫描缝（r4 语料）。"""

    def __init__(self, *, kb_corpus=(), kb_hits=(), sol_corpus=(),
                 sol_hits=(), scoped_kb=None, scoped_sol=None,
                 missing_solutions=False):
        self.kb_corpus = [dict(r) for r in kb_corpus]
        self.sol_corpus = [dict(r) for r in sol_corpus]
        self.kb_hits = [dict(r) for r in kb_hits]
        self.sol_hits = [dict(r) for r in sol_hits]
        self.scoped_kb = scoped_kb or {}
        self.scoped_sol = scoped_sol or {}
        self.missing_solutions = missing_solutions
        self.count_calls = []
        self.scan_calls = []

    def count_rows(self, table_name):
        self.count_calls.append(table_name)
        if table_name == "kb_chunks":
            return len(self.kb_corpus)
        if table_name == "solution_chunks":
            return 0 if self.missing_solutions else len(self.sol_corpus)
        raise AssertionError(f"未预期表名 {table_name}")

    def scan_rows(self, table_name, columns, *, limit=10 ** 6):
        self.scan_calls.append(table_name)
        if table_name == "kb_chunks":
            rows = self.kb_corpus
        elif table_name == "solution_chunks":
            rows = [] if self.missing_solutions else self.sol_corpus
        else:
            raise AssertionError(f"未预期表名 {table_name}")
        return [{c: r.get(c) for c in columns} for r in rows]

    def query_kb(self, query, *, namespace=None, project=None,
                 doc_types=None, top_k=5, include_index=False):
        if project is not None:
            return [dict(r) for r in self.scoped_kb.get(project, [])]
        return [dict(r) for r in self.kb_hits
                if r.get("namespace", "capabilities") == namespace]

    def query_solutions(self, query, *, top_k=8, solution_id=None):
        if self.missing_solutions:
            raise RuntimeError(
                "表 solution_chunks 不存在（先运行 ingest_solutions）")
        if solution_id is not None:
            return [dict(r) for r in self.scoped_sol.get(solution_id, [])]
        return [dict(r) for r in self.sol_hits]


def _kb(rid, dist, *, text=None, ns="capabilities", project="global",
        doc_type="guide"):
    return {"id": rid, "text": text or f"KB 行 {rid} 的正文。",
            "_distance": dist, "namespace": ns, "project": project,
            "doc_type": doc_type, "kb_relpath": f"knowledge/{rid}.md"}


def _sol(rid, dist, *, text=None, solution_id="s1"):
    return {"id": rid, "text": text or f"方案块 {rid}。", "_distance": dist,
            "solution_id": solution_id, "relpath": f"solutions/{rid}.md"}


def _lrn(rid, *, text=None):
    return {"id": rid, "text": text or f"学习行 {rid}：避坑叙述。",
            "evidence_relpath": f"@runs/{rid}.json"}


def _make(engine, main_store, extra=(), **kw):
    prod = ProductionRecall(main_store, engine,
                            extra_learning_stores=tuple(extra))
    return PlannerRecall(prod, **kw)


# ---------------------------------------------------------------- A 术语抽取

def test_tokenize_ascii_case_sensitive_min_len4():
    terms = tokenize_terms("ExecuteMode executeMode EXECUTEMODE env obs act abc")
    assert "ExecuteMode" in terms
    assert "executeMode" in terms
    assert "EXECUTEMODE" in terms
    # 长度 3（含下划线起首）不产术语：正则要求首字符 + ≥3 后续字符
    assert "env" not in terms and "obs" not in terms and "act" not in terms
    assert "abc" not in terms


def test_tokenize_underscore_lead_and_digits():
    terms = tokenize_terms("_check_success pick_offset_angle np.zeros(7)")
    assert "_check_success" in terms          # 下划线起首合法
    assert "pick_offset_angle" in terms
    assert "zeros" in terms                   # np.zeros → 从 z 起首的 ASCII 段
    assert "np" not in terms                  # 长度不足


def test_tokenize_cjk_ngram_sliding_and_short_run():
    terms = tokenize_terms("流体穿透桌面")
    # 6 字连续段 → 3 个重叠 4-gram（sorted 按码点序）
    assert [t for t in terms if not t.isascii()] \
        == ["体穿透桌", "流体穿透", "穿透桌面"]
    assert tokenize_terms("壶杯") == []       # 段长 < 4 不产术语
    assert tokenize_terms("举壶") == []


def test_tokenize_dedup_sorted_and_mixed():
    terms = tokenize_terms("alpha alpha beta 流体穿透桌面 alpha")
    assert terms == sorted(set(terms))
    assert terms.count("alpha") == 1
    assert terms.index("alpha") < terms.index("beta")


def test_tokenize_empty_and_punct_only():
    assert tokenize_terms("") == []
    assert tokenize_terms("，。；：！？") == []


# ---------------------------------------------------------------- B BM25

def test_bm25_corpus_statistics():
    idx = Bm25Index([{"id": "a", "text": "alpha beta"},
                     {"id": "b", "text": "alpha"}])
    assert idx.n == 2
    assert idx.lengths == [10, 5] and idx.avgdl == 7.5
    assert idx.df["alpha"] == 2 and idx.df["beta"] == 1
    # 字典序决定内部序（不受传入顺序影响）
    assert idx.ids == ["a", "b"]


def test_bm25_idf_closed_form():
    idx = Bm25Index([{"id": "a", "text": "alpha beta"},
                     {"id": "b", "text": "alpha"}])
    assert idx._idf("alpha") == pytest.approx(IDF_ALPHA, rel=1e-12)
    # df=1, N=2 → ln(1 + 1.5/1.5) = ln 2
    assert idx._idf("beta") == pytest.approx(math.log(2.0), rel=1e-12)


def test_bm25_scores_match_hand_computation():
    """闭式手推：短行 b 因长度归一化胜过长行 a。"""
    idx = Bm25Index([{"id": "a", "text": "alpha beta"},
                     {"id": "b", "text": "alpha"}])
    norm_a = BM25_K1 * (1.0 - BM25_B + BM25_B * 10 / 7.5)      # 1.5
    norm_b = BM25_K1 * (1.0 - BM25_B + BM25_B * 5 / 7.5)       # 0.9
    want_b = IDF_ALPHA * (1 * (BM25_K1 + 1.0)) / (1 + norm_b)
    want_a = IDF_ALPHA * (1 * (BM25_K1 + 1.0)) / (1 + norm_a)
    got = idx.scores("alpha")
    assert [rid for _s, rid in got] == ["b", "a"]
    assert got[0][0] == pytest.approx(want_b, rel=1e-12)
    assert got[1][0] == pytest.approx(want_a, rel=1e-12)
    # 手算数值锚点（与实现无关的第三重校核）
    assert got[0][0] == pytest.approx(0.21111, abs=5e-5)
    assert got[1][0] == pytest.approx(0.16044, abs=5e-5)


def test_bm25_tf_is_substring_count():
    idx = Bm25Index([{"id": "a", "text": "alpha alpha"},
                     {"id": "b", "text": "alpha"}])
    got = dict((rid, s) for s, rid in idx.scores("alpha"))
    # a 出现 2 次但更长且在 N=2/df=2 下 idf 相同 → 逐行按公式复核
    norm_a = BM25_K1 * (1.0 - BM25_B + BM25_B * 11 / 8.0)   # avgdl=(11+5)/2
    want_a = IDF_ALPHA * (2 * (BM25_K1 + 1.0)) / (2 + norm_a)
    assert got["a"] == pytest.approx(want_a, rel=1e-12)


def test_bm25_zero_score_rows_excluded_and_unknown_terms_ignored():
    idx = Bm25Index([{"id": "a", "text": "alpha beta"},
                     {"id": "b", "text": "gamma"}])
    assert [rid for _s, rid in idx.scores("alpha")] == ["a"]
    # 语料里不存在的术语不改变结果（df=0 无 idf 定义）
    assert idx.scores("alpha quantum") == idx.scores("alpha")
    assert idx.scores("nothing_here") == []


def test_bm25_tie_break_by_str_id():
    idx = Bm25Index([{"id": "y", "text": "gamma"},
                     {"id": "x", "text": "gamma"}])
    assert [rid for _s, rid in idx.scores("gamma")] == ["x", "y"]


def test_bm25_empty_corpus_is_safe():
    idx = Bm25Index([])
    assert idx.n == 0 and idx.avgdl == 0.0
    assert idx.scores("alpha") == []


# ---------------------------------------------------------------- C RRF

def test_rrf_equal_weight_rank_from_one():
    fused = rrf_fuse(["a", "b"], [(1.0, "b")], top_k=8)
    scores = dict((rid, s) for s, rid in fused)
    assert scores["a"] == pytest.approx(1.0 / (RRF_K + 1), rel=1e-15)
    assert scores["b"] == pytest.approx(1.0 / (RRF_K + 2)
                                        + 1.0 / (RRF_K + 1), rel=1e-15)
    assert [rid for _s, rid in fused] == ["b", "a"]


def test_rrf_dense_order_preserved_when_lexical_disjoint():
    dense = ["d1", "d2", "d3"]
    assert [rid for _s, rid in rrf_fuse(dense, [], top_k=8)] == dense


def test_rrf_tie_break_and_top_k_truncation():
    # 两通道名次对称 → 同一 id 得分相同，按 str(id) 升序
    fused = rrf_fuse(["a"], [(1.0, "b")], top_k=8)
    assert [rid for _s, rid in fused] == ["a", "b"]      # 1/61 vs 1/61
    dense = [f"d{i}" for i in range(10)]
    assert len(rrf_fuse(dense, [], top_k=4)) == 4
    assert [rid for _s, rid in rrf_fuse(dense, [], top_k=4)] == dense[:4]


def test_rrf_validation():
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            rrf_fuse(["a"], [], top_k=bad)
        with pytest.raises(ValueError):
            rrf_fuse(["a"], [], k=bad)


# ---------------------------------------------------------------- G 版本契约

def test_rules_version_constants_frozen():
    assert R3_RULES_VERSION == "r3"
    assert R4_RULES_VERSION == "r4"
    assert SUPPORTED_RULES_VERSIONS == ("r3", "r4")
    assert DEFAULT_RULES_VERSION == "r4"
    assert DEFAULT_TOP_K == 8 and RRF_K == 60
    assert (BM25_K1, BM25_B, CJK_NGRAM) == (1.2, 0.75, 4)


def test_rules_version_invalid_fail_closed():
    ve = _FakeEngine()
    store = _FakeStore("phase1_learnings")
    for bad in ("r2", "r5", "", None, "R4"):
        with pytest.raises(ValueError, match="rules_version"):
            _make(ve, store, rules_version=bad)


# ---------------------------------------------------------------- D/E/F 接线

def _mini_engine(**kw):
    """kb 两行（一长一短）+ 一条 solution，稠密序 = 语料序子集。"""
    kb_corpus = [
        {"id": "kbA", "text": "LIBERO 的 pick_offset_angle 参数说明。",
         "doc_type": "guide", "kb_relpath": "knowledge/a.md"},
        {"id": "kbB", "text": "robosuite 环境通用说明。",
         "doc_type": "guide", "kb_relpath": "knowledge/b.md"},
        {"id": "kbIndex", "text": "目录索引 pick_offset_angle",
         "doc_type": "index", "kb_relpath": "knowledge/index.md"},
    ]
    sol_corpus = [
        {"id": "solA", "text": "抓取偏移角 pick_offset_angle 的实现。",
         "relpath": "solutions/track1/a.py"},
        {"id": "solB", "text": "无关脚本文本。", "relpath": "solutions/b.py"},
    ]
    return _FakeEngine(
        kb_corpus=kb_corpus, kb_hits=[_kb("kbB", 0.1), _kb("kbA", 0.2)],
        sol_corpus=sol_corpus, sol_hits=[_sol("solB", 0.1), _sol("solA", 0.3)],
        **kw)


def _base_payload():
    return {"question": "pick_offset_angle 这个参数怎么用？"}


def test_lexical_query_uses_unexpanded_base_hint():
    """词法通道锚在未扩展 base hint：只有扩展串才含的词不得捞行。"""
    engine = _mini_engine()
    # 诱饵：只有 LIBERO 扩展串里的 robosuite 能命中，稠密侧也不给这一行
    engine.kb_corpus.append(
        {"id": "kbRobo", "text": "robosuite 专属说明，正文无其他术语。",
         "doc_type": "guide", "kb_relpath": "knowledge/robo.md"})
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)                       # 默认 r4
    payload = {"question": "LIBERO 的 pick_offset_angle 怎么用？"}
    ret = planner.retrieve("track2", payload, tier="probe")
    kb_ids = [r["id"] for r in ret["kb_hits"]]
    assert "kbRobo" not in kb_ids                        # 扩展词不得经词法捞行
    assert "kbA" in kb_ids                               # base hint 术语正常命中


def test_lexical_only_row_annotations():
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    by_id = {r["id"]: r for r in ret["kb_hits"]}
    assert "kbA" in by_id and "kbB" in by_id
    # kbA 词法名次 1 → 融合后应进 top-8（此处稠密仅 2 行）
    assert [r["id"] for r in ret["kb_hits"]][0] == "kbA"
    # 稠密行保留 _distance 与无 lexical_rank
    assert by_id["kbB"].get("_distance") == pytest.approx(0.1)
    assert "lexical_rank" not in by_id["kbB"]
    assert by_id["kbB"]["source_route"] == "kb"
    assert by_id["kbB"]["relpath"] == "knowledge/kbB.md"


def test_lexical_only_row_is_lexically_sourced():
    """稠密只给 solB，词法给 solA → solA 以词法行形态进入且注解齐全。"""
    engine = _FakeEngine(
        kb_corpus=[], kb_hits=[],
        sol_corpus=[{"id": "solA", "text": "pick_offset_angle 实现。",
                     "relpath": "solutions/track1/a.py"}],
        sol_hits=[_sol("solB", 0.1)])
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    ids = [r["id"] for r in ret["solution_hits"]]
    assert ids[0] == "solA" and ids[1] == "solB"
    row = ret["solution_hits"][0]
    assert row["source_route"] == "solutions"
    assert row["relpath"] == "solutions/track1/a.py"
    assert row["lexical_rank"] == 1
    assert "vector" not in row and "_distance" not in row
    assert "pick_offset_angle" in row["text"]


def test_kb_index_doc_type_excluded_from_corpus():
    """目录性文档不进词法语料（与 query_kb include_index=False 同集）。"""
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    assert "kbIndex" not in {r["id"] for r in ret["kb_hits"]}


def test_degenerate_lexical_matches_r3_dict_for_dict():
    """§3.3 硬性质：词法侧空 ⇒ 三路输出与 r3 逐 dict 相等。"""
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings", [
        _lrn("l1"), _lrn("l2")])
    payload = {"question": "zzz 无术语 无中文长度四 0 0"}   # 词法零命中
    r4 = _make(engine, store, rules_version="r4")
    r4_ret = r4.retrieve("track2", payload, tier="probe")
    r3 = _make(engine, store, rules_version="r3")
    r3_ret = r3.retrieve("track2", payload, tier="probe")
    for key in ("kb_hits", "solution_hits", "learning_hits",
                "kb_capabilities_hits", "kb_troubleshooting_hits"):
        assert r4_ret[key] == r3_ret[key], key


def test_learning_tail_seat_preserved():
    """learning 路 9 行（主库 8 + 额外库 1）：融合只换前 8，第 9 行原位保留。"""
    main = _FakeStore("phase1_learnings", [
        _lrn(f"m{i}", text=f"第 {i} 条学习：流体穿透桌面。") for i in range(8)])
    extra = _FakeStore("phase2_learnings", [
        _lrn("evo", text="演化志：V2 通过但仍被 v1 支配。")])
    engine = _FakeEngine(kb_corpus=[], kb_hits=[], sol_corpus=[], sol_hits=[])
    r3 = _make(engine, main, (extra,), rules_version="r3")
    r3_hits = r3.retrieve("track2", _base_payload(), tier="probe")["learning_hits"]
    assert len(r3_hits) == 9 and r3_hits[-1]["id"] == "evo"

    r4 = _make(engine, main, (extra,), rules_version="r4")
    hits = r4.retrieve("track2", _base_payload(), tier="probe")["learning_hits"]
    assert len(hits) == 9
    assert [r["id"] for r in hits[:8]] != [] and len({r["id"] for r in hits}) == 9
    assert hits[-1]["id"] == "evo"          # 尾席保持 r3 序
    assert hits[-1]["learning_table"] == "phase2_learnings"


def test_learning_tail_not_duplicated_when_promoted():
    """词法把第 9 行提进 top-8 时，尾席不得重复注入同一 id。"""
    main = _FakeStore("phase1_learnings", [
        _lrn(f"m{i}", text=f"第 {i} 条学习：普通叙述。") for i in range(8)])
    extra = _FakeStore("phase2_learnings", [
        _lrn("evo", text="pick_offset_angle 演化志。")])
    engine = _FakeEngine(kb_corpus=[], kb_hits=[], sol_corpus=[], sol_hits=[])
    planner = _make(engine, main, (extra,), rules_version="r4")
    hits = planner.retrieve("track2", _base_payload(), tier="probe")["learning_hits"]
    ids = [r["id"] for r in hits]
    assert ids.count("evo") == 1


def test_cache_reused_within_instance():
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    planner.retrieve("track2", _base_payload(), tier="probe")
    scans_after_first = list(engine.scan_calls)
    planner.retrieve("track2", _base_payload(), tier="probe")
    assert engine.scan_calls == scans_after_first      # 未重建
    assert engine.scan_calls.count("kb_chunks") == 1


def test_cache_invalidated_by_row_count():
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    first = planner.retrieve("track2", _base_payload(), tier="probe")
    assert "kbNew" not in {r["id"] for r in first["kb_hits"]}
    engine.kb_corpus.append(
        {"id": "kbNew", "text": "pick_offset_angle 新块。",
         "doc_type": "guide", "kb_relpath": "knowledge/new.md"})
    second = planner.retrieve("track2", _base_payload(), tier="probe")
    assert engine.scan_calls.count("kb_chunks") == 2   # 行数变化 → 重建
    assert "kbNew" in {r["id"] for r in second["kb_hits"]}


def test_r3_path_never_touches_lexical_seams():
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store, rules_version="r3")
    planner.retrieve("track2", _base_payload(), tier="probe")
    assert engine.count_calls == [] and engine.scan_calls == []
    assert store.scans == 0
    assert planner._lexical_cache == {}


def test_r4_requires_explicit_lexical_hints():
    """§3.4 修订 A4：r4 缺省 lexical_hints 一律 fail-closed。

    原预注册「缺省 = hints（向后兼容）」与同节「词法通道一律用未扩展 base
    hint」矛盾：缺省路径会让**已扩展** hint 静默充当词法查询串。改为必填，
    不猜、不静默降级；生产入口 ``retrieve()`` 显式传三路同 base。
    """
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    with pytest.raises(ValueError, match="r4 必须显式传 lexical_hints"):
        planner.retrieve_with_hints(
            {"kb": "pick_offset_angle", "solutions": "pick_offset_angle",
             "learnings": "pick_offset_angle"}, site="direction")
    # 失败发生在任何检索之前：不查库、不建索引
    assert engine.count_calls == [] and engine.scan_calls == []
    assert planner._lexical_cache == {}
    # 传了就走通（同一 hint 作词法串）
    ret = planner.retrieve_with_hints(
        {"kb": "pick_offset_angle", "solutions": "pick_offset_angle",
         "learnings": "pick_offset_angle"}, site="direction",
        lexical_hints={"kb": "pick_offset_angle",
                       "solutions": "pick_offset_angle",
                       "learnings": "pick_offset_angle"})
    assert "solA" in {r["id"] for r in ret["solution_hits"]}


def test_r3_ignores_lexical_hints_entirely():
    """r3 分支无词法通道 ⇒ 该参数可选，传了也不得改变任何返回行。"""
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store, rules_version="r3")
    hints = {"kb": "pick_offset_angle", "solutions": "pick_offset_angle",
             "learnings": "pick_offset_angle"}
    plain = planner.retrieve_with_hints(hints, site="direction")
    with_hints = planner.retrieve_with_hints(
        hints, site="direction", lexical_hints=dict(hints))
    for key in ("kb_hits", "solution_hits", "learning_hits"):
        assert plain[key] == with_hints[key], key


def test_r4_rejects_empty_lexical_hint():
    engine = _mini_engine()
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    with pytest.raises(ValueError, match="lexical_hints"):
        planner.retrieve_with_hints(
            {"kb": "q", "solutions": "q", "learnings": "q"},
            site="direction",
            lexical_hints={"kb": "q", "solutions": " ", "learnings": "q"})


def test_r4_outputs_are_deterministic_across_instances():
    def run():
        engine = _mini_engine()
        store = _FakeStore("phase1_learnings", [_lrn("l1"), _lrn("l2")])
        planner = _make(engine, store)
        ret = planner.retrieve("track2", _base_payload(), tier="probe")
        return {k: [r["id"] for r in ret[k]]
                for k in ("kb_hits", "solution_hits", "learning_hits")}

    assert run() == run()


def test_r4_missing_solutions_table_is_tolerated():
    engine = _FakeEngine(kb_corpus=[], kb_hits=[],
                         missing_solutions=True)
    store = _FakeStore("phase1_learnings")
    planner = _make(engine, store)
    ret = planner.retrieve("track2", _base_payload(), tier="probe",
                           allow_missing_solutions=True)
    assert ret["solution_hits"] == []


# ------------------------------------------------- M1 词法行渲染字段完整性
# GATE1-LANE-A M1：词法通道把行提进 top-8 时，该行从 ``_lexical_corpus`` 原样
# 复制，不走稠密查询；语料扫描列缺一列，prompt 里就出现 `?` 占位（硬规则 7 的
# 「不得伪造来源」边界）。以下直接跑 setter 渲染器，断言渲染文本里没有 `?`。

def test_lexical_kb_row_carries_renderer_fields():
    from orchestrator.direction.setter import _planner_kb_block
    engine = _FakeEngine(
        kb_corpus=[{"id": "kbA", "text": "pick_offset_angle 参数说明。",
                    "doc_type": "guide", "kb_relpath": "knowledge/a.md",
                    "section_path": "Q3/LIBERO"},
                   {"id": "kbB", "text": "无关正文。", "doc_type": "guide",
                    "kb_relpath": "knowledge/b.md", "section_path": "Q1"}],
        # 稠密行按真实查询形状给全字段（缺列是**稠密**侧的假象，不是本测标的）
        kb_hits=[{"id": "kbB", "text": "无关正文。", "_distance": 0.1,
                  "namespace": "capabilities", "project": "global",
                  "doc_type": "guide", "kb_relpath": "knowledge/b.md",
                  "section_path": "Q1"}])
    planner = _make(engine, _FakeStore("phase1_learnings"))
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    row = {r["id"]: r for r in ret["kb_hits"]}["kbA"]
    assert row["kb_relpath"] == "knowledge/a.md"
    assert row["section_path"] == "Q3/LIBERO"
    assert row["doc_type"] == "guide"
    block = _planner_kb_block(ret["kb_hits"])
    assert "?" not in block
    assert "knowledge/a.md#Q3/LIBERO" in block


def test_lexical_solution_row_carries_renderer_fields():
    from orchestrator.direction.setter import _planner_solution_block
    engine = _FakeEngine(
        kb_corpus=[], kb_hits=[],
        sol_corpus=[{"id": "solA", "text": "pick_offset_angle 实现。",
                     "relpath": "solutions/track1/a.py", "kind": "code",
                     "chunk_index": 3, "solution_id": "track1_tools"}],
        # 稠密行按真实查询形状给全字段（缺列是稠密侧 fixture 的事）
        sol_hits=[{"id": "solB", "text": "无关脚本文本。", "_distance": 0.1,
                   "relpath": "solutions/b.py", "kind": "readme",
                   "chunk_index": 0, "solution_id": "track1_tools"}])
    planner = _make(engine, _FakeStore("phase1_learnings"))
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    row = {r["id"]: r for r in ret["solution_hits"]}["solA"]
    assert row["kind"] == "code" and row["chunk_index"] == 3
    assert row["solution_id"] == "track1_tools"
    block = _planner_solution_block(ret["solution_hits"], 1)
    assert "?" not in block
    assert "solutions/track1/a.py#code/3" in block
    assert "solution_id=track1_tools" in block


def test_lexical_learning_row_carries_renderer_fields():
    from orchestrator.direction.setter import _planner_learning_block
    dense = {"id": "lrnB", "text": "无关学习行。",
             "evidence_relpath": "@runs/b.json", "run_id": "run-b",
             "gate_source": "gate2", "rung": 2}
    lexical = {"id": "lrnA", "text": "pick_offset_angle 踩坑：偏移角要取负。",
               "evidence_relpath": "@runs/a.json", "run_id": "run-a",
               "gate_source": "gate2_human", "rung": 1}
    store = _FakeStore("phase1_learnings", [dense, lexical])
    planner = _make(_mini_engine(), store, learning_top_k=1)
    ret = planner.retrieve("track2", _base_payload(), tier="probe")
    assert [r["id"] for r in ret["learning_hits"]] == ["lrnA"]
    row = ret["learning_hits"][0]
    assert row["learning_table"] == "phase1_learnings"
    assert row["run_id"] == "run-a"
    assert row["gate_source"] == "gate2_human"
    assert row["rung"] == 1
    block = _planner_learning_block(ret["learning_hits"])
    assert "?" not in block
    assert "run_id=run-a" in block and "rung=1" in block


def test_lexical_corpus_scan_columns_cover_renderer_fields():
    """扫描列集合与渲染器所需字段的显式对账（缺列即红，防回归）。"""
    from orchestrator.direction import planner_recall as pr
    assert set(pr.LEXICAL_KB_COLUMNS) >= {
        "id", "text", "doc_type", "kb_relpath", "section_path"}
    assert set(pr.LEXICAL_SOLUTION_COLUMNS) >= {
        "id", "text", "relpath", "kind", "chunk_index", "solution_id"}
    assert set(pr.LEXICAL_LEARNING_COLUMNS) >= {
        "id", "text", "evidence_relpath", "run_id", "gate_source", "rung"}
