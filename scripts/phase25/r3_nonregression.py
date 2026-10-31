#!/usr/bin/env python3
"""Task 6 r3 逐案非回归 + 严口径复核（lead 自有只读分析脚本）。

在 iter3 首条运行命令执行**之前**落盘并登记 sha256（还 MEDIUM-1 流程债）。
只读三份 JSON 报告，零联网零付费；不改钉死判分器一字。

输入：
  --iter3 PATH                r3 出分报告（exit 0/3 均可分析）
  --iter1 PATH                钉哈希的 iter1 报告
  --iter2 PATH                钉哈希的 iter2 报告（H2 来源）

输出（stdout JSON；--json-out 可选落盘）：
  anchors_ok            两份历史报告 sha256 是否匹配冻结常量
  h2/h3                 命中 id 集（排序）
  nonregression_pass    H2 ⊆ H3（含翻失明细）
  strict                仅 gold_id 计中的严口径（gold_text 回退不计）
  prediction_match      H3 是否恰为登记预测集 H2 ∪ 三增益案（见 PRED_GAINS）
  thresholds            iter3 报告主口径三门 all_pass 原样透传

门 2 只认本脚本的 nonregression_pass / prediction_match / strict；
任何一项 anchors_ok=False 则全部结论作废。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

ITER1_SHA256 = (
    "05a6c2647bece5243616fdb473596c11fcb276424f4259d29f4cbd4d8ca2a053"
)
ITER2_SHA256 = (
    "5aacab55e6fe3ebd6a520eed0b6283f1c5974cba88967682b542a4da841f5c81"
)

# 门 1 对抗模拟（r2 复现 29/29 零分歧、q=1 严口径 0.6170/0.4444 校验过的
# 同一模拟）在首跑前登记的逐案预测：读法 B（保底席仅 S-only）q=4 下恰三增益。
PRED_GAINS = ["sol_win1_env", "sol_win5_finger", "sol_win10_exit"]
PRED_METRICS = {
    "overall": {"hits": 38, "total": 47},
    "kb": {"hits": 13, "total": 17},
    "solutions": {"hits": 14, "total": 18},
    "learnings": {"hits": 11, "total": 12},
}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _hits(report: dict[str, Any]) -> set[str]:
    return {r["id"] for r in report["cases"] if r.get("hit")}


def _strict(report: dict[str, Any]) -> dict[str, Any]:
    per: dict[str, list[int]] = {}
    for row in report["cases"]:
        route = row["route"]
        slot = per.setdefault(route, [0, 0])
        slot[1] += 1
        if row.get("hit") and row.get("matched_by") == "gold_id":
            slot[0] += 1
    out: dict[str, Any] = {}
    total_h = total_n = 0
    for route, (hits, n) in sorted(per.items()):
        out[route] = {"hits": hits, "total": n,
                      "gold_id_at_8": round(hits / n, 4) if n else None}
        total_h += hits
        total_n += n
    out["overall"] = {"hits": total_h, "total": total_n,
                      "gold_id_at_8": round(total_h / total_n, 4) if total_n else None}
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iter3", required=True)
    ap.add_argument("--iter1", required=True)
    ap.add_argument("--iter2", required=True)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    anchors = {
        "iter1": {"path": args.iter1, "expected": ITER1_SHA256,
                  "actual": _sha256(args.iter1)},
        "iter2": {"path": args.iter2, "expected": ITER2_SHA256,
                  "actual": _sha256(args.iter2)},
    }
    anchors_ok = all(a["actual"] == a["expected"] for a in anchors.values())

    rep1, rep2, rep3 = _load(args.iter1), _load(args.iter2), _load(args.iter3)
    h1, h2, h3 = _hits(rep1), _hits(rep2), _hits(rep3)

    flips = sorted(h2 - h3)
    gains = sorted(h3 - h2)
    h1_flips = sorted(h1 - h3)

    predicted = h2 | set(PRED_GAINS)
    primary = rep3.get("metrics", {})
    # PRED_METRICS 用内部路由名，判分器报告 metrics 键名不同：
    # kb→kb_route_observed、solutions→solution_route、learnings→learning_route。
    report_key = {
        "overall": "overall",
        "kb": "kb_route_observed",
        "solutions": "solution_route",
        "learnings": "learning_route",
    }
    pred_metrics_ok = {
        route: primary.get(report_key[route], {}).get("hits") == spec["hits"]
        and primary.get(report_key[route], {}).get("total") == spec["total"]
        for route, spec in PRED_METRICS.items()
    }

    result = {
        "schema": "simpraxis.task6.r3_nonregression.v1",
        "iter3_iteration": rep3.get("iteration"),
        "iter3_rules_version": rep3.get("rules_version"),
        "anchors_ok": anchors_ok,
        "anchors": anchors,
        "h1_hits": sorted(h1),
        "h2_hits": sorted(h2),
        "h3_hits": sorted(h3),
        "nonregression_pass": anchors_ok and not flips and not h1_flips,
        "flips_h2_to_h3": flips,
        "flips_h1_to_h3": h1_flips,
        "gains_h3_over_h2": gains,
        "strict": _strict(rep3),
        "prediction": {
            "pred_gains": PRED_GAINS,
            "observed_gains": gains,
            "predicted_hit_set_match": h3 == predicted,
            "predicted_metrics_match": all(pred_metrics_ok.values()),
            "predicted_metrics_per_route": pred_metrics_ok,
        },
        "prediction_match": anchors_ok and h3 == predicted
        and all(pred_metrics_ok.values()),
        "thresholds_primary": {
            "overall": primary.get("overall", {}).get("pass"),
            "solution_route": primary.get("solution_route", {}).get("pass"),
            "learning_route": primary.get("learning_route", {}).get("pass"),
            "all_pass": primary.get("all_pass"),
        },
    }

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
