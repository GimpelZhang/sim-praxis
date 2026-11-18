#!/usr/bin/env python3
"""RSI-S1 双审驱动（S1-7；入 2909 网关账）。

两步，严格顺序：

1. ``--calibrate``：6 例校准包分 2 呼（3 例/呼，混合呈现以检验判别力）；逐例
   判定必须与已知答案一致。任一无效应例被接受（误接收）= 仪器未就绪 → 停付、
   不进入正式双审（R1.3 轮次纪律）。
2. ``--review``：2 通道 × 2 研究正式双审（4 呼）。审查包自足（不含机器判决/
   另一审意见）；六维评分 + 合并真值表（两票全过=PASS；任一双败=FAIL；其余
   INCONCLUSIVE，无第三票）。报告冻结（report_sha256），之后不得回改。

批准件自批：``quota/approvals/s1-review-calib-1.json``（2 呼）与
``s1-dual-review-1.json``（4 呼）；任务帽 task=2（8 呼）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import rsi_s1_closeout_dir  # noqa: E402
from orchestrator.gateway.quota_ledger import (QuotaLedger, RSI_S1_NAMESPACE,  # noqa: E402
                                               billing_context, write_approval)
from orchestrator.rsi_bridge import review_s1 as RV  # noqa: E402

CALIBRATION_BATCH = "s1-review-calib-1"
REVIEW_BATCH = "s1-dual-review-1"
TASK_ID = 2

CALIB_PROMPT = """你是审查仪器的校准受试者。下面给出 {n} 个微研究摘要（混合有效与无效）。
对每一个，独立判定应 accept 还是 reject，并只输出 JSON 数组：
[{{"id": "...", "verdict": "accept|reject", "reason": "一句话"}}]

评判口径：主张必须由给定设计与统计支撑；伪重复（同 run 时间步当独立样本）、
过度概括（外推到未测范围）、空模板（无可核内容）、与数据矛盾的结论一律 reject。

