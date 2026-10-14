"""Task3（§1.5/§6.3）计费收口集成测试：BillingGate × ModelGateway × billing_context。

全部 monkeypatch / 合成传输，0 联网 0 付费。断言口径（计划硬性契约）：
- 一次逻辑调用 = preflight 一次 + 至多一行整数 seq 账；HTTP 退避重试不双计数；
- 成功行回填 tokens_in/out、thread_id、每调用刷新的 evidence 引用；
- 越过 preflight 之后的任何失败（含 GatewayPausedError）恰好一行 ok=false；
- null stage / 入口熔断 / visual 误走文本入口 / image block 在 preflight 之前拒绝，
  零行账、零触网；
- 挂了账本但缺 billing_context → QuotaError fail-closed，零行账、零触网；
- preflight 拦截（无批次凭据）只写 seq=null 审计行，不占整数 seq，不写持久熔断文件；
- call_visual 是视觉唯一入口：stage 硬绑 visual_rollout，请求体含 1 image + 1 text。

本文件承接原 tests/milestone1/test_direction.py::test_09（记账收口后该用例不再
能经 setter 触发，故移到真 ModelGateway + BillingGate 链路上，覆盖更贴近生产）。
"""
from __future__ import annotations

import json
import types

import pytest
import yaml
from litellm.exceptions import (
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
)

import orchestrator.gateway.router as R
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import (
    QuotaError,
    QuotaLedger,
    billing_context,
    set_current_evidence,
)

BATCH_T2 = "T3-FIXTURE-T2"
BATCH_T4 = "T3-FIXTURE-T4"
BATCH_T5 = "T3-FIXTURE-T5"
ANCHOR = "供应侧若下线该阶段模型，执行期以 --validate-models 实呼为准，未恢复前该 stage 置 null"
DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "billing-test-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
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


def _gateway(env, transport=None, raw_transport=None, **kw):
    return R.ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                          transport=transport, raw_transport=raw_transport,
                          ledger=QuotaLedger(), **kw)


def _litellm_resp(content="ok", *, in_tok=7, out_tok=3, model="deepseek-v4-1-flash"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        model=model,
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))


def _rows(tmp):
    p = tmp / "phase1/quota/ledger.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _int_rows(tmp):
    return [r for r in _rows(tmp) if isinstance(r.get("seq"), int)]


def _approve(task, stage, batch, approved=8):
    ql.write_approval(batch_id=batch, task=task, stage=stage, approved=approved,
                      beyond_normal=False, note="fixture 批次（零付费单测）")


TEXT_MSG = [{"role": "user", "content": "hi"}]


# ---------- 1. 成功：一行整数 seq，tokens/thread/evidence 全回填 ----------

def test_success_single_row_tokens_evidence(env):
    tmp = env["tmp"]

    def transport(**kw):
        return _litellm_resp("方案文本", in_tok=123, out_tok=45)

    gw = _gateway(env, transport=transport)
    _approve(2, "macro_ideation", BATCH_T2)
    with billing_context(task=2, batch_id=BATCH_T2, thread_id="t-bill-1"):
        set_current_evidence("phase1/task2_direction/macro_t-bill-1_r0_a0.req.json")
        text, usage = gw.call("macro_ideation", TEXT_MSG, max_tokens=64)
    assert text == "方案文本" and usage == (123, 45)
    rows = _int_rows(tmp)
    assert len(rows) == 1
    r = rows[0]
    assert r["seq"] == 1 and r["ok"] is True
    assert r["stage"] == "macro_ideation" and r["model"] == "deepseek-v4.1-flash"
    assert r["task"] == 2 and r["batch_id"] == BATCH_T2
    assert r["thread_id"] == "t-bill-1"
    assert r["tokens_in"] == 123 and r["tokens_out"] == 45
    assert r["global_used_after"] == 1 and r["batch_used_after"] == 1
    assert r["evidence"] == "phase1/task2_direction/macro_t-bill-1_r0_a0.req.json"
    assert r["err"] is None


