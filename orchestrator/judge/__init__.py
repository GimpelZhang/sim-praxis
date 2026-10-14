"""Task5 Rung1–4 分级验证（详细计划 L508-530）。

- rungs：Rung1–3 规则判定（No-Fake-Physics：只消费 telemetry 文件数据）；
- rung4：visual_rollout 原子 rubric + 标定闸门（永不单独放行）；
- collage：首/中/末 + 接触时刻 ±5 帧 2×3 PNG 抽帧拼图；
- clips/calibrate：标定片合成与 6 题标定（付费批次，需书面批准）；
- seam：Task7 图 evaluate 接缝工厂（Task5 不切换 CLI 默认实现）。
"""
from __future__ import annotations

from .rung4 import Rung4Judge, Rung4Result, calibration_status
from .rungs import evaluate_rollout, validate_telemetry
from .thresholds import (
    CONTACT_DURATION_S,
    CONTACT_FORCE_CV_MAX,
    DT_BAND,
    GOAL_WINDOW_S,
    JERK_THRESHOLD_60HZ,
    MAX_PENETRATION_M,
)

__all__ = [
    "Rung4Judge",
    "Rung4Result",
    "calibration_status",
    "evaluate_rollout",
    "validate_telemetry",
    "MAX_PENETRATION_M",
    "JERK_THRESHOLD_60HZ",
    "GOAL_WINDOW_S",
    "CONTACT_DURATION_S",
    "CONTACT_FORCE_CV_MAX",
    "DT_BAND",
]
