"""RSI-R0.1 结案器/复验器单测（T4 / AC25）：零网络、零容器。

fixture 覆盖计划 §9.3 每个事件；复验器用**独立实现**重推，测试同时验证它能抓
住被篡改的 facts（不是同义反复）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts" / "rsi_r01", _REPO / "scripts" / "verification"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_rsi_r01_closure as CHK  # noqa: E402
import closeout as CO  # noqa: E402

from orchestrator.rsi_r01 import schemas as S  # noqa: E402


def test_every_section_93_event_has_expected_fixture(tmp_path):
    summary = CO.write_fixtures(tmp_path / "fixtures")
    assert summary["all_ok"], summary["problems"]
    assert len(summary["results"]) == len(CO.EVENT_EXPECTATIONS)


def test_fixture_process_axis_expectations(tmp_path):
    summary = CO.write_fixtures(tmp_path / "fixtures")
    for event, expected in CO.PROCESS_EXPECTATIONS.items():
        assert summary["results"][event]["process_ok"], event


def test_priority_override_keeps_all_reason_codes():
    facts = CO.fixture("safety_breach")
    facts["validation"] = CO.fixture("validation_ceiling")["validation"]
    decision = CO.decide(facts)
    assert decision["measurement_verdict"] == S.MeasurementVerdict.SAFETY_INVALID
    assert S.MeasurementVerdict.CEILING in decision["triggered_verdicts"]
    assert "SAFETY_INCIDENT" in decision["reason_codes"]
    assert "VALIDATION_FLOOR" in decision["reason_codes"]


def test_unknown_sent_unresolved_outranks_validation_floor():
    facts = CO.fixture("validation_ceiling")
    facts["unknown_sent"].update({"count": 1, "unresolved": 1})
    decision = CO.decide(facts)
    assert decision["measurement_verdict"] == S.MeasurementVerdict.BLOCKED_TRANSPORT
    assert S.ReasonCode.UNRESOLVED_SEND in decision["reason_codes"]
    assert decision["process_verdict"] == S.ProcessVerdict.ABORTED_INCOMPLETE


def test_family_denominators_keep_invalid_slots_visible():
    facts = CO.fixture("validation_floor_research")
    decision = CO.decide(facts)
    assert decision["families"]["R"] == {"assigned": 2, "success": 0,
                                         "effective": 2, "invalid": []}
    facts["validation"]["slots"][0]["state"] = S.TrialState.INVALID
    decision = CO.decide(facts)
    assert decision["families"]["E"]["invalid"] == [facts["validation"]["slots"][0]["slot"]]
    assert decision["measurement_verdict"] == S.MeasurementVerdict.INVALID_INSTRUMENT


def test_ledger_call_rows_exclude_events_and_preflight(tmp_path):
    root = tmp_path / "camp"
    (root / "quota").mkdir(parents=True)
    rows = [
        {"event": "selfcheck", "seq": 1, "model": None},          # 事件行
        {"event": "call", "seq": 2, "status": "ok"},
        {"event": "call", "seq": 3, "status": "ok"},
        {"event": "preflight_blocked", "seq": None},              # 占批次不占实呼
    ]
    (root / "quota" / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    facts = CO.gather(root)
    assert facts["ledger"] == {"rows": 4, "call_rows": 2, "event_rows": 2,
                               "post_rows": 2, "unknown_rows": 0}
    decision = CO.decide(facts)
    assert decision["paid_calls"] == 2


def test_report_and_checker_agree_standalone(tmp_path):
    root = tmp_path / "camp"
    (root / "closeout" / "fixtures").mkdir(parents=True)
    facts = CO.fixture("validation_ceiling")
    facts["campaign_id"] = "camp"
    CO.write_report(root, facts)
    CO.write_fixtures(root / "closeout" / "fixtures")
    out = CHK.verify(root)
    assert out["ok"], out["problems"]
    assert "evidence_pack_not_created" in out["gaps"]


def test_checker_detects_tampered_facts(tmp_path):
    root = tmp_path / "camp"
    (root / "closeout").mkdir(parents=True)
    facts = CO.fixture("validation_ceiling")
    facts["campaign_id"] = "camp"
    report = CO.build_report(facts)
    report["decision"]["measurement_verdict"] = S.MeasurementVerdict.CEILING
    # 篡改 facts：把一个 PASS 改成 VALID_FAIL_CAP，结论本应仍是 CEILING？
    # 不——改成 CANCELLED 后本应 ABORTED_INCOMPLETE，但仍写 CEILING。
    report["facts"]["validation"]["slots"][0]["state"] = S.TrialState.CANCELLED
    (root / "closeout" / "closeout_report.json").write_text(
        json.dumps(report, sort_keys=True), encoding="utf-8")
    out = CHK.verify(root)
    assert not out["ok"]
    assert any(p.startswith("measurement_mismatch") for p in out["problems"])


def test_checker_rejects_self_referential_manifest(tmp_path):
    root = tmp_path / "camp"
    (root / "closeout").mkdir(parents=True)
    facts = CO.fixture("manual_abort")
    facts["campaign_id"] = "camp"
    CO.write_report(root, facts)
    pack = root / "evidence_pack"
    (pack / "sub").mkdir(parents=True)
    (pack / "sub" / "a.txt").write_text("hello", encoding="utf-8")
    manifest = {"files": {
        "manifest.json": {"sha256": "x"},
        "sub/a.txt": {"sha256": CHK.sha256_file(pack / "sub" / "a.txt")},
    }}
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out = CHK.verify(root)
    assert any(p.startswith("evidence_manifest_self_reference") for p in out["problems"])


def test_checker_flags_missing_report(tmp_path):
    out = CHK.verify(tmp_path)
    assert not out["ok"] and out["problems"] == ["missing_closeout_report"]


def test_closeout_cli_runs_without_env_consent_lock_or_budget(tmp_path, monkeypatch):
    root = tmp_path / "r01_aborted_20261109T000000Z"
    root.mkdir()
    monkeypatch.delenv("SIMPRAXIS_RSI_R01_CAMPAIGN", raising=False)
    rc = CO.main(["--campaign-root", str(root)])
    assert rc == 0
    report = json.loads((root / "closeout" / "closeout_report.json").read_text())
    assert report["decision"]["process_verdict"] == S.ProcessVerdict.CLOSEOUT_INCOMPLETE
    missing = report["facts"]["closeout"]["missing"]
    assert "t2_design_gate.json" in missing
