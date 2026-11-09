"""RSI-R0.2 候选闭环测试（AC10/AC11/AC14/AC15/AC16）。

全部注入假模型；没有任何网络调用。断言的是**编排事实**：动态反馈真的进了下一轮、
失败不被静态第二请求掩盖、隐性重试被计数、恢复不重跑物理动作、救援不冒充独立成功。
"""
from __future__ import annotations

import inspect

import pytest

from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H

RUN = H.action_json(action="run_experiment", params={"force": 0.5})
ANALYZE = H.action_json(action="analyze", source="import json\nprint(1)\n",
                        run_ids=["run-1"])
SUBMIT = H.action_json(action="submit", claim="SUPPORTED", run_ids=["run-1"],
                       analysis_ids=["a-2"], mean_effect=1.0, z=2.0)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    H.write_slots(c, ["E-dev-01", "E-dev-02", "E-dev-03", "E-dev-04"])
    return c


def test_full_loop_dynamic_feedback_then_submit(ctx):
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02", "E-dev-03"],
        replies=[RUN, ANALYZE, SUBMIT], with_campaign_state=True)
    out = loop.run()
    assert out["outcome"] == "submitted"
    assert out["status"] == ST.TRIAL_SUBMITTED
    assert out["posts_sent"] == 3 and out["turns"] == 3
    assert out["independent_success"] is True
    assert [slots.status(s) for s in ("E-dev-01", "E-dev-02", "E-dev-03")] == \
        [ST.SLOT_OK] * 3
    # 第二轮请求里必须出现第一轮的真实结果（run-1），而不是静态预填
    second = model.requests[1]
    joined = " ".join(m["content"] for m in second.messages)
    assert "run-1" in joined
    assert model.requests[1].sha256 != model.requests[0].sha256
    # submit 后的裁判来自 trial.judge，不是候选自报
    judge_events = [e for e in journal.events if e.get("action") == "judge"]
    assert judge_events and judge_events[-1]["ok"] is True
    assert book.summary()["settled_usd"] > 0


def test_policy_violation_is_terminal_valid_fail(ctx):
    """§10.2 行8（评审 M5）：越权动作被拦截 ⇒ 该 trial VALID_FAIL_POLICY 并停。

    以前只记一个错误码然后继续自修——越权的 POST 已经花掉，模型若下一轮直接
    提交就会被计成独立成功（等于奖励越权）。现在必须终态停该 trial、不判成功、
    不叫泄密（未越权成功），剩余槽保留给别的 trial。
    """
    illegal = H.action_json(action="run_experiment", params={"force": 0.5,
                                                            "admin_reset": 1})
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02"],
        replies=[illegal, SUBMIT], with_campaign_state=True)
    out = loop.run()
    assert out["outcome"] == "policy_violation"
    assert out["status"] == ST.TRIAL_VALID_FAIL_POLICY
    assert out["independent_success"] is not True   # 从未提交，绝不计成功
    assert out["posts_sent"] == 1                 # 越权那一次 POST 已花
    assert len(model.requests) == 1               # 停 trial，绝不继续自修
    ev = [e for e in journal.events if e.get("code") == ST.EV_POLICY_VIOLATION]
    assert ev and ev[0]["ok"] is False
    assert ev[0]["payload"]["slot_id"] == "E-dev-01"
    assert slots.status("E-dev-02") == ST.SLOT_UNUSED
    assert book.summary()["settled_usd"] > 0


def test_malformed_json_gets_visible_error_and_self_repairs(ctx):
    loop, model, store, journal, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02"],
        replies=["不是 JSON，我是解释文字", SUBMIT])
    out = loop.run()
    assert out["outcome"] == "submitted" and out["posts_sent"] == 2
    second = model.requests[1]
    joined = " ".join(m["content"] for m in second.messages)
    assert "ACTION_NOT_JSON" in joined            # 结构化错误可见
    assert "不是 JSON" in joined                  # 原回复原样保留在历史
    fail = [e for e in journal.events if e.get("action") == "parse"]
    assert fail and fail[0]["code"] == "ACTION_NOT_JSON"


