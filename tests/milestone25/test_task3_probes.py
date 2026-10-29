"""Phase 2.5 Task3 Stage0 探针的 hermetic 单测（0 付费、0 触网）。

覆盖（合同：.omc/handoffs/phase25-t3-stage0-probe.md，AC-T3-1..8 的
mock 侧；付费实跑由探针脚本在 SIMPRAXIS_ALLOW_PAID=1 双守卫下人工执行）：
- P1 纯文本 JSON 数组并行：解析/判读、POST1 坏形态不补呼、POST2 观察
  回注续作+哨兵 stop、提前完成切追加观察恢复、供应侧拒绝原样留存；
- P2 原生 tool_use：单/双并行 id 不串、结果各自回灌 1:1 合法（错放即
  ValueError）、长观察（截断大限附近）头尾标记恢复、tool-only 200 经
  transport 捕获后仍按 raw 判形态、tools 经注入缝透传且无 tool_choice、
  api_key 不落证、400 提前失败即停不补呼；
- P3 失败分类：429/5xx/4xx=供应商错，200 length+不闭合=可自修截断，
  200 坏 JSON=可自修，恢复请求不复发同一调用；真实截断触发路径
  （1 POST、不恢复）；
- P5 GatewayModel 单方法缝（ChatMessage/token_usage 映射；tools/
  stop_sequences/response_format/image/tool 角色全拒）+ 复用 P1 原文的
  离线 code 抽取率/stop 对照；
- P0：静态遥测扫描分类、缓存快照 diff、socket 审计非环回取证、
  FakeModel+CodeAgent 一轮两只本地只读工具（真实 smolagents）；
- 五脚本统一双守卫/源码红线：probe_p4 不存在；调用点唯一；零
  orchestrator.graph/orchestrator.agentic 导入；零 model_id/供应商
  URL/image block；单发 max_attempts=1；paid main 路径打 paid marker。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.gateway.router import EmptyResponseError, NonRetryableError  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_DIR = _REPO_ROOT / "scripts" / "phase25" / "probes"
BATCH = "P25-B1"


def _load(fname: str, mod_name: str | None = None):
    path = _PROBE_DIR / fname
    spec = importlib.util.spec_from_file_location(mod_name or fname[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for k in ("SIMPRAXIS_PHASE1_ROOT", "SIMPRAXIS_PHASE2_ROOT",
              "SIMPRAXIS_PHASE25_ROOT", "SIMPRAXIS_GATEWAY_STATE"):
        monkeypatch.setenv(k, str(tmp_path / k.split("_")[-1].lower()))
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    return tmp_path


# ===========================================================================
# P1 纯文本工具协议
# ===========================================================================

p1 = _load("probe_p1_plaintext_tools.py", "t3_p1")

GOOD_ARRAY = ('```json\n[{"tool":"list_dir","arguments":{"path":"."}},'
              '{"tool":"read_file","arguments":{"path":"alpha.txt"}}]\n```')


class _ScriptedP1Gateway:
    """P1 假网关：按序返回 (text,(in,out))，异常脚本直接抛。"""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def call(self, stage, messages, *, max_tokens, temperature, timeout):
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens, "temperature": temperature,
                           "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, (10 + len(self.calls), 20 + len(self.calls))


def test_p1_guard_bare_run_exits_2(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("守卫必须在构造网关前 exit 2")

    monkeypatch.setattr(p1, "ModelGateway", _Boom)
    assert p1.main(["--batch-id", BATCH]) == 2


@pytest.mark.parametrize("argv", [
    ["--batch-id", "bad/batch"], ["--batch-id", BATCH, "--thread", "bad thread"],
])
def test_p1_invalid_args_exit_2(monkeypatch, argv):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert p1.main(argv) == 2


def test_p1_post1_good_parallel_array():
    j = p1.judge_post1(GOOD_ARRAY)
    assert j["pass"] and j["json_parse_rate"] == 1.0
    assert [c["tool"] for c in j["calls"]] == ["list_dir", "read_file"]


def test_p1_post1_rejects_single_call_nonarray_and_multi_candidates():
    assert not p1.judge_post1('[{"tool":"list_dir","arguments":{"path":"."}}]')["pass"]
    assert not p1.judge_post1('not json at all')["pass"]
    dup = GOOD_ARRAY + "\n" + GOOD_ARRAY
    assert not p1.judge_post1(dup)["pass"]  # 两个可解析数组：歧义，不冒充 100%
    swapped = GOOD_ARRAY.replace("list_dir", "read_file").replace(
        "read_file", "list_dir", 1)
    assert p1.judge_post1(swapped)["pass"]  # 顺序不要求，集合须恰为两只
    assert not p1.judge_post1(
        '[{"tool":"read_file","arguments":{"path":"a"}},{"tool":"evil","arguments":{}}]'
    )["pass"]


def test_p1_post2_stop_and_marker_judgment():
    good = f"结果：{p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}"
    assert p1.judge_post2(good, required_markers=[p1.ALPHA_MARKER])["pass"]
    j = p1.judge_post2(f"{p1.ALPHA_MARKER} {p1.STOP_SENTINEL} tail",
                       required_markers=[p1.ALPHA_MARKER])
    assert not j["pass"]  # 哨兵不在尾部
    j = p1.judge_post2(p1.ALPHA_MARKER, required_markers=[p1.ALPHA_MARKER])
    assert not j["pass"]  # 无哨兵
    # 回注后又发工具 JSON = stop 违规
    j = p1.judge_post2(GOOD_ARRAY + f"\n{p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}",
                       required_markers=[p1.ALPHA_MARKER])
    assert not j["pass"]


def test_p1_exec_readonly_tools_jail(tmp_path):
    ws = tmp_path / "ws"
    p1.make_workspace(ws)
    assert p1.ALPHA_MARKER in p1.exec_readonly_tool(
        "read_file", {"path": "alpha.txt"}, ws)
    assert "alpha.txt" in p1.exec_readonly_tool("list_dir", {"path": "."}, ws)
    with pytest.raises((ValueError, OSError)):
        p1.exec_readonly_tool("read_file", {"path": "../escape.txt"}, ws)


def test_p1_protocol_happy_path_two_posts(env, tmp_path):
    out = tmp_path / "ev"
    gw = _ScriptedP1Gateway([
        GOOD_ARRAY,
        f"答案引用 {p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}",
    ])
    result = p1.run_protocol(gw, out_dir=out, batch_id=BATCH,
                             thread_id="p25t3-p1", workspace=tmp_path / "ws")
    assert result["rc"] == 0 and result["verdict"]["p1_pass"] is True
    assert len(gw.calls) == 2
    # POST2 注入了两个 tool_result 且按序
    m2 = gw.calls[1]["messages"][-1]["content"][0]["text"]
    assert "<tool_result index=0 tool=list_dir" in m2
    assert "<tool_result index=1 tool=read_file" in m2
    assert p1.ALPHA_MARKER in m2 and p1.BETA_MARKER not in m2
    for fn in ("p1_post1_req.json", "p1_post1_resp.json",
               "p1_post2_req.json", "p1_post2_resp.json", "p1_verdict.json"):
        fp = out / fn
        assert fp.is_file() and fp.stat().st_mode & 0o777 == 0o600
    doc = json.loads((out / "p1_post1_resp.json").read_text())
    assert doc["judgment"]["pass"] and doc["usage"]["input_tokens"] == 11
    # 全程零 image block
    assert '"image"' not in json.dumps(gw.calls)


def test_p1_post1_bad_shape_does_not_make_up_post2(env, tmp_path):
    gw = _ScriptedP1Gateway(["抱歉我不会输出 JSON"])
    result = p1.run_protocol(gw, out_dir=tmp_path / "ev", batch_id=BATCH,
                             thread_id="t", workspace=tmp_path / "ws")
    assert result["rc"] == 3 and len(gw.calls) == 1
    assert result["verdict"]["stop_reason"] == "post1_protocol_failed"


def test_p1_early_finish_switches_to_beta_recovery(env, tmp_path):
    early = GOOD_ARRAY + f"\n\n我已完成 {p1.STOP_SENTINEL}"
    gw = _ScriptedP1Gateway([
        early,
        f"综合 alpha {p1.ALPHA_MARKER} 与 beta {p1.BETA_MARKER}\n{p1.STOP_SENTINEL}",
    ])
    result = p1.run_protocol(gw, out_dir=tmp_path / "ev", batch_id=BATCH,
                             thread_id="t", workspace=tmp_path / "ws")
    assert result["rc"] == 0
    m2 = gw.calls[1]["messages"][-1]["content"][0]["text"]
    assert "beta.txt" in m2 and p1.BETA_MARKER in m2
    # POST2 缺 beta 标记则判负（恢复事实，不放松）
    gw2 = _ScriptedP1Gateway([early, f"{p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}"])
    r2 = p1.run_protocol(gw2, out_dir=tmp_path / "ev2", batch_id=BATCH,
                         thread_id="t2", workspace=tmp_path / "ws2")
    assert r2["rc"] == 3


def test_p1_post2_missing_sentinel_fails(env, tmp_path):
    gw = _ScriptedP1Gateway([GOOD_ARRAY, f"答案 {p1.ALPHA_MARKER} 但忘了哨兵"])
    result = p1.run_protocol(gw, out_dir=tmp_path / "ev", batch_id=BATCH,
                             thread_id="t", workspace=tmp_path / "ws")
    assert result["rc"] == 3 and len(gw.calls) == 2


def test_p1_supplier_rejection_preserved_exit_10(env, tmp_path):
    gw = _ScriptedP1Gateway([NonRetryableError("400 bad request")])
    result = p1.run_protocol(gw, out_dir=tmp_path / "ev", batch_id=BATCH,
                             thread_id="t", workspace=tmp_path / "ws")
    assert result["rc"] == 10 and len(gw.calls) == 1
    doc = json.loads((tmp_path / "ev" / "p1_post1_resp.json").read_text())
    assert doc["outcome"] == "supplier_rejected" and "400" in doc["error_detail"]


@pytest.mark.paid
def test_p1_main_uses_phase25_ledger_single_shot(monkeypatch, env, tmp_path):
    """付费入口回归：phase25 账命名空间 + max_attempts=1（真调用受 paid 守卫）。"""
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        pytest.skip("paid 探针入口测试：裸跑 skip（零触网）")
    captured = {"max_attempts": None, "i": 0}

    class _GW:
        def __init__(self, *, ledger, max_attempts):
            from orchestrator.gateway.quota_ledger import QuotaLedger
            assert isinstance(ledger, QuotaLedger)
            captured["max_attempts"] = max_attempts

        def call(self, stage, messages, *, max_tokens, temperature, timeout):
            i = captured["i"]
            captured["i"] = i + 1
            return (GOOD_ARRAY if i == 0
                    else f"{p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}"), (1, 2)

    monkeypatch.setattr(p1, "ModelGateway", _GW)
    rc = p1.main(["--batch-id", BATCH, "--out-dir", str(tmp_path / "ev"),
                  "--workspace", str(tmp_path / "ws")])
    assert rc == 0 and captured["max_attempts"] == 1


# ===========================================================================
# P2 原生 tool_use
# ===========================================================================

p2 = _load("probe_p2_native_tooluse.py", "t3_p2")


def _resp_dump(calls, content=None, fr="tool_use"):
    return {"choices": [{"finish_reason": fr, "message": {
        "content": content,
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}
            for i, n, a in calls]}}],
        "usage": {"input_tokens": 33, "output_tokens": 44}}


class _Resp:
    def __init__(self, dump):
        self._dump = dump

    def model_dump(self):
        return self._dump


class _CapturingGateway:
    """模拟 ModelGateway：调 transport（富化包装器），tool-only 200 走空回答语义。

    返回什么由 monkeypatch 的 _call_completion 按 POST 计数决定（run_forms
    每形态各构造一个网关，故不能以网关数当 POST 序）。
    """
    def __init__(self, transport):
        self.transport = transport

    def call(self, stage, messages, *, max_tokens, temperature, timeout):
        resp = self.transport(
            model="anthropic/model-from-stage-config", api_base="https://x",
            api_key="FAKE-KEY-123", messages=messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        assert isinstance(resp, _Resp)
        dump = resp.model_dump()
        content = dump["choices"][0]["message"]["content"]
        if not content or not str(content).strip():
            raise EmptyResponseError("tool-only 200：G-12 文本空契约")
        u = dump.get("usage") or {}
        return content, (u.get("input_tokens"), u.get("output_tokens"))


def _gateway_factory(capture_calls):
    def _factory(capture):
        gw = _CapturingGateway(p2.make_tools_transport(capture))
        capture_calls.append(gw)
        return gw
    return _factory


def _scripted_completion(monkeypatch, mod, dumps):
    """按 POST 序返回 dump；异常对象直接抛出（不经任何包装）。"""
    idx = {"n": 0}

    def _fake(**kwargs):
        item = dumps[idx["n"]]
        idx["n"] += 1
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    monkeypatch.setattr(mod, "_call_completion", _fake)
    return idx


def test_p2_judges_single_and_parallel_id_integrity():
    r1 = p2.judge_single(_resp_dump([("c1", "list_dir", {"path": "."})]))
    assert r1["pass"] and r1["calls"][0]["id"] == "c1"
    rp = p2.judge_parallel(_resp_dump([
        ("cA", "list_dir", {"path": "."}),
        ("cB", "read_file", {"path": "alpha.txt"})]))
    assert rp["pass"]
    roles = [m["role"] for m in rp["feedback_messages"]]
    assert roles == ["assistant", "tool", "tool"]
    ids = [m.get("tool_call_id") for m in rp["feedback_messages"][1:]]
    assert sorted(ids) == ["cA", "cB"]
    # id 重复 = 串扰
    bad = p2.judge_parallel(_resp_dump([
        ("same", "list_dir", {"path": "."}),
        ("same", "read_file", {"path": "a"})]))
    assert not bad["pass"] and any("串扰" in r for r in bad["reasons"])
    # 结果错放（id↔工具名交叉）= ValueError
    with pytest.raises(ValueError):
        p2.build_tool_feedback(
            [{"id": "a", "name": "list_dir", "arguments": {"path": "."}},
             {"id": "b", "name": "read_file", "arguments": {"path": "x"}}],
            {"a": ("read_file", "X"), "b": ("list_dir", "Y")})
    # 多回/少回
    with pytest.raises(ValueError):
        p2.build_tool_feedback(
            [{"id": "a", "name": "list_dir", "arguments": {}}],
            {"a": ("list_dir", "X"), "b": ("read_file", "Y")})


def test_p2_transport_injects_tools_and_redacts_key(monkeypatch, env):
    seen_kwargs = []

    def _fake(**kwargs):
        seen_kwargs.append(kwargs)
        return _Resp(_resp_dump([("c1", "list_dir", {"path": "."})], content="计划"))

    monkeypatch.setattr(p2, "_call_completion", _fake)
    capture: dict = {}
    t = p2.make_tools_transport(capture)
    t(model="anthropic/x", api_key="SECRET", messages=[], max_tokens=10)
    assert [f["function"]["name"] for f in seen_kwargs[0]["tools"]] == \
        ["list_dir", "read_file"]
    assert "tool_choice" not in seen_kwargs[0]
    assert "api_key" not in capture["req_kwargs"]
    assert capture["raw"]["choices"][0]["message"]["tool_calls"][0]["id"] == "c1"


def test_p2_run_forms_all_pass_with_tool_only_post1(monkeypatch, env, tmp_path):
    calls: list = []
    dumps = [
        _resp_dump([("c1", "list_dir", {"path": "."})], content=None),   # tool-only 200
        _resp_dump([("cA", "list_dir", {"path": "."}),
                    ("cB", "read_file", {"path": "alpha.txt"})],
                   content="并行调用两只工具"),
        _resp_dump([], content=f"标记 {p2.HEAD_MARKER} 与 {p2.TAIL_MARKER}",
                   fr="stop"),
    ]
    _scripted_completion(monkeypatch, p2, dumps)
    result = p2.run_forms(_gateway_factory(calls), out_dir=tmp_path / "ev",
                          batch_id=BATCH, thread_prefix="p25t3-p2")
    assert result["rc"] == 0 and result["verdict"]["p2_pass"] is True
    assert len(calls) == 3
    forms = result["verdict"]["forms"]
    assert forms["1"]["outcome"] == "gateway_exit"  # tool-only：账 ok=false 但形态成立
    assert forms["2"]["outcome"] == "ok" and forms["3"]["outcome"] == "ok"
    # 长观察请求确实接近大限且头尾标记在观察两端
    req3 = json.loads((tmp_path / "ev" / "p2_post3_req.json").read_text())
    obs = req3["captured_req_kwargs"]["messages"][2]["content"]
    assert len(obs) >= p2.LONG_OBS_CHARS - 5000
    assert p2.HEAD_MARKER in obs[:200] and p2.TAIL_MARKER in obs[-200:]
    # 证据零密钥
    blob = "".join(p.read_text() for p in (tmp_path / "ev").glob("*.json"))
    assert "FAKE-KEY-123" not in blob
    assert "api_key" not in blob


def test_p2_long_observation_missing_tail_marker_fails(monkeypatch, env, tmp_path):
    calls: list = []
    dumps = [
        _resp_dump([("c1", "list_dir", {"path": "."})], content="a"),
        _resp_dump([("cA", "list_dir", {"path": "."}),
                    ("cB", "read_file", {"path": "alpha.txt"})], content="b"),
        _resp_dump([], content=f"只见头 {p2.HEAD_MARKER}", fr="stop"),
    ]
    _scripted_completion(monkeypatch, p2, dumps)
    result = p2.run_forms(_gateway_factory(calls), out_dir=tmp_path / "ev",
                          batch_id=BATCH, thread_prefix="t")
    assert result["rc"] == 3 and len(calls) == 3  # 形态 3 自己的 POST 已发，判负即停


def test_p2_supplier_400_stops_immediately_no_makeup(monkeypatch, env, tmp_path):
    calls: list = []
    _scripted_completion(
        monkeypatch, p2,
        [NonRetryableError("400 tools parameter not supported")])
    result = p2.run_forms(_gateway_factory(calls), out_dir=tmp_path / "ev",
                          batch_id=BATCH, thread_prefix="t")
    assert result["rc"] == 10 and len(calls) == 1
    resp = json.loads((tmp_path / "ev" / "p2_post1_resp.json").read_text())
    assert resp["outcome"] == "supplier_rejected"
    assert not (tmp_path / "ev" / "p2_post2_req.json").exists()


def test_p2_connection_error_no_raw_is_exit1(monkeypatch, env, tmp_path):
    calls: list = []
    _scripted_completion(monkeypatch, p2, [ConnectionError("upstream unreachable")])
    result = p2.run_forms(_gateway_factory(calls), out_dir=tmp_path / "ev",
                          batch_id=BATCH, thread_prefix="t")
    assert result["rc"] == 1 and len(calls) == 1


def test_p2_guard_bare_run_exits_2(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert p2.main(["--batch-id", BATCH]) == 2


# ===========================================================================
# P3 失败体
# ===========================================================================

p3 = _load("probe_p3_failures.py", "t3_p3")


@pytest.mark.parametrize("kw,cls", [
    (dict(exc_name="RateLimitError"), "supplier_retryable"),
    (dict(http_status=429), "supplier_retryable"),
    (dict(http_status=503), "supplier_retryable"),
    (dict(http_status=400), "supplier_fatal"),
    (dict(exc_name="NonRetryableError"), "supplier_fatal"),
    (dict(http_status=200, finish_reason="length",
          text='[{"i":1},{"i":2'), "self_repairable_truncation"),
    (dict(http_status=200, finish_reason="stop",
          text="我不输出JSON"), "self_repairable_bad_json"),
    (dict(http_status=200, finish_reason="stop",
          text='[{"i":1}]'), "ok"),
])
def test_p3_classify_table(kw, cls):
    assert p3.classify_failure(**kw)["cls"] == cls


def test_p3_self_repairable_classes_have_next_step_recovery():
    trunc = p3.classify_failure(http_status=200, finish_reason="length",
                                text='[{"i":1}')
    bad = p3.classify_failure(http_status=200, finish_reason="stop",
                              text="oops")
    assert trunc["self_repairable"] and bad["self_repairable"]
    assert not p3.classify_failure(http_status=429)["self_repairable"]
    msgs = p3.repair_request([{"role": "user", "content": "x"}], "残体")
    assert msgs[-1]["role"] == "user" and "JSON" in msgs[-1]["content"][0]["text"]
    assert msgs[-2]["role"] == "assistant"


class _P3Gateway:
    def __init__(self, transport, text, fr):
        self.transport, self.text, self.fr = transport, text, fr

    def call(self, stage, messages, *, max_tokens, temperature, timeout):
        resp = self.transport(model="m", api_key="SECRET", messages=messages,
                              max_tokens=max_tokens, temperature=temperature,
                              timeout=timeout)
        assert max_tokens == p3.TRIGGER_MAX_TOKENS
        return self.text, (5, 6)


def test_p3_truncation_observed_classified_no_recovery_post(monkeypatch, env, tmp_path):
    text = '[{"i":1},{"i":2},{"i":3'  # 不闭合
    capture: dict = {}
    monkeypatch.setattr(p3, "_call_completion",
                        lambda **kw: _Resp({"choices": [
                            {"finish_reason": "length",
                             "message": {"content": text}}]}))
    gw = _P3Gateway(p3.make_capture_transport(capture), text, "length")
    result = p3.run_truncation(gw, capture, out_dir=tmp_path / "ev",
                               batch_id=BATCH, thread_id="t")
    assert result["rc"] == 0 and result["cls"] == "self_repairable_truncation"
    doc = json.loads((tmp_path / "ev" / "p3_truncation.json").read_text())
    assert doc["finish_reason"] == "length" and doc["ledger_seq"] is None
    assert doc["request"]["max_tokens"] == 512


def test_p3_complete_array_is_fail_not_retuned(monkeypatch, env, tmp_path):
    text = json.dumps([{"i": i, "v": "x" * 10} for i in range(1, 201)])
    capture: dict = {}
    monkeypatch.setattr(p3, "_call_completion",
                        lambda **kw: _Resp({"choices": [
                            {"finish_reason": "stop",
                             "message": {"content": text}}]}))
    gw = _P3Gateway(p3.make_capture_transport(capture), text, "stop")
    result = p3.run_truncation(gw, capture, out_dir=tmp_path / "ev",
                               batch_id=BATCH, thread_id="t")
    assert result["rc"] == 3 and result["cls"] == "ok"  # 事实不符，不调参翻案


def test_p3_supplier_rejection_exit10(monkeypatch, env, tmp_path):
    capture: dict = {}
    gw = SimpleNamespace()

    class _Boom:
        def call(self, *a, **k):
            raise NonRetryableError("400 nope")

    result = p3.run_truncation(_Boom(), capture, out_dir=tmp_path / "ev",
                               batch_id=BATCH, thread_id="t")
    assert result["rc"] == 10 and result["cls"] == "supplier_fatal"


def test_p3_guard_bare_run_exits_2(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert p3.main(["--batch-id", BATCH]) == 2


# ===========================================================================
# P5 GatewayModel 缝 + 离线对照
# ===========================================================================

p5 = _load("probe_p5_gatewaymodel_smoke.py", "t3_p5")


class _TextGW:
    def __init__(self, text="答案", usage=(7, 9)):
        self.text, self.usage, self.kw = text, usage, None

    def call(self, stage, messages, *, max_tokens, temperature):
        self.kw = {"stage": stage, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature}
        return self.text, self.usage


def test_p5_model_seam_maps_chatmessage_and_usage():
    Cls = p5.build_probe_model_class()
    gw = _TextGW()
    msg = Cls(gw).generate([{"role": "user", "content": "hi"}])
    assert msg.role.value == "assistant" and msg.content == "答案"
    assert msg.token_usage.input_tokens == 7 and msg.raw["stage"] == "coding_debug"
    assert gw.kw["stage"] == "coding_debug" and gw.kw["max_tokens"] == 4096
    # usage 为 None 不崩
    msg2 = Cls(_TextGW("x", (None, None))).generate([{"role": "user", "content": "y"}])
    assert msg2.token_usage is None


def test_p5_model_seam_flattens_text_blocks():
    Cls = p5.build_probe_model_class()
    gw = _TextGW()
    Cls(gw).generate([{"role": "system",
                       "content": [{"type": "text", "text": "S"},
                                   {"type": "text", "text": "T"}]}])
    msgs = gw.kw["messages"]
    assert msgs == [{"role": "system",
                     "content": [{"type": "text", "text": "S\nT"}]}]


def test_p5_model_seam_rejects_unsupported_shapes():
    Cls = p5.build_probe_model_class()
    m = Cls(_TextGW())
    with pytest.raises(ValueError, match="tools"):
        m.generate([{"role": "user", "content": "x"}], tools_to_call_from=[1])
    with pytest.raises(ValueError, match="stop_sequences"):
        m.generate([{"role": "user", "content": "x"}], stop_sequences=["S"])
    with pytest.raises(ValueError, match="response_format"):
        m.generate([{"role": "user", "content": "x"}],
                   response_format={"type": "json_object"})
    with pytest.raises(ValueError, match="tools"):
        m.generate([{"role": "user", "content": "x"}], tools=[{"t": 1}])
    with pytest.raises(ValueError, match="image"):
        m.generate([{"role": "user",
                     "content": [{"type": "image", "source": {}}]}])
    with pytest.raises(ValueError, match="tool"):
        m.generate([{"role": "tool-response", "content": "r"}])


def test_p5_code_extract_metrics():
    on = p5.code_extract_metrics("```py\nfinal_answer(1)\n```")
    assert on["markdown_fallback_parser"]["extractable"]
    # parse_code_blobs 即使首参传 <code> 标签，分支①未中后仍走函数内硬编码
    # 分支② markdown 兜底（```python/```py 围栏正则）；裸 JSON 才走分支③
    # ast.parse 裸 blob（POST1 情形，见 test_p5_offline_analysis_reuses_p1_artifacts）
    assert on["code_tag_parser"]["extractable"]
    off = p5.code_extract_metrics("plain prose only")
    assert not off["code_tag_parser"]["extractable"]
    assert off["literal_code_tags"] == 0
    tagged = p5.code_extract_metrics("<code>\nfinal_answer(2)\n</code>")
    assert tagged["literal_code_tags"] == 1


def test_p5_offline_analysis_reuses_p1_artifacts(tmp_path):
    d = tmp_path / "stage0"
    d.mkdir()
    (d / "p1_post1_resp.json").write_text(json.dumps({
        "outcome": "ok", "ledger_seq": 1,
        "response_text": GOOD_ARRAY}), encoding="utf-8")
    (d / "p1_post2_resp.json").write_text(json.dumps({
        "outcome": "ok", "ledger_seq": 2,
        "response_text": f"答案 {p1.ALPHA_MARKER}\n{p1.STOP_SENTINEL}",
        "judgment": {"pass": True}}), encoding="utf-8")
    rep = p5.analyze_p1_artifacts(d)
    assert rep["new_paid_posts"] == 0 and rep["sample_size"] == 2
    assert rep["post2_stop_compliance"] is True
    assert 0.0 <= rep["code_tag_extractable_rate"] <= 1.0
    assert len(rep["shim_dependencies"]) == 4
    with pytest.raises(FileNotFoundError):
        p5.analyze_p1_artifacts(tmp_path / "missing")


def test_p5_guard_and_missing_artifact_exit(monkeypatch, env, tmp_path):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    assert p5.main(["--offline-p1", str(tmp_path)]) == 2
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert p5.main(["--offline-p1", str(tmp_path / "nope")]) == 1
    rc = p5.main(["--offline-p1", str(tmp_path), "--out",
                  str(tmp_path / "p5.json")])
    # 缺 P1 证据件同样 exit 1
    assert rc == 1


# ===========================================================================
# P0 无外发
# ===========================================================================

p0 = _load("probe_p0_egress.py", "t3_p0")


def test_p0_static_scan_classifies(tmp_path):
    pkg = tmp_path / "smolagents"
    pkg.mkdir()
    (pkg / "core.py").write_text(
        "import requests\nx=1\n", encoding="utf-8")
    (pkg / "default_tools.py").write_text(
        "import requests\nresponse = requests.get(base_url)\n", encoding="utf-8")
    (pkg / "push.py").write_text(
        "from huggingface_hub import snapshot_download\n", encoding="utf-8")
    report = p0.static_scan(pkg)
    assert any("core.py:1" in h for h in report["net_primitive"])
    assert any("default_tools.py:1" in h for h in report["net_primitive"])
    assert any("push.py:1" in h for h in report["skills_clone"])
    assert report["tracing"] == [] and report["update_check"] == []
    cls = p0.classify_static(report, {"requests"})
    assert cls["tracing"]["status"] == "absent"
    assert cls["net_primitive_imports"]["status"] == "loaded_not_called"


def test_p0_cache_snapshot_diff(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "old").write_text("x")
    before = p0.snapshot_dirs([a, b])
    (a / "new").write_text("y")
    after = p0.snapshot_dirs([a, b])
    added = p0.diff_snapshots(before, after)
    assert added == {str(a): ["new"]}
    assert p0.diff_snapshots(after, after) == {}


def test_p0_socket_audit_logs_non_loopback(monkeypatch):
    # 先把底层 connect 替成假实现（audit 安装时捕获的“原 connect”即它），
    # 全程零真实 syscall、零触网。
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def _fake_connect(self, address):
        if address[0] not in ("127.0.0.1", "::1"):
            raise OSError("blocked in netns")

    monkeypatch.setattr(socket.socket, "connect", _fake_connect)
    audit = p0.install_socket_audit()

    class _S:
        pass

    try:
        socket.socket.connect(_S(), ("127.0.0.1", 9))
        with pytest.raises(OSError):
            socket.socket.connect(_S(), ("192.0.2.10", 80))
        hosts = [(r["host"], r["loopback"], r["api"]) for r in audit["log"]]
        assert ("127.0.0.1", True, "socket.connect") in hosts
        assert ("192.0.2.10", False, "socket.connect") in hosts
    finally:
        # connect_ex/create_connection audit 安装时包了真描述符，手动还原。
        socket.socket.connect_ex = real_connect_ex
        socket.create_connection = real_create


def test_p0_mock_agent_runs_two_turns_no_network(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = p0.run_mock_agent(ws)
    assert result["ok"] is True and result["model_turns"] == 2
    assert p0._ALPHA_MARKER in result["final_answer"]


def test_p0_guard_bare_run_exits_2(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    rc = p0.main(["--out", str(tmp_path / "p0.json")])
    assert rc == 2 and not (tmp_path / "p0.json").exists()


# ===========================================================================
# 源码红线（AC-T3-5/AC-T3-8 + 一次性探针纪律）
# ===========================================================================

PROBE_FILES = [
    "probe_p0_egress.py", "probe_p1_plaintext_tools.py",
    "probe_p2_native_tooluse.py", "probe_p3_failures.py",
    "probe_p5_gatewaymodel_smoke.py",
]


def test_redline_p4_absent_and_probe_set_exact():
    assert not (_PROBE_DIR / "probe_p4_apiv3_gated.py").exists()
    pys = sorted(p.name for p in _PROBE_DIR.glob("probe_p*.py"))
    assert pys == sorted(PROBE_FILES)


def test_redline_single_mention_and_no_graph_imports():
    for fname in PROBE_FILES:
        stem = fname[:-3]
        mentions: set[str] = set()
        for root in ("orchestrator", "scripts"):
            for py in (_REPO_ROOT / root).rglob("*.py"):
                if stem in py.read_text(encoding="utf-8", errors="replace"):
                    mentions.add(str(py.relative_to(_REPO_ROOT)))
        assert mentions == {f"scripts/phase25/probes/{fname}"}, (stem, mentions)
        tree = ast.parse((_PROBE_DIR / fname).read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
        plain = {".".join(a.name.split(".")[:2]) for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
        forbidden = {m for m in mods | plain
                     if m == "orchestrator.graph"
                     or (m or "").startswith(("orchestrator.graph.",
                                              "orchestrator.agentic"))}
        assert not forbidden, (fname, forbidden)


def test_redline_no_model_id_no_url_no_image():
    for fname in PROBE_FILES:
        src = (_PROBE_DIR / fname).read_text(encoding="utf-8")
        assert "kimi" not in src and "volces" not in src
        assert "https://" not in src and "http://" not in src
        # 不构造 image block（证据串里出现字段名也算）
        assert '"type": "image"' not in src and "'type': 'image'" not in src
        # 唯一 stage 名（P0 0 付费无网关，不定义 STAGE；其余四件必须钉死）
        if fname != "probe_p0_egress.py":
            assert 'STAGE = "coding_debug"' in src
        # 不提供模型覆盖入口
        tree = ast.parse(src)
        flags = {kw.value for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "add_argument"
                 for kw in n.args if isinstance(kw, ast.Constant)}
        assert "--model" not in flags and "--model-id" not in flags


def test_redline_paid_probes_single_attempt_and_guard():
    for fname in ("probe_p1_plaintext_tools.py",
                  "probe_p2_native_tooluse.py", "probe_p3_failures.py"):
        src = (_PROBE_DIR / fname).read_text(encoding="utf-8")
        assert "MAX_ATTEMPTS = 1" in src
        assert "QuotaLedger.phase25()" in src
        assert 'SIMPRAXIS_ALLOW_PAID' in src
        assert "max_attempts=MAX_ATTEMPTS" in src
        assert "billing_context(task=TASK" in src
        # 一次性/非生产路径纪律写进模块 docstring
        assert "不是生产路径" in src


def test_redline_all_probes_have_paid_guard():
    for fname in PROBE_FILES:
        src = (_PROBE_DIR / fname).read_text(encoding="utf-8")
        assert 'os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1"' in src
        assert "return EXIT_GUARD" in src


# ===========================================================================
# 活账集成（裸跑 skip；ledger_integration marker 故意不注册 waiver）
# ===========================================================================

@pytest.mark.ledger_integration
def test_live_p25_b1_ticket_and_ledger_rows(monkeypatch):
    if os.environ.get("SIMPRAXIS_LEDGER_INTEGRATION") != "1":
        pytest.skip("活账集成：需 SIMPRAXIS_LEDGER_INTEGRATION=1")
    from orchestrator.common.runtime_paths import (
        phase25_approvals_dir, phase25_ledger_path)
    from orchestrator.gateway.quota_ledger import (
        read_lines, validate_ledger_rows)
    ticket = json.loads((phase25_approvals_dir() / "P25-B1.json").read_text())
    assert ticket["approved"] == 6 and ticket["task"] == 3
    assert ticket["stage"] == "coding_debug"
    assert "interval_t3.json" in ticket["note"]
    rows = read_lines(phase25_ledger_path())
    validate_ledger_rows(rows)
    for r in rows:  # 所有实呼必须在 B1 票/任务 3/stage coding_debug 内
        assert r["batch_id"] == BATCH and r["task"] == 3
        assert r["stage"] == "coding_debug"
