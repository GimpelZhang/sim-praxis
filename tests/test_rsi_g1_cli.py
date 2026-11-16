"""RSI-G1 CLI：零 POST、只暴露 A 层、不碰 study/review/sender。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g1_cli as CLI


def test_cli_has_no_study_or_review_subcommands():
    parser = CLI.build_parser()
    subs = {a for a in parser._subparsers._group_actions[0].choices}  # noqa: SLF001
    assert subs == {"inventory", "trace", "packet-audit", "delivery-test",
                    "decision", "verify", "probe-preregister", "probe",
                    "capture-demo", "verify-inputs", "state"}
    for forbidden in ("study", "review", "replay"):
        assert forbidden not in subs


def test_probe_refuses_without_approval_or_paid_env(tmp_path, monkeypatch):
    """B 层双闸：无批准件/无 SIMPRAXIS_ALLOW_PAID 一律零 POST。"""
    out = tmp_path / "g1"
    (out / "probe").mkdir(parents=True)
    (out / "probe" / "prereg.json").write_text(json.dumps(
        {"diagnostic_id": "g1t", "slots": []}), encoding="utf-8")
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    rc = CLI.main(["probe", "--diagnostic-id", "g1t", "--out", str(out),
                   "--approval", str(tmp_path / "none.json"),
                   "--stage-models", "orchestrator/gateway/stage_models.yaml"])
    assert rc == 2
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    rc = CLI.main(["probe", "--diagnostic-id", "g1t", "--out", str(out),
                   "--approval", str(tmp_path / "none.json"),
                   "--stage-models", "orchestrator/gateway/stage_models.yaml"])
    assert rc == 2
    assert not (out / "quota" / "ledger.jsonl").exists()


def test_cli_refuses_other_campaign(tmp_path, capsys):
    with pytest.raises(CLI.G1CliError):
        CLI.main(["inventory", "--campaign", "rsi-r1.3",
                  "--diagnostic-id", "g1t", "--out", str(tmp_path / "o"),
                  "--sources", str(tmp_path / "x.json")])


def test_inventory_and_decision_run_offline(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.json").write_text("{}", encoding="utf-8")
    os.chmod(root / "a.json", 0o600)
    sources = tmp_path / "allow.json"
    sources.write_text(json.dumps({
        "roots": [{"root_id": "src", "path": str(root)}],
        "files": [{"root_id": "src", "relpath": "a.json", "role": "x"}]}),
        encoding="utf-8")
    out = tmp_path / "g1"
    assert CLI.main(["inventory", "--diagnostic-id", "g1t", "--out", str(out),
                     "--sources", str(sources)]) == 0
    assert (out / "manifest" / "input_manifest.json").is_file()
    assert CLI.main(["decision", "--diagnostic-id", "g1t",
                     "--out", str(out)]) == 0
    doc = json.loads((out / "decision" / "decision.json").read_text())
    assert doc["axes"]["model_comparison"] == "NOT_RUN"
    assert doc["axes"]["history_observability"] == "UNOBSERVABLE"


def test_delivery_test_cli_writes_results(tmp_path):
    from tests.g1_cases import analysis_doc
    cases = {"cases": [{
        "case_id": "smoke", "interface": "by_ref",
        "artifacts": {"analysis/output.json": analysis_doc()},
        "submission": {
            "study_id": "A", "plan_sha256": "1" * 64,
            "analysis_receipt_sha": "2" * 64,
            "claims": [{"hypothesis_id": "h", "proposition": "increase_gt",
                        "metric_id": "displacement_x_m", "delta": 0.001,
                        "result_ref": {"artifact_id": "analysis/output.json",
                                       "json_pointer": "/contrasts/0"}}],
            "research_report": {"task_id": "t"}},
        "expect": {"ok": True}}]}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    out = tmp_path / "g1"
    assert CLI.main(["delivery-test", "--diagnostic-id", "g1t",
                     "--out", str(out), "--cases", str(path)]) == 0
    doc = json.loads((out / "compiler_cases" / "results.json").read_text())
    assert doc["summary"]["passed"] == 1


def test_a_layer_modules_do_not_import_transport_or_gateway():
    import ast
    for module in ("g1_trace", "g1_delivery", "g1_packet_audit",
                   "g1_decision", "g1_cli"):
        path = Path(__file__).resolve().parents[1] / "orchestrator" / "rsi_r1" / f"{module}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: "list[str]" = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        assert not [n for n in names if "gateway_transport" in n], module
    # g1_trace 只用 research_transport 的纯函数（Turn.clipped），不构造 sender
    src = (Path(__file__).resolve().parents[1] / "orchestrator" / "rsi_r1"
           / "g1_trace.py").read_text(encoding="utf-8")
    assert "GatewayTransport" not in src and "build_gateway" not in src


def test_capture_demo_and_state_cli(tmp_path):
    out = tmp_path / "g1"
    assert CLI.main(["capture-demo", "--diagnostic-id", "g1t",
                     "--out", str(out)]) == 0
    doc = json.loads((out / "trace" / "capture_summary.json").read_text())
    assert doc["next_request"]["carries_candidate_output"] is False
    assert CLI.main(["state", "--diagnostic-id", "g1t",
                     "--out", str(out)]) == 0


def test_verify_inputs_cli_detects_tamper(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.json"
    f.write_text("{}", encoding="utf-8")
    os.chmod(f, 0o600)
    sources = tmp_path / "allow.json"
    sources.write_text(json.dumps({
        "roots": [{"root_id": "src", "path": str(src)}],
        "files": [{"root_id": "src", "relpath": "a.json", "role": "x"}]}),
        encoding="utf-8")
    out = tmp_path / "g1"
    assert CLI.main(["inventory", "--diagnostic-id", "g1t", "--out", str(out),
                     "--sources", str(sources)]) == 0
    man = str(out / "manifest" / "input_manifest.json")
    assert CLI.main(["verify-inputs", "--diagnostic-id", "g1t",
                     "--out", str(out), "--manifest", man]) == 0
    f.write_text('{"x": 1}', encoding="utf-8")
    assert CLI.main(["verify-inputs", "--diagnostic-id", "g1t",
                     "--out", str(out), "--manifest", man]) == 2
