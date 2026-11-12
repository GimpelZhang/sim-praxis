"""RSI-R1.2 T2：恢复判别与里程碑细节（Plan §3.1；AC20/22/23/24/54）。

判别只看**宿主证据**（phase / 未闭 intent / 已确认效果 / 冻结状态 / 异常类），
缺证据默认 terminal；白名单只在 T0 注册，运行中不可扩展。
"""
from __future__ import annotations

import dataclasses

import pytest

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import recovery as RCV
from tests.rsi_r12_chain_helpers import FakeState, fail, make_parts, ok

POLICY = P.PROFILE_R12_DEF.protocol_policy


def ev(**kw) -> RCV.FailureEvidence:
    base = dict(phase="explore", frozen=False, open_intent=False,
                effect_status=RCV.EFFECT_NONE, code="RESOURCE_EXCEEDED")
    base.update(kw)
    return RCV.FailureEvidence(**base)


# ---------------------------------------------------------------------------
# AC20：帽是硬上限；白名单不可运行中扩展
# ---------------------------------------------------------------------------

def test_cap_is_hard_and_extra_post_never_sent() -> None:
    outcomes = [
        ok("write_artifact", kind="design", source_sha256="d" * 64),
        ok("propose_plan", plan_sha256="p" * 64, revision=1),
        ok("run_batch", run_ids=["r1"]),
    ] + [ok("analyze", analysis={"n": i}) for i in range(17)]
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    # 3 步推进 + 5 个额外动作后剩 4 呼 < 闭合依赖 5 呼 → 发送前停；
    # 无论停在哪一码，实呼都 ≤ 帽 12，第 13 呼永不发送
    assert parts.loop.turns == 8
    assert len(parts.loop.outcomes) == 12  # 余下脚本一呼未发
    assert result.stopped == RCV.CODE_BUDGET_UNREACHABLE
    assert result.completed is False and result.stage == 3
    assert len(result.actions) == 8
    assert result.qualification["total_posts"] == 8 <= 12
    assert result.qualification["budgeted_completion"] is False


def test_posts_exhausted_never_sends_past_cap() -> None:
    # 额外动作一直在推进无望的链：即使一直把帽打满，也绝不多发 1 呼
    outcomes = [ok("write_artifact", kind="design", source_sha256="d" * 64)] \
        + [ok("analyze", analysis={"n": i}) for i in range(30)]
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns <= 12
    assert len(parts.loop.outcomes) == 31 - parts.loop.turns
    assert result.stopped in ("POSTS_EXHAUSTED", RCV.CODE_BUDGET_UNREACHABLE)


def test_recovery_whitelist_is_frozen_at_t0() -> None:
    assert P.R12_POLICY.recoverable_codes is P.RECOVERABLE_ERROR_CODES
    assert isinstance(P.R12_POLICY.recoverable_codes, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        P.R12_POLICY.recoverable_codes = ("NOVEL",)  # type: ignore[misc]
    # 运行中出现的白名单外错误码一律 terminal（不可临场扩表）
    decision = RCV.classify_failure(ev(code="NOVEL_CODE"))
    assert decision.kind == RCV.RECOVERY_TERMINAL
    assert decision.code == RCV.CODE_UNCLASSIFIED


def test_recovery_module_exposes_no_registration_entry() -> None:
    callables = [n for n in dir(RCV) if callable(getattr(RCV, n))
                 and "register" in n.lower()]
    assert callables == []


# ---------------------------------------------------------------------------
# AC22：安全/未知发送/冻结后改方法/来源漂移 → terminal
# ---------------------------------------------------------------------------

def _advance_to_freeze():
    return [
        ok("write_artifact", kind="design", source_sha256="d" * 64),
        ok("propose_plan", plan_sha256="p" * 64, revision=1),
        ok("run_batch", run_ids=["r1"]),
        ok("write_artifact", kind="analysis", source_sha256="a" * 64),
        ok("analyze", analysis={"n_runs": 1}),
        ok("freeze_confirm", confirm_token="t" * 8, manifest_sha256="m" * 64),
    ]


def test_path_escape_is_terminal() -> None:
    parts = make_parts([fail("write_artifact", "PATH_ESCAPE", effect="UNKNOWN")])
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 1  # 不重发、不重试
    assert result.terminal is True
    assert result.stopped == "PATH_ESCAPE"


def test_send_unknown_is_terminal() -> None:
    parts = make_parts([fail("run_batch", "SEND_UNKNOWN", effect="UNKNOWN")])
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 1
    assert result.terminal is True and result.stopped == "SEND_UNKNOWN"


def test_method_change_after_freeze_is_terminal() -> None:
    outcomes = _advance_to_freeze() + [
        ok("write_artifact", kind="design", source_sha256="x" * 64)]
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 7
    assert result.terminal is True
    assert result.stopped == RCV.CODE_METHOD_CHANGE
    # 冻结后改 design 不能把冻结洗掉重来
    assert result.stage == 6


def test_source_drift_after_freeze_is_terminal() -> None:
    outcomes = _advance_to_freeze() + [
        fail("write_artifact", "SOURCE_SHA_INVALID", effect="NONE")]
    # 宿主侧 confirm_lock 已落：冻结状态是**宿主证据**，不由本链会话自述
    parts = make_parts(outcomes, state=FakeState(confirm_lock={"token": "t"}))
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 7
    assert result.terminal is True
    assert result.stopped == "SOURCE_SHA_INVALID"
    assert result.actions[-1]["recovery"]["kind"] == RCV.RECOVERY_TERMINAL


def test_pre_freeze_code_before_freeze_is_recoverable() -> None:
    decision = RCV.classify_failure(ev(code="SOURCE_SHA_INVALID"))
    assert decision.kind == RCV.RECOVERY_RECOVERABLE
    frozen = RCV.classify_failure(ev(code="SOURCE_SHA_INVALID", frozen=True))
    assert frozen.kind == RCV.RECOVERY_TERMINAL


# ---------------------------------------------------------------------------
# AC23：传输失败（空回答/超时）→ terminal，且只发一次
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["EmptyResponseError", "TimeoutError",
                                  "GatewayError"])
