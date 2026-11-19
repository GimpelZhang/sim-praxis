"""S2 战役接线冒烟（零付费）：v1 臂 K0→K8 全链 + NC 三连 + 六段报告。

用**测试夹具**候选包（不是战役候选；候选由 proposer 会话产出）驱动 v1 臂，
验证：NC 槽提名 → host 构造对照 → 三连判据 → 六段报告 → G-REPORT → 机器门。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator.hk import guards as G
from orchestrator.hk import report_contract as RC
from orchestrator.hk.candidate import load_candidate
from orchestrator.hk.driver import GateDecision, ResearchDriver
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                    default_confirmer)

_REPO = Path(__file__).resolve().parents[2]

_CANDIDATE_GUARDS = ((_REPO / "orchestrator" / "hk" / "guards.py")
                     .read_text(encoding="utf-8") + '''

def g_report(ctx):
    if ctx.report_doc is None:
        raise GuardViolation("g_report", "REPORT_MISSING", "缺报告")
    from orchestrator.hk import report_contract as RC
    missing = RC.missing_sections(ctx.report_doc)
    if missing:
        raise GuardViolation("g_report", "REPORT_INCOMPLETE", str(missing))


GUARDS["G-REPORT"] = g_report
''')

_CANDIDATE_TOUCHPOINTS = '''TOUCHPOINT_INSTRUCTIONS_V2 = {
    "propose_plan": "提名 negative_control（base/sham）",
    "write_analysis": "写分析程序",
    "submit_claim": "五值 verdict + report 六段",
    "submit_claim_repair": "补全 report",
    "diagnose_fail": "诊断",
}
'''


def _campaign_module():
    path = _REPO / "scripts" / "s2" / "prereg_campaign.py"
    spec = importlib.util.spec_from_file_location("s2_prereg_wiring", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    (package / "guards.py").write_text(_CANDIDATE_GUARDS, encoding="utf-8")
    (package / "touchpoints.py").write_text(_CANDIDATE_TOUCHPOINTS,
                                             encoding="utf-8")
    return package


def _actor_factory(state):
    """v1 模板的假 actor（逐字符合触点合同；不写死 host 结论）。"""

    def actor(name, contract, value):
        if name == "propose_plan":
            spec = value.get("spec") or {}
            slot = spec.get("negative_control_spec") or {}
            default = slot.get("default") or {}
            candidates = [str(c) for c in slot.get("candidates") or []]
            # NC 设计技能：选**非默认**的亚阈微扰 sham（默认是退化 torsional）
            shams = [c for c in candidates if c.startswith("sham")
                     and c != default.get("sham")]
            base = default.get("base") or candidates[0]
            sham = shams[0]
            return {"conditions": spec.get("conditions"), "n_block": 2,
                    "study": spec.get("study"), "plan_sha256": spec.get("plan_sha256"),
                    "negative_control": {"base": base, "sham": sham,
                                         "rationale": "亚阈微扰"}}
        if name == "write_analysis":
            return {"source": "# frozen analysis\n"}
        if name in ("submit_claim", "submit_claim_repair"):
            rows = value.get("contrasts") or [{}]
            host = rows[0].get("claim")
            report = {name_: f"{name_} 段：host 复算 {host}（证据在 run 目录）"
                      for name_ in RC.SECTIONS}
            return {"verdict": host, "statement": "host 一致",
                    "contrasts": [{k: r[k] for k in ("id", "estimate", "ci_low",
                                                     "ci_high", "n_blocks", "claim")}
                                  for r in rows],
                    "report": report}
        if name == "diagnose_fail":
            return {"diagnosis": "stub", "algorithm_patch": False}
        raise AssertionError(name)

    return actor


def test_v1_pipeline_completes_with_nc_and_report(tmp_path):
    campaign = _campaign_module()
    instance = campaign.INSTANCES[0]  # A 家族（6 条件 = 192 runs，n_block 降到 2）
    task = campaign.build_instance(instance)
    from orchestrator.hk.skills import register_task

    register_task(task)
    package = _candidate_package(tmp_path)
    candidate = load_candidate(package)
    assert "G-REPORT" in G.GUARDS

    spec = {"schema": "simpraxis.rsi_s2.experiment_spec",
            "task_id": task.task_id, "study": instance["family"],
            "plan_sha256": task.sha256,
            "conditions": [c.to_dict() for c in task.conditions],
            "n_block": 2, "prereg_bundle_task_sha256": task.sha256,
            "negative_control_spec": task.negative_control_spec}
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=_actor_factory({}), evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True, harness="v1",
        nc_sensitivity={"detected": True, "estimate": 0.04, "delta": 0.001})
    outcome = driver.run_campaign({"goal": "S2"}, {instance["family"]: spec})
    stage = outcome["stages"][instance["family"]]
    assert stage["status"] == "done", stage
    run_dir = tmp_path / "ev" / "runs" / f"{instance['family']}_run001"
    k5 = json.loads((run_dir / "K5.json").read_text())
    assert k5["negative_control"]["passed"] is True
    k8 = json.loads((run_dir / "K8.json").read_text())
    assert k8["verdict"] in ("SUPPORTED", "REFUTED", "NOT_DETECTED",
                              "EQUIVALENT", "INCONCLUSIVE")
    assert RC.missing_sections(k8["report"]) == []


def test_v0_pipeline_rejects_degenerate_default_nc(tmp_path):
    """v0 臂无 NC 提名 → 槽默认退化 sham → 结构性拒绝（S1 缺陷①复现）。"""
    campaign = _campaign_module()
    instance = campaign.INSTANCES[0]
    task = campaign.build_instance(instance)
    from orchestrator.hk.skills import register_task

    register_task(task)
    spec = {"schema": "simpraxis.rsi_s2.experiment_spec",
            "task_id": task.task_id, "study": instance["family"],
            "plan_sha256": task.sha256,
            "conditions": [c.to_dict() for c in task.conditions],
            "n_block": 2, "prereg_bundle_task_sha256": task.sha256,
            "negative_control_spec": task.negative_control_spec}

    def actor(name, contract, value):
        if name == "propose_plan":
            s = value.get("spec") or {}
            return {"conditions": s.get("conditions"), "n_block": 2,
                    "study": s.get("study"), "plan_sha256": s.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# x\n"}
        raise AssertionError(name)

    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True, harness="v0")
    outcome = driver.run_campaign({"goal": "S2"}, {instance["family"]: spec})
    stage = outcome["stages"][instance["family"]]
    assert stage["status"] == "human_failure"
    assert stage["code"] == "NC_DEGENERATE"


def test_v0_b_family_rejected_without_nc(tmp_path):
    campaign = _campaign_module()
    instance = campaign.INSTANCES[3]  # B 家族：槽无默认
    task = campaign.build_instance(instance)
    from orchestrator.hk.skills import register_task

    register_task(task)
    spec = {"schema": "simpraxis.rsi_s2.experiment_spec",
            "task_id": task.task_id, "study": instance["family"],
            "plan_sha256": task.sha256,
            "conditions": [c.to_dict() for c in task.conditions],
            "n_block": 2, "prereg_bundle_task_sha256": task.sha256,
            "negative_control_spec": task.negative_control_spec}

    def actor(name, contract, value):
        if name == "propose_plan":
            s = value.get("spec") or {}
            return {"conditions": s.get("conditions"), "n_block": 2,
                    "study": s.get("study"), "plan_sha256": s.get("plan_sha256")}
        if name == "write_analysis":
            return {"source": "# x\n"}
        raise AssertionError(name)

    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True, harness="v0")
    outcome = driver.run_campaign({"goal": "S2"}, {instance["family"]: spec})
    stage = outcome["stages"][instance["family"]]
    assert stage["status"] == "human_failure"
    assert "NC_UNREGISTERED" in (stage.get("detail") or "") or \
        stage["code"] == "STAGE_RUNTIME_ERROR"