def test_injected_send_failure_not_retried(ctx):
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02"],
        replies=[TimeoutError("连接中断"), SUBMIT], with_campaign_state=True)
    out = loop.run()
    assert out["outcome"] == "unknown_sent"
    assert out["status"] == ST.TRIAL_UNKNOWN_SENT
    assert len(model.requests) == 1               # 绝不重发
    assert slots.status("E-dev-01") == ST.SLOT_UNKNOWN
    assert slots.status("E-dev-02") == ST.SLOT_UNUSED
    summary = book.summary()
    assert summary["pending_usd"] == 1.5 and summary["paused"] is True
    assert ST.CampaignState(ctx).paused is True


def test_empty_response_blocks_trial_without_retry(ctx):
    loop, model, store, journal, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02"], replies=["", SUBMIT])
    out = loop.run()
    assert out["outcome"] == "empty_response"
    assert out["status"] == ST.TRIAL_BLOCKED
    assert len(model.requests) == 1
    assert slots.status("E-dev-01") == ST.SLOT_FAIL


def test_post_cap_reached_is_valid_fail_cap(ctx):
    loop, model, store, _, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02", "E-dev-03"],
        replies=[RUN, RUN], post_cap=2)
    out = loop.run()
    assert out["outcome"] == "valid_fail_cap"
    assert out["status"] == ST.TRIAL_VALID_FAIL_CAP
    assert out["posts_sent"] == 2
    assert len(model.requests) == 2
    assert slots.status("E-dev-03") == ST.SLOT_UNUSED


def test_input_limit_blocks_before_any_post(ctx):
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=["E-dev-01"], replies=[SUBMIT],
        counter=lambda text: 20001)
    out = loop.run()
    assert out["outcome"] == "input_limit"
    assert out["status"] == ST.TRIAL_BLOCKED_INPUT_LIMIT
    assert model.requests == []                  # 零 POST
    assert slots.status("E-dev-01") == ST.SLOT_UNUSED
    assert book.summary()["settled_usd"] == 0.0
    codes = [e.get("code") for e in journal.events]
    assert "TOKEN_OVER_CAP" in codes


def test_budget_block_cancels_remaining_slots(ctx):
    loop, model, store, journal, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02", "E-dev-03"],
        replies=[RUN, RUN], with_campaign_state=True,
        billing=B.BillingBook(ctx, policy=B.BillingPolicy(cap_usd=1.5)))
    out = loop.run()
    assert out["outcome"] == "budget"
    assert out["status"] == ST.TRIAL_BLOCKED
    assert out["reason_code"] == ST.EV_BUDGET_HIT
    assert slots.status("E-dev-01") == ST.SLOT_OK
    assert slots.status("E-dev-02") == ST.SLOT_CANCELLED
    assert slots.status("E-dev-03") == ST.SLOT_CANCELLED
    assert "BLOCKED_BUDGET" in (ST.CampaignState(ctx).pause_reason() or "")


def test_hidden_retry_is_counted_not_hidden(ctx):
    loop, model, store, journal, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01"], replies=[SUBMIT], attempts_http=2)
    out = loop.run()
    assert out["posts_sent"] == 1
    assert out["posts_hidden_retry"] == 1
    ev = [e for e in journal.events if e.get("code") == CD.EV_HIDDEN_RETRY]
    assert ev and "attempts_http=2" in ev[0]["message"]


def test_tool_outcome_unknown_stops_and_pauses(ctx):
    loop, model, store, journal, slots, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01", "E-dev-02"], replies=[RUN, RUN],
        trial=H.FakeTrial(raise_on={"run_experiment"}),
        with_campaign_state=True)
    out = loop.run()
    assert out["outcome"] == "tool_outcome_unknown"
    assert out["status"] == ST.TRIAL_TOOL_OUTCOME_UNKNOWN
    assert ST.CampaignState(ctx).paused is True
    pend = journal.pending_intents(loop.state.trial_id)
    assert len(pend) == 1 and pend[0]["action"] == "run_experiment"
    assert len(model.requests) == 1                   # 不重跑原实验


def test_resume_after_tool_crash_never_reruns(ctx):
    loop, model, store, journal, _, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01"], replies=[RUN],
        trial=H.FakeTrial(raise_on={"run_experiment"}),
        with_campaign_state=True)
    loop.run()
    resumed = store.resume(store.load(loop.state.trial_id), ST.Journal(ctx))
    assert resumed.status == ST.TRIAL_TOOL_OUTCOME_UNKNOWN
    assert len(model.requests) == 1


