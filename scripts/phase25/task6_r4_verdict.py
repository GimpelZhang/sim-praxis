#!/usr/bin/env python3
"""Task 6 Stage B（r4）：保留判据机判（只读、0 付费、确定性）。

预注册 ``scripts/phase25/task6_r4_prereg.md`` §7.2/§8 的机判实现。输入四份
**只读** JSON：

1. ``--iter3``  r3 冻结基准报告（冻结判分器机产，sha f1c0e7f4…）
2. ``--iter5``  r4 绑定迭代报告（冻结判分器机产；**只读，不重跑**）
3. ``--predictions`` 设计期冻结点预测快照（sha e7e1bc38…）
4. ``--evidence`` 窗口工程证据（四表身份/ledger/门禁/显存等，由收尾步骤机产）

机判五条件（预注册 §8）的合取：

1. ``thresholds``  三门阈值全过（读 iter5.metrics.*.pass 与 all_pass）
2. ``h3_subset_h4``  r3 的每个命中案在 iter5 仍命中（翻失 = 违例，明细列出）
3. ``residual_recovery``  r3 的 9 个残留里回收 ≥6 且 gold_id 直中 ≥5
4. ``data_identity``  四表行数/dim/id 集 sha + Q1Q2Q3 有序 id 集恒等（读 evidence）
5. ``engineering``  0 付费 ledger 恒 56 + 两排除全套 rc0 + kbint 门绿 +
   gate_static 绿 + 容器 0 + 显存 ≤200 MiB（读 evidence）

本脚本**不判分、不连库、不载模、不写任何输入件**；输出一份 JSON 到 stdout
（或 ``--out``）。任何输入缺失/损坏即 exit 2（不作判据）。

用法：
  python scripts/phase25/task6_r4_verdict.py \
      --iter3 <iter3.json> --iter5 <iter5.json> \
      --predictions scripts/phase25/snapshots/task6_r4_point_predictions.json \
      --evidence <window_evidence.json> [--out PATH]
退出码：0=五条件全真（保留 r4）；3=任一为假（判负，预授权形态甲回滚）；
        2=输入不可用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ITER3_SHA256 = "f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307"
PREDICTIONS_SHA256 = (
    "e7e1bc3853ac199bdc25bb99feabda19b1f688b5f4f214f10483f6a9d0740a76")
EVAL_SCHEMA = "simpraxis.phase25.planner_gold_eval.v1"
PRED_SCHEMA = "simpraxis.phase25.task6_r4_point_predictions.v1"
EVIDENCE_SCHEMA = "simpraxis.phase25.task6_r4_window_evidence.v1"
SCHEMA = "simpraxis.phase25.task6_r4_verdict.v1"

# 预注册 §8 条件 3 的下限（不随结果可调）。
RECOVER_MIN = 6
RECOVER_GOLD_ID_MIN = 5
# 预注册 §8 条件 4/5 的冻结期望值。
EXPECTED_ROWS = {"kb_chunks": 1542, "solution_chunks": 94,
                 "phase1_learnings": 44, "phase2_learnings": 1}
EXPECTED_ID_SHA8 = {"kb_chunks": "35d4bf61", "solution_chunks": "cdab79c7",
                    "phase1_learnings": "022341f7",
                    "phase2_learnings": "82058d2f"}
EXPECTED_DIM = 768
EXPECTED_LEDGER_ROWS = 56
VRAM_CEILING_MIB = 200


class InputError(RuntimeError):
    """输入件缺失/损坏/不符冻结锚点（exit 2，不作判据）。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, *, schema: "str | None" = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"读不了 {path}：{exc}") from exc
    if schema and data.get("schema") != schema:
        raise InputError(f"{path} schema 不符：{data.get('schema')!r}")
    return data


def load_inputs(args) -> tuple[dict, dict, dict, dict]:
    iter3_p, iter5_p = Path(args.iter3), Path(args.iter5)
    pred_p, ev_p = Path(args.predictions), Path(args.evidence)
    if _sha256(iter3_p) != ITER3_SHA256:
        raise InputError(f"iter3 报告 sha 不符冻结锚点：{iter3_p}")
    if _sha256(pred_p) != PREDICTIONS_SHA256:
        raise InputError(f"点预测快照 sha 不符冻结锚点：{pred_p}")
    iter3 = _load(iter3_p, schema=EVAL_SCHEMA)
    iter5 = _load(iter5_p, schema=EVAL_SCHEMA)
    pred = _load(pred_p, schema=PRED_SCHEMA)
    ev = _load(ev_p, schema=EVIDENCE_SCHEMA)
    if iter5.get("iteration") != 5:
        raise InputError(f"iter5 报告 iteration={iter5.get('iteration')!r}，期望 5")
    if iter5.get("rules_version") != "r4":
        raise InputError(
            f"iter5 报告 rules_version={iter5.get('rules_version')!r}，期望 'r4'")
    return iter3, iter5, pred, ev


