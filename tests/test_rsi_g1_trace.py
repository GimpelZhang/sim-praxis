"""RSI-G1 证据链：跳点、缺件、裁剪重建、清单守卫（零 POST/零网络）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g1_trace as GT


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.chmod(path, 0o600)


def _source_root(tmp_path: Path, *, claim_est: float,
                 confirm_est: float = 0.040867675677873194,
                 big_result: bool = True) -> Path:
    """构造一个最小 campaign 根：journal/replies/claim/analysis 原件。"""
    root = tmp_path / "camp"
    confirm_result = {
        "ok": True, "action": "run_confirm",
        "candidate_output": {"schema": "simpraxis.rsi_r1.analysis_output.v1",
                             "schema_version": 1,
                             "run_ids": [f"run-{i:04d}" for i in range(160)],
                             "contrasts": [{"contrast_id": "friction_x_speed",
                                            "metric_id": "displacement_x_m",
                                            "estimate": confirm_est,
                                            "lower": confirm_est - 1e-5,
                                            "upper": confirm_est + 1e-5,
                                            "status": "SUPPORTED",
                                            "n_blocks": 32}],
                             "diagnostics": {
                                 "contrasts": [{
                                     "contrast_id": "friction_x_speed",
                                     "effect": confirm_est,
                                     "block_effects": [
                                         {"block_id": f"b{i:02d}",
                                          "effect": confirm_est + i * 1e-6}
                                         for i in range(32)]}],
                                 "coverage": {"blocks": [f"b{i:02d}" for i in range(32)],
                                              "run_count": 160}}},
        "analysis_receipt": {"sha256": "f" * 64},
        "evidence_index": {"analysis/output.json": "a" * 64,
                           "analysis/receipt.json": "b" * 64,
                           "confirm/blocks.json": "c" * 64},
        "primary_contrast_id": "friction_x_speed", "n_runs": 160,
        "result_sha256": "d" * 64, "run_ids_head": ["run-0000"],
    }
    if not big_result:
        # 小回包：不触发裁剪，模型可见视图里就带着 candidate_output
        confirm_result.pop("evidence_index")
        confirm_result["candidate_output"]["run_ids"] = ["run-0000"]
    action = {"action": "submit_claim",
              "claims": [{"hypothesis_id": "h_interaction",
                          "proposition": "increase_gt",
                          "metric_id": "displacement_x_m", "delta": 0.001,
                          "estimate": claim_est, "lower": claim_est - 1e-4,
                          "upper": claim_est + 1e-4}],
              "report_text": "x", "research_report": {"task_id": "t"}}
    rows = [
        {"event": "reply", "payload": {"response_id": "r1-study_A-007-aaaa",
                                       "relpath": "state/replies/submit.txt",
                                       "n_bytes": 10, "phase": "confirm"}},
        {"event": "reply", "payload": {"response_id": "r1-study_A-999",
                                       "relpath": "state/replies/other.txt",
                                       "n_bytes": 1, "phase": "explore"}},
        {"event": "action", "payload": {"response_id": "r1-study_A-007-aaaa",
                                        "name": "submit_claim",
                                        "payload": action, "study": "A"}},
        {"event": "action", "payload": {"response_id": "r1-study_A-008-bbbb",
                                        "name": "run_confirm", "payload": {},
                                        "study": "A"}},
        {"event": "action_result", "payload": {"response_id": "r1-study_A-008-bbbb",
                                               "action": "run_confirm",
                                               "result": confirm_result}},
    ]
    (root / "state").mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "state" / "journal.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8")
    (root / "state" / "replies").mkdir(mode=0o700)
    (root / "state" / "replies" / "submit.txt").write_text(
        json.dumps(action, ensure_ascii=False), encoding="utf-8")
    (root / "private" / "analysis").mkdir(parents=True, mode=0o700)
    (root / "private" / "analysis" / "A-confirm.raw.json").write_text(
        json.dumps(confirm_result["candidate_output"], sort_keys=True),
        encoding="utf-8")
    _write(root / "claim" / "submission_A.json",
           {"study_id": "A", "claims": action["claims"]})
    return root


def _trace(tmp_path: Path, root: Path, study: str = "A"):
    out = tmp_path / "g1"
    return GT.trace_study(source_root=root, study=study, diagnostic_id="g1t",
                          out_root=out, source_commit="deadbeef"), out


def test_trace_chain_and_verify(tmp_path):
    root = _source_root(tmp_path, claim_est=0.0407)
    result, out = _trace(tmp_path, root)
    assert GT.verify_trace(out / "trace" / "A")["ok"]
    kinds = result["manifest"]["kinds"]
    for kind in ("analysis_output", "dispatch_return", "clipped_view",
                 "request_serialized", "response_raw", "action_parsed",
                 "claim_persisted"):
        assert kind in kinds, kind
    vis = dict(zip(result["manifest"]["kinds"], result["manifest"]["visibilities"]))
    assert vis["analysis_output"] == "observed"
    assert vis["dispatch_return"] == "observed"
    assert vis["clipped_view"] == "reconstructed"
    assert vis["request_serialized"] == "missing"      # 历史未落盘，不补造


def test_oversized_confirm_result_clips_candidate_output(tmp_path):
    """G1 case 7：确认回包超帽 → 模型可见视图丢掉 candidate_output。"""
    root = _source_root(tmp_path, claim_est=0.0407, big_result=True)
    result, _ = _trace(tmp_path, root)
    comp = result["summary"]["comparisons"]
    assert comp["candidate_output_estimate"] == pytest.approx(0.040867675677873194)
    assert comp["model_visible_estimate"] is None
    assert comp["visible_keys"] == ["code", "message", "ok", "truncated"]
    assert result["summary"]["suspected"] == "RETURN_VIEW"
    assert result["summary"]["first_divergence"] == "UNOBSERVABLE"
    assert result["summary"]["attribution_level"] == "RECONSTRUCTED"


def test_small_result_keeps_candidate_output_and_flags_view_branch(tmp_path):
    """视图里数值正确但提交值不同 → 疑点在 MODEL_VISIBLE_VIEW（仍 UNOBSERVABLE）。"""
    root = _source_root(tmp_path, claim_est=0.0407, big_result=False)
    result, _ = _trace(tmp_path, root)
    comp = result["summary"]["comparisons"]
    assert comp["model_visible_estimate"] == pytest.approx(0.040867675677873194)
    assert result["summary"]["suspected"] == "MODEL_VISIBLE_VIEW"
    assert result["summary"]["first_divergence"] == "UNOBSERVABLE"


def test_trace_missing_analysis_is_missing_not_empty_hash(tmp_path):
    root = _source_root(tmp_path, claim_est=0.0407)
    (root / "private" / "analysis" / "A-confirm.raw.json").unlink()
    result, out = _trace(tmp_path, root)
    line = [json.loads(l) for l in
            (out / "trace" / "A" / "events.jsonl").read_text().splitlines()][0]
    assert line["visibility"] == "missing"
    assert line["bytes_sha256"] is None and line["n_bytes"] is None
    assert "analysis_output" in result["summary"]["gaps"]


def test_inventory_rejects_symlink_and_caps(tmp_path):
    root = tmp_path / "ro"
    (root).mkdir()
    real = root / "f.json"
    real.write_text("{}", encoding="utf-8")
    (root / "link.json").symlink_to(real)
    specs_root = [GT.RootSpec("r", root, max_files=10, max_total_bytes=1000)]
    files = [GT.FileSpec("r", "f.json", "role")]
    man = GT.inventory_evidence(specs_root, files, out_root=tmp_path / "o",
                                diagnostic_id="g1t")
    assert man["files"][0]["state"] == "observed"
    missing = GT.inventory_evidence(
        [GT.RootSpec("r", root)], [GT.FileSpec("r", "nope.json", "role")],
        out_root=tmp_path / "o2", diagnostic_id="g1t")
    assert missing["files"][0]["sha256"] is None
    with pytest.raises(GT.G1TraceError) as exc:
        GT.inventory_evidence(specs_root, [GT.FileSpec("r", "link.json", "role")],
                              out_root=tmp_path / "o3", diagnostic_id="g1t")
    assert exc.value.code == "ALLOWLIST_SYMLINK"
    with pytest.raises(GT.G1TraceError):
        GT.inventory_evidence([GT.RootSpec("r", root, max_total_bytes=1)],
                              files, out_root=tmp_path / "o4",
                              diagnostic_id="g1t")
    with pytest.raises(GT.G1TraceError):
        GT.inventory_evidence(specs_root, [GT.FileSpec("r", "../x", "role")],
                              out_root=tmp_path / "o5", diagnostic_id="g1t")


def test_tampered_event_is_detected(tmp_path):
    root = _source_root(tmp_path, claim_est=0.0407)
    _, out = _trace(tmp_path, root)
    path = out / "trace" / "A" / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[0])
    doc["n_bytes"] = (doc["n_bytes"] or 0) + 1
    lines[0] = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert GT.verify_trace(out / "trace" / "A")["ok"] is False
