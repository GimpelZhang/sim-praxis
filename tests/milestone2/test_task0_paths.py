#!/usr/bin/env python3
"""Phase 2 Task0：runtime_paths 独立新账根 + 0700/0600 私有权限。"""
from __future__ import annotations

import json
import stat

from orchestrator.common import runtime_paths as rp
from orchestrator.common.fsutil import atomic_write_text


def test_phase2_root_private_dirs(tmp_path, monkeypatch):
    p1 = tmp_path / "phase1"
    p2 = tmp_path / "phase2"
    p1.mkdir()
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(p1))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))

    # 新账根与 Phase 1 根不同；缺省回退是 phase1 同级 phase2/
    assert rp.phase1_root() == p1
    assert rp.phase2_root() == p2
    assert rp.phase2_runs_root() == p2 / "runs"

    monkeypatch.delenv("SIMPRAXIS_PHASE2_ROOT", raising=False)
    assert rp.phase2_root() == p1.parent / "phase2"
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(p2))

    qdir = rp.phase2_quota_dir()
    assert qdir == p2 / "quota"
    assert stat.S_IMODE(qdir.stat().st_mode) == 0o700
    adir = rp.phase2_approvals_dir()
    assert stat.S_IMODE(adir.stat().st_mode) == 0o700
    assert rp.phase2_ledger_path() == qdir / "ledger.jsonl"
    assert rp.phase2_counters_path() == qdir / "counters.json"

    # 已存在但权限被放宽的目录同样收敛回 0700
    adir.chmod(0o755)
    assert rp.phase2_approvals_dir() == adir
    assert stat.S_IMODE(adir.stat().st_mode) == 0o700

    # 原子台账行 0600
    ledger = rp.phase2_ledger_path()
    atomic_write_text(ledger, json.dumps({"seq": 1, "probe": True}) + "\n")
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600

    # 熔断态目录由 env 决定，与账根解耦
    sdir = rp.gateway_state_dir()
    assert sdir == tmp_path / "gwstate"
    assert rp.gateway_state_path() == sdir / "quota_pause.json"
