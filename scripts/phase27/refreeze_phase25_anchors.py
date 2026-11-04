#!/usr/bin/env python3
"""Phase 2.7 Task 6 收口：内容层扩容后的 Phase 2.5 活库锚点**重冻 v2**（0 付费）。

## 为什么需要重冻（锁内适配，2026-11-04）

Task 6 按计划 §T6.7 在 Gate2 三卡真人 approve 之后回收
``solutions/track1_tools/04_libero_vla``（KB ``b5de4bd``）：``solution_chunks``
94 → 106（+12 块）。Phase 2.5 Task 6 Stage B 的**活库**验收锚在 94 行语料上
机产的"iter3 报告（r3 稠密基线）"与"r4 点预测快照"上；语料一进 solutions 路，
top-8 的成员/次序必然重排——这不是代码漂移，是**经批准的语料变更**。

判据强度不降的做法（本脚本的执行口径）：

1. **v1 件逐字节不动**：``planner_gold_iter3_*.json``（sha ``f1c0e7f4…``）与
   ``snapshots/task6_r4_point_predictions.json``（sha ``e7e1bc38…``）仍是
   Phase 2.5 的历史证据，测试里 sha 照旧钉死；
2. **v2 件由机器重产、不是手改**：iter3-v2 走**同一个判分实现**
   （``eval_planner_recall.evaluate/summarize``），r4-v2 走**独立的第二实现**
   （``task6_r4_reference_sim``，与引擎 r4 两套实现互证的性质不变），
   dense 侧锚在 iter3-v2 上；
3. **hit 零漂移是重冻的**前置条件：只要新语料改变了任何一案的 hit/miss
   （真回归或真收益），本脚本 ``AnchorError`` 拒产 v2 —— 那种情况必须由人裁决，
   不得用"重冻"盖过去；
4. **漂移逐案登记**：v1→v2 的 top-8 成员差、gold 名次差全部写进
   ``snapshots/phase25_anchor_refreeze_v2.json``，测试按此围栏断言
   「变化必须逐案等于登记值」。

## 用法

    python scripts/phase27/refreeze_phase25_anchors.py            # 产 v2（写盘）
    python scripts/phase27/refreeze_phase25_anchors.py --dry-run  # 只算不写

零联网零付费（三位离线开关 setdefault）；只读活库（不建表/不写块/不调网关）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

for _var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(_var, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.phase25 import eval_planner_recall as EVAL  # noqa: E402
from scripts.phase25 import task6_r4_reference_sim as SIM  # noqa: E402

SNAPSHOTS = REPO_ROOT / "scripts" / "phase25" / "snapshots"
V1_PRED = SNAPSHOTS / "task6_r4_point_predictions.json"
V1_PROD_UNION = SNAPSHOTS / "task6_r3_mpnet_prod_union_snapshot.json"
V2_ITER3 = SNAPSHOTS / "planner_gold_iter3_corpus106.json"
V2_PRED = SNAPSHOTS / "task6_r4_point_predictions_corpus106.json"
MANIFEST = SNAPSHOTS / "phase25_anchor_refreeze_v2.json"

SCHEMA = "simpraxis.phase27.phase25_anchor_refreeze.v1"
V2_SCHEMA = "simpraxis.phase25.planner_gold_eval.v1"

# v1 注册 sha（历史证据，本脚本不写、测试照旧钉死）
V1_ITER3_SHA = SIM.ITER3_REPORT_SHA256
V1_PRED_SHA = (
    "e7e1bc3853ac199bdc25bb99feabda19b1f688b5f4f214f10483f6a9d0740a76")
V1_PROD_UNION_SHA = (
    "53c043dc202c7de8c611fb523ee842546b47f9d5cbbbaf72633064fcbf98cc7b")


class AnchorError(RuntimeError):
    """重冻前置不满足（不得产出 v2）。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _v1_iter3() -> dict:
    from orchestrator.common.runtime_paths import phase25_root
    hits = sorted((phase25_root() / "task6_recall").glob(
        "planner_gold_iter3_*.json"))
    if len(hits) != 1:
        raise AnchorError(f"v1 iter3 报告须恰 1 份，实得 {len(hits)}")
    if _sha256(hits[0]) != V1_ITER3_SHA:
        raise AnchorError(f"v1 iter3 报告 sha 与注册值不符：{hits[0]}")
    return json.loads(hits[0].read_text(encoding="utf-8"))


def _corpus_rows() -> dict:
    """活语料行数（与 re-freeze 同时刻采集，写进 v2 件与 manifest）。"""
    from memory_flywheel import kb_pins
    import lancedb
    db = lancedb.connect(kb_pins.default_lancedb_uri())
    out = {}
    for name in ("kb_chunks", "solution_chunks", "phase1_learnings",
                 "phase2_learnings"):
        out[name] = db.open_table(name).count_rows()
    return out


