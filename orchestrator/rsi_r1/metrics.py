"""RSI-R1 指标：只从宿主侧可信 raw 记录计算（Plan §3.2/§3.3/§4.1）。

纪律：

* 指标一律由 **host** 从 raw 数组重算；容器/reply 自报的数字只用于互证，不入
  判据；候选根本拿不到本模块的调用权。
* 定义只在 ``rubric.py``：这里 import 它，禁止第二份 δ/窗口/阈值字面量。
* 右删失（stop_time 在 4 s 内未达成）**不得**当 0 处理；以 ``censored`` 标记，
  下游统计只做删失感知的汇总。
* 硬失稳（|q|>4 rad 或非有限）的 B run 记 ``LOSS_CAP``，不按"误差很大"混同。

本模块不 import mujoco / numpy / docker，只处理已规范化的 Python 列表。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1 import rubric as R

#: 指标名（与 rubric.METRICS 键一一对应）。
DISPLACEMENT = "displacement_x_m"
STOP_TIME = "stop_time_s"
TRACKING_RMSE = "normalized_tracking_rmse"


class MetricError(RuntimeError):
    """raw 不足以算指标（形状不对/时间轴坏/参数缺失）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StudyMetrics:
    """一条 run 的指标集；``censored`` 为真的键不出现在 ``values`` 里。"""

    study_id: str
    values: "Mapping[str, Decimal]"
    censored: "Mapping[str, bool]" = field(default_factory=dict)
    details: "Mapping[str, Any]" = field(default_factory=dict)

    def to_dict(self) -> "dict[str, Any]":
        return {"study_id": self.study_id,
                "values": {k: str(v) for k, v in sorted(self.values.items())},
                "censored": {k: bool(v) for k, v in sorted(self.censored.items())},
                "details": dict(self.details)}


def _column(record: Mapping[str, Any], name: str, index: int = 0) -> "list[float]":
    series = record[name]
    if not isinstance(series, list) or not series:
        raise MetricError("RAW_SHAPE", f"{name} 不是非空数组")
    out: "list[float]" = []
    for row in series:
        if isinstance(row, list):
            try:
                value = row[index]
            except IndexError:
                raise MetricError("RAW_SHAPE", f"{name} 行宽不足 {index + 1}") from None
        else:
            if index != 0:
                raise MetricError("RAW_SHAPE", f"{name} 是标量序列，取不到第 {index} 列")
            value = row
        out.append(float(value))
    return out


def _uniform_dt(times: "list[float]") -> float:
    if len(times) < 2:
        raise MetricError("RAW_SHAPE", "时间轴太短")
    dt = times[1] - times[0]
    if not (dt > 0.0) or not math.isfinite(dt):
        raise MetricError("RAW_SHAPE", f"dt={dt!r} 非正")
    for i in range(1, len(times)):
        if abs((times[i] - times[i - 1]) - dt) > 1e-9:
            raise MetricError("DT_MISMATCH", f"[{i-1}]→[{i}] 间隔不均匀")
    return dt


def _dec(value: float) -> Decimal:
    """float64 → Decimal 的最短可复现十进制表示（不做隐式舍入）。"""
    return Decimal(repr(float(value)))


def _stop_time(times: "list[float]", vx: "list[float]", *,
               eps: float, hold_s: float) -> "tuple[float | None, bool]":
    """首次 ``|vx|<eps`` 且随后 ``hold_s`` 内保持的最早时刻；否则右删失。"""
    dt = _uniform_dt(times)
    hold_steps = int(round(hold_s / dt))
    n = len(times)
    for i in range(n):
        if abs(vx[i]) >= eps:
            continue
        if i + hold_steps >= n:
            # 剩余窗口不足以证实保持时长：不能把"看起来停了"当停稳。
            return None, True
        if all(abs(vx[j]) < eps for j in range(i, i + hold_steps + 1)):
            return times[i], False
    return None, True


