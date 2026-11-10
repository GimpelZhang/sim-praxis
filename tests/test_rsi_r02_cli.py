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
                   {"gate": "G-P", "campaign_id": ctx.campaign_id,
                    "authorized_posts": 8, "cap_usd": 100.0})
    with pytest.raises(CI.CliError):
        CI.require_gp_approval(ctx, posts=72)
    # 2026-11-10 root D reviewer LOW：批准件必须绑定本 campaign——同一 root 里
    # 残留的他 campaign 件（campaign_id 不符或缺席）一律不放行。
    ctx.write_json("quota/approvals/p26b.json",
                   {"gate": "G-P", "campaign_id": "r02_other",
                    "authorized_posts": 72, "cap_usd": 100.0})
    with pytest.raises(CI.CliError):
        CI.require_gp_approval(ctx, posts=72)
    ctx.write_json("quota/approvals/p26c.json",
                   {"gate": "G-P", "authorized_posts": 72, "cap_usd": 100.0})
    with pytest.raises(CI.CliError):
        CI.require_gp_approval(ctx, posts=72)
    ctx.write_json("quota/approvals/p26d.json",
                   {"gate": "G-P", "campaign_id": ctx.campaign_id,
                    "authorized_posts": 72, "cap_usd": 100.0})
    doc = CI.require_gp_approval(ctx, posts=72)
    assert doc["authorized_posts"] == 72
    assert doc["campaign_id"] == ctx.campaign_id


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


# ---- 按族付费归属（2026-11-10 T5 首启事故：三族共用 task 0 的批次） ----


def test_parse_family_batches_requires_all_three_families():
    assert CI.parse_family_batches("E=a,D=b,R=c") == {"E": "a", "D": "b", "R": "c"}
    with pytest.raises(CI.CliError):        # 单一批次无法覆盖三族
        CI.parse_family_batches("R02-T5-dev-1")
    with pytest.raises(CI.CliError):        # 缺族
        CI.parse_family_batches("E=a,D=b")
    with pytest.raises(CI.CliError):        # 未知族
        CI.parse_family_batches("E=a,D=b,R=c,X=d")
    with pytest.raises(CI.CliError):        # 重复族
        CI.parse_family_batches("E=a,E=b,R=c")
    with pytest.raises(CI.CliError):        # 空批
        CI.parse_family_batches("E=a,D=,R=c")


def test_task_of_family_matches_budget_contract():
    """计划 §7：family 帽 E/D/R 各 72 ⇒ task 1/2/3（协议 0 只给协议链）。"""
    assert CI.TASK_OF_FAMILY == {"E": 1, "D": 2, "R": 3}


def test_run_phase_requires_a_caller(ctx):
    _prepare(ctx, SG.dev_specs()[:1])
    with pytest.raises(CI.CliError):
        CI.run_phase(ctx, "dev", counter=lambda t: 100, trial_factory=_factory)


def test_run_phase_uses_per_family_caller(ctx):
    """整阶段必须逐族取 caller：E/D/R 走 task 1/2/3，而不是一个固定归属。"""
    _prepare(ctx, SG.dev_specs())
    seen, models = [], {}

    def caller_for(spec):
        seen.append((spec.trial_id, spec.family))
        models[spec.family] = H.FakeModel([SUBMIT])
        return models[spec.family]

    out = CI.run_phase(ctx, "dev", call_model_for=caller_for,
                       counter=lambda t: 100, trial_factory=_factory)
    assert out["completed"] is True
    assert seen == [("E-dev", "E"), ("D-dev", "D"), ("R-dev", "R")]
    assert all(len(m.requests) == 1 for m in models.values())


