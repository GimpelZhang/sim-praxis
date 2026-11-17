"""RSI-R1-N 传输加固（2026-11-17 用户裁决「新轮+加固」）的零付费单测。

背景：首轮 r1n-paid-20261117a 的 24 呼里 2 次是 provider 传输失败
（一次 120s 硬墙钟、一次 Server disconnected），按「每槽一次发送」的旧规则
132 槽几乎不可能跑完。新轮显式登记的三件事在这里逐条锁死：

1. 传输类错误允许至多重试到 ``max_attempts``，每次重试**逐行**写
   ``quota/retries.jsonl``（可审计，不是隐式重试）；
2. 空回答（HTTP 200 无正文）在 ``retry_on_empty=False`` 时**不重试**
   （不把模型输出问题当传输问题补考）；
3. 轮级重试预算用尽后立即失败（``RETRY_BUDGET_EXHAUSTED``），不再发网。

全部 monkeypatch，零触网、零费用。
"""
from __future__ import annotations

import json
import types

import pytest
import yaml
from litellm.exceptions import BadRequestError, InternalServerError

import orchestrator.gateway.router as R
from orchestrator.rsi_r1 import gateway_transport as GT

STAGE = "macro_ideation"
MODEL = "glm-5.3"


def _write_map(path):
    sm = {}
    for s in R.STAGES:
        caps = ["text", "vision"] if s == R.VISION_STAGE else ["text"]
        sm[s] = {"model_id": MODEL, "capabilities": caps, "purpose": "t"}
    path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm,
    }, allow_unicode=True, width=4096), encoding="utf-8")


def _ok(text="ok"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=text))],
        model=MODEL, id="resp-1",
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


def _transport_failing(n=1, *, empty_after=None, exc=None):
    """前 ``n`` 次抛传输类错误，之后返回正文（或空正文）。"""
    calls = []

    def fake(**_kw):
        calls.append(1)
        if len(calls) <= n:
            raise (exc or InternalServerError(
                "Server disconnected without sending a response.",
                "anthropic", MODEL, None))
        if empty_after is not None:
            return _ok("")
        return _ok()
    fake.calls = calls
    return fake


def _gateway(tmp_path, transport, **kw):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    base = dict(unguarded=True, stage_models_path=map_path,
                transport=transport, sleep=lambda _s: None)
    base.update(kw)
    return R.ModelGateway(**base)


def test_transport_retry_is_logged_and_succeeds(tmp_path):
    retry_log = tmp_path / "quota" / "retries.jsonl"
    fake = _transport_failing(1)
    gw = _gateway(tmp_path, fake, max_attempts=3, retry_budget=5,
                  retry_log_path=retry_log)
    text, _tokens = gw.call(STAGE, [{"role": "user", "content": "hi"}])
    assert text == "ok"
    assert len(fake.calls) == 2                      # 1 首发 + 1 重试
    lines = retry_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["schema"] == "simpraxis.gateway.retry.v1"
    assert row["cause"] == "InternalServerError"
    assert row["attempt"] == 1


def test_transport_retry_budget_exhausted_stops_sending(tmp_path):
    retry_log = tmp_path / "quota" / "retries.jsonl"
    fake = _transport_failing(5)
    gw = _gateway(tmp_path, fake, max_attempts=10, retry_budget=2,
                  retry_log_path=retry_log)
    with pytest.raises(R.GatewayError, match="RETRY_BUDGET_EXHAUSTED"):
        gw.call(STAGE, [{"role": "user", "content": "hi"}])
    # 预算 2 → 只可能发生 2 次重试（首发 + 2 = 3 次 HTTP 尝试）后即停
    assert len(fake.calls) == 3
    assert len(retry_log.read_text(encoding="utf-8").splitlines()) == 3


def test_non_transport_error_is_not_retried(tmp_path):
    retry_log = tmp_path / "quota" / "retries.jsonl"
    fake = _transport_failing(9, exc=BadRequestError(
        "bad body", "anthropic", MODEL))
    gw = _gateway(tmp_path, fake, max_attempts=3, retry_budget=5,
                  retry_log_path=retry_log)
    with pytest.raises(R.NonRetryableError):
        gw.call(STAGE, [{"role": "user", "content": "hi"}])
    assert len(fake.calls) == 1
    assert not retry_log.exists() or retry_log.read_text() == ""


def test_empty_response_not_retried_when_disabled(tmp_path):
    retry_log = tmp_path / "quota" / "retries.jsonl"
    fake = _transport_failing(0, empty_after=True)
    gw = _gateway(tmp_path, fake, max_attempts=3, retry_budget=5,
                  retry_log_path=retry_log, retry_on_empty=False)
    with pytest.raises(R.EmptyResponseError, match="空回答"):
        gw.call(STAGE, [{"role": "user", "content": "hi"}])
    assert len(fake.calls) == 1


def test_empty_response_retried_by_default(tmp_path):
    fake = _transport_failing(0, empty_after=True)
    gw = _gateway(tmp_path, fake, max_attempts=2, retry_budget=5)
    with pytest.raises(R.EmptyResponseError, match="空回答"):
        gw.call(STAGE, [{"role": "user", "content": "hi"}])
    assert len(fake.calls) == 2


class _FakeGateway:
    def __init__(self, attempts):
        self.max_attempts = attempts
        self._billing = object()

    def call(self, *_a, **_kw):  # pragma: no cover - 本测试不触达
        raise AssertionError("不应调用")


def test_transport_declares_expected_attempts():
    gw = _FakeGateway(3)
    t = GT.GatewayTransport(gateway=gw, lane="probe", batch_id="b",
                            expected_attempts=3, timeout=300)
    assert t.expected_attempts == 3 and t.timeout == 300
    with pytest.raises(GT.TransportError, match="HIDDEN_RETRY"):
        GT.GatewayTransport(gateway=_FakeGateway(1), lane="probe",
                            batch_id="b", expected_attempts=3)


def test_build_transport_passes_timeout_and_attempts(monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    t = GT.build_transport(gateway=_FakeGateway(3), lane="probe",
                           batch_id="b", env={"SIMPRAXIS_ALLOW_PAID": "1"},
                           timeout=300, expected_attempts=3)
    assert t.timeout == 300 and t.expected_attempts == 3


def test_reply_attempts_bounded_by_declared_policy():
    """Reply 只按**声明值**放宽；旧轮（无声明＝1）仍判 HIDDEN_RETRY。"""
    from orchestrator.rsi_r1 import loop as L

    ok = L.Reply(text="x", response_id="r", attempts_http=3,
                 expected_attempts=3)
    assert ok.attempts_http == 3
    with pytest.raises(L.LoopError, match="HIDDEN_RETRY"):
        L.Reply(text="x", response_id="r", attempts_http=2)
    with pytest.raises(L.LoopError, match="HIDDEN_RETRY"):
        L.Reply(text="x", response_id="r", attempts_http=4,
                expected_attempts=3)
