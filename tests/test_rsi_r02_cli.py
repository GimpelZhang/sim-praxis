"""RSI-R0.2 入口测试（§6.2/§6.3/§10.2，AC10/AC12/AC15/AC23）。

入口只做三件事：固定顺序、显式授权、恢复不重跑。测试全部注入假模型，
断言「拒绝发生在零 POST 之前」与「停因不被下一 trial 越过去」。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r02 import cli as CI
from orchestrator.rsi_r02 import closeout as CO
from orchestrator.rsi_r02 import stages as SG
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H

SUBMIT = H.action_json(action="submit", claim="SUPPORTED", run_ids=["run-1"],
                       analysis_ids=["a-2"], mean_effect=1.0, z=2.0)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    return H.make_campaign(tmp_path)


def _factory(*_a, **_k):
    return H.FakeTrial()


def _prepare(ctx, specs):
    H.write_trial_slots(ctx, specs)
    for spec in specs:
        H.write_public_task(ctx, spec.instance_id, family=spec.family)


def _dev_pass(ctx):
    store = ST.StateStore(ctx)
    for spec in SG.dev_specs():
        st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                         family=spec.family, phase=spec.phase,
                         slot_ids=[], post_cap=spec.post_cap)
        st.independent_success = True
        store.transition(st, status=ST.TRIAL_SUBMITTED, code="SUBMITTED")
    return store


# ---- 单 trial ----


def test_run_trial_submits_and_starts_campaign(ctx):
    _prepare(ctx, SG.dev_specs()[:1])
    model = H.FakeModel([SUBMIT])
    out = CI.run_trial(ctx, SG.dev_specs()[0], call_model=model,
                       counter=lambda t: 100, trial=H.FakeTrial())
    assert out["status"] == ST.TRIAL_SUBMITTED
    assert out["independent_success"] is True
    assert ST.CampaignState(ctx).status == ST.CAMP_RUNNING


def test_run_trial_public_doc_override_reaches_the_model(ctx):
    """T4 协议 fixture 的公开文档必须**真的**进系统提示（锁内适配 D-T3-13）。

    协议链要求模型看到 nonce 与四动作合同；缺省路径读的是实例自带文档，
    盲跑等于让候选凭猜答协议。这里钉死「显式传入即逐字进系统提示」，
    同时不改变缺省行为。
    """
    from orchestrator.rsi_r02 import protocol_fixture as PF

    _prepare(ctx, SG.dev_specs()[:2])
    fx = PF.make_fixture(campaign_id=ctx.campaign_id, fixture_id="P1",
                         slot_id="E-dev-01")
    model = H.FakeModel([SUBMIT])
    CI.run_trial(ctx, SG.dev_specs()[0], call_model=model, counter=lambda t: 100,
                 trial=H.FakeTrial(), public_doc=fx.task_doc())
    system = model.requests[0].system
    assert fx.nonce in system
    assert "protocol" in system

    model2 = H.FakeModel([SUBMIT])
    CI.run_trial(ctx, SG.dev_specs()[1], call_model=model2, counter=lambda t: 100,
                 trial=H.FakeTrial())
    assert fx.nonce not in model2.requests[0].system


def test_run_trial_refuses_paused_campaign_before_post(ctx):
    _prepare(ctx, SG.dev_specs()[:1])
    ST.CampaignState(ctx).pause(code=ST.EV_UNKNOWN_SENT, reason="发送不明")
    model = H.FakeModel([SUBMIT])
    with pytest.raises(CI.CliError):
        CI.run_trial(ctx, SG.dev_specs()[0], call_model=model,
                     counter=lambda t: 100, trial=H.FakeTrial())
    assert model.requests == []


def test_zero_post_reentry_runs_same_trial(ctx):
    _prepare(ctx, SG.dev_specs()[:1])
    spec = SG.dev_specs()[0]
    ST.StateStore(ctx).start(trial_id=spec.trial_id,
                             instance_id=spec.instance_id, family=spec.family,
                             phase=spec.phase, slot_ids=[], post_cap=24)
    model = H.FakeModel([SUBMIT])
    out = CI.run_trial(ctx, spec, call_model=model, counter=lambda t: 100,
                       trial=H.FakeTrial())
    assert out["status"] == ST.TRIAL_SUBMITTED
    assert len(model.requests) == 1


def test_unrecoverable_interruption_aborts_without_rerun(ctx):
    _prepare(ctx, SG.dev_specs())
    spec = SG.dev_specs()[0]
    store = ST.StateStore(ctx)
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase, slot_ids=[],
                     post_cap=24)
    st.posts_sent = 1
    store.transition(st, status=ST.TRIAL_RUNNING)
    ST.Journal(ctx).append(kind="decision", trial_id=spec.trial_id,
                           action="check_input", ok=True)
    model = H.FakeModel([SUBMIT])
    out = CI.run_trial(ctx, spec, call_model=model, counter=lambda t: 100,
                       trial=H.FakeTrial())
    assert out["outcome"] == "aborted_unrecoverable"
    assert out["status"] == ST.TRIAL_ABORTED
    assert model.requests == []
    assert ST.SlotBook(ctx).status("E-dev-01") == ST.SLOT_CANCELLED


def test_terminal_trial_is_never_rerun(ctx):
    _prepare(ctx, SG.dev_specs()[:1])
    spec = SG.dev_specs()[0]
    store = ST.StateStore(ctx)
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase, slot_ids=[],
                     post_cap=24)
    store.transition(st, status=ST.TRIAL_VALID_FAIL_CAP,
                     code=ST.EV_VALID_FAIL_CAP)
    model = H.FakeModel([SUBMIT])
    out = CI.run_trial(ctx, spec, call_model=model, counter=lambda t: 100,
                       trial=H.FakeTrial())
    assert out["outcome"] == "already_terminal"
    assert model.requests == []


# ---- 阶段调度 ----


def test_dev_phase_runs_fixed_order(ctx):
    _prepare(ctx, SG.dev_specs())
    model = H.FakeModel([SUBMIT, SUBMIT, SUBMIT])
    out = CI.run_phase(ctx, "dev", call_model=model, counter=lambda t: 100,
                       trial_factory=_factory)
    assert out["completed"] is True and out["stop_reason"] is None
    assert [r["trial_id"] for r in out["results"]] == \
        ["E-dev", "D-dev", "R-dev"]
    assert all(r["status"] == ST.TRIAL_SUBMITTED for r in out["results"])


def test_unknown_send_stops_phase_and_leaves_rest_untouched(ctx):
    _prepare(ctx, SG.dev_specs())
    model = H.FakeModel([TimeoutError("连接中断"), SUBMIT])
    out = CI.run_phase(ctx, "dev", call_model=model, counter=lambda t: 100,
                       trial_factory=_factory)
    assert out["completed"] is False
    assert len(out["results"]) == 1
    assert out["results"][0]["status"] == ST.TRIAL_UNKNOWN_SENT
    assert SG.trial_status(ST.StateStore(ctx), SG.dev_specs()[1]) is None
    assert len(model.requests) == 1


def test_phase_prechecks_all_slot_books_before_any_post(ctx):
    H.write_trial_slots(ctx, SG.dev_specs()[:1])   # D/R 缺槽
    H.write_public_task(ctx, "E-dev", family="E")
    model = H.FakeModel([SUBMIT, SUBMIT, SUBMIT])
    with pytest.raises(SG.StageError):
        CI.run_phase(ctx, "dev", call_model=model, counter=lambda t: 100,
                     trial_factory=_factory)
    assert model.requests == []


def test_validation_refused_without_gv(ctx):
    _prepare(ctx, SG.validation_specs())
    model = H.FakeModel([SUBMIT])
    with pytest.raises(CI.CliError):
        CI.run_phase(ctx, "validation", call_model=model,
                     counter=lambda t: 100, trial_factory=_factory)
    assert model.requests == []


def test_validation_runs_fixed_six_after_gv(ctx):
    _dev_pass(ctx)
    _prepare(ctx, SG.validation_specs())
    model = H.FakeModel([SUBMIT] * 6)
    out = CI.run_phase(ctx, "validation", call_model=model,
                       counter=lambda t: 100, trial_factory=_factory)
    assert out["completed"] is True
    assert [r["trial_id"] for r in out["results"]] == list(SG.VALIDATION_ORDER)


# ---- 零 POST 命令 ----


def test_resume_flags_pending_intent(ctx):
    H.write_trial_slots(ctx, SG.dev_specs()[:1])
    spec = SG.dev_specs()[0]
    store = ST.StateStore(ctx)
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase, slot_ids=[],
                     post_cap=24)
    store.transition(st, status=ST.TRIAL_RUNNING)
    ST.Journal(ctx).append(kind="intent", trial_id=spec.trial_id,
                           action="run_experiment", slot_id="E-dev-01")
    out = CI.resume_all(ctx)
    # intent 仍作为原始证据留在 journal 里（不伪造 result），只是 trial 已裁决
    assert out["pending_intents"] == 1
    row = [r for r in out["resumed"] if r["trial_id"] == spec.trial_id][0]
    assert row["after"] == ST.TRIAL_TOOL_OUTCOME_UNKNOWN


def test_clear_pause_requires_resolution(ctx):
    ST.CampaignState(ctx).pause(code=ST.EV_UNKNOWN_SENT, reason="发送不明")
    with pytest.raises(ST.StateError):
        CI.clear_pause(ctx, "   ")
    out = CI.clear_pause(ctx, "provider_log request_id=req-9")
    assert out["cleared"] is True
    assert not ST.CampaignState(ctx).paused


def test_status_reads_without_writing(ctx):
    out = CI.status(ctx)
    assert len(out["trials"]) == 9
    assert out["campaign_status"] == ST.CAMP_NOT_STARTED
    assert out["journal_integrity"] == []


# ---- 授权闸口 ----


def test_gp_approval_missing_refuses(ctx):
    with pytest.raises(CI.CliError):
        CI.require_gp_approval(ctx, posts=72)


def test_gp_approval_requires_enough_posts_and_cap(ctx):
    ctx.write_json("quota/approvals/p26.json",
                   {"gate": "G-P", "authorized_posts": 8, "cap_usd": 100.0})
    with pytest.raises(CI.CliError):
        CI.require_gp_approval(ctx, posts=72)
    ctx.write_json("quota/approvals/p26b.json",
                   {"gate": "G-P", "authorized_posts": 72, "cap_usd": 100.0})
    doc = CI.require_gp_approval(ctx, posts=72)
    assert doc["authorized_posts"] == 72


def test_make_paid_caller_requires_explicit_batch_id():
    """批次归属没有缺省值：省略即 TypeError，空串即 CliError（都在触网/读分词器之前）。"""
    with pytest.raises(TypeError):
        CI.make_paid_caller(tokenizer_path="x", tokenizer_sha256="y")
    with pytest.raises(CI.CliError):
        CI.make_paid_caller(tokenizer_path="x", tokenizer_sha256="y",
                            batch_id="   ")


def test_cli_run_without_execute_paid_is_refused(ctx, capsys):
    rc = CI.main(["--campaign-id", "r02_test", "run", "--phase", "dev"])
    assert rc == CI.EXIT_REFUSED
    out = json.loads(capsys.readouterr().out)
    assert "--execute-paid" in out["refused"]


def test_cli_run_refuses_paid_post_without_frozen_lock(ctx, capsys):
    """AC21：未冻结（或已漂移）的 LOCK 一律拒绝付费 POST，早于任何 G-P/分词器检查。"""
    rc = CI.main(["--campaign-id", "r02_test", "run", "--phase", "dev",
                  "--execute-paid"])
    assert rc == CI.EXIT_REFUSED
    out = json.loads(capsys.readouterr().out)
    assert "FREEZE_INVALID" in out["refused"]
    assert "FINAL_LOCK_MISSING" in out["refused"]
    # 评审 M6：还没开跑的 campaign 被拒不是「仪器失效」，不得写 FREEZE_INVALID
    assert "已记 EV_FREEZE_INVALID" not in out["refused"]
    assert not any(e.get("code") == ST.EV_FREEZE_INVALID
                   for e in ST.Journal(ctx).events)


def test_cli_run_records_freeze_invalid_once_trials_started(ctx, capsys):
    """评审 M6：跑到一半实现漂移 ⇒ 必须留下 EV_FREEZE_INVALID 事件。

    否则结案只能看到「没跑完」（INCOMPLETE），仪器失效被记成进度不足。
    """
    _prepare(ctx, SG.dev_specs())
    store = ST.StateStore(ctx)
    spec = SG.dev_specs()[0]
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase,
                     slot_ids=[], post_cap=spec.post_cap)
    store.transition(st, status=ST.TRIAL_RUNNING)
    rc = CI.main(["--campaign-id", "r02_test", "run", "--phase", "dev",
                  "--execute-paid"])
    assert rc == CI.EXIT_REFUSED
    out = json.loads(capsys.readouterr().out)
    assert "已记 EV_FREEZE_INVALID" in out["refused"]
    ev = [e for e in ST.Journal(ctx).events
          if e.get("code") == ST.EV_FREEZE_INVALID]
    assert len(ev) == 1 and ev[0]["ok"] is False
    assert ev[0]["action"] == "freeze"


def test_cli_closeout_main_end_to_end(ctx, capsys):
    _prepare(ctx, SG.dev_specs())
    _dev_pass(ctx)
    rc = CO.main(["--campaign-id", "r02_test"])
    assert rc == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["process"]["status"] == CO.PROCESS_COMPLETE
    assert verdict["capability"]["status"] == CO.CAP_INCONCLUSIVE


# ---- 救援 ----


def test_assist_trial_marks_independent_false(ctx):
    _dev_pass(ctx)
    out = CI.assist_trial(ctx, "E-dev", "维护者手工替换了候选分析源码")
    assert out["status"] == ST.TRIAL_ASSISTED
    assert out["independent_success"] is False
