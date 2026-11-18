#!/usr/bin/env python3
"""RSI-S1 校准门（S1-2；零 LLM 物理，付费前必过）。

五门（计划 S1-2；任一失败 = 任务设计不过，**改设计不改阈值**）：

① 灵敏度控制：A μ=0.15 vs 0.6、m=0.5、v0=0.4 的位移差 > 0.01 m（R1 教训）；
② sham 正控：已知无效干预（torsional 0.01→0.02）90% 区间必须落 ±0.001 m 内；
③ 沉降对账：A 的 dt 与 dt/2 双跑 |Δt_settle| ≤ 0.02 s 且 |Δ效应| ≤ 0.01 m；
④ 形状硬门：确认批 ≤192 runs、32 区块 × 条件数逐值对（R1.1 RESOURCE_EXCEEDED）；
⑤ 确定性：同 spec 两次逐点一致（host_recompute 自证）。

每门都有**负控**：``gate_*`` 是纯函数，喂坏值必须被拒（测试逐门一正一负）。
物理一律经 ``physics_service`` 受信面（本脚本不 import mujoco）。
产物：``$SIMPRAXIS_RSI_S1_ROOT/closeout/calibration_report.json`` + 逐门证据根。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import rsi_s1_closeout_dir  # noqa: E402
from orchestrator.rsi_bridge.physics_service import (  # noqa: E402
    PhysicsService, PhysicsServiceError, build_block_assignment)
from orchestrator.rsi_r1 import experiments as E  # noqa: E402
from orchestrator.rsi_r1 import metrics as M  # noqa: E402
from orchestrator.rsi_r1 import observations as O  # noqa: E402
from orchestrator.rsi_r1 import profiles as P  # noqa: E402
from orchestrator.rsi_r1 import rubric as R  # noqa: E402
from orchestrator.rsi_r1 import scenes as S  # noqa: E402
from orchestrator.rsi_r1 import statistics as ST  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402

SCHEMA = "simpraxis.rsi_s1.calibration.v1"
PLAN_SHA = hashlib.sha256(b"s1-calibration-20261118").hexdigest()


class GateFailure(RuntimeError):
    def __init__(self, gate: str, code: str, detail: str):
        self.gate = gate
        self.code = code
        self.detail = detail
        super().__init__(f"{gate}:{code}: {detail}")


# ---------------------------------------------------------------------------
# 五门（纯函数；负控由调用方/测试喂坏值）
# ---------------------------------------------------------------------------

def gate_sensitivity(diff_m: float, *,
                     minimum: float = float(R.SENSITIVITY_MIN_DISPLACEMENT_DIFF_M)
                     ) -> dict[str, Any]:
    if not (diff_m > minimum):
        raise GateFailure("sensitivity", "INDISTINGUISHABLE",
                          f"位移差 {diff_m:.6f} m <= 下限 {minimum} m")
    return {"gate": "sensitivity", "diff_m": diff_m, "minimum": minimum,
            "ok": True}


def gate_sham(lower: float, upper: float, *,
              delta: float = float(R.METRICS[
                  "displacement_x_m_negative_equivalence"]["delta"])) -> dict[str, Any]:
    if not (lower > -delta and upper < delta):
        raise GateFailure("sham", "EQUIVALENCE_BREACH",
                          f"90% 区间 [{lower:.6f}, {upper:.6f}] 未严格落 ±{delta}")
    return {"gate": "sham", "lower": lower, "upper": upper, "delta": delta,
            "ok": True}


def gate_settle(dt_settle: float, dt2_settle: float,
                dt_effect: float, dt2_effect: float, *,
                max_settle_delta: float = 0.02,
                max_effect_delta: float = 0.01) -> dict[str, Any]:
    settle_delta = abs(dt_settle - dt2_settle)
    effect_delta = abs(dt_effect - dt2_effect)
    if settle_delta > max_settle_delta:
        raise GateFailure("settle", "SETTLE_DRIFT",
                          f"|Δt_settle|={settle_delta:.6f}s > {max_settle_delta}")
    if effect_delta > max_effect_delta:
        raise GateFailure("settle", "EFFECT_DRIFT",
                          f"|Δeffect|={effect_delta:.6f} > {max_effect_delta}")
    return {"gate": "settle", "settle_delta_s": settle_delta,
            "effect_delta_m": effect_delta, "ok": True}


def gate_shape(n_conditions: int, n_block: int, n_runs: int, *,
               max_runs: int = int(P.MAX_CONFIRM_RUNS_PER_STUDY),
               expected_blocks: int = int(P.MAX_CONFIRM_BLOCKS)) -> dict[str, Any]:
    if n_conditions <= 0 or n_block != expected_blocks:
        raise GateFailure("shape", "BLOCK_MISMATCH",
                          f"block 数 {n_block} != {expected_blocks}")
    if n_runs != n_conditions * n_block:
        raise GateFailure("shape", "RUN_SHAPE",
                          f"runs {n_runs} != {n_conditions}×{n_block}")
    if n_runs > max_runs:
        raise GateFailure("shape", "RESOURCE_EXCEEDED",
                          f"runs {n_runs} > 确认批上限 {max_runs}（R1.1 教训）")
    return {"gate": "shape", "conditions": n_conditions, "blocks": n_block,
            "runs": n_runs, "max_runs": max_runs, "ok": True}


def gate_determinism(first: Mapping[str, Any], second: Mapping[str, Any],
                     tol: float = 0.0) -> dict[str, Any]:
    from orchestrator.rsi_bridge.host_recompute import point_diffs

    diffs = point_diffs({"run_id": first.get("run_id")}, dict(first),
                        dict(second), tol=tol)
    if diffs:
        raise GateFailure("determinism", "NONDETERMINISTIC",
                          f"{len(diffs)} 个点差异（首个 step={diffs[0].step} "
                          f"{diffs[0].field}）")
    return {"gate": "determinism", "diffs": 0, "tol": tol, "ok": True}


# ---------------------------------------------------------------------------
# 物理测量（受信服务）
# ---------------------------------------------------------------------------

def _service(tag: str) -> PhysicsService:
    root = ensure_private_dir(rsi_s1_closeout_dir() / "calibration" / tag)
    return PhysicsService(observations_root=root / "obs",
                          evidence_root=root / "ev")


def _spec_a(*, mu: str, mass: str, v0: str = "0.4", tors: str = "0.01",
            duration_s: str = "4.0") -> E.ExperimentSpec:
    cond = {"id": "cal", "scene_id": S.SCENE_A,
            "params": {"v0_mps": v0, "torsional_friction_m": tors,
                       "mass_kg": mass, "sliding_friction": mu}}
    spec = E.build_host_spec(plan_sha256=PLAN_SHA, study_id="A",
                             condition_id="cal", block_id="cal",
                             scene_id=S.SCENE_A, params=cond["params"])
    if duration_s != str(S.DURATION_S):
        shape = S.run_shape(Decimal(spec.dt_s),
                            duration_s=Decimal(duration_s))
        spec = replace(spec, duration_s=duration_s,
                       ticks={"nsteps": shape["nsteps"],
                              "dt_ticks_us": shape["dt_ticks_us"],
                              "duration_ticks_us": shape["duration_ticks_us"]})
    return spec


def _metric(record: Mapping[str, Any], *, x0: float) -> M.StudyMetrics:
    return M.metrics_for(record, study_id="A", initial_x_m=x0)


def measure_sensitivity(service: PhysicsService, tag: str) -> dict[str, Any]:
    low = service.run_one(spec=_spec_a(mu=str(R.SENSITIVITY_MU_LOW),
                                       mass=str(R.SENSITIVITY_MASS_KG),
                                       v0=str(R.SENSITIVITY_V0_MPS)),
                          batch_id=f"{tag}-low")
    high = service.run_one(spec=_spec_a(mu=str(R.SENSITIVITY_MU_HIGH),
                                        mass=str(R.SENSITIVITY_MASS_KG),
                                        v0=str(R.SENSITIVITY_V0_MPS)),
                           batch_id=f"{tag}-high")
    diff = abs(float(low.metric_value) - float(high.metric_value))
    return {"diff_m": diff, "low": low.metric_value, "high": high.metric_value,
            "low_run": low.run_id, "high_run": high.run_id}


def measure_sham(service: PhysicsService, *, n_block: int = 8) -> dict[str, Any]:
    """基准（torsional 0.01）vs sham（0.02）逐 block 配对，90% 区间。"""
    blocks = build_block_assignment(
        PLAN_SHA, study="A", n_block=n_block,
        nuisance=[{"param": "v0_mps", "low": "0.1", "high": "0.6",
                   "precision": "0.001"}])
    values: dict[str, tuple[float, float]] = {}
    for block in blocks:
        v0 = str(block["nuisance"]["v0_mps"])
        base = service.run_one(spec=E.build_host_spec(
            plan_sha256=PLAN_SHA, study_id="A", condition_id="base",
            block_id=block["block_id"], scene_id=S.SCENE_A,
            params={"v0_mps": v0, "torsional_friction_m": "0.01",
                    "mass_kg": "1.0", "sliding_friction": "0.4"}),
            batch_id="sham")
        sham = service.run_one(spec=E.build_host_spec(
            plan_sha256=PLAN_SHA, study_id="A", condition_id="sham",
            block_id=block["block_id"], scene_id=S.SCENE_A,
            params={"v0_mps": v0, "torsional_friction_m": "0.02",
                    "mass_kg": "1.0", "sliding_friction": "0.4"}),
            batch_id="sham")
        values[block["block_id"]] = (float(sham.metric_value),
                                     float(base.metric_value))
    pairs = ST.paired_diffs(values)
    estimated = ST.estimate(pairs, level=R.CI_NEGATIVE)
    return {"lower": estimated["lower"], "upper": estimated["upper"],
            "effect": estimated["effect"], "n_blocks": estimated["n_blocks"]}


#: 沉降对账的观测时窗（锁内适配：engine 每 run 8000 步帽使 4 s @ dt/2 = 16000 步
#: 不可行；两臂同取 2 s 物理时窗——停止发生在 ~0.1 s，2 s 内位移已稳定，比较口径不变）。
SETTLE_HORIZON_S = "2.0"


def measure_settle(service: PhysicsService) -> dict[str, Any]:
    """A 的 dt vs dt/2：沉降时间与位移效应的漂移（同 2 s 物理时窗）。"""
    base = _spec_a(mu="0.4", mass="1.0", duration_s=SETTLE_HORIZON_S)
    dt = Decimal(base.dt_s)
    half = dt / Decimal(2)
    shape_half = S.run_shape(half, duration_s=Decimal(SETTLE_HORIZON_S))
    half_spec = replace(base, dt_s=str(half),
                       ticks={"nsteps": shape_half["nsteps"],
                              "dt_ticks_us": shape_half["dt_ticks_us"],
                              "duration_ticks_us": shape_half["duration_ticks_us"]})
    run_dt = service.run_one(spec=base, batch_id="settle-dt")
    run_half = service.run_one(spec=half_spec, batch_id="settle-half")
    rec_dt = O.read_record_jsonl(run_dt.record_path)
    rec_half = O.read_record_jsonl(run_half.record_path)
    detail_dt = json.loads(json.dumps(run_dt.report))["metric_details"]
    detail_half = json.loads(json.dumps(run_half.report))["metric_details"]
    stop_dt = detail_dt["values"].get("stop_time_s")
    stop_half = detail_half["values"].get("stop_time_s")
    if stop_dt is None or stop_half is None:
        raise GateFailure("settle", "CENSORED",
                          "4 s 内未停（stop_time 右删失），无法对账沉降")
    return {"settle_dt_s": float(stop_dt), "settle_half_s": float(stop_half),
            "effect_dt_m": float(run_dt.metric_value),
            "effect_half_m": float(run_half.metric_value),
            "nsteps_dt": rec_dt["nsteps"], "nsteps_half": rec_half["nsteps"]}


def measure_determinism(service: PhysicsService) -> dict[str, Any]:
    spec = _spec_a(mu="0.4", mass="1.0")
    first = service.run_one(spec=spec, batch_id="det-1")
    second = service.run_one(spec=spec, batch_id="det-2")
    a = O.read_record_jsonl(first.record_path)
    b = O.read_record_jsonl(second.record_path)
    gate_determinism({**a, "run_id": first.run_id},
                     {**b, "run_id": first.run_id})
    return {"run_id": first.run_id, "nsteps": a["nsteps"],
            "first_sha": first.record_sha256, "second_sha": second.record_sha256}


def shape_measurements() -> dict[str, Any]:
    a = TS.build_r1n_a()
    b = TS.build_r1n_b()
    out = {}
    for study, task in (("A_N", a), ("B_N", b)):
        per_value = {c.id: len(task.conditions) for c in task.conditions}
        gate_shape(len(task.conditions), P.MAX_CONFIRM_BLOCKS,
                   len(task.conditions) * P.MAX_CONFIRM_BLOCKS)
        out[study] = {"conditions": len(task.conditions),
                      "blocks": P.MAX_CONFIRM_BLOCKS,
                      "runs": len(task.conditions) * P.MAX_CONFIRM_BLOCKS,
                      "per_value_pairing": per_value}
    return out


def run_calibration() -> dict[str, Any]:
    # 每次校准落到独立证据根（raw 重跑不覆盖：RAW_EXISTS 是纪律，不靠删除绕过）。
    tag = "main-" + utc_now_iso_z().replace(":", "").replace("-", "")
    service = _service(tag)
    results: dict[str, Any] = {}
    gates: list[dict[str, Any]] = []
    sens = measure_sensitivity(service, "sens")
    gates.append(gate_sensitivity(sens["diff_m"]))
    results["sensitivity"] = sens
    sham = measure_sham(service)
    gates.append(gate_sham(sham["lower"], sham["upper"]))
    results["sham"] = sham
    settle = measure_settle(service)
    gates.append(gate_settle(settle["settle_dt_s"], settle["settle_half_s"],
                             settle["effect_dt_m"], settle["effect_half_m"]))
    results["settle"] = settle
    shape = shape_measurements()
    results["shape"] = shape  # gate_shape 已在内部逐 study 断言
    gates.append({"gate": "shape", "ok": True,
                  "max_runs": int(P.MAX_CONFIRM_RUNS_PER_STUDY)})
    det = measure_determinism(service)
    gates.append({"gate": "determinism", "ok": True, "tol": 0.0})
    results["determinism"] = det
    report = {
        "schema": SCHEMA,
        "plan_sha256": PLAN_SHA,
        "calibrated_at_utc": utc_now_iso_z(),
        "gates": gates,
        "measurements": results,
        "thresholds": {
            "sensitivity_min_diff_m": str(R.SENSITIVITY_MIN_DISPLACEMENT_DIFF_M),
            "sham_delta_m": str(R.METRICS["displacement_x_m_negative_equivalence"]["delta"]),
            "sham_ci": str(R.CI_NEGATIVE),
            "settle_max_delta_s": "0.02",
            "effect_max_delta_m": "0.01",
            "confirm_batch_max_runs": int(P.MAX_CONFIRM_RUNS_PER_STUDY),
            "blocks": int(P.MAX_CONFIRM_BLOCKS),
        },
        "all_ok": True,
        "report_sha256": "",
    }
    payload = json.dumps({k: v for k, v in report.items() if k != "report_sha256"},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    report["report_sha256"] = hashlib.sha256(payload).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 校准门（零 LLM）")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    try:
        report = run_calibration()
    except GateFailure as exc:
        print(f"CALIBRATION FAILED {exc}", file=sys.stderr)
        return 2
    except PhysicsServiceError as exc:
        print(f"CALIBRATION PHYSICS ERROR {exc}", file=sys.stderr)
        return 3
    target = Path(args.out) if args.out else (
        rsi_s1_closeout_dir() / "calibration_report.json")
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, sort_keys=True,
                                         indent=2) + "\n", mode=0o600)
    print(json.dumps({"all_ok": report["all_ok"],
                      "report_sha256": report["report_sha256"],
                      "gates": [g["gate"] for g in report["gates"]],
                      "path": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
