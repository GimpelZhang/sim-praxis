"""S0-9 账套：命名空间 2908 独立、硬帽 12、任务帽与根隔离（零付费）。"""
from __future__ import annotations

import pytest

from orchestrator.gateway.quota_ledger import (LEDGER_RSI_S0, RSI_S0_HARD_CAP,
                                               RSI_S0_NAMESPACE, QuotaError,
                                               QuotaLedger, write_approval)


def test_namespace_is_2908_and_cap_is_12():
    assert LEDGER_RSI_S0 == 2908
    assert RSI_S0_HARD_CAP == 12
    assert dict(RSI_S0_NAMESPACE.task_caps) == {1: 12}


def test_ledger_paths_under_s0_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_S0_ROOT", str(tmp_path / "rsi_s0"))
    ledger = QuotaLedger.rsi_s0()
    path = RSI_S0_NAMESPACE.ledger_path_fn()
    assert str(path).startswith(str(tmp_path / "rsi_s0"))
    assert path.name == "ledger.jsonl"
    assert ledger.ns.phase == LEDGER_RSI_S0


def test_approval_task_must_belong_to_s0_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_S0_ROOT", str(tmp_path / "rsi_s0"))
    with pytest.raises(QuotaError):
        write_approval(batch_id="s0-probe-1", task=99, stage="coding_debug",
                       approved=1, beyond_normal=False, note="bad",
                       namespace=RSI_S0_NAMESPACE)
    path = write_approval(batch_id="s0-probe-1", task=1, stage="coding_debug",
                          approved=2, beyond_normal=False, note="s0 probe",
                          namespace=RSI_S0_NAMESPACE)
    assert path.is_file()


def test_approval_cannot_exceed_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_S0_ROOT", str(tmp_path / "rsi_s0"))
    with pytest.raises(QuotaError):
        write_approval(batch_id="s0-probe-2", task=1, stage="coding_debug",
                       approved=13, beyond_normal=False, note="over cap",
                       namespace=RSI_S0_NAMESPACE)


def test_root_isolation_refuses_shared_root(tmp_path, monkeypatch):
    shared = tmp_path / "phase2_shared"
    monkeypatch.setenv("SIMPRAXIS_RSI_S0_ROOT", str(shared))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(shared))
    with pytest.raises(Exception) as excinfo:
        QuotaLedger.rsi_s0()
    assert "互异" in str(excinfo.value) or "isolat" in str(excinfo.value).lower()