摘要：
{cases}
"""


def _gateway():
    from orchestrator.gateway.router import ModelGateway

    return ModelGateway(max_attempts=1, ledger=QuotaLedger.rsi_s1())


def _call(call_fn, prompt: str) -> "tuple[str, tuple[int | None, int | None]]":
    return call_fn(prompt)


def _approve(batch_id: str, approved: int, note: str) -> Path:
    return write_approval(batch_id=batch_id, task=TASK_ID, stage="coding_debug",
                          approved=approved, beyond_normal=False, note=note,
                          namespace=RSI_S1_NAMESPACE)


def _parse_array(text: str, *, expected_ids: list[str]) -> dict[str, str]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise RV.ReviewError("CALIB_PARSE", "输出不含 JSON 数组")
    try:
        rows = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise RV.ReviewError("CALIB_PARSE", str(exc)) from None
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        rid = str(row.get("id") or "")
        verdict = str(row.get("verdict") or "").strip().lower()
        if rid in expected_ids and verdict in ("accept", "reject"):
            out[rid] = verdict
    missing = [i for i in expected_ids if i not in out]
    if missing:
        raise RV.ReviewError("CALIB_INCOMPLETE", f"缺判定 {missing}")
    return out


def run_calibration(gateway) -> dict[str, Any]:
    cases = list(RV.calibration_cases())
    batches = [cases[i:i + 3] for i in range(0, len(cases), 3)]
    _approve(CALIBRATION_BATCH, len(batches),
             "S1-7 审查仪器校准（6 例分 2 呼；自批）")
    results: dict[str, str] = {}
    raw_calls: list[dict[str, Any]] = []
    call = lambda prompt: gateway.call("coding_debug",  # noqa: E731
                                       [{"role": "user", "content": prompt}],
                                       max_tokens=1024, temperature=0.0)
    with billing_context(task=TASK_ID, batch_id=CALIBRATION_BATCH):
        for batch in batches:
            ids = [case["id"] for case in batch]
            prompt = CALIB_PROMPT.format(
                n=len(batch),
                cases=json.dumps([{"id": c["id"], "summary": c["summary"]}
                                  for c in batch], ensure_ascii=False, indent=2))
            text, tokens = call(prompt)
            raw_calls.append({"ids": ids, "raw_text": text, "tokens": list(tokens),
                              "at_utc": utc_now_iso_z()})
            results.update(_parse_array(text, expected_ids=ids))
    expected = RV.calibration_expectations()
    mismatches = {cid: {"expected": expected[cid], "got": results.get(cid)}
                  for cid in expected if results.get(cid) != expected[cid]}
    misaccept = [cid for cid, row in mismatches.items()
                 if row["got"] == "accept" and row["expected"] == "reject"]
    ready = not mismatches
    report = {
        "schema": f"{RV.SCHEMA}.calibration",
        "ready": ready,
        "verdict": "REVIEW_INSTRUMENT_READY" if ready else "REVIEW_INSTRUMENT_NOT_READY",
        "results": results, "expected": expected,
        "mismatches": mismatches, "misaccept": misaccept,
        "calls": raw_calls,
        "calibrated_at_utc": utc_now_iso_z(),
    }
    return RV.freeze_report(report)


# ---------------------------------------------------------------------------
# 正式双审
# ---------------------------------------------------------------------------

def _load_study(evidence_root: Path, study: str) -> dict[str, Any]:
    runs = sorted((evidence_root / "runs").glob(f"{study}_run*"))
    if not runs:
        raise RV.ReviewError("STUDY_EVIDENCE_MISSING", f"{study} 无 run 目录")
    run_dir = runs[-1]
    k7 = json.loads((run_dir / "K7.json").read_text(encoding="utf-8"))
    k8 = json.loads((run_dir / "K8.json").read_text(encoding="utf-8"))
    return {"run_dir": str(run_dir), "confirm_result": k7, "claim": k8}


def build_pack(study: str, evidence_root: Path, prereg_study: Mapping[str, Any]
               ) -> dict[str, Any]:
    loaded = _load_study(evidence_root, study)
    pack = RV.build_review_pack(
        study=study,
        question=str(prereg_study["task"].get("public_question") or ""),
        methods=("2×2 全因子 + %d 配对 block（宿主私有 nuisance 分配）；"
                 "逐 block 线性对比；percentile bootstrap（10000 重抽样，固定 seed）；"
                 "等效类主张用 90% 区间；主张由 host 逐点复算值经 G-CLAIM 校验。"
                 % int(prereg_study["n_block"])),
        results=[{k: row.get(k) for k in ("id", "estimate", "ci_low", "ci_high",
                                          "n_blocks")}
                 for row in loaded["confirm_result"].get("contrasts", [])],
        claim={"verdict": loaded["claim"].get("verdict"),
               "statement": loaded["claim"].get("statement"),
               "contrasts": loaded["claim"].get("contrasts")},
        negative_control=("sham 有效阴性：torsional 0.01→0.02，90% 区间须落 ±0.001 m"
                          if study == "A_N" else "本机制无注册阴性对照"),
    )
    return pack


def run_review(gateway, *, evidence_root: Path,
               prereg_dir: Path) -> dict[str, Any]:
    studies = ("A_N", "B_N")
    approvals = len(studies) * 2
    _approve(REVIEW_BATCH, approvals, "S1-7 正式双审（2 通道 × 2 研究；自批）")
    call = lambda prompt: gateway.call("coding_debug",  # noqa: E731
                                       [{"role": "user", "content": prompt}],
                                       max_tokens=1024, temperature=0.0)
    packs = {}
    channels: dict[str, dict[str, Any]] = {}
    truth: dict[str, Any] = {}
    with billing_context(task=TASK_ID, batch_id=REVIEW_BATCH):
        for study in studies:
            prereg_study = json.loads(
                (prereg_dir / f"study_{study}.json").read_text(encoding="utf-8"))
            pack = build_pack(study, evidence_root, prereg_study)
            packs[study] = pack
            runs = {}
            for channel in ("channel_a", "channel_b"):
                run = RV.run_review(call, pack=pack, channel=channel)
                run.reviewed_at_utc = utc_now_iso_z()
                runs[channel] = run.to_dict()
            channels[study] = runs
            truth[study] = RV.merge_truth_table(runs["channel_a"]["parsed"],
                                                runs["channel_b"]["parsed"])
    report = RV.freeze_report({
        "schema": f"{RV.SCHEMA}.dual_review",
        "packs": packs, "channels": channels, "truth_table": truth,
        "reviewed_at_utc": utc_now_iso_z(),
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 双审驱动")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if not (args.calibrate or args.review):
        parser.error("至少 --calibrate 或 --review")
    out = Path(args.out) if args.out else rsi_s1_closeout_dir() / "review"
    ensure_private_dir(out, mode=0o700)
    gateway = _gateway()
    if args.calibrate:
        report = run_calibration(gateway)
        target = out / "review_calibration.json"
        atomic_write_text(target, json.dumps(report, ensure_ascii=False,
                                             indent=2, sort_keys=True) + "\n",
                          mode=0o600)
        print(json.dumps({"ready": report["ready"], "verdict": report["verdict"],
                          "misaccept": report["misaccept"],
                          "report_sha256": report["report_sha256"]},
                         ensure_ascii=False))
        if not report["ready"]:
            return 3
    if args.review:
        evidence = Path(args.evidence) if args.evidence else Path(
            sorted((rsi_s1_closeout_dir().parent / "evidence").glob("*/"))[-1])
        from orchestrator.common.runtime_paths import rsi_s1_prereg_dir

        report = run_review(gateway, evidence_root=evidence,
                            prereg_dir=rsi_s1_prereg_dir())
        target = out / "dual_review.json"
        atomic_write_text(target, json.dumps(report, ensure_ascii=False,
                                             indent=2, sort_keys=True) + "\n",
                          mode=0o600)
        print(json.dumps({"truth_table": report["truth_table"],
                          "report_sha256": report["report_sha256"]},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
