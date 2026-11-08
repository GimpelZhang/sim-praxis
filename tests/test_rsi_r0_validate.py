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


def _arm(campaign, monkeypatch):
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
