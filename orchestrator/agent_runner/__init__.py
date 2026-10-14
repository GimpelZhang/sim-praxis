"""Task4 agent_runner：真实 coding_debug 多轮补救（详细计划 Task4）。

对外主类 AgentRunner；真实 track 运行命令由 Task7/8 经 RunSpec 注入，
本任务不安装/不运行任何仿真平台本体。
"""
from __future__ import annotations

from .runner import (
    MAX_CODING_ROUNDS,
    AgentRunner,
    CodingLimitError,
    PatchSafetyError,
    RunSpec,
    agent_run_seam,
)

__all__ = [
    "MAX_CODING_ROUNDS",
    "AgentRunner",
    "CodingLimitError",
    "PatchSafetyError",
    "RunSpec",
    "agent_run_seam",
]