def test_make_phase_callers_binds_family_task_and_batch(monkeypatch):
    """付费 caller 的归属必须逐族钉死：E→task1/D→task2/R→task3 + 各族批次。"""
    seen = []

    def fake_paid(*, task, batch_id):
        seen.append((task, batch_id))
        return object()

    monkeypatch.setattr(CI, "_paid_gateway", fake_paid)
    monkeypatch.setattr(CI.M.LocalCounter, "load",
                        classmethod(lambda cls, p, s: object()))
    caller_for, _counter = CI.make_phase_callers(
        {"E": "bE", "D": "bD", "R": "bR"},
        tokenizer_path="p", tokenizer_sha256="s")
    for spec in SG.dev_specs():
        caller_for(spec)
    assert seen == [(1, "bE"), (2, "bD"), (3, "bR")]
    caller_for(SG.dev_specs()[0])              # 同族复用，不重复构造
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# T5-A1 零付费端到端预演：真 ModelGateway + 真 QuotaLedger + 假 transport
# ---------------------------------------------------------------------------
#
# 2026-11-10 T5 首启事故（4 实呼后全停）暴露的控制缺口：单测里 ``_paid_gateway``
# 与 ``QuotaLedger`` 都被替身绕过，于是「task 归属错」「发网前阻断被记成
# UNKNOWN_SENT」两处缺陷在端到端路径上无人看见。这里只替换网络这一层
# （``ModelGateway(transport=...)``），其余全部走生产代码：真实批次凭据校验、
# 真实 preflight、真实账行落盘、真实 billing_context 归属。

import types

import orchestrator.gateway.router as R
from orchestrator.gateway import quota_ledger as ql
from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import candidate as CD

RUN = H.action_json(action="run_experiment", params={"force": 0.5})


def _ok_transport(payload, calls):
    """litellm completion 替身：记录每次真实 POST，返回给定正文。"""
    def fake_completion(**kw):
        calls.append(kw)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=payload))],
            model=str(kw.get("model", "")).split("/", 1)[-1],
            usage=types.SimpleNamespace(input_tokens=11, output_tokens=3))
    return fake_completion


def _wire_real_gateway(monkeypatch, transport):
    """只把 transport 换成替身；构造参数（ledger/max_attempts/暂停态）保持生产样。"""
    real = R.ModelGateway

    def build(**kw):
        kw.setdefault("transport", transport)
        kw.setdefault("sleep", lambda s: None)
        return real(**kw)

    monkeypatch.setattr(R, "ModelGateway", build)
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "r02-test-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL", "https://llm.invalid/api/coding")
    # 分词器与归属无关：本地计数器由 run_phase 的 counter 注入
    monkeypatch.setattr(CI.M.LocalCounter, "load",
                        classmethod(lambda cls, p, s: object()))


def _approve(family_batch, *, task, approved=24):
    ql.write_approval(batch_id=family_batch, task=task, stage=CD.DEFAULT_STAGE,
                      approved=approved, beyond_normal=False, note="T5-A1 fixture",
                      namespace=ql.RSI_R02_NAMESPACE)


def _ledger_rows():
    from orchestrator.common.runtime_paths import rsi_r02_ledger_path
    p = rsi_r02_ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_paid_e2e_attributes_each_family_to_its_own_task(ctx, monkeypatch):
    """三族各 1 POST：账行必须分别落 task 1/2/3 与各族批次（T5 首启根因一）。"""
    _prepare(ctx, SG.dev_specs())
    calls: "list[dict]" = []
    _wire_real_gateway(monkeypatch, _ok_transport(SUBMIT, calls))
    _approve("R02-T5-E1", task=1)
    _approve("R02-T5-D1", task=2)
    _approve("R02-T5-R1", task=3)

    caller_for, _counter = CI.make_phase_callers(
        {"E": "R02-T5-E1", "D": "R02-T5-D1", "R": "R02-T5-R1"},
        tokenizer_path="unused", tokenizer_sha256="unused")
    out = CI.run_phase(ctx, "dev", call_model_for=caller_for,
                       counter=lambda t: 100, trial_factory=_factory)

    assert out["completed"] is True, out["stop_reason"]
    assert [r["status"] for r in out["results"]] == [ST.TRIAL_SUBMITTED] * 3
    assert len(calls) == 3                     # 每次提交恰好一次真实 POST

    rows = [r for r in _ledger_rows() if r.get("event") in (None, "call")]
    assert [(r["task"], r["batch_id"], r["stage"]) for r in rows] == [
        (1, "R02-T5-E1", CD.DEFAULT_STAGE),
        (2, "R02-T5-D1", CD.DEFAULT_STAGE),
        (3, "R02-T5-R1", CD.DEFAULT_STAGE)]
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert all(r["ok"] is True for r in rows)


