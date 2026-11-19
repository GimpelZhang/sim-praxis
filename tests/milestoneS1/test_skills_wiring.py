"""S1-2/AC-06/AC-17：K3/K5/K7 真接线 + G-CLAIM 负控。

- K3：``default_batch_runner`` 经受信服务实跑（条件漂移必须被拒）；
- K5：``default_analyzer`` 线性对比（DiD/pairwise）+ 90% 阴性区间；
- K7：``default_confirmer`` 逐点复算（不一致即停止码）；
- G-CLAIM：自报与 host 复算不符 → CLAIM_INVALID（AC-17 负控）。
"""
from __future__ import annotations

import json

from pathlib import Path

import pytest

from orchestrator.hk import contracts as C
from orchestrator.hk.driver import ResearchDriver, GateDecision
from orchestrator.hk.skills import (SkillError, default_analyzer,
                                    default_batch_runner, default_confirmer)
from orchestrator.rsi_r1 import task_specs as TS

PLAN_SHA = "d" * 64


def _conditions(task):
    return [c.to_dict() for c in task.conditions]


def test_batch_runner_rejects_condition_drift(tmp_path):
    task = TS.build_r1n_a()
    plan = {"study": "A_N", "plan_sha256": PLAN_SHA, "n_block": 1,
            "conditions": _conditions(task)[:4]}  # 缺 sham
    with pytest.raises(SkillError) as excinfo:
        default_batch_runner(plan, {"observations_root": str(tmp_path / "o"),
                                    "evidence_root": str(tmp_path / "e")})
    assert "CONDITIONS_DRIFT" in str(excinfo.value)


def test_batch_runner_without_roots_is_honest_failure():
    task = TS.build_r1n_a()
    plan = {"study": "A_N", "plan_sha256": PLAN_SHA, "n_block": 1,
            "conditions": _conditions(task)}
    from orchestrator.hk.skills import SkillEngineNotWired
    with pytest.raises(SkillEngineNotWired):
        default_batch_runner(plan)


def _run_pipeline(tmp_path: Path, n_block: int = 2):
    task = TS.build_r1n_a()
    ctx = {"observations_root": str(tmp_path / "obs"),
           "evidence_root": str(tmp_path / "ev"),
           "batch_id": "t", "fresh_root": str(tmp_path / "fresh")}
    plan = {"study": "A_N", "plan_sha256": PLAN_SHA, "n_block": n_block,
            "conditions": _conditions(task)}
    manifest = default_batch_runner(plan, ctx)
    analysis = default_analyzer(manifest)
    frozen = dict(analysis["confirm_plan"])
    frozen.update(frozen=True, analysis_sha256="x" * 64)
    confirmed = default_confirmer(frozen, ctx)
    return manifest, analysis, frozen, confirmed


def test_pipeline_k3_k5_k7_real(tmp_path):
    manifest, analysis, frozen, confirmed = _run_pipeline(tmp_path)
    assert manifest["n_runs"] == 5 * 2
    ids = {c["id"] for c in analysis["contrasts"]}
    assert ids == {"friction_x_mass", "negative_torsional"}
    negative = next(c for c in analysis["contrasts"]
                    if c["id"] == "negative_torsional")
    assert negative["level"] == "0.90"  # 阴性等效必须用 90% 区间
    primary = next(c for c in analysis["contrasts"] if c["id"] == "friction_x_mass")
    assert primary["level"] == "0.95"
    assert confirmed["diffs"] == []          # 逐点复算零差
    assert confirmed["recompute_report"]["n_diffs"] == 0
    for row in confirmed["contrasts"]:
        assert row["n_blocks"] == 2


def test_confirmer_requires_frozen_and_fresh_root(tmp_path):
    manifest, analysis, frozen, _ = _run_pipeline(tmp_path)
    with pytest.raises(SkillError):
        default_confirmer({**frozen, "frozen": False},
                          {"fresh_root": str(tmp_path / "f2")})
    with pytest.raises(SkillError) as excinfo:
        default_confirmer(frozen, {})
    assert "fresh_root" in str(excinfo.value)


