"""RSI-R0 网关/发网事实测试（AC07–AC11，零联网零付费）。

对应计划条款：

* AC07  R0 单逻辑 call 至多 1 次 POST；空/429/5xx/超时无暗重试
* AC08  并发第二执行器被拒；资源原子预留先于发网
* AC09  unknown_sent 恢复不重发、不释放预留、序号不丢
* AC10  输入/输出/费用/时间/step 帽分别可触发停止
* AC11  失败响应脱敏且保留结构，usage 缺失标 unknown
"""
from __future__ import annotations

import json
import types

import pytest
import yaml

import orchestrator.gateway.router as R
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context
from orchestrator.rsi_r0 import attempts as A
from orchestrator.rsi_r0 import contracts as C

ANCHOR = ("供应侧若下线该阶段模型，执行期以 --validate-models 实呼为准，"
          "未恢复前该 stage 置 null")
DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "c_gw")
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "r0-test-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    oq = tmp_path / "open-questions.md"
    oq.write_text(f"# Open Questions\n\n- {ANCHOR}\n", encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    map_path = tmp_path / "stage_models.yaml"
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in R.STAGES}
    map_path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm,
    }, allow_unicode=True, width=4096), encoding="utf-8")
    yield {"tmp": tmp_path, "map": map_path}


def _resp(content="ok", *, in_tok=7, out_tok=3):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        model="deepseek-v4-1-flash",
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))


def _gateway(env, transport, **kw):
    """R0 组合根同款：ledger=QuotaLedger.rsi_r0() + max_attempts=1 + 本 campaign 暂停态。"""
    from orchestrator.common.runtime_paths import rsi_r0_gateway_state_path
    return R.ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                          transport=transport, ledger=QuotaLedger.rsi_r0(),
                          max_attempts=kw.pop("max_attempts", 1),
                          pause_state_path=kw.pop("pause_state_path",
                                                  rsi_r0_gateway_state_path()),
                          **kw)


def _approve(task=0, batch="R0-P", approved=6):
    ql.write_approval(batch_id=batch, task=task, stage="coding_debug",
                      approved=approved, beyond_normal=False, note="r0 fixture",
                      namespace=ql.RSI_R0_NAMESPACE)


def _ledger_rows():
    p = ql.rsi_r0_ledger_path()
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