def compute_study_a(record: Mapping[str, Any], *,
                    initial_x_m: float) -> StudyMetrics:
    """A：4 s 内 x 方向净位移 + 停止时间（右删失感知）。"""
    times = [float(t) for t in record["time_s"]]
    _uniform_dt(times)
    x = _column(record, "qpos", 0)
    vx = _column(record, "qvel", 0)
    if len(x) != len(times):
        raise MetricError("RAW_SHAPE", f"x 长度 {len(x)} != time_s {len(times)}")
    displacement = x[-1] - float(initial_x_m)
    stop, censored = _stop_time(times, vx,
                                eps=float(R.STOP_VX_EPS_MPS),
                                hold_s=float(R.STOP_HOLD_S))
    values: "dict[str, Decimal]" = {DISPLACEMENT: _dec(displacement)}
    flags = {STOP_TIME: censored}
    if not censored:
        values[STOP_TIME] = _dec(stop)
    return StudyMetrics(
        study_id="A", values=values, censored=flags,
        details={"x0_m": float(initial_x_m), "x_last_m": x[-1],
                 "vx_last_mps": vx[-1], "nsteps": len(times),
                 "dt_s": times[1] - times[0]})


def compute_study_b(record: Mapping[str, Any], *,
                    amplitude_rad: float) -> StudyMetrics:
    """B：0.5–4 s 归一化跟踪 RMSE；硬失稳/非有限记 ``LOSS_CAP``。"""
    amplitude = float(amplitude_rad)
    if not (amplitude > 0.0) or not math.isfinite(amplitude):
        raise MetricError("PARAM_INVALID", f"目标幅值 {amplitude_rad!r} 非法")
    events = record.get("instability_events") or []
    if not isinstance(events, list):
        raise MetricError("RAW_SHAPE", "instability_events 不是数组")
    times = [float(t) for t in record["time_s"]]
    _uniform_dt(times)
    if events:
        return StudyMetrics(
            study_id="B", values={TRACKING_RMSE: R.LOSS_CAP},
            censored={TRACKING_RMSE: False},
            details={"loss_applied": True, "events": list(events),
                     "nsteps": len(times)})
    q = _column(record, "qpos", 0)
    target = _column(record, "target_q", 0)
    lo = float(R.TRACKING_WINDOW_START_S)
    hi = float(R.TRACKING_WINDOW_END_S)
    idx = [i for i, t in enumerate(times) if lo <= t <= hi]
    if len(idx) < 2:
        raise MetricError("WINDOW_EMPTY", f"[{lo}, {hi}] s 内只有 {len(idx)} 个采样")
    sq = 0.0
    for i in idx:
        diff = q[i] - target[i]
        sq += diff * diff
    rmse = math.sqrt(sq / len(idx)) / amplitude
    return StudyMetrics(
        study_id="B", values={TRACKING_RMSE: _dec(rmse)},
        censored={TRACKING_RMSE: False},
        details={"loss_applied": False, "window_samples": len(idx),
                 "amplitude_rad": amplitude, "nsteps": len(times),
                 "dt_s": times[1] - times[0]})


def metrics_for(record: Mapping[str, Any], *, study_id: str,
                **kwargs: Any) -> StudyMetrics:
    """按 study 分派；study 之外的键一律拒绝（防止把 A 的 x0 传进 B）。"""
    if study_id == "A":
        unknown = set(kwargs) - {"initial_x_m"}
        if unknown or "initial_x_m" not in kwargs:
            raise MetricError("PARAM_INVALID", f"A 需要 initial_x_m，多余 {sorted(unknown)}")
        return compute_study_a(record, initial_x_m=kwargs["initial_x_m"])
    if study_id == "B":
        unknown = set(kwargs) - {"amplitude_rad"}
        if unknown or "amplitude_rad" not in kwargs:
            raise MetricError("PARAM_INVALID", f"B 需要 amplitude_rad，多余 {sorted(unknown)}")
        return compute_study_b(record, amplitude_rad=kwargs["amplitude_rad"])
    raise MetricError("STUDY_UNKNOWN", repr(study_id))
