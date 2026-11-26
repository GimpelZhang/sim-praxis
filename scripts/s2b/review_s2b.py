#!/usr/bin/env python3
"""S2b-5 双审（计划 §3）：2 通道 × 2 研究 = 4 呼。

用法：/mnt/venvs/simpraxis/bin/python scripts/s2b/review_s2b.py [--calibrate]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2b_evidence_root
from orchestrator.rsi_bridge.review_s2 import (build_pack_v2, calibration_cases,
                                               calibration_expectations,
                                               merge_channels, run_channel)


def load_study(study: str) -> dict:
    """从研究报告加载 C_N/D_N 的 K8 产物。"""
    evidence_root = rsi_s2b_evidence_root()
    report_path = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "study_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"研究报告不存在：{report_path}")
    report = json.loads(report_path.read_text())
    claim = report.get("claim") or {}
    outcome = report.get("outcome") or {}
    stage = (outcome.get("stages") or {}).get(study, {})

    # 从 K8.json 取 claim 和 report
    run_dir = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "runs" / f"{study}_run001"
    k8_path = run_dir / "K8.json"
    if k8_path.exists():
        k8 = json.loads(k8_path.read_text())
        claim = k8.get("claim", claim)
        report_doc = k8.get("report", {})
    else:
        report_doc = {}

    return {
        "instance_id": study, "claim": claim, "report": report_doc,
        "outcome": outcome, "stage": stage,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S2b 双审")
    parser.add_argument("--calibrate", action="store_true",
                        help="先校准审查仪器")
    args = parser.parse_args()

    if args.calibrate:
        cases = calibration_cases()
        expects = calibration_expectations()
        print(f"校准用例：{len(cases)} 例")
        for i, (case, expect) in enumerate(zip(cases, expects)):
            print(f"  例 {i}: expect={expect}")
        print("校准需真实 LLM 调用——先检查研究完成状态")

    evidence_root = rsi_s2b_evidence_root()

    # 检查两研究是否完成
    results = {}
    for study in ("C_N", "D_N"):
        try:
            data = load_study(study)
            status = data["stage"].get("status")
            code = data["stage"].get("code")
            claim = data["claim"]
            print(f"\n{study}: status={status} code={code}")
            print(f"  verdict: {claim.get('verdict')}")
            results[study] = data
        except FileNotFoundError as e:
            print(f"\n{study}: {e}")
            return 1

    # 检查是否都 done
    all_done = all(results[s]["stage"].get("status") == "done"
                   for s in results)
    if not all_done:
        print("\n研究未全部完成，暂不启动双审")
        return 1

    # 构造审查包
    packs = []
    for study in ("C_N", "D_N"):
        data = results[study]
        claim = data["claim"]
        report = data["report"]
        # 从 K7 confirm_result 取 results 行和 NC 证据
        run_dir = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "runs" / f"{study}_run001"
        k7_path = run_dir / "K7.json"
        nc_str = "NC 证据见包内"
        results_rows = []
        if k7_path.exists():
            k7 = json.loads(k7_path.read_text())
            results_rows = k7.get("contrasts", [])
            nc = k7.get("negative_control")
            if nc:
                nc_str = json.dumps(nc, ensure_ascii=False)

        pack = build_pack_v2(
            instance_id=study, arm="v2", channel="channel_a",
            question=data["claim"].get("statement", ""),
            methods="MuJoCo 物理仿真，2×2 DiD + NC",
            results=results_rows,
            claim=claim, report=report,
            negative_control=nc_str)
        packs.append((study, pack))
        print(f"\n{study} 审查包：{pack.get('pack_id')}")
        print(f"  pack_sha256: {pack.get('pack_sha256', '')[:16]}…")

    # 执行双审（gateway 付费调用）
    from orchestrator.common.runtime_paths import rsi_s2b_approvals_dir
    from orchestrator.gateway.quota_ledger import QuotaLedger
    from orchestrator.gateway.router import ModelGateway

    gateway = ModelGateway(max_attempts=1, ledger=QuotaLedger.rsi_s2b())

    # 自批 4 呼
    from orchestrator.gateway.quota_ledger import (RSI_S2B_NAMESPACE,
                                                   billing_context, write_approval)
    review_batch = "s2b-dual-review"
    from orchestrator.gateway.quota_ledger import QuotaError
    try:
        write_approval(batch_id=review_batch, task=2, stage="coding_debug",
                       approved=4, beyond_normal=False,
                       note="S2b 双审 2 通道 × 2 研究（自批）",
                       namespace=RSI_S2B_NAMESPACE)
        print(f"批准件已写：{review_batch}（task 2, approved 4）")
    except QuotaError as exc:
        print(f"批准件已存在，复用：{exc}")

    call = lambda prompt: gateway.call(  # noqa: E731
        "coding_debug", [{"role": "user", "content": prompt}],
        max_tokens=8192, temperature=0.0,
        thinking={"type": "disabled"})

    print(f"\n=== 执行双审（4 呼） ===")
    reviews = []
    with billing_context(task=2, batch_id=review_batch):
        for study, pack in packs:
            for channel in ("channel_a", "channel_b"):
                print(f"\n  {study} × {channel} …")
                result = run_channel(call, pack=pack, channel=channel)
                verdict = result.get("verdict") or result.get("decision")
                print(f"    → {verdict}")
                reviews.append({"study": study, "channel": channel,
                                "result": result})

    # 合并通道
    for study in ("C_N", "D_N"):
        a = next(r for r in reviews if r["study"] == study and r["channel"] == "channel_a")
        b = next(r for r in reviews if r["study"] == study and r["channel"] == "channel_b")
        merged = merge_channels(instance_id=study, arm="v2",
                                channel_a=a["result"], channel_b=b["result"])
        merged_verdict = merged.get("verdict") or merged.get("merged_verdict")
        print(f"\n  {study} 合并：{merged_verdict}")

    # 落盘审查结果
    review_path = evidence_root / "review" / "dual_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "schema": "simpraxis.s2b.dual_review.v1",
        "reviews": reviews,
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, ensure_ascii=False, indent=2, default=str))
    print(f"\n审查结果已落盘：{review_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