# ---------- 2. HTTP 重试不双计数：3 次传输尝试仍只 1 行 seq ----------

def test_http_retries_still_one_logical_row(env):
    tmp = env["tmp"]
    attempts = {"n": 0}

    def transport(**kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ServiceUnavailableError(
                "503 unavailable", "anthropic", "deepseek-v4.1-flash", None)
        return _litellm_resp()

    gw = _gateway(env, transport=transport)
    _approve(2, "macro_ideation", BATCH_T2)
    with billing_context(task=2, batch_id=BATCH_T2):
        text, usage = gw.call("macro_ideation", TEXT_MSG)
    assert text == "ok" and usage == (7, 3)
    assert attempts["n"] == 3
    rows = _int_rows(tmp)
    assert len(rows) == 1 and rows[0]["ok"] is True and rows[0]["seq"] == 1


# ---------- 3. 不可重试失败：恰好一行 ok=false 后原样抛 ----------

def test_failure_records_one_ok_false_row(env):
    tmp = env["tmp"]

    def transport(**kw):
        raise BadRequestError("400 bad request", model="deepseek-v4.1-flash",
                              llm_provider="anthropic", response=None)

    gw = _gateway(env, transport=transport)
    _approve(2, "macro_ideation", BATCH_T2)
    with billing_context(task=2, batch_id=BATCH_T2, thread_id="t-bill-3"):
        with pytest.raises(R.NonRetryableError):
            gw.call("macro_ideation", TEXT_MSG)
    rows = _int_rows(tmp)
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] is False and r["seq"] == 1 and r["thread_id"] == "t-bill-3"
    assert "BadRequestError" in r["err"] and "400" in r["err"]
    # 失败也占批次/全局计数（宁紧勿松，与 Task2 保守口径一致）
    assert r["batch_used_after"] == 1 and r["global_used_after"] == 1


# ---------- 4. 配额长熔断逃出重试循环：一行 ok=false + 暂停态文件 ----------

def test_quota_long_circuit_records_ok_false(env):
    tmp = env["tmp"]

    def transport(**kw):
        raise RateLimitError(
            "429 QuotaExceeded: insufficient quota, 欠费",
            "anthropic", "deepseek-v4.1-flash", None)

    gw = _gateway(env, transport=transport)
    _approve(2, "macro_ideation", BATCH_T2)
    with billing_context(task=2, batch_id=BATCH_T2):
        with pytest.raises(R.GatewayPausedError):
            gw.call("macro_ideation", TEXT_MSG)
    rows = _int_rows(tmp)
    assert len(rows) == 1 and rows[0]["ok"] is False and rows[0]["seq"] == 1
    assert (tmp / "gwstate/quota_pause.json").exists()
    # 熔断窗口内第二次调用：router 入口 is_paused 在 ledger.preflight 之前拦截，
    # 不触网、不新增任何账行（含审计行）
    def must_not_call(**kw):
        raise AssertionError("熔断窗口内禁止触网")

    gw2 = _gateway(env, transport=must_not_call)
    with billing_context(task=2, batch_id=BATCH_T2):
        with pytest.raises(R.GatewayPausedError):
            gw2.call("macro_ideation", TEXT_MSG)
    assert len(_int_rows(tmp)) == 1 and len(_rows(tmp)) == 1
    # 直接打到账本闸门时，paused 经 preflight 拦并写一行 seq=null 审计
    with pytest.raises(R.GatewayPausedError):
        QuotaLedger().preflight("macro_ideation", task=2, batch_id=BATCH_T2)
    blocked = [r for r in _rows(tmp) if r.get("event") == "preflight_blocked"]
    assert len(blocked) == 1 and blocked[0]["seq"] is None
    assert len(_int_rows(tmp)) == 1


