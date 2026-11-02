#!/usr/bin/env python3
"""Task 6 Stage B（r4 词法混合召回）：设计期参考模拟（只读、0 付费、确定性）。

本脚本是预注册 ``scripts/phase25/task6_r4_prereg.md`` 的**点预测机产件**
来源：在引擎批（生产 r4 实现）落地**之前**运行，按预注册冻结的规则规格
（§3）在本进程内独立实现 BM25 词法通道 + RRF 融合，对 47 个冻结 gold 案
逐案产出 {lexical_ids, fused_ids, hit, rank, matched_by, margin} 与聚合
指标，写入 ``snapshots/task6_r4_point_predictions.json``（注册 sha）。

规则规格逐字对应预注册 §3（任何不一致都以预注册为准）：
- 术语：ASCII ``[A-Za-z_][A-Za-z0-9_]{3,}``（大小写敏感，长度 ≥4）；
  CJK 连续段长度 4 的滑窗 4-gram；
- BM25：k1=1.2、b=0.75、idf=ln(1+(N-df+0.5)/(df+0.5))、tf=子串出现次数、
  dl=len(text) 字符数；语料 = 该路全表（kb 排除 doc_type ∈ {root_index,
  index}；learnings 合并 phase1+phase2）；
- 词法通道取 top-8（(-score, str(id)) 升序）；
- 稠密侧 = r3 生产并集 top-8（**逐案强断言等于冻结 iter3 报告 top8_ids**）；
- 融合：RRF k=60，等权，score=Σ1/(60+rank)，取 top-8（(-score, str(id))）。

0 下载：import 前置三位离线开关；只读活 lancedb（不建表/不写块/不调网关）。
本脚本**不判分**、不写活库；判分唯一入口仍是冻结判分器
``scripts/phase25/eval_planner_recall.py``（sha 钉死 e73fe8b8）。
重跑输出逐字节相同（无时间戳、行按 id 排序、术语按字典序求和），
``--check`` 可对注册件做逐字节复核。

用法：
  python scripts/phase25/task6_r4_reference_sim.py [--out PATH] [--check PATH]
退出码：0=成功；2=前置不满足（锚点/活库/iter3 不一致）。
契约边界（门 1 L5）：只有 `AnchorError` 被翻译成 rc=2；其余未预期异常按
Python 默认 rc=1 + traceback 抛出——那是**实现崩溃**而不是「锚点不符」，
不得当成判据继续（预注册 §7.1 首呼前允许修复重来）。
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import sys
from pathlib import Path

for _var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_var, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import lancedb  # noqa: E402

from memory_flywheel.phase1.learning_store import LearningStore  # noqa: E402
from memory_flywheel.phase2.evolution_store import EvolutionStore  # noqa: E402
from memory_flywheel.recall import ProductionRecall  # noqa: E402
from memory_flywheel.vector_engine import VectorEngine  # noqa: E402
from orchestrator.direction.planner_recall import (  # noqa: E402
    ROUTE_KB,
    ROUTE_LEARNINGS,
    ROUTE_SOLUTIONS,
    PlannerRecall,
    build_route_hints,
    matched_projects,
)

FIXTURE = (REPO_ROOT / "tests" / "milestone25" / "fixtures"
           / "planner_golden_queries.json")
FIXTURE_SHA256 = (
    "f5c5cc58890ce5f9aff1c5f0dd6b97bf1486e716060bcd29d612793458b7c3a6")
ITER3_REPORT_SHA256 = (
    "f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307")
DEFAULT_OUT = REPO_ROOT / "scripts" / "phase25" / "snapshots" \
    / "task6_r4_point_predictions.json"

ROUTE_KEY = {ROUTE_KB: "kb_hits", ROUTE_SOLUTIONS: "solution_hits",
             ROUTE_LEARNINGS: "learning_hits"}

# ---- 预注册 §3 冻结常量（不随运行可调） ----
ASCII_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
CJK_RE = re.compile(r"[一-鿿]+")
CJK_NGRAM = 4
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
TOP_K = 8
INDEX_DOC_TYPES = frozenset({"root_index", "index"})
SCHEMA = "simpraxis.phase25.task6_r4_point_predictions.v1"


class AnchorError(RuntimeError):
    """冻结锚点/活库不满足（判据前失败，exit 2）。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def query_terms(text: str) -> list[str]:
    """预注册 §3.1 术语抽取（返回字典序去重列表，保证浮点求和确定性）。"""
    terms = {m.group(0) for m in ASCII_RE.finditer(text)}
    for m in CJK_RE.finditer(text):
        run = m.group(0)
        for i in range(len(run) - CJK_NGRAM + 1):
            terms.add(run[i:i + CJK_NGRAM])
    return sorted(terms)


