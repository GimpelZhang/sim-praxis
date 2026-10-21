"""response_format 强制 JSON 诊断探针的 hermetic 单测（0 付费、0 触网）。

覆盖（镜像 tests/milestone2/test_diag_max_tokens.py 脚本层纪律）：
- 裸跑付费守卫 exit 2 零触网；参数非法（票号/线程字符集、max_tokens
  区间）exit 2 且在构造网关之前；
- A 段静态纯函数：真调 litellm 本地转换层（不触网），钉死两形态 wire
  形状——json_object 被静默丢弃（升级 litellm 即报警），json_schema 被
  翻译成强制 tool call（tools/tool_choice/json_mode）；
- **litellm 1.99.4 canary（离线、零触网）**：真跑 AnthropicConfig
  .transform_response(json_mode=True) 三态方舟响应——
  死通道钉死（"original_response" 不进 _hidden_params；tool/text 两态
  choice 级字面相同：tool_calls=None/finish="stop"；input=None 实测产出
  字面串 "null" 而非 Python None，与简报预期不符，按实测钉死并注释）。
  canary 一旦变红说明 litellm 修复了 hidden_params 通道，应回退到更省
  的 hidden_params 取证；
- HTTPHandler.post 拦截器（假 HTTPHandler 类）：正常/异常路径 finally
  复原、异常不吞、不可重入、多 attempt final 标记移动、非 200/坏 JSON/
  异形块只取布尔、dict 与 data 序列化串两种请求形态四布尔、哨兵泄密
  红线（input/text/请求体都含 ZZSECRETZZ，元数据序列化后零泄漏）；
- transport 闭包：单点注入 json_schema 常量形态，其余键原样透传，预置
  response_format 触发断言；拦截器在 completion() 期间装上、finally 复原；
  **零 socket 真 litellm.completion e2e**：打桩 HTTPHandler.post 回放
  本地 httpx.Response（Ark tool_use / 纯文本两态），闭包元数据
  tool_channel True/False、请求侧四布尔、attempts 全对；
- _verify_envelope：合法压力信封 enforced=True 且五 marker 齐全；裸控制
  字符/缺 chunk/非法 Python/缺 marker 短码均 enforced=False 且不抛；
  code_over_max 仅标志不门控（>8000 合法压力包仍 enforced=True）；
- reason 分类器与 main 退出语义：tool 通道+内容合规→exit 0
  reason=tool_channel_delivered_n1（n=1 直接证据，非数学证明强制力）；
  text 通道内容合规→11 text_compliant_voluntary；tool 通道内容不过→11
  content_failed_enforcement；input=None→11 tool_input_null；无通道
  证据→11 unattestable；200 后异常→11 exception_after_200:<ExcType>；
  连接异常无 200→exit 1 无证据；
- **单发**：ModelGateway(max_attempts=1)（G-12 退避不得在探针复活）；
- 明确拒绝（NonRetryableError，如 400）：脱敏证据 exit 10，附拦截状态；
- 红线：双根唯一提及、graph/runner 无名、AST 双查禁 import graph、
  无模型覆盖参数、deepseek 模型名 AST 计数=1 且不传给网关、main 的
  gateway.call 无 model/response_format kwarg；monkeypatch 靶点必须是
  AST 钉死的字面串 "...HTTPHandler.post"，代码里 HTTPHandler 只允许以
  该串出现（不得出现 AsyncHTTPHandler/裸类名，防误包异步路径）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests

import orchestrator.gateway.router as R
from orchestrator.gateway.quota_ledger import QuotaLedger

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _REPO_ROOT / "scripts" / "phase2" / "diag_json_mode.py"
BATCH = "T6-B8-probejson"
THREAD = "t6-genesis-ascent-20261016a"
TARGET_PATH = "genesis_humanoid_dr.py"
# 非真密钥形态（scan_secrets 严口径不命中），仅作证据不回显凭据的哨兵。
TOKEN = "diag-json-test-key-redacted-0001"
# Bearer 宽脱敏形态：redact_text 必剥（redact_only），严口径扫描不命中。
BEARER_SECRET = "Bearer diagjsontestredacted0001"
HOST = "ark.example.invalid"
# 与脚本红线常量同源的期望串（AST 测试另从源码取，这里只作相等断言）。
_EXPECTED_POST_TARGET = \
    "litellm.llms.custom_httpx.http_handler.HTTPHandler.post"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    # 假网关/本地回放吃掉全部网络，凭据/URL 仅用于断言证据不回显。
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", TOKEN)
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL", f"https://{HOST}/api/coding")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    return tmp_path


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "phase2_diag_json_mode", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 压力 code 构造：合法 Python、含全部转义压力特征、长度 4000–8000
# ---------------------------------------------------------------------------

_STRESS_HEADER = r'''"""具身人形驱动压力模块（三引号中文 docstring）。

