#!/usr/bin/env python3
"""S2b 校准门：C_N/D_N 两机制物理校准（计划 §3 S2b-0）。

五项（S1 口径）+ NC 灵敏度校准（S2 口径）：
1. 灵敏度：机制参数可检出（DiD 效应绝对值 > 最小阈值）
2. 非退化亚阈：NC 微扰非退化（max|Δ|>0）且亚阈（|est|<δ）
3. 沉降：物理稳定（无 NaN/inf、无 runaway）
4. 形状 ≤ 192：确认批 run 总数
5. 确定性：同参数重复跑逐位一致

用法：/mnt/venvs/simpraxis/bin/python scripts/s2b/calibrate_s2b.py [--report PATH]
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import scenes as S
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_bridge.physics_service import PhysicsService


def _run_one(svc: PhysicsService, spec: E.ExperimentSpec, batch_id: str) -> dict:
    result = svc.run_one(spec=spec, batch_id=batch_id)
    return {"record_path": str(result.record_path),
            "metric_value": float(result.metric_value)}


def _make_spec(*, study_id: str, condition_id: str, block_id: str,
               scene_id: str, params: dict) -> E.ExperimentSpec:
    return E.build_host_spec(
        plan_sha256="a" * 64, study_id=study_id,
        condition_id=condition_id, block_id=block_id,
        scene_id=scene_id, params=params)


def calibrate_c() -> dict:
    """C_N 碰撞域校准。"""
    scene_id = S.SCENE_C
    tmp = Path(tempfile.mkdtemp(prefix="s2b-cal-c-"))
    svc = PhysicsService(observations_root=tmp / "obs", evidence_root=tmp / "ev")

    # 条件矩阵（2×2 + NC sham）
    conds = {
        "hi_damp_hi_vel": {"solref_damping_ratio": "0.8", "v0_mps": "4.0", "solimp_damping": "0.9"},
        "hi_damp_lo_vel": {"solref_damping_ratio": "0.8", "v0_mps": "1.5", "solimp_damping": "0.9"},
        "lo_damp_hi_vel": {"solref_damping_ratio": "0.3", "v0_mps": "4.0", "solimp_damping": "0.9"},
        "lo_damp_lo_vel": {"solref_damping_ratio": "0.3", "v0_mps": "1.5", "solimp_damping": "0.9"},
        "sham_solimp": {"solref_damping_ratio": "0.3", "v0_mps": "4.0", "solimp_damping": "0.905"},
    }

    # 跑 3 block 每条件（校准用小样本）
    results = {}
    for cid, params in conds.items():
        for bid in range(3):
            spec = _make_spec(study_id="C", condition_id=cid,
                              block_id=f"b{bid:02d}", scene_id=scene_id, params=params)
            r = _run_one(svc, spec, "cal_c")
            results.setdefault(cid, []).append(r["metric_value"])

    # 1. 灵敏度：DiD 效应
    did = ((sum(results["hi_damp_hi_vel"]) / 3 - sum(results["lo_damp_hi_vel"]) / 3)
           - (sum(results["hi_damp_lo_vel"]) / 3 - sum(results["lo_damp_lo_vel"]) / 3))
    sensitivity_ok = abs(did) > 0.001  # 最小可检出效应

    # 2. NC 非退化亚阈
    base_vals = results["lo_damp_hi_vel"]
    sham_vals = results["sham_solimp"]
    nc_diffs = [abs(s - b) for s, b in zip(sham_vals, base_vals)]
    nc_max_diff = max(nc_diffs) if nc_diffs else 0.0
    nc_est = sum(s - b for s, b in zip(sham_vals, base_vals)) / len(nc_diffs) if nc_diffs else 0
    nc_non_degenerate = nc_max_diff > 0
    nc_sub_threshold = abs(nc_est) < 0.001

    # 3. 沉降：全有限
    all_vals = [v for vals in results.values() for v in vals]
    settling_ok = all(abs(v) < 100 for v in all_vals)

    # 4. 形状：5 条件 × 32 block = 160 ≤ 192
    shape_ok = 5 * 32 <= 192

    # 5. 确定性：同参数重跑（不同 batch_id 避免 RAW_EXISTS）
    spec1 = _make_spec(study_id="C", condition_id="hi_damp_hi_vel",
                       block_id="det0", scene_id=scene_id, params=conds["hi_damp_hi_vel"])
    spec2 = _make_spec(study_id="C", condition_id="hi_damp_hi_vel",
                       block_id="det0", scene_id=scene_id, params=conds["hi_damp_hi_vel"])
    r1 = _run_one(svc, spec1, "cal_c_det1")
    r2 = _run_one(svc, spec2, "cal_c_det2")
    determinism_ok = r1["metric_value"] == r2["metric_value"]

    return {
        "study": "C_N", "scene": scene_id,
        "sensitivity": {"did": did, "ok": sensitivity_ok},
        "nc_non_degenerate": {"max_diff": nc_max_diff, "ok": nc_non_degenerate},
        "nc_sub_threshold": {"estimate": nc_est, "ok": nc_sub_threshold},
        "settling": {"ok": settling_ok},
        "shape": {"n_conditions": 5, "n_blocks": 32, "total": 160, "ok": shape_ok},
        "determinism": {"ok": determinism_ok},
        "all_pass": all([sensitivity_ok, nc_non_degenerate, nc_sub_threshold,
                         settling_ok, shape_ok, determinism_ok]),
    }


def calibrate_d() -> dict:
    """D_N 受迫振动域校准。"""
    scene_id = S.SCENE_D
    tmp = Path(tempfile.mkdtemp(prefix="s2b-cal-d-"))
    svc = PhysicsService(observations_root=tmp / "obs", evidence_root=tmp / "ev")

    conds = {
        "hi_freq_hi_damp": {"drive_freq_hz": "2.5", "joint_damping": "0.3", "drive_phase_rad": "0.0"},
        "hi_freq_lo_damp": {"drive_freq_hz": "2.5", "joint_damping": "0.05", "drive_phase_rad": "0.0"},
        "lo_freq_hi_damp": {"drive_freq_hz": "1.0", "joint_damping": "0.3", "drive_phase_rad": "0.0"},
        "lo_freq_lo_damp": {"drive_freq_hz": "1.0", "joint_damping": "0.05", "drive_phase_rad": "0.0"},
        "sham_phase": {"drive_freq_hz": "2.5", "joint_damping": "0.3", "drive_phase_rad": "0.01"},
    }

    results = {}
    for cid, params in conds.items():
        for bid in range(3):
            spec = _make_spec(study_id="D", condition_id=cid,
                              block_id=f"b{bid:02d}", scene_id=scene_id, params=params)
            r = _run_one(svc, spec, "cal_d")
            results.setdefault(cid, []).append(r["metric_value"])

    # 1. 灵敏度
    did = ((sum(results["hi_freq_hi_damp"]) / 3 - sum(results["lo_freq_hi_damp"]) / 3)
           - (sum(results["hi_freq_lo_damp"]) / 3 - sum(results["lo_freq_lo_damp"]) / 3))
    sensitivity_ok = abs(did) > 0.01

    # 2. NC
    base_vals = results["hi_freq_hi_damp"]
    sham_vals = results["sham_phase"]
    nc_diffs = [abs(s - b) for s, b in zip(sham_vals, base_vals)]
    nc_max_diff = max(nc_diffs) if nc_diffs else 0.0
    nc_est = sum(s - b for s, b in zip(sham_vals, base_vals)) / len(nc_diffs) if nc_diffs else 0
    nc_non_degenerate = nc_max_diff > 0
    nc_sub_threshold = abs(nc_est) < 0.01

    # 3. 沉降
    all_vals = [v for vals in results.values() for v in vals]
    settling_ok = all(abs(v) < 4.0 for v in all_vals)

    # 4. 形状
    shape_ok = 5 * 32 <= 192

    # 5. 确定性
    spec1 = _make_spec(study_id="D", condition_id="hi_freq_hi_damp",
                       block_id="det0", scene_id=scene_id, params=conds["hi_freq_hi_damp"])
    spec2 = _make_spec(study_id="D", condition_id="hi_freq_hi_damp",
                       block_id="det0", scene_id=scene_id, params=conds["hi_freq_hi_damp"])
    r1 = _run_one(svc, spec1, "cal_d_det1")
    r2 = _run_one(svc, spec2, "cal_d_det2")
    determinism_ok = r1["metric_value"] == r2["metric_value"]

    return {
        "study": "D_N", "scene": scene_id,
        "sensitivity": {"did": did, "ok": sensitivity_ok},
        "nc_non_degenerate": {"max_diff": nc_max_diff, "ok": nc_non_degenerate},
        "nc_sub_threshold": {"estimate": nc_est, "ok": nc_sub_threshold},
        "settling": {"ok": settling_ok},
        "shape": {"n_conditions": 5, "n_blocks": 32, "total": 160, "ok": shape_ok},
        "determinism": {"ok": determinism_ok},
        "all_pass": all([sensitivity_ok, nc_non_degenerate, nc_sub_threshold,
                         settling_ok, shape_ok, determinism_ok]),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S2b 校准门")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    print("=== S2b 校准门：C_N ===")
    cal_c = calibrate_c()
    for k, v in cal_c.items():
        if k not in ("study", "scene", "all_pass"):
            print(f"  {k}: {v}")
    print(f"  all_pass: {cal_c['all_pass']}")

    print("=== S2b 校准门：D_N ===")
    cal_d = calibrate_d()
    for k, v in cal_d.items():
        if k not in ("study", "scene", "all_pass"):
            print(f"  {k}: {v}")
    print(f"  all_pass: {cal_d['all_pass']}")

    report = {
        "schema": "simpraxis.s2b.calibration.v1",
        "C_N": cal_c, "D_N": cal_d,
        "all_pass": cal_c["all_pass"] and cal_d["all_pass"],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
        print(f"报告已写：{args.report}")

    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
