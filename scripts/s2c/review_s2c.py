#!/usr/bin/env python3
"""S2c P 池双审（2 通道 × 6 实例 = 12 呼；ark deepseek-v4.1-flash）。

沿用 S2b 修复形态：max_tokens 16384、verdict 从 parsed、增量持久化。
用法：python scripts/s2c/review_s2c.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2c_evidence_root
from orchestrator.gateway.quota_ledger import (QuotaError, RSI_S2C_NAMESPACE,
                                               billing_context, write_approval)
from orchestrator.gateway.router import ModelGateway
from orchestrator.gateway.quota_ledger import QuotaLedger
from orchestrator.rsi_bridge.review_s2 import build_pack_v2, merge_channels, run_channel

INSTANCES = ("strong_C", "strong_D", "zero_D2", "zero_D3", "near_delta_C", "nc_trap_C")


def _load(study: str) -> dict:
    ev = rsi_s2c_evidence_root()
    rpt = json.loads((ev / "pool_p" / "trial" / study / "study_report.json").read_text())
    run_dir = ev / "pool_p" / "trial" / study / "runs"
    k8 = None
    for d in run_dir.glob("**/K8.json"):
        k8 = json.loads(d.read_text()); break
    k7 = None
    for d in run_dir.glob("**/K7.json"):
        k7 = json.loads(d.read_text()); break
    claim = k8 or {}
    report = claim.get("report", {}) if isinstance(claim, dict) else {}
    results = (k7 or {}).get("contrasts", [])
    nc = (k7 or {}).get("negative_control")
    nc_str = json.dumps(nc, ensure_ascii=False) if nc else "NC 见包"
    return {"claim": claim, "report": report, "results": results,
            "nc": nc_str, "rpt": rpt}


def main() -> int:
    ev = rsi_s2c_evidence_root()
    batch = "s2c-p-dual-review"
    try:
        write_approval(batch_id=batch, task=2, stage="coding_debug", approved=12,
                       beyond_normal=False, note="S2c P 池双审 2×6（自批）",
                       namespace=RSI_S2C_NAMESPACE)
        print(f"批准件：{batch} task2 approved 12")
    except QuotaError as e:
        print(f"批准件复用：{e}")
    gw = ModelGateway(max_attempts=1, ledger=QuotaLedger.rsi_s2c())
    call = lambda p: gw.call("coding_debug", [{"role": "user", "content": p}],
                             max_tokens=16384, temperature=0.0,
                             thinking={"type": "disabled"})

    packs = {}
    for s in INSTANCES:
        try:
            data = _load(s)
        except FileNotFoundError:
            print(f"{s}: 缺研究报告，跳过"); continue
        claim = data["claim"]
        pack = build_pack_v2(instance_id=s, arm="v2", channel="channel_a",
                             question=claim.get("statement", ""),
                             methods="MuJoCo 物理仿真 2×2 DiD+NC",
                             results=data["results"], claim=claim,
                             report=data["report"], negative_control=data["nc"])
        packs[s] = pack
        print(f"{s} 包 {pack.get('pack_id')} sha {pack.get('pack_sha256','')[:12]}")
    if not packs:
        print("无可审实例"); return 1

    partial = ev / "review" / "pool_p_dual_review.partial.json"
    partial.parent.mkdir(parents=True, exist_ok=True)
    reviews = {}
    with billing_context(task=2, batch_id=batch):
        for s, pack in packs.items():
            chans = {}
            for ch in ("channel_a", "channel_b"):
                print(f"  {s} × {ch} …")
                r = run_channel(call, pack=pack, channel=ch)
                chans[ch] = r
                print(f"    → {(r.get('parsed') or {}).get('verdict')}")
            merged = merge_channels(instance_id=s, arm="v2",
                                    channel_a=chans["channel_a"],
                                    channel_b=chans["channel_b"])
            reviews[s] = {"channels": chans, "merged": merged}
            print(f"  {s} 合并：{merged.get('verdict')}")
            partial.write_text(json.dumps(
                {k: {"merged": v["merged"]} for k, v in reviews.items()},
                ensure_ascii=False, indent=2, default=str))

    out = ev / "review" / "pool_p_dual_review.json"
    merged_all = {k: v["merged"] for k, v in reviews.items()}
    out.write_text(json.dumps({
        "schema": "simpraxis.s2c.pool_p_dual_review.v1",
        "reviews": reviews, "merged": merged_all,
        "all_pass": all(m.get("verdict") == "PASS" for m in merged_all.values()),
    }, ensure_ascii=False, indent=2, default=str))
    print(f"\n双审结果：{out}")
    print(f"all_pass: {all(m.get('verdict')=='PASS' for m in merged_all.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
