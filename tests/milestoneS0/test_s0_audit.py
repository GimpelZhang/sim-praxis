"""S0-1：审计实现可失败（负控）与两项快速检查（完整跑批由 CLI 取证，不进单测）。"""
from __future__ import annotations

from pathlib import Path

from orchestrator.rsi_bridge.audit import (EXPECTED_VERSION, check_compileall,
                                          check_imports, check_license,
                                          check_smoke, run_audit)


def test_imports_and_smoke_pass_with_pinned_library():
    checks: list[dict] = []
    assert check_imports(checks) is True
    installed = next(c for c in checks if c["id"] == "imports")
    assert installed["detail"]["installed_version"] == EXPECTED_VERSION
    assert check_smoke(checks) is True
    smoke = next(c for c in checks if c["id"] == "smoke")
    assert smoke["detail"]["compose"] == "pass"
    assert smoke["detail"]["mismatches_unit"] and smoke["detail"]["mismatches_shape"]


def test_compileall_green():
    checks: list[dict] = []
    assert check_compileall(checks) is True


def test_audit_fails_closed_on_bad_repo(tmp_path):
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    checks: list[dict] = []
    assert check_license(checks, bad) is False
    ok, checks = run_audit(bad, skip_upstream_tests=True)
    assert ok is False
    failed = {check["id"] for check in checks if not check["pass"]}
    assert {"upstream_pytest", "license"} <= failed
