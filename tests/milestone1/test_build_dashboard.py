"""build_dashboard.py 纯函数/实读口径单测（H-18），0 付费 0 联网。

覆盖：
  - _paid_block：total 只认真实触网行（call_lines 口径，seq 整数），
    preflight 拦截行（seq=null）单列 blocked_preflight，ledger_lines 对账；
  - _hitl_block pending：旧键 "approved" 改名 quota_approvals 且从批次
    批准件实读（缺失为 None，不编数字）；
  - _t8_row：closeout/gate 打回数/n_agent_runs 从结案信封实读；信封缺失
    即 unavailable（None），不再硬编码 done/0；human_notes 从 gate 行实读。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "build_dashboard_under_test",
    str(_REPO / "scripts" / "milestone1" / "build_dashboard.py"))
dash = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = dash
_SPEC.loader.exec_module(dash)

T8_THREAD = "t8-genesis-20261016a"


# ---------------- _paid_block ----------------

def test_paid_block_splits_real_calls_and_blocked():
    ledger = [
        {"seq": 1, "stage": "coding_debug", "task": "t", "ok": True,
         "batch_id": "B"},
        {"seq": 2, "stage": "visual_rollout", "task": "t", "ok": False,
         "batch_id": "B"},
        # preflight 拦截行：seq=null，未触网，不进 total
        {"seq": None, "stage": "coding_debug", "task": "t", "ok": False,
         "batch_id": "B"},
    ]
    pc = dash._paid_block(ledger)
    assert pc["total"] == 2 and pc["ok"] == 1 and pc["failed"] == 1
    assert pc["blocked_preflight"] == 1
    assert pc["ledger_lines"] == 3
    assert pc["hard_cap_ok"] is True


def test_paid_block_today_zero_blocked_equivalence():
    # 今日现场：29 行全部 seq 整数，两口径数值相等
    ledger = [{"seq": i + 1, "stage": "coding_debug", "task": "t",
               "ok": True, "batch_id": "B"} for i in range(29)]
    pc = dash._paid_block(ledger)
    assert pc["total"] == 29 and pc["blocked_preflight"] == 0
    assert pc["ledger_lines"] == 29


# ---------------- _hitl_block pending ----------------

def test_hitl_pending_quota_approvals_live_read(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "PHASE1_ROOT", tmp_path)
    adir = tmp_path / "quota" / "approvals"
    adir.mkdir(parents=True)
    (adir / "T9-B1.json").write_text(json.dumps({
        "batch_id": "T9-B1", "approved": 4, "beyond_normal": False}),
        encoding="utf-8")
    h = dash._hitl_block([])
    assert h["status"] == "pending"
    assert h["quota_approvals"] == 4
    assert "approved" not in h  # 旧键名必须消失


def test_hitl_pending_quota_approvals_missing_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "PHASE1_ROOT", tmp_path)
    h = dash._hitl_block([])
    assert h["status"] == "pending" and h["quota_approvals"] is None


# ---------------- _t8_row 实读 ----------------

def _patch_t8_inputs(monkeypatch, tmp_path, *, envelope, gates):
    """绕开现场 /mnt 依赖：batch/rung4/JSON 全合成。"""
    monkeypatch.setattr(dash, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(dash, "PHASE1_ROOT", tmp_path)
    monkeypatch.setattr(dash, "_envelopes", lambda: envelope)
    monkeypatch.setattr(dash, "_gates_rows", lambda: gates)
    monkeypatch.setattr(dash, "evaluate_batch", lambda wd: {
        "rung1": {"pass": True}, "rung2": {"pass": True},
        "rung3": {"pass": True}, "rules_pass": True, "seeds": 3,
        "groups": {}})
    monkeypatch.setattr(dash, "_rung4_answers",
                        lambda p: {"status": "ok", "yes": 1, "n": 1,
                                   "low_confidence": False, "answers": []})
    monkeypatch.setattr(dash, "_load_json", lambda p: {"schema": "x"})


def test_t8_row_unavailable_when_envelope_missing(tmp_path, monkeypatch):
    _patch_t8_inputs(monkeypatch, tmp_path, envelope={}, gates=[])
    row = dash._t8_row()
    assert row["closeout"] == "unavailable"
    assert row["closeout_source"] is None
    assert row["gate1_rejects"] is None
    assert row["gate2"]["rejects"] is None
    assert row["n_agent_runs"] is None


def test_t8_row_reads_done_envelope_and_gate_notes(tmp_path, monkeypatch):
    env = {T8_THREAD: {"status": "done", "gate1_rejects": 0,
                       "gate2_rejects": 0, "n_agent_runs": 1,
                       "thread_id": T8_THREAD}}
    gates = [{"thread_id": T8_THREAD, "gate": 1,
              "decision": "approve", "source": "card_form",
              "human_notes": ""},
             {"thread_id": T8_THREAD, "gate": 2,
              "decision": "approve", "source": "card_form",
              "human_notes": "真人意见X"}]
    _patch_t8_inputs(monkeypatch, tmp_path, envelope=env, gates=gates)
    row = dash._t8_row()
    assert row["closeout"] == "done"
    assert row["closeout_source"] == "DONE.json"
    assert row["n_agent_runs"] == 1
    assert row["gate1_rejects"] == 0 and row["gate2"]["rejects"] == 0
    assert row["gate2"]["human_notes"] == "真人意见X"  # 不再硬编码 ""
    # 循环分布从信封值取 agent_runs
    dist = dash._loop_distribution([], row, [])
    assert dist["per_thread"][T8_THREAD]["agent_runs"] == 1


def test_t8_row_reads_failure_envelope(tmp_path, monkeypatch):
    env = {T8_THREAD: {"status": "human_failure", "gate1_rejects": 2,
                       "gate2_rejects": 3, "n_agent_runs": 2,
                       "thread_id": T8_THREAD}}
    _patch_t8_inputs(monkeypatch, tmp_path, envelope=env, gates=[])
    row = dash._t8_row()
    assert row["closeout"] == "human_failure"
    assert row["closeout_source"] == "FAILURE.json"
    assert row["gate1_rejects"] == 2 and row["gate2"]["rejects"] == 3
    assert row["n_agent_runs"] == 2
