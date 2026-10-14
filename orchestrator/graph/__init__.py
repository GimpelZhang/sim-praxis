"""Task3 LangGraph 双轨状态机（详细计划 Task3，替换基础计划 §7）。

依赖方向：graph → direction(Task2) / feishu_gateway(Task1) / gateway；
agent_run(Task4)、evaluate 内 Rung1–4(Task5)、persist_learning(Task6)
在 Task3 仅以「接缝」存在：默认实现显式抛 TaskSeamError 或只读文件，
节点内禁止任何字面量/伪造指标（No Fake Physics，计划 L490）。
"""
from __future__ import annotations

from .nodes import (
    FAILURE_JSON,
    GraphDeps,
    TaskSeamError,
    build_graph,
    file_backed_evaluate,
    initial_state,
)
from .state import SimPraxisState, graph_db_path, run_dir_for

__all__ = [
    "SimPraxisState",
    "GraphDeps",
    "TaskSeamError",
    "FAILURE_JSON",
    "build_graph",
    "file_backed_evaluate",
    "initial_state",
    "graph_db_path",
    "run_dir_for",
]
