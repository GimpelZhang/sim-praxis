"""RSI-R1 T5：人工时间与救援令记账（Plan §8.4，AC25）。

全部离线：临时 campaign 根 + 注入时钟，不触网、不启容器。
验收点逐条对应 AC25：begin/end 成对、未知不填 0、救援令独立且默认不成功、
运维不自动变研究救援。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import human_events as HE
from tests import rsi_r1_helpers as H


class Clock:
    def __init__(self, t: str = "2026-11-11T00:00:00Z") -> None:
        self.t = t
        self.calls = 0

    def set(self, t: str) -> "Clock":
        self.t = t
        return self

    def __call__(self) -> str:
        self.calls += 1
        return self.t


def _log(tmp_path: Path, clock: "Clock | None" = None) -> "tuple[HE.HumanEventLog, Path]":
    ctx = H.make_ctx(tmp_path)
    clock = clock or Clock()
    return HE.HumanEventLog(ctx, clock=clock), ctx.relative(HE.RELPATH)


# ---------------------------------------------------------------------------
# AC25-1：begin/end 成对，已知秒数如实累计
# ---------------------------------------------------------------------------

def test_begin_end_pair_records_known_seconds(tmp_path):
    clock = Clock("2026-11-11T01:00:00Z")
    log, path = _log(tmp_path, clock)
    ev = log.begin(kind="approval", actor="user", reason="批准 T1b-v2 重跑")
    assert ev.open and ev.duration_s() is None
    clock.set("2026-11-11T01:01:30Z")
    done = log.end(event_id=ev.event_id)
    assert not done.open
    assert done.duration_s() == 90.0
    s = log.summary()
    assert s["kinds"]["approval"] == {"count": 1, "known_seconds": 90.0,
                                      "unknown": 0}
    assert s["known_seconds_total"] == 90.0
    assert s["unknown_human_time"] is False
    assert s["complete"] is True
    log.assert_complete_for_closeout()  # 不抛
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600


def test_multiple_kinds_summed_separately(tmp_path):
    clock = Clock("2026-11-11T02:00:00Z")
    log, _ = _log(tmp_path, clock)
    a = log.begin(kind="approval", actor="user", reason="approve")
    clock.set("2026-11-11T02:00:30Z")
    log.end(event_id=a.event_id)
    b = log.begin(kind="maintenance", actor="ops", reason="重启看门狗")
    clock.set("2026-11-11T02:01:00Z")
    log.end(event_id=b.event_id)
    s = log.summary()
    assert s["kinds"]["approval"]["known_seconds"] == 30.0
    assert s["kinds"]["maintenance"]["known_seconds"] == 30.0
    assert s["known_seconds_total"] == 60.0
    assert s["n_events"] == 2


# ---------------------------------------------------------------------------
# AC25-2：未知不填 0；结案门拒绝
# ---------------------------------------------------------------------------

def test_open_event_is_unknown_and_never_zero_filled(tmp_path):
    clock = Clock("2026-11-11T03:00:00Z")
    log, _ = _log(tmp_path, clock)
    open_ev = log.begin(kind="scientific_review", actor="user",
                        reason="逐字复核证据包")
    closed = log.begin(kind="approval", actor="user", reason="批准")
    clock.set("2026-11-11T03:00:10Z")
    log.end(event_id=closed.event_id)

    s = log.summary()
    assert s["unknown_event_ids"] == [open_ev.event_id]
    assert s["unknown_human_time"] is True
    assert s["complete"] is False
    # 已知秒数只累计已结束事件；未知事件**不**按 0 计入「完整成本」。
    assert s["kinds"]["scientific_review"]["unknown"] == 1
    assert s["kinds"]["scientific_review"]["known_seconds"] == 0.0
    assert s["known_seconds_total"] == 10.0
    with pytest.raises(HE.HumanEventError) as ei:
        log.assert_complete_for_closeout()
    assert ei.value.code == "UNKNOWN_HUMAN_TIME"


def test_duration_of_open_event_is_none_not_zero(tmp_path):
    log, _ = _log(tmp_path)
    ev = log.begin(kind="approval", actor="user", reason="x")
    assert ev.duration_s() is None
    assert ev.as_dict()["duration_s"] is None
    assert ev.as_dict()["unknown_human_time"] is True


# ---------------------------------------------------------------------------
# 时间与 id 纪律
# ---------------------------------------------------------------------------

def test_end_before_begin_rejected(tmp_path):
    clock = Clock("2026-11-11T04:00:10Z")
    log, _ = _log(tmp_path, clock)
    ev = log.begin(kind="approval", actor="user", reason="x",
                   start_utc="2026-11-11T04:00:10Z")
    with pytest.raises(HE.HumanEventError) as ei:
        log.end(event_id=ev.event_id, end_utc="2026-11-11T04:00:09Z")
    assert ei.value.code == "TIME_REVERSED"


def test_double_end_rejected(tmp_path):
    log, _ = _log(tmp_path)
    ev = log.begin(kind="approval", actor="user", reason="x",
                   start_utc="2026-11-11T05:00:00Z")
    log.end(event_id=ev.event_id, end_utc="2026-11-11T05:00:01Z")
    with pytest.raises(HE.HumanEventError) as ei:
        log.end(event_id=ev.event_id, end_utc="2026-11-11T05:00:02Z")
    assert ei.value.code == "ALREADY_ENDED"


def test_non_z_timestamp_rejected(tmp_path):
    log, _ = _log(tmp_path)
    with pytest.raises(HE.HumanEventError) as ei:
        log.begin(kind="approval", actor="user", reason="x",
                  start_utc="2026-11-11T05:00:00+08:00")
    assert ei.value.code == "TIMESTAMP_INVALID"


def test_unknown_kind_and_touch_rejected(tmp_path):
    log, _ = _log(tmp_path)
    with pytest.raises(HE.HumanEventError) as ei:
        log.begin(kind="coffee", actor="user", reason="x")
    assert ei.value.code == "UNKNOWN_KIND"
    with pytest.raises(HE.HumanEventError) as ei:
        log.begin(kind="scientific_review", actor="user", reason="x",
                  touch="vibes")
    assert ei.value.code == "UNKNOWN_TOUCH"


def test_duplicate_event_id_rejected(tmp_path):
    log, _ = _log(tmp_path)
    log.begin(kind="approval", actor="user", reason="x", event_id="he-fixed")
    with pytest.raises(HE.HumanEventError) as ei:
        log.begin(kind="approval", actor="user", reason="y", event_id="he-fixed")
    assert ei.value.code == "DUPLICATE_EVENT_ID"


def test_artifact_refs_and_study_preserved(tmp_path):
    log, path = _log(tmp_path)
    ev = log.begin(kind="approval", actor="user", reason="Gate2",
                   artifact_refs=("contract/calibration_report_v2.json",
                                  "judge/report_A.json"),
                   study="A")
    assert ev.artifact_refs == ("contract/calibration_report_v2.json",
                                "judge/report_A.json")
    assert ev.study == "A"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])[
        "payload"]["artifact_refs"][0].startswith("contract/")


# ---------------------------------------------------------------------------
# AC25-3：救援令独立成行、默认 success=false，只有裁决能翻转
# ---------------------------------------------------------------------------

def test_rescue_order_is_separate_and_not_success(tmp_path):
    clock = Clock("2026-11-11T06:00:00Z")
    log, path = _log(tmp_path, clock)
    order = log.record_rescue_order(actor="user", reason="允许人工修 prompt",
                                    order_ref="rescue/order_0001.md",
                                    start_utc="2026-11-11T06:00:00Z",
                                    end_utc="2026-11-11T06:00:00Z",
                                    study="B")
    assert order.kind == "rescue"
    assert order.success is False and order.adjudicated is False
    s = log.summary()
    assert len(s["rescue_orders"]) == 1
    assert s["rescue_orders"][0]["success"] is False
    assert s["rescue_success_count"] == 0
    # 「独立成功 false」是**一行**事件（begin+end 两条记录），不是另一个事件。
    assert s["n_events"] == 1
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == [HE.EV_BEGIN, HE.EV_END]


def test_only_adjudication_flips_rescue_success(tmp_path):
    log, _ = _log(tmp_path)
    order = log.record_rescue_order(actor="user", reason="补一张图",
                                    order_ref="rescue/order_0002.md",
                                    start_utc="2026-11-11T07:00:00Z")
    with pytest.raises(HE.HumanEventError) as ei:
        log.adjudicate_rescue(event_id=order.event_id, success=True,
                             actor="", evidence_ref="x")
    assert ei.value.code == "MISSING_KEY"
    with pytest.raises(HE.HumanEventError) as ei:
        log.adjudicate_rescue(event_id=order.event_id, success=True,
                             actor="user", evidence_ref="  ")
    assert ei.value.code == "MISSING_KEY"
    flipped = log.adjudicate_rescue(
        event_id=order.event_id, success=True, actor="user",
        evidence_ref="human_events/support/order_0002_outcome.md")
    assert flipped.success is True and flipped.adjudicated is True
    assert log.summary()["rescue_success_count"] == 1
    with pytest.raises(HE.HumanEventError) as ei:
        log.adjudicate_rescue(event_id=order.event_id, success=False,
                             actor="user", evidence_ref="x")
    assert ei.value.code == "ALREADY_ADJUDICATED"


def test_adjudicate_non_rescue_rejected(tmp_path):
    log, _ = _log(tmp_path)
    ev = log.begin(kind="approval", actor="user", reason="x")
    with pytest.raises(HE.HumanEventError) as ei:
        log.adjudicate_rescue(event_id=ev.event_id, success=True,
                             actor="user", evidence_ref="x")
    assert ei.value.code == "NOT_A_RESCUE"


def test_rescue_requires_order_ref(tmp_path):
    log, _ = _log(tmp_path)
    with pytest.raises(HE.HumanEventError) as ei:
        log.record_rescue_order(actor="user", reason="x", order_ref="")
    assert ei.value.code == "MISSING_KEY"


# ---------------------------------------------------------------------------
# AC25-4：assisted 规则——运维不触发，关键解释/代码修订触发
# ---------------------------------------------------------------------------

class FakeState:
    def __init__(self) -> None:
        self.assisted: "list[tuple[str, str]]" = []

    def mark_assisted(self, study: str, *, reason: str) -> None:
        self.assisted.append((study, reason))


def test_maintenance_never_triggers_assisted(tmp_path):
    log, _ = _log(tmp_path)
    state = FakeState()
    ev = log.begin(kind="maintenance", actor="ops", reason="清容器",
                   study="A", touch="code")
    assert log.apply_assisted_rule(state, ev.event_id) is False
    assert state.assisted == []


def test_scientific_review_without_touch_does_not_trigger_assisted(tmp_path):
    log, _ = _log(tmp_path)
    state = FakeState()
    ev = log.begin(kind="scientific_review", actor="user", reason="读报告",
                   study="A")
    assert log.apply_assisted_rule(state, ev.event_id) is False
    assert state.assisted == []


@pytest.mark.parametrize("touch", HE.ASSISTED_TOUCHES)
def test_scientific_review_with_key_touch_triggers_assisted(tmp_path, touch):
    log, _ = _log(tmp_path)
    state = FakeState()
    ev = log.begin(kind="scientific_review", actor="user",
                   reason="手工修判据解释", study="B", touch=touch)
    assert log.apply_assisted_rule(state, ev.event_id) is True
    assert state.assisted and state.assisted[0][0] == "B"
    assert touch in state.assisted[0][1]


def test_assisted_requires_study(tmp_path):
    log, _ = _log(tmp_path)
    state = FakeState()
    ev = log.begin(kind="scientific_review", actor="user", reason="x",
                   touch="code")
    with pytest.raises(HE.HumanEventError) as ei:
        log.apply_assisted_rule(state, ev.event_id)
    assert ei.value.code == "MISSING_KEY"
    assert state.assisted == []


# ---------------------------------------------------------------------------
# 只追加：链完整、重读一致
# ---------------------------------------------------------------------------

def test_reload_reads_same_fold(tmp_path):
    clock = Clock("2026-11-11T08:00:00Z")
    ctx = H.make_ctx(tmp_path)
    log = HE.load(ctx, clock=clock)
    a = log.begin(kind="approval", actor="user", reason="x")
    clock.set("2026-11-11T08:00:05Z")
    log.end(event_id=a.event_id)
    again = HE.load(ctx, clock=clock)
    assert [e.event_id for e in again.events()] == [a.event_id]
    assert again.summary() == log.summary()
