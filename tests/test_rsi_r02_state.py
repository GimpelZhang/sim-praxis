"""RSI-R0.2 状态机测试（§10.2，AC10/AC15/AC31）：槽账、链式日志、恢复与暂停。"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    H.write_slots(c, ["E-dev-01", "E-dev-02", "E-dev-03"])
    return c


# ---- 槽状态机 ----


def test_slot_claim_and_settle(ctx):
    book = ST.SlotBook(ctx)
    assert book.status("E-dev-01") == ST.SLOT_UNUSED
    book.claim("E-dev-01", call_id="c1", trial_id="t1")
    assert book.status("E-dev-01") == ST.SLOT_SENT
    book.settle("E-dev-01", status=ST.SLOT_OK)
    assert book.status("E-dev-01") == ST.SLOT_OK
    with pytest.raises(ST.TransitionRefused):
        book.settle("E-dev-01", status=ST.SLOT_FAIL)   # 终态不得改写
    with pytest.raises(ST.TransitionRefused):
        book.claim("E-dev-01", call_id="c2", trial_id="t1")


def test_unknown_occupies_slot_and_is_never_recycled(ctx):
    book = ST.SlotBook(ctx)
    book.claim("E-dev-02", call_id="c", trial_id="t")
    book.settle("E-dev-02", status=ST.SLOT_UNKNOWN, reason="UNKNOWN_SENT")
    assert book.status("E-dev-02") == ST.SLOT_UNKNOWN
    cancelled = book.cancel_unused(reason="BLOCKED_BUDGET")
    assert "E-dev-02" not in cancelled
    assert set(cancelled) == {"E-dev-01", "E-dev-03"}
    counts = book.counts()
    assert counts[ST.SLOT_UNKNOWN] == 1 and counts[ST.SLOT_CANCELLED] == 2


def test_cancel_requires_reason(ctx):
    book = ST.SlotBook(ctx)
    with pytest.raises(ST.StateError):
        book.cancel("E-dev-01", reason="  ")


def test_slotbook_persists_across_instances(ctx):
    ST.SlotBook(ctx).claim("E-dev-03", call_id="c9", trial_id="t9")
    again = ST.SlotBook(ctx)
    assert again.status("E-dev-03") == ST.SLOT_SENT
    assert again.get("E-dev-03")["call_id"] == "c9"


def test_unknown_slot_and_missing_book(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    with pytest.raises(ST.StateError):
        ST.SlotBook(c)                                  # 槽账缺件
    H.write_slots(c, ["E-dev-01"])
    with pytest.raises(ST.StateError):
        ST.SlotBook(c).claim("NOPE-1", call_id="c", trial_id="t")


# ---- 链式日志 ----


def test_journal_chain_and_integrity(ctx):
    j = ST.Journal(ctx)
    e1 = j.append(kind="decision", trial_id="t1", action="check_input",
                  ok=True, payload={"a": 1})
    e2 = j.append(kind="intent", trial_id="t1", action="run_experiment",
                  slot_id="E-dev-01", payload={"params": {"force": 0.5}})
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e2["prev_sha256"] == e1["event_sha256"]
    assert ST.Journal(ctx).integrity() == []
    assert j.path.stat().st_mode & 0o777 == 0o600


def test_journal_tamper_is_detected(ctx):
    j = ST.Journal(ctx)
    j.append(kind="decision", trial_id="t1", action="a", ok=True)
    j.append(kind="decision", trial_id="t1", action="b", ok=True)
    lines = j.path.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[0])
    doc["message"] = "被改过"                                  # 改内容不动链
    lines[0] = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    j.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = ST.Journal(ctx).integrity()
    assert any("event_sha256" in p for p in problems)


def test_pending_intents_only_unclosed(ctx):
    j = ST.Journal(ctx)
    j.append(kind="intent", trial_id="t1", action="run_experiment",
             slot_id="E-dev-01")
    j.append(kind="result", trial_id="t1", action="run_experiment",
             slot_id="E-dev-01", ok=True)
    j.append(kind="intent", trial_id="t1", action="analyze", slot_id="E-dev-02")
    pend = j.pending_intents("t1")
    assert len(pend) == 1 and pend[0]["action"] == "analyze"
    assert ST.Journal(ctx).pending_intents("t2") == []


# ---- trial 状态与恢复 ----


def test_trial_state_roundtrip_and_terminal_lock(ctx):
    store = ST.StateStore(ctx)
    st = store.start(trial_id="t1", instance_id="E1", family="E", phase="dev",
                     slot_ids=["E-dev-01"], post_cap=1)
    assert st.status == ST.TRIAL_READY
    store.transition(st, status=ST.TRIAL_VALID_FAIL_CAP, code=ST.EV_VALID_FAIL_CAP)
    with pytest.raises(ST.TransitionRefused):
        store.transition(st, status=ST.TRIAL_SUBMITTED)
    loaded = ST.StateStore(ctx).load("t1")
    assert loaded.status == ST.TRIAL_VALID_FAIL_CAP
    assert loaded.ended_at_utc is not None
    with pytest.raises(ST.TransitionRefused):
        store.start(trial_id="t1", instance_id="E1", family="E", phase="dev",
                    slot_ids=["E-dev-01"], post_cap=1)


def test_resume_flags_unclosed_intent(ctx):
    store = ST.StateStore(ctx)
    j = ST.Journal(ctx)
    st = store.start(trial_id="t1", instance_id="E1", family="E", phase="dev",
                     slot_ids=["E-dev-01"], post_cap=1)
    store.transition(st, status=ST.TRIAL_RUNNING)
    j.append(kind="intent", trial_id="t1", action="run_experiment",
             slot_id="E-dev-01", payload={"params": {"force": 0.5}})
    out = store.resume(store.load("t1"), j)
    assert out.status == ST.TRIAL_TOOL_OUTCOME_UNKNOWN
    assert out.reason_code == ST.EV_TOOL_OUTCOME_UNKNOWN
    assert "不重跑" in (out.reason or "")
    # 再次恢复不改变结论（终态幂等）
    again = store.resume(store.load("t1"), j)
    assert again.status == ST.TRIAL_TOOL_OUTCOME_UNKNOWN


def test_resume_is_noop_without_pending(ctx):
    store = ST.StateStore(ctx)
    st = store.start(trial_id="t1", instance_id="E1", family="E", phase="dev",
                     slot_ids=[], post_cap=0)
    store.transition(st, status=ST.TRIAL_RUNNING)
    out = store.resume(store.load("t1"), ST.Journal(ctx))
    assert out.status == ST.TRIAL_RUNNING


def test_mark_assisted_sets_independent_false(ctx):
    store = ST.StateStore(ctx)
    j = ST.Journal(ctx)
    st = store.start(trial_id="t1", instance_id="E1", family="E", phase="dev",
                     slot_ids=[], post_cap=0)
    st = ST.mark_assisted(store, j, st, reason="维护者替换了候选分析源码")
    assert st.assisted is True and st.independent_success is False
    assert st.status == ST.TRIAL_ASSISTED
    ev = [e for e in j.events if e.get("code") == ST.EV_ASSISTED]
    assert ev and ev[0]["payload"]["independent_success"] is False


# ---- campaign 状态 ----


def test_campaign_pause_requires_resolution_to_clear(ctx):
    camp = ST.CampaignState(ctx)
    assert camp.status == ST.CAMP_NOT_STARTED and not camp.paused
    camp.start()
    camp.pause(code=ST.EV_UNKNOWN_SENT, reason="发送状态不明")
    assert camp.status == ST.CAMP_PAUSED
    assert "UNKNOWN_SENT" in (camp.pause_reason() or "")
    with pytest.raises(ST.StateError):
        camp.clear_pause(resolution="   ")
    camp.clear_pause(resolution="provider_log request_id=req-9")
    assert not ST.CampaignState(ctx).paused
    assert ST.CampaignState(ctx).doc["resolutions"][0]["resolution"].startswith(
        "provider_log")


def test_campaign_pause_writes_decision_event_before_state(ctx):
    """评审 M4：暂停必须先把决策事件落 journal，再改状态。

    结案的 ``codes`` 只数 journal 事件；暂停若不写事件，「运行时因污染暂停」
    在生产里永远不会被判成 CONTAMINATED（状态文件不进 codes）。顺序也必须
    是 journal 先行——写事件失败就不得声称已暂停（fail-closed）。
    """
    camp = ST.CampaignState(ctx)
    camp.start()
    camp.pause(code=ST.EV_CONTAMINATION, reason="fixture:污染暂停")
    j = ST.Journal(ctx)
    decisions = [e for e in j.events
                 if e.get("kind") == "decision" and e.get("action") == "pause"]
    assert len(decisions) == 1
    assert decisions[0]["code"] == ST.EV_CONTAMINATION
    assert decisions[0]["ok"] is False
    assert [e["code"] for e in decisions] == [ST.EV_CONTAMINATION]
    assert ST.CampaignState(ctx).paused
    with pytest.raises(ST.StateError):
        camp.pause(code=ST.EV_UNKNOWN_SENT, reason="   ")


def test_campaign_close_blocks_restart(ctx):
    camp = ST.CampaignState(ctx)
    camp.close()
    with pytest.raises(ST.TransitionRefused):
        ST.CampaignState(ctx).start()


# ---- §10.2 矩阵 ----


def test_disposition_matrix_covers_every_row():
    events = {d.event for d in ST.DISPOSITIONS}
    for evt in (ST.EV_APPROVAL_MISSING, ST.EV_SPEND_GUARANTEE_MISSING,
                ST.EV_ASSET_MISMATCH, ST.EV_PROTOCOL_FAIL, ST.EV_MALFORMED_ACTION,
                ST.EV_CALL_FAILED, ST.EV_INPUT_LIMIT, ST.EV_VALID_FAIL_CAP,
                ST.EV_POLICY_VIOLATION, ST.EV_CONTAMINATION, ST.EV_ASSISTED,
                ST.EV_UNKNOWN_SENT, ST.EV_COST_UNKNOWN,
                ST.EV_TOOL_OUTCOME_UNKNOWN, ST.EV_FREEZE_INVALID,
                ST.EV_BUDGET_HIT, ST.EV_ABORTED):
        assert evt in events
    d = ST.disposition_for(ST.EV_UNKNOWN_SENT)
    assert d.campaign_action == "PAUSE_GLOBAL" and d.verdict == "UNKNOWN_SENT"
    assert ST.disposition_for(ST.EV_ASSISTED).trial_action == "ASSISTED"


def test_unknown_event_refused():
    with pytest.raises(ST.StateError):
        ST.disposition_for("LOOKS_LIKE_A_NEW_CODE")