class Bm25Index:
    """全表 BM25（预注册 §3.2）。纯函数式打分，不写任何状态。"""

    def __init__(self, rows: list[dict]):
        self.rows = sorted(rows, key=lambda r: str(r.get("id")))
        self.ids = [str(r.get("id")) for r in self.rows]
        self.texts = [str(r.get("text") or "") for r in self.rows]
        self.lengths = [len(t) for t in self.texts]
        self.n = len(self.texts)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        for text, terms in ((t, set(query_terms(t))) for t in self.texts):
            del text
            for term in terms:
                df[term] = df.get(term, 0) + 1
        self.df = df

    def _idf(self, term: str) -> float:
        d = self.df[term]
        return math.log(1.0 + (self.n - d + 0.5) / (d + 0.5))

    def scores(self, query: str) -> list[tuple[float, str]]:
        terms = query_terms(query)
        out: list[tuple[float, str]] = []
        for idx, text in enumerate(self.texts):
            score = 0.0
            for term in terms:                    # 字典序（确定性求和）
                df = self.df.get(term, 0)
                if not df:
                    continue
                tf = text.count(term)
                if not tf:
                    continue
                norm = BM25_K1 * (
                    1.0 - BM25_B + BM25_B * self.lengths[idx] / self.avgdl)
                score += self._idf(term) * (tf * (BM25_K1 + 1.0)) / (tf + norm)
            if score > 0.0:
                out.append((score, self.ids[idx]))
        out.sort(key=lambda y: (-y[0], y[1]))
        return out

    def top(self, query: str, top_k: int = TOP_K) -> list[tuple[float, str]]:
        return self.scores(query)[:top_k]


def rrf_fuse(dense_ids: list[str],
             lexical: list[tuple[float, str]],
             top_k: int = TOP_K) -> tuple[list[tuple[float, str]], float]:
    """预注册 §3.3：RRF k=60 等权融合；返回 (fused, 第8/第9 名分差)。"""
    fused: dict[str, float] = {}
    for rank, rid in enumerate(dense_ids, 1):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (_score, rid) in enumerate(lexical, 1):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(fused.items(), key=lambda y: (-y[1], y[0]))
    margin = 0.0
    if len(ordered) > top_k:
        margin = round(ordered[top_k - 1][1] - ordered[top_k][1], 10)
    return [(score, rid) for rid, score in ordered[:top_k]], margin


def _load_fixture() -> dict:
    raw = FIXTURE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FIXTURE_SHA256:
        raise AnchorError(f"golden fixture sha 不符：{FIXTURE}")
    data = json.loads(raw.decode("utf-8"))
    if data.get("schema") != "simpraxis.phase25.planner_golden.v1":
        raise AnchorError("golden fixture schema 不符")
    return data


def _load_iter3() -> dict:
    from orchestrator.common.runtime_paths import phase25_root
    hits = sorted((phase25_root() / "task6_recall").glob(
        "planner_gold_iter3_*.json"))
    if len(hits) != 1:
        raise AnchorError(f"iter3 报告须恰 1 份，实得 {len(hits)}")
    if _sha256(hits[0]) != ITER3_REPORT_SHA256:
        raise AnchorError(f"iter3 报告 sha 不符：{hits[0]}")
    return json.loads(hits[0].read_text(encoding="utf-8"))


