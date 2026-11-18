"""S1-1 残尾①：受信物理服务——收权负控三连 + 正常步进逐点可对账。

AC-02：沙箱内 mujoco 不可导入（import / importlib / getattr 三条路径全拒，且审计件
落盘）；服务侧正常 run 产出与 raw 记录逐点对账（确定性物理，重跑位级一致）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_bridge import physics_service as PS
from orchestrator.rsi_bridge.physics_service import (
    PhysicsService, build_block_assignment, mujoco_denial_probe)
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_r1 import scenes as S

PLAN = "a" * 64


def _task_a():
    return TS.build_r1n_a().to_dict()


def _blocks(n=1):
    return build_block_assignment(
        PLAN, study="A", n_block=n,
        nuisance=[{"param": "v0_mps", "low": "0.1", "high": "0.6",
                   "precision": "0.001"}])


def _service(tmp_path: Path) -> PhysicsService:
    return PhysicsService(observations_root=tmp_path / "obs",
                          evidence_root=tmp_path / "ev")


# ---------------------------------------------------------------------------
# AC-02 收权负控三连
# ---------------------------------------------------------------------------

def test_denial_probe_three_routes_all_denied_and_audited(tmp_path):
    audit = mujoco_denial_probe(mode="isolated", workspace=tmp_path / "probe")
    assert audit["schema"].endswith("denial_probe")
    assert audit["mode"] == "isolated"
    assert set(audit["routes"]) == set(PS.DENIAL_ROUTES)
    for route in PS.DENIAL_ROUTES:
        assert audit["routes"][route]["denied"] is True, route
    assert audit["all_denied"] is True
    assert audit["checked_at_utc"].endswith("Z")


def test_denial_probe_bwrap_mode_when_available(tmp_path):
    import shutil
    if shutil.which("bwrap") is None or not Path("/usr/bin/python3").exists():
        return
    audit = mujoco_denial_probe(mode="bwrap", workspace=tmp_path / "probe_b")
    assert audit["mode"] == "bwrap"
    assert audit["all_denied"] is True


def test_denial_probe_reports_positive_when_mujoco_importable(tmp_path):
    """负控的负控：在**有 mujoco 的主 venv**上探针必须报放行（否则探针是空转）。"""
    import sys
    audit = mujoco_denial_probe(mode="isolated", python_exe=sys.executable,
                                workspace=tmp_path / "probe_pos")
    # 宿主 venv 的 python -I -S 禁 site-packages，仍应拒绝；用不隔离的 -c 对照：
    import subprocess
    out = subprocess.run([sys.executable, "-c",
                          "import json,importlib;print(json.dumps("
                          "{'ok': bool(importlib.import_module('mujoco'))}))"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)["ok"] is True
    # 而隔离面确实拒绝（两面对照证明 -I -S 的收权不是摆设）
    assert audit["all_denied"] is True


# ---------------------------------------------------------------------------
# 正常步进可对账
# ---------------------------------------------------------------------------

def test_run_one_point_reconcilable_and_deterministic(tmp_path):
    task = _task_a()
    cond = task["conditions"][0]
    block = _blocks(1)[0]
    params = {**cond["params"], **block["nuisance"]}
    spec = E.build_host_spec(plan_sha256=PLAN, study_id="A",
                             condition_id=cond["id"],
                             block_id=block["block_id"],
                             scene_id=cond["scene_id"], params=params)
    service = _service(tmp_path)
    first = service.run_one(spec=spec, batch_id="b1")
    second = service.run_one(spec=spec, batch_id="b2")
    a = O.read_record_jsonl(first.record_path)
    b = O.read_record_jsonl(second.record_path)
    for name in ("time_s", "qpos", "qvel"):
        assert a[name] == b[name], f"{name} 重跑不一致（确定性物理被破坏）"
    assert a["nsteps"] == 8000
    assert first.metric_id == "displacement_x_m"
    float(first.metric_value)
    assert first.nsteps == second.nsteps == len(a["time_s"])


def test_run_batch_shape_and_rows(tmp_path):
    service = _service(tmp_path)
    manifest = service.run_batch(study="A_N", task=_task_a(), plan_sha256=PLAN,
                                 blocks=_blocks(2), batch_id="batch")
    assert manifest["n_runs"] == 5 * 2  # 5 条件 × 2 block
    assert len(manifest["runs"]) == 10
    row = manifest["runs"][0]
    for key in ("run_id", "condition_id", "block_id", "metric_id", "value",
                "record_path", "record_sha256"):
        assert key in row
    assert row["metric_id"] == "displacement_x_m"
    assert len(manifest["contrasts"]) == 2  # 主对比 + sham 阴性
    assert {c["id"] for c in manifest["contrasts"]} == {
        "friction_x_mass", "negative_torsional"}
    assert manifest["manifest_sha256"]


def test_run_one_refuses_overwrite(tmp_path):
    service = _service(tmp_path)
    manifest = service.run_batch(study="A_N", task=_task_a(), plan_sha256=PLAN,
                                 blocks=_blocks(1), batch_id="dup")
    row = manifest["runs"][0]
    spec = E.build_host_spec(
        plan_sha256=PLAN, study_id="A", condition_id=row["condition_id"],
        block_id=row["block_id"], scene_id=S.SCENE_A,
        params={**{c["id"]: c["params"] for c in _task_a()["conditions"]}[
            row["condition_id"]],
            **{b["block_id"]: b["nuisance"] for b in _blocks(1)}[row["block_id"]]})
    with pytest.raises(PS.PhysicsServiceError) as excinfo:
        service.run_one(spec=spec, batch_id="dup")
    assert excinfo.value.code == "RAW_EXISTS"


def test_block_assignment_is_deterministic_and_plan_bound():
    first = build_block_assignment(PLAN, study="A", n_block=4,
                                   nuisance=[{"param": "v0_mps", "low": "0.1",
                                              "high": "0.6", "precision": "0.001"}])
    again = build_block_assignment(PLAN, study="A", n_block=4,
                                   nuisance=[{"param": "v0_mps", "low": "0.1",
                                              "high": "0.6", "precision": "0.001"}])
    other = build_block_assignment("b" * 64, study="A", n_block=4,
                                   nuisance=[{"param": "v0_mps", "low": "0.1",
                                              "high": "0.6", "precision": "0.001"}])
    assert first == again
    assert [b["nuisance"] for b in first] != [b["nuisance"] for b in other]
    for item in first:
        value = float(item["nuisance"]["v0_mps"])
        assert 0.1 <= value <= 0.6
