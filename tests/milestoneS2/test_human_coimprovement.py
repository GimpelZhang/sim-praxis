"""AC-17：人机共同改进（HUMAN_COIMPROVEMENT）降级路径的负控。

打回意见含实质性算法修补 → driver 标 `human_cofix` 并把 claim 的
`conclusion_grade` 降级为 `human_machine_joint_improvement`；R2 不得据此判
`FIRST_CONTROLLED_IMPROVEMENT_ESTABLISHED`。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from orchestrator.hk.driver import GateDecision, ResearchDriver
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                    default_confirmer, register_task)

_REPO = Path(__file__).resolve().parents[2]


def _campaign_module():
    path = _REPO / "scripts" / "s2" / "prereg_campaign.py"
    spec = importlib.util.spec_from_file_location("s2_prereg_hc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_reject_marks_human_cofix_and_downgrades(tmp_path):
    campaign = _campaign_module()
    instance = campaign.INSTANCES[0]
    task = register_task(campaign.build_instance(instance))
    decisions = [GateDecision(approved=False,
                              human_notes="请在分析里加入实质性算法修补：改用分层 bootstrap"),
                 GateDecision(approved=True)]
    spec = {"schema": "simpraxis.rsi_s2.experiment_spec",
            "task_id": task.task_id, "study": instance["family"],
            "plan_sha256": task.sha256,
            "conditions": [c.to_dict() for c in task.conditions],
            "n_block": 2, "prereg_bundle_task_sha256": task.sha256,
            "negative_control_spec": task.negative_control_spec}

    def actor(name, contract, value):
        if name == "propose_plan":
            s = value.get("spec") or {}
            slot = s.get("negative_control_spec") or {}
            default = slot.get("default") or {}
            shams = [c for c in slot.get("candidates") or []
                     if str(c).startswith("sham") and c != default.get("sham")]
            return {"conditions": s.get("conditions"), "n_block": 2,
                    "study": s.get("study"), "plan_sha256": s.get("plan_sha256"),
                    "negative_control": {"base": default.get("base"),
                                         "sham": shams[0], "rationale": "亚阈"}}
        if name == "write_analysis":
            return {"source": "# x\n"}
        if name in ("submit_claim", "submit_claim_repair"):
            rows = value.get("contrasts") or [{}]
            report = {dim: f"{dim} 段：host 复算 {rows[0].get('claim')}（证据在 run 目录）"
                      for dim in ("question_hypothesis", "methods", "main_results",
                                   "heterogeneity_scope", "limitations_next_steps",
                                   "negative_control_handling")}
            return {"verdict": rows[0].get("claim"), "statement": "host 一致",
                    "contrasts": [{k: r[k] for k in ("id", "estimate", "ci_low",
                                                     "ci_high", "n_blocks", "claim")}
                                  for r in rows],
                    "report": report}
        if name == "diagnose_fail":
            return {"diagnosis": "需分层 bootstrap", "algorithm_patch": True}
        raise AssertionError(name)

    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: decisions.pop(0) if decisions else
        GateDecision(approved=True),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        # 人机共同改进降级是 **driver 级语义**（与 harness 无关）；用 v0 臂
        # 避免依赖候选包注册 G-REPORT。
        confirmer=default_confirmer, claim_guard=True, harness="v0",
        max_rejects_per_stage=2,
        nc_sensitivity={"detected": True, "estimate": 0.04, "delta": 0.001})
    outcome = driver.run_campaign({"goal": "S2"}, {instance["family"]: spec})
    stage = outcome["stages"][instance["family"]]
    assert stage["status"] == "done"
    # 打回带实质性修补 → 结论降级（人机共同改进），不得算 R2
    claim = stage.get("claim") or {}
    assert claim.get("human_cofix") is True
    assert claim.get("conclusion_grade") == "human_machine_joint_improvement"
