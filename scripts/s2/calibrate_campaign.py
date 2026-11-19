#!/usr/bin/env python3
"""RSI-S2 校准门（S2-0/S2-4；零 LLM 物理，付费前必过）。

在 S1 五门（灵敏度/sham/沉降/形状/确定性）之上加**第六门：NC 灵敏度校准**——
S2 规则要求每个研究的阴性对照"同管线注入已知效应必可检出"（计划 §2.1 第 3 行
三连判据之三）。按家族各做一次已知大效应的 pairwise 对照，走与 NC 完全相同的
管线（``_linear_contrast_values`` + block 级 percentile bootstrap）：

* A 家族：μ=0.15 vs 0.60（质量 0.5 kg、v0 0.4 m/s）——理想 Coulomb 下
  位移差 ≈0.04 m，远大于 δ=0.001；
* B 家族：kd=0.09 vs 0.11（kp=15、Tc=0.016）——阻尼差对归一化 RMSE 的
  已知效应必须可检出（> δ=0.01）。

校准不过 = 任务设计不过，**改设计不改阈值**；产物落
``$SIMPRAXIS_RSI_S2_ROOT/closeout/calibration_report.json``（0600），
付费脚本在首次网关调用前强制校验 ``all_ok``。

用法::

    python scripts/s2/calibrate_campaign.py [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402
from orchestrator.common.runtime_paths import rsi_s2_closeout_dir  # noqa: E402
from orchestrator.rsi_bridge.host_recompute import _linear_contrast_values  # noqa: E402
from orchestrator.rsi_bridge.physics_service import (  # noqa: E402
    PhysicsService, build_block_assignment)
from orchestrator.rsi_r1 import statistics as ST  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402

SCHEMA = "simpraxis.rsi_s2.calibration.v1"
PLAN_SHA = hashlib.sha256(b"s2-calibration-20261119").hexdigest()
N_BLOCK = 8  # 校准用 8 个 block（与 S1 灵敏度探针同口径；确认批另 32）


class GateFailure(RuntimeError):
    def __init__(self, gate: str, code: str, detail: str):
        self.gate = gate
        self.code = code
        self.detail = detail
        super().__init__(f"{gate}:{code}: {detail}")


def _s1_module():
    """复用 S1 五门实现（同一物理管线；不复制门函数）。"""
    path = _REPO / "scripts" / "s1" / "calibrate_studies.py"
    spec = importlib.util.spec_from_file_location("s1_calibration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nc_contrast(metric: str, delta: str, base: str, sham: str) -> dict[str, Any]:
    return {"id": f"cal_nc_{base}_vs_{sham}", "kind": "pairwise_difference",
            "metric": metric, "proposition": "equivalent", "delta": delta,
            "terms": [{"condition_id": base, "weight": "1"},
                      {"condition_id": sham, "weight": "-1"}]}


def measure_nc_sensitivity(service: PhysicsService) -> "dict[str, Any]":
    """第六门：已知大效应经 NC 管线必须可检出（按家族各一次）。"""
    out: "dict[str, Any]" = {}
    # —— A 家族：μ 0.15 vs 0.60 ——
    base = {"v0_mps": "0.4", "torsional_friction_m": "0.01", "mass_kg": "0.5"}
    a_conditions = [
        {"id": "mu_low", "scene_id": "A_contact_friction",
         "params": {**base, "sliding_friction": "0.15"}},
        {"id": "mu_high", "scene_id": "A_contact_friction",
         "params": {**base, "sliding_friction": "0.60"}},
    ]
    a_contrast = _nc_contrast("displacement_x_m_negative_equivalence", "0.001",
                              "mu_high", "mu_low")
    blocks = build_block_assignment(PLAN_SHA, study="A_N", n_block=N_BLOCK,
                                    nuisance=[{"param": "v0_mps", "low": "0.1",
                                               "high": "0.6", "precision": "0.001"}])
    man = service.run_batch(
        study="A_N", plan_sha256=PLAN_SHA, blocks=blocks, batch_id="s2-cal-a",
        task={"conditions": a_conditions, "primary": a_contrast,
              "negative_control_spec": None})
    values = _linear_contrast_values(
        [{**row, "value": row.get("value")} for row in man["runs"]], a_contrast)
    est = ST.estimate(values, level="0.90")
    detected = abs(est["effect"]) > 0.001
    out["A_N"] = {"detected": bool(detected), "estimate_m": est["effect"],
                  "ci_low": est["lower"], "ci_high": est["upper"],
                  "delta_m": 0.001, "n_blocks": est["n_blocks"],
                  "probe": "μ=0.15 vs 0.60（mass 0.5, v0 0.4）"}
    if not detected:
        raise GateFailure("nc_sensitivity", "NC_UNCALIBRATED",
                          f"A 家族已知效应不可检出：est={est['effect']:.3e}")
    # —— B 家族：kd 0.09 vs 0.11 ——
    b_base = {"controller": "PD", "dt_s": "0.001", "target_amplitude_rad": "0.2",
              "target_freq_hz": "1.0", "inertia_ratio": "1.0", "kp": "15",
              "tc_s": "0.016"}
    b_conditions = [
        {"id": "kd_low", "scene_id": "B_sampling_integration",
         "params": {**b_base, "kd": "0.09"}},
        {"id": "kd_high", "scene_id": "B_sampling_integration",
         "params": {**b_base, "kd": "0.11"}},
    ]
    b_contrast = _nc_contrast("normalized_tracking_rmse_negative_equivalence",
                              "0.01", "kd_high", "kd_low")
    blocks_b = build_block_assignment(
        PLAN_SHA, study="B_N", n_block=N_BLOCK,
        nuisance=[{"param": "target_freq_hz", "low": "0.2", "high": "2.0",
                   "precision": "0.01"}])
    man_b = service.run_batch(
        study="B_N", plan_sha256=PLAN_SHA, blocks=blocks_b, batch_id="s2-cal-b",
        task={"conditions": b_conditions, "primary": b_contrast,
              "negative_control_spec": None})
    values_b = _linear_contrast_values(
        [{**row, "value": row.get("value")} for row in man_b["runs"]], b_contrast)
    est_b = ST.estimate(values_b, level="0.90")
    detected_b = abs(est_b["effect"]) > 0.01
    out["B_N"] = {"detected": bool(detected_b), "estimate": est_b["effect"],
                  "ci_low": est_b["lower"], "ci_high": est_b["upper"],
                  "delta": 0.01, "n_blocks": est_b["n_blocks"],
                  "probe": "kd=0.09 vs 0.11（kp 15, Tc 0.016）"}
    if not detected_b:
        raise GateFailure("nc_sensitivity", "NC_UNCALIBRATED",
                          f"B 家族已知效应不可检出：est={est_b['effect']:.3e}")
    return out


def run_calibration() -> "dict[str, Any]":
    s1 = _s1_module()
    tag = "s2-main-" + utc_now_iso_z().replace(":", "").replace("-", "")
    service = s1._service(tag)  # noqa: SLF001
    gates: "list[dict[str, Any]]" = []
    measurements: "dict[str, Any]" = {}

    sens = s1.measure_sensitivity(service, "sens")
    gates.append(s1.gate_sensitivity(sens["diff_m"]))
    measurements["sensitivity"] = sens

    sham = s1.measure_sham(service)
    gates.append(s1.gate_sham(sham["lower"], sham["upper"]))
    measurements["sham"] = sham

    settle = s1.measure_settle(service)
    gates.append(s1.gate_settle(settle["settle_dt_s"], settle["settle_half_s"],
                                settle["effect_dt_m"], settle["effect_half_m"]))
    measurements["settle"] = settle

    shape = s1.shape_measurements()
    gates.append({"gate": "shape", "ok": True,
                  "max_runs": int(TS.P.MAX_CONFIRM_RUNS_PER_STUDY)})
    measurements["shape"] = shape

    det = s1.measure_determinism(service)
    gates.append({"gate": "determinism", "ok": True, "tol": 0.0})
    measurements["determinism"] = det

    nc_sens = measure_nc_sensitivity(service)
    gates.append({"gate": "nc_sensitivity", "ok": True,
                  "detected": {k: bool(v["detected"]) for k, v in nc_sens.items()}})
    measurements["nc_sensitivity"] = nc_sens

    report = {
        "schema": SCHEMA,
        "plan_sha256": PLAN_SHA,
        "calibrated_at_utc": utc_now_iso_z(),
        "gates": gates,
        "measurements": measurements,
        "nc_sensitivity": {k: {"detected": bool(v["detected"]),
                               "estimate": v.get("estimate_m", v.get("estimate")),
                               "delta": v.get("delta_m", v.get("delta"))}
                           for k, v in nc_sens.items()},
        "all_ok": all(bool(g.get("ok")) for g in gates),
        "report_sha256": "",
    }
    payload = json.dumps({k: v for k, v in report.items() if k != "report_sha256"},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    report["report_sha256"] = hashlib.sha256(payload).hexdigest()
    return report


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S2 校准门（零 LLM）")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    try:
        report = run_calibration()
    except GateFailure as exc:
        print(f"CALIBRATION FAILED {exc}", file=sys.stderr)
        return 2
    target = Path(args.out) if args.out else (
        rsi_s2_closeout_dir() / "calibration_report.json")
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, sort_keys=True,
                                         indent=2) + "\n", mode=0o600)
    print(json.dumps({"all_ok": report["all_ok"],
                      "report_sha256": report["report_sha256"],
                      "gates": [g.get("gate") for g in report["gates"]]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
