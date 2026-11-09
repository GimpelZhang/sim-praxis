"""付费受闸通道单测（reviewer MEDIUM-4）：零网络、零付费。

证明 ``PaidTransport`` 是生产调用点：闸失败时 sender 一次都不被调用，
闸通过时恰好一次 POST、attempt 记账。
"""
from __future__ import annotations

import pytest

from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import paid as P
from orchestrator.rsi_r01 import transport as T


def _plan() -> T.SendPlan:
    return T.plan_from_messages(
        system=None, messages=[{"role": "user", "content": "ping"}],
        model="test-model")


def _ok_sender(calls: "list[T.SendPlan]"):
    def sender(plan: T.SendPlan):
        calls.append(plan)
        return {"content_blocks": [{"type": "text", "text": "pong"}],
                "http_status": 200}, "pong"
    return sender


def test_gate_failure_blocks_before_any_send(monkeypatch):
    def boom(ctx, repo_root=L.REPO_ROOT):
        raise L.LockError("LOCK_CHANGED", ["事实已变"])

    monkeypatch.setattr(L, "assert_paid_allowed", boom)
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    with pytest.raises(L.LockError):
        transport.send(plan, T.count_input(plan), slot_id="slot-1",
                       sender=_ok_sender(calls))
    assert calls == []            # 一个字节都没发
    assert transport.posts == 0
    assert transport.attempts == []


def test_gate_pass_sends_exactly_once(monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    att = transport.send(plan, T.count_input(plan), slot_id="slot-1",
                         sender=_ok_sender(calls))
    assert len(calls) == 1 and transport.posts == 1
    assert att.state == T.SendState.SENT_OK
    assert att.request_sha256 == plan.sha256


def test_gate_is_rechecked_for_every_post(monkeypatch):
    seen = {"n": 0}

    def gate(ctx, repo_root=L.REPO_ROOT):
        seen["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(L, "assert_paid_allowed", gate)
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    for _ in range(3):
        transport.send(plan, T.count_input(plan), slot_id="slot-x",
                       sender=_ok_sender(calls))
    assert seen["n"] == 3 and transport.posts == 3   # 每 POST 前都重算