def build_v2_iter3(rows: dict) -> dict:
    """r3 判分实现（同一个 ``eval_planner_recall``）在新语料上的逐案产物。"""
    data = EVAL.load_cases(SIM.FIXTURE)
    planner = SIM._build_planner()          # 显式 r3（内含版本核对）
    if getattr(planner, "rules_version", None) != "r3":
        raise AnchorError("planner 不是 r3")
    results = EVAL.evaluate(planner, data["cases"])
    metrics = EVAL.summarize(data, results)
    return {
        "schema": V2_SCHEMA,
        "ts_utc": _now_z(),
        "iteration": 3,
        "rules_version": "r3",
        "fixture": str(SIM.FIXTURE.resolve()),
        "top_k": 8,
        "metrics": metrics,
        "iterations": [{"iteration": 3, "rules_version": "r3",
                        "overall": metrics["overall"]["gold_at_8"],
                        "solution_route": metrics["solution_route"]["gold_at_8"],
                        "learning_route": metrics["learning_route"]["gold_at_8"]}],
        "cases": results,
        "refreeze": {
            "kind": "phase27_task6_content_expansion",
            "supersedes": "planner_gold_iter3_*.json（sha " + V1_ITER3_SHA + "）",
            "corpus_rows": rows,
            "reason": ("Phase 2.7 Task 6 按计划回收 track1_tools/04_libero_vla，"
                       "solution_chunks 94→106；solutions 路 top-8 重排，"
                       "hit 判定零漂移。"),
            "generated_by": "scripts/phase27/refreeze_phase25_anchors.py",
        },
    }


