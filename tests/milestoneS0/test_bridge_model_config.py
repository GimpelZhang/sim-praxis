"""S0-6b：模型接缝——1 逻辑调用 ≤1 账行、stage 钉死、image 触网前拒绝、无模型 ID 字面量。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.rsi_bridge.model_config import (STAGE_BY_PURPOSE,
                                                  GatewayLanguageProposer,
                                                  ModelConfigError)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeGateway:
    """替身网关：每次逻辑调用恰写一行"账"（模拟 BillingGate 一次 seq）。"""

    def __init__(self, text="ok", tokens=(11, 7)):
        self.calls = []
        self.ledger_rows = []
        self._text = text
        self._tokens = tokens

    def call(self, stage, messages, *, max_tokens=512, temperature=0.2,
             timeout=120, thinking=None, capture=None):
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens, "capture": capture})
        self.ledger_rows.append({"stage": stage, "seq": len(self.ledger_rows) + 1})
        return self._text, self._tokens


def test_stage_mapping_is_pinned():
    assert STAGE_BY_PURPOSE["plan"] == "macro_ideation"
    assert STAGE_BY_PURPOSE["ideate"] == "macro_ideation"
    for purpose in ("diagnose", "design", "implement"):
        assert STAGE_BY_PURPOSE[purpose] == "coding_debug"


def test_one_logical_call_writes_at_most_one_ledger_row():
    gateway = FakeGateway()
    proposer = GatewayLanguageProposer(gateway)
    out = proposer.propose("plan", [{"role": "user", "content": "design A_N"}])
    assert out["stage"] == "macro_ideation"
    assert out["tokens"] == {"in": 11, "out": 7}
    assert len(gateway.calls) == 1
    assert len(gateway.ledger_rows) == 1
    proposer("diagnose the failed batch", purpose="diagnose")
    assert len(gateway.calls) == 2 and len(gateway.ledger_rows) == 2


def test_unknown_purpose_rejected_before_call():
    gateway = FakeGateway()
    with pytest.raises(ModelConfigError):
        GatewayLanguageProposer(gateway).propose("visual", [{"role": "user", "content": "x"}])
    assert gateway.calls == []


def test_image_block_rejected_before_network():
    gateway = FakeGateway()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}]
    with pytest.raises(ModelConfigError) as excinfo:
        GatewayLanguageProposer(gateway).propose("design", messages)
    assert "image" in str(excinfo.value)
    assert gateway.calls == []


def test_empty_messages_and_prompt_rejected():
    gateway = FakeGateway()
    with pytest.raises(ModelConfigError):
        GatewayLanguageProposer(gateway).propose("plan", [])
    with pytest.raises(ModelConfigError):
        GatewayLanguageProposer(gateway)("   ", purpose="plan")
    assert gateway.calls == []


def test_module_contains_no_model_id_literals():
    source = (REPO_ROOT / "orchestrator" / "rsi_bridge" / "model_config.py").read_text(
        encoding="utf-8")
    banned = re.compile(
        r"(deepseek|glm|kimi|mimo|claude-|gpt-|qwen|v4\.1|v2\.6|v3\.1)",
        re.IGNORECASE)
    assert banned.search(source) is None
    assert "requests" not in source and "http" not in source.lower().replace("http 200", "")