def check_thresholds(iter5: dict) -> dict:
    m = iter5.get("metrics") or {}
    keys = ("overall", "solution_route", "learning_route")
    detail = {}
    for k in keys:
        sub = m.get(k) or {}
        detail[k] = {"hits": sub.get("hits"), "total": sub.get("total"),
                     "gold_at_8": sub.get("gold_at_8"),
                     "threshold": sub.get("threshold"),
                     "pass": bool(sub.get("pass"))}
    ok = bool(m.get("all_pass")) and all(detail[k]["pass"] for k in keys)
    return {"ok": ok, "all_pass": bool(m.get("all_pass")), "detail": detail}


def check_h3_subset_h4(iter3: dict, iter5: dict) -> dict:
    h3 = {c["id"] for c in iter3["cases"] if c.get("hit")}
    h4 = {c["id"] for c in iter5["cases"] if c.get("hit")}
    flips = sorted(h3 - h4)
    return {"ok": not flips, "n_h3": len(h3), "n_h4": len(h4),
            "flip_loss": flips}


def check_residual_recovery(iter3: dict, iter5: dict) -> dict:
    missing3 = [c["id"] for c in iter3["cases"] if not c.get("hit")]
    by_id5 = {c["id"]: c for c in iter5["cases"]}
    recovered, still = [], []
    for cid in missing3:
        case = by_id5.get(cid)
        if case is None:
            raise InputError(f"iter5 报告缺 case：{cid}")
        if case.get("hit"):
            recovered.append({"id": cid, "rank": case.get("rank"),
                              "matched_by": case.get("matched_by")})
        else:
            still.append(cid)
    gold_id_direct = [r for r in recovered if r["matched_by"] == "gold_id"]
    ok = (len(recovered) >= RECOVER_MIN
          and len(gold_id_direct) >= RECOVER_GOLD_ID_MIN)
    return {"ok": ok, "n_residual": len(missing3),
            "n_recovered": len(recovered),
            "n_gold_id_direct": len(gold_id_direct),
            "min_required": {"recovered": RECOVER_MIN,
                             "gold_id_direct": RECOVER_GOLD_ID_MIN},
            "recovered": recovered, "still_missed": sorted(still)}


def check_point_prediction(iter5: dict, pred: dict) -> dict:
    pred_ids = {c["id"]: list(c["fused_ids"]) for c in pred["cases"]}
    mismatches = []
    for case in iter5["cases"]:
        expect = pred_ids.get(case["id"])
        if expect is None:
            mismatches.append({"id": case["id"], "reason": "快照无此 case"})
            continue
        got = [str(x) for x in case.get("top8_ids") or []]
        if got != expect:
            mismatches.append({"id": case["id"], "expected": expect,
                               "got": got})
    return {"ok": not mismatches, "n_cases": len(iter5["cases"]),
            "mismatches": mismatches}


def check_data_identity(ev: dict) -> dict:
    four = ev.get("four_tables") or {}
    rows = four.get("rows") or {}
    dims = four.get("dims") or {}
    shas = four.get("id_sha8") or {}
    problems = []
    for name, want in EXPECTED_ROWS.items():
        if rows.get(name) != want:
            problems.append(f"{name} 行数 {rows.get(name)!r} != {want}")
    for name in EXPECTED_ROWS:
        if dims.get(name) != EXPECTED_DIM:
            problems.append(f"{name} dim {dims.get(name)!r} != {EXPECTED_DIM}")
        got = str(shas.get(name) or "")[:8]
        if got != EXPECTED_ID_SHA8[name]:
            problems.append(
                f"{name} id 集 sha8 {got!r} != {EXPECTED_ID_SHA8[name]}")
    q = ev.get("q1q2q3") or {}
    if not q.get("ok"):
        problems.append(f"Q1/Q2/Q3 有序 13 id 集不恒等：{q}")
    writes = ev.get("live_writes_except_gate_restamp")
    # 缺键/None 一律判负（fail-closed）：这是保留条件，观测不到不等于不存在
    # （GATE1 规格 lane MAJOR-2——原 `not in (0, None)` 把缺数据当通过）。
    if writes != 0:
        problems.append(f"活库写入计数非 0 或未观测：{writes!r}")
    # 授权例外（§7.3）的范围也要单独钉死：只有 solution_chunks.ingested_at
    # 重戳可豁免，别的表/别的戳记列一旦变化，即便被折叠进 unexpected 也必须
    # 显式核对计数为 0（GATE1-LANE-A C3(b)）。缺 write_surface 同样是观测缺口。
    surface = ev.get("write_surface")
    if not isinstance(surface, dict):
        problems.append("缺 write_surface（无法核对 §7.3 授权例外范围）")
    elif surface.get("gate_restamp_other_rows") != 0:
        problems.append(
            "非授权戳记变化行数非 0 或未观测："
            f"{surface.get('gate_restamp_other_rows')!r}")
    # 向量轴（GATE1-LANE-A C-1）：逐行哈希不覆盖 vector 列，同维重嵌入在
    # 写入面比对里看起来干净。集合哈希必须**被观测**且不变，缺观测即判负。
    vec = ev.get("vector_identity")
    if not isinstance(vec, dict) or vec.get("observed") is not True:
        problems.append("缺 vector_identity 或未观测（无法断言嵌入身份不变）")
    elif vec.get("unchanged") is not True:
        problems.append(f"四表向量集合哈希变化：{vec.get('mismatches')!r}")
    return {"ok": not problems, "problems": problems}


