#!/usr/bin/env python3
"""Task8 Track2 步态/滑移 batch 判定（Phase_1_Plan_detailed.md L561-572）。

判定对象是 Genesis DR 对照 batch（三臂 normal / ice_nominal / ice_dr × N≥3
种子），与 Track1 单 rollout 的 rungs.py 独立。铁律：

- 只从文件读数（telemetry series、CSV、summary、标定件），不吃脚本自报的
  outcomes 做结论——outcomes 只用于「独立重算对质」（No-Fake-Physics）。
- 三级向量判的是**证据可信度**（结构/finite、重算一致、物理界）；科学结论
  （对照是否有序、DR 是否改善）放在 contrast 段，矛盾按 L571 升级人工，
  绝不设「实验组必须更好」的通过阈值。
- 零网络、零付费；Rung4 视觉意见由 task8 图接缝另行组合。
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from .gait_thresholds import (
    ARMS,
    CONTACT_FORCE_MAX_N,
    CONTACT_FORCE_MAX_N_DR,
    CONTACT_FORCE_N,
    CONTRAST_RATIO,
    DUR_S,
    ENGINE,
    ENGINE_VERSION,
    EXPECTED_FRAMES,
    FALL_TILT_DEG,
    FALL_Z_M,
    MIN_SEEDS,
    SLIP_DIST_REL_TOL,
    SLIP_V_MAX_MPS,
    TELEM_HZ,
    TELEMETRY_SCHEMA,
    TILT_MAX_ABS_TOL_DEG,
    VMAX_ABS_TOL,
)

SERIES_KEYS = ("t", "torso_z", "torso_x", "tilt_deg", "vx",
               "contact_force_R", "contact_force_L",
               "slip_v_R", "slip_v_L")
ARTIFACTS = (
    "dr_controls_vs_experimental.csv", "summary.json", "SUMMARY.md",
    "collage.png", "cover.png", "plots/contact_forces.png",
    "plots/slip_comparison.png",
    "split_normal_vs_ice_nominal_s0.mp4",
    "split_ice_nominal_vs_ice_dr_s0.mp4",
    "push_window_calibration.json",
)
DT_FRAME = 1.0 / TELEM_HZ


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------

def _finite_leaves(data: Any, prefix: str = "") -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out.extend(_finite_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.extend(_finite_leaves(v, f"{prefix}[{i}]"))
    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        out.append((prefix, float(data)))
    return out


def validate_telemetry_structure(data: Any, run_id: str) -> list[str]:
    """单 run telemetry 形状/口径校验。finite 在 Rung1 统一查。"""
    errs: list[str] = []
    if not isinstance(data, dict):
        return [f"{run_id}: telemetry 顶层必须是对象"]
    if data.get("schema") != TELEMETRY_SCHEMA:
        errs.append(f"{run_id}: schema={data.get('schema')!r}，要求 {TELEMETRY_SCHEMA}")
    if data.get("engine") != ENGINE or data.get("engine_version") != ENGINE_VERSION:
        errs.append(f"{run_id}: engine/version 必须是 {ENGINE} {ENGINE_VERSION}，"
                    f"实际 {data.get('engine')} {data.get('engine_version')}")
    sim = data.get("sim")
    if not isinstance(sim, dict):
        errs.append(f"{run_id}: 缺 sim 块")
    else:
        if sim.get("use_deterministic_algorithms") is not True:
            errs.append(f"{run_id}: sim.use_deterministic_algorithms 必须为 true"
                        "（autotuner 双数值臂根因，见 push_window_calibration v2）")
        if sim.get("gs_enable_zerocopy") is not False:
            errs.append(f"{run_id}: sim.gs_enable_zerocopy 必须为 false"
                        "（zerocopy int32 gather 缺陷）")
        if sim.get("telem_hz") != TELEM_HZ or sim.get("dur_s") != DUR_S:
            errs.append(f"{run_id}: sim 口径必须 telem_hz={TELEM_HZ}/dur_s={DUR_S}")
    series = data.get("series")
    if not isinstance(series, dict):
        errs.append(f"{run_id}: 缺 series 块")
        return errs
    n = None
    for k in SERIES_KEYS:
        v = series.get(k)
        if not isinstance(v, list) or not v:
            errs.append(f"{run_id}: series.{k} 必须是非空数值数组")
            continue
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v):
            errs.append(f"{run_id}: series.{k} 含非数值元素")
        if n is None:
            n = len(v)
        elif len(v) != n:
            errs.append(f"{run_id}: series.{k} 长度 {len(v)} != {n}")
    if n is not None and n != EXPECTED_FRAMES:
        errs.append(f"{run_id}: 帧数 {n} != {EXPECTED_FRAMES}（{DUR_S:g}s×{TELEM_HZ}Hz）")
    out = data.get("outcomes")
    if not isinstance(out, dict):
        errs.append(f"{run_id}: 缺 outcomes 块")
    params = data.get("params")
    if not isinstance(params, dict) or params.get("arm") not in ARMS:
        errs.append(f"{run_id}: params.arm 必须属于 {ARMS}")
    return errs


# ---------------------------------------------------------------------------
# 从 series 独立重算（不信 outcomes）
# ---------------------------------------------------------------------------

def recompute_run(data: dict) -> dict:
    """只消费 series + params，重算成败/滑移/极值/推力窗响应。"""
    s = data["series"]
    params = data["params"]
    t = s["t"]
    z, tilt = s["torso_z"], s["tilt_deg"]
    fall_frame = None
    for i, (zi, ai) in enumerate(zip(z, tilt)):
        if zi < FALL_Z_M or ai > FALL_TILT_DEG:
            fall_frame = i
            break
    dt = DT_FRAME
    push_t = float(params["push"]["t"])
    push_dur = float(params["push"]["dur"])
    per_foot: dict[str, dict[str, float]] = {}
    for foot in ("R", "L"):
        force = s[f"contact_force_{foot}"]
        slip = s[f"slip_v_{foot}"]
        slip_dist = sum(v * dt for f, v in zip(force, slip)
                        if f > CONTACT_FORCE_N)
        in_win = [v for tt, v in zip(t, slip)
                  if push_t <= tt < push_t + 2.5]
        per_foot[foot] = {
            "slip_dist_m": round(slip_dist, 4),
            "slip_vmax_mps": round(max(slip), 3),
            "slip_vmax_postpush_mps": round(max(in_win), 3),
            "contact_force_peak_N": round(max(force), 2),
            "contact_frames": sum(1 for f in force if f > CONTACT_FORCE_N),
        }
    return {
        "fell": fall_frame is not None,
        "fall_frame": fall_frame,
        "fall_time_s": (None if fall_frame is None
                        else round(fall_frame / TELEM_HZ, 3)),
        "torso_z_min": round(min(z), 4),
        "tilt_max_deg": round(max(tilt), 2),
        "torso_x_last": round(s["torso_x"][-1], 4),
        "vx_mean_tail": round(
            sum(s["vx"][len(s["vx"]) // 4:])
            / max(1, len(s["vx"]) - len(s["vx"]) // 4), 3),
        "push_n": float(params["push"]["n"]),
        "push_t": push_t,
        "push_dur": push_dur,
        "feet": per_foot,
    }


# ---------------------------------------------------------------------------
# Rung1：结构 + finite + 产物齐套 + 标定件
# ---------------------------------------------------------------------------

def _check_calibration(workdir: Path) -> tuple[dict, list[str]]:
    p = workdir / "push_window_calibration.json"
    info: dict[str, Any] = {"present": p.is_file()}
    errs: list[str] = []
    if not p.is_file():
        return info, ["缺 push_window_calibration.json（推力窗口标定件必须随批）"]
    cal = json.loads(p.read_text(encoding="utf-8"))
    info["schema"] = cal.get("schema")
    sel = cal.get("selected_window", {})
    info["selected_window"] = sel
    info["all_bit_exact"] = (cal.get("cross_process_replication", {})
                             .get("all_bit_exact"))
    if cal.get("schema") != "simpraxis.task8.track2.push_window_calibration.v2":
        errs.append("标定件必须是 v2（确定性求解臂口径）")
    if sel.get("push_n_N") != 35 or sel.get("push_dur_s") != 0.2:
        errs.append(f"标定选定窗口必须 35N×0.2s，实际 {sel}")
    if info["all_bit_exact"] is not True:
        errs.append("标定件跨进程逐位复验必须全部为 true")
    return info, errs


def rung1_check(workdir: Path, telemetries: dict[str, dict],
                seeds: int) -> tuple[dict, list[str]]:
    errs: list[str] = []
    missing = [a for a in ARTIFACTS if not (workdir / a).exists()]
    if missing:
        errs.append(f"缺批次产物：{missing}")
    cal_info, cal_errs = _check_calibration(workdir)
    errs.extend(cal_errs)

    bad_finite: list[str] = []
    for run_id, data in telemetries.items():
        for path, v in _finite_leaves(data.get("series", {})):
            if not math.isfinite(v):
                bad_finite.append(f"{run_id}:{path}")
        for path, v in _finite_leaves(data.get("outcomes", {})):
            if not math.isfinite(v):
                bad_finite.append(f"{run_id}:outcomes.{path}")
    if bad_finite:
        errs.append(f"series/outcomes 含非有限值：{bad_finite[:10]}")

    # 帧数/编号与三臂 × N 种子齐套
    for arm in ARMS:
        for seed in range(seeds):
            if f"{arm}_s{seed}" not in telemetries:
                errs.append(f"缺 telemetry：{arm}_s{seed}")
    checks = {
        "artifacts_complete": not missing,
        "missing_artifacts": missing,
        "calibration": cal_info,
        "all_finite": not bad_finite,
        "nonfinite_paths": bad_finite[:10],
        "n_runs": len(telemetries),
        "seeds": seeds,
    }
    return {"pass": not errs, "checks": checks, "errors": errs}, errs


# ---------------------------------------------------------------------------
# Rung2：独立重算 vs outcomes/CSV 对质
# ---------------------------------------------------------------------------

def _close(a: float, b: float, rel: float, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))


def rung2_check(telemetries: dict[str, dict], csv_rows: list[dict]) -> dict:
    diffs: list[dict] = []
    csv_by_id = {r["run_id"]: r for r in csv_rows}
    for run_id, data in telemetries.items():
        rec = recompute_run(data)
        o = data["outcomes"]
        d: dict[str, Any] = {"run_id": run_id}
        if rec["fell"] != bool(o["fell"]):
            d["fell"] = {"recomputed": rec["fell"], "declared": bool(o["fell"])}
        if rec["fall_frame"] != o.get("fall_frame"):
            d["fall_frame"] = {"recomputed": rec["fall_frame"],
                               "declared": o.get("fall_frame")}
        if rec["torso_z_min"] != o.get("torso_z_min"):
            d["torso_z_min"] = {"recomputed": rec["torso_z_min"],
                                "declared": o.get("torso_z_min")}
        # tilt 两级精度舍入桶界：series round(3)→重算再 round(2) 与 outcomes
        # 直接 round(raw,2) 可差末位一单位（TILT_MAX_ABS_TOL_DEG 溯源见阈值文件）。
        # 浮点回读余量：123.81-123.8=0.010000000000005，ε 取 1e-9°
        if abs(rec["tilt_max_deg"] - float(o.get("tilt_max_deg"))) > \
                TILT_MAX_ABS_TOL_DEG + 1e-9:
            d["tilt_max_deg"] = {"recomputed": rec["tilt_max_deg"],
                                 "declared": o.get("tilt_max_deg")}
        for foot in ("R", "L"):
            rf, of = rec["feet"][foot], o["slip_dist_m"][foot]
            if not _close(rf["slip_dist_m"], float(of), SLIP_DIST_REL_TOL, 0.005):
                d[f"slip_dist_{foot}"] = {
                    "recomputed": rf["slip_dist_m"], "declared": float(of)}
            # 峰速口径差：outcomes 从未取整序列取 max 再 round(3)，重算基于
            # round(4) series，窗口 argmax 可能换帧（VMAX_ABS_TOL 溯源见阈值文件）。
            for key_o, key_r in (("slip_v_max_mps", "slip_vmax_mps"),
                                 ("slip_v_max_postpush_mps",
                                  "slip_vmax_postpush_mps")):
                if not _close(float(rf[key_r]), float(o[key_o][foot]),
                              0.0, VMAX_ABS_TOL):
                    d[f"{key_o}_{foot}"] = {
                        "recomputed": rf[key_r],
                        "declared": o[key_o][foot]}
            if not _close(rf["contact_force_peak_N"],
                          float(o["contact_force_peak_N"][foot]), 0.002, 0.5):
                d[f"contact_force_peak_N_{foot}"] = {
                    "recomputed": rf["contact_force_peak_N"],
                    "declared": o["contact_force_peak_N"][foot]}
            if rf["contact_frames"] != o["contact_frames"][foot]:
                d[f"contact_frames_{foot}"] = {
                    "recomputed": rf["contact_frames"],
                    "declared": o["contact_frames"][foot]}
        row = csv_by_id.get(run_id)
        if row is None:
            d["csv_row"] = "缺失"
        else:
            if int(row["fell"]) != int(o["fell"]) \
                    or _csv_blank(row["fall_frame"]) != (o["fall_frame"] is None):
                d["csv_fell"] = {"csv": (row["fell"], row["fall_frame"]),
                                 "telemetry": (o["fell"], o["fall_frame"])}
            if not _close(float(row["torso_z_min"]), o["torso_z_min"], 1e-3, 0):
                d["csv_torso_z_min"] = {"csv": row["torso_z_min"],
                                        "telemetry": o["torso_z_min"]}
            for foot in ("R", "L"):
                if not _close(float(row[f"slip_vmax_{foot}_mps"]),
                              o["slip_v_max_mps"][foot], 1e-3, 0):
                    d[f"csv_slip_vmax_{foot}"] = {
                        "csv": row[f"slip_vmax_{foot}_mps"],
                        "telemetry": o["slip_v_max_mps"][foot]}
        if len(d) > 1:
            diffs.append(d)
    return {"pass": not diffs, "checks": {
        "recomputed_from_series": True,
        "fall_criterion": f"z<{FALL_Z_M}m or tilt>{FALL_TILT_DEG}°",
        "slip_integration": f"Σ|v_xy|·{DT_FRAME:g}s while contact_force>{CONTACT_FORCE_N}N",
        "slip_dist_rel_tol": SLIP_DIST_REL_TOL,
        "csv_cross_checked": True,
        "disagreements": diffs,
    }}


def _csv_blank(v: str) -> bool:
    return str(v).strip() == ""


# ---------------------------------------------------------------------------
# Rung3：物理界 + 扰动交付 + 时间轴
# ---------------------------------------------------------------------------

def rung3_check(telemetries: dict[str, dict]) -> tuple[dict, dict[str, dict]]:
    violations: list[str] = []
    recomputed: dict[str, dict] = {}
    for run_id, data in telemetries.items():
        s = data["series"]
        rec = recompute_run(data)
        recomputed[run_id] = rec
        # ice_dr 的 Kp 包络到 1e6（名义 ×90），冲击力界按 DR 包络单独推导。
        force_bound = (CONTACT_FORCE_MAX_N_DR
                       if data["params"]["arm"] == "ice_dr"
                       else CONTACT_FORCE_MAX_N)
        for foot in ("R", "L"):
            vmax = max(s[f"slip_v_{foot}"])
            fmax = max(s[f"contact_force_{foot}"])
            if min(s[f"slip_v_{foot}"]) < -1e-6:
                violations.append(f"{run_id}: slip_v_{foot} 出现负值")
            if vmax > SLIP_V_MAX_MPS:
                violations.append(f"{run_id}: slip_v_{foot}={vmax} > {SLIP_V_MAX_MPS}")
            if min(s[f"contact_force_{foot}"]) < -1e-6:
                violations.append(f"{run_id}: contact_force_{foot} 出现负值")
            if fmax > force_bound:
                violations.append(
                    f"{run_id}: force_{foot}={fmax} > {force_bound}"
                    + ("（DR Kp 包络界）" if force_bound == CONTACT_FORCE_MAX_N_DR
                       else ""))
        if max(s["tilt_deg"]) > 180.0 + 1e-6 or min(s["tilt_deg"]) < -1e-6:
            violations.append(f"{run_id}: tilt 越出 [0,180]°")
        if min(s["torso_z"]) < -0.5 or max(s["torso_z"]) > 2.5:
            violations.append(f"{run_id}: torso_z 越出物理界 [-.5,2.5]m")
        # 时间轴单调等距
        tt = s["t"]
        gaps = [round(tt[i + 1] - tt[i], 4) for i in range(len(tt) - 1)]
        if any(abs(g - DT_FRAME) > 1e-3 for g in gaps):
            violations.append(f"{run_id}: telemetry 时间轴不等距（应为 {DT_FRAME:g}s）")
        # 扰动交付：35N×.2s 且 push_t 在脚本三种子相位表内
        if rec["push_n"] != 35.0 or abs(rec["push_dur"] - 0.2) > 1e-9:
            violations.append(
                f"{run_id}: 扰动参数 {rec['push_n']}N×{rec['push_dur']}s ≠ 35N×0.2s")
    checks = {
        "slip_v_bound_mps": SLIP_V_MAX_MPS,
        "contact_force_bound_N": CONTACT_FORCE_MAX_N,
        "timeline_regular_dt_s": DT_FRAME,
        "disturbance": "35N×0.2s 躯干 +x 外力（标定 v2 选定窗口）",
        "violations": violations,
    }
    return {"pass": not violations, "checks": checks}, recomputed


# ---------------------------------------------------------------------------
# 组对照（结论段，不作为可信度通过条件；L571）
# ---------------------------------------------------------------------------

def _group_stats(arm: str, seeds: int, telemetries: dict[str, dict],
                 recomputed: dict[str, dict]) -> dict:
    ids = [f"{arm}_s{i}" for i in range(seeds)]
    rows = [recomputed[r] for r in ids]
    n = len(rows)
    n_fell = sum(1 for r in rows if r["fell"])
    postpush = [max(r["feet"]["R"]["slip_vmax_postpush_mps"],
                    r["feet"]["L"]["slip_vmax_postpush_mps"]) for r in rows]
    slip_dist = [(r["feet"]["R"]["slip_dist_m"]
                  + r["feet"]["L"]["slip_dist_m"]) / 2 for r in rows]
    return {
        "arm": arm, "n": n,
        "success_rate": round((n - n_fell) / n, 4),
        "n_fell": n_fell,
        "fall_frames": [r["fall_frame"] for r in rows],
        "mu_per_seed": [round(float(telemetries[r]["params"]["mu"]), 4)
                        for r in ids],
        "postpush_vmax_max_mps": round(max(postpush), 3),
        "postpush_vmax_mean_mps": round(sum(postpush) / n, 3),
        "slip_dist_mean_m": round(sum(slip_dist) / n, 4),
        "tilt_max_deg_max": round(max(r["tilt_max_deg"] for r in rows), 2),
    }


def contrast_block(groups: dict[str, dict]) -> dict:
    """预注册有序性（写于正式 batch 之前，阈值 gait_thresholds.CONTRAST_RATIO）。

    - control_stands：normal 组全部存活（Case A 对照组前提）；
    - ordered_slip：ice_nominal 推力后峰速组均值 ≥ CONTRAST_RATIO × normal；
    - dr_effect：ice_dr vs ice_nominal 的成功率/滑移方向（只报告方向，不判优劣）。
    任何前提不成立都不「判负调参」，输出 hypothesis_status 供 Gate2 真人裁决。
    """
    g_n, g_i, g_d = (groups[a] for a in ARMS)
    control_stands = g_n["n_fell"] == 0
    ratio = (g_i["postpush_vmax_mean_mps"]
             / max(g_n["postpush_vmax_mean_mps"], 1e-9))
    ordered_slip = ratio >= CONTRAST_RATIO
    dr_fewer_falls = g_d["n_fell"] < g_i["n_fell"]
    dr_lower_slip = (g_d["postpush_vmax_mean_mps"]
                     < g_i["postpush_vmax_mean_mps"])
    if control_stands and ordered_slip:
        status = "resolved"            # 对照可分辨（DR 方向另列，不绑定假设）
    elif not control_stands:
        status = "contradiction_control_not_robust"
    else:
        status = "not_distinguishable"
    return {
        "control_stands": control_stands,
        "ice_over_normal_postpush_ratio": round(ratio, 3),
        "preregistered_ratio": CONTRAST_RATIO,
        "ordered_slip": ordered_slip,
        "dr_vs_nominal": {
            "fewer_falls": dr_fewer_falls,
            "lower_postpush_slip": dr_lower_slip,
            "direction": "improved" if (dr_fewer_falls and dr_lower_slip)
            else "worsened" if (not dr_fewer_falls and not dr_lower_slip)
            else "mixed",
        },
        "hypothesis_status": status,
        "l571_note": "矛盾/不可分辨即结论：不调参到好看，outcome 与 mechanism "
                     "分列，Gate2 真人裁决。",
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def load_batch(workdir: str | Path) -> tuple[dict[str, dict], list[dict],
                                             dict, list[str]]:
    wd = Path(workdir)
    summary = json.loads((wd / "summary.json").read_text(encoding="utf-8"))
    seeds = int(summary["seeds"])
    telemetries: dict[str, dict] = {}
    errs: list[str] = []
    for arm in ARMS:
        for seed in range(seeds):
            p = wd / "telemetry" / f"{arm}_s{seed}.json"
            if not p.is_file():
                errs.append(f"缺 {p}")
                continue
            telemetries[f"{arm}_s{seed}"] = json.loads(
                p.read_text(encoding="utf-8"))
    rows: list[dict] = []
    with open(wd / "dr_controls_vs_experimental.csv", newline="",
              encoding="utf-8") as fh:
        rows = list(csv.DictReader(line for line in fh
                                   if not line.startswith("#")))
    return telemetries, rows, summary, errs


def evaluate_batch(workdir: str | Path) -> dict:
    """三级向量 + 组对照。rules_pass 仅代表证据可信度。"""
    wd = Path(workdir)
    schema_errors: list[str] = []
    telemetries, csv_rows, summary, load_errs = load_batch(wd)
    schema_errors.extend(load_errs)
    seeds = int(summary.get("seeds", 0)) if isinstance(summary, dict) else 0
    if seeds < MIN_SEEDS:
        schema_errors.append(f"summary.seeds={seeds} < {MIN_SEEDS}（计划 L567 N≥3）")

    for run_id, data in telemetries.items():
        schema_errors.extend(validate_telemetry_structure(data, run_id))

    if schema_errors:
        return {"ok": False, "schema_errors": schema_errors,
                "rung1": {"pass": False}, "rung2": {"pass": False},
                "rung3": {"pass": False}, "rung4": None,
                "rules_pass": False,
                "provenance": f"file:{wd}/summary.json+telemetry/*.json+csv"}

    r1, _ = rung1_check(wd, telemetries, seeds)
    r2 = rung2_check(telemetries, csv_rows)
    r3, recomputed = rung3_check(telemetries)
    groups = {a: _group_stats(a, seeds, telemetries, recomputed) for a in ARMS}
    contrast = contrast_block(groups)
    passed = r1["pass"] and r2["pass"] and r3["pass"]
    return {
        "ok": True,
        "engine": ENGINE,
        "schema_errors": [],
        "seeds": seeds,
        "rung1": r1, "rung2": r2, "rung3": r3, "rung4": None,
        "vector": {"rung1": r1["pass"], "rung2": r2["pass"],
                   "rung3": r3["pass"]},
        "rules_pass": passed,
        "groups": groups,
        "contrast": contrast,
        "disturbance": summary.get("disturbance", {}),
        "dr_envelope": summary.get("dr_envelope", {}),
        "provenance": f"file:{wd}/summary.json+telemetry/*.json+csv(series-recomputed)",
    }