def _drift(cases_v1: list, cases_v2: list) -> dict:
    v1 = {c["id"]: c for c in cases_v1}
    v2 = {c["id"]: c for c in cases_v2}
    if set(v1) != set(v2):
        raise AnchorError("v1/v2 案集不同")
    sets: dict = {}
    ranks: dict = {}
    hits: list = []
    for cid in sorted(v1):
        a, b = v1[cid], v2[cid]
        if bool(a["hit"]) != bool(b["hit"]):
            hits.append({"id": cid, "v1": bool(a["hit"]), "v2": bool(b["hit"])})
        if a.get("rank") != b.get("rank"):
            ranks[cid] = {"v1": a.get("rank"), "v2": b.get("rank")}
        ai = [str(x) for x in a["top8_ids"]]
        bi = [str(x) for x in b["top8_ids"]]
        if ai != bi:
            sets[cid] = {"removed": sorted(set(ai) - set(bi)),
                         "added": sorted(set(bi) - set(ai)),
                         "order_changed": set(ai) == set(bi)}
    return {"hit_drift": hits, "rank_drift": ranks, "top8_drift": sets}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只算不写盘")
    args = ap.parse_args(argv)

    try:
        if _sha256(V1_PRED) != V1_PRED_SHA:
            raise AnchorError(f"v1 点预测 sha 与注册值不符：{V1_PRED}")
        rows = _corpus_rows()
        if rows.get("solution_chunks") != 106:
            raise AnchorError(
                f"solution_chunks 期望 106（Task 6 回收后），实得 "
                f"{rows.get('solution_chunks')}")
        v1_iter3 = _v1_iter3()
        v2_iter3 = build_v2_iter3(rows)
        drift3 = _drift(v1_iter3["cases"], v2_iter3["cases"])
        if drift3["hit_drift"]:
            raise AnchorError(
                f"r3 hit 出现漂移，禁止重冻（须人裁决）：{drift3['hit_drift']}")

        iter3_text = _dump(v2_iter3)
        tmp_iter3 = V2_ITER3 if not args.dry_run else (
            Path("/tmp") / V2_ITER3.name)
        tmp_iter3.parent.mkdir(parents=True, exist_ok=True)
        tmp_iter3.write_text(iter3_text, encoding="utf-8")

        v2_pred = SIM.simulate(iter3_path=tmp_iter3)
        pred_v1 = json.loads(V1_PRED.read_text(encoding="utf-8"))
        drift_p = _drift(
            [{"id": c["id"], "hit": c["hit"], "rank": c["rank"],
              "top8_ids": c["fused_ids"]} for c in pred_v1["cases"]],
            [{"id": c["id"], "hit": c["hit"], "rank": c["rank"],
              "top8_ids": c["fused_ids"]} for c in v2_pred["cases"]])
        if drift_p["hit_drift"]:
            raise AnchorError(
                f"r4 hit 出现漂移，禁止重冻（须人裁决）：{drift_p['hit_drift']}")

        pred_text = _dump(v2_pred)
        tmp_pred = V2_PRED if not args.dry_run else (Path("/tmp") / V2_PRED.name)
        tmp_pred.write_text(pred_text, encoding="utf-8")

        manifest = {
            "schema": SCHEMA,
            "ts_utc": _now_z(),
            "generated_by": "scripts/phase27/refreeze_phase25_anchors.py",
            "reason": ("Phase 2.7 Task 6 内容层回收（KB b5de4bd，Gate2 三卡 "
                       "approve）：solution_chunks 94→106，solutions 路 top-8 重排。"),
            "corpus_rows": rows,
            "v1": {
                "iter3_report": {"sha256": V1_ITER3_SHA,
                                 "path": "phase25 根 task6_recall/"
                                         "planner_gold_iter3_*.json"},
                "point_predictions": {
                    "sha256": V1_PRED_SHA,
                    "path": "scripts/phase25/snapshots/"
                            "task6_r4_point_predictions.json"},
                "prod_union_snapshot": {
                    "sha256_pre_task6": V1_PROD_UNION_SHA,
                    "sha256_after_task6": _sha256(V1_PROD_UNION),
                    "path": "scripts/phase25/snapshots/"
                            "task6_r3_mpnet_prod_union_snapshot.json",
                    "note": ("本件由 scripts/phase25/gen_r3_prod_union_snapshot.py "
                             "在 106 行活库上机产重产（Task 6 收口），"
                             "重产取证见 phase27 证据包 "
                             "manifests/r3_prod_union_snapshot_refresh.json。")},
            },
            "v2": {
                "iter3_report": {"path": str(V2_ITER3.relative_to(REPO_ROOT)),
                                 "sha256": hashlib.sha256(
                                     iter3_text.encode("utf-8")).hexdigest()},
                "point_predictions": {
                    "path": str(V2_PRED.relative_to(REPO_ROOT)),
                    "sha256": hashlib.sha256(
                        pred_text.encode("utf-8")).hexdigest()},
            },
            "metrics_v1_v2": {
                "r3": {"v1": v1_iter3["metrics"]["overall"],
                       "v2": v2_iter3["metrics"]["overall"],
                       "solution_route_v1": v1_iter3["metrics"]["solution_route"],
                       "solution_route_v2": v2_iter3["metrics"]["solution_route"],
                       "learning_route_v1": v1_iter3["metrics"]["learning_route"],
                       "learning_route_v2": v2_iter3["metrics"]["learning_route"]},
                "r4": {"v1": pred_v1["metrics"], "v2": v2_pred["metrics"]},
            },
            "hit_drift": {"r3": drift3["hit_drift"], "r4": drift_p["hit_drift"]},
            "rank_drift": {"r3": drift3["rank_drift"], "r4": drift_p["rank_drift"]},
            "top8_drift": {"r3": drift3["top8_drift"], "r4": drift_p["top8_drift"]},
            "flip_loss": {"v1": pred_v1.get("flip_loss"),
                          "v2": v2_pred.get("flip_loss")},
        }
        man_text = _dump(manifest)
        if args.dry_run:
            print(man_text)
            return 0
        MANIFEST.write_text(man_text, encoding="utf-8")

        print(f"[REFREEZE] v2 iter3   {V2_ITER3.name} "
              f"sha256={manifest['v2']['iter3_report']['sha256']}")
        print(f"[REFREEZE] v2 pred    {V2_PRED.name} "
              f"sha256={manifest['v2']['point_predictions']['sha256']}")
        print(f"[REFREEZE] manifest   {MANIFEST.name} "
              f"sha256={_sha256(MANIFEST)}")
        print(f"[REFREEZE] r3 {v2_iter3['metrics']['overall']['hits']}/"
              f"{v2_iter3['metrics']['overall']['total']} "
              f"(v1 {v1_iter3['metrics']['overall']['hits']}/"
              f"{v1_iter3['metrics']['overall']['total']}) "
              f"drift_cases={len(drift3['top8_drift'])}")
        print(f"[REFREEZE] r4 {v2_pred['metrics']['overall']['hits']}/"
              f"{v2_pred['metrics']['overall']['total']} "
              f"(v1 {pred_v1['metrics']['overall']['hits']}/"
              f"{pred_v1['metrics']['overall']['total']}) "
              f"drift_cases={len(drift_p['top8_drift'])}")
        print(f"[REFREEZE] hit_drift r3={drift3['hit_drift']} "
              f"r4={drift_p['hit_drift']}")
        return 0
    except AnchorError as exc:
        print(f"[REFREEZE][STOP] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