三引号块内含换行与引号：他说"启动"，她答'就绪'。
"""
import re

WIN_PATH = r"C:\Users\bot\new\task"
ESCAPED = "tab\there\nnewline and backslash \\ end"
PATTERN = re.compile(r"\d+\b\t\n")
NAME = "双引号字符串"
OTHER = '单引号字符串'


# 中文注释：Windows 路径、正则反斜杠与 JSON 转义压力
def parse_win_path(p):
    # 中文注释：按反斜杠拆分
    return p.split("\\")


def drive_step(angle, length):
    # 中文注释：角度归一化
    return angle % 360, length * 2
'''


def _stress_code(n_funcs=60) -> str:
    """生成合法 Python：header 带全部 marker，函数填充长度。

    n_funcs=60 ≈ 5.3k（正常压力窗 4000–8000）；n_funcs=120 ≈ 9.6k
    （超窗包：enforced 仍应 True，code_over_max 仅作证据标志）。
    """
    funcs = "\n".join(
        f"def func_{i}(value):\n"
        f"    # 中文函数注释 {i}：反斜杠 \\ 与引号 \" ' 压力\n"
        f"    return value + {i}\n"
        for i in range(n_funcs))
    return _STRESS_HEADER + funcs


def _envelope_json(code: str | None = None, **fields) -> str:
    code = _stress_code() if code is None else code
    chunk = {"path": TARGET_PATH, "index": 3, "total": 4, "code": code}
    chunk.update(fields)
    return json.dumps({"chunk": chunk}, ensure_ascii=False)


# 方舟 Anthropic-Messages 原始响应形态（本地构造，零 socket）。
def _ark_tool_body(inp, out_tokens=99, stop="tool_use",
                   name="json_tool_call"):
    return {"id": "msg_1", "type": "message", "role": "assistant",
            "model": "deepseek-v4.1-flash",
            "content": [{"type": "tool_use", "id": "tu_1",
                         "name": name, "input": inp}],
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 31, "output_tokens": out_tokens}}


def _ark_text_body(text, out_tokens=120):
    return {"id": "msg_2", "type": "message", "role": "assistant",
            "model": "deepseek-v4.1-flash",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 31, "output_tokens": out_tokens}}


class _FakeResp:
    """最小 httpx.Response 替身：status_code + .json()。"""

    def __init__(self, status, body=200):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


def _install_post(monkeypatch, body=None, *, resp=None, status=200):
    """打桩真实 HTTPHandler.post 回放本地 httpx.Response（零 socket）。"""
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    def fake_post(self_inst, url, **kwargs):
        request = httpx.Request(
            "POST", f"https://{HOST}/api/coding/v1/messages")
        if resp is not None:
            returned = resp
        else:
            returned = httpx.Response(status, json=body, request=request)
        return returned

    monkeypatch.setattr(HTTPHandler, "post", fake_post)
    return HTTPHandler


class _FakeGateway:
    """替身只实现 call()：记录 init/call 参数，返回 (text, (in, out))。"""
    seen: dict = {}
    in_tokens = 31
    out_tokens = 5200
    raises: Exception | None = None
    text: str | None = None

    def __init__(self, *, ledger=None, max_attempts=None, transport=None,
                 **kw):
        type(self).seen["ledger"] = ledger
        type(self).seen["init_kwargs"] = {
            "max_attempts": max_attempts, "transport": transport, "extra": kw}

    def call(self, stage, messages, *, max_tokens, temperature, timeout,
             **extra):
        type(self).seen.update(
            stage=stage, messages=messages, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout, call_extra=extra)
        if self.raises is not None:
            raise self.raises
        return (self.text if self.text is not None else ""), (
            self.in_tokens, self.out_tokens)


def _fake(*, text=None, out_tokens=5200, exc=None):
    return type("FG", (_FakeGateway,),
                {"seen": {}, "text": text, "out_tokens": out_tokens,
                 "raises": exc})


class _TransportGateway(_FakeGateway):
    """经真实 transport 闭包（真 litellm.completion + 打桩 post）的网关替身。

    ledger/billing_context 仍走 main 的生产路径；只绕过 ModelGateway
    自身（preflight 在闭包 e2e 里不触网，批准票属于付费实跑纪律）。
    """

    def call(self, stage, messages, *, max_tokens, temperature, timeout,
             **extra):
        type(self).seen.update(
            stage=stage, messages=messages, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout, call_extra=extra)
        resp = self.seen["init_kwargs"]["transport"](
            model="anthropic/deepseek-v4.1-flash",
            api_base=f"https://{HOST}/api/coding",
            api_key=TOKEN, messages=messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        message = resp.choices[0].message
        usage = resp.usage
        return message.content, (usage.prompt_tokens,
                                 usage.completion_tokens)


def _run_main(mod, monkeypatch, out, gateway_cls):
    monkeypatch.setattr(mod, "ModelGateway", gateway_cls)
    return mod.main(["--batch-id", BATCH, "--thread", THREAD,
                     "--max-tokens", "32768", "--out", str(out)])


# ---------------------------------------------------------------------------
# 1. 付费守卫 + 参数校验：裸跑/非法参数零触网 exit 2
# ---------------------------------------------------------------------------

def test_script_guard_bare_run_exits_2_before_gateway(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    mod = _load_script()

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("守卫必须在构造网关之前 exit 2（零触网）")

    monkeypatch.setattr(mod, "ModelGateway", _Boom)
    rc = mod.main(["--batch-id", BATCH])
    assert rc == mod.EXIT_GUARD


@pytest.mark.parametrize("argv", [
    ["--batch-id", "bad/batch"],                       # 票号字符集
    ["--batch-id", BATCH, "--thread", "bad thread"],   # 线程字符集
    ["--batch-id", BATCH, "--max-tokens", "16384"],    # 下界：等于现网即非法
    ["--batch-id", BATCH, "--max-tokens", "65537"],    # 超探针上限
])
def test_script_invalid_args_exits_2(monkeypatch, env, argv):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("参数非法必须在构造网关之前 exit 2")

    monkeypatch.setattr(mod, "ModelGateway", _Boom)
    assert mod.main(argv) == mod.EXIT_GUARD


# ---------------------------------------------------------------------------
# 2. A 段静态纯函数：真 litellm 本地转换，零触网
# ---------------------------------------------------------------------------

def test_static_transform_shapes_zeronet_pins_litellm_behavior():
    mod = _load_script()
    # 纯函数可重复。
    shapes = mod._static_transform_shapes()
    assert shapes == mod._static_transform_shapes()
    assert isinstance(shapes["litellm"], str) and shapes["litellm"]
    assert shapes["model"] == "deepseek-v4.1-flash"

    jo = shapes["shapes"]["json_object"]
    # 无 schema 形态：参数被客户端静默全丢弃（LEAD 离线实测事实，升级即报警）。
    assert jo["wire_has_tools"] is False
    assert jo["wire_has_tool_choice"] is False
    assert jo["wire_has_output_format"] is False
    assert jo["json_mode"] is None
    assert jo["param_keys"] == ["max_tokens", "temperature"]

    js = shapes["shapes"]["json_schema"]
    # json_schema 形态：翻译成强制 tool call。
    assert js["wire_has_tools"] is True
    assert js["wire_has_tool_choice"] is True
    assert js["wire_has_output_format"] is False
    assert js["json_mode"] is True
    assert "raised" not in jo and "raised" not in js

    # 直接跑转换层钉死 tool_choice/tool 名与 schema 透传内容。
    import orchestrator.gateway.router  # noqa: F401  typing 垫片先行
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    wire = AnthropicConfig().map_openai_params(
        non_default_params={"max_tokens": 32768, "temperature": 0,
                            "response_format": mod.JSON_SCHEMA_RF},
        optional_params={}, model="deepseek-v4.1-flash", drop_params=True)
    assert wire["tool_choice"] == {"name": "json_tool_call", "type": "tool"}
    # 工具名与脚本内常量同源（单一源；tool_choice 整体形状逐字钉翻译层）。
    assert wire["tools"][0]["name"] == mod.EXPECTED_TOOL_NAME
    assert wire["tools"][0]["input_schema"] == mod.RESPONSE_SCHEMA
    assert wire["json_mode"] is True


def test_response_schema_constant_structure():
    mod = _load_script()
    schema = mod.RESPONSE_SCHEMA
    assert schema["type"] == "object" and schema["required"] == ["chunk"]
    chunk = schema["properties"]["chunk"]
    assert chunk["type"] == "object"
    assert chunk["required"] == ["path", "index", "total", "code"]
    props = chunk["properties"]
    assert props["path"] == {"type": "string"}
    assert props["index"] == {"type": "integer"}
    assert props["total"] == {"type": "integer"}
    assert props["code"] == {"type": "string"}
    assert mod.JSON_SCHEMA_RF == {
        "type": "json_schema",
        "json_schema": {"name": "chunk_envelope", "schema": schema}}


# ---------------------------------------------------------------------------
# 3. litellm 1.99.4 canary：真 transform_response 三态钉死死通道事实
# ---------------------------------------------------------------------------

class _NoopLog:
    """transform_response 要求 logging_obj 有 post_call；其余属性全 no-op。"""

    def post_call(self, *a, **k):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _transform_raw(mod, body):
    """对本地构造的方舟原始响应跑真实 litellm 转换（json_mode=True）。"""
    import orchestrator.gateway.router  # noqa: F401  typing 垫片先行
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    from litellm.types.utils import ModelResponse
    request = httpx.Request(
        "POST", f"https://{HOST}/api/v1/messages")
    raw = httpx.Response(200, json=body, request=request)
    return AnthropicConfig().transform_response(
        "deepseek-v4.1-flash", raw, ModelResponse(), _NoopLog(),
        {}, [{"role": "user", "content": "hi"}], {}, {}, None,
        json_mode=True)


def test_canary_transform_response_dead_hidden_params_channel():
    """fresh reviewer 真实管线 harness + LEAD 离线复现事实的离线钉死。

    三态：tool_use+input 对象 / 纯文本 / tool_use input=None。
    (i)  "_hidden_params" 不含 original_response（L2417 空 dict → L2492
         写旧 dict → L2510 整体替换的死赋值，1.99.4 非流式路径）；
    (ii) tool/text 两态 choice 级 message.content 字面相同——同 payload
         经 tool_use 重序列化与纯文本携带不可分辨；
    (iii) 两态 tool_calls 均 None、finish_reason 均 "stop"（choice 级
         三字段无法分辨强制力，只能在 HTTPHandler 原始块层取证）；
    (iv) input=None：实测 content 是字面**字符串 "null"**（truthy 非空，
         不触发网关 G-12 空 200；内容核验 json.loads→None 必失败），
         **不是**简报预期的 Python None——按实测钉死，分歧已上报 LEAD。
    canary 变红即 litellm 修了通道：回退到更省的 hidden_params 取证。
    """
    mod = _load_script()
    payload = {"chunk": {"path": TARGET_PATH, "index": 3, "total": 4,
                         "code": "x = 1\n"}}
    reserialized = json.dumps(payload, ensure_ascii=False)

    out_tool = _transform_raw(mod, _ark_tool_body(payload))
    out_text = _transform_raw(mod, _ark_text_body(reserialized))
    out_null = _transform_raw(mod, _ark_tool_body(None))

    for out in (out_tool, out_text, out_null):
        # (i) 死通道：hp 仅这两个白名单键。
        hidden = getattr(out, "_hidden_params", {})
        assert "original_response" not in hidden
        assert set(hidden) <= {"additional_headers",
                               "provider_specific_fields"}

    msg_t, msg_x, msg_n = (out_tool.choices[0].message,
                           out_text.choices[0].message,
                           out_null.choices[0].message)
    # (ii) tool 重序列化与 text 直携带字面相同。
    assert msg_t.content == msg_x.content == reserialized
    # (iii) choice 级两态同形（剥离证据）。
    assert msg_t.tool_calls is None and msg_x.tool_calls is None
    assert msg_n.tool_calls is None
    assert out_tool.choices[0].finish_reason == "stop"
    assert out_text.choices[0].finish_reason == "stop"
    assert out_null.choices[0].finish_reason == "stop"
    # (iv) 实测分歧点：input=None → 字面串 "null"（非 Python None）。
    assert msg_n.content == "null" and isinstance(msg_n.content, str)


def test_post_target_resolver_pins_sync_class(monkeypatch):
    mod = _load_script()
    assert mod.HTTPHANDLER_POST_TARGET == _EXPECTED_POST_TARGET
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    assert mod._resolve_handler_class() is HTTPHandler

    # 靶点被改坏（非 .post 后缀 / 非 HTTPHandler 类）必须 RuntimeError，
    # 宁停勿错包到异步或其他类。
    for bad in ("litellm.llms.custom_httpx.http_handler.Other.post",
                "litellm.llms.custom_httpx.http_handler.HTTPHandler.send"):
        monkeypatch.setattr(mod, "HTTPHANDLER_POST_TARGET", bad)
        with pytest.raises(RuntimeError):
            mod._resolve_handler_class()


# ---------------------------------------------------------------------------
# 4. HTTPHandler.post 拦截器：假 HTTPHandler 类，全机制单测
# ---------------------------------------------------------------------------

def _make_handler_class(scripted):
    """scripted: 顺序脚本，元素为 _FakeResp 或 Exception 实例。"""
    state = {"calls": 0, "kwargs": []}

    class FakeHTTPHandler:
        def post(self, url, **kwargs):
            i = state["calls"]
            state["calls"] += 1
            state["kwargs"].append(kwargs)
            step = scripted[i]
            if isinstance(step, BaseException):
                raise step
            return step

    return FakeHTTPHandler, state


def _tool_resp(status=200, inp=None):
    inp = {"chunk": {"code": "x=1\n"}} if inp is None else inp
    return _FakeResp(status, _ark_tool_body(inp))


def _text_resp(status=200, text='{"chunk": null}'):
    return _FakeResp(status, _ark_text_body(text))


def test_interceptor_success_tool_and_text_meta():
    mod = _load_script()
    for resp, expect_tool in ((_tool_resp(), True), (_text_resp(), False)):
        cls, state = _make_handler_class([resp])
        orig = cls.post
        ic = mod._PostInterceptor(cls).install()
        req_json = {
            "tools": [{"name": "json_tool_call"}],
            "tool_choice": {"type": "tool", "name": "json_tool_call"},
            "messages": []}
        try:
            got = cls().post("u", json=req_json)
        finally:
            ic.restore()
        assert got is resp and cls.post is orig
        ev = ic.evidence(got)
        assert ev["http_status"] == 200 and ev["parse_ok"] is True
        assert ev["tool_channel"] is expect_tool
        assert ev["tool_input_null"] is False
        assert ev["attempts"] == [{
            "n": 1, "http_status": 200, "parse_ok": True,
            "tool_channel": expect_tool, "tool_input_null": False,
            "blocks": ([{"type": "tool_use", "name": "json_tool_call"}]
                       if expect_tool else [{"type": "text", "name": None}]),
            "final": True}]
        # 非 litellm 响应对象：choice 级三字段全 None（结构恒在）。
        assert ev["choice_level"] == {
            "tool_calls_present": None, "tool_call_names": None,
            "finish_reason": None}
        # 请求侧四布尔（json= dict 形态）。
        assert ic.request_meta == {
            "wire_req_has_tools": True,
            "wire_req_tool_names": ["json_tool_call"],
            "wire_req_tool_choice": True,
            "wire_req_has_response_format": False}
        # wrapper 只读不改：url 位置参之外，原始 kwargs 原样到达。
        assert state["kwargs"] == [{"json": req_json}]


@pytest.mark.parametrize("resp,check", [
    # 非 200 但 JSON 可解析：只记状态 + 块（4xx 方舟错误体无 content）。
    (_FakeResp(429, {"error": {"type": "rate"}}),
     lambda e: e["http_status"] == 429 and e["parse_ok"] is True
     and e["blocks"] == [] and e["tool_channel"] is False),
    # .json() 抛错：parse_ok False，不炸 wrapper。
    (_FakeResp(500, ValueError("no json")),
     lambda e: e["http_status"] == 500 and e["parse_ok"] is False),
    # JSON 但非 dict（malformed body → parse_ok False，不可证通道）。
    (_FakeResp(200, ["not", "dict"]),
     lambda e: e["parse_ok"] is False and e["blocks"] == []),
    # dict 但无 content：parse_ok True、无块。
    (_FakeResp(200, {"id": "x"}),
     lambda e: e["parse_ok"] is True and e["blocks"] == []),
    # 异形块：非 dict 跳过；tool_use input=None 置 null 标志；缺型 text。
    (_FakeResp(200, {"content": [
        {"type": "tool_use", "name": "t", "input": None},
        "junk", 5, {"type": "text"}]}),
     lambda e: e["tool_channel"] is True and e["tool_input_null"] is True
     and e["blocks"] == [{"type": "tool_use", "name": "t"},
                          {"type": "text", "name": None}]),
    # tool_use 块缺 input 键：不算 null（缺失 ≠ null）。
    (_FakeResp(200, {"content": [{"type": "tool_use", "name": "t"}]}),
     lambda e: e["tool_channel"] is True and e["tool_input_null"] is False),
    # 无 status_code 属性的怪响应：不炸，status None。
    (SimpleNamespace(json=lambda: {"content": []}),
     lambda e: e["http_status"] is None and e["parse_ok"] is True
     and e["blocks"] == []),
])
def test_interceptor_non_200_bad_json_weird_blocks(resp, check):
    mod = _load_script()
    cls, _ = _make_handler_class([resp])
    ic = mod._PostInterceptor(cls).install()
    try:
        got = cls().post("u")
    finally:
        ic.restore()
    assert check(ic.evidence(got))


def test_interceptor_sentinel_never_leaks_into_metadata():
    mod = _load_script()
    secret = "ZZSECRETZZ-7f3a-code-body"
    # input 体、text 体、请求体三处都埋哨兵；元数据序列化后零泄漏。
    body = {"content": [
        {"type": "tool_use", "id": "t", "name": "json_tool_call",
         "input": {"chunk": {"code": secret}}},
        {"type": "text", "text": secret}]}
    cls, _ = _make_handler_class([_FakeResp(200, body)])
    ic = mod._PostInterceptor(cls).install()
    try:
        got = cls().post("u", json={"messages": [{"content": secret}],
                                   "tools": [{"name": "json_tool_call",
                                              "input_schema": secret}],
                                   "tool_choice": {"name": secret}})
    finally:
        ic.restore()
    blob = json.dumps(
        {"wire_request": ic.request_meta, "wire_response": ic.evidence(got)},
        ensure_ascii=False)
    assert secret not in blob
    # blocks 只有 type/name 两个键。
    for block in ic.evidence(got)["blocks"]:
        assert set(block) == {"type", "name"}


def test_interceptor_multiple_attempts_final_moves():
    mod = _load_script()
    r500 = _FakeResp(500, ValueError("x"))
    r_text = _text_resp()
    r_null = _FakeResp(200, _ark_tool_body(None))
    cls, _ = _make_handler_class([r500, r_text, r_null])
    ic = mod._PostInterceptor(cls).install()
    try:
        h = cls()
        h.post("u"); got1 = h.post("u"); got2 = h.post("u")
    finally:
        ic.restore()
    assert [a["final"] for a in ic.attempts] == [False, False, True]
    assert [a["http_status"] for a in ic.attempts] == [500, 200, 200]
    assert ic.captured_200() is True
    ev = ic.evidence(got2)
    assert ev["tool_channel"] is True and ev["tool_input_null"] is True
    assert ev["attempts"][1]["blocks"] == [{"type": "text", "name": None}]


class _HTTPErr(Exception):
    pass


def test_interceptor_exception_with_response_recorded_and_reraised():
    mod = _load_script()
    err_resp = _FakeResp(429, {"error": "rate"})
    err = _HTTPErr("rate limited")
    err.response = err_resp
    cls, _ = _make_handler_class([err])
    orig = cls.post
    ic = mod._PostInterceptor(cls).install()
    try:
        with pytest.raises(_HTTPErr):     # 原始异常不吞
            cls().post("u")
    finally:
        ic.restore()
    assert cls.post is orig              # finally 复原
    assert ic.captured_200() is False
    item = ic.attempts[-1]
    assert item["http_status"] == 429 and item["raised"] == "_HTTPErr"
    assert item["final"] is False
    ev = ic.evidence(None)               # completion 无返回对象
    assert ev["http_status"] == 429 and ev["choice_level"] is None


def test_interceptor_exception_without_response_not_recorded():
    mod = _load_script()
    cls, _ = _make_handler_class([requests.ConnectionError("refused")])
    ic = mod._PostInterceptor(cls).install()
    try:
        with pytest.raises(requests.ConnectionError):
            cls().post("u")
    finally:
        ic.restore()
    assert ic.attempts == [] and ic.captured_200() is False
    ev = ic.evidence(None)
    assert ev["attempts"] == [] and ev["http_status"] is None
    assert ev["tool_channel"] is None


def test_interceptor_non_reentrant_and_restore_idempotent():
    mod = _load_script()
    cls, state = _make_handler_class([_text_resp(), _text_resp()])
    ic = mod._PostInterceptor(cls).install()
    try:
        cls().post("u")
        with pytest.raises(AssertionError, match="不可重入"):
            ic.install()
    finally:
        ic.restore()
    ic.restore()                          # 二次 restore 无副作用
    # 复原后 post 行为回到原样（不经 wrapper，attempts 不再增长）。
    before = len(ic.attempts)
    cls().post("u")
    assert len(ic.attempts) == before
    assert state["calls"] == 2


@pytest.mark.parametrize("payload,expected", [
    ({"json": {"tools": [{"name": "json_tool_call"}],
               "tool_choice": {"type": "tool", "name": "json_tool_call"}}},
     {"wire_req_has_tools": True, "wire_req_tool_names": ["json_tool_call"],
      "wire_req_tool_choice": True, "wire_req_has_response_format": False}),
    ({"json": {"tools": [], "tool_choice": None}},
     {"wire_req_has_tools": False, "wire_req_tool_names": [],
      "wire_req_tool_choice": False, "wire_req_has_response_format": False}),
    ({"json": {"response_format": {"type": "json_object"}}},
     {"wire_req_has_tools": False, "wire_req_tool_names": [],
      "wire_req_tool_choice": False, "wire_req_has_response_format": True}),
    # data= 序列化串形态（anthropic 同步路径真实形态）。
    ({"data": json.dumps({"tools": [{"name": "json_tool_call"}],
                          "tool_choice": {"x": 1}})},
     {"wire_req_has_tools": True, "wire_req_tool_names": ["json_tool_call"],
      "wire_req_tool_choice": True, "wire_req_has_response_format": False}),
    ({"data": b'{"tools": []}'},
     {"wire_req_has_tools": False, "wire_req_tool_names": [],
      "wire_req_tool_choice": False, "wire_req_has_response_format": False}),
    ({"data": "not-json"},
     {"wire_req_has_tools": None, "wire_req_tool_names": None,
      "wire_req_tool_choice": None, "wire_req_has_response_format": None}),
    ({},
     {"wire_req_has_tools": None, "wire_req_tool_names": None,
      "wire_req_tool_choice": None, "wire_req_has_response_format": None}),
])
def test_request_booleans_dict_and_data_forms(payload, expected):
    mod = _load_script()
    assert mod._request_booleans(payload) == expected


# ---------------------------------------------------------------------------
# 5. choice 级三字段：仅作"litellm 已剥离"对照
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory,expected", [
    (lambda: "RESP", {"tool_calls_present": None, "tool_call_names": None,
                      "finish_reason": None}),
    (lambda: SimpleNamespace(choices=[]),
     {"tool_calls_present": None, "tool_call_names": None,
      "finish_reason": None}),
    (lambda: SimpleNamespace(choices=[SimpleNamespace(message=None)]),
     {"tool_calls_present": None, "tool_call_names": None,
      "finish_reason": None}),
    (lambda: SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(tool_calls=None))]),
     {"tool_calls_present": False, "tool_call_names": [],
      "finish_reason": "stop"}),
    (lambda: SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="tool_calls",
        message=SimpleNamespace(tool_calls=[
            SimpleNamespace(function=SimpleNamespace(
                name="zzz_tool", arguments=None)),
            SimpleNamespace(name="json_tool_call")]))]),
     {"tool_calls_present": True,
      "tool_call_names": ["json_tool_call", "zzz_tool"],
      "finish_reason": "tool_calls"}),
    # 怪异 choices（dict 下标）触发兜底：全 None，不抛。
    (lambda: SimpleNamespace(choices={}),
     {"tool_calls_present": None, "tool_call_names": None,
      "finish_reason": None}),
])
def test_choice_level_meta_shapes(factory, expected):
    mod = _load_script()
    assert mod._choice_level_meta(factory()) == expected


# ---------------------------------------------------------------------------
# 6. transport 闭包：单点注入 + 拦截器生命周期 + 零 socket 真 litellm e2e
# ---------------------------------------------------------------------------

def test_json_schema_transport_injects_and_passthrough():
    mod = _load_script()
    captured: list[dict] = []
    sentinel_resp = object()

    def _fake_completion(**kwargs):
        captured.append(kwargs)
        return sentinel_resp

    send = mod._json_schema_transport(_fake_completion)
    base = {"model": "anthropic/from-yaml", "api_base": "https://example.invalid",
            "api_key": "k", "messages": [{"role": "user"}],
            "max_tokens": 32768, "temperature": 0, "timeout": 900}
    assert send(**dict(base)) is sentinel_resp   # 响应原样返回
    assert captured[0]["response_format"] == mod.JSON_SCHEMA_RF
    for key, value in base.items():
        assert captured[0][key] == value
    assert set(send.sent_keys) == set(base) | {"response_format"}
    # fake completion 不发起 post：wire_request None、attempts 空但结构齐。
    meta = send.last_meta
    assert meta["wire_request"] is None
    assert meta["wire_response"]["attempts"] == []
    # 任意对象（object()）上取不到 choices：三 None 但结构恒在。
    assert meta["wire_response"]["choice_level"] == {
        "tool_calls_present": None, "tool_call_names": None,
        "finish_reason": None}
    assert send.captured_200 is False

    # 预置 response_format 必须断言失败（防误注入/静默覆盖）。
    bad = dict(base)
    bad["response_format"] = {"type": "json_object"}
    with pytest.raises(AssertionError):
        send(**bad)

    # 默认工厂只做 litellm 导入（不触网），返回可调用闭包。
    default_send = mod._json_schema_transport()
    assert callable(default_send)
    assert default_send.sent_keys == [] and default_send.last_meta is None
    assert default_send.captured_200 is False


def test_closure_records_real_post_success_and_restores(monkeypatch):
    mod = _load_script()
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    body = _ark_tool_body({"chunk": {"code": "x=1\n"}})
    _install_post(monkeypatch, body)
    patched = HTTPHandler.post

    def _completion(**kwargs):
        # 模拟 litellm 同步出口：经当前被包的 HTTPHandler.post 发一次。
        return HTTPHandler().post("u", json={
            "tools": [{"name": "json_tool_call"}],
            "tool_choice": {"type": "tool", "name": "json_tool_call"},
            "messages": []})

    send = mod._json_schema_transport(_completion)
    resp = send(model="anthropic/x", messages=[])
    assert resp.status_code == 200
    # finally 复原到测试打桩（wrapper 已卸载：再调 attempts 不增长）。
    assert HTTPHandler.post is patched
    assert len(send.last_meta["wire_response"]["attempts"]) == 1
    HTTPHandler().post("u")
    assert len(send.last_meta["wire_response"]["attempts"]) == 1
    assert send.captured_200 is True


def test_closure_exception_after_200_records_restores_and_reraises(monkeypatch):
    mod = _load_script()
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    _install_post(monkeypatch, _ark_text_body("{}"))
    patched = HTTPHandler.post

    def _completion(**kwargs):
        HTTPHandler().post("u", data=json.dumps({
            "tools": [{"name": "json_tool_call"}],
            "tool_choice": {"type": "tool"}, "messages": []}))
        raise RuntimeError("empty 200")

    send = mod._json_schema_transport(_completion)
    with pytest.raises(RuntimeError, match="empty 200"):
        send(model="anthropic/x", messages=[])
    assert HTTPHandler.post is patched          # 异常路径也复原
    assert send.captured_200 is True
    wr = send.last_meta["wire_response"]
    assert wr["attempts"][-1]["http_status"] == 200
    assert wr["attempts"][-1]["final"] is True
    assert send.last_meta["wire_request"]["wire_req_has_tools"] is True

    # 无 200（post 回放 500）则 captured_200 False。
    _install_post(monkeypatch, None,
                  resp=_FakeResp(500, ValueError("x")))

    def _completion_500(**kwargs):
        HTTPHandler().post("u")
        raise RuntimeError("500 then boom")

    send2 = mod._json_schema_transport(_completion_500)
    with pytest.raises(RuntimeError):
        send2(model="anthropic/x", messages=[])
    assert send2.captured_200 is False


@pytest.mark.parametrize("form", ["tool", "text"])
def test_closure_real_litellm_zero_socket_e2e(monkeypatch, form):
    """真 litellm.completion 全链路：post 回放本地响应，零 socket。"""
    mod = _load_script()
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    envelope_obj = json.loads(_envelope_json(code="x = 1\n"))
    if form == "tool":
        body = _ark_tool_body(envelope_obj, out_tokens=99)
    else:
        body = _ark_text_body(json.dumps(envelope_obj), out_tokens=120)
    patched_cls = _install_post(monkeypatch, body)
    patched_post = patched_cls.post

    send = mod._json_schema_transport()        # 默认：真 litellm.completion
    resp = send(
        model="anthropic/deepseek-v4.1-flash",
        api_base=f"https://{HOST}/api/coding", api_key=TOKEN,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=2048, temperature=0, timeout=30)

    # 闭包已复原到测试打桩（不是 wrapper，也不是未安装状态由 monkeypatch 管）。
    assert patched_cls.post is patched_post
    msg = resp.choices[0].message
    assert json.loads(msg.content) == envelope_obj
    meta = send.last_meta
    wr, wq = meta["wire_response"], meta["wire_request"]
    assert wr["http_status"] == 200 and wr["parse_ok"] is True
    assert wr["tool_channel"] is (form == "tool")
    assert wr["tool_input_null"] is False
    assert wr["blocks"] == (
        [{"type": "tool_use", "name": "json_tool_call"}]
        if form == "tool" else [{"type": "text", "name": None}])
    assert len(wr["attempts"]) == 1 and wr["attempts"][0]["final"] is True
    # choice 级两态同形：这正是需要 HTTP 层取证的原因。
    assert wr["choice_level"] == {
        "tool_calls_present": False, "tool_call_names": [],
        "finish_reason": "stop"}
    assert wq == {
        "wire_req_has_tools": True,
        "wire_req_tool_names": ["json_tool_call"],
        "wire_req_tool_choice": True,
        "wire_req_has_response_format": False}
    # 真 litellm 出口请求体里有 tools/tool_choice、无 response_format 键。
    # （post 打桩不保留 kwargs；经 wq 四布尔同源断言。）
    assert resp.usage.completion_tokens == (99 if form == "tool" else 120)


# ---------------------------------------------------------------------------
# 7. reason 分类器
# ---------------------------------------------------------------------------

def _wr(*, tool=False, text=False, null=False, parse=True, blocks=True,
        tool_name="json_tool_call"):
    if not blocks:
        return {"parse_ok": parse, "blocks": [], "tool_channel": tool,
                "tool_input_null": null}
    blk = ({"type": "tool_use", "name": tool_name} if tool
           else {"type": "text", "name": None})
    return {"parse_ok": parse, "blocks": [blk], "tool_channel": tool,
            "tool_input_null": null}


@pytest.mark.parametrize("enforced,wr,reason", [
    # tool 通道内容不过（含非法 JSON 被透传成串的情形）。
    (False, _wr(tool=True), "content_failed_enforcement"),
    # 纯文本通道但内容合规：历史自觉基率情形。
    (True, _wr(text=True), "text_compliant_voluntary"),
    # 纯文本通道内容也不过。
    (False, _wr(text=True), "content_failed_enforcement"),
    # tool_use input=None 优先（即便内容奇迹般合规也不允许 exit 0）。
    (True, _wr(tool=True, null=True), "tool_input_null"),
    # 200 但块不可解析/无块：通道不可证。
    (True, _wr(parse=False, blocks=False), "unattestable"),
    (True, _wr(blocks=False), "unattestable"),
    # 假网关/未经闭包：无任何通道证据。
    (True, None, "unattestable"),
])
def test_classify_not_enforced_reasons(enforced, wr, reason):
    mod = _load_script()
    verify = {"enforced": enforced}
    assert mod._classify_not_enforced(verify, wr) == reason


# ---------------------------------------------------------------------------
# 8. _verify_envelope：压力信封 enforced=True；各类坏包 enforced=False 不抛
# ---------------------------------------------------------------------------

def test_verify_envelope_valid_stress_envelope_enforced():
    mod = _load_script()
    code = _stress_code()
    assert 4000 <= len(code) <= 8000
    v = mod._verify_envelope(_envelope_json())
    assert v["json_ok"] is True and v["json_error"] is None
    assert v["path_ok"] and v["index_ok"] and v["total_ok"]
    assert v["envelope_ok"] is True
    assert v["code_present"] and v["code_python_ok"] is True
    assert v["code_python_error"] is None
    assert v["code_chars"] == len(code) >= 4000
    for marker in ("has_backslash", "has_double_quote", "has_single_quote",
                   "has_triple_quote", "has_non_ascii"):
        assert v[marker] is True, marker
    assert v["stress_all"] is True and v["enforced"] is True
    assert v["code_over_max"] is False


def test_verify_envelope_oversize_code_still_enforced_but_flagged():
    # MINOR3：9000+ 字符合法压力 code → enforced 不被超窗门控，仍 True；
    # code_over_max=True 只作证据标志（超窗无法伪造强制力）。
    mod = _load_script()
    code = _stress_code(n_funcs=120)
    assert len(code) > 8000
    v = mod._verify_envelope(_envelope_json(code=code))
    assert v["code_python_ok"] is True
    assert v["code_chars"] > 8000 and v["code_over_max"] is True
    assert v["stress_all"] is True and v["enforced"] is True
    # 初始字段恒存在：非 JSON/缺 code 路径 code_over_max 也是 False。
    assert mod._verify_envelope("not json")["code_over_max"] is False
    missing = mod._verify_envelope(json.dumps({"chunk": {"path": "x"}}))
    assert missing["code_present"] is False
    assert missing["code_over_max"] is False


def test_verify_envelope_bare_control_char_is_json_error():
    mod = _load_script()
    # code 内换行在合法信封里是两字符 \n；把第一处替成真换行 = 裸控制字符。
    raw = _envelope_json().replace("\\n", "\n", 1)
    v = mod._verify_envelope(raw)
    assert v["json_ok"] is False
    assert v["json_error"] and v["json_error"].startswith("Char ")
    assert len(v["json_error"]) <= 200
    assert v["enforced"] is False


@pytest.mark.parametrize("obj", [
    {"nope": 1},                          # 缺 chunk
    {"chunk": None},                      # chunk 非对象
    {"chunk": {"path": TARGET_PATH, "index": 3, "total": 4}},  # 缺 code
])
def test_verify_envelope_bad_envelope_not_enforced(obj):
    mod = _load_script()
    v = mod._verify_envelope(json.dumps(obj, ensure_ascii=False))
    assert v["json_ok"] is True and v["envelope_ok"] is False
    assert v["enforced"] is False


def test_verify_envelope_wrong_field_values_detail_flags():
    mod = _load_script()
    v = mod._verify_envelope(_envelope_json(path="other.py", index=2, total=9))
    assert v["json_ok"] is True
    assert v["path_ok"] is False
    assert v["index_ok"] is False
    assert v["total_ok"] is False
    assert v["envelope_ok"] is False and v["enforced"] is False
    # bool 不许冒充整数（bool 是 int 子类）。
    v2 = mod._verify_envelope(_envelope_json(index=True))
    assert v2["index_ok"] is False


def test_verify_envelope_invalid_python_not_enforced():
    mod = _load_script()
    bad_code = 'def broken(:\n    x = "\\d"\n'
    v = mod._verify_envelope(_envelope_json(code=bad_code))
    assert v["json_ok"] and v["envelope_ok"]
    assert v["code_python_ok"] is False and v["code_python_error"]
    assert v["enforced"] is False


def test_verify_envelope_short_code_missing_markers_not_enforced():
    mod = _load_script()
    v = mod._verify_envelope(_envelope_json(code="x = 1\n"))
    assert v["code_python_ok"] is True
    assert v["has_backslash"] is False
    assert v["has_non_ascii"] is False
    assert v["stress_all"] is False and v["enforced"] is False


def test_verify_envelope_non_json_inputs_never_raise():
    mod = _load_script()
    for raw in ("not json at all", "", "[", "123", "null",
                json.dumps(["not", "object"]), "「中文非 json」"):
        v = mod._verify_envelope(raw)
        assert v["enforced"] is False
        assert set(v) >= {"json_ok", "envelope_ok", "enforced"}


# ---------------------------------------------------------------------------
# 9. main exit 0/11：零 socket e2e（真闭包 + 真 litellm + 打桩 post）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form,expect_rc,expect_reason", [
    ("tool-valid", 0, "tool_channel_delivered_n1"),
    ("text-valid", 11, "text_compliant_voluntary"),
    ("tool-wrong-name", 11, "content_failed_enforcement"),
    ("tool-bad-envelope", 11, "content_failed_enforcement"),
    ("tool-null", 11, "tool_input_null"),
])
def test_main_tool_channel_conjunction_exit_semantics(
        monkeypatch, env, form, expect_rc, expect_reason):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    if form == "tool-valid":
        body = _ark_tool_body(json.loads(_envelope_json()), out_tokens=5200)
    elif form == "text-valid":
        body = _ark_text_body(_envelope_json(), out_tokens=5200)
    elif form == "tool-wrong-name":
        # 通道为真但块名不是约定工具名：即使内容四层全过也不许 exit 0，
        # 不新增 reason，落现有 tool_channel 分支 content_failed_enforcement。
        body = _ark_tool_body(
            json.loads(_envelope_json()), out_tokens=5200,
            name="some_other_tool")
    elif form == "tool-bad-envelope":
        # 工具通道交付但内容不是信封（含非法 JSON 透传成串的同族失败）。
        body = _ark_tool_body({"nope": 1}, out_tokens=40)
    else:
        # tool-null
        body = _ark_tool_body(None, out_tokens=5)
    _install_post(monkeypatch, body)
    out = env / f"diag-{form}.json"
    rc = _run_main(mod, monkeypatch, out, _TransportGateway)
    assert rc == expect_rc
    assert out.is_file() and out.stat().st_mode & 0o777 == 0o600
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["reason"] == expect_reason
    wr, wq = doc["wire_response"], doc["wire_request"]
    assert wr["http_status"] == 200 and wr["parse_ok"] is True
    assert len(wr["attempts"]) == 1 and wr["attempts"][0]["final"] is True
    assert wq["wire_req_has_tools"] is True
    assert wq["wire_req_tool_names"] == ["json_tool_call"]
    assert wq["wire_req_has_response_format"] is False
    if form == "tool-wrong-name":
        # 实测 1.99.4：_resolve_json_mode_non_streaming 按 function.name ==
        # "json_tool_call" 匹配才剥离/上浮工具载荷；非约定名块保留为普通
        # tool_calls（finish tool_calls），载荷不进 message.content。
        assert wr["choice_level"] == {
            "tool_calls_present": True,
            "tool_call_names": ["some_other_tool"],
            "finish_reason": "tool_calls"}
    else:
        # 其余四形：json_mode 剥离对照恒 False/[]/"stop"。
        assert wr["choice_level"] == {
            "tool_calls_present": False, "tool_call_names": [],
            "finish_reason": "stop"}
    blob = out.read_text(encoding="utf-8")
    assert TOKEN not in blob and HOST not in blob
    assert "arguments" not in blob

    if form == "tool-wrong-name":
        # 当前 litellm 下名错位还会连带载荷不上浮（enforced 亦 False）；
        # 实际块名必须原样进证据，请求侧工具名仍是约定名（错位可辨）。
        assert doc["enforced"] is False
        assert wr["tool_channel"] is True
        assert wr["tool_input_null"] is False
        assert wr["blocks"] == [
            {"type": "tool_use", "name": "some_other_tool"}]
        assert wq["wire_req_tool_names"] == [mod.EXPECTED_TOOL_NAME]

    if form == "tool-valid":
        # 全证据/透传契约集中在此 case 断言。
        assert doc["probe"] == "diag_json_mode" and "rejected" not in doc
        assert doc["enforced"] is True
        assert wr["tool_channel"] is True
        assert wr["blocks"] == [{"type": "tool_use",
                                "name": mod.EXPECTED_TOOL_NAME}]
        assert doc["stage"] == "coding_debug"
        assert doc["max_tokens"] == 32768
        assert doc["batch_id"] == BATCH and doc["thread_id"] == THREAD
        assert doc["usage"] == {"input_tokens": 31, "output_tokens": 5200}
        assert doc["code_chars"] >= 4000 and doc["stress_all"] is True
        assert doc["code_over_max"] is False
        assert len(doc["raw_head"]) <= 300 and len(doc["raw_tail"]) <= 300
        assert isinstance(doc["static"]["litellm"], str)
        assert doc["static"]["shapes"]["json_schema"]["wire_has_tools"] is True
        seen = _TransportGateway.seen
        assert seen["stage"] == "coding_debug"
        assert seen["max_tokens"] == 32768 and seen["temperature"] == 0
        assert seen["timeout"] == mod.TIMEOUT_S
        assert seen["call_extra"] == {}    # 模型 ID 绝不作为 call kwarg
        init = seen["init_kwargs"]
        assert init["max_attempts"] == 1 and callable(init["transport"])
        assert init["extra"] == {}
        assert isinstance(seen["ledger"], QuotaLedger)
        msgs = seen["messages"]
        assert "image" not in json.dumps(msgs)
        assert msgs == [{"role": "user", "content": [
            {"type": "text", "text": msgs[0]["content"][0]["text"]}]}]
        prompt = msgs[0]["content"][0]["text"]
        for needle in ("JSON", TARGET_PATH, "Windows", "\\d", "docstring",
                       "4000", "裸控制字符"):
            assert needle in prompt
        # 请求体（含压力 prompt）绝不进元数据：压力码片段不在 wire_* 中。
        wire_blob = json.dumps(
            {"q": wq, "r": wr}, ensure_ascii=False)
        assert _stress_code()[:80] not in wire_blob


def test_main_compliant_payload_wrong_tool_name_still_not_delivered(
        monkeypatch, env):
    """名闸门独立证据：即使内容四层全过（覆盖未来 litellm 不按名剥离也上浮
    载荷的情形），原始块名 != EXPECTED_TOOL_NAME 仍不许 exit 0，落现有
    tool_channel 分支 content_failed_enforcement（不新增 reason）。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    valid = _envelope_json()
    # 真闭包 + 真 litellm + 打桩 post：原始块名 some_other_tool、input 合规。
    _install_post(
        monkeypatch,
        _ark_tool_body(json.loads(valid), out_tokens=5200,
                       name="some_other_tool"))

    class _NameGateGateway(_TransportGateway):
        def call(self, stage, messages, *, max_tokens, temperature, timeout,
                 **extra):
            type(self).seen.update(
                stage=stage, messages=messages, max_tokens=max_tokens,
                temperature=temperature, timeout=timeout, call_extra=extra)
            # 照调真实闭包：拦截器对真原始块取证（main 读同一闭包 last_meta），
            # 只把 litellm 因名错位未上浮的载荷替换为合规内容，单独钉名闸门。
            self.seen["init_kwargs"]["transport"](
                model="anthropic/deepseek-v4.1-flash",
                api_base=f"https://{HOST}/api/coding",
                api_key=TOKEN, messages=messages,
                max_tokens=max_tokens, temperature=temperature, timeout=timeout)
            return valid, (31, 5200)

    out = env / "diag-wrong-name-compliant.json"
    rc = _run_main(mod, monkeypatch, out, _NameGateGateway)
    assert rc == 11
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["reason"] == "content_failed_enforcement"
    assert doc["enforced"] is True
    wr, wq = doc["wire_response"], doc["wire_request"]
    assert wr["http_status"] == 200 and wr["parse_ok"] is True
    assert wr["tool_channel"] is True
    assert wr["tool_input_null"] is False
    assert wr["blocks"] == [
        {"type": "tool_use", "name": "some_other_tool"}]
    assert wq["wire_req_tool_names"] == [mod.EXPECTED_TOOL_NAME]


