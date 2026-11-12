"""RSI-R1.2 T2：单链 + 有界自修协议面（Plan §3.1；AC17/18/19/21/54）。

全部离线：用 ``tests/rsi_r12_chain_helpers`` 的脚本化假件驱动
``protocol.drive_chain(policy=R12_POLICY)``，不发任何 POST。
"""
from __future__ import annotations

import pytest

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import recovery as RCV
from tests.rsi_r12_chain_helpers import FakeState, fail, make_parts, ok

POLICY = P.PROFILE_R12_DEF.protocol_policy


# ---------------------------------------------------------------------------
# AC17：两 lane 各一条玩具链；帽 12/16；绝不要求恰好 8/16 呼
# ---------------------------------------------------------------------------

def test_r12_has_exactly_one_chain_per_lane() -> None:
    assert POLICY.mode == "bounded_recovery"
    assert POLICY.single_practice_chain is True
    assert PT.r12_studies(PT.PROBE, POLICY) == ("A",)
    assert PT.r12_studies(PT.PRACTICE, POLICY) == ("A",)
    assert P.PROFILE_R12_DEF.lane_post_caps == {
        "probe": 12, "practice": 16, "study_A": 48, "study_B": 48}


def test_legacy_policy_keeps_two_practice_chains() -> None:
    assert PT.r12_studies(PT.PRACTICE, P.LEGACY_POLICY) == ("A", "B")
    assert PT.r12_studies(PT.PROBE, P.LEGACY_POLICY) == ("A",)


def _clean_chain(**claim: object):
    return [
        ok("write_artifact", kind="design", source_sha256="d" * 64),
        ok("propose_plan", plan_sha256="p" * 64, revision=1),
        ok("run_batch", run_ids=["r1", "r2"]),
        ok("write_artifact", kind="analysis", source_sha256="a" * 64),
        ok("analyze", analysis={"n_runs": 2}, analysis_relpath="analysis.json"),
        ok("freeze_confirm", confirm_token="t" * 8, manifest_sha256="m" * 64),
        ok("run_confirm", result_sha256="c" * 64, n_runs=32),
        ok("submit_claim", judge_verdict="JUDGE_OK", verdict="VERIFIED",
           **claim),
    ]


@pytest.mark.parametrize("cap", [12, 16])
def test_clean_chain_completes_with_fewer_posts_than_cap(cap: int) -> None:
    parts = make_parts(_clean_chain())
    result = PT.drive_chain(parts, strict=False, cap=cap, policy=POLICY)
    assert result.completed is True and result.stopped == ""
    assert result.stage == 8
    assert len(result.actions) == 8  # 8 ≤ 帽，不要求恰好 12/16
    assert result.qualification is not None
    q = result.qualification
    assert q["schema"] == RCV.QUALIFICATION_SCHEMA
    assert q["budgeted_completion"] is True
    assert q["total_posts"] == 8 and q["posts_cap"] == cap
    assert q["first_attempt_ok_count"] == 8
    assert q["first_attempt_ok_all"] is True
    assert q["recoverable_failures"] == [] and q["first_error"] is None
    assert "小样本" in q["note"]


# ---------------------------------------------------------------------------
# AC18：11 POST 修复轨迹在 probe 12 / practice 16 两帽下都能闭合
# ---------------------------------------------------------------------------

def _repair_trajectory():
    """Plan §3.1 原轨迹：design→plan→run_batch 前执行拒绝→改 design→
    plan 修订→run_batch→analysis→analyze→freeze→run_confirm→submit＝11 POST。"""
    return [
        ok("write_artifact", kind="design", source_sha256="d1" * 32),
        ok("propose_plan", plan_sha256="p1" * 32, revision=1),
        fail("run_batch", "RESOURCE_EXCEEDED", effect="NONE"),
        ok("write_artifact", kind="design", source_sha256="d2" * 32),
        ok("propose_plan", plan_sha256="p2" * 32, revision=2),
        ok("run_batch", run_ids=["r1", "r2", "r3"]),
        ok("write_artifact", kind="analysis", source_sha256="a" * 64),
        ok("analyze", analysis={"n_runs": 3},
           analysis_relpath="analysis.json"),
        ok("freeze_confirm", confirm_token="t" * 8, manifest_sha256="m" * 64),
        ok("run_confirm", result_sha256="c" * 64, n_runs=32),
        ok("submit_claim", judge_verdict="JUDGE_OK", verdict="VERIFIED"),
    ]


