"""S0-7：Gate1/Gate2 打回循环五剧本（语义照 nodes.py；假门剧本）。"""
from __future__ import annotations

from pathlib import Path

from orchestrator.hk.driver import (GATE2_MAX_REJECTS_PER_STAGE, GateDecision,
                                    ResearchDriver)

from ._fakes import (GateScript, approve, fake_analyzer, fake_actor,
                     fake_batch_runner, fake_confirmer, reject, spec)


def _driver(tmp_path: Path, *, gate1=None, gate2=None, actor=None, runner=None,
            touches=None, max_rejects=GATE2_MAX_REJECTS_PER_STAGE):
    actor_log = []
    batch_calls = []

    def batch(plan, context=None):
        batch_calls.append(plan)
        return fake_batch_runner(plan, context)

    driver = ResearchDriver(
        gate1_hook=gate1 or GateScript([]),
        gate2_hook=gate2 or GateScript([]),
        actor=actor or fake_actor(actor_log),
        evidence_root=tmp_path / "evidence",
        batch_runner=runner or batch,
        analyzer=fake_analyzer,
        confirmer=fake_confirmer,
        max_rejects_per_stage=max_rejects,
    )
    driver.actor_log = actor_log
    driver.batch_calls = batch_calls
    return driver


def test_approve_first_time(tmp_path):
    driver = _driver(tmp_path)
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    assert outcome["status"] == "done"
    stage = outcome["stages"]["s1"]
    assert stage["status"] == "done"
    assert stage["run_idx"] == 1
    assert stage["rejects"] == {}
    assert stage["claim"]["verdict"] == "INCONCLUSIVE"
    assert (tmp_path / "evidence" / "runs" / "s1_run001" / "K8.json").exists()


def test_one_reject_with_notes_reruns_as_run_idx_2(tmp_path):
    driver = _driver(tmp_path, gate2=GateScript([reject("补充 sham 对照")]))
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    stage = outcome["stages"]["s1"]
    assert stage["status"] == "done"
    assert stage["run_idx"] == 2
    assert stage["rejects"] == {"s1": 1}
    # 意见注入补救线程：第二轮 K1 的 actor 输入带 human_notes
    notes = [entry["notes"] for entry in driver.actor_log if entry["name"] == "propose_plan"]
    assert notes[:1] == [""] and notes[1] == "补充 sham 对照"
    # 两轮各自全新 run 目录（task8.py:160 语义）
    run1 = tmp_path / "evidence" / "runs" / "s1_run001"
    run2 = tmp_path / "evidence" / "runs" / "s1_run002"
    assert run1.is_dir() and run2.is_dir()


def test_two_rejects_exhaust_stage_budget(tmp_path):
    driver = _driver(tmp_path, gate2=GateScript([reject("a"), reject("b")]))
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    stage = outcome["stages"]["s1"]
    assert outcome["status"] == "human_failure"
    assert stage["code"] == "REJECT_BUDGET_EXHAUSTED"
    assert stage["rejects"] == {"s1": 2}
    assert stage["run_idx"] == 2  # 打回 2 次即结案，不产生第三次 run


def test_reject_without_notes_closes_immediately(tmp_path):
    driver = _driver(tmp_path, gate2=GateScript([GateDecision(False, "")]))
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    stage = outcome["stages"]["s1"]
    assert stage["status"] == "human_failure"
    assert stage["code"] == "REJECT_WITHOUT_NOTES"
    assert stage["run_idx"] == 1
    assert stage["rejects"] == {}


def test_two_stages_count_rejects_independently(tmp_path):
    gate2 = GateScript([reject("stage-a 意见"), approve(), reject("stage-b 意见")])
    driver = _driver(tmp_path, gate2=gate2)
    outcome = driver.run_campaign({"goal": "chain"},
                                  {"stage_a": spec(), "stage_b": spec()})
    assert outcome["status"] == "done"
    assert outcome["rejects"] == {"stage_a": 1, "stage_b": 1}  # CR-20：per-Stage
    assert outcome["stages"]["stage_a"]["run_idx"] == 2
    assert outcome["stages"]["stage_b"]["run_idx"] == 2


def test_gate1_rejects_twice_and_no_engine_runs_before_approval(tmp_path):
    gate1 = GateScript([reject("目标不清"), reject("仍不清")], default_approved=False)
    driver = _driver(tmp_path, gate1=gate1)
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    assert outcome == {"status": "human_failure", "code": "REJECT_BUDGET_EXHAUSTED",
                       "stage": "gate1"}
    assert driver.actor_log == []      # No Fake Approval：批准前零 System 2 调用
    assert driver.batch_calls == []    # 批准前零引擎实跑


def test_gate1_empty_notes_closes_without_engine(tmp_path):
    gate1 = GateScript([GateDecision(False, "")], default_approved=False)
    driver = _driver(tmp_path, gate1=gate1)
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    assert outcome["code"] == "REJECT_WITHOUT_NOTES"
    assert driver.batch_calls == []


def test_touchpoint_budget_is_enforced_per_run(tmp_path):
    driver = _driver(tmp_path, actor=fake_actor([]))
    driver.touchpoint_budget = 2  # 3 个触点（K1/K4/K8）> 预算 2
    outcome = driver.run_campaign({"goal": "A_N"}, {"s1": spec()})
    stage = outcome["stages"]["s1"]
    assert stage["status"] == "human_failure"
    assert stage["code"] == "TOUCHPOINT_BUDGET_EXCEEDED"
    # 预算按 run 重置：同一个 driver 上第二次 run_stage 不应累计耗尽
    driver3 = _driver(tmp_path / "third")
    driver3.touchpoint_budget = 3  # 恰好等于一次 run 的触点数
    first = driver3.run_stage("s1", spec())
    second = driver3.run_stage("s2", spec())
    assert first.status == "done" and second.status == "done", (first, second)
