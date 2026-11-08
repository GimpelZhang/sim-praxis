"""T7 验证批次的零付费单测：固定顺序、停批语义、报告分母。

真跑六条轨迹属于 T7 实跑；这里只证明流程规矩：顺序不可调、仪器类失败停批且
其余 slot 记 CANCELLED 不从分母消失。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import develop as dv, gates, validate as val
from orchestrator.rsi_r0 import runner as rn
from tests.test_rsi_r0_develop import (PRIVATE, TASK, FakeGateway, FakeSession,
                                       _factory)


def _dev_report() -> dict:
    """形状合法的开发报告：三族终态 + E/D L4 PASS、R L5 PASS（G-V 的判定面）。"""
    from orchestrator.rsi_r0.contracts import sha256_of

    report = {
        "schema": "rsi_r0.development_report.v1",
        "families": [
            {"family": "E", "state": "SUBMITTED",
             "evaluation": {"layers": {"L4": {"status": "PASS"}}}},
            {"family": "D", "state": "SUBMITTED",
             "evaluation": {"layers": {"L4": {"status": "PASS"}}}},
            {"family": "R", "state": "SUBMITTED",
             "evaluation": {"layers": {"L5": {"status": "PASS"}}}},
        ],
    }
    report["report_sha256"] = sha256_of(report)
    return report


def _arm(campaign, monkeypatch, *, gate_v: bool = True):
    from orchestrator.gateway import quota_ledger as ql
    from tests.test_rsi_r0_protocol import _arm as arm_protocol

    runner = arm_protocol(campaign)
    for _f, _i, batch, task in val.VALIDATION_PLAN:
        ql.write_approval(batch_id=batch, task=task, stage="coding_debug",
                          approved=val.VALIDATION_CAP, beyond_normal=False,
                          note=f"T7 unit fixture {batch}", namespace=ql.RSI_R0_NAMESPACE)
        gates.write_approval(runner.gate_approval_path(batch), gates.approval_body(
            batch_id=batch, task_cap=val.VALIDATION_CAP, hard_cap=ql.RSI_R0_HARD_CAP,
            currency="USD", usd_ceiling=100.0, approved_by="user",
            approved_at_utc="2026-11-08T00:00:00Z", scope="validation"))
    # 评审 N-1：验证批次现在必须先在**开发**侧成立（AC27 的 G-V）。夹具就给一份
    # 形状合法的开发报告并冻结验证快照；gate_v=False 时不写，专门用来做负控。
    if gate_v:
        dv.write_report(_dev_report())
        runner.write_validation_snapshot()
    monkeypatch.setattr(dv, "load_task", lambda _id: TASK)
    monkeypatch.setattr(dv, "load_private", lambda _id: PRIVATE)
    monkeypatch.setattr(dv, "build_scene", lambda _t, _p: "<mujoco/>")
    return runner


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r0"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "t7")
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    yield root / "t7"
    for var in ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN",
                "SIMPRAXIS_ALLOW_PAID"):
        monkeypatch.delenv(var, raising=False)


def test_plan_is_fixed_order_and_six_slots():
    assert [p[1] for p in val.VALIDATION_PLAN] == [
        "E-v1", "D-v1", "R-v1", "E-v2", "D-v2", "R-v2"]
    assert [p[3] for p in val.VALIDATION_PLAN] == [2, 2, 2, 3, 3, 3]
    assert len(val.VALIDATION_PLAN) == 6


def test_empty_plan_is_refused_with_a_clear_message(campaign, monkeypatch):
    """第三份评审 LOW-2：空计划不该以 ``tuple index out of range`` 冒充门失败。"""
    runner = _arm(campaign, monkeypatch)
    with pytest.raises(val.ValidationError, match="计划为空"):
        val.run_validation(runner, FakeGateway([]), plan=(), session_factory=_factory())


def test_hidden_retry_stops_the_batch_and_cancels_the_rest(campaign, monkeypatch):
    runner = _arm(campaign, monkeypatch)

    class RetryingGateway(FakeGateway):
        def call(self, stage, messages, *, max_tokens, temperature, timeout, capture=None):
            text, usage = super().call(stage, messages, max_tokens=max_tokens,
                                       temperature=temperature, timeout=timeout,
                                       capture=None)
            if capture is not None:
                capture.update({"stop_reason": "end_turn", "attempts_http": 2,
                                "provider_model": "fake", "body_sha256": "b" * 64})
            return text, usage

    gateway = RetryingGateway([])
    report = val.run_validation(runner, gateway, session_factory=_factory(),
                                clock=lambda: "2026-11-08T00:00:00Z")
    states = [s["state"] for s in report["slots"]]
    assert states[0] == "BLOCKED"
    assert all(s == "CANCELLED" for s in states[1:]), states
    assert report["halted"] and "HiddenRetryDetected" in report["halted"]
    assert report["all_assigned"]["denominator"] == 6
    assert report["all_assigned"]["cancelled"] == 5


def test_report_keeps_every_assigned_slot(campaign, monkeypatch):
    runner = _arm(campaign, monkeypatch)
    submit = json.dumps({"action": "submit", "family": "E",
                         "run_id": "run_000",
                         "claim": {"distance": 0.0, "final_speed": 0.0}})
    gateway = FakeGateway([submit] * 6)
    report = val.run_validation(runner, gateway, session_factory=_factory(),
                                clock=lambda: "2026-11-08T00:00:00Z")
    assert len(report["slots"]) == 6
    assert report["all_assigned"]["assigned"] == 6
    assert report["all_assigned"]["denominator"] == 6
    assert report["halted"] is None
    steps = report["totals"]["requests_used"]
    assert steps == 6, steps        # 每 slot 恰好一次交卷请求


# ---------------------------------------------------------------------------
# AC27 的负控（评审 N-1）：G-V 不能只在 CLI 一层
# ---------------------------------------------------------------------------
def test_run_validation_refuses_before_the_first_request_without_gate_v(campaign, monkeypatch):
    """开发没过门 ⇒ 一个验证请求都不许发（哪怕是直接调 API）。"""
    runner = _arm(campaign, monkeypatch, gate_v=False)
    submit = json.dumps({"action": "submit", "family": "E", "run_id": "run_000",
                         "claim": {"distance": 0.0, "final_speed": 0.0}})
    gateway = FakeGateway([submit] * 6)
    with pytest.raises(val.ValidationError, match="G-V"):
        val.run_validation(runner, gateway, session_factory=_factory(),
                           clock=lambda: "2026-11-08T00:00:00Z")
    assert gateway.rows == 0, f"G-V 不成立却已经发了 {gateway.rows} 次请求"


def test_validation_trial_preconditions_check_gate_v_not_gate_p(campaign, monkeypatch):
    """同一台机器按 role 选门：验证试验不能只凭 G-P 就发网。"""
    runner = _arm(campaign, monkeypatch, gate_v=False)
    # G-P 在这个夹具里是真（协议夹具已把六条都做实），G-V 缺开发报告与快照。
    runner.require_gate("G-P", batch_id="R0-V-E1")
    trial = dv.DevelopmentTrial(
        runner, FakeGateway([]), "E", instance_id="E-v1", batch_id="R0-V-E1",
        role="validation", task_id=2, request_cap=val.VALIDATION_CAP, checkpoints=())
    with pytest.raises(dv.DevelopmentError, match="G-V"):
        trial.preconditions()


def test_development_trial_still_checks_gate_p(campaign, monkeypatch):
    """角色分叉的另一半：开发试验的门名没有被连带改掉。"""
    runner = _arm(campaign, monkeypatch, gate_v=False)
    trial = dv.DevelopmentTrial(
        runner, FakeGateway([]), "E", instance_id="E-dev", batch_id="R0-V-E1",
        role="development", task_id=1, request_cap=24, checkpoints=())
    pre = trial.preconditions()
    assert pre["gate"]["gate"] == "G-P", pre["gate"]