def _actor_for(claim_tamper: bool):
    def actor(name, contract, value):
        if name == "propose_plan":
            spec = value.get("spec") or {}
            return {"conditions": spec.get("conditions"),
                    "n_block": spec.get("n_block", 2),
                    "study": spec.get("study", "A_N"),
                    "plan_sha256": spec.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# frozen analysis program\n"}
        if name == "submit_claim":
            rows = []
            for row in value.get("contrasts", []):
                rows.append({"id": row["id"],
                             "estimate": row["estimate"] + (1e-3 if claim_tamper else 0.0),
                             "ci_low": row["ci_low"], "ci_high": row["ci_high"],
                             "n_blocks": row["n_blocks"],
                             "claim": row.get("claim")})
            return {"claim": "TEST", "contrasts": rows,
                    "verdict": (value.get("contrasts") or [{}])[0].get("claim")}
        if name == "diagnose_fail":
            return {"diagnosis": "stub"}
        raise AssertionError(name)
    return actor


def _driver(tmp_path: Path, *, tamper: bool) -> ResearchDriver:
    task = TS.build_r1n_a()
    spec = {"schema": "simpraxis.rsi_s1.experiment_spec",
            "task_id": task.task_id, "study": "A_N", "plan_sha256": PLAN_SHA,
            "conditions": _conditions(task), "n_block": 2}
    return spec, ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=_actor_for(tamper),
        evidence_root=tmp_path / "driver_evidence",
        batch_runner=default_batch_runner,
        analyzer=default_analyzer,
        confirmer=default_confirmer,
        claim_guard=True,
    )


def test_g_claim_passes_when_self_report_matches_host(tmp_path):
    task = TS.build_r1n_a()
    spec = {"schema": "simpraxis.rsi_s1.experiment_spec",
            "task_id": task.task_id, "study": "A_N", "plan_sha256": PLAN_SHA,
            "conditions": _conditions(task), "n_block": 2}
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=_actor_for(False), evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    assert outcome["status"] == "done", outcome
    # 判词由 host 主对比分类决定（不得自报）；A_N 主对比在 2 block 小样本下为 REFUTED
    assert outcome["stages"]["A_N"]["claim"]["verdict"] == "REFUTED"


def test_g_claim_rejects_transcription_mismatch(tmp_path):
    """AC-17 负控：自报与 host 复算不符即 CLAIM_INVALID（R1.3 转写错形状）。"""
    task = TS.build_r1n_a()
    spec = {"schema": "simpraxis.rsi_s1.experiment_spec",
            "task_id": task.task_id, "study": "A_N", "plan_sha256": PLAN_SHA,
            "conditions": _conditions(task), "n_block": 2}
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=_actor_for(True), evidence_root=tmp_path / "ev2",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    assert outcome["status"] == "human_failure"
    assert outcome["stages"]["A_N"]["code"] == "CLAIM_INVALID"