def test_main_enforced_without_channel_evidence_is_unattestable(
        monkeypatch, env):
    """内容合规但无通道证据（假网关/闭包未触发）：不许 exit 0。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    valid = _envelope_json()
    fake = _fake(text=valid, out_tokens=5200)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-no-channel.json"
    rc = mod.main(["--batch-id", BATCH, "--thread", THREAD,
                   "--max-tokens", "32768", "--out", str(out)])
    assert rc == 11
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["reason"] == "unattestable" and doc["enforced"] is True
    assert doc["wire_request"] is None and doc["wire_response"] is None


def test_main_exception_after_200_writes_evidence_exit_11(monkeypatch, env):
    """200 已截获但 completion 随后抛错（典型 EmptyResponseError）→ exit 11。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    _install_post(monkeypatch, _ark_text_body("{}"))

    import litellm

    def _fake_completion(**kwargs):
        # 模拟 litellm 拿到 200 后在 transform/空包处理阶段抛错。
        HTTPHandler().post("u", data=json.dumps({
            "tools": [{"name": "json_tool_call"}],
            "tool_choice": {"type": "tool", "name": "json_tool_call"},
            "messages": []}))
        raise RuntimeError("synthetic empty-200 failure")

    monkeypatch.setattr(litellm, "completion", _fake_completion)
    out = env / "diag-after200.json"
    rc = _run_main(mod, monkeypatch, out, _TransportGateway)
    assert rc == 11
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["reason"] == "exception_after_200:RuntimeError"
    assert doc["exception_after_200"] is True and doc["accepted"] is True
    assert doc["wire_response"]["attempts"][0]["http_status"] == 200
    assert doc["wire_response"]["attempts"][0]["final"] is True
    assert doc["wire_request"]["wire_req_has_tools"] is True
    assert "synthetic empty-200" in doc["err"]


