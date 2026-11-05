"""Phase 2.8.0（= 路线图 SimPraxis Roadmap Phase 0）研究工程 Benchmark 基座。

本包只承载 **测量工具**：任务契约、运行指纹、出生验证、独立评测与证据包。
本阶段 0 付费、0 模型实验；不得在此包内出现任何 `ModelGateway` 调用。

模块边界：
- ``paths``：数据根唯一解析入口（``SIMPRAXIS_BENCHMARK_ROOT`` 优先）；
- ``intake``：七类基线快照的采集函数（纯函数 + 显式 subprocess 超时）；
- ``cli``：``python -m orchestrator.benchmark.cli <subcommand>`` 统一入口。

``scripts/benchmark/*.py`` 只是按路径运行的薄封装（``scripts/`` 不是包）。
"""
from __future__ import annotations

__all__ = ["paths"]