def test_driver_passes_skill_context_and_diagnose(tmp_path):
    """打回意见注入 diagnose 触点（≤2），且意见进下一轮 K1 输入。"""
    task = TS.build_r1n_a()
    spec = {"schema": "simpraxis.rsi_s1.experiment_spec",
            "task_id": task.task_id, "study": "A_N", "plan_sha256": PLAN_SHA,
            "conditions": _conditions(task), "n_block": 2}
    seen = []

    def actor(name, contract, value):
        seen.append((name, value))

        if name == "propose_plan":
            spec = value.get("spec") or {}
            return {"conditions": spec.get("conditions"),
                    "n_block": spec.get("n_block", 2),
                    "study": spec.get("study", "A_N"),
                    "plan_sha256": spec.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# x\n"}
        if name == "submit_claim":
            rows = [{"id": r["id"], "estimate": r["estimate"],
                     "ci_low": r["ci_low"], "ci_high": r["ci_high"],
                     "n_blocks": r["n_blocks"], "claim": r["claim"]}
                    for r in value["contrasts"]]
            return {"claim": "T",
                    "verdict": (value.get("contrasts") or [{}])[0].get("claim"),
                    "contrasts": rows}
        if name == "diagnose_fail":
            return {"diagnosis": "重查结算"}
        raise AssertionError(name)

    gates = [GateDecision(approved=False, human_notes="请复核统计口径"),
             GateDecision(approved=True)]
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: gates.pop(0),
        actor=actor, evidence_root=tmp_path / "ev3",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    assert outcome["status"] == "done"
    names = [n for n, _ in seen]
    assert names.count("diagnose_fail") == 1
    k1_inputs = [v for n, v in seen if n == "propose_plan"]
    assert k1_inputs[1].get("human_notes") == "请复核统计口径"
    assert k1_inputs[1].get("diagnose", {}).get("diagnosis") == "重查结算"


def test_diagnose_budget_caps_and_human_cofix_marks_claim(tmp_path):
    """AC-08/AC-13：诊断触点 ≤2；含算法修补意见时 claim 标 human_cofix。"""
    from orchestrator.rsi_r1 import task_specs as TS

    task = TS.build_r1n_a()
    spec = {"schema": "x", "task_id": task.task_id, "study": "A_N",
            "plan_sha256": "f" * 64, "n_block": 2,
            "conditions": [c.to_dict() for c in task.conditions]}
    diagnoses = []

    def actor(name, contract, value):
        if name == "propose_plan":
            s = value.get("spec") or {}
            return {"conditions": s.get("conditions"), "n_block": 2,
                    "study": "A_N", "plan_sha256": s.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# x\n"}
        if name == "submit_claim":
            return {"verdict": (value.get("contrasts") or [{}])[0].get("claim"),
                    "statement": "s",
                    "contrasts": [{k: r[k] for k in ("id", "estimate", "ci_low",
                                                     "ci_high", "n_blocks", "claim")}
                                  for r in value["contrasts"]]}
        if name == "diagnose_fail":
            diagnoses.append(value.get("run_idx"))
            return {"diagnosis": "补统计", "algorithm_patch": True}
        raise AssertionError(name)

    gates = [GateDecision(approved=False, human_notes=f"第{i}次打回，请改方法")
             for i in (1, 2, 3)]  # 第 3 次把 max_rejects=3 打满
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: gates.pop(0),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True,
        max_rejects_per_stage=3, diagnose_budget=2)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    stage = outcome["stages"]["A_N"]
    assert stage["status"] == "human_failure"
    assert stage["code"] == "REJECT_BUDGET_EXHAUSTED"
    assert diagnoses == [1, 2]                    # 诊断帽恰 2：第三轮超帽不再调用
    k8 = json.loads((tmp_path / "ev" / "runs" / "A_N_run003" / "K8.json").read_text())
    assert k8.get("human_cofix") is True
    assert k8.get("conclusion_grade") == "human_machine_joint_improvement"


def test_claim_verdict_must_equal_host_primary_classification(tmp_path):
    """评审 HIGH-2 负控：数值行逐字正确但总判词翻转 → CLAIM_INVALID。"""
    from orchestrator.rsi_r1 import task_specs as TS

    task = TS.build_r1n_a()
    spec = {"schema": "x", "task_id": task.task_id, "study": "A_N",
            "plan_sha256": "9" * 64, "n_block": 2,
            "conditions": [c.to_dict() for c in task.conditions]}

    def actor(name, contract, value):
        if name == "propose_plan":
            s = value.get("spec") or {}
            return {"conditions": s.get("conditions"), "n_block": 2,
                    "study": "A_N", "plan_sha256": s.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# x\n"}
        if name == "submit_claim":
            host = (value.get("contrasts") or [{}])[0].get("claim")
            flipped = {"REFUTED": "SUPPORTED", "SUPPORTED": "REFUTED"}.get(host, "INCONCLUSIVE")
            return {"verdict": flipped, "statement": "s",
                    "contrasts": [{k: r[k] for k in ("id", "estimate", "ci_low",
                                                     "ci_high", "n_blocks", "claim")}
                                  for r in value["contrasts"]]}
        if name == "diagnose_fail":
            return {"diagnosis": "stub"}
        raise AssertionError(name)

    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    assert outcome["status"] == "human_failure"
    assert outcome["stages"]["A_N"]["code"] == "CLAIM_INVALID"
