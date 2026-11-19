"""S2 双审合并与无代写重放（2026-11-19 复评 CRITICAL/HIGH 回归）。

- `merge_channels` 必须接受**正常两通道**（同盲包内容、不同温度/盐），
  并在参数相同时拒绝（同源偏差）；
- `_transcript_writes` 必须能重放 Write/Edit/MultiEdit 与 Bash `cp`，
  且对不可核文件 fail-closed。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator.rsi_bridge import review_s2 as RV2

_REPO = Path(__file__).resolve().parents[2]


def _pack(channel: str):
    return RV2.build_pack_v2(
        instance_id="s2-x", arm="v1", channel=channel,
        question="摩擦是否影响位移？", methods="2×2 DiD 32 块",
        results=[{"id": "c", "estimate": 1e-6, "ci_low": -1e-6,
                  "ci_high": 2e-6, "n_blocks": 32, "claim": "EQUIVALENT",
                  "proposition": "decrease_gt", "delta": "0.001"}],
        claim={"verdict": "EQUIVALENT", "statement": "s"},
        report={name: f"{name} 段内容" for name in
                ("question_hypothesis", "methods", "main_results",
                 "heterogeneity_scope", "limitations_next_steps",
                 "negative_control_handling")},
        negative_control="NC 三连通过")


def _call_ok(prompt):
    return json.dumps({"dimension_scores": {d: 3 for d in
                                            ("problem_hypothesis", "design_confounding",
                                             "statistics_censoring", "claim_strength",
                                             "limitations_next_steps", "negative_control")},
                       "verdict": "accept", "reason": "ok"}), (10, 5)


def test_merge_accepts_normal_two_channels():
    a = RV2.run_channel(_call_ok, pack=_pack("channel_a"), channel="channel_a")
    b = RV2.run_channel(_call_ok, pack=_pack("channel_b"), channel="channel_b")
    merged = RV2.merge_channels(instance_id="s2-x", arm="v1",
                                channel_a=a, channel_b=b)
    assert merged["verdict"] == "PASS"
    assert merged["channel_a"] == "accept" and merged["channel_b"] == "accept"


def test_merge_rejects_same_parameters(monkeypatch):
    a = RV2.run_channel(_call_ok, pack=_pack("channel_a"), channel="channel_a")
    b = RV2.run_channel(_call_ok, pack=_pack("channel_a"), channel="channel_a")
    with pytest.raises(RV2.ReviewS2Error) as excinfo:
        RV2.merge_channels(instance_id="s2-x", arm="v1",
                           channel_a=a, channel_b=b)
    assert excinfo.value.code == "CHANNELS_NOT_DISTINCT"


def _proposer_module():
    path = _REPO / "scripts" / "s2" / "propose_mutation.py"
    spec = importlib.util.spec_from_file_location("s2_proposer_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(name, payload):
    return json.dumps({"message": {"content": [
        {"type": "tool_use", "name": name, "input": payload}]}})


def test_transcript_replay_write_edit_cp(tmp_path):
    module = _proposer_module()
    ws = tmp_path
    (ws / "context").mkdir()
    (ws / "context" / "guards.py").write_text("AAA\nBB\nCCC\n", encoding="utf-8")
    transcript = ws / "transcript.jsonl"
    transcript.write_text("\n".join([
        _event("Bash", {"command": "cp context/guards.py candidate/guards.py && ls -l"}),
        _event("Edit", {"file_path": str(ws / "candidate" / "guards.py"),
                        "old_string": "BB", "new_string": "BBB"}),
        _event("Write", {"file_path": str(ws / "candidate" / "nc_toolkit.py"),
                         "content": "toolkit\n"}),
    ]) + "\n", encoding="utf-8")
    writes = module._transcript_writes(transcript, context_dir=ws / "context")
    assert writes["guards.py"] == "AAA\nBBB\nCCC\n"
    assert writes["nc_toolkit.py"] == "toolkit\n"


def test_transcript_replay_fails_closed_on_unknown_write(tmp_path):
    module = _proposer_module()
    ws = tmp_path
    (ws / "candidate").mkdir()
    (ws / "candidate" / "guards.py").write_text("AAA\n", encoding="utf-8")
    (ws / "transcript.jsonl").write_text("", encoding="utf-8")
    package = {"files": [{"path": "orchestrator/hk/guards.py", "file": "guards.py"}]}
    with pytest.raises(module.ProposerError) as excinfo:
        module._assert_package_matches_workspace(package, ws)
    assert excinfo.value.code == "PROPOSER_NO_TRANSCRIPT_WRITE"