def test_script_default_out_under_task6_ascent():
    mod = _load_script()
    assert mod.default_out().name == "diag_json_mode.json"
    assert mod.default_out().parent.name == "task6_ascent"


# ---------------------------------------------------------------------------
# 10. accepted-but-not-enforced：假网关（无通道证据）各失败内容仍 exit 11
# ---------------------------------------------------------------------------

def test_script_accepted_but_not_enforced_exit_11(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(text="not json at all", out_tokens=120)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-not-enforced.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == mod.EXIT_ACCEPTED_NOT_ENFORCED == 11
    assert out.is_file() and out.stat().st_mode & 0o777 == 0o600
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["enforced"] is False
    assert doc["reason"] == "unattestable"
    assert doc["json_ok"] is False and doc["json_error"]
    assert doc["usage"]["output_tokens"] == 120
    assert isinstance(doc["static"]["litellm"], str)


def test_script_enforced_false_variants_also_exit_11(monkeypatch, env):
    # 合法 JSON 但信封错 / code 非法 Python 同样 exit 11。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    for raw in (json.dumps({"nope": 1}),
                _envelope_json(code='def broken(:\n')):
        mod = _load_script()
        fake = _fake(text=raw)
        monkeypatch.setattr(mod, "ModelGateway", fake)
        out = env / "diag-variant.json"
        assert mod.main(["--batch-id", BATCH, "--out", str(out)]) == 11
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["accepted"] is True and doc["enforced"] is False
        assert doc["reason"] == "unattestable"


