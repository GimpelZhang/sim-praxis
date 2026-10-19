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
from typing import Any, Mapping

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
    POSTPUSH_WINDOW_S,
    PUSH_DUR_S,
    PUSH_FORCE_N,
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

# rung2 CSV 对质实际消费的列（L-3：缺列→schema 错误；与生产脚本
# genesis_humanoid_dr.py CSV_FIELDS 的子集对应）。
CSV_REQUIRED_FIELDS = (
    "run_id", "fell", "fall_frame", "torso_z_min",
    "slip_vmax_R_mps", "slip_vmax_L_mps",
)

# Task6 ascent 可选加严 profile 的七键（消费侧键集钉死，缺/多键 fail-closed）。
# 只覆盖重算一致性公差与接触/滑移数值界；FALL_Z_M/FALL_TILT_DEG/
# PUSH_FORCE_N/PUSH_DUR_S/CONTRAST_RATIO 等物理与预注册锚点不进 profile，
# 任何调用方都改不了（contrast_block 与跌倒判定恒走 gait_thresholds 常量）。
# None = 默认常量（Task8 行为逐字节不变）；加严值唯一来源
# orchestrator.judge.ascent_thresholds.tight_profile()。
PROFILE_KEYS = (
    "slip_dist_rel_tol", "vmax_abs_tol", "tilt_max_abs_tol_deg",
    "contact_force_n", "slip_v_max_mps",
    "contact_force_max_n", "contact_force_max_n_dr",
)


def resolve_threshold_profile(
        profile: "Mapping[str, float] | None") -> dict[str, float]:
    """归一化加严 profile：None→gait_thresholds 默认常量（Task8 口径）。

    映射必须恰含 PROFILE_KEYS 七键、值为正有限实数（bool 不认），否则
    ValueError——加严口径是科学判定的一部分，禁止半张表静默走默认。
    """
    if profile is None:
        return {
            "slip_dist_rel_tol": SLIP_DIST_REL_TOL,
            "vmax_abs_tol": VMAX_ABS_TOL,
            "tilt_max_abs_tol_deg": TILT_MAX_ABS_TOL_DEG,
            "contact_force_n": CONTACT_FORCE_N,
            "slip_v_max_mps": SLIP_V_MAX_MPS,
            "contact_force_max_n": CONTACT_FORCE_MAX_N,
            "contact_force_max_n_dr": CONTACT_FORCE_MAX_N_DR,
        }
    if not isinstance(profile, Mapping):
        raise ValueError(
            f"threshold profile 必须是映射或 None，收到 {type(profile).__name__}")
    if set(profile) != set(PROFILE_KEYS):
        missing = sorted(set(PROFILE_KEYS) - set(profile))
        extra = sorted(set(profile) - set(PROFILE_KEYS))
        raise ValueError(
            f"threshold profile 键集必须恰为 {list(PROFILE_KEYS)}："
            f"缺 {missing}，多余 {extra}")
    out: dict[str, float] = {}
    for key in PROFILE_KEYS:
        v = profile[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)) or float(v) <= 0:
            raise ValueError(
                f"threshold profile[{key!r}] 必须是正有限实数，收到 {v!r}")
        out[key] = float(v)
    return out


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


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# rung2 对质实际消费的 outcomes 形状（L-3：缺键/坏形状在此转 schema 错误，
# 不允许裸 KeyError 逃出 evaluate_batch；生产脚本 genesis_humanoid_dr.py
# 的 outcomes 块是该形状的唯一生产来源）。
_OUTCOME_FEET_NUMERIC = ("slip_dist_m", "slip_v_max_mps",
                         "slip_v_max_postpush_mps", "contact_force_peak_N")


