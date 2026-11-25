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
            instance_id=study, arm="v2", channel="A",
            question=data["claim"].get("statement", ""),
            methods="MuJoCo 物理仿真，2×2 DiD + NC",
            results=results_rows,
            claim=claim, report=report,
            negative_control=nc_str)
        packs.append((study, pack))
        print(f"\n{study} 审查包：{pack.get('pack_id')}")
        print(f"  pack_sha256: {pack.get('pack_sha256', '')[:16]}…")

    # 双审需真实 LLM 调用（gateway POST）
    print(f"\n共 {len(packs)} 个审查包 × 2 通道 = {len(packs)*2} 呼")
    print("双审执行需 gateway 调用——请在预算内启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