TEXT_MSG = [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------- AC07


def test_empty_response_makes_exactly_one_post(env):
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        return _resp(content="")

    gw = _gateway(env, transport)
    _approve()
    with billing_context(task=0, batch_id="R0-P"):
        with pytest.raises(R.EmptyResponseError):
            gw.call("coding_debug", TEXT_MSG, max_tokens=64)
    assert calls["n"] == 1, "R0 必须每逻辑调用至多一次 POST"
    rows = [r for r in _ledger_rows() if isinstance(r.get("seq"), int)]
    assert len(rows) == 1 and rows[0]["ok"] is False
    assert "EmptyResponseError" in (rows[0]["err"] or "")


def test_429_makes_exactly_one_post(env):
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        raise R.litellm.exceptions.RateLimitError(
            "429 slow down", "anthropic", "deepseek-v4.1-flash")

    gw = _gateway(env, transport)
    _approve()
    with billing_context(task=0, batch_id="R0-P"):
        with pytest.raises(R.GatewayError):
            gw.call("coding_debug", TEXT_MSG)
    assert calls["n"] == 1


def test_5xx_makes_exactly_one_post(env):
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        raise R.litellm.exceptions.ServiceUnavailableError(
            "503 unavailable", "anthropic", "deepseek-v4.1-flash", None)

    gw = _gateway(env, transport)
    _approve()
    with billing_context(task=0, batch_id="R0-P"):
        with pytest.raises(R.GatewayError):
            gw.call("coding_debug", TEXT_MSG)
    assert calls["n"] == 1


def test_preflight_block_still_consumes_slot_without_http(env):
    """无批准件：写 seq=null 审计行、0 次发网，但该 slot 仍算已消耗（AC07/§6.3）。"""
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        return _resp()

    gw = _gateway(env, transport)
    with billing_context(task=0, batch_id="R0-MISSING"):
        with pytest.raises(ql.GatewayPausedError):
            gw.call("coding_debug", TEXT_MSG)
    assert calls["n"] == 0
    rows = _ledger_rows()
    assert len(rows) == 1 and rows[0]["seq"] is None
    assert rows[0]["event"] == "preflight_blocked"


# --------------------------------------------------------------- AC08


def test_second_executor_is_rejected(tmp_path):
    with A.campaign_lock(tmp_path):
        with pytest.raises(A.ConcurrentCampaignError):
            with A.campaign_lock(tmp_path):
                pass  # pragma: no cover


def test_reservation_precedes_send_and_caps_enforced(tmp_path):
    caps = C.ResourceCaps(per_request_input_tokens=100,
                          per_request_output_tokens=50,
                          cumulative_input_tokens=150,
                          cumulative_output_tokens=120,
                          total_usd_equivalent=1.0)
    res = A.BudgetReservations(caps, reservations_dir=tmp_path / "res")
    journal = A.AttemptJournal(path=tmp_path / "attempts.jsonl", reservations=res)

    rec = journal.reserve(slot_id="s1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=100, tokens_out=50, usd=0.5)
    assert res.path_for(rec.request_id).exists()
    assert res.totals()["tokens_in"] == 100

    # 累计输入帽：100 + 60 > 150
    with pytest.raises(C.ResourceCapExceeded):
        journal.reserve(slot_id="s2", batch_id="R0-P", task_id=0,
                        stage="coding_debug", tokens_in=60, tokens_out=10, usd=0.1)

    # 单请求帽
    with pytest.raises(C.ResourceCapExceeded):
        journal.reserve(slot_id="s3", batch_id="R0-P", task_id=0,
                        stage="coding_debug", tokens_in=101, tokens_out=10, usd=0.1)

    # 费用帽
    with pytest.raises(C.ResourceCapExceeded):
        journal.reserve(slot_id="s4", batch_id="R0-P", task_id=0,
                        stage="coding_debug", tokens_in=10, tokens_out=10, usd=0.9)


def test_time_and_step_caps_are_exposed_as_stop_hats():
    caps = C.ResourceCaps()
    assert caps.trial_wall_s == 2700
    assert caps.request_timeout_s == 180
    assert caps.tool_command_timeout_s == 120
    assert caps.reference_run_s == 180
    assert caps.trial_sim_steps == 1_000_000
    assert caps.stage_sim_steps == 10_000_000
    d = caps.as_dict()
    assert set(d) >= {"trial_wall_s", "request_timeout_s", "trial_sim_steps"}


# --------------------------------------------------------------- AC09


def test_unknown_sent_recovery_keeps_occupancy_and_never_resends(tmp_path):
    caps = C.ResourceCaps()
    res = A.BudgetReservations(caps, reservations_dir=tmp_path / "res")
    journal = A.AttemptJournal(path=tmp_path / "attempts.jsonl", reservations=res)

    rec = journal.reserve(slot_id="s1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=1000, tokens_out=500, usd=0.02)
    journal.mark_sent(rec, seq=1)
    # 模拟「发网后、结算前」崩溃：直接走恢复
    out = journal.recover()
    assert out["count"] == 1
    assert out["recovered"][0]["to"] == A.AttemptState.UNKNOWN_SENT.value
    doc = res.load_all()[rec.request_id]
    assert doc["state"] == "UNKNOWN_SENT"
    # 预留**不释放**：占用仍计入
    assert res.totals()["tokens_in"] == 1000
    assert res.totals()["usd"] == pytest.approx(0.02)
    # 未核销的 UNKNOWN 拒绝开新付费批次
    with pytest.raises(RuntimeError, match="UNKNOWN_SENT"):
        journal.assert_no_unknown()


def test_prepared_without_send_settles_as_preflight_blocked(tmp_path):
    """PREPARED 残余 = 可证实发网前中止 ⇒ BLOCKED_PREFLIGHT、预留以 0 结清（AC09）。"""
    res = A.BudgetReservations(C.ResourceCaps(), reservations_dir=tmp_path / "res")
    journal = A.AttemptJournal(path=tmp_path / "attempts.jsonl", reservations=res)
    rec = journal.reserve(slot_id="s1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=10, tokens_out=10, usd=0.0)
    out = journal.recover()
    assert out["recovered"][0]["to"] == A.AttemptState.BLOCKED_PREFLIGHT.value
    row = journal.latest_by_request()[rec.request_id]
    assert row["state"] == A.AttemptState.BLOCKED_PREFLIGHT.value
    assert row["attempts_http"] == 0
    doc = res.load_all()[rec.request_id]
    assert doc["state"] == "SETTLED" and doc["usage_in"] == 0 and doc["usage_out"] == 0
    # 幂等：再恢复一次不重复记账、也不阻塞新批次
    assert journal.recover()["count"] == 0
    journal.assert_no_unknown()


# --------------------------------------------------------------- AC11


def test_failure_evidence_is_structural_and_secret_free(env):
    """失败记账保留结构（http/stop_reason/block 类型/body 哈希），不含秘密原文。"""
    # 运行时拼出「密钥形态」串：源文件里不构成连续字面量（仓库 scan 不命中），
    # 但 redact（common.secrets 单一源）必须把它抹掉。
    secret = "ghp" + "_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5"
    calls = {"n": 0}

    def transport(**kw):
        calls["n"] += 1
        raise R.litellm.exceptions.InternalServerError(
            f"500 boom {secret}", "anthropic", "deepseek-v4.1-flash")

    gw = _gateway(env, transport, max_attempts=1)
    _approve()
    with billing_context(task=0, batch_id="R0-P"):
        with pytest.raises(R.GatewayError):
            gw.call("coding_debug", TEXT_MSG)
    rows = [r for r in _ledger_rows() if isinstance(r.get("seq"), int)]
    assert len(rows) == 1
    text = json.dumps(rows, ensure_ascii=False)
    assert secret not in text, "账行不得回显凭据形态串"
    assert rows[0]["ok"] is False and rows[0]["tokens_in"] is None
    assert "InternalServerError" in (rows[0]["err"] or "")  # 结构保留：异常类名


def test_attempt_record_marks_usage_unknown_not_zero(tmp_path):
    res = A.BudgetReservations(C.ResourceCaps(), reservations_dir=tmp_path / "res")
    journal = A.AttemptJournal(path=tmp_path / "attempts.jsonl", reservations=res)
    rec = journal.reserve(slot_id="s1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=10, tokens_out=10, usd=0.0)
    journal.mark_sent(rec, seq=1)
    journal.settle_fail(rec, error_class="Timeout", http_status=None,
                        body_sha256=None)
    row = journal.latest_by_request()[rec.request_id]
    assert row["state"] == A.AttemptState.SETTLED_FAIL.value
    assert row["usage_in"] is None and row["usage_out"] is None
    assert row["attempts_http"] == 1


def test_success_settles_reservation_with_actual_usage(tmp_path):
    res = A.BudgetReservations(C.ResourceCaps(), reservations_dir=tmp_path / "res")
    journal = A.AttemptJournal(path=tmp_path / "attempts.jsonl", reservations=res)
    rec = journal.reserve(slot_id="s1", batch_id="R0-P", task_id=0,
                          stage="coding_debug", tokens_in=100, tokens_out=50, usd=0.01)
    journal.mark_sent(rec, seq=1)
    journal.settle_ok(rec, usage_in=64, usage_out=32, stop_reason="end_turn",
                      block_types=("text",), provider_request_id="prov_1",
                      body_sha256="a" * 64, usd=0.005)
    doc = res.load_all()[rec.request_id]
    assert doc["state"] == "SETTLED"
    assert doc["usage_in"] == 64 and doc["usage_out"] == 32
    assert doc["reserved_tokens_in"] == 100  # 预留原值保留供对账