def test_assisted_marks_independent_success_false(ctx):
    loop, model, store, journal, _, _ = H.build_loop(
        ctx, slot_ids=["E-dev-01"], replies=[SUBMIT])
    loop.run()
    st = loop.assist("维护者手工替换了候选分析源码")
    assert st.status == ST.TRIAL_ASSISTED
    assert st.assisted is True and st.independent_success is False
    assert any(e.get("code") == ST.EV_ASSISTED for e in journal.events)


def test_no_submit_injection_api():
    src = inspect.getsource(CD)
    assert "def submit" not in src
    assert "def inject" not in src


def test_gateway_caller_requires_single_attempt():
    class Gw:
        max_attempts = 5

        def call(self, *a, **k):        # pragma: no cover - 构造期就该拒绝
            raise AssertionError("不得调用")

    with pytest.raises(CD.CandidateError):
        CD.GatewayCaller(gateway=Gw())


def _req() -> "CD.M.Request":
    return CD.M.Request(system="SYS", messages=({"role": "user", "content": "hi"},),
                        max_tokens=8192, serialized="{}", sha256="0" * 64,
                        n_bytes=2, local_tokens=3)


def test_gateway_caller_requires_billing_when_gateway_has_ledger():
    """T4 首跑事故的构造期守卫：挂了账本却没有记账归属 ⇒ 造 caller 就拒绝。

    此前该缺陷只在第一次网关调用时由 ``BillingGate._ctx()`` 暴露，槽已 claim、
    那次 POST 被记成 UNKNOWN_SENT，整条协议链作废。守卫前移到构造点。
    """
    class Gw:
        max_attempts = 1
        _billing = object()

        def call(self, *a, **k):        # pragma: no cover - 构造期就该拒绝
            raise AssertionError("不得调用")

    with pytest.raises(CD.CandidateError):
        CD.GatewayCaller(gateway=Gw())


def test_gateway_caller_wraps_each_call_in_billing_context():
    from orchestrator.gateway.quota_ledger import current_billing

    seen: "dict[str, object]" = {}

    class Gw:
        max_attempts = 1
        _billing = object()

        def call(self, stage, messages, **kw):
            ctx = current_billing()
            seen.update(task=None if ctx is None else ctx.task,
                        batch_id=None if ctx is None else ctx.batch_id,
                        thread_id=None if ctx is None else ctx.thread_id)
            kw["capture"].update({"attempts_http": 1})
            return "ok", (1, 1)

    caller = CD.GatewayCaller(gateway=Gw(),
                              billing={"task": 0, "batch_id": "R02-T4-1",
                                       "thread_id": "proto"})
    reply = caller(_req())
    assert reply.text == "ok"
    assert seen == {"task": 0, "batch_id": "R02-T4-1", "thread_id": "proto"}
    # 上下文只包住单次调用，不在进程里常驻（跨 trial 不串账）
    assert current_billing() is None


def test_gateway_caller_rejects_blank_batch_id():
    class Gw:
        max_attempts = 1
        _billing = object()

    with pytest.raises(CD.CandidateError):
        CD.GatewayCaller(gateway=Gw(), billing={"task": 0, "batch_id": "   "})


def test_gateway_caller_passes_system_and_capture():
    seen = {}

    class Gw:
        max_attempts = 1

        def call(self, stage, messages, **kw):
            seen.update(stage=stage, messages=messages, **kw)
            kw["capture"].update({"usage": {"in": 10, "out": 2,
                                            "cache_read": 0, "cache_create": 0},
                                  "attempts_http": 1})
            return "ok", (10, 2)

    req = CD.M.Request(system="SYS", messages=({"role": "user", "content": "hi"},),
                       max_tokens=8192, serialized="{}", sha256="0" * 64,
                       n_bytes=2, local_tokens=3)
    reply = CD.GatewayCaller(gateway=Gw())(req)
    assert reply.text == "ok" and reply.attempts_http == 1
    assert seen["messages"][0] == {"role": "system", "content": "SYS"}
    assert seen["messages"][1]["content"] == "hi"
    assert seen["max_tokens"] == 8192