# ---------- 5. 缺 billing_context：fail-closed，零账零网 ----------

def test_missing_context_fail_closed_no_rows(env):
    tmp = env["tmp"]

    def must_not_call(**kw):
        raise AssertionError("无 billing_context 必须在触网前拒绝")

    gw = _gateway(env, transport=must_not_call)
    _approve(2, "macro_ideation", BATCH_T2)
    # 故意不进入 billing_context
    with pytest.raises(QuotaError, match="billing_context"):
        gw.call("macro_ideation", TEXT_MSG)
    assert _rows(tmp) == []


# ---------- 6. image block 在 preflight 之前被拒：零账零网 ----------

def test_image_block_guard_before_preflight_zero_rows(env):
    tmp = env["tmp"]
    img_msg = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
        {"type": "text", "text": "see?"}]}]

    def must_not_call(**kw):
        raise AssertionError("image block 必须在触网前拒绝")

    gw = _gateway(env, transport=must_not_call)
    _approve(2, "macro_ideation", BATCH_T2)
    with billing_context(task=2, batch_id=BATCH_T2):
        with pytest.raises(R.NonRetryableError, match="call_visual"):
            gw.call("macro_ideation", img_msg)
    assert _rows(tmp) == []


# ---------- 7. visual_rollout 走文本入口：preflight 之前拒绝，零账零网 ----------

def test_visual_stage_text_call_rejected_zero_rows(env):
    tmp = env["tmp"]

    def must_not_call(**kw):
        raise AssertionError("视觉阶段禁止走文本 call()")

    gw = _gateway(env, transport=must_not_call)
    _approve(5, "visual_rollout", BATCH_T5)
    with billing_context(task=5, batch_id=BATCH_T5):
        with pytest.raises(R.NonRetryableError, match="call_visual"):
            gw.call("visual_rollout", TEXT_MSG)
    assert _rows(tmp) == []


# ---------- 8. 无批次凭据：preflight 拦截不触网 + seq=null 审计行 + 不落持久熔断 ----------

def test_preflight_missing_credential_blocks_without_net(env):
    tmp = env["tmp"]

    def must_not_call(**kw):
        raise AssertionError("无凭据必须在触网前拦截")

    gw = _gateway(env, transport=must_not_call)
    with billing_context(task=2, batch_id="NO-SUCH-BATCH"):
        with pytest.raises(R.GatewayPausedError) as ei:
            gw.call("macro_ideation", TEXT_MSG)
    assert ei.value.state["reason"] == "batch_approval_missing"
    int_rows = _int_rows(tmp)
    blocked = [r for r in _rows(tmp) if r.get("event") == "preflight_blocked"]
    assert int_rows == [] and len(blocked) == 1 and blocked[0]["seq"] is None
    assert not (tmp / "gwstate/quota_pause.json").exists()


# ---------- 9. null stage：不查凭据、不记账、不触网 ----------

def test_null_stage_no_rows_no_net(env):
    tmp = env["tmp"]
    # 单独写一份 visual=null 的映射
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in R.STAGES}
    sm["macro_ideation"] = {"model_id": None, "capabilities": ["text"],
                            "purpose": "t", "open_question": ANCHOR}
    null_map = tmp / "null_map.yaml"
    null_map.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm}, allow_unicode=True, width=4096), encoding="utf-8")

    def must_not_call(**kw):
        raise AssertionError("null stage 不得触网")

    gw = R.ModelGateway(stage_models_path=null_map, sleep=lambda s: None,
                        transport=must_not_call, ledger=QuotaLedger())
    with billing_context(task=2, batch_id=BATCH_T2):
        with pytest.raises(R.StageUnresolvedError):
            gw.call("macro_ideation", TEXT_MSG)
    assert _rows(tmp) == []


# ---------- 10. call_visual：硬绑视觉 stage，1 image + 1 text，一行视觉账 ----------

