"""Phase 2.8.1（＝Roadmap Phase 1）实验包。

只导出常量入口；路径解析在 :mod:`orchestrator.benchmark.phase281.paths`，
后续 Task 的评测器/出生/运行器各自成模块（见计划 §16.1 的文件总表）。
"""
from __future__ import annotations

from orchestrator.benchmark.phase281.constants import (  # noqa: F401
    ARMS,
    ARM_IDS,
    CALL_CAP_PER_TRIAL,
    CONFIRMATORY_BUDGET,
    EXPERIMENT_ID,
    HARD_CAP,
    PHASE_NAME,
    PILOT_BUDGET,
    REPS_TOTAL,
    TASK_ORDER,
    trial_id,
)

__all__ = (
    "ARMS",
    "ARM_IDS",
    "CALL_CAP_PER_TRIAL",
    "CONFIRMATORY_BUDGET",
    "EXPERIMENT_ID",
    "HARD_CAP",
    "PHASE_NAME",
    "PILOT_BUDGET",
    "REPS_TOTAL",
    "TASK_ORDER",
    "trial_id",
)
