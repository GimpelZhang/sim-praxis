"""全部 monkeypatch：不联网、不调用方舟、不产生费用。
v4：三阶段 stage_models 配置 + Messages 实呼校验层；fixture 值不允许长得像密钥
（B2）：SIMPRAXIS_LLM_TOKEN 用 redacted 形态串，不含 ark-/hf_ 等前缀。
共 18 个测试函数（v4.1：15 + 新增 16/17/18）：三 stage ping/回显/换模/404 指引/
null 三态/视觉两态（test 8 含 n2 词边界锁定）+ 退避、配额熔断、400/5xx/401-403、
redact（test 15 含 n1 大写形态）+ 16 m9 信封 type/code 优先的瞬时限流、
17 m3 两传输层 5xx 先尊重 Retry-After、18 m4 FAILURE.json 且 latest 不移动。
"""
import base64
import json
import struct
import types

import pytest
import yaml
from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
)

import orchestrator.gateway.router as R

STAGES = R.STAGES
ANCHOR = "供应侧若下线该阶段模型，执行期以 --validate-models 实呼为准，未恢复前该 stage 置 null"
DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}
ECHO = {"deepseek-v4.1-flash": "deepseek-v4-1-flash",
        "glm-5.3-flash": "glm-5-3-flash",
        "ark-code-latest": "auto",
        "glm-5-3-flash-260828": "glm-5-3-flash-260828"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    # 非密钥形态：纯描述串，不含任何 token 前缀（B2 + v4 第 9 条正则）
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "smoke-test-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(tmp_path / "runs"))
    oq = tmp_path / "open-questions.md"
    oq.write_text(f"# Open Questions\n\n- {ANCHOR}\n", encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    yield


def _write_map(path, nulls=(), models=None):
    models = models or DEFAULT_IDS
    sm = {}
    for s in STAGES:
        caps = ["text", "vision"] if s == R.VISION_STAGE else ["text"]
        if s in nulls:
            sm[s] = {"model_id": None, "capabilities": caps,
                     "purpose": "t", "open_question": ANCHOR}
        else:
            sm[s] = {"model_id": models[s], "capabilities": caps, "purpose": "t"}
    # allow_unicode+大 width：锚点是中文长句，禁止 \u 转义或折行（否则下游文本 replace
    # 类校验会失配——锚点逐字匹配按单行纯文本进行）
    path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm,
    }, allow_unicode=True, width=4096), encoding="utf-8")


def _msg_body(model, text="pong"):
    return {"type": "message", "model": ECHO.get(model, model),
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 4, "output_tokens": 1}}


def _fake_raw(sent, *, status=200, body_factory=None, status_map=None):
    """记录请求 kwargs 并返回假 response；body_factory(url, kwargs) 可按请求定制。"""
    def _post(url, headers=None, json=None, timeout=None):
        sent.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        model = json["model"]
        is_vision = any(isinstance(b, dict) and b.get("type") == "image"
                        for b in json["messages"][0]["content"])
        if status_map:
            st, body = status_map(model, is_vision)
        elif body_factory:
            st, body = status, body_factory(model, is_vision)
        else:
            answer = "Red" if is_vision else "pong"
            st, body = status, _msg_body(model, answer)
        return types.SimpleNamespace(
            status_code=st, json=lambda b=body: b, text=json_dumps(body), headers={})
    return _post


def json_dumps(body):
    return json.dumps(body, ensure_ascii=False)


# ---------- 1. 三 stage ping 全绿 + result.json/latest ----------

def test_three_stage_ping_all_green(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    sent: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_fake_raw(sent), sleep=lambda s: None)
    doc = gw.validate_models()
    assert doc["skipped_null"] == []
    rows = {r["stage"]: r for r in doc["results"]}
    assert set(rows) == set(STAGES)
    for r in rows.values():
        assert r["http_status"] == 200 and r["type"] == "message"
        assert r["requested_id"] and r["echo_model"] and r["latency_ms"] >= 0
    latest = tmp_path / "runs" / "model_validate" / "latest" / "result.json"
    assert latest.is_file()
    assert json.loads(latest.read_text())["skipped_null"] == []
    # 四个付费请求：三文本 + 一视觉
    assert len(sent) == 4
    assert {s["json"]["model"] for s in sent[:3]} == set(DEFAULT_IDS.values())


# ---------- 2. 点号->连字符回显规范化落盘 ----------