def _scan(name: str) -> list[dict]:
    from memory_flywheel import kb_pins
    db = lancedb.connect(kb_pins.default_lancedb_uri())
    rows = (db.open_table(name).search(None)
            .select(["id", "text", "doc_type"] if name == "kb_chunks"
                    else ["id", "text"])
            .limit(10 ** 6).to_list())
    for row in rows:
        row.pop("vector", None)
    return rows


def build_corpora() -> dict[str, list[dict]]:
    kb = [r for r in _scan("kb_chunks")
          if r.get("doc_type") not in INDEX_DOC_TYPES]
    return {
        ROUTE_KB: kb,
        ROUTE_SOLUTIONS: _scan("solution_chunks"),
        ROUTE_LEARNINGS: _scan("phase1_learnings") + _scan("phase2_learnings"),
    }


def _build_planner() -> PlannerRecall:
    ve = VectorEngine()
    ls = LearningStore()
    es = EvolutionStore()
    if not ve.solution_table_exists():
        raise AnchorError("活库缺 solution_chunks")
    if not ls.table_exists() or not es.table_exists():
        raise AnchorError("活库缺 phase1/phase2_learnings 表")
    prod = ProductionRecall(ls, ve, extra_learning_stores=(es,))
    # r3 稠密语义钉子（GATE1-LANE-A L4）：引擎批已落地，`rules_version` 是
    # 构造器正式参数。原先的 `try: ...(rules_version="r3") / except TypeError:
    # ...()` 会在参数被改名时**静默**退回构造器默认（r4），把 r3 稠密基线换成
    # 混合召回——那是一种看起来"能跑"的错。改为显式前置 + 事后核对，缺参数
    # 或实际规则版本不符一律 AnchorError。
    if "rules_version" not in inspect.signature(
            PlannerRecall.__init__).parameters:
        raise AnchorError(
            "PlannerRecall 构造器无 rules_version 参数（r3 稠密基线不可钉）")
    planner = PlannerRecall(prod, rules_version="r3")
    if getattr(planner, "rules_version", None) != "r3":
        raise AnchorError(
            f"构造出的 planner rules_version="
            f"{getattr(planner, 'rules_version', None)!r}，期望 'r3'")
    return planner


def _payload(case: dict) -> dict:
    if case["track"] == "track2":
        return {"question": case["query"]}
    return {"name": case["query"], "urls": []}


def lexical_query(case: dict) -> str:
    """预注册 §3.4：词法通道用**未扩展** base hint（与 setter 旧查询串一致）。"""
    from orchestrator.direction.planner_recall import legacy_direction_hint
    return legacy_direction_hint(case["track"], _payload(case))


def _judge(case: dict, ids: list[str], by_id: dict[str, dict]) -> tuple:
    gold_id = case.get("gold_id")
    if gold_id and gold_id in ids:
        return ids.index(gold_id) + 1, "gold_id"
    gold_text = case.get("gold_text")
    if gold_text:
        for rank, rid in enumerate(ids, 1):
            row = by_id.get(rid)
            if row is not None and gold_text in str(row.get("text") or ""):
                return rank, "gold_text"
    return None, None


