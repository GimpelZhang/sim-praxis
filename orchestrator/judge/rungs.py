#!/usr/bin/env python3
"""Rung1–3 规则判定（详细计划 L515-524；只消费 telemetry 数据，No-Fake-Physics）。

输出是**分级向量**而非总分（计划 L524）：
  {"rung1": {"pass": ...}, "rung2": {...}, "rung3": {...}, "rules_pass": bool}
不存在「一个总分用于自动放行」。Rung4 的 VLM 意见由 rung4.py 组合，且永不
单独放行；规则与 VLM 分歧升级人工（D8）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .thresholds import (
    CONTACT_DURATION_S,
    CONTACT_FORCE_CV_MAX,
    DT_BAND,
    GOAL_WINDOW_S,
    MAX_PENETRATION_M,
    jerk_threshold,
)

_SCHEMA_PATH = Path(__file__).with_name("telemetry_schema.json")

# ---------------------------------------------------------------------------
# 结构校验（不引第三方 jsonschema：draft-07 的本契约所需检查手写即可）
# ---------------------------------------------------------------------------

_REQUIRED = ["dt", "ee_positions", "object_poses", "goal_box",
             "penetrations", "contact_forces", "contact_active",
             "substeps_per_frame", "engine", "run_id"]


def validate_telemetry(data: Any) -> list[str]:
    """返回结构错误列表（空列表 = 通过）。只做形状/类型，finite 在 Rung1。"""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["telemetry 顶层必须是对象"]
    for k in _REQUIRED:
        if k not in data:
            errs.append(f"缺必填字段：{k}")
    if errs:
        return errs

    dt = data["dt"]
    if isinstance(dt, bool) or not isinstance(dt, (int, float)) or not dt > 0:
        errs.append("dt 必须为正数（bool 不接受）")

    t = len(data["ee_positions"])
    if t < 6:
        errs.append("ee_positions 至少 6 帧（三阶差分端点所需）")
    for name in ("ee_positions", "object_poses"):
        arr = data[name]
        if not isinstance(arr, list) or any(
                not isinstance(p, list) or len(p) < 3
                or not all(isinstance(v, (int, float)) for v in p[:3])
                for p in arr):
            errs.append(f"{name} 必须是 T×≥3 数值数组")
    if len(data["object_poses"]) != t:
        errs.append("object_poses 帧数必须与 ee_positions 一致")

    gb = data["goal_box"]
    if not isinstance(gb, dict) or set(("center", "half_extents")) - set(gb):
        errs.append("goal_box 必须含 center/half_extents")
    else:
        for k in ("center", "half_extents"):
            if not (isinstance(gb[k], list) and len(gb[k]) == 3
                    and all(isinstance(v, (int, float)) for v in gb[k])):
                errs.append(f"goal_box.{k} 必须是长度 3 的数值数组")

    spf = data["substeps_per_frame"]
    if not isinstance(spf, int) or isinstance(spf, bool) or spf < 1:
        errs.append("substeps_per_frame 必须为正整数")
    pen = data["penetrations"]
    if not isinstance(pen, list) or not all(isinstance(v, (int, float)) for v in pen):
        errs.append("penetrations 必须是数值数组（每物理子步）")
    elif isinstance(spf, int) and not isinstance(spf, bool) \
            and len(pen) != t * spf:
        errs.append(f"penetrations 长度 {len(pen)} != T*substeps={t * spf}")

    cf, ca = data["contact_forces"], data["contact_active"]
    if not (isinstance(cf, list) and len(cf) == t
            and all(isinstance(v, (int, float)) for v in cf)):
        errs.append("contact_forces 必须是长度 T 的数值数组")
    if not (isinstance(ca, list) and len(ca) == t
            and all(isinstance(v, bool) for v in ca)):
        errs.append("contact_active 必须是长度 T 的布尔数组")

    if "joint_torques" in data:
        jt = data["joint_torques"]
        if not (isinstance(jt, list) and len(jt) == t
                and all(isinstance(row, list)
                        and all(isinstance(v, (int, float)) for v in row)
                        for row in jt)):
            errs.append("joint_torques 必须是 T×J 数值数组")
    jl = data.get("joint_limits")
    if jl is not None:
        if not isinstance(jl, dict) or "torque" not in jl:
            errs.append("joint_limits 若给出必须是含 torque 的对象")
        elif not (isinstance(jl["torque"], list)
                  and all(isinstance(x, list) and len(x) == 2
                          and all(isinstance(v, (int, float)) for v in x)
                          for x in jl["torque"])):
            errs.append("joint_limits.torque 必须是 [lo,hi] 数组")
        elif "joint_torques" in data and isinstance(data["joint_torques"], list) \
                and data["joint_torques"] and \
                len(jl["torque"]) != len(data["joint_torques"][0]):
            errs.append("joint_limits.torque 与 joint_torques 关节数不一致")
    if not isinstance(data["engine"], str) or not data["engine"]:
        errs.append("engine 必须为非空字符串")
    if not isinstance(data["run_id"], str) or not data["run_id"]:
        errs.append("run_id 必须为非空字符串")
    return errs


def _finite_leaf_paths(data: Any, prefix: str = "") -> list[tuple[str, float]]:
    """枚举所有数值叶（dict/list 递归）；bool 不算数值。"""
    out: list[tuple[str, float]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out.extend(_finite_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.extend(_finite_leaf_paths(v, f"{prefix}[{i}]"))
    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        out.append((prefix, float(data)))
    return out


# ---------------------------------------------------------------------------
# Rung1：finite 全检 + 逐子步穿模 + 关节力矩越界
# ---------------------------------------------------------------------------

def rung1_check(data: dict) -> dict:
    bad: list[str] = [p for p, v in _finite_leaf_paths(data)
                      if not math.isfinite(v)]
    pen = np.asarray(data["penetrations"], dtype=float)
    # 锁内适配（基础计划 L670/L695-699）：本引擎无 swept CCD 接口，以每物理
    # 子步接触深度替代，子步长=dt/substeps_per_frame；残余风险：高于帧率的
    # 隧穿（单步跨越、接触深度为 0）无法由本检查检出，需 Rung4 视觉/人工兜底。
    # 不得在任何文档里声称 Task5 做了 swept/连续碰撞检测。
    max_pen = float(np.max(np.abs(pen))) if pen.size else 0.0
    checks = {
        "all_finite": len(bad) == 0,
        "max_penetration_m": max_pen,
        "penetration_limit_m": MAX_PENETRATION_M,
        "penetration_within_limit": max_pen <= MAX_PENETRATION_M,
        "ccd_swept": bool(isinstance(data.get("ccd"), dict)
                          and data["ccd"].get("swept") is True),
        "ccd_method": "swept" if (isinstance(data.get("ccd"), dict)
                                  and data["ccd"].get("swept") is True)
                      else "per_substep_contact_depth",
    }
    limit_violations: list[str] = []
    jl = data.get("joint_limits") or {}
    torque_limits = jl.get("torque")
    jt = data.get("joint_torques")
    if torque_limits and jt:
        arr = np.asarray(jt, dtype=float)
        lo = np.asarray([x[0] for x in torque_limits], dtype=float)
        hi = np.asarray([x[1] for x in torque_limits], dtype=float)
        over = np.argwhere((arr < lo - 1e-9) | (arr > hi + 1e-9))
        for fi, ji in over[:10]:
            limit_violations.append(
                f"frame{int(fi)}.joint{int(ji)}={arr[fi, ji]:.6g} "
                f"out of [{lo[ji]:.6g},{hi[ji]:.6g}]")
    checks["joint_torque_within_limits"] = not limit_violations
    passed = (not bad) and checks["penetration_within_limit"] \
        and checks["joint_torque_within_limits"]
    return {"pass": passed, "checks": checks,
            "nonfinite_paths": bad[:20], "joint_limit_violations": limit_violations}


# ---------------------------------------------------------------------------
# Rung2：位姿时序计算 goal（不吃传入 goal_reached）+ 接触持续性
# ---------------------------------------------------------------------------

def _longest_true_run(mask: np.ndarray) -> tuple[int, int]:
    """返回最长连续 True 段的 (起点, 长度)。"""
    best_i, best_n, cur_i, cur_n = -1, 0, -1, 0
    for i, v in enumerate(mask.tolist()):
        if v:
            if cur_n == 0:
                cur_i = i
            cur_n += 1
            if cur_n > best_n:
                best_i, best_n = cur_i, cur_n
        else:
            cur_i, cur_n = -1, 0
    return best_i, best_n


def rung2_check(data: dict) -> dict:
    dt = float(data["dt"])
    obj = np.asarray(data["object_poses"], dtype=float)[:, :3]
    gb = data["goal_box"]
    center = np.asarray(gb["center"], dtype=float)
    half = np.asarray(gb["half_extents"], dtype=float)
    inside = np.all(np.abs(obj - center) <= half + 1e-9, axis=1)

    n_win = max(1, int(math.ceil(GOAL_WINDOW_S / dt)))
    # 短于 0.5 s 末窗口的片段不得"用全部帧凑满"判通过（No-Fake-Physics）
    n_frames = len(inside)
    window_long_enough = n_frames >= n_win
    tail_inside = window_long_enough and bool(inside[-n_win:].all())
    goal_frames = int(inside.sum())

    # 锁内适配（基础计划 L702）：goal_reached 只能由位姿时序与公差盒计算；
    # telemetry 即便带了 goal_reached 字段也绝不读取。
    active = np.asarray(data["contact_active"], dtype=bool)
    forces = np.abs(np.asarray(data["contact_forces"], dtype=float))
    i0, run_n = _longest_true_run(active)
    duration = run_n * dt
    if run_n > 0:
        window = forces[i0:i0 + run_n]
        mean = float(window.mean())
        cv = float(window.std() / mean) if mean > 1e-12 else float("inf")
    else:
        mean, cv = 0.0, float("inf")
    stable_contact = (duration >= CONTACT_DURATION_S - 1e-9) \
        and cv <= CONTACT_FORCE_CV_MAX

    passed = tail_inside and stable_contact
    return {"pass": passed, "checks": {
        "goal_from_poses": True,
        "goal_input_bool_ignored": "goal_reached" in data,
        "last_window_frames": n_win,
        "window_long_enough": window_long_enough,
        "last_window_all_inside_box": tail_inside,
        "inside_box_frames": goal_frames,
        "contact_longest_run_s": round(duration, 6),
        "contact_cv": None if math.isinf(cv) else round(cv, 6),
        "contact_force_mean_n": round(mean, 6),
        "stable_contact": stable_contact,
        "duration_required_s": CONTACT_DURATION_S,
        "cv_max": CONTACT_FORCE_CV_MAX,
    }}


# ---------------------------------------------------------------------------
# Rung3：jerk 三阶差分（端点单侧）+ 超阈计数
# ---------------------------------------------------------------------------

def _third_derivative(x: np.ndarray, dt: float) -> np.ndarray:
    """逐帧三阶导数。内部中心差分/(2dt³)，端点单侧差分/dt³。"""
    n = x.shape[0]
    out = np.zeros_like(x)
    if n < 4:
        return out
    # 中心：f'''(i) ≈ (x[i+2]-2x[i+1]+2x[i-1]-x[i-2]) / (2 h^3)
    i = np.arange(2, n - 2)
    out[2:n - 2] = (x[i + 2] - 2 * x[i + 1] + 2 * x[i - 1] - x[i - 2]) \
        / (2.0 * dt ** 3)
    # 前端前向 Δ³/h³；后端后向 Δ³/h³
    out[0] = (x[3] - 3 * x[2] + 3 * x[1] - x[0]) / dt ** 3
    out[1] = (x[4] - 3 * x[3] + 3 * x[2] - x[1]) / dt ** 3 if n >= 5 else out[1]
    out[n - 1] = (x[n - 1] - 3 * x[n - 2] + 3 * x[n - 3] - x[n - 4]) / dt ** 3
    if n >= 5:
        out[n - 2] = (x[n - 2] - 3 * x[n - 3] + 3 * x[n - 4] - x[n - 5]) / dt ** 3
    return out


def rung3_check(data: dict) -> dict:
    dt = float(data["dt"])
    ee = np.asarray(data["ee_positions"], dtype=float)[:, :3]
    lo, hi = DT_BAND
    if not (lo <= dt <= hi):
        return {"pass": False, "checks": {
            "dt_band_ok": False, "dt": dt, "dt_band": [lo, hi],
            "jerk_max_mps3": None, "over_threshold_count": None,
            "jerk_threshold_mps3": None,
            "reason": "dt 超出阈值锚定带 [1/120,1/30]，保守判负"}}
    jerk = _third_derivative(ee, dt)
    mag = np.linalg.norm(jerk, axis=1)
    thr = jerk_threshold(dt)
    over = int(np.sum(mag > thr))
    return {"pass": over == 0, "checks": {
        "dt_band_ok": True, "dt": dt,
        "jerk_max_mps3": round(float(mag.max()), 6),
        "jerk_threshold_mps3": round(float(thr), 6),
        "over_threshold_count": over,
        "jerk_per_frame_mps3": [round(float(v), 3) for v in mag],
    }}


# ---------------------------------------------------------------------------
# 汇总：分级向量（无总分）
# ---------------------------------------------------------------------------

def evaluate_rollout(data: dict) -> dict:
    schema_errors = validate_telemetry(data)
    if schema_errors:
        return {"ok": False, "schema_errors": schema_errors,
                "rung1": {"pass": False}, "rung2": {"pass": False},
                "rung3": {"pass": False}, "rung4": None,
                "rules_pass": False,
                "provenance": "file:telemetry_uri"}
    r1, r2, r3 = rung1_check(data), rung2_check(data), rung3_check(data)
    return {
        "ok": True,
        "engine": data["engine"], "run_id": data["run_id"],
        "fixture": bool(data.get("_fixture", False)),
        "schema_errors": [],
        "rung1": r1, "rung2": r2, "rung3": r3, "rung4": None,
        "vector": {"rung1": r1["pass"], "rung2": r2["pass"],
                   "rung3": r3["pass"]},
        "rules_pass": bool(r1["pass"] and r2["pass"] and r3["pass"]),
        "provenance": "file:telemetry_uri",
    }


def evaluate_telemetry_file(uri: str | Path) -> dict:
    """Task7 图接缝入口：metrics 只从文件读数。"""
    with open(uri, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    result = evaluate_rollout(data)
    result["telemetry_uri"] = str(uri)
    return result
