"""SimPraxisState —— Task3 契约逐字状态结构（详细计划 L469-483）。

字段一个不多一个不少；events 用 operator.add 归并，其余字段按节点返回值覆盖。
状态只存可序列化数据：进程内组件（网关/飞书/向量库）走 GraphDeps 注入，
**绝不**进入 LangGraph 检查点（SqliteSaver 里只落 JSON）。
"""
from __future__ import annotations

import os
import operator
from pathlib import Path
from typing import Annotated, TypedDict

# 锁内适配（计划 L466-467）：检查点 DB 路径并入 SIMPRAXIS_PHASE1_ROOT/state，
# 环境变量名 SIMPRAXIS_GRAPH_SQLITE；不使用基础计划的 SIMPRAXIS_SQLITE_PATH。


class SimPraxisState(TypedDict, total=False):
    thread_id: str
    track: str                 # track1 | track2
    question: str
    proposals: list
    chosen: dict
    gate1_rejects: int
    agent_runs: list
    gate2_rejects: int
    coding_round: int
    telemetry_uri: str
    metrics: dict
    rung4: dict
    # Phase 2 Task2：ascent 双模式（加法字段；旧快照缺省等价 standard）。
    run_mode: str              # standard | ascent（classify 节点写入）
    v1_archive: "dict | None"  # ascent 命中时的 V1 档案白名单副本
    ascent_targets: dict       # 加严项与因子（standard 为 {}）
    pareto_verdict: "dict | None"  # evaluate 后回填（Task4 接线）
    # Phase 2 Task6：ascent 演化志四来源件路径块（Gate1 批准写部分块、
    # Gate2 批准补齐五键；仅 dict 加法透传，reject/hard-failure 路径不写）。
    evolution_sources: dict
    # Phase 2 Task7 B 线（libero variant）：Stage0 兼容报告路径。CLI run 时注入
    # 初始状态（langgraph 0.2.28 对 TypedDict 未声明键静默丢弃——实测 input/
    # 节点 update 双侧都丢，故必须声明为通道）；Gate1 提案机判（task7_libero.
    # gate1_proposal）与 D-4 降级的唯一状态输入。standard 任务缺省不写。
    stage0_report_path: str
    events: Annotated[list, operator.add]


def phase1_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                               "/mnt/artifacts/simpraxis/phase1"))


def graph_db_path() -> Path:
    """检查点 sqlite：$SIMPRAXIS_GRAPH_SQLITE 优先，否则 phase1_root/state/。"""
    override = os.environ.get("SIMPRAXIS_GRAPH_SQLITE")
    if override:
        return Path(override)
    return phase1_root() / "state" / "langgraph_checkpoints.db"


def run_dir_for(thread_id: str) -> Path:
    """单 thread 运行产物根（FAILURE.json / DONE.json / 运行 telemetry）。"""
    safe = "".join(ch for ch in thread_id if ch.isalnum() or ch in ("-", "_"))
    if not safe or safe != thread_id:
        raise ValueError(f"非法 thread_id：{thread_id!r}（仅允许 [A-Za-z0-9_-]）")
    d = phase1_root() / "runs" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
