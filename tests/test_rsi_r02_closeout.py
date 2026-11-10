"""RSI-R0.2 结案事实与三轴测试（§10.1，AC19/AC31/AC35）。

关键断言：事实只从槽账/状态/journal/预留重算；缺件如实 missing；三轴互不吞并；
能力轴按固定优先级，且 ASSISTED/INVALID 不会被「成功」字样洗白。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import closeout as CO
from orchestrator.rsi_r02 import stages as SG
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    H.write_trial_slots(c, (*SG.dev_specs(), *SG.validation_specs()))
    return c


def _finish(store, spec, *, status=ST.TRIAL_SUBMITTED, success=True,
            assisted=False):
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase,
                     slot_ids=[], post_cap=spec.post_cap)
    st.independent_success = success
    st.assisted = assisted
    code = {ST.TRIAL_SUBMITTED: "SUBMITTED",
            ST.TRIAL_VALID_FAIL_CAP: ST.EV_VALID_FAIL_CAP,
            ST.TRIAL_INVALID: ST.EV_ASSET_MISMATCH}.get(status, "SUBMITTED")
    store.transition(st, status=status, code=code)
    return st


def _all_pass(ctx):
    store = ST.StateStore(ctx)
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        _finish(store, spec)


# ---- 空 campaign ----


def test_empty_campaign_is_honestly_incomplete(ctx):
    facts = CO.read_facts(ctx)
    assert facts["slots"]["missing"] is False
    verdict = CO.judge(facts)
    assert verdict["process"]["status"] == CO.PROCESS_COMPLETE   # 结构完好
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE
    assert "NO_TRIAL_STARTED" in verdict["measurement"]["reasons"]
    assert verdict["capability"]["status"] == CO.CAP_NOT_MEASURED


def test_missing_slot_book_is_closeout_incomplete(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    facts = CO.read_facts(c)
    assert facts["slots"]["missing"] is True
    verdict = CO.judge(facts)
    assert verdict["process"]["status"] == CO.PROCESS_INCOMPLETE
    assert "SLOT_BOOK_MISSING" in verdict["process"]["reasons"]


# ---- 能力轴优先级 ----


def test_full_success_is_ceiling(ctx):
    _all_pass(ctx)
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["process"]["status"] == CO.PROCESS_COMPLETE
    assert verdict["measurement"]["status"] == CO.MEAS_VALID
    cap = verdict["capability"]
    assert cap["status"] == CO.CAP_CEILING
    assert cap["success_counts"] == {"R": 2, "E": 2, "D": 2}
    assert cap["gv"]["passed"] is True


def test_dev_partial_is_development_not_ready(ctx):
    store = ST.StateStore(ctx)
    specs = SG.dev_specs()
    _finish(store, specs[0])
    _finish(store, specs[1], status=ST.TRIAL_VALID_FAIL_CAP, success=False)
    _finish(store, specs[2])
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["capability"]["status"] == CO.CAP_DEV_NOT_READY


def test_assisted_dev_breaks_gate_even_if_success_flag_set(ctx):
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        _finish(store, spec, assisted=(spec.family == "E"))
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["capability"]["status"] == CO.CAP_DEV_NOT_READY


def test_partial_validation_is_inconclusive(ctx):
    """两条验证判 INVALID ⇒ 有效独立结果只有 4/6，能力只能 INCONCLUSIVE。"""
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        _finish(store, spec)
    for i, spec in enumerate(SG.validation_specs()):
        status = ST.TRIAL_INVALID if i < 2 else ST.TRIAL_SUBMITTED
        _finish(store, spec, status=status)
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["measurement"]["status"] == CO.MEAS_INVALID_INSTRUMENT
    assert verdict["capability"]["status"] == CO.CAP_INCONCLUSIVE


def test_validation_valid_fail_counts_as_coverage(ctx):
    """VALID_FAIL_CAP 是有效的独立结果：计入覆盖，只是不算成功。"""
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        _finish(store, spec)
    for spec in SG.validation_specs():
        status = (ST.TRIAL_VALID_FAIL_CAP if spec.trial_id == "R-val-2"
                  else ST.TRIAL_SUBMITTED)
        _finish(store, spec, status=status,
                success=status == ST.TRIAL_SUBMITTED)
    cap = CO.judge(CO.read_facts(ctx))["capability"]
    assert cap["status"] == CO.CAP_PILOT
    assert cap["success_counts"] == {"R": 1, "E": 2, "D": 2}


def test_research_floor_when_r_zero(ctx):
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        _finish(store, spec)
    for spec in SG.validation_specs():
        status = (ST.TRIAL_VALID_FAIL_CAP if spec.family == "R"
                  else ST.TRIAL_SUBMITTED)
        _finish(store, spec, status=status,
                success=status == ST.TRIAL_SUBMITTED)
    cap = CO.judge(CO.read_facts(ctx))["capability"]
    assert cap["status"] == CO.CAP_RESEARCH_FLOOR
    assert cap["success_counts"]["R"] == 0


def test_engineering_floor_when_e_zero(ctx):
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        _finish(store, spec)
    for spec in SG.validation_specs():
        status = (ST.TRIAL_VALID_FAIL_CAP if spec.family == "E"
                  else ST.TRIAL_SUBMITTED)
        _finish(store, spec, status=status,
                success=status == ST.TRIAL_SUBMITTED)
    cap = CO.judge(CO.read_facts(ctx))["capability"]
    assert cap["status"] == CO.CAP_ENGINEERING_FLOOR


def test_assisted_validation_is_not_independent_coverage(ctx):
    _all_pass(ctx)
    store = ST.StateStore(ctx)
    spec = SG.validation_specs()[0]
    st = store.load(spec.trial_id)
    ST.mark_assisted(store, ST.Journal(ctx), st, reason="维护者替换分析源码")
    cap = CO.judge(CO.read_facts(ctx))["capability"]
    assert cap["status"] == CO.CAP_INCONCLUSIVE


# ---- 测量与过程轴 ----


def test_contamination_is_measurement_contaminated(ctx):
    _all_pass(ctx)
    ST.Journal(ctx).append(kind="decision", trial_id="E-dev", action="policy",
                           ok=False, code=ST.EV_CONTAMINATION,
                           message="越权读取真值")
    assert CO.judge(CO.read_facts(ctx))["measurement"]["status"] == \
        CO.MEAS_CONTAMINATED


def test_contamination_outranks_invalid_trial(ctx):
    """§10.2 行9：污染行的 trial 处置就是 INVALID；测量轴必须判 CONTAMINATED。

    评审 H3 复现：仪器检查在前时，污染永远被判成 INVALID_INSTRUMENT，安全结论
    被仪器结论吞掉。
    """
    store = ST.StateStore(ctx)
    spec = SG.dev_specs()[0]
    _finish(store, spec, status=ST.TRIAL_INVALID, success=False)
    ST.Journal(ctx).append(kind="decision", trial_id=spec.trial_id,
                           action="analyze", ok=False,
                           code=ST.EV_CONTAMINATION, message="越权读取真值")
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["measurement"]["status"] == CO.MEAS_CONTAMINATED
    assert "CONTAMINATION" in verdict["measurement"]["reasons"]


def test_freeze_invalid_event_invalidates_instrument(ctx):
    """§10「LOCK 语义改变 ⇒ FREEZE_INVALID」：无 INVALID trial 也判仪器失效。"""
    _all_pass(ctx)
    ST.Journal(ctx).append(kind="decision", trial_id="E-dev", action="freeze",
                           ok=False, code=ST.EV_FREEZE_INVALID,
                           message="LOCK 语义改变")
    facts = CO.read_facts(ctx)
    verdict = CO.judge(facts)
    assert facts["journal"]["codes"]["freeze_invalid"] == 1
    assert verdict["measurement"]["status"] == CO.MEAS_INVALID_INSTRUMENT
    assert "FREEZE_INVALID" in verdict["measurement"]["reasons"]


def test_budget_hit_records_cancelled_slots_without_faking_adds(ctx):
    """触帽是合同内结局：事件计数 + 取消槽如实记录，process 不因此伪装失败。"""
    _all_pass(ctx)
    ST.Journal(ctx).append(kind="decision", trial_id="campaign", action="budget",
                           ok=False, code=ST.EV_BUDGET_HIT, message="触帽")
    book = ST.SlotBook(ctx)
    book.cancel("E-dev-24", reason="BLOCKED_BUDGET")
    facts = CO.read_facts(ctx)
    verdict = CO.judge(facts)
    assert facts["journal"]["codes"]["budget_hit"] == 1
    assert facts["slots_cancelled"]["slot_ids"] == ["E-dev-24"]
    assert verdict["process"]["status"] == CO.PROCESS_COMPLETE
    assert verdict["measurement"]["status"] == CO.MEAS_VALID


def test_unknown_cost_keeps_process_incomplete(ctx):
    _all_pass(ctx)
    book = B.BillingBook(ctx)
    book.reserve(call_id="c1", slot_id="E-dev-01")
    book.mark_unknown(call_id="c1", failure="连接中断")
    facts = CO.read_facts(ctx)
    verdict = CO.judge(facts)
    assert facts["billing"]["unknown_call_ids"] == ["c1"]
    assert verdict["process"]["status"] == CO.PROCESS_INCOMPLETE
    assert "UNSETTLED_COST" in verdict["process"]["reasons"]
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE


def test_pending_intent_blocks_closeout(ctx):
    _all_pass(ctx)
    ST.Journal(ctx).append(kind="intent", trial_id="E-dev",
                           action="run_experiment", slot_id="E-dev-01")
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["process"]["status"] == CO.PROCESS_INCOMPLETE
    assert "PENDING_INTENTS" in verdict["process"]["reasons"]


def test_adjudicated_intent_blocks_measurement_not_process(ctx):
    """已被 trial 终态裁决的 intent 不算开放项；但测量仍然缺证据。"""
    store = ST.StateStore(ctx)
    spec = SG.dev_specs()[0]
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase, slot_ids=[],
                     post_cap=spec.post_cap)
    store.transition(st, status=ST.TRIAL_TOOL_OUTCOME_UNKNOWN,
                     code=ST.EV_TOOL_OUTCOME_UNKNOWN)
    ST.Journal(ctx).append(kind="intent", trial_id=spec.trial_id,
                           action="run_experiment", slot_id="E-dev-01")
    facts = CO.read_facts(ctx)
    verdict = CO.judge(facts)
    assert facts["journal"]["pending_intents"] == []
    assert len(facts["journal"]["adjudicated_intents"]) == 1
    assert verdict["process"]["status"] == CO.PROCESS_COMPLETE
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE
    assert "TOOL_OUTCOME_UNKNOWN" in verdict["measurement"]["reasons"]


def test_paused_campaign_blocks_closeout(ctx):
    _all_pass(ctx)
    ST.CampaignState(ctx).pause(code=ST.EV_UNKNOWN_SENT, reason="发送不明")
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["process"]["status"] == CO.PROCESS_INCOMPLETE
    assert "CAMPAIGN_PAUSED" in verdict["process"]["reasons"]


def test_three_axes_are_independent(ctx):
    """能力可以「很好」而测量/过程仍不合格——不得互相吞并。"""
    _all_pass(ctx)
    ST.Journal(ctx).append(kind="intent", trial_id="E-dev",
                           action="analyze", slot_id="E-dev-02")
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["capability"]["status"] == CO.CAP_CEILING
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE
    assert verdict["process"]["status"] == CO.PROCESS_INCOMPLETE


# ---- 落盘 ----


def test_write_facts_writes_private_files(ctx):
    _all_pass(ctx)
    out = CO.write_facts(ctx)
    for name in ("facts.json", "judgement.json"):
        path = ctx.root / "closeout" / name
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
        json.loads(path.read_text(encoding="utf-8"))
    assert out["judgement"]["capability"]["status"] == CO.CAP_CEILING


# ---- D-T4-3：协议 trial 必须在事实与三轴里可见 ---------------------------

def test_facts_include_protocol_trials_but_not_in_gv(ctx):
    from orchestrator.rsi_r02 import closeout as CO2

    _all_pass(ctx)
    facts = CO2.read_facts(ctx)
    proto = [t for t in facts["trials"] if t["phase"] == "protocol"]
    assert {t["trial_id"] for t in proto} == {"proto_P1", "proto_P2"}
    assert all(t["started"] is False for t in proto)   # 本用例没跑协议链
    # 协议行不得影响 G-V：dev 三族仍各自命中自己的 trial
    gate = CO2.dev_gate(facts)
    assert gate["passed"] is True
    verdict = CO2.judge(facts)
    assert verdict["capability"]["status"] == CO2.CAP_CEILING


# ---- 协议轴（§10.2：协议 FAIL ⇒ BLOCKED_PROTOCOL + NOT_MEASURED） ----------


def test_protocol_axis_not_started_by_default(ctx):
    assert CO.judge(CO.read_facts(ctx))["protocol"]["status"] == \
        CO.PROTOCOL_NOT_STARTED


def test_protocol_fail_is_labelled_blocked_protocol(ctx):
    """T4 判负后判决件必须自带 BLOCKED_PROTOCOL 标签。

    以前这一轴没有机判出口：process=COMPLETE、measurement=VALID，只剩 capability
    一句话的解释，计划 §10.2 要求的标签在判决件里根本不存在。协议链判负必须由
    机器可见的状态承载，不能靠人读理由。
    """
    _all_pass(ctx)
    store = ST.StateStore(ctx)
    spec = {s.trial_id: s for s in CO.protocol_specs()}["proto_P1"]
    _finish(store, spec, status=ST.TRIAL_VALID_FAIL_CAP, success=False)
    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["protocol"]["status"] == CO.PROTOCOL_BLOCKED
    assert verdict["protocol"]["failed"] == ["proto_P1"]
    # §10.2：协议 FAIL 不许被读成「测量有效」
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE
    assert "PROTOCOL_BLOCKED" in verdict["measurement"]["reasons"]
    # 判负的协议链不掩盖已跑通的六格（三轴互不吞并）
    assert verdict["capability"]["status"] == CO.CAP_CEILING  # 本用例已把六格跑通


def test_protocol_pass_and_partial_are_distinguished(ctx):
    _all_pass(ctx)
    store = ST.StateStore(ctx)
    specs = {s.trial_id: s for s in CO.protocol_specs()}
    _finish(store, specs["proto_P1"])
    assert CO.judge(CO.read_facts(ctx))["protocol"]["status"] == \
        CO.PROTOCOL_PARTIAL
    _finish(store, specs["proto_P2"])
    assert CO.judge(CO.read_facts(ctx))["protocol"]["status"] == CO.PROTOCOL_OK
