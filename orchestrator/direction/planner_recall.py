#!/usr/bin/env python3
"""Phase 2.5 Task6（AC-T6-1/T6-2）：Gate1 全量知识四路召回 + 查询构造器。

0 付费：全部检索走本地 mpnet 缓存，不触方舟；本模块对活库只读。

四路（在 ``memory_flywheel.recall`` 的 kb+learning 双路上**加法**，不改
ProductionRecall 及其 setter/runner 旧调用点）：

1. kb 路并查：同一查询串分别检索 kb_chunks 的 capabilities 与
   troubleshooting 两 namespace（各自 top_k、独立结果），按 ``_distance``
   升序合并去重后取前 ``kb_top_k``，行保留原 namespace 字段；
2. solutions 路：经 VectorEngine.query_solutions 新只读缝检索
   solution_chunks（94 块/5 方案），不建表、不改切块；
3+4. learning 路：phase1_learnings 主库 + 额外学习库（生产为
   phase2_learnings）合并，缺表返 []（既有语义，不报错不建表），主库优先、
   按 id 去重；命中行带 learning_table 标签。

每行结果（浅拷贝，不改写原行）统一补两键：``source_route`` ∈
{kb, solutions, learnings} 与 ``relpath``（kb_relpath / relpath /
evidence_relpath 归一），供 setter 三段注入与采纳遥测对账。

查询构造器（AC-T6-2，同义词典数据在 ``synonyms.yaml``，不硬编码 prompt）：
- probe 档且无项目别名命中时，hint 与 setter.run_direction 现行查询串逐字节
  一致（golden 测试钉死）；命中时仅在尾上按词典序追加去重后的项目扩展词；
- policy_closed_loop 档只给 kb 路尾加能力主题句（动作从哪来/action space/
  7-DOF delta/夹爪归一化/Stage 切分/policy_source）；solution/learning 两路
  不加（项目代码与战役叙述切块会被泛化主题句稀释——预注册固定规则）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from memory_flywheel.recall import (
    SOURCE_ROUTE_KB,
    SOURCE_ROUTE_LEARNINGS,
    SOURCE_ROUTE_SOLUTIONS,
    SITES,
    ProductionRecall,
)
from memory_flywheel.vector_engine import (
    NAMESPACES,
    SOLUTION_TABLE_NAME,
    TABLE_NAME as KB_TABLE_NAME,
)
from orchestrator.direction.lexical_index import Bm25Index, rrf_fuse

SYNONYMS_PATH = Path(__file__).with_name("synonyms.yaml")

TIER_PROBE = "probe"
TIER_POLICY_CLOSED_LOOP = "policy_closed_loop"
TIERS = (TIER_PROBE, TIER_POLICY_CLOSED_LOOP)

ROUTE_KB = SOURCE_ROUTE_KB
ROUTE_SOLUTIONS = SOURCE_ROUTE_SOLUTIONS
ROUTE_LEARNINGS = SOURCE_ROUTE_LEARNINGS

DEFAULT_KB_TOP_K = 8
DEFAULT_SOLUTION_TOP_K = 8
DEFAULT_LEARNING_TOP_K = 8

# r3 召回合并规则版本（2026-10-31 冻结，见
# scripts/phase25/task6_r3_prereg.md §5.1/§5.6）：kb/solutions 两路在 r2
# 全局+作用域并行探针（深度恒 8、查询串不变）之上加「作用域保底配额」——
# 保底席只给 S-only 行（作用域探针可达、但全局探针不可达的 id），
# q=floor(top_k/2)（生产 top_k=8 时 q=4）；G∩S 双属行不进保底段、不耗
# 保底席，只在开放段按 G 距离序竞争并保持全局注解形态；learning 路不引入
# 配额；matched_projects 为空时与 r2 距离合并逐字节相同；生产 retrieve()
# 与判分器走同一实现，无 eval 专用分支。
R3_RULES_VERSION = "r3"

# Task6 Stage B（用户 2026-11-02 裁决立项，预注册
# scripts/phase25/task6_r4_prereg.md）：在 r3 之上**只加**一条精确术语词法
# 通道（BM25 全表倒排）并与 r3 稠密 top-8 做 RRF 秩融合。稠密侧（探针深度、
# 查询串、作用域保底配额）逐字不动——r3 分支在本题下与 r3 时点逐字节相同。
R4_RULES_VERSION = "r4"
SUPPORTED_RULES_VERSIONS = (R3_RULES_VERSION, R4_RULES_VERSION)
# 预注册 §3.7，按修订 A7 更正：r4 只是**构造函数默认值**——
# `orchestrator/graph/cli.py` 构造 `DirectionSetter` 时从不传 `planner_recall`，
# **生产代码**里没有第二个注入点（测试会直接构造 `PlannerRecall`，那不算生产
# 消费者），故 `PlannerRecall` 今日**没有生产消费者**。"生产默认 r4"
# 仅指直接构造（判分器/参考模拟/测试）时生效；把它接进 setter/cli 属另一件事，
# 须另立预注册。历史锚点/回归路径显式传 rules_version="r3"。
DEFAULT_RULES_VERSION = R4_RULES_VERSION

# 预注册 §3.5 词法语料：kb 路排除目录性文档（与 query_kb 的 include_index=False
# 同集）；solutions/learnings 全表。
LEXICAL_EXCLUDED_DOC_TYPES = frozenset({"root_index", "index"})

# 词法语料扫描列（预注册修订 A3 / GATE1-LANE-A M1）：必须覆盖 setter 渲染器
# 读取的**全部**来源字段，否则词法行被提进 top-8 时 prompt 出现 `?` 占位。
# 列名逐字对应三表 schema（kb_chunks / solution_chunks / *_learnings）。
LEXICAL_KB_COLUMNS = ("id", "text", "doc_type", "kb_relpath", "section_path")
LEXICAL_SOLUTION_COLUMNS = ("id", "text", "relpath", "kind", "chunk_index",
                            "solution_id")
LEXICAL_LEARNING_COLUMNS = ("id", "text", "evidence_relpath", "run_id",
                            "gate_source", "rung")

# AC-T6-4 r2 固定并行查询规则：synonyms.yaml 的 kb_projects 只允许活库既有
# project 标签（fail-closed，防数据文件写出无意义 WHERE）。
KB_PROJECT_TAGS = frozenset(
    {"lw_benchhub", "ge_sim_v2", "genie_sim_v3", "genesis_world", "global"})


# ---------- 查询构造器（纯函数 + 数据文件） ----------

def legacy_direction_hint(track: str, payload: dict) -> str:
    """setter.run_direction 现行查询串（逐字节单一源，旧路径不得改字）。"""
    if track == "track2":
        return payload["question"]
    if track == "track1":
        return f"{payload['name']} 架构 物理引擎 特性 闭环仿真"
    raise ValueError(f"未知 track={track!r}，合法值 track1|track2")


@lru_cache(maxsize=8)
def load_synonyms(path_str: str) -> dict:
    """加载并轻校验同义词典（按路径缓存；文件改后需重启/清缓存）。"""
    path = Path(path_str)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != \
            "simpraxis.planner_synonyms.v1":
        raise ValueError(f"同义词典 schema 不符：{path}")
    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError(f"同义词典 projects 为空或非法：{path}")
    for p in projects:
        if not isinstance(p, dict) or not p.get("id") \
                or not isinstance(p.get("aliases"), list) \
                or not p.get("expansion"):
            raise ValueError(f"同义词典项目非法：{p!r}（{path}）")
        kb_projects = p.get("kb_projects", [])
        if not isinstance(kb_projects, list) or any(
                not isinstance(x, str) or x not in KB_PROJECT_TAGS
                for x in kb_projects):
            raise ValueError(
                f"项目 {p.get('id')!r} kb_projects 非法（白名单 {sorted(KB_PROJECT_TAGS)}）：{kb_projects!r}")
        solution_id = p.get("solution_id")
        if solution_id is not None and (
                not isinstance(solution_id, str) or not solution_id.strip()):
            raise ValueError(f"项目 {p.get('id')!r} solution_id 非法：{solution_id!r}")
    tiers = data.get("tiers") or {}
    if TIER_PROBE not in tiers:
        raise ValueError(f"同义词典缺 tiers.{TIER_PROBE}：{path}")
    return data


def _alias_regex(alias: str) -> re.Pattern:
    """ASCII 别名：大小写敏感 + 字母数字词边界（ACT 不命中 react/action）。

    CJK/符号别名（含任何非 ASCII 字符）：子串匹配，不另加边界（中文无
    空白分词）。两侧用 lookaround 保证零宽，sub/匹配行为可预测。
    """
    if alias.isascii():
        return re.compile(r"(?<![A-Za-z0-9])"
                          + re.escape(alias) + r"(?![A-Za-z0-9])")
    return re.compile(re.escape(alias))


def detect_projects(text: str, data: "dict | None" = None) -> list[dict]:
    """返回命中文本的项目词典条目（按词典出现序，去重）。"""
    data = data or load_synonyms(str(SYNONYMS_PATH))
    matched: list[dict] = []
    seen: set[str] = set()
    for proj in data["projects"]:
        if proj["id"] in seen:
            continue
        for alias in proj["aliases"]:
            if not isinstance(alias, str) or not alias:
                continue
            if _alias_regex(alias).search(text):
                matched.append(proj)
                seen.add(proj["id"])
                break
    return matched


def build_planner_hint(track: str, payload: dict, *,
                       tier: str = TIER_PROBE,
                       data: "dict | None" = None) -> str:
    """单路查询串（probe 档无命中 == legacy_direction_hint，逐字节）。"""
    if tier not in TIERS:
        raise ValueError(f"tier 非法：{tier!r}；白名单 {TIERS}")
    base = legacy_direction_hint(track, payload)
    projects = detect_projects(base, data)
    parts = [base]
    parts.extend(p["expansion"] for p in projects)
    hint = " ".join(parts)
    if tier == TIER_POLICY_CLOSED_LOOP:
        theme = (data or load_synonyms(str(SYNONYMS_PATH))) \
            ["tiers"][TIER_POLICY_CLOSED_LOOP]
        if theme:
            hint = f"{hint} {theme}"
    return hint


def build_route_hints(track: str, payload: dict, *,
                      tier: str = TIER_PROBE,
                      data: "dict | None" = None) -> dict[str, str]:
    """四路各自的查询串。

    kb 路走带档全量串（probe 无命中逐字节旧）；solutions/learnings 两路
    恒走 probe 扩展串（即 policy 档也不追加能力主题句，固定规则见模块头）。
    无项目命中时三路串完全相同且等于 legacy hint。
    """
    if tier not in TIERS:
        raise ValueError(f"tier 非法：{tier!r}；白名单 {TIERS}")
    kb_hint = build_planner_hint(track, payload, tier=tier, data=data)
    if tier == TIER_PROBE:
        route_hint = kb_hint
    else:
        route_hint = build_planner_hint(
            track, payload, tier=TIER_PROBE, data=data)
    return {ROUTE_KB: kb_hint, ROUTE_SOLUTIONS: route_hint,
            ROUTE_LEARNINGS: route_hint}


def matched_projects(track: str, payload: dict, *,
                     data: "dict | None" = None) -> list[dict]:
    """r2 固定并行规则用：只在**未扩展的** base hint 上识别命中项目。

    扩展串本身含他项目别名（如 LIBERO 扩展里带 robosuite/MuJoCo/BDDL），
    在扩展串上二次识别会级联误命中，故项目作用域探针的项目集一律从
    legacy base 识别（与 hint 扩展用的是同一次 detect_projects 结果）。
    """
    base = legacy_direction_hint(track, payload)
    return detect_projects(base, data)


# ---------- 四路召回编排 ----------

def _annotate(row: dict, *, route: str, relpath: "str | None",
              **extra: Any) -> dict:
    out = dict(row)  # 浅拷贝：永不改写引擎/store 返回的原行
    out["source_route"] = route
    out["relpath"] = relpath
    out.update(extra)
    return out


def _scoped_kb_projects(projects: "list[dict] | None") -> list[str]:
    """命中项目声明的 kb_projects 并集（按声明序去重；空列表/缺省不发探针）。"""
    out: list[str] = []
    for proj in projects or []:
        for tag in proj.get("kb_projects") or []:
            if tag not in out:
                out.append(tag)
    return out


def _scoped_solution_ids(projects: "list[dict] | None") -> list[str]:
    """命中项目声明的 solution_id 并集（按声明序去重；非法 id 由引擎 fail-closed）。"""
    out: list[str] = []
    for proj in projects or []:
        sid = proj.get("solution_id")
        if sid and sid not in out:
            out.append(sid)
    return out


def _store_label(store: Any) -> str:
    """学习库表名标签：表存在时直接问表对象；缺表/异常给保守名。"""
    try:
        return str(store.table(create=False).name)
    except Exception:
        return "learning_table"


class PlannerRecall:
    """四路召回（在 ProductionRecall 双路上**加法**组合，不改其任一旧行为）。

    组合而非继承：kb 双 namespace 并查与 learning 多库合并复用
    ``ProductionRecall`` 已持有的 VectorEngine / learning stores；采纳遥测
    也仍由同一个 ProductionRecall 单一写入（record_adoption 透传），生产
    jsonl 不出现第二个写入方。solution_chunks 与 kb_chunks 同库同嵌入，
    默认复用同一 VectorEngine 句柄的 query_solutions 只读缝。
    """

    def __init__(self, production_recall: ProductionRecall, *,
                 solutions_engine: "Any | None" = None,
                 kb_top_k: int = DEFAULT_KB_TOP_K,
                 solution_top_k: int = DEFAULT_SOLUTION_TOP_K,
                 learning_top_k: int = DEFAULT_LEARNING_TOP_K,
                 rules_version: str = DEFAULT_RULES_VERSION):
        self.prod = production_recall
        self.vector_engine = production_recall.vector_engine
        self.solutions_engine = solutions_engine or production_recall.vector_engine
        self.learning_store = production_recall.learning_store
        self.extra_learning_stores = production_recall.extra_learning_stores
        for name, val in (("kb_top_k", kb_top_k),
                          ("solution_top_k", solution_top_k),
                          ("learning_top_k", learning_top_k)):
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                raise ValueError(f"{name} 必须是正整数，收到 {val!r}")
        if rules_version not in SUPPORTED_RULES_VERSIONS:
            raise ValueError(
                f"rules_version 非法：{rules_version!r}；白名单 "
                f"{SUPPORTED_RULES_VERSIONS}")
        self.kb_top_k = kb_top_k
        self.solution_top_k = solution_top_k
        self.learning_top_k = learning_top_k
        self.rules_version = rules_version
        # r4 词法索引缓存（预注册 §3.6）：{失效键: (行数元组, 索引状态)}。
        self._lexical_cache: dict[str, tuple[tuple, dict]] = {}

    # ---------- 采纳遥测：透传 ProductionRecall 单一写入方 ----------

    def record_adoption(self, **kwargs: Any) -> bool:
        """委托给组合的 ProductionRecall（jsonl 唯一写入方不变）。"""
        return self.prod.record_adoption(**kwargs)

    # ---------- learning 路（主库 + 额外库，缺表 []） ----------

    @staticmethod
    def _probe_store(store: Any, hint: str, top_k: int) -> list[dict]:
        # 与 ProductionRecall._store_learning_hits 同语义：table_exists 探表，
        # 缺表路径绝不触发表创建。
        if not store.table_exists():
            return []
        return store.query(hint, top_k)

    def _learning_route(self, hint: str) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        stores = (self.learning_store, *self.extra_learning_stores)
        for store in stores:
            label = _store_label(store)
            for row in self._probe_store(store, hint, self.learning_top_k):
                rid = row.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                out.append(_annotate(
                    row, route=ROUTE_LEARNINGS,
                    relpath=row.get("evidence_relpath"),
                    learning_table=label))
        return out

    # ---------- 距离合并（同表同嵌入同度量，跨 lane 可直接比 _distance） ----------

    @staticmethod
    def _row_distance(row: dict) -> float:
        """排序键距离分量（r2/r3 共用单一源）：缺/坏 _distance → inf 排末尾。"""
        d = row.get("_distance")
        try:
            return float(d)
        except (TypeError, ValueError):
            return float("inf")

    @staticmethod
    def _sort_dedup_lanes(lanes: list[list[dict]]) -> list[dict]:
        """flatten lanes → 按 (_distance, str(id)) 升序 → 按 id 去重保留首次
        出现行。r2 ``_merge_lanes`` 与 r3 保底配额合并唯一公共排序/去重段，
        语义与 r2 逐字一致（tie-break 用 str(id)，见 task6_r3_prereg §5.6 N1；
        无距离元数据时排末尾并保持词典序稳定）。
        """
        ordered = sorted(
            (row for lane in lanes for row in lane),
            key=lambda r: (PlannerRecall._row_distance(r),
                           str(r.get("id"))))
        merged: list[dict] = []
        seen: set = set()
        for row in ordered:
            rid = row.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(row)
        return merged

    @staticmethod
    def _merge_lanes(lanes: list[list[dict]], top_k: int) -> list[dict]:
        # r2 纯距离合并（learning 路与 r3 退化情形仍走此入口）：排序/去重
        # 只允许走公共段，禁止与 r3 复制出两套不一致逻辑。
        return PlannerRecall._sort_dedup_lanes(lanes)[:top_k]

    @staticmethod
    def _merge_with_scoped_quota(global_lanes: list[list[dict]],
                                 scoped_lanes: list[list[dict]],
                                 top_k: int) -> list[dict]:
        """r3 作用域保底配额合并（R3_RULES_VERSION，规则逐字对应
        task6_r3_prereg.md §5.1/§5.6 NOTES 1–4）：

        - P_G/P_S：global/scoped lanes 各自 flatten 后按 (_distance, str(id))
          升序、按 id 去重保留首次出现行（同一公共排序段，与 r2 逐字一致）；
        - 保底段 R 只取 S-only 行（id ∉ ids(P_G)），q=top_k//2（floor），
          至多 q 行，保留其作用域注解（scoped_project/scoped_solution_id）；
          G∩S 双属行不进保底段、不耗保底席；
        - 开放段取 P_G 距离序、跳过 R 中 id 依次补足 top_k——双属行在此以其
          全局 lane 注解形态出现（无 scoped_* 键，§5.6 NOTES-2）；
        - 候选总数不足 top_k 返回实际并集行数，不填充、不报错（NOTES-3）；
        - scoped_lanes 为空时退化为 r2：与
          ``_merge_lanes(global_lanes, top_k)`` 逐 dict 相等。
        """
        p_g = PlannerRecall._sort_dedup_lanes(global_lanes)
        p_s = PlannerRecall._sort_dedup_lanes(scoped_lanes)
        global_ids = {r.get("id") for r in p_g}
        s_only = [r for r in p_s if r.get("id") not in global_ids]
        q = top_k // 2
        reserved = s_only[:min(q, len(s_only))]
        reserved_ids = {r.get("id") for r in reserved}
        open_segment: list[dict] = []
        for row in p_g:
            if row.get("id") in reserved_ids:
                continue
            open_segment.append(row)
            if len(reserved) + len(open_segment) >= top_k:
                break
        return reserved + open_segment

    # ---------- r4 词法通道（预注册 §3；r3 路径绝不触碰以下任何方法） ----------

    @staticmethod
    def _lexical_relpath(route: str, row: dict) -> "str | None":
        """词法行 → 与稠密行同口径的 relpath（§3.3 注解补全）。"""
        if route == ROUTE_KB:
            return row.get("kb_relpath")
        if route == ROUTE_SOLUTIONS:
            return row.get("relpath")
        return row.get("evidence_relpath")

    def _lexical_counts(self) -> tuple:
        """失效键（§3.6）：三路语料表的行数逐表比对，行数变化即重建索引。

        kb 路用**整表行数**作失效键（不扫 doc_type 子集）：本仓 kb_chunks 的
        doc_type 不就地改写，追加只发生在整表重建（`drop_existing` + 重灌）
        路径上，故「总行数不变而 doc_type 翻转」不在生产路径内。这是**已知
        边界**，不声称与语料行数等价——将来若出现就地改 doc_type 的写入，
        本键会漏判（GATE1-LANE-A L7）。缺表/缺库记 0（不建表、不报错）。
        """
        counts: list[tuple[str, int]] = [
            ("kb", self.vector_engine.count_rows(KB_TABLE_NAME)),
            ("sol", self.solutions_engine.count_rows(SOLUTION_TABLE_NAME)),
        ]
        for store in (self.learning_store, *self.extra_learning_stores):
            counts.append((_store_label(store), store.count()))
        return tuple(counts)

    def _lexical_corpus(self) -> dict[str, list[dict]]:
        """§3.5 三路语料只读扫描（vector 列不入内存）。

        扫描列必须覆盖 **setter 渲染器要读的每一个来源字段**：词法通道把某行
        提进 top-8 时，该行是从本语料原样复制来的（不走稠密查询），缺列就会让
        prompt 里出现 `?` 占位（``_planner_kb_block`` 要 ``section_path``；
        ``_planner_solution_block`` 要 ``kind``/``chunk_index``/``solution_id``；
        ``_planner_learning_block`` 要 ``run_id``/``gate_source``/``rung``/
        ``learning_table``，最后一项不是列而是按库注入的标签）。GATE1-LANE-A M1。
        """
        kb_rows = self.vector_engine.scan_rows(
            KB_TABLE_NAME, list(LEXICAL_KB_COLUMNS))
        kb = [r for r in kb_rows
              if r.get("doc_type") not in LEXICAL_EXCLUDED_DOC_TYPES]
        sol = self.solutions_engine.scan_rows(
            SOLUTION_TABLE_NAME, list(LEXICAL_SOLUTION_COLUMNS))
        lrn: list[dict] = []
        for store in (self.learning_store, *self.extra_learning_stores):
            label = _store_label(store)
            for row in store.scan_rows(list(LEXICAL_LEARNING_COLUMNS)):
                lrn.append(dict(row, learning_table=label))
        return {ROUTE_KB: kb, ROUTE_SOLUTIONS: sol, ROUTE_LEARNINGS: lrn}

    def _lexical_state(self) -> dict[str, dict]:
        """惰性构建并复用三路词法索引（行数不变即命中缓存）。"""
        counts = self._lexical_counts()
        cached = self._lexical_cache.get("state")
        if cached is not None and cached[0] == counts:
            return cached[1]
        corpus = self._lexical_corpus()
        state = {
            route: {"index": Bm25Index(rows),
                    "by_id": {str(r.get("id")): r for r in rows}}
            for route, rows in corpus.items()
        }
        self._lexical_cache["state"] = (counts, state)
        return state

    def _fuse_route(self, route: str, dense_rows: list[dict],
                    lexicon: dict, query: str, top_k: int) -> list[dict]:
        """§3.3：r3 稠密前 ``top_k`` 行 ⊕ 词法 top-``top_k``，RRF 融合。

        - 稠密侧 id 序 D = ``dense_rows[:top_k]``（r3 生产结果，语义与注解不变）；
        - 融合后 id 在 D 中 → 沿用 D 的原行对象（保留 ``_distance``/
          ``scoped_*`` 等稠密注解）；否则取词法扫描到的原始行并补
          ``source_route``/``relpath``/``lexical_rank``；
        - **尾席保持（预注册 §3.3 修订 A1）**：``dense_rows`` 长度超过
          ``top_k`` 时（learning 路 = 主库 8 + 额外库去重后可达 9 行，
          ``_learning_route`` 不截断、setter 不切片），超出部分按 r3 原序**追加**
          在融合段之后。理由：融合只定义最终 top-8（§3.3 / §6 点预测按 8 行冻结），
          而 r3 生产会把第 9 行（phase2 演化志）一并注入；若不追加，r4 会在
          11/12 个 learning 案上静默改变注入内容，构成未声明的第二变量。
        - 退化性质：词法列表为空时融合 id 序 == D 逐位相等（稠密分 1/(60+r)
          严格单调），此时返回的行对象与 ``dense_rows`` 逐 dict 相等。
        """
        dense_top = dense_rows[:top_k]
        # 缺 id 的稠密行在 RRF 里无法被标识：`str(r.get("id"))` 会静默变成
        # 字面量 "None"，把多行并成一行、还会和词法侧同形 id 混淆。活库三表
        # 的 id 恒非空，出现即装配缺陷，fail-closed 报错（GATE1-LANE-A L8）。
        # 扫描**全部** dense_rows 而不只是 dense_top：尾席（第 9 行起）同样会
        # 被追加进最终结果，尾行缺 id 时下面的 `str(r.get("id")) not in
        # emitted` 一样会把 None 当普通 id（GATE1-LANE-A 第二轮 LOW-2）。
        bad = [i for i, r in enumerate(dense_rows) if r.get("id") is None]
        if bad:
            raise ValueError(
                f"稠密行缺 id（下标 {bad}）：RRF 融合无法标识行，"
                "不得记作字符串 'None'")
        dense_ids = [str(r["id"]) for r in dense_top]
        lex = lexicon["index"].top(query, top_k)
        fused = rrf_fuse(dense_ids, lex, top_k=top_k)
        dense_by_id = {str(r["id"]): r for r in dense_top}
        lex_rank = {rid: rank for rank, (_s, rid) in enumerate(lex, 1)}
        out: list[dict] = []
        for _score, rid in fused:
            row = dense_by_id.get(rid)
            if row is None:
                raw = dict(lexicon["by_id"].get(rid) or {})
                raw.pop("vector", None)
                row = _annotate(
                    raw, route=route, relpath=self._lexical_relpath(route, raw),
                    lexical_rank=lex_rank.get(rid))
            out.append(row)
        # 尾席按 r3 原序追加，但跳过已进融合段的行（词法通道可能把第 9 行
        # 提到 top-8 内，此时不得重复注入同一 id）。
        emitted = {str(r.get("id")) for r in out}
        out.extend(r for r in dense_rows[top_k:]
                   if str(r.get("id")) not in emitted)
        return out

    # ---------- kb 路（双 namespace 全局并查 + r2 项目作用域并行探针） ----------

    def _kb_route(self, hint: str,
                  projects: "list[dict] | None" = None
                  ) -> tuple[list[dict], list[dict], list[dict]]:
        per_ns: dict[str, list[dict]] = {}
        global_lanes: list[list[dict]] = []
        for namespace in NAMESPACES:
            rows = self.vector_engine.query_kb(
                hint, namespace=namespace, top_k=self.kb_top_k)
            annotated = [
                _annotate(r, route=ROUTE_KB, relpath=r.get("kb_relpath"))
                for r in rows]
            per_ns[namespace] = annotated
            global_lanes.append(annotated)
        # r2 固定并行规则：每个命中项目声明的 kb_projects 各发一条 project
        # 过滤探针（namespace=None 跨两 namespace）。探针签名/深度（恒
        # kb_top_k=8）/查询串与 r2 逐字不变；r3 仅改合并策略：作用域 lane
        # 走保底配额（保底席仅 S-only，q=floor(k/2)，见 R3_RULES_VERSION）。
        scoped_lanes: list[list[dict]] = []
        for tag in _scoped_kb_projects(projects):
            rows = self.vector_engine.query_kb(
                hint, project=tag, top_k=self.kb_top_k)
            scoped_lanes.append([
                _annotate(r, route=ROUTE_KB, relpath=r.get("kb_relpath"),
                          scoped_project=tag)
                for r in rows])
        merged = self._merge_with_scoped_quota(
            global_lanes, scoped_lanes, self.kb_top_k)
        return merged, per_ns[NAMESPACES[0]], per_ns[NAMESPACES[1]]

    # ---------- solution 路（全局 + r2 方案作用域并行探针） ----------

    def _solution_route(self, hint: str,
                        projects: "list[dict] | None" = None) -> list[dict]:
        rows = self.solutions_engine.query_solutions(
            hint, top_k=self.solution_top_k)
        global_lanes: list[list[dict]] = [[
            _annotate(r, route=ROUTE_SOLUTIONS, relpath=r.get("relpath"))
            for r in rows]]
        # r2 固定并行规则：命中项目声明 solution_id 时加一条方案过滤探针；
        # 同 query 同表同嵌入，作用域 lane 的 _distance 与全局 lane 可比。
        # 探针签名/深度（恒 solution_top_k=8）/查询串不变；r3 仅把作用域
        # lane 改走保底配额合并（R3_RULES_VERSION，规则见 task6_r3_prereg §5.1）。
        scoped_lanes: list[list[dict]] = []
        for solution_id in _scoped_solution_ids(projects):
            rows = self.solutions_engine.query_solutions(
                hint, top_k=self.solution_top_k, solution_id=solution_id)
            scoped_lanes.append([
                _annotate(r, route=ROUTE_SOLUTIONS, relpath=r.get("relpath"),
                          scoped_solution_id=solution_id)
                for r in rows])
        return self._merge_with_scoped_quota(
            global_lanes, scoped_lanes, self.solution_top_k)

    def retrieve_with_hints(self, hints: "dict[str, str]", *,
                            site: str = "direction",
                            allow_missing_solutions: bool = False,
                            matched_projects: "list[dict] | None" = None,
                            lexical_hints: "dict[str, str] | None" = None,
                            ) -> dict[str, list[dict] | dict]:
        """按已构造好的三路查询串执行四路召回。

        solution_chunks 缺表默认 fail-closed（与 query_kb 缺表同语义）；
        ``allow_missing_solutions=True`` 时该路返 []（仅彩排/特殊容错用，
        生产 Task9 装配时表必在）。``matched_projects`` 为 r2 固定并行规则的
        项目识别结果（必须基于未扩展 base hint；缺省 = 只发全局探针）。

        ``lexical_hints`` 仅 r4 使用（预注册 §3.4）：词法通道**必须**用未扩展的
        base hint，因此 r4 下该参数为**必填**（缺省即 ``ValueError``）。

        预注册 §3.4 原先写「缺省 ``= hints``（向后兼容）」，与同节首条「词法
        通道一律用未扩展的 base hint」自相矛盾：直接调本入口（不经
        ``retrieve()``）的人会静默拿到**已扩展** hint 当词法查询串，等于一条
        未登记的第二种语义。修订 A4 取 fail-closed：缺省不猜、显式传入。
        r3 分支不使用词法通道，本参数保持可选（传了也忽略）。
        """
        if site not in SITES:
            raise ValueError(f"site 非法：{site!r}；白名单 {SITES}")
        for route in (ROUTE_KB, ROUTE_SOLUTIONS, ROUTE_LEARNINGS):
            h = hints.get(route)
            if not isinstance(h, str) or not h.strip():
                raise ValueError(f"hints[{route!r}] 必须是非空字符串")
        lex_hints = hints if lexical_hints is None else lexical_hints
        if self.rules_version == R4_RULES_VERSION:
            if lexical_hints is None:
                raise ValueError(
                    "r4 必须显式传 lexical_hints（未扩展 base hint；预注册 "
                    "§3.4 修订 A4 fail-closed——缺省 = hints 会让扩展串静默"
                    "充当词法查询；生产入口 retrieve() 已显式传三路同 base）")
            for route in (ROUTE_KB, ROUTE_SOLUTIONS, ROUTE_LEARNINGS):
                h = lex_hints.get(route)
                if not isinstance(h, str) or not h.strip():
                    raise ValueError(
                        f"lexical_hints[{route!r}] 必须是非空字符串（r4）")
        kb_hits, cap_hits, tb_hits = self._kb_route(
            hints[ROUTE_KB], matched_projects)
        try:
            solution_hits = self._solution_route(
                hints[ROUTE_SOLUTIONS], matched_projects)
        except RuntimeError as exc:
            if not allow_missing_solutions or "solution_chunks" not in str(exc):
                raise
            solution_hits = []
        learning_hits = self._learning_route(hints[ROUTE_LEARNINGS])
        if self.rules_version == R4_RULES_VERSION:
            state = self._lexical_state()
            kb_hits = self._fuse_route(
                ROUTE_KB, kb_hits, state[ROUTE_KB],
                lex_hints[ROUTE_KB], self.kb_top_k)
            solution_hits = self._fuse_route(
                ROUTE_SOLUTIONS, solution_hits, state[ROUTE_SOLUTIONS],
                lex_hints[ROUTE_SOLUTIONS], self.solution_top_k)
            learning_hits = self._fuse_route(
                ROUTE_LEARNINGS, learning_hits, state[ROUTE_LEARNINGS],
                lex_hints[ROUTE_LEARNINGS], self.learning_top_k)
        return {
            "hints": dict(hints),
            "kb_hits": kb_hits,
            "kb_capabilities_hits": cap_hits,
            "kb_troubleshooting_hits": tb_hits,
            "solution_hits": solution_hits,
            "learning_hits": learning_hits,
        }

    def retrieve(self, track: str, payload: dict, *,
                 tier: str = TIER_PROBE, site: str = "direction",
                 allow_missing_solutions: bool = False
                 ) -> dict[str, list[dict] | dict]:
        """查询构造 + 四路召回（生产 Task9 主入口；0 付费）。"""
        hints = build_route_hints(track, payload, tier=tier)
        projects = matched_projects(track, payload)
        lexical_hints = None
        if self.rules_version == R4_RULES_VERSION:
            base = legacy_direction_hint(track, payload)
            lexical_hints = {ROUTE_KB: base, ROUTE_SOLUTIONS: base,
                             ROUTE_LEARNINGS: base}
        return self.retrieve_with_hints(
            hints, site=site,
            allow_missing_solutions=allow_missing_solutions,
            matched_projects=projects,
            lexical_hints=lexical_hints)


def route_segments(result: dict) -> dict[str, list[dict]]:
    """四路结果按注入三段归组：{kb, solutions, learnings}。"""
    return {
        ROUTE_KB: list(result["kb_hits"]),
        ROUTE_SOLUTIONS: list(result["solution_hits"]),
        ROUTE_LEARNINGS: list(result["learning_hits"]),
    }
