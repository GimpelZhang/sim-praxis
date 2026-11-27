"""S2c S2c-3：Q 池双审（2 通道 × N；ark deepseek-v4.1-flash）。

复用 S2b 修复形态（max_tokens 16384 / verdict 从 parsed / 增量持久化）。Q 包在
P 包基础上把「简报公开面 + 设计 + G-DESIGN 门报告公开面」并入 question/methods，
使审查者能判「设计是否检验所提假设」「竞争解释是否被区分」（设计有效性）。
世界变体真值只进审查者通道（methods 附 private regime 说明，不进 actor 面）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2c_evidence_root
from orchestrator.gateway.quota_ledger import (QuotaError, QuotaLedger, RSI_S2C_NAMESPACE,
                                               billing_context, write_approval)
from orchestrator.gateway.router import ModelGateway
from orchestrator.rsi_bridge.review_s2 import build_pack_v2, merge_channels, run_channel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pool_open import build_briefs


def _load(bid: str) -> dict | None:
    ev = rsi_s2c_evidence_root()
    tdir = ev / "pool_q" / "trial" / bid
    rpt = tdir / "study_report.json"
    if not rpt.exists():
        return None
    rep = json.loads(rpt.read_text())
    if rep.get("status") != "DESIGN_READY" or rep.get("code") != "APPROVED":
        return rep  # 非完成实例（DESIGN_NOT_READY 等）
    claim = rep.get("claim") or {}
    report = claim.get("report") or {}
    k8 = k7 = None
    for d in (tdir / "runs").glob("**/K8.json"):
        k8 = json.loads(d.read_text()); break
    for d in (tdir / "runs").glob("**/K7.json"):
        k7 = json.loads(d.read_text()); break
    results = (k7 or {}).get("contrasts", [])
    nc = (k7 or {}).get("negative_control")
    gate = rep.get("gate_reports", [{}])[-1]
    return {"claim": claim, "report": report, "results": results, "nc": nc,
            "gate": gate, "rep": rep}


def main() -> int:
    ev = rsi_s2c_evidence_root()
    batch = "s2c-q-dual-review"
    try:
        write_approval(batch_id=batch, task=3, stage="coding_debug", approved=16,
                       beyond_normal=False, note="S2c Q 池双审 2×N（自批）",
                       namespace=RSI_S2C_NAMESPACE)
        print(f"批准件：{batch} task3 approved 16")
    except QuotaError as e:
        print(f"批准件复用：{e}")
    gw = ModelGateway(max_attempts=1, ledger=QuotaLedger.rsi_s2c())
    call = lambda p: gw.call("coding_debug", [{"role": "user", "content": p}],
                             max_tokens=16384, temperature=0.0,
                             thinking={"type": "disabled"}, timeout=300)
    briefs = build_briefs()
    partial = ev / "review" / "pool_q_dual_review.partial.json"
    partial.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(partial.read_text()) if partial.exists() else {}
    done = {k for k, v in existing.items() if (v.get("merged") or {}).get("verdict") == "PASS"}
    reviews = dict(existing)
    with billing_context(task=3, batch_id=batch):
        for bid, brief in briefs.items():
            if bid in done:
                print(f"  {bid} 已 PASS，跳过"); continue
            data = _load(bid)
            if data is None or data.get("status") != "DESIGN_READY" or data.get("code") != "APPROVED":
                code = (data or {}).get("code", "MISSING")
                reviews[bid] = {"merged": {"verdict": "INCOMPLETE", "code": code}}
                print(f"  {bid}: 非完成（{code}）→ INCOMPLETE")
                continue
            q_text = brief.question
            methods = (f"MuJoCo 物理 2×2 DiD + NC（C/D 域）。简报公开面："
                       f"{json.dumps(brief.public_document(), ensure_ascii=False)[:900]}\n"
                       f"G-DESIGN 门报告(公开)：{json.dumps(data['gate'].get('public', {}), ensure_ascii=False)}\n"
                       f"设计有效性须判：① 设计是否检验所提假设；② 竞争解释是否被区分。")
            pack = build_pack_v2(instance_id=bid, arm="v2", channel="channel_a",
                                 question=q_text, methods=methods,
                                 results=data["results"], claim=data["claim"],
                                 report=data["report"], negative_control=json.dumps(data["nc"], ensure_ascii=False))
            chans = {}
            for ch in ("channel_a", "channel_b"):
                print(f"  {bid} × {ch} …")
                chans[ch] = run_channel(call, pack=pack, channel=ch)
                print(f"    → {(chans[ch].get('parsed') or {}).get('verdict')}")
            merged = merge_channels(instance_id=bid, arm="v2",
                                    channel_a=chans["channel_a"], channel_b=chans["channel_b"])
            reviews[bid] = {"channels": chans, "merged": merged}
            print(f"  {bid} 合并：{merged.get('verdict')}")
            partial.write_text(json.dumps({k: {"merged": v["merged"]} for k, v in reviews.items()},
                                          ensure_ascii=False, indent=2, default=str))
    merged_all = {k: v["merged"] for k, v in reviews.items()}
    verdicts = {k: v.get("verdict") for k, v in merged_all.items()}
    n_pass = sum(1 for v in verdicts.values() if v == "PASS")
    out = ev / "review" / "pool_q_dual_review.json"
    out.write_text(json.dumps({"schema": "simpraxis.s2c.pool_q_dual_review.v1",
                               "reviews": reviews, "merged": merged_all,
                               "verdicts": verdicts, "n_pass": n_pass}, ensure_ascii=False,
                              indent=2, default=str))
    print(f"\n双审：PASS {n_pass}/{len(verdicts)}  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
