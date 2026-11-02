#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 活库验收（kb_integration：只读、0 付费）。

预注册 ``scripts/phase25/task6_r4_prereg.md`` §4.6 与 §8 条件的**活库**部分：

  1. 词法语料身份：kb 1364（排除 root_index/index）/ solutions 94 /
     learnings 45（phase1 44 ∪ phase2 1），逐路 id 集 sha8 与冻结值全等；
  2. 生产 r4 逐案 == §6 冻结点预测快照（47 案 ``fused_ids`` 逐字节全等 +
     hit/rank/matched_by + 四路聚合指标），任何不一致即实现未复现冻结规格；
  3. r3 分支仍逐字复现冻结 iter3 报告（``top8_ids`` 与 hit 全等）——证明
     r4 落地没有改动 r3 语义（历史锚点零漂移）。

纪律：mpnet 走 /mnt 本地缓存（三位离线开关）；只读活库，不建表/不写块/
不调网关；裸跑自动 skip（``SIMPRAXIS_KB_INTEGRATION=1`` 才执行）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.kb_support import require_kb_integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (REPO_ROOT / "tests" / "milestone25" / "fixtures"
           / "planner_golden_queries.json")
PREDICTIONS = (REPO_ROOT / "scripts" / "phase25" / "snapshots"
               / "task6_r4_point_predictions.json")

FIXTURE_SHA256 = (
    "f5c5cc58890ce5f9aff1c5f0dd6b97bf1486e716060bcd29d612793458b7c3a6")
PREDICTIONS_SHA256 = (
    "e7e1bc3853ac199bdc25bb99feabda19b1f688b5f4f214f10483f6a9d0740a76")
ITER3_SHA256 = "f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307"

# §3.5 冻结语料身份（2026-11-02 活库复算：行数 + 排序 id 集 sha256 前 8）
CORPUS_ROWS = {"kb": 1364, "solutions": 94, "learnings": 45}
CORPUS_ID_SHA8 = {"kb": "60a3cf27", "solutions": "cdab79c7",
                  "learnings": "dcfdf6a3"}
ROUTE_KEY = {"kb": "kb_hits", "solutions": "solution_hits",
             "learnings": "learning_hits"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _corpus_id_sha8(ids) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:8]


def _payload(case: dict) -> dict:
    if case["track"] == "track2":
        return {"question": case["query"]}
    return {"name": case["query"], "urls": []}


def _build(rules_version: "str | None" = None):
    from memory_flywheel.phase1.learning_store import LearningStore
    from memory_flywheel.phase2.evolution_store import EvolutionStore
    from memory_flywheel.recall import ProductionRecall
    from memory_flywheel.vector_engine import VectorEngine
    from orchestrator.direction.planner_recall import PlannerRecall

    ve = VectorEngine()
    ls = LearningStore()
    es = EvolutionStore()
    assert ve.solution_table_exists()
    prod = ProductionRecall(ls, ve, extra_learning_stores=(es,))
    if rules_version is None:
        return PlannerRecall(prod)              # 生产默认（§3.7 = r4）
    return PlannerRecall(prod, rules_version=rules_version)


def _judge(case: dict, rows: list[dict]) -> tuple:
    """与冻结判分器同判据：gold_id 先行，其次 gold_text 子串。"""
    ids = [str(r.get("id")) for r in rows]
    gold_id = case.get("gold_id")
    if gold_id and gold_id in ids:
        return ids.index(gold_id) + 1, "gold_id"
    gold_text = case.get("gold_text")
    if gold_text:
        for i, row in enumerate(rows, 1):
            if gold_text in str(row.get("text", "")):
                return i, "gold_text"
    return None, None


def _iter3_report() -> dict:
    from orchestrator.common.runtime_paths import phase25_root
    hits = sorted((phase25_root() / "task6_recall").glob(
        "planner_gold_iter3_*.json"))
    assert len(hits) == 1, f"iter3 报告须恰 1 份，实得 {len(hits)}"
    assert _sha256(hits[0]) == ITER3_SHA256
    return json.loads(hits[0].read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def offline_env():
    import os
    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
                "HF_DATASETS_OFFLINE"):
        os.environ[var] = "1"