def check_engineering(ev: dict) -> dict:
    problems = []
    if ev.get("ledger_rows") != EXPECTED_LEDGER_ROWS:
        problems.append(
            f"ledger 行数 {ev.get('ledger_rows')!r} != {EXPECTED_LEDGER_ROWS}"
            "（Stage B 须 0 实呼）")
    hermetic = ev.get("hermetic") or {}
    # 两排除全套：rc==0 且收口脚本声明 ok；skipped 只允许既有同构 skip（记录
    # 在 evidence 里，不在此处放水——ok 由执行方按实跑结论写入并被本脚本读取）。
    if hermetic.get("rc") != 0 or hermetic.get("ok") is not True:
        problems.append(f"两排除 hermetic 全套不合格：{hermetic}")
    # kbint 门必须**绑到实跑 rc**（修订 A10-M1）：只认 rc==0，裸 ok 布尔不算。
    # 证据机产器已改为解析 pytest 日志摘要得到 rc，此处再要求 rc 存在且为 0，
    # 缺 rc（例如旧证据只有 {"ok": true}）一律判负。
    kbint = ev.get("kbint") or {}
    if kbint.get("rc") != 0 or kbint.get("ok") is not True:
        problems.append(f"kbint 门不合格（须 rc==0）：{kbint}")
    gate = ev.get("gate_static") or {}
    if gate.get("ok") is not True:
        problems.append(f"gate_static 不合格：{gate}")
    if ev.get("containers_present") != 0:
        problems.append(
            f"容器残留或未观测：{ev.get('containers_present')!r}"
            "（口径 = docker ps -a，含已退出容器，硬规则 5）")
    vram = ev.get("vram_mib")
    # 负值不是合法读数（异常驱动可能给 -1，天然落在上限内）——按未测处理
    # （GATE1-LANE-A A10-L2）。
    if not isinstance(vram, int) or vram < 0 or vram > VRAM_CEILING_MIB:
        problems.append(
            f"显存 {vram!r} MiB 超 {VRAM_CEILING_MIB}、为负或未测")
    if ev.get("provenance_restore") is not True:
        problems.append("PROC-7 门后溯源终复未完成")
    return {"ok": not problems, "problems": problems}


def verdict(args) -> dict:
    iter3, iter5, pred, ev = load_inputs(args)
    c1 = check_thresholds(iter5)
    c2 = check_h3_subset_h4(iter3, iter5)
    c3 = check_residual_recovery(iter3, iter5)
    c4 = check_data_identity(ev)
    c5 = check_engineering(ev)
    match = check_point_prediction(iter5, pred)
    conditions = {"1_thresholds": c1, "2_h3_subset_h4": c2,
                  "3_residual_recovery": c3, "4_data_identity": c4,
                  "5_engineering": c5}
    # 点预测一致性不是保留条件，而是**实现正确性前置**：不一致说明生产实现
    # 未复现冻结规格（预注册 §6.1），此时任何指标解读都无意义。故或列入
    # retain（更严），不列入即等于放宽。
    ok = all(c["ok"] for c in conditions.values()) and match["ok"]
    return {
        "schema": SCHEMA,
        "iter3_sha256": ITER3_SHA256,
        "iter5_sha256": _sha256(Path(args.iter5)),
        "predictions_sha256": PREDICTIONS_SHA256,
        "conditions": conditions,
        "point_prediction_match": match,
        "verdict": "RETAIN_R4" if ok else "NEGATIVE_ROLLBACK_R3",
        "retain": ok,
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iter3", required=True)
    ap.add_argument("--iter5", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    try:
        out = verdict(args)
    except InputError as exc:
        print(f"[T6R4-VERDICT][STOP] {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    text = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for name, cond in out["conditions"].items():
        print(f"{name:24s} {'PASS' if cond['ok'] else 'FAIL'}", file=sys.stderr)
    print(f"point_prediction_match  "
          f"{'PASS' if out['point_prediction_match']['ok'] else 'FAIL'}",
          file=sys.stderr)
    print(f"verdict = {out['verdict']}", file=sys.stderr)
    return 0 if out["retain"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
