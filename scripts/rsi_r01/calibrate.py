#!/usr/bin/env python3
"""T2 物理/数值校准（零付费、真实容器）：R 收敛 + D 可辨识 + E 参考可达。

计划 §4.1/§4.3 与 T2 的逐项落地。三条不可越过的纪律：

1. **先登记后跑**：``calibration_prereg.json``（带内容 hash）必须在**首次校准
   运行之前**写盘；``--run`` 找不到它即拒绝（不先跑完再补登记）。
2. **收敛界是设计门，不是事后接受界**：R 的每个实例在 dt 与 dt/2 各独立跑四格，
   沉降绝对差 ≤0.02 s、效应绝对差 ≤0.01 才算收敛；不满足 ⇒ STOP_TASK_DESIGN，
   **不换参数、不换界**。
3. **物理收敛误差 ≠ 计算容差**：binary64 复算误差单独报（``numerical_error_budget``
   ），不从旧 0.004694 差值反推宽松界；两条线不得互相遮掩。

产物（``<campaign>/analysis/calibration/``）：

* ``calibration_prereg.json``      —— 预登记（参数、顺序、界、预期判决）
* ``r_convergence.json``           —— 24 次独立运行的收敛对账
* ``identifiability.json``         —— D 三故障的可区分性 + 公开域内可修复性
* ``physical_calibration.json``    —— 汇总（含每条 run 的容器结果与运行身份）
* ``numerical_error_budget.json``  —— 计算容差推导与冻结值
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r01 import evaluator as EV
from orchestrator.rsi_r01 import observations as O
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC

SCHEMA_PREREG = "simpraxis.rsi_r01.calibration_prereg.v1"
SCHEMA_RCONV = "simpraxis.rsi_r01.r_convergence.v1"
SCHEMA_IDENT = "simpraxis.rsi_r01.identifiability.v1"
SCHEMA_PHYS = "simpraxis.rsi_r01.physical_calibration.v1"
SCHEMA_ERRBUDGET = "simpraxis.rsi_r01.numerical_error_budget.v1"

#: 计划钉死的收敛界（**不是**计算容差）。
SETTLE_TOL_S = 0.02
EFFECT_TOL = 0.01
DT_CAL = S.DT_S
DT_FINE = S.DT_S / 2.0

#: D 可辨识：同一公开参考增益下，两个故障的末位置差至少要能看见。
IDENT_DISTANCE_GAP_M = 0.01


class CalibrationError(RuntimeError):
    """校准前置条件不成立（缺预登记/执行顺序被破坏）。"""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def campaign_root() -> Path:
    root = RP.rsi_r01_campaign_root()
    if not root.is_dir():
        raise CalibrationError(f"campaign 根不存在：{root}")
    return root


def out_dir(root: Path) -> Path:
    d = Path(root) / "analysis" / "calibration"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 预登记
# ---------------------------------------------------------------------------


def _expected_claim(instance_id: str) -> str:
    """按物理常识先行写死的预期：阻尼增大 ⇒ 沉降更快。

    h=+1 的实例预期 SUPPORTED；R-val-2 的公开假设是 h=−1（高阻尼更慢），
    数据若显示高阻尼更快，则对**它的公开假设**是 REFUTED。这是刻意设计的
    负向，不是「跑出来不好看再改」。
    """
    h = int(TC.R_MATRIX[instance_id]["h"])
    return S.ClaimStatus.SUPPORTED if h == 1 else S.ClaimStatus.REFUTED


def _d_fault_table() -> dict[str, Any]:
    """D 族设计（v2）预登记：机制 + 幅度 + 参考增益下的解析稳态偏移。

    v1 的瞬态型故障被闭环 P 控制抹平（T2 v1 判 DESIGN_NOT_READY），v2 只用
    稳态可测机制；本表在**首次运行之前**落进预登记，跑完不得改。
    """
    from orchestrator.rsi_r01.faults import steady_state_offset

    table: dict[str, Any] = {}
    for inst in TC.by_family("D"):
        fault = inst.private["fault"]
        kp_ref = float(inst.private["reference_gains"]["kp"])
        table[inst.instance_id] = {
            "mechanism": fault["kind"],
            "params": dict(fault),
            "reference_kp": kp_ref,
            "analytic_offset_at_reference_kp_m": round(
                steady_state_offset(fault, kp=kp_ref), 6),
            "analytic_offset_note": ("量级估计，不作判据；friction_scale 的接触"
                                     "摩擦合成会使实测小于该值"),
            "tolerance_m": float(inst.tolerance["distance"]),
        }
    return table


def prereg_doc(*, campaign_id: str, plan_sha256: str | None) -> dict[str, Any]:
    problems = TC.assert_matrix_complete() + TC.validate_fault_table()
    if problems:
        raise CalibrationError("任务表不合法，拒绝登记：" + "; ".join(problems[:5]))
    cells = {iid: TC.prereg_expected_cells(iid)
             for iid in ("R-dev", "R-val-1", "R-val-2")}
    order: list[str] = []
    for iid in ("R-dev", "R-val-1", "R-val-2"):
        for dt_name in ("dt", "dt/2"):
            for cell in cells[iid]:
                order.append(f"{iid}|{dt_name}|damping={cell[0]}|theta0={cell[1]}")
    for inst in TC.by_family("E"):
        order.append(f"{inst.instance_id}|reference")
    for inst in TC.by_family("D"):
        order.append(f"{inst.instance_id}|nominal_reference")
        order.append(f"{inst.instance_id}|faulted_reference")
        order.append(f"{inst.instance_id}|repair_grid")
    body: dict[str, Any] = {
        "schema": SCHEMA_PREREG,
        "campaign_id": campaign_id,
        "plan_sha256": plan_sha256,
        "created_utc": S.utc_now(),
        "observation_contract": {
            "dt_s": DT_CAL, "fine_dt_s": DT_FINE, "n_steps": S.N_STEPS,
            "rows": S.EXPECTED_ROWS, "t_end_s": S.T_END_S,
            "note": "trial 观测固定 2001 行；校准的 dt/2 运行行数不同，按仪器件处理",
        },
        "band": {"theta_rad": S.THETA_BAND, "omega_rad_s": S.OMEGA_BAND,
                 "strict": True},
        "delta": S.DELTA,
        "convergence_bounds": {"settle_abs_s": SETTLE_TOL_S,
                               "effect_abs": EFFECT_TOL},
        "cells": {k: [[c[0], c[1]] for c in v] for k, v in cells.items()},
        "expected_claim": {iid: _expected_claim(iid) for iid in cells},
        "d_identifiability": {"metric": "final_distance_m",
                              "min_pairwise_gap": IDENT_DISTANCE_GAP_M,
                              "repair_required": True},
        "d_fault_table_v2": _d_fault_table(),
        "d_design_revision": {
            "revision": "v2",
            "reason": ("v1 瞬态型故障被闭环 P 控制抹平，稳态不可辨识"
                       "（campaign r01 T2 判 DESIGN_NOT_READY）"),
            "r_and_e_unchanged": True,
        },
        "execution_order": order,
        "stop_rule": ("任一 R 格删失 / 收敛超界 / |z|<δ / D 不可区分或不可修复"
                      " ⇒ STOP_TASK_DESIGN（不改参数、不换界、不跑完再挑）"),
    }
    body["prereg_sha256"] = S.sha256_text(S.canonical_json(body))
    return body


def write_prereg(root: Path, *, plan_sha256: str | None = None) -> Path:
    path = out_dir(root) / "calibration_prereg.json"
    doc = prereg_doc(campaign_id=Path(root).name, plan_sha256=plan_sha256)
    if path.is_file():
        existing = S.read_json(path)
        if existing.get("prereg_sha256") != doc["prereg_sha256"]:
            raise CalibrationError(
                "已存在的 calibration_prereg.json 与当前合同不一致：预登记不可改，"
                "请人工裁决（禁止跑完再换参数）")
        return path
    S.write_canonical_json(path, doc)
    return path


def load_prereg(root: Path) -> dict[str, Any]:
    path = out_dir(root) / "calibration_prereg.json"
    if not path.is_file():
        raise CalibrationError(
            f"缺 {path}：首次校准运行之前必须先 --pre-register（先登记后跑）")
    doc = S.read_json(path)
    if doc.get("schema") != SCHEMA_PREREG:
        raise CalibrationError("calibration_prereg.json schema 不对")
    body = {k: v for k, v in doc.items() if k != "prereg_sha256"}
    if S.sha256_text(S.canonical_json(body)) != doc.get("prereg_sha256"):
        raise CalibrationError("calibration_prereg.json 内容 hash 与登记值不一致")
    return doc


# ---------------------------------------------------------------------------
# 单次运行（真实容器）
# ---------------------------------------------------------------------------


def _cal_shape_problems(doc: Mapping[str, Any], *, steps: int, dt: float,
                        kind: str) -> list[str]:
    """校准件形状：首末点齐全、严格单调、末点=steps×dt、finite。"""
    problems: list[str] = []
    for name in ("t",) + S.OBS_SERIES[kind]:
        series = doc.get(name)
        if not isinstance(series, list) or len(series) != steps + 1:
            problems.append(f"{name}: 长度 {len(series) if isinstance(series, list) else 'n/a'} ≠ {steps + 1}")
    if problems:
        return problems
    t = doc["t"]
    for i in range(1, len(t)):
        if not (t[i] > t[i - 1]):
            problems.append(f"t 非严格单调于 {i}")
            break
    if abs(t[0]) > 1e-12:
        problems.append("t[0] ≠ 0")
    if abs(t[-1] - steps * dt) > 1e-9:
        problems.append(f"t[-1]={t[-1]} ≠ steps×dt={steps * dt}")
    for name in ("t",) + S.OBS_SERIES[kind]:
        if not all(isinstance(v, (int, float)) for v in doc[name]):
            problems.append(f"{name} 含非数值")
            break
    return problems


def test_id(prefix: str, counter: list[int]) -> str:
    counter[0] += 1
    return f"{prefix}_{counter[0]:03d}"


def _record(*, instance: str, kind: str, label: str, request: O.ExperimentRequest,
            gpus: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="rsi_r01_cal_") as td:
        result, doc = O.run_unchecked(request, out_dir=Path(td), gpus=gpus)
    problems = _cal_shape_problems(doc, steps=request.steps,
                                   dt=float(request.params["timestep"]),
                                   kind=kind)
    if problems:
        raise CalibrationError(f"{instance}/{label}: 观测形状不合法 {problems[:3]}")
    return result, doc


# ---------------------------------------------------------------------------
# R：收敛与可判别
# ---------------------------------------------------------------------------


def _settle(doc: Mapping[str, Any]) -> EV.SettleResult:
    return EV.settling(doc["theta"], doc["omega"], doc["t"])


def calibrate_r(root: Path, *, gpus: bool, trials: dict[str, Any],
                counter: list[int]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    verdicts: dict[str, Any] = {}
    for iid in ("R-dev", "R-val-1", "R-val-2"):
        inst = TC.get(iid)
        per_dt: dict[str, dict[str, Any]] = {}
        for dt_name, dt, steps in (("dt", DT_CAL, S.N_STEPS),
                                   ("dt/2", DT_FINE, S.N_STEPS * 2)):
            times: dict[str, Any] = {}
            for cell in inst.r_cells:
                run_id = test_id(f"cal_{iid.replace('-', '_')}_{dt_name.replace('/', '_')}",
                                 counter)
                req = O.ExperimentRequest.pendulum(
                    trial_id=run_id, instance_id=iid, scene_xml=inst.scene_xml(),
                    damping=cell["damping"], initial_angle=cell["theta0"],
                    timestep=dt, steps=steps)
                t0 = time.monotonic()
                result, doc = _record(instance=iid, kind=S.OBS_KIND_PENDULUM,
                                      label=f"{dt_name}/{cell['cell']}",
                                      request=req, gpus=gpus)
                settle = _settle(doc)
                rows.append({"schema": SCHEMA_RCONV, "instance_id": iid,
                             "cell": cell["cell"], "damping": cell["damping"],
                             "theta0": cell["theta0"], "dt_s": dt, "steps": steps,
                             "run_id": result["run_id"], "rows": len(doc["t"]),
                             "settle": settle.as_dict(),
                             "applied": result.get("applied"),
                             "seconds": round(time.monotonic() - t0, 2)})
                times[cell["cell"]] = settle.time_s
            per_dt[dt_name] = times

        # 四格效应（两初角算术平均）与跨 dt 收敛
        per_angle: dict[str, Any] = {}
        convergence: list[dict[str, Any]] = []
        censored = [c for c, t in per_dt["dt"].items() if t is None]
        for angle, low_key, high_key in (("plus", "low/plus", "high/plus"),
                                         ("minus", "low/minus", "high/minus")):
            e_dt = EV.effect(per_dt["dt"][low_key], per_dt["dt"][high_key])
            e_fine = EV.effect(per_dt["dt/2"][low_key], per_dt["dt/2"][high_key])
            per_angle[angle] = e_dt
            delta = (abs(e_dt["value"] - e_fine["value"])
                     if e_dt["value"] is not None and e_fine["value"] is not None
                     else None)
            convergence.append({
                "angle": angle, "effect_dt": e_dt["value"],
                "effect_dt_half": e_fine["value"], "delta": delta,
                "ok": delta is not None and delta <= EFFECT_TOL,
                "settle_delta_s": (abs(per_dt["dt"][low_key] - per_dt["dt/2"][low_key])
                                   if per_dt["dt"][low_key] is not None
                                   and per_dt["dt/2"][low_key] is not None else None)})
        mean = EV.mean_effect(per_angle)
        claim, z = EV.claim_of(mean["value"], int(TC.R_MATRIX[iid]["h"]))
        settle_ok = all(
            abs(per_dt["dt"][c] - per_dt["dt/2"][c]) <= SETTLE_TOL_S
            for c in per_dt["dt"]
            if per_dt["dt"][c] is not None and per_dt["dt/2"][c] is not None)
        complete = not censored and all(t is not None for t in per_dt["dt"].values()) \
            and all(t is not None for t in per_dt["dt/2"].values())
        decisive = z is not None and abs(z) >= S.DELTA
        verdict = {
            "instance_id": iid, "censored_cells": censored,
            "settle_dt": per_dt["dt"], "settle_dt_half": per_dt["dt/2"],
            "settle_converged": settle_ok, "effect_convergence": convergence,
            "effect_converged": all(c["ok"] for c in convergence),
            "mean_effect": mean, "z": z, "claim": claim,
            "expected_claim": _expected_claim(iid),
            "claim_matches_expectation": claim == _expected_claim(iid),
            "complete": complete, "decisive": decisive,
            "ok": bool(complete and settle_ok and all(c["ok"] for c in convergence)
                       and decisive),
        }
        verdicts[iid] = verdict
    report = {"schema": SCHEMA_RCONV, "bounds": {"settle_abs_s": SETTLE_TOL_S,
                                                 "effect_abs": EFFECT_TOL},
              "runs": rows, "instances": verdicts,
              "all_ok": all(v["ok"] for v in verdicts.values())}
    S.write_canonical_json(out_dir(root) / "r_convergence.json", report)
    trials["r"] = report
    return report


# ---------------------------------------------------------------------------
# E：参考增益可达
# ---------------------------------------------------------------------------


def calibrate_e(root: Path, *, gpus: bool, trials: dict[str, Any],
                counter: list[int]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    verdicts: dict[str, Any] = {}
    for inst in TC.by_family("E"):
        gains = inst.private["reference_gains"]
        run_id = test_id(f"cal_{inst.instance_id.replace('-', '_')}", counter)
        req = O.ExperimentRequest.push(
            trial_id=run_id, instance_id=inst.instance_id,
            scene_xml=inst.faulted_scene(), kp=gains["kp"], kd=gains["kd"],
            max_force=gains["max_force"], target=tuple(inst.public["goal"]["target"]),
            timestep=S.DT_S, steps=int(inst.private["reference_steps"]))
        result, doc = _record(instance=inst.instance_id, kind=S.OBS_KIND_PUSH,
                              label="reference", request=req, gpus=gpus)
        ok = EV.lower_bound_success(distance=result["final_distance"],
                                    final_speed=result["final_speed"],
                                    tolerance=inst.tolerance)
        rows.append({"instance_id": inst.instance_id, "run_id": result["run_id"],
                     "gains": gains, "final_distance": result["final_distance"],
                     "final_speed": result["final_speed"],
                     "tolerance": inst.tolerance, "ok": ok})
        verdicts[inst.instance_id] = ok
    report = {"schema": SCHEMA_PHYS, "e_reference": rows,
              "all_ok": all(verdicts.values())}
    trials["e"] = report
    return report


# ---------------------------------------------------------------------------
# D：可辨识 + 公开域可修复
# ---------------------------------------------------------------------------


# v2 修复网格：kp 上限扩到 800——稳态偏移 ∝ 1/kp，外力偏置 20 N 需要
# kp ≥ 约 375 才能压到 0.06 m 容差以内（v1 网格止步 300，无法覆盖）。
REPAIR_GRID = [(kp, kd, fmax) for kp in (50.0, 120.0, 300.0, 500.0, 800.0)
               for kd in (5.0, 15.0, 40.0) for fmax in (40.0, 100.0)]


def _push_run(inst: TC.Instance, *, scene_xml: str, gains: Mapping[str, float],
              counter: list[int], label: str, gpus: bool,
              with_fault: bool
              ) -> tuple[dict[str, Any], dict[str, Any]]:
    """``with_fault`` 控制宿主私有注入；nominal 跑必须**不带**任何故障项。"""
    run_id = test_id(f"cal_{inst.instance_id.replace('-', '_')}_{label}", counter)
    req = O.ExperimentRequest.push(
        trial_id=run_id, instance_id=inst.instance_id, scene_xml=scene_xml,
        kp=gains["kp"], kd=gains["kd"], max_force=gains["max_force"],
        target=tuple(inst.public["goal"]["target"]), timestep=S.DT_S,
        steps=int(inst.private["reference_steps"]),
        host_injected=inst.host_injections if with_fault else {})
    return _record(instance=inst.instance_id, kind=S.OBS_KIND_PUSH,
                   label=label, request=req, gpus=gpus)


def calibrate_d(root: Path, *, gpus: bool, trials: dict[str, Any],
                counter: list[int]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    verdicts: dict[str, Any] = {}
    for inst in TC.by_family("D"):
        gains = inst.private["reference_gains"]
        nominal_result, _ = _push_run(inst, scene_xml=inst.scene_xml(), gains=gains,
                                      counter=counter, label="nominal", gpus=gpus,
                                      with_fault=False)
        faulted_result, _ = _push_run(inst, scene_xml=inst.faulted_scene(),
                                      gains=gains, counter=counter,
                                      label="faulted", gpus=gpus, with_fault=True)
        detectable = (not EV.lower_bound_success(
            distance=faulted_result["final_distance"],
            final_speed=faulted_result["final_speed"], tolerance=inst.tolerance))
        nominal_ok = EV.lower_bound_success(
            distance=nominal_result["final_distance"],
            final_speed=nominal_result["final_speed"], tolerance=inst.tolerance)
        # 公开域内修复：粗网格搜索（这就是候选可以做的公开实验）
        best: dict[str, Any] | None = None
        for kp, kd, fmax in REPAIR_GRID:
            result, _ = _push_run(inst, scene_xml=inst.faulted_scene(),
                                  gains={"kp": kp, "kd": kd, "max_force": fmax},
                                  counter=counter,
                                  label=f"repair_kp{kp:g}_kd{kd:g}_f{fmax:g}",
                                  gpus=gpus, with_fault=True)
            ok = EV.lower_bound_success(distance=result["final_distance"],
                                        final_speed=result["final_speed"],
                                        tolerance=inst.tolerance)
            item = {"gains": {"kp": kp, "kd": kd, "max_force": fmax},
                    "final_distance": result["final_distance"],
                    "final_speed": result["final_speed"], "ok": ok}
            if best is None or (ok and not best["ok"]) or (
                    ok == best["ok"] and item["final_distance"] < best["final_distance"]):
                best = item
        repairable = bool(best and best["ok"])
        rows.append({"instance_id": inst.instance_id,
                     "fault_kind": inst.private["fault"]["kind"],
                     "nominal": {"final_distance": nominal_result["final_distance"],
                                 "final_speed": nominal_result["final_speed"],
                                 "ok": nominal_ok},
                     "faulted": {"final_distance": faulted_result["final_distance"],
                                 "final_speed": faulted_result["final_speed"],
                                 "ok": not detectable},
                     "detectable": detectable, "repair_best": best,
                     "repairable": repairable})
        verdicts[inst.instance_id] = {"detectable": detectable,
                                      "nominal_ok": nominal_ok,
                                      "repairable": repairable}

    # 三故障两两可区分：同一公开参考增益下的末位置互差 ≥ 0.01 m
    pairs: list[dict[str, Any]] = []
    ids = [r["instance_id"] for r in rows]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            gap = abs(a["faulted"]["final_distance"] - b["faulted"]["final_distance"])
            pairs.append({"a": a["instance_id"], "b": b["instance_id"],
                          "gap_m": gap, "ok": gap >= IDENT_DISTANCE_GAP_M})
    distinct = all(p["ok"] for p in pairs)
    all_ok = distinct and all(v["detectable"] and v["nominal_ok"] and v["repairable"]
                              for v in verdicts.values())
    report = {"schema": SCHEMA_IDENT, "runs": rows, "pairwise": pairs,
              "distinct": distinct, "min_pairwise_gap_m": IDENT_DISTANCE_GAP_M,
              "instances": verdicts, "all_ok": all_ok}
    S.write_canonical_json(out_dir(root) / "identifiability.json", report)
    trials["d"] = report
    return report


# ---------------------------------------------------------------------------
# 计算误差预算（与物理收敛分离）
# ---------------------------------------------------------------------------


def error_budget(root: Path, *, r_report: Mapping[str, Any]) -> dict[str, Any]:
    """binary64 复算容差推导（冻结，不再随结果调整）。

    t 由 ``step_index × dt`` 生成并序列化为十进制文本，往返误差 ≤ 0.5 ulp；
    效应 ``e=(t_low−t_high)/t_low`` 的减法在相近量级下是良态的，除法再引入
    ≤1 ulp。两次独立实现（judge 与 oracle）在同一份观测上应逐位一致；留给
    序列化/解析往返的余量取 **1e-12 相对**（比 2^-52≈2.2e-16 宽三个数量级，
    但仍远小于 δ=0.10）。**物理收敛误差（不同 dt）另报，不并入此容差。**
    """
    doc = {
        "schema": SCHEMA_ERRBUDGET,
        "binary64_ulp": "2^-52 ≈ 2.22e-16",
        "time_generation": "t = step_index × dt，文本往返 ≤0.5 ulp",
        "effect_relative_tolerance": 1e-12,
        "rationale": ("同一份完整观测上，两独立实现（反向扫描 vs 后缀归约）"
                      "应逐位相同；1e-12 只吸收十进制序列化往返，不放宽物理口径"),
        "physical_convergence_bounds": {"settle_abs_s": SETTLE_TOL_S,
                                        "effect_abs": EFFECT_TOL},
        "separation": ("物理收敛界（跨 dt）**不**用于掩盖实现差异；实现差异界"
                       "（1e-12）**不**用于通过物理门"),
        "observed_r_effect_deltas": {
            iid: [c["delta"] for c in v["effect_convergence"]]
            for iid, v in r_report.get("instances", {}).items()},
    }
    S.write_canonical_json(out_dir(root) / "numerical_error_budget.json", doc)
    return doc


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run(root: Path, *, gpus: bool = False, plan_sha256: str | None = None
        ) -> dict[str, Any]:
    prereg = load_prereg(root)          # 先登记后跑（缺失即拒）
    counter = [0]
    trials: dict[str, Any] = {}
    t0 = time.monotonic()
    r_report = calibrate_r(root, gpus=gpus, trials=trials, counter=counter)
    e_report = calibrate_e(root, gpus=gpus, trials=trials, counter=counter)
    d_report = calibrate_d(root, gpus=gpus, trials=trials, counter=counter)
    budget = error_budget(root, r_report=r_report)
    summary = {
        "schema": SCHEMA_PHYS,
        "campaign_id": Path(root).name,
        "prereg_sha256": prereg["prereg_sha256"],
        "finished_utc": S.utc_now(),
        "wall_seconds": round(time.monotonic() - t0, 1),
        "container_runs": counter[0],
        "r_converged": r_report["all_ok"],
        "e_reference_ok": e_report["all_ok"],
        "d_identifiable": d_report["all_ok"],
        "error_budget": budget,
        "r": r_report, "e": e_report, "d": d_report,
        "design_ok": bool(r_report["all_ok"] and e_report["all_ok"]
                          and d_report["all_ok"]),
    }
    S.write_canonical_json(out_dir(root) / "physical_calibration.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T2 校准（零付费容器）")
    ap.add_argument("--pre-register", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--gpus", action="store_true",
                    help="给容器加 --gpus（本校准不使用渲染，默认不加）")
    ap.add_argument("--plan-sha256", default=None)
    args = ap.parse_args(argv)
    root = campaign_root()
    if args.pre_register:
        path = write_prereg(root, plan_sha256=args.plan_sha256)
        doc = S.read_json(path)
        print(json.dumps({"prereg": str(path),
                          "sha256": doc["prereg_sha256"]}, ensure_ascii=False))
    if args.run:
        summary = run(root, gpus=args.gpus)
        print(json.dumps({"design_ok": summary["design_ok"],
                          "r_converged": summary["r_converged"],
                          "e_reference_ok": summary["e_reference_ok"],
                          "d_identifiable": summary["d_identifiable"],
                          "container_runs": summary["container_runs"],
                          "wall_seconds": summary["wall_seconds"]},
                         ensure_ascii=False))
        return 0 if summary["design_ok"] else 3
    if not (args.pre_register or args.run):
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
