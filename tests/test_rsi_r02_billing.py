"""RSI-R0.2 计费账测试（AC13/AC14）：预留、结算、未知与触帽。

全部在 tmp campaign 根下跑：不触网、不读真实账。
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import campaign as camp


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r01"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r02"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", "r02_bill")
    yield


@pytest.fixture
def ctx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="Junchuan Zhang",
               GIT_AUTHOR_EMAIL="zjunchuan@gmail.com",
               GIT_COMMITTER_NAME="Junchuan Zhang",
               GIT_COMMITTER_EMAIL="zjunchuan@gmail.com")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "x.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text("authorized\n", encoding="utf-8")
    return camp.create_campaign(campaign_id="r02_bill", plan_path=plan,
                                consent_path=consent, repo_root=repo,
                                root=tmp_path / "rsi_r02")


def _book(ctx, policy=None):
    return B.BillingBook(ctx) if policy is None else B.BillingBook(ctx, policy=policy)


def _usage(in_tok=1000, out_tok=200, cache_read=0, cache_create=0):
    return B.Usage.normalize({"input_tokens": in_tok, "output_tokens": out_tok,
                              "cache_read_input_tokens": cache_read,
                              "cache_creation_input_tokens": cache_create})


# ---- 预留与上界 ----


def test_reserve_writes_pending_before_send(ctx):
    book = _book(ctx)
    rec = book.reserve(call_id="c1", slot_id="E-dev#1")
    assert rec.status == B.STATUS_PENDING and rec.reserved_usd == 1.5
    path = ctx.root / "quota" / "reservations" / "c1.json"
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    doc = json.loads(path.read_text())
    assert doc["status"] == "PENDING" and doc["settled_usd"] is None
    s = book.summary()
    assert s["settled_usd"] == 0.0 and s["pending_usd"] == 1.5
    assert s["headroom_usd"] == 97.0


def test_duplicate_call_and_occupied_slot_refused(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="slot-1")
    with pytest.raises(B.BillingError):
        book.reserve(call_id="c1", slot_id="slot-2")
    with pytest.raises(B.BillingError):
        book.reserve(call_id="c2", slot_id="slot-1")


def test_budget_cap_boundary(ctx):
    # 判据是 settled + pending + 预留 > cap 才拒；cap=4.5 时第 3 笔恰好 4.5 通过，第 4 笔 6.0 拒
    ok_book = _book(ctx, B.BillingPolicy(cap_usd=4.5))
    ok_book.reserve(call_id="a", slot_id="s1")
    ok_book.reserve(call_id="b", slot_id="s2")
    ok_book.reserve(call_id="c", slot_id="s3")      # 4.5 <= 4.5：边界放行
    # headroom = cap - settled - pending - 预留 = 4.5 - 4.5 - 1.5 = -1.5（不够再发一笔）
    assert ok_book.summary()["headroom_usd"] == -1.5
    with pytest.raises(B.BudgetBlocked):
        ok_book.reserve(call_id="d", slot_id="s4")


def test_budget_block_message_keeps_numbers(ctx):
    book = _book(ctx, B.BillingPolicy(cap_usd=2.0))
    book.reserve(call_id="a", slot_id="s1")
    with pytest.raises(B.BudgetBlocked) as ei:
        book.reserve(call_id="b", slot_id="s2")
    msg = str(ei.value)
    assert "1.500000" in msg and "cap=2.00" in msg


# ---- 结算 ----


def test_settle_exact_releases_difference(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.settle(call_id="c1", usage=_usage())
    assert rec.status == B.STATUS_SETTLED
    assert rec.cost_status == B.COST_EXACT
    assert rec.settled_usd == pytest.approx(0.0018)
    s = book.summary()
    assert s["settled_usd"] == pytest.approx(0.0018)
    assert s["pending_usd"] == 0.0


def test_cached_usage_is_bounded_upper(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.settle(call_id="c1", usage=_usage(cache_read=500, cache_create=100))
    known = (1000 * 1.0 + 200 * 4.0) / 1e6
    upper = known + (500 + 100) * 4.0 / 1e6
    assert rec.cost_status == B.COST_BOUNDED_UPPER
    assert rec.settled_usd == pytest.approx(upper)


def test_absent_cache_fields_bounded_by_input(ctx):
    usage = B.Usage.normalize({"input_tokens": 1000, "output_tokens": 200})
    cost = B.cost_of(usage)
    assert cost.status == B.COST_BOUNDED_UPPER
    assert cost.usd == pytest.approx((1000 * 1.0 + 200 * 4.0 + 1000 * 4.0) / 1e6)
    assert cost.detail["reason"] == "cache_fields_absent_bounded_by_input"


def test_usage_missing_fields_not_zero():
    u = B.Usage.normalize({})
    assert u.in_tokens is None and u.out_tokens is None
    assert u.prompt_total is None
    assert "input_tokens" in u.unknown_fields
    assert B.cost_of(u).status == B.COST_UNKNOWN
    assert B.cost_of(B.Usage.normalize(None)).status == B.COST_UNKNOWN


def test_prompt_total_only_when_cache_zero():
    assert B.Usage.normalize({"input_tokens": 10, "output_tokens": 1,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}).prompt_total == 10
    assert B.Usage.normalize({"input_tokens": 10, "output_tokens": 1,
                              "cache_read_input_tokens": 5,
                              "cache_creation_input_tokens": 0}).prompt_total is None


# ---- 未知与暂停 ----


def test_missing_usage_marks_unknown_keeps_reservation_and_pauses(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.settle(call_id="c1", usage=B.Usage.normalize(None))
    assert rec.status == B.STATUS_UNKNOWN and rec.settled_usd is None
    s = book.summary()
    assert s["pending_usd"] == 1.5          # 预留不释放
    assert s["unknown_call_ids"] == ["c1"]
    assert s["paused"] is True
    with pytest.raises(B.BillingPaused):
        book.reserve(call_id="c2", slot_id="s2")


def test_cost_over_reservation_marks_unknown(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.settle(call_id="c1", usage=_usage(out_tok=500_000))  # 2.0 > 1.5
    assert rec.status == B.STATUS_UNKNOWN
    assert rec.failure.startswith("cost_exceeds_reservation")
    assert book.pause_reason() is not None


def test_settlement_exceeds_cap_guarded(ctx):
    _book(ctx).reserve(call_id="c1", slot_id="s1")
    tight = _book(ctx, B.BillingPolicy(cap_usd=0.2))
    rec = tight.settle(call_id="c1", usage=_usage(in_tok=100_000, out_tok=50_000))
    assert rec.status == B.STATUS_UNKNOWN
    assert rec.failure.startswith("settlement_exceeds_cap")


def test_mark_unknown_and_resolve(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.mark_unknown(call_id="c1", failure="send_state_ambiguous")
    assert rec.status == B.STATUS_UNKNOWN
    assert "UNKNOWN_SENT" in (book.pause_reason() or "")
    with pytest.raises(B.BillingError):
        book.settle(call_id="c1", usage=_usage())      # 未知记录不可重复结算
    fixed = book.resolve(call_id="c1", resolution={"source": "provider_log",
                                                   "request_id": "req-x"},
                         settled_usd=0.002, cost_status=B.COST_EXACT)
    assert fixed.status == B.STATUS_SETTLED and fixed.settled_usd == 0.002
    assert "provider_log" in (fixed.failure or "")
    book.clear_pause(resolution="provider log matched request_id=req-x")
    assert book.pause_reason() is None
    book.reserve(call_id="c2", slot_id="s2")           # 解除后可继续


def test_clear_pause_requires_resolution(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    book.mark_unknown(call_id="c1", failure="x")
    with pytest.raises(B.BillingError):
        book.clear_pause(resolution="   ")


def test_policy_recorded_as_not_supplier_guarantee(ctx):
    book = _book(ctx)
    s = book.summary()
    assert s["policy"]["claimed_as_supplier_guarantee"] is False
    assert s["policy"]["cap_usd"] == 100.0
    assert s["policy"]["reservation_usd"] == 1.5


# ---- 发网前阻断释放（§7「发网前阻断单列零POST」） ----


def test_release_pending_is_zero_settled_and_never_unknown(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    rec = book.release(call_id="c1", reason="task_hard_cap")
    assert rec.status == B.STATUS_SETTLED
    assert rec.settled_usd == 0.0 and rec.cost_status == B.COST_EXACT
    assert "PREFLIGHT_BLOCKED" in (rec.failure or "")
    assert book.pause_reason() is None            # 零 POST 不触发费用暂停
    s = book.summary()
    assert s["settled_usd"] == 0.0 and s["unknown_call_ids"] == []
    assert s["pending_usd"] == 0.0 and s["paused"] is False


def test_release_requires_reason_and_pending_state(ctx):
    book = _book(ctx)
    book.reserve(call_id="c1", slot_id="s1")
    with pytest.raises(B.BillingError):
        book.release(call_id="c1", reason="   ")
    book.release(call_id="c1", reason="ok")
    with pytest.raises(B.BillingError):
        book.release(call_id="c1", reason="again")   # 已结清，不得二次释放
