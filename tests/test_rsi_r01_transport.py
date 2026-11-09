"""RSI-R0.1 传输与计数层单测（零网络、零付费）：AC20–AC24 的确定性证据。

fake sender 只在本进程内计数，不构造任何真实连接；真实协议链在 T6 才允许付费。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from orchestrator.rsi_r01 import attempts as AT
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import transport as T


def _plan(**kw) -> T.SendPlan:
    messages = kw.pop("messages", [{"role": "user", "content": "hello"}])
    return T.plan_from_messages(system=kw.pop("system", "s"),
                                messages=messages, **kw)


def _ok_sender(calls: list):
    def send(plan: T.SendPlan):
        calls.append(plan.sha256)
        return ({"http_status": 200, "request_id": "req-1", "stop_reason": "end_turn",
                 "usage": {"input_tokens": 7, "output_tokens": 3},
                 "content_blocks": [{"type": "text", "text": "hi"}]}, "hi")
    return send


# ---------------------------------------------------------------------------
# AC20：一逻辑请求最多一 POST
# ---------------------------------------------------------------------------


def test_one_logical_request_one_post_and_one_sender_call():
    calls: list = []
    tr = T.Transport()
    plan = _plan()
    att = tr.send(plan, T.count_input(plan), slot_id="s1", sender=_ok_sender(calls))
    assert calls == [plan.sha256]          # 恰好一次，无暗重试
    assert tr.posts == 1 and att.state == T.SendState.SENT_OK
    assert att.provider_request_id == "req-1" and att.usage["input_tokens"] == 7


def test_sender_exception_is_unknown_sent_and_not_resent():
    calls: list = []

    def boom(plan):
        calls.append(plan.sha256)
        raise ConnectionResetError("peer reset")

    tr = T.Transport()
    plan = _plan()
    with pytest.raises(ConnectionResetError):
        tr.send(plan, T.count_input(plan), slot_id="s1", sender=boom)
    assert len(calls) == 1                 # 绝不自动重发
    assert tr.posts == 1
    assert tr.attempts[0].state == T.SendState.UNKNOWN_SENT
    assert S.ReasonCode.UNKNOWN_SENT in tr.attempts[0].reason_codes


def test_preflight_refusal_never_calls_sender():
    calls: list = []
    tr = T.Transport()
    plan = _plan()
    bad = T.Accounting(verdict=T.AccountingVerdict.TOKEN_OVER_CAP,
                       n_bytes=plan.n_bytes, upper_tokens=plan.n_bytes,
                       lower_tokens=None, interval=None, interval_ok=False,
                       request_sha256=plan.sha256)
    with pytest.raises(T.PreflightRefused):
        tr.send(plan, bad, slot_id="s1", sender=_ok_sender(calls))
    assert calls == [] and tr.posts == 0
    assert tr.attempts[0].state == T.SendState.PREFLIGHT_BLOCKED


def test_accounting_hash_mismatch_refused_before_send():
    calls: list = []
    tr = T.Transport()
    plan = _plan()
    acc = T.count_input(plan)
    forged = T.Accounting(verdict=acc.verdict, n_bytes=acc.n_bytes,
                          upper_tokens=acc.upper_tokens,
                          lower_tokens=acc.lower_tokens, interval=acc.interval,
                          interval_ok=acc.interval_ok,
                          request_sha256="0" * 64)
    with pytest.raises(T.PreflightRefused):
        tr.send(plan, forged, slot_id="s1", sender=_ok_sender(calls))
    assert calls == [] and tr.posts == 0


# ---------------------------------------------------------------------------
# AC21：完整输入硬帽（计数对象＝将发送的完整序列化字节）
# ---------------------------------------------------------------------------


def test_cap_boundary_allows_20000_and_rejects_20001_bytes():
    base = {"messages": [{"role": "user", "content": ""}]}
    plan = T.SendPlan(request=base)
    pad = 20000 - plan.n_bytes
    exact = T.SendPlan(request={"messages": [{"role": "user", "content": "x" * pad}]})
    assert exact.n_bytes == 20000
    assert T.count_input(exact).verdict == T.AccountingVerdict.OK
    over = T.SendPlan(request={"messages": [{"role": "user", "content": "x" * (pad + 1)}]})
    assert over.n_bytes == 20001
    acc = T.count_input(over)
    assert acc.verdict == T.AccountingVerdict.TOKEN_OVER_CAP
    assert acc.upper_tokens == 20001 and not acc.interval_ok


def test_counting_covers_tools_history_and_hashes_serialized_bytes():
    bare = _plan()
    with_tools = _plan(tools=[{"name": "analyze", "description": "d" * 100}],
                       messages=[{"role": "user", "content": "hello"},
                                 {"role": "assistant", "content": "prior"},
                                 {"role": "user", "content": "again"}])
    assert with_tools.n_bytes > bare.n_bytes
    acc = T.count_input(with_tools)
    assert acc.request_sha256 == hashlib.sha256(with_tools.serialized).hexdigest()
    assert acc.upper_tokens == with_tools.n_bytes


def test_interval_requires_provable_lower_bound():
    plan = _plan()
    acc = T.count_input(plan, interval=(15000, 18000))
    assert acc.verdict == T.AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
    assert "LOWER_BOUND_UNPROVABLE" in acc.reasons
    with_const = T.count_input(plan, interval=(1, plan.n_bytes + 1),
                               max_token_bytes=4)
    assert with_const.lower_tokens == -(-plan.n_bytes // 4)
    assert with_const.verdict == T.AccountingVerdict.OK
    with pytest.raises(ValueError):
        T.count_input(plan, max_token_bytes=0)


# ---------------------------------------------------------------------------
# AC22：空答/非文本/截断证据不丢
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("meta,text,code", [
    ({"http_status": 200, "request_id": "r1", "usage": {"output_tokens": 0}}, "",
     S.ReasonCode.EMPTY_CONTENT),
    ({"http_status": 200, "request_id": "r2",
      "content_blocks": [{"type": "tool_use", "id": "t"}]}, "",
     S.ReasonCode.NON_TEXT_BLOCK),
    ({"http_status": 200, "request_id": "r3", "stop_reason": "max_tokens"}, "partial",
     S.ReasonCode.TRUNCATED),
])
def test_empty_answer_states_keep_request_id_and_raw_usage(meta, text, code):
    tr = T.Transport()
    plan = _plan()
    att = tr.send(plan, T.count_input(plan), slot_id="s1",
                  sender=lambda p: (meta, text))
    assert att.state == T.SendState.SENT_FAIL
    assert code in att.reason_codes
    assert att.provider_request_id == meta["request_id"]
    assert att.usage == meta.get("usage") or att.usage is None
    assert tr.posts == 1


def test_sent_fail_still_counts_as_consumed_post():
    tr = T.Transport()
    plan = _plan()
    tr.send(plan, T.count_input(plan), slot_id="s1", sender=lambda p: ({}, ""))
    assert tr.summary() == {"attempts": 1, "posts": 1,
                            T.SendState.PREFLIGHT_BLOCKED: 0,
                            T.SendState.SENT_OK: 0,
                            T.SendState.SENT_FAIL: 1,
                            T.SendState.UNKNOWN_SENT: 0}


# ---------------------------------------------------------------------------
# AC23 / AC24：UNKNOWN 占槽不重发、恢复可重算；帽越界硬拒且不挪额度
# ---------------------------------------------------------------------------


def test_unknown_sent_slot_occupies_and_survives_reopen(tmp_path):
    path = tmp_path / "quota" / "slots.json"
    sb = AT.SlotBudget(path)
    sb.claim(slot_id="p1", scope="protocol", family=None, trial_id=None)
    sb.record_post("p1", state=T.SendState.UNKNOWN_SENT)
    with pytest.raises(AT.SlotExhausted):
        sb.claim(slot_id="p1", scope="protocol", family=None, trial_id=None)
    reopened = AT.SlotBudget(path)          # 模拟崩溃后重开
    assert reopened.slots["p1"]["state"] == T.SendState.UNKNOWN_SENT
    assert reopened.slots["p1"]["posts"] == 1
    assert reopened.used_global() == 1      # 占槽可重算，不因未知而释放


def test_preflight_blocked_occupies_slot_without_post(tmp_path):
    sb = AT.SlotBudget(tmp_path / "slots.json")
    sb.claim(slot_id="t1", scope="trial", family="E", trial_id="dev-e")
    sb.mark_preflight_blocked("t1", reason="TOKEN_OVER_CAP")
    assert sb.slots["t1"]["state"] == "PREFLIGHT_BLOCKED"
    assert sb.slots["t1"]["posts"] == 0
    assert sb.used_trial("dev-e") == 1      # 占槽不补考


def test_trial_family_and_global_caps_do_not_borrow(tmp_path):
    sb = AT.SlotBudget(tmp_path / "slots.json")
    for i in range(AT.TRIAL_CAP):
        sb.claim(slot_id=f"a{i}", scope="trial", family="E", trial_id="trial-a")
    with pytest.raises(AT.SlotExhausted):
        sb.claim(slot_id="a-extra", scope="trial", family="E", trial_id="trial-a")
    # 同 trial 帽满不影响别的 trial 额度，但族帽独立计数
    sb.claim(slot_id="b0", scope="trial", family="E", trial_id="trial-b")
    assert sb.used_trial("trial-b") == 1 and sb.used_family("E") == AT.TRIAL_CAP + 1


def test_family_cap_blocks_even_with_unused_global_room(tmp_path):
    sb = AT.SlotBudget(tmp_path / "slots.json")
    for i in range(AT.FAMILY_CAP):
        sb.claim(slot_id=f"f{i}", scope="trial", family="D", trial_id=f"t{i}")
    assert sb.used_global() < AT.GLOBAL_CAP
    with pytest.raises(AT.SlotExhausted):
        sb.claim(slot_id="f-extra", scope="trial", family="D", trial_id="t-extra")


def test_protocol_cap_is_twelve():
    assert AT.PROTOCOL_CAP == 12
    assert S.TOTAL_NORMAL_POSTS == 228
