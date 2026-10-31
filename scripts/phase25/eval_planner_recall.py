#!/usr/bin/env python3
"""Phase 2.5 Task6（AC-T6-4）：Gate1 四路召回 gold@8 预注册机判。

0 付费、对活 lancedb 只读（绝不建表/写块/调方舟）：embedding 全程走
/mnt 本地 mpnet 缓存，脚本启动即置三位离线开关（已显式导出者不覆盖），
任何模型下载企图只会报错，不会静默触网。

判据（阈值冻结在 fixture，代码里没有第二个事实源；禁止改标签/改界翻案）：
  overall        = 全部 case 的 gold∈top8 比例，阈值 ≥0.80；
  solution 子路  = route=solutions 的 case 子集，≥0.75；
  learning 子路  = route=learnings 的 case 子集，≥0.75。
case 的 gold 形如 {"gold_id": 32 位行 id} 或 {"gold_text": 逐字指纹子串}，
标签从 Phase 2 真实 req 证据与 Task 5 历史墙事实**逆向工程**（见 fixture
provenance 字段），不按检索结果回填（gold-fishing 即预注册违规）。

预注册的唯一一次修复迭代只允许改规则（synonyms.yaml 同义词 / 固定并行
查询规则集）：每次跑都落一份带迭代号的报告，--prior-iterations 可把历史
迭代号数字并进本次报告的 iterations[]。阈值不过且迭代已用尽 → exit 3
（STOP，交 team-lead 裁决），不自动改 fixture、不改阈值。

用法：
  python scripts/phase25/eval_planner_recall.py [--fixture PATH] \
      [--json-out PATH] [--iteration 1] [--prior-iterations a.json b.json]
退出码：0=过阈；3=未过阈（判据性失败）；2=装配/活库缺失等前置失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 离线开关必须在任何 lancedb/fastembed 导入前生效（0 下载硬保证）。
for _var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_var, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.runtime_paths import phase25_root  # noqa: E402
from orchestrator.direction.planner_recall import PlannerRecall  # noqa: E402
from memory_flywheel.phase1.learning_store import LearningStore  # noqa: E402
from memory_flywheel.phase2.evolution_store import EvolutionStore  # noqa: E402
from memory_flywheel.recall import ProductionRecall  # noqa: E402
from memory_flywheel.vector_engine import VectorEngine  # noqa: E402

DEFAULT_FIXTURE = (REPO_ROOT / "tests" / "milestone25" / "fixtures"
                   / "planner_golden_queries.json")
ROUTE_KEY = {"kb": "kb_hits", "solutions": "solution_hits",
             "learnings": "learning_hits"}
SCHEMA = "simpraxis.phase25.planner_gold_eval.v1"


def load_cases(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "simpraxis.phase25.planner_golden.v1":
        raise ValueError(f"golden fixture schema 不符：{path}")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) < 25:
        raise ValueError(f"golden case 数必须 ≥25，实得 {len(cases) if isinstance(cases, list) else 0}")
    required = {"id", "query", "track", "tier", "route", "provenance"}
    for c in cases:
        missing = required - set(c)
        if missing:
            raise ValueError(f"case {c.get('id')!r} 缺字段 {missing}")
        if c["track"] not in ("track1", "track2"):
            raise ValueError(f"case {c['id']} track 非法：{c['track']}")
        if c["route"] not in ROUTE_KEY:
            raise ValueError(f"case {c['id']} route 非法：{c['route']}")
        if not ("gold_id" in c or "gold_text" in c):
            raise ValueError(f"case {c['id']} 必须给 gold_id 或 gold_text")
    thresholds = data.get("thresholds") or {}
    for key in ("overall", "solution_route", "learning_route"):
        if not isinstance(thresholds.get(key), (int, float)):
            raise ValueError(f"fixture 缺数值阈值 thresholds.{key}")
    return data


def build_planner() -> PlannerRecall:
    """生产同构装配：同一 VectorEngine + phase1 主学习库 + phase2 额外库。"""
    ve = VectorEngine()
    ls = LearningStore()
    es = EvolutionStore()
    if not ve.solution_table_exists():
        raise RuntimeError("活库缺 solution_chunks（先跑 ingest_solutions）")
    if not ls.table_exists() or not es.table_exists():
        raise RuntimeError("活库缺 phase1/phase2_learnings 表")
    prod = ProductionRecall(ls, ve, extra_learning_stores=(es,))
    return PlannerRecall(prod)


def _payload(case: dict) -> dict:
    if case["track"] == "track2":
        return {"question": case["query"]}
    return {"name": case["query"], "urls": []}


def evaluate(planner: PlannerRecall, cases: list[dict], *,
             top_k: int = 8) -> list[dict]:
    """逐 case 跑四路（只取该 case 标注路的 top_k），返回判定明细。"""
    results: list[dict] = []
    for case in cases:
        # 走生产主入口 retrieve()：r2 项目作用域并行探针的项目识别在内部
        # 基于未扩展 base hint 完成（与 setter.run_direction planner 支路同构）。
        ret = planner.retrieve(case["track"], _payload(case),
                               tier=case["tier"], site="direction")
        rows = ret[ROUTE_KEY[case["route"]]][:top_k]
        ids = [str(r.get("id")) for r in rows]
        rank = None
        via = None
        gold_id = case.get("gold_id")
        gold_text = case.get("gold_text")
        if gold_id and gold_id in ids:
            rank, via = ids.index(gold_id) + 1, "gold_id"
        if rank is None and gold_text:
            for i, r in enumerate(rows, 1):
                if gold_text in str(r.get("text", "")):
                    rank, via = i, "gold_text"
                    break
        results.append({
            "id": case["id"], "route": case["route"],
            "track": case["track"], "tier": case["tier"],
            "hit": rank is not None, "rank": rank, "matched_by": via,
            "top8_ids": ids,
            "provenance": case.get("provenance"),
        })
    return results


def _ratio(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def summarize(data: dict, results: list[dict]) -> dict:
    th = data["thresholds"]
    n = len(results)
    hits = sum(1 for r in results if r["hit"])

    route_threshold_key = {"solutions": "solution_route",
                           "learnings": "learning_route"}

    def subset(route: str) -> tuple[int, int, float, float]:
        sub = [r for r in results if r["route"] == route]
        h = sum(1 for r in sub if r["hit"])
        return h, len(sub), _ratio(h, len(sub)), th[route_threshold_key[route]]

    sol_h, sol_n, sol_r, sol_th = subset("solutions")
    lr_h, lr_n, lr_r, lr_th = subset("learnings")
    overall_r = _ratio(hits, n)
    passed = {
        "overall": overall_r >= th["overall"],
        "solution_route": sol_r >= sol_th,
        "learning_route": lr_r >= lr_th,
    }
    return {
        "n_cases": n,
        "overall": {"hits": hits, "total": n, "gold_at_8": overall_r,
                    "threshold": th["overall"], "pass": passed["overall"]},
        "solution_route": {"hits": sol_h, "total": sol_n,
                           "gold_at_8": sol_r, "threshold": sol_th,
                           "pass": passed["solution_route"]},
        "learning_route": {"hits": lr_h, "total": lr_n,
                           "gold_at_8": lr_r, "threshold": lr_th,
                           "pass": passed["learning_route"]},
        "kb_route_observed": {
            "total": sum(1 for r in results if r["route"] == "kb"),
            "hits": sum(1 for r in results
                        if r["route"] == "kb" and r["hit"])},
        "all_pass": all(passed.values()),
        "misses": [{"id": r["id"], "route": r["route"],
                    "provenance": r["provenance"]}
                   for r in results if not r["hit"]],
    }


def run(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--iteration", type=int, default=1,
                    help="规则迭代号（1=预注册首跑；唯一修复迭代用 2）")
    ap.add_argument("--rules-version", default=None,
                    help="规则集版本标签（缺省读 fixture.rules_version）")
    ap.add_argument("--prior-iterations", nargs="*", default=[],
                    help="既往迭代报告 JSON，把其指标并入 iterations[] 留痕")
    args = ap.parse_args(argv)

    try:
        data = load_cases(Path(args.fixture))
        planner = build_planner()
        results = evaluate(planner, data["cases"])
        metrics = summarize(data, results)
    except Exception as exc:  # 前置/装配失败：判据前失败，exit 2 区分
        print(f"[PLANNER GOLD EVAL][STOP] {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    prior = []
    for p in args.prior_iterations:
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            prior.append({"iteration": d.get("iteration"),
                          "rules_version": d.get("rules_version"),
                          "metrics": {k: d["metrics"][k] for k in
                                      ("overall", "solution_route",
                                       "learning_route")}})
        except (OSError, ValueError, KeyError) as exc:
            print(f"[PLANNER GOLD EVAL][STOP] 既往迭代件不可读 {p}: {exc}",
                  file=sys.stderr)
            return 2

    report = {
        "schema": SCHEMA,
        "ts_utc": utc_now_iso_z(),
        "iteration": args.iteration,
        "rules_version": args.rules_version
        or data.get("rules_version", "unknown"),
        "fixture": str(Path(args.fixture).resolve()),
        "top_k": 8,
        "metrics": metrics,
        "iterations": prior + [{
            "iteration": args.iteration,
            "rules_version": args.rules_version
            or data.get("rules_version", "unknown"),
            "overall": metrics["overall"]["gold_at_8"],
            "solution_route": metrics["solution_route"]["gold_at_8"],
            "learning_route": metrics["learning_route"]["gold_at_8"]}],
        "cases": results,
    }
    out = Path(args.json_out) if args.json_out else (
        phase25_root() / "task6_recall"
        / f"planner_gold_iter{args.iteration}_"
          f"{report['ts_utc'].replace(':', '')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass

    m = metrics
    print(f"iter={args.iteration} rules={report['rules_version']}")
    print(f"overall        {m['overall']['hits']}/{m['overall']['total']} "
          f"gold@8={m['overall']['gold_at_8']} "
          f"(阈值 {m['overall']['threshold']}, "
          f"{'PASS' if m['overall']['pass'] else 'FAIL'})")
    print(f"solution_route {m['solution_route']['hits']}/"
          f"{m['solution_route']['total']} "
          f"gold@8={m['solution_route']['gold_at_8']} "
          f"(阈值 {m['solution_route']['threshold']}, "
          f"{'PASS' if m['solution_route']['pass'] else 'FAIL'})")
    print(f"learning_route {m['learning_route']['hits']}/"
          f"{m['learning_route']['total']} "
          f"gold@8={m['learning_route']['gold_at_8']} "
          f"(阈值 {m['learning_route']['threshold']}, "
          f"{'PASS' if m['learning_route']['pass'] else 'FAIL'})")
    if m["misses"]:
        print("misses: " + ", ".join(x["id"] for x in m["misses"]))
    print(f"报告：{out}")
    return 0 if m["all_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(run())