def simulate() -> dict:
    fixture = _load_fixture()
    iter3 = _load_iter3()
    dense_by_id = {c["id"]: list(c["top8_ids"]) for c in iter3["cases"]}
    r3_hit = {c["id"]: bool(c["hit"]) for c in iter3["cases"]}

    corpora = build_corpora()
    indexes = {route: Bm25Index(rows) for route, rows in corpora.items()}
    by_id = {route: {str(r["id"]): r for r in rows}
             for route, rows in corpora.items()}

    planner = _build_planner()
    cases_out: list[dict] = []
    for case in fixture["cases"]:
        hints = build_route_hints(case["track"], _payload(case),
                                  tier=case["tier"])
        projects = matched_projects(case["track"], _payload(case))
        ret = planner.retrieve_with_hints(
            hints, site="direction", matched_projects=projects)
        route = case["route"]
        dense_ids = [str(r.get("id"))
                     for r in ret[ROUTE_KEY[route]][:TOP_K]]
        if dense_ids != dense_by_id[case["id"]]:
            raise AnchorError(
                f"稠密侧与冻结 iter3 top8_ids 不一致：{case['id']}")
        lex = indexes[route].top(lexical_query(case), TOP_K)
        fused, margin = rrf_fuse(dense_ids, lex)
        ids = [rid for _s, rid in fused]
        rank, via = _judge(case, ids, by_id[route])
        cases_out.append({
            "id": case["id"],
            "route": route,
            "track": case["track"],
            "tier": case["tier"],
            "dense_ids": dense_ids,
            "r3_hit": r3_hit[case["id"]],
            "lexical_top8": [{"score": round(s, 6), "id": rid}
                             for s, rid in lex],
            "fused_top8": [{"score": round(s, 10), "id": rid}
                           for s, rid in fused],
            "fused_ids": ids,
            "hit": rank is not None,
            "rank": rank,
            "matched_by": via,
            "cut_margin_8_9": margin,
            "flip_loss": r3_hit[case["id"]] and rank is None,
        })

    def _agg(route: str | None) -> dict:
        sub = [c for c in cases_out if route is None or c["route"] == route]
        hits = sum(1 for c in sub if c["hit"])
        return {"hits": hits, "total": len(sub),
                "gold_at_8": round(hits / len(sub), 4) if sub else 0.0}

    res9 = [c for c in cases_out if not c["r3_hit"]]
    return {
        "schema": SCHEMA,
        "note": ("设计期参考模拟（预注册 §3 规则规格的独立实现）；"
                 "稠密侧逐案强断言 == 冻结 iter3 报告 top8_ids。"),
        "fixture_sha256": FIXTURE_SHA256,
        "iter3_report_sha256": ITER3_REPORT_SHA256,
        "rules": {"ascii_token_regex": ASCII_RE.pattern,
                  "cjk_ngram": CJK_NGRAM, "bm25_k1": BM25_K1,
                  "bm25_b": BM25_B, "rrf_k": RRF_K, "top_k": TOP_K,
                  "index_doc_types_excluded": sorted(INDEX_DOC_TYPES),
                  "lexical_query": "legacy_direction_hint（未扩展 base）"},
        "corpus_rows": {route: len(rows) for route, rows in corpora.items()},
        "metrics": {"overall": _agg(None),
                    "kb_route": _agg(ROUTE_KB),
                    "solution_route": _agg(ROUTE_SOLUTIONS),
                    "learning_route": _agg(ROUTE_LEARNINGS)},
        "flip_loss": sorted(c["id"] for c in cases_out if c["flip_loss"]),
        "recovered": sorted(
            ({"id": c["id"], "rank": c["rank"],
              "matched_by": c["matched_by"]} for c in res9 if c["hit"]),
            key=lambda c: c["id"]),
        "still_missed": sorted(c["id"] for c in res9 if not c["hit"]),
        "cases": cases_out,
    }


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check", default=None,
                    help="对注册件逐字节复核（不出分、不写文件）")
    args = ap.parse_args(argv)
    try:
        payload = simulate()
    except AnchorError as exc:
        print(f"[T6R4-REF][STOP] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = _dump(payload)
    if args.check:
        target = Path(args.check)
        if _sha256(target) != hashlib.sha256(text.encode("utf-8")).hexdigest():
            print(f"[T6R4-REF][STOP] 注册件逐字节不符：{target}", file=sys.stderr)
            return 2
        print(f"[T6R4-REF] check ok {target}")
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    m = payload["metrics"]
    print(f"overall {m['overall']['hits']}/{m['overall']['total']} "
          f"={m['overall']['gold_at_8']}")
    print(f"kb {m['kb_route']['hits']}/{m['kb_route']['total']} "
          f"solutions {m['solution_route']['hits']}/"
          f"{m['solution_route']['total']} "
          f"learnings {m['learning_route']['hits']}/"
          f"{m['learning_route']['total']}")
    print(f"flip_loss={payload['flip_loss']}")
    print(f"recovered={[c['id'] for c in payload['recovered']]}")
    print(f"still_missed={payload['still_missed']}")
    print(f"out={out} sha256={_sha256(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