def test_paid_e2e_preflight_block_is_zero_post_not_unknown(ctx, monkeypatch):
    """批次帽在发网前拦下 ⇒ 落 PREFLIGHT_BLOCKED（零 POST），绝不算 UNKNOWN_SENT。

    E 批次只批 1 次，而 E trial 的第二次 POST 会被 preflight 拦下：真实账行
    必须是 **1 条 call + 1 条 seq=null 的 preflight_blocked**，transport 只被
    调用 1 次，预留按 $0 结清，槽 CANCELLED，整阶段以 campaign 暂停收口。
    """
    _prepare(ctx, SG.dev_specs())
    calls: "list[dict]" = []
    _wire_real_gateway(monkeypatch, _ok_transport(RUN, calls))
    _approve("R02-T5-Eb1", task=1, approved=1)    # 只够 1 次
    _approve("R02-T5-Db1", task=2)
    _approve("R02-T5-Rb1", task=3)

    caller_for, _counter = CI.make_phase_callers(
        {"E": "R02-T5-Eb1", "D": "R02-T5-Db1", "R": "R02-T5-Rb1"},
        tokenizer_path="unused", tokenizer_sha256="unused")
    out = CI.run_phase(ctx, "dev", call_model_for=caller_for,
                       counter=lambda t: 100, trial_factory=_factory)

    assert out["completed"] is False
    assert out["stop_reason"].startswith("campaign_paused")
    first = out["results"][0]
    assert first["outcome"] == "preflight_blocked"
    assert first["reason_code"] == ST.EV_BUDGET_HIT
    assert first["posts_sent"] == 1
    assert len(calls) == 1                     # 拦截那次零 POST
    assert len(out["results"]) == 1            # 停全部：D/R 不再启动

    rows = _ledger_rows()
    call_rows = [r for r in rows if r.get("event") in (None, "call")]
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]
    assert len(call_rows) == 1 and call_rows[0]["task"] == 1
    assert len(blocked) == 1
    assert blocked[0]["seq"] is None and blocked[0]["task"] == 1
    assert blocked[0]["batch_id"] == "R02-T5-Eb1"
    assert "batch R02-T5-Eb1 used 1>=1" in str(blocked[0].get("err") or "")
    from orchestrator.common.runtime_paths import rsi_r02_gateway_state_path
    paused = json.loads(rsi_r02_gateway_state_path().read_text())
    assert paused["reason"] == "batch_budget_reached"   # 费用类帽写持久暂停态

    slots = ST.SlotBook(ctx)
    assert slots.status("E-dev-02") == ST.SLOT_CANCELLED
    assert slots.status("E-dev-03") == ST.SLOT_CANCELLED
    rec = B.BillingBook(ctx).record("E-dev::E-dev-02")
    assert rec.status == B.STATUS_SETTLED and rec.settled_usd == 0.0
    assert rec.cost_status == B.COST_EXACT

    camp = ST.CampaignState(ctx)
    assert camp.paused and ST.EV_BUDGET_HIT in (camp.pause_reason() or "")
    journal_codes = [e.get("code") for e in ST.Journal(ctx).events if e.get("code")]
    assert ST.EV_UNKNOWN_SENT not in journal_codes


def _failing_transport(error, calls):
    """POST 已发出后才失败：transport 被真实调用一次，然后抛供应端错误。"""
    def fake_completion(**kw):
        calls.append(kw)
        raise error
    return fake_completion