def test_echo_model_normalization_recorded(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_fake_raw([]), sleep=lambda s: None)
    rows = {r["stage"]: r for r in gw.validate_models()["results"]}
    assert rows["macro_ideation"]["requested_id"] == "deepseek-v4.1-flash"
    assert rows["macro_ideation"]["echo_model"] == "deepseek-v4-1-flash"
    assert rows["coding_debug"]["echo_model"] == "glm-5-3-flash"
    assert rows["visual_rollout"]["echo_model"] == "auto"


# ---------- 3. 供应换模：改 model_id 后请求必须打向新 ID（两条路径） ----------

def test_swap_model_id_routes_to_new_id(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path, models={**DEFAULT_IDS, "coding_debug": "glm-5-3-flash-260828"})
    sent: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_fake_raw(sent), sleep=lambda s: None)
    rec = gw.ping_text("coding_debug")
    assert rec["requested_id"] == "glm-5-3-flash-260828"
    assert sent[-1]["json"]["model"] == "glm-5-3-flash-260828"
    # 运行期 litellm 路径同样打新 ID
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
            model=kwargs["model"].split("/", 1)[1],
            usage=types.SimpleNamespace(input_tokens=3, output_tokens=1))

    monkeypatch.setattr(R.litellm, "completion", fake_completion)
    gw2 = R.ModelGateway(stage_models_path=map_path, transport=fake_completion)
    # Task3：call() 返回 (text, (tokens_in, tokens_out))——内容与 usage 同时断言
    text_out, usage = gw2.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert text_out == "ok" and usage == (3, 1)
    assert captured["model"] == "anthropic/glm-5-3-flash-260828"
    assert captured["api_base"].endswith("/api/coding")


# ---------- 4. 404 必须给换模动作指引 ----------

def test_404_gives_swap_model_guidance(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    body = {"type": "error", "error": {"type": "not_found_error",
                                       "code": "model_not_found",
                                       "message": "model or endpoint not found"}}

    def _post(url, headers=None, json=None, timeout=None):
        return types.SimpleNamespace(status_code=404,
                                    json=lambda: body, text=json_dumps(body), headers={})

    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_post, sleep=lambda s: None)
    with pytest.raises(R.NonRetryableError, match="stage_models.yaml") as exc:
        gw.ping_text("coding_debug")
    msg = str(exc.value)
    assert "glm-5-3-flash-260828" in msg and "--validate-models" in msg


# ---------- 5/6. null 无锚点拒绝；锚点不逐字拒绝 ----------

def test_null_stage_without_anchor_rejected(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in STAGES}
    sm["macro_ideation"]["model_id"] = None  # 无 open_question
    map_path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm}), encoding="utf-8")
    with pytest.raises(R.GatewayError, match="open_question"):
        R.ModelGateway(stage_models_path=map_path)


def test_null_stage_anchor_mismatch_rejected(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path, nulls=("coding_debug",))
    text = map_path.read_text(encoding="utf-8").replace(
        ANCHOR, "这句话在 open-questions.md 里根本不存在")
    map_path.write_text(text, encoding="utf-8")
    with pytest.raises(R.GatewayError, match="逐字出现"):
        R.ModelGateway(stage_models_path=map_path)


# ---------- 7. 合规 null：可构造/不触网/validate 报告 SKIP ----------

def test_null_stage_skips_and_unresolved_call(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path, nulls=("visual_rollout",))
    sent: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_fake_raw(sent), sleep=lambda s: None)
    assert gw.model_id("visual_rollout") is None
    assert gw.null_stages() == ["visual_rollout"]

    def must_not_call(**kwargs):
        raise AssertionError("null stage 不得走 litellm 发起网络调用")

    monkeypatch.setattr(R.litellm, "completion", must_not_call)
    with pytest.raises(R.StageUnresolvedError):
        gw.call("visual_rollout", [{"role": "user", "content": "see image?"}])

    doc = gw.validate_models()
    assert doc["skipped_null"] == ["visual_rollout"]
    assert len(doc["results"]) == 2
    # 视觉 stage 被跳过：raw 传输里不应出现图像请求
    assert not any(
        any(isinstance(b, dict) and b.get("type") == "image"
            for b in s["json"]["messages"][0]["content"])
        for s in sent)


# ---------- 8. 视觉 32x32 纯红：断言 red + PNG 尺寸 ----------

