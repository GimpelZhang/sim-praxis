"""RSI-G2 离线 CLI：全链零 POST、资源/离线守卫与诚实隔离结论。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g2_cli as CLI
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g2_validation as GV
from tests.test_rsi_g2_contract import contract


def _scope_file(tmp_path: Path) -> Path:
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(contract().to_dict()), encoding="utf-8")
    return path


def _cases_file(tmp_path: Path, *, bad: bool = False) -> Path:
    claims = {}
    for case in GV.build_cases():
        claim = GV.expected_claim(case)
        if bad and case["case_id"] == "g2c01_supported_mean":
            claim["target_population"] = "registered_subgroup"
        claims[case["case_id"]] = claim
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(GV.build_case_file(claims=claims)),
                    encoding="utf-8")
    return path


def _run(argv):
    return CLI.main(argv)


def test_full_cycle_zero_post(tmp_path, capsys):
    root = tmp_path / "g2root"
    assert _run(["init", "--root", str(root), "--diagnostic-id", "g2test",
                 "--source-commit", "cdb8a4c"]) == 0
    assert _run(["contract-check", "--root", str(root),
                 "--scope-contract", str(_scope_file(tmp_path))]) == 0
    assert _run(["validate-visibility", "--root", str(root)]) == 0
    assert _run(["validate-scope", "--root", str(root),
                 "--cases", str(_cases_file(tmp_path))]) == 0
    # 同 uid ⇒ 诚实 ISOLATION_UNVERIFIED（rc=1），不宣称无泄漏
    assert _run(["validate-isolation", "--root", str(root)]) == 1
    assert _run(["closeout", "--root", str(root)]) == 0
    closeout = json.loads((root / "closeout" / "g2_closeout.json").read_text(
        encoding="utf-8"))
    assert closeout["overall"] == "G2_ENGINEERING_READY_WITH_GAPS"
    assert "ISOLATION_UNVERIFIED" in closeout["gaps"]
    assert closeout["scientific_claim"].startswith("NONE")
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["step"] == "CLOSED"


def test_scope_gate_fails_on_tampered_claim(tmp_path):
    root = tmp_path / "g2root2"
    assert _run(["init", "--root", str(root), "--diagnostic-id", "g2bad"]) == 0
    assert _run(["validate-scope", "--root", str(root),
                 "--cases", str(_cases_file(tmp_path, bad=True))]) == 1
    report = json.loads((root / "scope" / "report.json").read_text(
        encoding="utf-8"))
    assert not report["ok"]
    assert report["scope_mismatch"] == ["g2c01_supported_mean"]


def test_contract_check_rejects_expected_drift(tmp_path):
    root = tmp_path / "g2root3"
    assert _run(["init", "--root", str(root), "--diagnostic-id", "g2drift"]) == 0
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"contract_sha256": "0" * 64}),
                        encoding="utf-8")
    assert _run(["contract-check", "--root", str(root),
                 "--scope-contract", str(_scope_file(tmp_path)),
                 "--expected", str(expected)]) == 2


def test_visibility_report_marks_legacy_negative_control(tmp_path):
    root = tmp_path / "g2root4"
    assert _run(["init", "--root", str(root), "--diagnostic-id", "g2vis"]) == 0
    assert _run(["validate-visibility", "--root", str(root)]) == 0
    report = json.loads((root / "visibility" / "report.json").read_text(
        encoding="utf-8"))
    legacy = next(r for r in report["results"]
                  if r["case_id"] == "legacy_control_8k")
    assert legacy["outcome"] == "EXPECTED_LEGACY_FAILURE"
    assert legacy["primary_marker_present"] is False
    assert report["read_paths"]["ok"] is True


def test_limit_problems_are_pure_and_fail_closed():
    assert CLI.limit_problems(wall_s=1, peak_rss=1, out_bytes=1) == []
    assert any("wall" in p for p in CLI.limit_problems(
        wall_s=CLI.MAX_WALLCLOCK_S + 1, peak_rss=1, out_bytes=1))
    assert any("peak_rss" in p for p in CLI.limit_problems(
        wall_s=1, peak_rss=CLI.MAX_RSS_BYTES + 1, out_bytes=1))
    assert any("out" in p for p in CLI.limit_problems(
        wall_s=1, peak_rss=1, out_bytes=CLI.MAX_OUT_BYTES + 1))
    assert any("in" in p for p in CLI.limit_problems(
        wall_s=1, peak_rss=1, out_bytes=1, in_bytes=CLI.MAX_INPUT_BYTES + 1))


def test_state_records_out_of_order_warning(tmp_path):
    state = CLI.G2State(tmp_path, diagnostic_id="g2state")
    state.advance("VISIBILITY")
    doc = state.load()
    assert doc["step"] == "VISIBILITY"
    assert doc["warnings"] and doc["warnings"][0]["out_of_order_from"] == \
        "REGISTERED"


def test_resource_guard_does_not_fail_on_host_peak_in_pytest(tmp_path,
                                                             monkeypatch):
    """Plan §3.3：pytest 宿主峰值不归本命令；墙钟/输出仍判。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic")
    guard = CLI._ResourceGuard(tmp_path)
    monkeypatch.setattr(guard, "peak_rss",
                        lambda: CLI.MAX_RSS_BYTES * 2)
    with guard:
        pass
    assert any(p.startswith("peak_rss=") for p in guard.last_problems)
    assert CLI.limit_problems(wall_s=CLI.MAX_WALLCLOCK_S + 1, peak_rss=1,
                              out_bytes=0)


def test_fail_if_called_guards_gateway_and_physics():
    guard = G2.FailIfCalled("gateway")
    with pytest.raises(G2.G2Error) as exc:
        guard()
    assert exc.value.code == "PAID_GATE"
    with pytest.raises(G2.G2Error):
        guard.send_once(None, None)


def test_init_creates_private_layout_and_isolated_root(tmp_path):
    root = tmp_path / "g2root5"
    assert _run(["init", "--root", str(root), "--diagnostic-id", "g2layout"]) == 0
    for sub in ("manifest", "trace", "contract", "public", "private_expected",
                "raw", "judge", "visibility", "scope", "isolation",
                "reports", "closeout", "logs"):
        assert (root / sub).is_dir(), sub
    freeze = json.loads((root / "manifest" / "g2_freeze.json").read_text(
        encoding="utf-8"))
    assert freeze["policy_ids"] == ["legacy", "g2_atomic_v1"]
    assert freeze["source_commit"] == ""