def test_paid_e2e_post_send_400_is_unknown_sent(ctx, monkeypatch):
    """发网后 400 ⇒ UNKNOWN_SENT（占槽、预留保留、全局暂停），绝不是零 POST。

    票六评审 HIGH 的端到端面：同一 ``NonRetryableError`` 类型在发网前后都会抛，
    按类型判相位会把这次真实付费调用洗成 $0/EXACT，与账本整数 seq 的 ok=false
    行直接矛盾。真实网关在此打的是「无相位标记」的异常，故必须走 UNKNOWN。
    """
    import litellm

    _prepare(ctx, SG.dev_specs())
    calls: "list[dict]" = []
    _wire_real_gateway(monkeypatch, _failing_transport(
        litellm.exceptions.BadRequestError(
            message="bad request", model="test-model", llm_provider="anthropic"),
        calls))
    _approve("R02-T5-Eu1", task=1)
    _approve("R02-T5-Du1", task=2)
    _approve("R02-T5-Ru1", task=3)

    caller_for, _counter = CI.make_phase_callers(
        {"E": "R02-T5-Eu1", "D": "R02-T5-Du1", "R": "R02-T5-Ru1"},
        tokenizer_path="unused", tokenizer_sha256="unused")
    out = CI.run_phase(ctx, "dev", call_model_for=caller_for,
                       counter=lambda t: 100, trial_factory=_factory)

    assert len(calls) == 1                     # 请求确实发了出去
    assert out["completed"] is False
    first = out["results"][0]
    assert first["outcome"] == "unknown_sent"
    assert first["reason_code"] == ST.EV_UNKNOWN_SENT

    rows = _ledger_rows()
    call_rows = [r for r in rows if r.get("event") in (None, "call")]
    assert len(call_rows) == 1
    assert call_rows[0]["seq"] == 1 and call_rows[0]["ok"] is False
    assert not [r for r in rows if r.get("event") == "preflight_blocked"]

    rec = B.BillingBook(ctx).record("E-dev::E-dev-01")
    assert rec.status == B.STATUS_UNKNOWN
    assert rec.cost_status == B.COST_UNKNOWN and rec.settled_usd is None
    assert ST.SlotBook(ctx).status("E-dev-01") == ST.SLOT_UNKNOWN


def test_paid_e2e_post_send_429_is_unknown_sent(ctx, monkeypatch):
    """发网后 429（配额熔断）⇒ UNKNOWN_SENT：熔断是**响应**，不是发网前拦截。

    与 400 同理，``GatewayPausedError`` 在两处都会抛（发网前早检查 / 发网后
    429 长熔断），判相位只能信网关标记。
    """
    import litellm

    _prepare(ctx, SG.dev_specs())
    calls: "list[dict]" = []
    _wire_real_gateway(monkeypatch, _failing_transport(
        litellm.exceptions.RateLimitError(
            message="配额已用尽 quota exceeded", model="test-model",
            llm_provider="anthropic"),
        calls))
    _approve("R02-T5-Eq1", task=1)
    _approve("R02-T5-Dq1", task=2)
    _approve("R02-T5-Rq1", task=3)

    caller_for, _counter = CI.make_phase_callers(
        {"E": "R02-T5-Eq1", "D": "R02-T5-Dq1", "R": "R02-T5-Rq1"},
        tokenizer_path="unused", tokenizer_sha256="unused")
    out = CI.run_phase(ctx, "dev", call_model_for=caller_for,
                       counter=lambda t: 100, trial_factory=_factory)

    assert len(calls) == 1
    first = out["results"][0]
    assert first["outcome"] == "unknown_sent"
    assert first["reason_code"] == ST.EV_UNKNOWN_SENT

    rows = _ledger_rows()
    call_rows = [r for r in rows if r.get("event") in (None, "call")]
    assert len(call_rows) == 1 and call_rows[0]["ok"] is False
    assert not [r for r in rows if r.get("event") == "preflight_blocked"]

    rec = B.BillingBook(ctx).record("E-dev::E-dev-01")
    assert rec.status == B.STATUS_UNKNOWN and rec.cost_status == B.COST_UNKNOWN
    assert ST.SlotBook(ctx).status("E-dev-01") == ST.SLOT_UNKNOWN
