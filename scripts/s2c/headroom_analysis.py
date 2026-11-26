#!/usr/bin/env python3
"""S2c V_HEADROOM 分析（计划 §3 S2c-1）：P 池逐实例定位 + 全局分布。

读 P 池研究报告 + 双审结果，按预注册规则定位（全过=天花板、全败=地板、其余=有余量），
出每实例通过/verdict/成本与全局通过率、六维分分布。

用法：python scripts/s2c/headroom_analysis.py [--report PATH]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2c_evidence_root


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def analyze() -> dict:
    evidence = rsi_s2c_evidence_root()
    pool_root = evidence / "pool_p" / "trial"
    review_path = evidence / "review" / "pool_p_dual_review.json"
    review = _load(review_path) or {}

    per_instance = {}
    for label in ("strong_C", "strong_D", "zero_D2", "zero_D3", "near_delta_C", "nc_trap_C"):
        rpt = _load(pool_root / label / "study_report.json")
        if not rpt:
            per_instance[label] = {"status": "MISSING"}
            continue
        status = rpt.get("status")
        verdict = rpt.get("verdict")
        # 双审
        rev = (review.get("merged") or {}).get(label, {})
        review_verdict = rev.get("verdict")
        passed = (status == "done" and review_verdict == "PASS")
        per_instance[label] = {
            "label": rpt.get("label"), "status": status, "claim_verdict": verdict,
            "review_verdict": review_verdict, "passed": passed,
        }

    # 全局分布
    n = len(per_instance)
    n_pass = sum(1 for v in per_instance.values() if v.get("passed"))
    rate = n_pass / n if n else 0.0
    # 定位规则
    if n_pass == n:
        position = "天花板（全过）"
    elif n_pass == 0:
        position = "地板（全败）"
    else:
        position = "有余量"
    # 主指标候选（R2 决策表分支）
    if 0 < rate < 1:
        r2 = "P 通过率∈(0,1) → R2 第二轮主指标取通过率维度，用 P 型实例"
    elif n_pass == n:
        r2 = "P 全过 → 预置题无余量，R2 改用 Q 型实例（设计一次过率/判别力/成本）"
    else:
        r2 = "P 大面积失败 → R1「成立于 2/N 样本」，先修再 R2"

    return {
        "schema": "simpraxis.s2c.v_headroom.v1",
        "per_instance": per_instance,
        "n_instances": n, "n_pass": n_pass, "pass_rate": rate,
        "position": position, "r2_next_metric": r2,
        "v_headroom": ("HAS_HEADROOM" if 0 < rate < n or n_pass == n
                       else ("FLOOR" if n_pass == 0 else "PARTIAL")),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()
    rep = analyze()
    print(f"V_HEADROOM：{rep['position']}（{rep['n_pass']}/{rep['n_instances']} 过，"
          f"通过率 {rep['pass_rate']:.2f}）")
    for k, v in rep["per_instance"].items():
        print(f"  {k} [{v.get('label')}] {v.get('status')}/{v.get('claim_verdict')} "
              f"双审={v.get('review_verdict')} passed={v.get('passed')}")
    print(f"R2 下一步：{rep['r2_next_metric']}")
    out = args.report or (rsi_s2c_evidence_root() / "closeout" / "v_headroom.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