def test_vision_red_image_32px(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    sent: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_fake_raw(sent), sleep=lambda s: None)
    rec = gw.vision_check()
    assert rec["red_assert"] is True and rec["http_status"] == 200
    assert rec["answer"].strip().lower() == "red"
    assert rec["min_edge_px"] == 14
    # 发出去的确为 32x32 PNG（IHDR 宽高），不是任意伪造串
    vis_req = [s for s in sent
               if any(isinstance(b, dict) and b.get("type") == "image"
                      for b in s["json"]["messages"][0]["content"])][0]
    block = vis_req["json"]["messages"][0]["content"][0]
    png = base64.b64decode(block["source"]["data"])
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (32, 32)

    # n2 词边界锁定：回答 "tired"（含子串 red）必须判失败，禁止子串假绿；
    # 回答 "red."（带标点/词边界）必须判成功。
    def _answer(raw_answer):
        def _post(url, headers=None, json=None, timeout=None):
            is_vision = any(isinstance(b, dict) and b.get("type") == "image"
                            for b in json["messages"][0]["content"])
            body = _msg_body(json["model"], raw_answer if is_vision else "pong")
            return types.SimpleNamespace(
                status_code=200, json=lambda: body,
                text=json_dumps(body), headers={})
        return _post

    gw_tired = R.ModelGateway(stage_models_path=map_path,
                              raw_transport=_answer("I am tired today"),
                              sleep=lambda s: None)
    with pytest.raises(R.GatewayError, match="不含 red/红"):
        gw_tired.vision_check()
    gw_dot = R.ModelGateway(stage_models_path=map_path,
                            raw_transport=_answer("red."),
                            sleep=lambda s: None)
    assert gw_dot.vision_check()["red_assert"] is True


# ---------- 9. 小于 14px：服务端 400 必须带着 14px 提示落成不可重试错误 ----------

