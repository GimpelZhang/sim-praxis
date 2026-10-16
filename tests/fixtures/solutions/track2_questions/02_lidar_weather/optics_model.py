"""合成 fixture 方案（Task0b）：Q2 光学库模块占位。

正本在 KB 公开仓
solutions/track2_questions/02_lidar_weather/optics_model.py（含真实天气消光/
后向散射模型与 13 条物理数值断言）。本 fixture 只承载真人闸门「读源码不
import」契约所需的 QBACK_DEFAULT 常量；数值与正本一致（0.4，未标定残差见
KB README），但不含任何物理实现。
"""
from __future__ import annotations

QBACK_DEFAULT = 0.4
