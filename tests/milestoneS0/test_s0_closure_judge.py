"""S0 结案机判：可失败（空证据 → problems）与全绿合成证据两向。"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _canonical_ledger(tmp_path, monkeypatch, lines: str, approved: int = 2):
    """把 2908 根指到 tmp，并在规范路径写账本 + 批准件。"""
    import os
    root = tmp_path / "rsi_s0"
    (root / "quota" / "approvals").mkdir(parents=True, exist_ok=True)
    ledger = root / "quota" / "ledger.jsonl"
    ledger.write_text(lines, encoding="utf-8")
    (root / "quota" / "approvals" / "b1.json").write_text(
        json.dumps({"batch_id": "b1", "task": 1, "stage": "coding_debug",
                    "approved": approved}), encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_RSI_S0_ROOT", str(root))
    return ledger


def _run(probe: Path, audit: Path, report: Path):
    cmd = ["/mnt/venvs/simpraxis/bin/python",
           str(REPO_ROOT / "scripts" / "verification" / "check_s0_closure.py"),
           "--probe", str(probe), "--audit", str(audit), "--report", str(report)]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_empty_evidence_fails(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    audit = tmp_path / "audit.json"
    _write(audit, {"pass": False, "checks": [{"id": "x", "pass": False}]})
    report = tmp_path / "judge.json"
    proc = _run(probe, audit, report)
    assert proc.returncode == 1
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert verdict["problems"] and not verdict["pass"]


def test_synthetic_green_evidence_passes(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    _write(probe / "report.json", {
        "schema": "simpraxis.s0.probe_report.v1",
        "exits": {"E1": True, "E2": True, "E3": True, "E4": True},
        "session": {"returncode": 0},
    })
    _write(probe / "e1_host_judgement.json", {
        "ladder": {key: True for key in (
            "A1_effective_response", "A2_artifacts_written", "A3_real_load",
            "A4_physics_stepping", "A5_functional_target")},
        "claim_matches_host": True, "host_displacement_m": 0.2,
    })
    ledger = _canonical_ledger(tmp_path, monkeypatch,
        '{"event":"call","seq":1,"ts_iso":"2026-11-18T00:00:00Z",'
        '"batch_id":"b1","ok":true}\n'
        '{"event":"call","seq":2,"ts_iso":"2026-11-18T00:00:01Z",'
        '"batch_id":"b1","ok":true}\n')
    _write(probe / "e2_ledger_reconciliation.json", {
        "pass": True, "seqs": [1, 2], "ts_all_z": True, "batch_rows": 2,
        "batch_id": "b1", "before_seq": 0, "new_call_rows": 2,
        "ledger_path": str(ledger),
        "results": [{"ok": True}, {"ok": True}],
    })
    _write(probe / "e3_reconstruction.json", {
        "session_id": "s", "model": "m", "tool_calls": [{"name": "Write"}],
        "write_replay_mismatches": [],
    })
    _write(probe / "e4_isolation.json", {"pass": True})
    _write(probe / "e4_gate_rehearsal.json", {"pass": True})
    audit = tmp_path / "audit.json"
    _write(audit, {"pass": True, "checks": [{"id": "imports", "pass": True}]})
    report = tmp_path / "judge.json"
    proc = _run(probe, audit, report)
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert proc.returncode == 0, verdict
    assert verdict["problems"] == []
    assert verdict["pass"] is True


def test_ledger_mismatch_against_probe_self_report_fails(tmp_path, monkeypatch):
    """探针自报与账本复算不符必须判红（评审 HIGH 的回归）。"""
    probe = tmp_path / "probe"
    _write(probe / "report.json", {
        "schema": "simpraxis.s0.probe_report.v1",
        "exits": {"E1": True, "E2": True, "E3": True, "E4": True},
    })
    _write(probe / "e1_host_judgement.json", {
        "ladder": {key: True for key in (
            "A1_effective_response", "A2_artifacts_written", "A3_real_load",
            "A4_physics_stepping", "A5_functional_target")},
        "claim_matches_host": True, "host_displacement_m": 0.2,
    })
    ledger = _canonical_ledger(tmp_path, monkeypatch,
        '{"event":"call","seq":1,"ts_iso":"2026-11-18T00:00:00Z",'
        '"batch_id":"b1","ok":true}\n')
    _write(probe / "e2_ledger_reconciliation.json", {
        "pass": True, "seqs": [1, 2], "ts_all_z": True, "batch_rows": 2,
        "batch_id": "b1", "before_seq": 0, "new_call_rows": 2,
        "ledger_path": str(ledger),
        "results": [{"ok": True}, {"ok": True}],
    })
    _write(probe / "e3_reconstruction.json", {
        "session_id": "s", "model": "m", "tool_calls": [{"name": "Write"}],
        "write_replay_mismatches": [],
    })
    _write(probe / "e4_isolation.json", {"pass": True})
    _write(probe / "e4_gate_rehearsal.json", {"pass": True})
    audit = tmp_path / "audit.json"
    _write(audit, {"pass": True, "checks": [{"id": "imports", "pass": True}]})
    report = tmp_path / "judge.json"
    proc = _run(probe, audit, report)
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert proc.returncode == 1
    assert any("新增账行" in p or "seqs" in p or "seq" in p for p in verdict["problems"])