@pytest.mark.parametrize("cap", [12, 16])
def test_eleven_post_repair_trajectory_closes_within_cap(cap: int) -> None:
    parts = make_parts(_repair_trajectory())
    result = PT.drive_chain(parts, strict=False, cap=cap, policy=POLICY)
    assert result.completed is True, result.stopped
    assert result.stopped == "" and result.terminal is False
    assert len(result.actions) == 11 and result.ok_count == 10
    assert parts.loop.turns == 11
    q = result.qualification
    assert q["total_posts"] == 11 and q["budgeted_completion"] is True
    # 首次尝试记录第一错，不被后续修复抹掉
    assert q["first_error"]["position"] == 2
    assert q["first_error"]["code"] == "RESOURCE_EXCEEDED"
    assert q["first_error"]["action_index"] == 3
    row = q["positions"][2]
    assert row["first_attempt_ok"] is False
    assert row["first_code"] == "RESOURCE_EXCEEDED"
    assert q["first_attempt_ok_count"] == 7
    assert q["first_attempt_ok_all"] is False
    assert len(q["recoverable_failures"]) == 1
    # 改 design 让 plan 及其下游失效：旧 raw 归档、不复用为新计划证据
    assert len(q["invalidations"]) == 1
    inv = q["invalidations"][0]
    assert inv["position"] == 0 and inv["from"] == 1 and inv["dropped"] == [1]
    # 最终里程碑仍是八位置全齐（重建后）
    assert [m["position"] for m in q["milestones"]] == list(range(8))
    assert q["milestones"][1]["consumed"]["plan_sha256"] == "p2" * 32


# ---------------------------------------------------------------------------
# AC19：凑满 8 个 ok 但缺 submit_claim，也算没走完
# ---------------------------------------------------------------------------

def test_eight_ok_without_submit_claim_is_incomplete() -> None:
    outcomes = _clean_chain()[:-1] + [ok("run_batch", run_ids=["r9"])]
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=8, policy=POLICY)
    assert result.ok_count == 8
    assert result.completed is False
    assert result.stopped == "POSTS_EXHAUSTED"
    assert result.stage == 7  # 只有 submit_claim 没走
    assert result.qualification["positions"][7]["first_attempt_ok"] is None
    assert result.qualification["budgeted_completion"] is False


# ---------------------------------------------------------------------------
# AC21：首次尝试与最终完成两面分开报
# ---------------------------------------------------------------------------

def test_first_attempt_failure_does_not_erase_final_completion() -> None:
    outcomes = [fail("write_artifact", "MALFORMED_JSON", effect="NONE")] \
        + _clean_chain()
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=12, policy=POLICY)
    assert result.completed is True and result.stopped == ""
    q = result.qualification
    assert q["positions"][0]["first_attempt_ok"] is False
    assert q["positions"][0]["first_code"] == "MALFORMED_JSON"
    assert q["first_attempt_ok_count"] == 7
    assert q["attempted_positions"] == 8
    assert q["budgeted_completion"] is True
    assert q["total_posts"] == 9
    assert "不包装成稳定成功率" in q["note"]


# ---------------------------------------------------------------------------
# 旧路径逐字节不变（不传 policy 时无 qualification 键）
# ---------------------------------------------------------------------------

def test_legacy_drive_chain_has_no_qualification() -> None:
    parts = make_parts(_clean_chain())
    result = PT.drive_chain(parts, strict=False, cap=16)
    assert result.completed is True
    assert result.qualification is None
    assert "qualification" not in result.as_dict()


# ---------------------------------------------------------------------------
# AC54：剩余呼数不足闭合依赖 → 发送前 BUDGET_UNREACHABLE，不发明调用
# ---------------------------------------------------------------------------

def test_budget_unreachable_stops_before_sending() -> None:
    # 帽 5 < 八位置闭合依赖 8：第一呼之前就停，不发明不可能闭合的调用
    parts = make_parts(_clean_chain())
    result = PT.drive_chain(parts, strict=False, cap=5, policy=POLICY)
    assert parts.loop.turns == 0
    assert result.stopped == RCV.CODE_BUDGET_UNREACHABLE
    assert result.completed is False and result.stage == 0
    q = result.qualification
    assert q["stop_note"] and "不发明知无法闭合的调用" in q["stop_note"]
    assert q["budgeted_completion"] is False


def test_budget_rechecked_after_each_recoverable_failure() -> None:
    # 帽 8 恰好够八位置；一次可恢复失败烧掉 1 呼后剩 7 < 8 → 立即停在发送前
    outcomes = [fail("write_artifact", "RESOURCE_EXCEEDED", effect="NONE")] \
        + _clean_chain()
    parts = make_parts(outcomes)
    result = PT.drive_chain(parts, strict=False, cap=8, policy=POLICY)
    assert parts.loop.turns == 1  # 只发了那一呼失败的
    assert result.stopped == RCV.CODE_BUDGET_UNREACHABLE
    assert result.stage == 0
    assert result.qualification["stop_note"]


# ---------------------------------------------------------------------------
# R1.2 无 repair_reserve：付费后宿主缺陷只能整轮停
# ---------------------------------------------------------------------------

def test_run_protocol_bounded_refuses_repair_lane() -> None:
    with pytest.raises(PT.ProtocolError) as exc:
        PT.run_protocol(None, lane=PT.PROBE, spend=None,  # type: ignore[arg-type]
                        transport_factory=lambda: None, runner=None,
                        image="img", batch_id="b1", counter=lambda lane: 0,
                        billing_lane="repair_reserve",
                        profile=P.PROFILE_R12_DEF)
    assert exc.value.code == "REPAIR_NOT_AVAILABLE"
    assert "无 repair_reserve" in str(exc.value)
