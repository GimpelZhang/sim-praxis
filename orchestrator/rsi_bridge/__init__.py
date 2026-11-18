"""PhysicalRSI 接缝层（引擎仓开源面；只经文件/JSON 与 py3.11 外环进程交换）。

三接缝（设计族 §6 / 计划 S0-6）：
- `adapter`      环境适配 `physicalrsi.task/v1`（check/run/evolve/status）
- `model_config` 模型调用一律回 `ModelGateway.call`（stage 映射钉死，禁直填模型 ID）
- `receipts` / `memory_store` / `selection`  证据互证 / 闭源 ResearchMemory / 选择器透传
"""
from __future__ import annotations

__all__ = ["adapter", "model_config", "receipts", "memory_store", "selection", "audit"]
