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
from memory_flywheel.vector_engine import NAMESPACES

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
                 learning_top_k: int = DEFAULT_LEARNING_TOP_K):
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
        self.kb_top_k = kb_top_k
        self.solution_top_k = solution_top_k
        self.learning_top_k = learning_top_k

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
    def _merge_lanes(lanes: list[list[dict]], top_k: int) -> list[dict]:
        def _dist(r: dict) -> float:
            d = r.get("_distance")
            try:
                return float(d)
            except (TypeError, ValueError):
                return float("inf")

        # 无距离元数据（假引擎）时排末尾并保持词典序稳定；按 id 去重。
        ordered = sorted(
            [r for lane in lanes for r in lane],
            key=lambda r: (_dist(r), str(r.get("id"))))
        merged: list[dict] = []
        seen: set[str] = set()
        for row in ordered:
            rid = row.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(row)
            if len(merged) >= top_k:
                break
        return merged

    # ---------- kb 路（双 namespace 全局并查 + r2 项目作用域并行探针） ----------

    def _kb_route(self, hint: str,
                  projects: "list[dict] | None" = None
                  ) -> tuple[list[dict], list[dict], list[dict]]:
        per_ns: dict[str, list[dict]] = {}
        lanes: list[list[dict]] = []
        for namespace in NAMESPACES:
            rows = self.vector_engine.query_kb(
                hint, namespace=namespace, top_k=self.kb_top_k)
            annotated = [
                _annotate(r, route=ROUTE_KB, relpath=r.get("kb_relpath"))
                for r in rows]
            per_ns[namespace] = annotated
            lanes.append(annotated)
        # r2 固定并行规则：每个命中项目声明的 kb_projects 各发一条 project
        # 过滤探针（namespace=None 跨两 namespace），与全局 lane 距离合并。
        for tag in _scoped_kb_projects(projects):
            rows = self.vector_engine.query_kb(
                hint, project=tag, top_k=self.kb_top_k)
            lanes.append([
                _annotate(r, route=ROUTE_KB, relpath=r.get("kb_relpath"),
                          scoped_project=tag)
                for r in rows])
        merged = self._merge_lanes(lanes, self.kb_top_k)
        return merged, per_ns[NAMESPACES[0]], per_ns[NAMESPACES[1]]

    # ---------- solution 路（全局 + r2 方案作用域并行探针） ----------

    def _solution_route(self, hint: str,
                        projects: "list[dict] | None" = None) -> list[dict]:
        rows = self.solutions_engine.query_solutions(
            hint, top_k=self.solution_top_k)
        lanes: list[list[dict]] = [[
            _annotate(r, route=ROUTE_SOLUTIONS, relpath=r.get("relpath"))
            for r in rows]]
        # r2 固定并行规则：命中项目声明 solution_id 时加一条方案过滤探针；
        # 同 query 同表同嵌入，作用域 lane 的 _distance 与全局 lane 可比。
        for solution_id in _scoped_solution_ids(projects):
            rows = self.solutions_engine.query_solutions(
                hint, top_k=self.solution_top_k, solution_id=solution_id)
            lanes.append([
                _annotate(r, route=ROUTE_SOLUTIONS, relpath=r.get("relpath"),
                          scoped_solution_id=solution_id)
                for r in rows])
        return self._merge_lanes(lanes, self.solution_top_k)

    def retrieve_with_hints(self, hints: "dict[str, str]", *,
                            site: str = "direction",
                            allow_missing_solutions: bool = False,
                            matched_projects: "list[dict] | None" = None,
                            ) -> dict[str, list[dict] | dict]:
        """按已构造好的三路查询串执行四路召回。

        solution_chunks 缺表默认 fail-closed（与 query_kb 缺表同语义）；
        ``allow_missing_solutions=True`` 时该路返 []（仅彩排/特殊容错用，
        生产 Task9 装配时表必在）。``matched_projects`` 为 r2 固定并行规则的
        项目识别结果（必须基于未扩展 base hint；缺省 = 只发全局探针）。
        """
        if site not in SITES:
            raise ValueError(f"site 非法：{site!r}；白名单 {SITES}")
        for route in (ROUTE_KB, ROUTE_SOLUTIONS, ROUTE_LEARNINGS):
            h = hints.get(route)
            if not isinstance(h, str) or not h.strip():
                raise ValueError(f"hints[{route!r}] 必须是非空字符串")
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
        return self.retrieve_with_hints(
            hints, site=site,
            allow_missing_solutions=allow_missing_solutions,
            matched_projects=projects)


def route_segments(result: dict) -> dict[str, list[dict]]:
    """四路结果按注入三段归组：{kb, solutions, learnings}。"""
    return {
        ROUTE_KB: list(result["kb_hits"]),
        ROUTE_SOLUTIONS: list(result["solution_hits"]),
        ROUTE_LEARNINGS: list(result["learning_hits"]),
    }
