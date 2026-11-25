#!/usr/bin/env python3
"""RSI-S2b 结案机判（S2b-6）：五条件 R1 判词 + 判件不动点。

用法：/mnt/venvs/simpraxis/bin/python scripts/verification/check_s2b_closure.py [--report PATH]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2b_evidence_root, rsi_s2b_rules_prereg_dir


def _sha(doc) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def check_five_conditions() -> dict:
    """R1 五条件优先级表逐项取证。"""
    evidence_root = rsi_s2b_evidence_root()
    results = {}

    # 1. 两机制闭环（C_N + D_N K0→K8 done）
    studies = {}
    for study in ("C_N", "D_N"):
        report_path = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "study_report.json"
        if report_path.exists():
            d = json.loads(report_path.read_text())
            stage = (d.get("outcome") or {}).get("stages", {}).get(study, {})
            studies[study] = {
                "status": stage.get("status"),
                "code": stage.get("code"),
                "verdict": (stage.get("claim") or {}).get("verdict"),
            }
        else:
            studies[study] = {"status": "MISSING"}
    results["two_mechanisms"] = {
        "studies": studies,
        "pass": all(s.get("status") == "done" for s in studies.values()),
    }

    # 2. 有效阴性（NC 三连过）
    nc_results = {}
    for study in ("C_N", "D_N"):
        k7_path = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "runs" / f"{study}_run001" / "K7.json"
        if k7_path.exists():
            k7 = json.loads(k7_path.read_text())
            nc = k7.get("negative_control")
            nc_results[study] = {
                "registered": nc is not None,
                "non_degenerate": (nc or {}).get("non_degenerate"),
                "sub_threshold": (nc or {}).get("sub_threshold"),
                "sensitivity_calibrated": (nc or {}).get("sensitivity_calibrated"),
            }
        else:
            nc_results[study] = {"registered": False}
    results["valid_negative"] = {
        "nc": nc_results,
        "pass": all(nc.get("registered") and nc.get("non_degenerate")
                    and nc.get("sub_threshold") and nc.get("sensitivity_calibrated")
                    for nc in nc_results.values()),
    }

    # 3. 独立复跑（n_diffs=0）
    rerun_results = {}
    for study in ("C_N", "D_N"):
        rerun_path = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "rerun.json"
        if rerun_path.exists():
            d = json.loads(rerun_path.read_text())
            rerun_results[study] = {"n_diffs": d.get("n_diffs"), "pass": d.get("n_diffs") == 0}
        else:
            rerun_results[study] = {"n_diffs": None, "pass": False}
    results["independent_rerun"] = {
        "rerun": rerun_results,
        "pass": all(r.get("pass") for r in rerun_results.values()),
    }

    # 4. 双审 PASS（2 通道 × 2 研究）
    review_path = evidence_root / "review" / "dual_review.json"
    if review_path.exists():
        d = json.loads(review_path.read_text())
        results["dual_review"] = {"review": d, "pass": d.get("all_pass", False)}
    else:
        results["dual_review"] = {"review": None, "pass": False}

    # 5. 无代写（fresh 会话 transcript 产者链完整）
    no_ghost = True
    for study in ("C_N", "D_N"):
        run_dir = evidence_root / "campaign" / "trial" / f"s2b-{study.lower()}" / "runs" / f"{study}_run001"
        for tp in ("propose_plan", "write_analysis", "submit_claim"):
            meta_path = run_dir / f"touchpoint_{tp}.meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                if not meta.get("claude_session_id"):
                    no_ghost = False
    results["no_ghost_writing"] = {"pass": no_ghost}

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S2b 结案机判")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    five = check_five_conditions()
    all_pass = all(v.get("pass") for v in five.values())

    # R1 判词
    if all_pass:
        r1 = "MINIMAL_RESEARCH_LOOP_ESTABLISHED"
    else:
        failed = [k for k, v in five.items() if not v.get("pass")]
        r1 = f"NOT_ESTABLISHED（{', '.join(failed)} 未过）"

    judge = {
        "schema": "simpraxis.s2b.closure.v1",
        "five_conditions": five,
        "all_pass": all_pass,
        "r1_verdict": r1,
    }
    judge_sha = _sha(judge)
    judge["judge_sha256"] = judge_sha

    print(f"R1 判词：{r1}")
    for k, v in five.items():
        print(f"  {k}: {'PASS' if v.get('pass') else 'FAIL'}")
    print(f"judge_sha256: {judge_sha}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(judge, indent=2, sort_keys=True, default=str))
        # 不动点：再判一次
        judge2 = json.loads(args.report.read_text())
        judge2.pop("judge_sha256", None)
        sha2 = _sha(judge2)
        print(f"不动点校验：{sha2 == judge_sha}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