def _validate_outcomes_shape(out: dict, run_id: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(out.get("fell"), bool):
        errs.append(f"{run_id}: outcomes.fell 必须是布尔")
    ff = out.get("fall_frame")
    if ff is not None and (not isinstance(ff, int) or isinstance(ff, bool)
                           or ff < 0):
        errs.append(f"{run_id}: outcomes.fall_frame 必须是 null 或非负整数")
    for k in ("torso_z_min", "tilt_max_deg"):
        if not _is_num(out.get(k)):
            errs.append(f"{run_id}: outcomes.{k} 必须是数值")
    for k in _OUTCOME_FEET_NUMERIC:
        d = out.get(k)
        if not isinstance(d, dict):
            errs.append(f"{run_id}: outcomes.{k} 必须是 {{R,L}} 对象")
            continue
        for foot in ("R", "L"):
            if not _is_num(d.get(foot)):
                errs.append(f"{run_id}: outcomes.{k}.{foot} 必须是数值")
    cf = out.get("contact_frames")
    if not isinstance(cf, dict):
        errs.append(f"{run_id}: outcomes.contact_frames 必须是 {R,L} 对象")
    else:
        for foot in ("R", "L"):
            v = cf.get(foot)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errs.append(
                    f"{run_id}: outcomes.contact_frames.{foot} 必须是非负整数")
    return errs


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
    else:
        errs.extend(_validate_outcomes_shape(out, run_id))
    params = data.get("params")
    if not isinstance(params, dict) or params.get("arm") not in ARMS:
        errs.append(f"{run_id}: params.arm 必须属于 {ARMS}")
    return errs


# ---------------------------------------------------------------------------
# 从 series 独立重算（不信 outcomes）
# ---------------------------------------------------------------------------

def recompute_run(data: dict, *,
                  profile: "Mapping[str, float] | None" = None) -> dict:
    """只消费 series + params，重算成败/滑移/极值/推力窗响应。

    profile 仅替换滑移积分的接触力门（contact_force_n）；跌倒判定恒用锚点
    FALL_Z_M/FALL_TILT_DEG，推力窗恒读 params（锚点不进 profile）。
    """
    prof = resolve_threshold_profile(profile)
    contact_force_n = prof["contact_force_n"]
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
        # 滑移距离 = Σ|v_xy|·dt（接触帧）：生产脚本的 vxy 是水平面速度向量
        # 的范数（恒 ≥0），故对真实 telemetry 数值结论零变化；abs 只把文档
        # 口径（Σ|v_xy|·dt）落实成代码，防止带符号 slip 被正负抵消（L-13）。
        slip_dist = sum(abs(v) * dt for f, v in zip(force, slip)
                        if f > contact_force_n)
        # 推后方窗与生产脚本/标定件 v2 共用 POSTPUSH_WINDOW_S（L-8）。
        in_win = [v for tt, v in zip(t, slip)
                  if push_t <= tt < push_t + POSTPUSH_WINDOW_S]
        per_foot[foot] = {
            "slip_dist_m": round(slip_dist, 4),
            "slip_vmax_mps": round(max(slip), 3),
            "slip_vmax_postpush_mps": round(max(in_win), 3),
            "contact_force_peak_N": round(max(force), 2),
            "contact_frames": sum(1 for f in force if f > contact_force_n),
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
    if sel.get("push_n_N") != PUSH_FORCE_N or sel.get("push_dur_s") != PUSH_DUR_S:
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


def rung2_check(telemetries: dict[str, dict], csv_rows: list[dict], *,
                profile: "Mapping[str, float] | None" = None) -> dict:
    prof = resolve_threshold_profile(profile)
    diffs: list[dict] = []
    # L-3：缺列在 load_batch 已转 schema 错误短路；这里 row.get 只是第二道
    # 防线，保证直接调用 rung2_check 也不会抛裸 KeyError。
    csv_by_id = {r.get("run_id"): r for r in csv_rows if r.get("run_id")}
    for run_id, data in telemetries.items():
        rec = recompute_run(data, profile=prof)
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
                prof["tilt_max_abs_tol_deg"] + 1e-9:
            d["tilt_max_deg"] = {"recomputed": rec["tilt_max_deg"],
                                 "declared": o.get("tilt_max_deg")}
        for foot in ("R", "L"):
            rf, of = rec["feet"][foot], o["slip_dist_m"][foot]
            if not _close(rf["slip_dist_m"], float(of),
                          prof["slip_dist_rel_tol"], 0.005):
                d[f"slip_dist_{foot}"] = {
                    "recomputed": rf["slip_dist_m"], "declared": float(of)}
            # 峰速口径差：outcomes 从未取整序列取 max 再 round(3)，重算基于
            # round(4) series，窗口 argmax 可能换帧（VMAX_ABS_TOL 溯源见阈值文件）。
            for key_o, key_r in (("slip_v_max_mps", "slip_vmax_mps"),
                                 ("slip_v_max_postpush_mps",
                                  "slip_vmax_postpush_mps")):
                if not _close(float(rf[key_r]), float(o[key_o][foot]),
                              0.0, prof["vmax_abs_tol"]):
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
            # 缺列已在 load_batch 转 schema 错误；row.get 防直接调用裸 KeyError。
            csv_fell, csv_ff = row.get("fell"), row.get("fall_frame")
            if (csv_fell is None or csv_ff is None
                    or int(csv_fell) != int(o["fell"])
                    or _csv_blank(csv_ff) != (o["fall_frame"] is None)):
                d["csv_fell"] = {"csv": (csv_fell, csv_ff),
                                 "telemetry": (o["fell"], o["fall_frame"])}
            csv_tz = row.get("torso_z_min")
            if csv_tz is None:
                d["csv_torso_z_min"] = {"csv": "缺列",
                                        "telemetry": o["torso_z_min"]}
            elif not _close(float(csv_tz), o["torso_z_min"], 1e-3, 0):
                d["csv_torso_z_min"] = {"csv": csv_tz,
                                        "telemetry": o["torso_z_min"]}
            for foot in ("R", "L"):
                csv_v = row.get(f"slip_vmax_{foot}_mps")
                if csv_v is None:
                    d[f"csv_slip_vmax_{foot}"] = {
                        "csv": "缺列",
                        "telemetry": o["slip_v_max_mps"][foot]}
                elif not _close(float(csv_v),
                                o["slip_v_max_mps"][foot], 1e-3, 0):
                    d[f"csv_slip_vmax_{foot}"] = {
                        "csv": csv_v,
                        "telemetry": o["slip_v_max_mps"][foot]}
        if len(d) > 1:
            diffs.append(d)
    return {"pass": not diffs, "checks": {
        "recomputed_from_series": True,
        "fall_criterion": f"z<{FALL_Z_M}m or tilt>{FALL_TILT_DEG}°",
        "slip_integration": f"Σ|v_xy|·{DT_FRAME:g}s while contact_force>{prof['contact_force_n']}N",
        "slip_dist_rel_tol": prof["slip_dist_rel_tol"],
        "csv_cross_checked": True,
        "disagreements": diffs,
    }}


def _csv_blank(v: str) -> bool:
    return str(v).strip() == ""


# ---------------------------------------------------------------------------
# Rung3：物理界 + 扰动交付 + 时间轴
# ---------------------------------------------------------------------------

def rung3_check(telemetries: dict[str, dict], *,
                profile: "Mapping[str, float] | None" = None
                ) -> tuple[dict, dict[str, dict]]:
    prof = resolve_threshold_profile(profile)
    violations: list[str] = []
    recomputed: dict[str, dict] = {}
    for run_id, data in telemetries.items():
        s = data["series"]
        rec = recompute_run(data, profile=prof)
        recomputed[run_id] = rec
        # ice_dr 的 Kp 包络到 1e6（名义 ×90），冲击力界按 DR 包络单独推导。
        force_bound = (prof["contact_force_max_n_dr"]
                       if data["params"]["arm"] == "ice_dr"
                       else prof["contact_force_max_n"])
        for foot in ("R", "L"):
            vmax = max(s[f"slip_v_{foot}"])
            fmax = max(s[f"contact_force_{foot}"])
            if min(s[f"slip_v_{foot}"]) < -1e-6:
                violations.append(f"{run_id}: slip_v_{foot} 出现负值")
            if vmax > prof["slip_v_max_mps"]:
                violations.append(f"{run_id}: slip_v_{foot}={vmax} > {prof['slip_v_max_mps']}")
            if min(s[f"contact_force_{foot}"]) < -1e-6:
                violations.append(f"{run_id}: contact_force_{foot} 出现负值")
            if fmax > force_bound:
                violations.append(
                    f"{run_id}: force_{foot}={fmax} > {force_bound}"
                    + ("（DR Kp 包络界）"
                       if force_bound == prof["contact_force_max_n_dr"]
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
        if rec["push_n"] != PUSH_FORCE_N or abs(rec["push_dur"] - PUSH_DUR_S) > 1e-9:
            violations.append(
                f"{run_id}: 扰动参数 {rec['push_n']}N×{rec['push_dur']}s ≠ 35N×0.2s")
    checks = {
        "slip_v_bound_mps": prof["slip_v_max_mps"],
        "contact_force_bound_N": prof["contact_force_max_n"],
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
        reader = csv.DictReader(line for line in fh
                                if not line.startswith("#"))
        rows = list(reader)
        header = set(reader.fieldnames or [])
    # L-3：CSV 缺列转 schema 错误（evaluate_batch 在发任何视觉调用前短路），
    # 而不是让 rung2 的 row[...] 抛裸 KeyError。
    missing_cols = [c for c in CSV_REQUIRED_FIELDS if c not in header]
    if missing_cols:
        errs.append(f"dr_controls_vs_experimental.csv 缺列：{missing_cols}")
    return telemetries, rows, summary, errs


def evaluate_batch(workdir: str | Path, *,
                   profile: "Mapping[str, float] | None" = None) -> dict:
    """三级向量 + 组对照。rules_pass 仅代表证据可信度。

    profile=None 走 gait_thresholds 默认常量（Task8 口径，逐字节不变）；
    Task6 ascent V2 传 ascent_thresholds.tight_profile() 加严重算。锚点
    （FALL_Z_M/FALL_TILT_DEG/35N/CONTRAST_RATIO）不随 profile 改变。
    """
    prof = resolve_threshold_profile(profile)
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
    r2 = rung2_check(telemetries, csv_rows, profile=prof)
    r3, recomputed = rung3_check(telemetries, profile=prof)
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