@pytest.mark.kb_integration
def test_live_lexical_corpus_identity(offline_env):
    require_kb_integration(data=True)
    assert _sha256(PREDICTIONS) == PREDICTIONS_SHA256
    planner = _build()
    assert planner.rules_version == "r4"
    state = planner._lexical_state()
    for route, want_rows in CORPUS_ROWS.items():
        rows = state[route]["index"].rows
        ids = [str(r["id"]) for r in rows]
        assert len(ids) == want_rows, route
        assert _corpus_id_sha8(ids) == CORPUS_ID_SHA8[route], route
        assert state[route]["index"].n == want_rows


@pytest.mark.kb_integration
def test_live_production_r4_equals_frozen_point_predictions(offline_env):
    require_kb_integration(data=True)
    assert _sha256(FIXTURE) == FIXTURE_SHA256
    assert _sha256(PREDICTIONS) == PREDICTIONS_SHA256
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snap = json.loads(PREDICTIONS.read_text(encoding="utf-8"))
    pred = {c["id"]: c for c in snap["cases"]}
    assert len(pred) == len(fixture["cases"]) == 47

    planner = _build()                          # 生产默认 r4
    results = []
    for case in fixture["cases"]:
        ret = planner.retrieve(case["track"], _payload(case),
                               tier=case["tier"], site="direction")
        rows = ret[ROUTE_KEY[case["route"]]]
        ids = [str(r.get("id")) for r in rows]
        # 尾席保持（预注册 §3.3 修订 A1）的**活库**断言（GATE1-LANE-A L6）：
        # 原先只比前 8 行，A1 想防的「r4 静默丢弃 phase2 演化志行」恰好看不见。
        # learning 路稠密侧是 8 行 phase1 + 1 行 phase2，融合段只定义 top-8，
        # 第 9 行必须作为尾席原序追加；kb/solutions 路稠密侧恒 8 行，不得凭空
        # 长出尾席（那也是未声明的注入面变化）。
        assert len(ids) == len(set(ids)), f"{case['id']} 输出含重复 id"
        if case["route"] == "learnings":
            assert len(rows) in (8, 9), f"{case['id']} learning 路长度异常 {len(ids)}"
            assert any(r.get("learning_table") == "phase2_learnings"
                       for r in rows), f"{case['id']} 尾席丢了 phase2 演化志行"
        else:
            assert len(rows) == 8, f"{case['id']} 非 learning 路不该有尾席"
        rows = rows[:8]
        want = pred[case["id"]]
        assert ids[:8] == want["fused_ids"], f"{case['id']} 融合 top-8 与冻结快照不符"
        rank, via = _judge(case, rows)
        assert rank == want["rank"], case["id"]
        assert via == want["matched_by"], case["id"]
        assert (rank is not None) == want["hit"], case["id"]
        results.append({"id": case["id"], "route": case["route"],
                        "hit": rank is not None})

    def _agg(route: "str | None") -> dict:
        sub = [r for r in results if route is None or r["route"] == route]
        hits = sum(1 for r in sub if r["hit"])
        return {"hits": hits, "total": len(sub),
                "gold_at_8": round(hits / len(sub), 4)}

    got = {"overall": _agg(None), "kb_route": _agg("kb"),
           "solution_route": _agg("solutions"),
           "learning_route": _agg("learnings")}
    for key, want in snap["metrics"].items():
        assert got[key] == want, key
    assert snap["flip_loss"] == []


@pytest.mark.kb_integration
def test_live_r3_branch_reproduces_frozen_iter3(offline_env):
    require_kb_integration(data=True)
    assert _sha256(FIXTURE) == FIXTURE_SHA256
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    iter3 = _iter3_report()
    frozen = {c["id"]: c for c in iter3["cases"]}
    assert len(frozen) == 47

    planner = _build(rules_version="r3")
    assert planner.rules_version == "r3"
    for case in fixture["cases"]:
        ret = planner.retrieve(case["track"], _payload(case),
                               tier=case["tier"], site="direction")
        rows = ret[ROUTE_KEY[case["route"]]][:8]
        ids = [str(r.get("id")) for r in rows]
        want = frozen[case["id"]]
        assert ids == list(want["top8_ids"]), f"{case['id']} r3 top-8 漂移"
        rank, via = _judge(case, rows)
        assert (rank is not None) == bool(want["hit"]), case["id"]
