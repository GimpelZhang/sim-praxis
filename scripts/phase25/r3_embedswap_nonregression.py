#!/usr/bin/env python3
"""Task 6 Stage A（换嵌入 iter4）逐案非回归 + 严口径 + 点预测机判（lead 只读件）。

在 iter4 首条绑定运行**之前**随预注册冻结提交落盘并登记 sha256（范式同
r3_nonregression.py）。只读四份 JSON 报告，零联网零付费；不改判分器一字。

输入：
  --iter4 PATH                Stage A 换嵌入出分报告（exit 0/3 均可分析）
  --iter1/--iter2/--iter3    钉哈希的历史报告（H3 来自 iter3）

机判字段（门 2 只认本脚本）：
  nonregression_pass          H3 ⊆ H4（零翻失，含 H1/H2 同源翻失明细）
  residual_recovery_strict    iter3 的 9 残留 miss 中，iter4 至少 1 案
                              matched_by=="gold_id" 直命中（gold_text 回退不计）
  point_prediction_match      增益集恰 {sol_win4_region}、零翻失、
                              主口径四路 == 39/13/15/11、严口径 == 36/11/14/11
  strict                      仅 gold_id 计中的严口径四路数
任一 anchors_ok=False 或 prereg_ok=False 则全部结论作废（verifier MF-B：
三个判决布尔均 and prereg_ok，预注册 sha 未回填/不符即 fail-closed）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

ITER1_SHA256 = (
    "05a6c2647bece5243616fdb473596c11fcb276424f4259d29f4cbd4d8ca2a053"
)
ITER2_SHA256 = (
    "5aacab55e6fe3ebd6a520eed0b6283f1c5974cba88967682b542a4da841f5c81"
)
ITER3_SHA256 = (
    "f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307"
)
# Stage A 预注册冻结件 sha（攒批122 冻结后回填；回填本身在冻结提交内一次成形）。
EMBEDSWAP_PREREG_PATH = "scripts/phase25/task6_embedding_swap_prereg.md"
EMBEDSWAP_PREREG_SHA256 = (
    "fcb3fcd698739300ea58bd590cdf94e1b14a1cff95775f2c2cb786074dc41185"
)  # v3.4 定稿回填（MAJOR-A＋critic MINOR 1–4 余文），随攒批122 一次成形；
# 预注册再改字必须重算本常量

# §7 冻结点预测：唯一有机制通路的残留案（生产 k8 可见、S-only 第 5 席差 1）。
PRED_GAIN = "sol_win4_region"
PRED_PRIMARY = {
    "overall": {"hits": 39, "total": 47},
    "kb": {"hits": 13, "total": 17},
    "solutions": {"hits": 15, "total": 18},
    "learnings": {"hits": 11, "total": 12},
}
PRED_STRICT = {
    "overall": {"hits": 36, "total": 47},
    "kb": {"hits": 11, "total": 17},
    "solutions": {"hits": 14, "total": 18},
    "learnings": {"hits": 11, "total": 12},
}
REPORT_KEY = {
    "overall": "overall",
    "kb": "kb_route_observed",
    "solutions": "solution_route",
    "learnings": "learning_route",
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
        slot = per.setdefault(row["route"], [0, 0])
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


def _metrics_match(report: dict[str, Any], spec: dict[str, dict[str, int]]):
    primary = report.get("metrics", {})
    per_route: dict[str, bool] = {}
    observed: dict[str, dict[str, Any]] = {}
    for route, want in spec.items():
        got = primary.get(REPORT_KEY[route], {})
        observed[route] = {"hits": got.get("hits"), "total": got.get("total")}
        per_route[route] = got.get("hits") == want["hits"] and got.get("total") == want["total"]
    return all(per_route.values()), per_route, observed


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iter4", required=True)
    ap.add_argument("--iter1", required=True)
    ap.add_argument("--iter2", required=True)
    ap.add_argument("--iter3", required=True)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    anchors = {
        "iter1": {"path": args.iter1, "expected": ITER1_SHA256,
                  "actual": _sha256(args.iter1)},
        "iter2": {"path": args.iter2, "expected": ITER2_SHA256,
                  "actual": _sha256(args.iter2)},
        "iter3": {"path": args.iter3, "expected": ITER3_SHA256,
                  "actual": _sha256(args.iter3)},
    }
    prereg = {
        "path": EMBEDSWAP_PREREG_PATH,
        "expected": EMBEDSWAP_PREREG_SHA256,
        "actual": _sha256(EMBEDSWAP_PREREG_PATH),
    }
    anchors_ok = all(a["actual"] == a["expected"] for a in anchors.values())
    prereg_ok = bool(EMBEDSWAP_PREREG_SHA256) and prereg["actual"] == prereg["expected"]

    rep1, rep2, rep3, rep4 = (_load(p) for p in (args.iter1, args.iter2,
                                                args.iter3, args.iter4))
    cases4 = {r["id"]: r for r in rep4["cases"]}
    cases3 = {r["id"]: r for r in rep3["cases"]}
    # defense-in-depth（verifier LOW）：fixture/scorer 双锚之外再机判案集一致。
    assert set(cases4) == set(cases3), (
        f"iter4/iter3 case id 集不一致：iter4-only={sorted(set(cases4) - set(cases3))} "
        f"iter3-only={sorted(set(cases3) - set(cases4))}")
    h1, h2, h3, h4 = _hits(rep1), _hits(rep2), _hits(rep3), _hits(rep4)

    flips = sorted(h3 - h4)
    gains = sorted(h4 - h3)

    # 9 残留案（iter3 未中）在 iter4 的逐案严格回收明细。
    residual3 = sorted(set(cases4) - h3)
    residual_detail = []
    strict_recoveries = []
    for cid in residual3:
        row = cases4[cid]
        hit = bool(row.get("hit"))
        matched_by = row.get("matched_by")
        strict_direct = hit and matched_by == "gold_id"
        residual_detail.append({
            "id": cid, "route": row["route"], "hit": hit,
            "matched_by": matched_by, "gold_id_direct": strict_direct,
        })
        if strict_direct:
            strict_recoveries.append(cid)

    # critic MINOR3：保留命中（H3∩H4）内部 matched_by 身份互换透明字段——
    # 集合包含与严口径总数都捕不到「一退一进」对冲，单列不改任何判决定义。
    retained = sorted(h3 & h4)
    mb3 = {cid: cases3[cid].get("matched_by") for cid in retained}
    mb4 = {cid: cases4[cid].get("matched_by") for cid in retained}
    retained_matched_by_swaps = [
        {"id": cid, "iter3": mb3[cid], "iter4": mb4[cid]}
        for cid in retained if mb3[cid] != mb4[cid]
    ]
    retained_goldid_to_goldtext = [s["id"] for s in retained_matched_by_swaps
                                   if s["iter3"] == "gold_id"
                                   and s["iter4"] == "gold_text"]

    primary_ok, primary_per_route, primary_obs = _metrics_match(rep4, PRED_PRIMARY)
    strict4 = _strict(rep4)
    # 严口径预测用本脚本重算的 strict 块比对（报告 metrics 不保证含严口径四路）。
    # _strict 块用原始路由名（kb/solutions/learnings），与主口径 metrics 的
    # 报告键（kb_route_observed 等）不同，这里不得复用 REPORT_KEY。
    strict_per_route = {
        route: {
            "match": strict4.get(route, {}).get("hits") == want["hits"]
            and strict4.get(route, {}).get("total") == want["total"],
            "observed": {
                "hits": strict4.get(route, {}).get("hits"),
                "total": strict4.get(route, {}).get("total"),
            },
        }
        for route, want in PRED_STRICT.items()
    }
    strict_ok = all(v["match"] for v in strict_per_route.values())

    point_prediction_match = (
        anchors_ok and prereg_ok and not flips and gains == [PRED_GAIN]
        and primary_ok and strict_ok
    )
    residual_recovery_strict = (
        anchors_ok and prereg_ok and bool(strict_recoveries))

    result = {
        "schema": "simpraxis.task6.embedswap_nonregression.v1",
        "iter4_iteration": rep4.get("iteration"),
        "iter4_rules_version": rep4.get("rules_version"),
        "anchors_ok": anchors_ok,
        "anchors": anchors,
        "prereg_ok": prereg_ok,
        "prereg": prereg,
        "h3_hits": sorted(h3),
        "h4_hits": sorted(h4),
        "nonregression_pass": anchors_ok and prereg_ok and not flips,
        "flips_h3_to_h4": flips,
        "flips_h1_to_h4": sorted(h1 - h4),
        "flips_h2_to_h4": sorted(h2 - h4),
        "gains_h4_over_h3": gains,
        "retained_matched_by_swaps": retained_matched_by_swaps,
        "retained_goldid_to_goldtext": retained_goldid_to_goldtext,
        "residual_cases_iter3": residual_detail,
        "residual_strict_recoveries": strict_recoveries,
        "residual_recovery_strict": residual_recovery_strict,
        "strict": strict4,
        "point_prediction": {
            "pred_gain": PRED_GAIN,
            "observed_gains": gains,
            "predicted_zero_flips": not flips,
            "predicted_primary_match": primary_ok,
            "primary_per_route": primary_per_route,
            "primary_observed": primary_obs,
            "predicted_strict_match": strict_ok,
            "strict_per_route": strict_per_route,
        },
        "point_prediction_match": point_prediction_match,
        "thresholds_primary": {
            "overall": rep4.get("metrics", {}).get("overall", {}).get("pass"),
            "solution_route": rep4.get("metrics", {}).get(
                "solution_route", {}).get("pass"),
            "learning_route": rep4.get("metrics", {}).get(
                "learning_route", {}).get("pass"),
            "all_pass": rep4.get("metrics", {}).get("all_pass"),
        },
        "verdict_hint": (
            "保留成功且点预测命中" if point_prediction_match and residual_recovery_strict
            else "保留成功但点预测失准（calibration failure）"
            if (residual_recovery_strict and not point_prediction_match)
            else "保留条件未全真（须回滚或 STOP，按预注册 §7）"
        ),
    }

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        # verifier LOW：与 eval_planner_recall.py:241 同范式，证据件 0600。
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(args.json_out, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.chmod(args.json_out, 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