def test_transport_failure_is_terminal_and_sent_once(code: str) -> None:
    parts = make_parts([fail("propose_plan", code, effect="UNKNOWN")])
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 1
    assert result.terminal is True
    assert result.stopped == RCV.CODE_TRANSPORT
    decision = RCV.classify_failure(ev(code="ANY", transport=True))
    assert decision.kind == RCV.RECOVERY_TERMINAL


# ---------------------------------------------------------------------------
# AC24：KNOWN_COMPLETE + 未闭 intent → 只允许幂等重发布，不重跑
# ---------------------------------------------------------------------------

def test_known_complete_with_open_intent_is_republish_only() -> None:
    state = FakeState(open_intent={"intent_id": "i1"})
    parts = make_parts([fail("run_confirm", "CONFIRM_BLOCKED",
                             effect=RCV.EFFECT_KNOWN_COMPLETE)], state=state)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 1  # 不重跑物理调用
    assert result.terminal is True
    assert result.stopped == RCV.CODE_REPUBLISH_ONLY
    assert result.actions[-1]["recovery"]["kind"] == RCV.RECOVERY_REPUBLISH_ONLY


def test_open_intent_without_known_result_is_terminal_unknown() -> None:
    state = FakeState(open_intent={"intent_id": "i1"})
    parts = make_parts([fail("run_confirm", "CONFIRM_BLOCKED",
                             effect=RCV.EFFECT_UNKNOWN)], state=state)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert parts.loop.turns == 1
    assert result.terminal is True
    assert result.stopped == RCV.CODE_UNKNOWN_EFFECT


# ---------------------------------------------------------------------------
# 判别细则：证据优先于错误码
# ---------------------------------------------------------------------------

def test_same_code_different_evidence() -> None:
    # 同一 RESOURCE_EXCEEDED：前执行（NONE）可恢复；已部分执行（UNKNOWN）终止
    assert RCV.classify_failure(ev()).recoverable is True
    assert RCV.classify_failure(
        ev(effect_status=RCV.EFFECT_UNKNOWN)).kind == RCV.RECOVERY_TERMINAL
    # 预算耗尽优先于白名单
    assert RCV.classify_failure(
        ev(budget_exhausted=True)).code == RCV.CODE_BUDGET_EXHAUSTED
    # 缺证据（空码）默认终止
    assert RCV.classify_failure(ev(code="")).kind == RCV.RECOVERY_TERMINAL
    # 未知异常类不当作可恢复
    assert RCV.classify_failure(
        ev(code="", error_class="KeyError")).kind == RCV.RECOVERY_TERMINAL


def test_unreadable_state_counts_as_missing_evidence() -> None:
    class Boom:
        def study_status(self, study: str):
            raise RuntimeError("state unavailable")

        def study_state(self, study: str) -> str:
            return "EXPLORING"

    parts = make_parts([fail("run_batch", "RESOURCE_EXCEEDED",
                             effect=RCV.EFFECT_UNKNOWN)], state=Boom())
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    # effect 不是 NONE 本身已终止；状态读失败也不会被当成 open_intent=False 放行
    assert result.terminal is True
    assert result.stopped == RCV.CODE_UNKNOWN_EFFECT


def test_same_bytes_rewrite_does_not_invalidate() -> None:
    state = RCV.MilestoneState(policy=POLICY)
    design = {"kind": "design", "source_sha256": "d" * 64}
    state.record_success(action="write_artifact", result=design, action_index=1)
    state.record_success(action="propose_plan",
                         result={"plan_sha256": "p" * 64, "revision": 1},
                         action_index=2)
    # 同字节重发 design：不洗掉已完成的 plan 里程碑
    state.record_success(action="write_artifact", result=dict(design),
                         action_index=3)
    assert state.cursor == 2
    assert state.invalidations == []
