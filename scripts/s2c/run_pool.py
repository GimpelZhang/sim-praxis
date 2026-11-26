#!/usr/bin/env python3
"""S2c P 池执行器（余量调查；不改 v2）：6 预置实例 K0→K8 + 独立复跑。

复用 S2b 的 v2 流程（run_s2b 同构），仅不改 v2、逐实例 register_task。
用法：python scripts/s2c/run_pool.py --pool P
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import rsi_s2c_evidence_root, rsi_s2c_sessions_dir
from orchestrator.hk.driver import GateDecision, ResearchDriver
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                    default_confirmer, register_task)
from orchestrator.hk.touchpoints import HeadlessActor, HeadlessSessionRunner
from orchestrator.rsi_bridge.session_ledger import SessionLedger

from pool_preset import build_instances


def run_pool() -> int:
    evidence_root = rsi_s2c_evidence_root()
    campaign_root = ensure_private_dir(evidence_root / "pool_p", mode=0o700)
    ledger = SessionLedger(rsi_s2c_sessions_dir() / "sessions.jsonl",
                           hard_cap_sessions=56)
    insts = build_instances()

    results = {}
    for name, (label, task) in insts.items():
        register_task(task)
        label_dir = ensure_private_dir(campaign_root / "trial" / name, mode=0o700)
        print(f"\n=== {name} [{label}] {task.task_id} ===")
        runner = HeadlessSessionRunner(sessions_root=rsi_s2c_sessions_dir() / "ws",
                                       ledger=ledger, max_turns=90, timeout_s=1800)
        actor = HeadlessActor(runner, evidence_root=label_dir, host_label=name)
        driver = ResearchDriver(
            gate1_hook=lambda card: GateDecision(approved=True),
            gate2_hook=lambda card, stage: GateDecision(approved=True),
            actor=actor, evidence_root=label_dir,
            batch_runner=default_batch_runner, analyzer=default_analyzer,
            confirmer=default_confirmer, claim_guard=True, harness="v2",
            touchpoint_budget=3, diagnose_budget=2)
        # spec 由 task 构建（沿用 S2b 的 study 字段）
        study = {"C_collision_rebound": "C_N", "D_forced_vibration": "D_N",
                 "A_contact_friction": "A_N"}[task.mechanism_id]
        spec = {"schema": "simpraxis.s2c.experiment_spec", "task_id": task.task_id,
                "study": study, "plan_sha256": task.sha256, "n_block": 32,
                "conditions": [c.to_dict() for c in task.conditions]}
        outcome = driver.run_campaign({"goal": name}, {study: spec})
        stage = (outcome.get("stages") or {}).get(study, {})
        claim = stage.get("claim") or {}
        status = outcome.get("status")
        print(f"  {status} / {stage.get('code')} verdict={claim.get('verdict')}")
        results[name] = {"label": label, "status": status,
                         "code": stage.get("code"),
                         "verdict": claim.get("verdict"),
                         "claim": claim, "outcome": outcome}
        (label_dir / "study_report.json").write_text(json.dumps(
            {"schema": "simpraxis.s2c.pool_study.v1", **results[name]},
            ensure_ascii=False, indent=2, sort_keys=True, default=str))

    summary = {"schema": "simpraxis.s2c.pool_p_summary.v1",
               "instances": {k: {"label": v["label"], "status": v["status"],
                                 "code": v["code"], "verdict": v["verdict"]}
                             for k, v in results.items()}}
    (campaign_root / "campaign_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\n汇总：{campaign_root / 'campaign_summary.json'}")
    done = sum(1 for v in results.values() if v["status"] == "done")
    print(f"done: {done}/{len(results)}")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", choices=("P",), default="P")
    args = ap.parse_args()
    return run_pool()


if __name__ == "__main__":
    raise SystemExit(main())
