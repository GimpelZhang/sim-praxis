"""T6 协议单测（AC28）：零网络、零付费、零容器。

锁死三件事：先登记后跑（预登记 hash 不符即拒）、单链失败即停不补考、
长链区间无法证明时**零 POST** 记 BLOCKED_CONTEXT_MEASUREMENT。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import paid as P
from orchestrator.rsi_r01 import protocol as PR
from orchestrator.rsi_r01 import transport as T


class StubTokenizer:
    """只做计数：4 字节 ≈ 1 token（测试用，不声称与供应商一致）。"""

    sha256 = "f" * 64
    path = "stub-tokenizer.json"

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)


class FakeTool(PR.ToolExec):
    """不跑容器的工具替身：按预登记内容返回同口径 sha256。"""

    def __init__(self) -> None:
        super().__init__(root=Path("/nonexistent"))
        self.calls: "list[tuple[str, str]]" = []
        self.content = ""

    def prepare(self, *, name: str = PR.MARKER_NAME,
                content: "str | None" = None) -> "dict[str, str]":
        import hashlib
        self.content = content or "deadbeef"
        raw = (self.content + "\n").encode("utf-8")
        return {"name": name, "content": self.content,
                "host_sha256": hashlib.sha256(raw).hexdigest()}

    def read(self, name: str, *, timeout_s: int = 120) -> "dict[str, Any]":
        import hashlib
        self.calls.append((name, self.content))
        raw = (self.content + "\n").encode("utf-8")
        return {"ok": True, "name": name, "content": self.content,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "executed_in_container": True}


def _campaign(tmp_path: Path) -> C.CampaignContext:
    plan = tmp_path / "plan.md"
    consent = tmp_path / "consent.txt"
    plan.write_text("plan-bytes", encoding="utf-8")
    consent.write_text("consent-bytes", encoding="utf-8")
    return C.create_campaign(campaign_id="rt01_proto", plan_path=plan,
                             consent_path=consent, repo_root=L.REPO_ROOT,
                             root=tmp_path / "campaigns")


def _scripted_sender(prereg: dict, *, fail_at: "int | None" = None,
                     usage_extra: int = 0):
    """按预登记顺序逐件回话；回话内容引用当轮 nonce/标记。"""
    order = {rec["request_sha256"]: rec for rec in prereg["posts"]}
    state = {"n": 0}

    def sender(plan: T.SendPlan):
        rec = order.get(plan.sha256)
        assert rec is not None, "发送了未预登记的请求"
        state["n"] += 1
        if fail_at is not None and state["n"] == fail_at:
            raise RuntimeError("network down")
        nonce = rec["nonce"]
        content = PR.marker_for(nonce)
        import hashlib
        sha = hashlib.sha256((content + "\n").encode("utf-8")).hexdigest()
        idx = int(rec["index"])
        if rec["kind"] == "short_text":
            body = {"nonce": nonce, "ok": True}
        elif rec["kind"] == "tool_roundtrip":
            body = ({"action": {"tool": "read_file", "name": PR.MARKER_NAME},
                     "nonce": nonce} if idx == 1 else
                    {"nonce": nonce, "value": content, "sha256": sha})
        else:
            body = ({"action": {"tool": "read_file", "name": PR.MARKER_NAME},
                     "nonce": nonce} if idx == 1 else
                    {"nonce": nonce, "value": content, "sha256": sha,
                     "seen": True})
        text = json.dumps(body, sort_keys=True)
        tok = len(PR.render_text(plan)) // 4
        usage = {"input_tokens": tok + usage_extra,
                 "output_tokens": 8}
        meta = {"content_blocks": [{"type": "text", "text": text}],
                "http_status": 200, "request_id": f"req-{state['n']}",
                "stop_reason": "end_turn", "usage": usage}
        return meta, text
    return sender


def _runner(ctx: C.CampaignContext, tokenizer: "object | None" = None) -> PR.ProtocolRunner:
    return PR.ProtocolRunner(ctx=ctx, model="probe-model", tokenizer=tokenizer,
                             transport=P.PaidTransport(ctx=None),
                             root=ctx.root / "analysis" / "t6_protocol")


def test_prereg_is_deterministic_and_complete(tmp_path):
    ctx = _campaign(tmp_path)
    doc = PR.build_prereg(model="probe-model", tokenizer=StubTokenizer(),
                          nonces={i: f"n{i:04d}" for i in range(1, 7)})
    assert doc["post_count"] == 12
    assert len(doc["posts"]) == 12
    by_slot: "dict[int, list[dict]]" = {}
    for rec in doc["posts"]:
        by_slot.setdefault(rec["slot"], []).append(rec)
    assert [len(by_slot[s]) for s in range(1, 7)] == [1, 1, 2, 2, 3, 3]
    # 文字相同 ⇒ hash 相同（可复算）；换 nonce ⇒ hash 变
    again = PR.build_prereg(model="probe-model", tokenizer=StubTokenizer(),
                            nonces={i: f"n{i:04d}" for i in range(1, 7)})
    assert [r["request_sha256"] for r in again["posts"]] == \
           [r["request_sha256"] for r in doc["posts"]]


def test_interval_unprovable_without_tokenizer():
    plan = T.plan_from_messages(system=None,
                                messages=[{"role": "user", "content": "x" * 100}],
                                model="m")
    acc = PR.count_plan(plan, interval=(15000, 18000))
    assert acc.verdict == T.AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
    assert "LOWER_BOUND_UNPROVABLE" in acc.reasons


def test_short_chain_without_tokenizer_blocks_long_with_zero_posts(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    ctx = _campaign(tmp_path)
    runner = _runner(ctx, tokenizer=None)
    prereg = runner.write_prereg(nonces={i: f"n{i:04d}" for i in range(1, 7)})
    report = runner.run(sender=_scripted_sender(prereg), tool=FakeTool())
    assert report["verdict"] == "FAILED"
    assert report["blocked_code"] == "BLOCKED_CONTEXT_MEASUREMENT"
    assert report["posts_sent"] == 6                 # 只有前 6 个（短+工具）
    long_rows = [r for r in report["slots"] if r["slot"] in (5, 6)]
    by_slot = {r["slot"]: r for r in long_rows}
    assert by_slot[5]["state"] == "FAILED"
    assert by_slot[5]["code"] == "BLOCKED_CONTEXT_MEASUREMENT"
    assert by_slot[6]["state"] == "CANCELLED"        # 失败即停，不补考
    # 零长链 POST：没有任何 slot 5/6 的发送记录
    assert not [p for p in report["posts"] if p["slot"] in (5, 6)]


def test_full_protocol_passes_with_tokenizer_band(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    ctx = _campaign(tmp_path)
    runner = _runner(ctx, tokenizer=StubTokenizer())
    prereg = runner.write_prereg(nonces={i: f"n{i:04d}" for i in range(1, 7)})
    report = runner.run(sender=_scripted_sender(prereg), tool=FakeTool())
    assert report["verdict"] == "PASS", report.get("blocked_detail")
    assert report["posts_sent"] == 12
    assert report["overhead_band"] == [0, 0]
    for row in report["slots"]:
        assert row["state"] == "PASS", row


def test_prereg_mismatch_refused_before_any_send(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    ctx = _campaign(tmp_path)
    runner = _runner(ctx)
    runner.write_prereg(nonces={i: f"n{i:04d}" for i in range(1, 7)})
    path = ctx.root / runner.prereg_relpath()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["posts"][0]["request"]["messages"][0]["content"] = "tampered"
    path.write_text(json.dumps(doc), encoding="utf-8")
    sent: "list[int]" = []

    def sender(plan):
        sent.append(1)
        raise AssertionError("不应发送")
    with pytest.raises(PR.ProtocolBlocked) as exc:
        runner.run(sender=sender, tool=FakeTool())
    assert exc.value.code == "PREREG_MISMATCH"
    assert sent == []


def test_failure_stops_remaining_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    ctx = _campaign(tmp_path)
    runner = _runner(ctx)
    prereg = runner.write_prereg(nonces={i: f"n{i:04d}" for i in range(1, 7)})
    report = runner.run(sender=_scripted_sender(prereg, fail_at=2),
                        tool=FakeTool())
    assert report["verdict"] == "FAILED"
    assert report["blocked_code"] == "CALL_FAILED"
    # 第 2 个 POST 状态不确定：仍计入 posts_sent（可能已发网），但不重发
    assert report["posts_sent"] == 2
    assert report["posts"][-1]["attempt"]["state"] == "unknown_sent"
    assert report["slots"][-1]["state"] == "CANCELLED"


def test_provider_usage_outside_declared_bound_stops_chain(tmp_path, monkeypatch):
    """事后证伪：供应商 usage 越出发网前声明的计数界 ⇒ 仪器故障停链。"""
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    ctx = _campaign(tmp_path)
    runner = _runner(ctx)
    prereg = runner.write_prereg(nonces={i: f"n{i:04d}" for i in range(1, 7)})
    report = runner.run(sender=_scripted_sender(prereg, usage_extra=500),
                        tool=FakeTool())
    assert report["verdict"] == "FAILED"
    assert report["blocked_code"] == "ACCOUNTING_MISMATCH"
    assert report["posts_sent"] == 1        # 第 1 个 POST 就停，不补考
    assert all(r["state"] == "CANCELLED" for r in report["slots"][1:])