# ---------------------------------------------------------------------------
# 11. None usage 接受路径不崩（无通道证据 → 11，证据照常落盘）
# ---------------------------------------------------------------------------

def test_script_none_usage_tokens_still_writes_evidence(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(text=_envelope_json(), out_tokens=None)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-none.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == 11 and out.is_file()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["reason"] == "unattestable" and doc["enforced"] is True
    assert doc["usage"] == {"input_tokens": 31, "output_tokens": None}


# ---------------------------------------------------------------------------
# 12. 明确拒绝（NonRetryableError，如供应侧 400）：脱敏证据 exit 10
# ---------------------------------------------------------------------------

def test_script_rejected_writes_redacted_evidence_exit_10(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=R.NonRetryableError(
        f"400 invalid_request_error: tools unsupported for this model; docs "
        f"https://{HOST}/api/coding/v1/messages {BEARER_SECRET}"))
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-reject.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == mod.EXIT_REJECTED and out.is_file()
    assert out.stat().st_mode & 0o777 == 0o600
    blob = out.read_text(encoding="utf-8")
    doc = json.loads(blob)
    assert doc["accepted"] is False and doc["rejected"] is True
    assert doc["max_tokens"] == 32768
    assert "tools unsupported" in doc["err"] and len(doc["err"]) <= 300
    # 拒绝证据同样带 static 段、拦截元数据键（假网关 → None）且不回显敏感。
    assert isinstance(doc["static"]["litellm"], str)
    assert doc["wire_request"] is None and doc["wire_response"] is None
    assert HOST not in blob and "diagjsontestredacted0001" not in blob
    assert "[url]" in doc["err"] and "Bearer" in doc["err"]  # 前缀留、本体剥


def test_script_rejected_evidence_write_failure_exit_1(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=R.NonRetryableError("400 tools unsupported"))
    monkeypatch.setattr(mod, "ModelGateway", fake)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", _boom)
    rc = mod.main(["--batch-id", BATCH, "--out", str(env / "x" / "d.json")])
    assert rc == mod.EXIT_ERROR


def test_script_accepted_evidence_write_failure_exit_1(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(text=_envelope_json())
    monkeypatch.setattr(mod, "ModelGateway", fake)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", _boom)
    rc = mod.main(["--batch-id", BATCH, "--out", str(env / "d.json")])
    assert rc == mod.EXIT_ERROR


# ---------------------------------------------------------------------------
# 13. 连接类异常：无 200 截获 → exit 1，不落证据
# ---------------------------------------------------------------------------

def test_script_exception_exit_1_no_evidence(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=requests.ConnectionError("refused"))
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "nope.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == mod.EXIT_ERROR and not out.exists()


# ---------------------------------------------------------------------------
# 14. 源码红线：唯一提及点、图永不接线、无模型覆盖、靶点字面钉死
# ---------------------------------------------------------------------------

def test_redline_call_site_single_and_graph_unaware():
    mentions: set[str] = set()
    for root in ("orchestrator", "scripts"):
        for py in (_REPO_ROOT / root).rglob("*.py"):
            if "diag_json_mode" in py.read_text(encoding="utf-8"):
                mentions.add(str(py.relative_to(_REPO_ROOT)))
    # 引擎/脚本中只有探针脚本自身提及该名（测试文件不在扫描根内）。
    assert mentions == {"scripts/phase2/diag_json_mode.py"}
    # graph/ 与 runner 任何文件连名字都不许出现（永不接线进生产流程）。
    for forbidden in list((_REPO_ROOT / "orchestrator/graph").rglob("*.py")) + \
            [_REPO_ROOT / "orchestrator/agent_runner/runner.py"]:
        assert "diag_json_mode" not in forbidden.read_text(encoding="utf-8")

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # 脚本自身不反向 import 图（ast.Import 与 ast.ImportFrom 双查）。
    from_modules = {n.module for n in ast.walk(tree)
                     if isinstance(n, ast.ImportFrom) and n.module}
    plain_modules = {".".join(a.name.split(".")[:2]) for n in ast.walk(tree)
                     if isinstance(n, ast.Import) for a in n.names}
    assert not any(m == "orchestrator.graph"
                   or (m or "").startswith("orchestrator.graph.")
                   for m in from_modules | plain_modules)
    # 不提供任何模型 ID 覆盖入口（硬规则 1：模型只能读 stage_models.yaml）。
    flags = {kw.value for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "add_argument"
             for kw in n.args if isinstance(kw, ast.Constant)}
    assert "--model" not in flags and "--model-id" not in flags

    # deepseek 模型名仅作为 A 段转换探针常量出现 1 次（AST 等值匹配，
    # docstring 长串只是包含子串不算），且 main 内 gateway.call 不带
    # model/response_format kwarg（模型 ID 只读 yaml、response_format 只
    # 经 transport 闭包单点注入）。
    model_hits = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Constant)
                  and n.value == "deepseek-v4.1-flash"]
    assert len(model_hits) == 1
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    assert any(any(getattr(t, "id", None) == "_TRANSFORM_PROBE_MODEL"
                   for t in a.targets)
               and isinstance(a.value, ast.Constant)
               and a.value.value == "deepseek-v4.1-flash"
               for a in assigns)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    for node in ast.walk(main_fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"):
            kw_names = {kw.arg for kw in node.keywords}
            assert "model" not in kw_names
            assert "response_format" not in kw_names

    # monkeypatch 靶点：AST 中恰好 1 个该字面量常量（赋值来源 + 解析唯一
    # 来源），值与期望逐字相等。
    target_hits = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Constant)
                   and n.value == _EXPECTED_POST_TARGET]
    assert len(target_hits) == 1
    assert any(any(getattr(t, "id", None) == "HTTPHANDLER_POST_TARGET"
                   for t in a.targets)
               and isinstance(a.value, ast.Constant)
               and a.value.value == _EXPECTED_POST_TARGET
               for a in assigns)
    # EXPECTED_TOOL_NAME 单一源：脚本内 "json_tool_call" 等值字面 AST
    # 恰好 1 个（docstring/注释只是包含子串，不算等值常量），且即该模块
    # 常量赋值——exit 0 合取与 map 结果必须引用常量，禁止第二字面源。
    tool_name_hits = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Constant)
                      and n.value == "json_tool_call"]
    assert len(tool_name_hits) == 1
    assert any(any(getattr(t, "id", None) == "EXPECTED_TOOL_NAME"
                   for t in a.targets)
               and isinstance(a.value, ast.Constant)
               and a.value.value == "json_tool_call"
               for a in assigns)
    # 代码里 HTTPHandler 只允许以靶点串出现：无裸类名 Name/Attribute，
    # 不得出现 AsyncHTTPHandler（防误包异步路径）。
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "HTTPHandler"
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("HTTPHandler", "AsyncHTTPHandler")
    assert "AsyncHTTPHandler" not in source
    # 替换点只允许是实例属性 ".post" 赋值给解析出的类（无其他 setattr）。
    assert "setattr(" not in source
