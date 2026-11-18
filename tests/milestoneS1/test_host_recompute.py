"""S1-1 残尾②：逐点 host 重仿真——一正一负（改单点必须命中并定位）。

AC-03：合成夹具逐点一正一负；集成路径（受信服务重跑）逐点零 Diff，篡改基线
单点后 Diff 定位到 run / block / step / 字段。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_bridge import host_recompute as HR
from orchestrator.rsi_bridge.physics_service import PhysicsService, build_block_assignment
from orchestrator.rsi_r1 import task_specs as TS

PLAN = "c" * 64


def _record(tmp_path: Path, name: str, *, x_shift: float = 0.0) -> Path:
    doc = {
        "time_s": [0.001, 0.002, 0.003],
        "qpos": [[0.0, 0.0, 0.05], [0.1 + x_shift, 0.0, 0.05], [0.2, 0.0, 0.05]],
        "qvel": [[0.4, 0.0, 0.0], [0.39, 0.0, 0.0], [0.38, 0.0, 0.0]],
    }
    path = tmp_path / name
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _row(run_id: str) -> dict:
    return {"run_id": run_id, "condition_id": "c1", "block_id": "b01",
            "metric_id": "m", "value": "0.1"}


def test_point_diffs_zero_when_identical(tmp_path):
    path = _record(tmp_path, "a.jsonl")
    baseline = json.loads(path.read_text())
    diffs = HR.point_diffs(_row("r1"), baseline, baseline, tol=0.0)
    assert diffs == []


def test_point_diffs_locate_single_tampered_point(tmp_path):
    path = _record(tmp_path, "a.jsonl")
    baseline = json.loads(path.read_text())
    replay = json.loads(path.read_text())
    replay["qpos"][1][0] += 1e-9
    diffs = HR.point_diffs(_row("r1"), baseline, replay, tol=0.0)
    assert len(diffs) == 1
    diff = diffs[0]
    assert (diff.run_id, diff.condition_id, diff.block_id) == ("r1", "c1", "b01")
    assert diff.step == 1 and diff.field == "qpos[0]"
    assert abs(diff.replay - diff.baseline) == pytest.approx(1e-9)


def test_point_diffs_flags_length_mismatch(tmp_path):
    path = _record(tmp_path, "a.jsonl")
    baseline = json.loads(path.read_text())
    replay = json.loads(path.read_text())
    replay["time_s"] = replay["time_s"][:2]
    diffs = HR.point_diffs(_row("r1"), baseline, replay, tol=0.0)
    assert [d.field for d in diffs] == ["time_s.length"]
    assert diffs[0].step == -1


def test_point_diffs_tolerance_is_strict(tmp_path):
    path = _record(tmp_path, "a.jsonl")
    baseline = json.loads(path.read_text())
    replay = json.loads(path.read_text())
    replay["qvel"][2][0] += 1e-13
    assert HR.point_diffs(_row("r1"), baseline, replay, tol=0.0)
    assert HR.point_diffs(_row("r1"), baseline, replay, tol=1e-12) == []


# ---------------------------------------------------------------------------
# 集成：受信服务重跑 → 逐点零 Diff；篡改基线必须被命中
# ---------------------------------------------------------------------------

def _confirm_plan(tmp_path: Path) -> dict:
    service = PhysicsService(observations_root=tmp_path / "obs",
                             evidence_root=tmp_path / "ev")
    blocks = build_block_assignment(
        PLAN, study="A", n_block=2,
        nuisance=[{"param": "v0_mps", "low": "0.1", "high": "0.6",
                   "precision": "0.001"}])
    task = TS.build_r1n_a().to_dict()
    # 只留两个条件以缩短耗时：主对比所需的一对
    keep = {"hi_mu_hi_mass", "lo_mu_hi_mass"}
    task["conditions"] = [c for c in task["conditions"] if c["id"] in keep]
    task["primary"] = {**task["primary"],
                       "terms": [t for t in task["primary"]["terms"]
                                 if t["condition_id"] in keep]}
    manifest = service.run_batch(study="A_N", task=task, plan_sha256=PLAN,
                                 blocks=blocks, batch_id="b")
    return {
        "frozen": True, "study": "A_N", "plan_sha256": PLAN,
        "conditions": task["conditions"], "blocks": blocks,
        "contrasts": [c for c in manifest["contrasts"]
                      if c["id"] == "friction_x_mass"],
        "runs": manifest["runs"],
    }


def test_recompute_integrated_zero_diffs_then_tamper_detected(tmp_path):
    plan = _confirm_plan(tmp_path)
    handle = HR.recompute(plan, fresh_root=tmp_path / "fresh")
    assert handle.diffs == []
    assert handle.confirm_result["n_runs"] == len(plan["runs"])
    assert handle.confirm_result["result_sha256"]
    for row in handle.runs:
        assert Path(row["replay_record_path"]).is_file()

    # 负控：篡改一条基线记录的单点 → 重算必须命中（不是重跑求解）
    victim = Path(plan["runs"][0]["record_path"])
    doc = json.loads(victim.read_text())
    doc["qpos"][10][0] += 1e-6
    victim.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    tampered = HR.recompute(plan, fresh_root=tmp_path / "fresh2")
    assert tampered.diffs, "篡改基线单点未被逐点比较命中"
    first = tampered.diffs[0]
    assert first.run_id == plan["runs"][0]["run_id"]
    assert first.step == 10 and first.field.startswith("qpos[0]")


def test_recompute_requires_frozen_plan(tmp_path):
    with pytest.raises(HR.HostRecomputeError) as excinfo:
        HR.recompute({"frozen": False}, fresh_root=tmp_path / "x")
    assert excinfo.value.code == "NOT_FROZEN"