def test_vision_under_14px_error(tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    body = {"type": "error", "error": {"type": "invalid_request_error",
                                       "code": "InvalidParameter",
                                       "message": "Image dimensions are too small"}}

    def _post(url, headers=None, json=None, timeout=None):
        return types.SimpleNamespace(status_code=400,
                                    json=lambda: body, text=json_dumps(body), headers={})

    gw = R.ModelGateway(stage_models_path=map_path,
                        raw_transport=_post, sleep=lambda s: None)
    with pytest.raises(R.NonRetryableError, match="14"):
        gw.vision_check(size=8)


# ---------- 10. 429 Retry-After 后成功（litellm 运行期路径） ----------

def _rl_exc(text="429 RateLimitExceeded", response=None):
    return RateLimitError(text, "anthropic", "glm-5.3-flash", response)


def test_429_retry_after_then_success(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    fake_resp = types.SimpleNamespace(
        headers={"retry-after": "3"},
        text="429 RateLimitExceeded please retry after 3s")
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
        model="glm-5-3-flash",
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=2))
    calls = {"n": 0}
    sleeps = []

    def fake_completion(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _rl_exc(response=fake_resp)
        return resp

    monkeypatch.setattr(R.litellm, "completion", fake_completion)
    gw = R.ModelGateway(stage_models_path=map_path, sleep=sleeps.append,
                        transport=fake_completion)
    text_out, usage = gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert text_out == "ok" and usage == (5, 2)
    assert calls["n"] == 3
    assert sleeps[0] == 3 and sleeps[1] == 3


# ---------- 11. 配额文本 -> 长熔断，窗口内不再触网 ----------

def test_quota_error_long_circuit_pauses(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)

    def boom(**kwargs):
        raise _rl_exc("429 QuotaExceeded: insufficient quota, 欠费")

    monkeypatch.setattr(R.litellm, "completion", boom)
    gw = R.ModelGateway(stage_models_path=map_path, sleep=lambda s: None, transport=boom)
    with pytest.raises(R.GatewayPausedError):
        gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    state = json.loads((tmp_path / "state" / "quota_pause.json").read_text())
    assert state["status"] == "PAUSED_WAITING_QUOTA"
    assert state["stage"] == "coding_debug"
    assert state["resume_at_epoch"] > state["paused_at_epoch"]

    def must_not_call(**kwargs):
        raise AssertionError("paused 期间不应调用 completion")

    gw2 = R.ModelGateway(stage_models_path=map_path, transport=must_not_call)
    with pytest.raises(R.GatewayPausedError):
        gw2.call("coding_debug", [{"role": "user", "content": "hi"}])


# ---------- 12. 400 不可重试 ----------

def test_bad_request_is_non_retryable(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    sleeps = []
    # v4.1 m10：关键字构造（1.99.4 wheel 实证签名，两族顺序见 §18 第 9 条）
    monkeypatch.setattr(
        R.litellm, "completion",
        lambda **kw: (_ for _ in ()).throw(
            BadRequestError("400 bad", model="glm-5.3-flash",
                            llm_provider="anthropic", response=None)))
    gw = R.ModelGateway(stage_models_path=map_path, sleep=sleeps.append)
    with pytest.raises(R.NonRetryableError):
        gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert sleeps == []


# ---------- 13. 5xx 耗尽且不产生 pause 文件 ----------

def test_5xx_exhausts_without_pause_state(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)

    def boom(**kw):
        raise InternalServerError("500 upstream down", "anthropic", "glm-5.3-flash", None)

    monkeypatch.setattr(R.litellm, "completion", boom)
    gw = R.ModelGateway(stage_models_path=map_path, max_attempts=2,
                        sleep=lambda s: None, transport=boom)
    with pytest.raises(R.GatewayError):
        gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert not (tmp_path / "state" / "quota_pause.json").exists()


# ---------- 14. 401/403 零重试立即失败 ----------

def test_401_403_immediate_non_retryable(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    sleeps = []
    # wheel 实证：PermissionDeniedError(403) 经 openai APIStatusError 构造时会解引用
    # response.request / response.status_code / response.headers，response=None 会在
    # “构造异常”时就抛 AttributeError（AuthenticationError 的 401 基类容忍 None）。
    # 真实 401/403 必带响应，这里给最小假响应，两个异常共用。
    fake_4xx = types.SimpleNamespace(request=object(), status_code=403, headers={})
    for exc_cls in (AuthenticationError, PermissionDeniedError):
        def boom(*, _c=exc_cls, **kw):
            raise _c("401/403 not permitted", "anthropic", "glm-5.3-flash", fake_4xx)
        monkeypatch.setattr(R.litellm, "completion", boom)
        gw = R.ModelGateway(stage_models_path=map_path, sleep=sleeps.append,
                            transport=boom)
        with pytest.raises(R.NonRetryableError):
            gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert sleeps == []
    assert not (tmp_path / "state" / "quota_pause.json").exists()


# ---------- 15. 脱敏（Bearer / x-api-key 大小写两形态 + token 前缀分支） ----------

def test_redact():
    token = "redacted-test-token-0001"
    out = R.redact(f"Authorization: Bearer {token} tail")
    assert token not in out and "REDACTED" in out
    out2 = R.redact(f"x-api-key: {token}")
    assert token not in out2 and "REDACTED" in out2
    # n1：IGNORECASE——全大写形态同样必须脱敏
    out3 = R.redact(f"AUTHORIZATION: BEARER {token} tail")
    assert token not in out3 and "REDACTED" in out3
    out4 = R.redact(f"X-API-KEY: {token}")
    assert token not in out4 and "REDACTED" in out4
    assert R.redact("no secret here") == "no secret here"


# ---------- 16. m9：信封 type/code 明示瞬时限流，即使 message 含 quota 字样也不熔断 ----------

def test_structured_rate_limit_type_beats_quota_text(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    quota_body = {"type": "error", "error": {
        "type": "rate_limit_exceeded",
        "code": "too_many_requests",
        "message": "insufficient quota for this minute, retry later"}}
    fake_resp = types.SimpleNamespace(
        headers={"retry-after": "3"},
        text=json.dumps(quota_body, ensure_ascii=False))
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
        model="glm-5-3-flash",
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=2))
    calls = {"n": 0}
    sleeps = []

    def fake_completion(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            # Family A 实证签名：(message, llm_provider, model, response)
            raise RateLimitError(
                "429 rate limited", "anthropic", "glm-5.3-flash", fake_resp)
        return resp

    monkeypatch.setattr(R.litellm, "completion", fake_completion)
    gw = R.ModelGateway(stage_models_path=map_path, sleep=sleeps.append,
                        transport=fake_completion)
    text_out, usage = gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert text_out == "ok" and usage == (5, 2)
    assert calls["n"] == 3
    # Retry-After: 3 被两跳严格尊重（不是指数退避的近似值）
    assert sleeps == [3, 3]
    # 瞬时限流：绝不允许写长熔断状态
    assert not (tmp_path / "state" / "quota_pause.json").exists()


# ---------- 17. m3：5xx 两传输层都先尊重 Retry-After，无头才指数退避 ----------

def test_5xx_retry_after_honored_both_transports(tmp_path, monkeypatch):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)

    # (a) litellm 运行期路径：ServiceUnavailableError（family A）带头 Retry-After: 7
    fake_resp = types.SimpleNamespace(headers={"Retry-After": "7"},
                                      text="503 service unavailable")
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
        model="glm-5-3-flash",
        usage=types.SimpleNamespace(input_tokens=4, output_tokens=1))
    n = {"i": 0}
    sleeps_a = []

    def fake_completion(**kwargs):
        n["i"] += 1
        if n["i"] == 1:
            raise ServiceUnavailableError(
                "503 unavailable", "anthropic", "glm-5.3-flash", fake_resp)
        return resp

    monkeypatch.setattr(R.litellm, "completion", fake_completion)
    gw = R.ModelGateway(stage_models_path=map_path, sleep=sleeps_a.append,
                        transport=fake_completion)
    text_out, usage = gw.call("coding_debug", [{"role": "user", "content": "hi"}])
    assert text_out == "ok" and usage == (4, 1)
    assert sleeps_a == [7], f"litellm 路径必须严格尊重 Retry-After: {sleeps_a}"

    # (b) 直 POST 路径：503 带头 retry-after: 9，第二跳 200
    def _post(url, headers=None, json=None, timeout=None):
        model = json["model"]
        if not getattr(_post, "seen", False):
            _post.seen = True
            body = {"type": "error", "error": {"type": "service_unavailable",
                                               "code": "unavailable", "message": "retry"}}
            return types.SimpleNamespace(status_code=503, json=lambda: body,
                                         text=json_dumps(body),
                                         headers={"retry-after": "9"})
        return types.SimpleNamespace(
            status_code=200, json=lambda: _msg_body(model, "pong"),
            text=json_dumps(_msg_body(model, "pong")), headers={})

    sleeps_b = []
    gw2 = R.ModelGateway(stage_models_path=map_path, raw_transport=_post,
                         sleep=sleeps_b.append)
    rec = gw2.ping_text("macro_ideation")
    assert rec["http_status"] == 200
    assert sleeps_b == [9], f"直 POST 路径必须严格尊重 retry-after: {sleeps_b}"


# ---------- 18. m4：validate 中途失败写 FAILURE.json（新时间戳目录），latest 不移动 ----------

def test_validate_failure_writes_failure_json_latest_unmoved(tmp_path, monkeypatch):
    import time as _time
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    root = tmp_path / "runs" / "model_validate"

    # 时间戳固定且递增，杜绝同秒目录碰撞让本测产生偶发
    seq = iter(["20260101-000000", "20260101-000001"])
    real_strftime = _time.strftime

    def fake_strftime(fmt, t=None):
        if fmt == "%Y%m%d-%H%M%S":
            return next(seq)
        return real_strftime(fmt, t)

    monkeypatch.setattr(R.time, "strftime", fake_strftime)

    # 第一次全绿：latest 指向绿目录
    gw_ok = R.ModelGateway(stage_models_path=map_path,
                           raw_transport=_fake_raw([]), sleep=lambda s: None)
    gw_ok.validate_models()
    green = (root / "latest").resolve()
    assert (green / "result.json").is_file()
    assert json.loads((green / "result.json").read_text())["status"] == "OK"

    # 第二次：macro 成功、coding 404（vision 不应被触达）
    def _post(url, headers=None, json=None, timeout=None):
        if json["model"] == DEFAULT_IDS["coding_debug"]:
            body = {"type": "error", "error": {"type": "not_found_error",
                                               "code": "model_not_found",
                                               "message": "model or endpoint not found"}}
            return types.SimpleNamespace(status_code=404, json=lambda: body,
                                         text=json_dumps(body), headers={})
        return _fake_raw([])(url, headers=headers, json=json, timeout=timeout)

    gw_bad = R.ModelGateway(stage_models_path=map_path, raw_transport=_post,
                            sleep=lambda s: None)
    with pytest.raises(R.NonRetryableError):
        gw_bad.validate_models()

    # latest 仍然指向旧的全绿证据，绝不指向失败目录
    assert (root / "latest").resolve() == green
    fails = sorted(root.glob("*/FAILURE.json"))
    assert len(fails) == 1, f"失败目录应恰好一个 FAILURE.json：{fails}"
    assert fails[0].parent != green
    doc = json.loads(fails[0].read_text())
    assert doc["status"] == "FAIL"
    assert doc["error_type"] == "NonRetryableError"
    assert [r["stage"] for r in doc["results"]] == ["macro_ideation"]
    # 绿目录里不允许混入失败文件
    assert not (green / "FAILURE.json").exists()