def _visual_message_body(model, text="red"):
    return {"type": "message", "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 88, "output_tokens": 2}}


def test_call_visual_success_one_visual_row_with_image_block(env):
    tmp = env["tmp"]
    seen = {}

    def raw_post(url, headers=None, json=None, timeout=None):
        seen["json"] = json
        body = _visual_message_body(json["model"])
        return types.SimpleNamespace(status_code=200, json=lambda: body,
                                     text=json_dumps(body), headers={})

    gw = _gateway(env, raw_transport=raw_post)
    _approve(5, "visual_rollout", BATCH_T5, approved=6)
    with billing_context(task=5, batch_id=BATCH_T5, thread_id="t-vis-1"):
        set_current_evidence("phase1/task5_rung4/frame_0001.req.json")
        text, usage = gw.call_visual("What color?", "QUJDREVG", max_tokens=16)
    assert text == "red" and usage == (88, 2)
    blocks = seen["json"]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert blocks[0]["source"]["data"] == "QUJDREVG"
    assert seen["json"]["model"] == "ark-code-latest"
    rows = _int_rows(tmp)
    assert len(rows) == 1
    r = rows[0]
    assert r["stage"] == "visual_rollout" and r["ok"] is True
    assert r["task"] == 5 and r["batch_id"] == BATCH_T5 and r["thread_id"] == "t-vis-1"
    assert r["tokens_in"] == 88 and r["tokens_out"] == 2
    assert r["evidence"] == "phase1/task5_rung4/frame_0001.req.json"


# ---------- 11. call_visual 失败：一行 ok=false 视觉账后抛出 ----------

def test_call_visual_failure_one_ok_false_row(env):
    tmp = env["tmp"]

    def raw_post(url, headers=None, json=None, timeout=None):
        body = {"type": "error", "error": {"type": "service_unavailable",
                                          "code": "unavailable", "message": "retry"}}
        return types.SimpleNamespace(status_code=503, json=lambda: body,
                                     text=json_dumps(body), headers={})

    gw = _gateway(env, raw_transport=raw_post, max_attempts=1)
    _approve(5, "visual_rollout", BATCH_T5, approved=6)
    with billing_context(task=5, batch_id=BATCH_T5):
        with pytest.raises(R.GatewayError):
            gw.call_visual("What color?", "QUJDREVG")
    rows = _int_rows(tmp)
    assert len(rows) == 1
    assert rows[0]["stage"] == "visual_rollout" and rows[0]["ok"] is False
    assert "503" in (rows[0]["err"] or "")


# ---------- 12. 连续两次调用：seq/计数连续，evidence 按调用刷新互不串 ----------

def test_per_call_evidence_refresh_and_seq_continuity(env):
    tmp = env["tmp"]

    gw = _gateway(env, transport=lambda **kw: _litellm_resp())
    # 批次凭据 stage 必须与调用 stage 匹配：macro/coding 各持一批
    _approve(2, "macro_ideation", BATCH_T2)
    _approve(4, "coding_debug", BATCH_T4)
    with billing_context(task=2, batch_id=BATCH_T2, thread_id="t-bill-12"):
        set_current_evidence("ev/req-a.json")
        gw.call("macro_ideation", TEXT_MSG)
    with billing_context(task=4, batch_id=BATCH_T4, thread_id="t-bill-12"):
        set_current_evidence("ev/req-b.json")
        gw.call("coding_debug", TEXT_MSG)
    rows = _int_rows(tmp)
    assert [r["seq"] for r in rows] == [1, 2]
    assert [r["global_used_after"] for r in rows] == [1, 2]
    assert [r["batch_used_after"] for r in rows] == [1, 1]
    assert [r["evidence"] for r in rows] == ["ev/req-a.json", "ev/req-b.json"]
    assert [r["stage"] for r in rows] == ["macro_ideation", "coding_debug"]


def json_dumps(body):
    return json.dumps(body, ensure_ascii=False)
