"""H_k 研究驾驭层：类型化研究技能序列 + 守卫 + 稀疏记忆 + 参数面（S0 骨架）。

归属（设计族 §5 产物归属表）：本包是**引擎仓开源面**——通用技能机、守卫框架、
Contract 类型表；领域技能函数与任务构建器在 KB 仓 `solutions/`；运行态数据
（m_t / ResearchMemory / 进化档案 / 参数经验值）在闭源数据层。

依赖单一真值：Contract/SkillDescriptor/CallableSkill/compose_sequence/adapt/
ParameterSpace 全部来自 pin 死的 `physicalrsi==0.3.0`（见 `_physicalrsi.py`）。
本包不复制、不改写上游实现。
"""
from __future__ import annotations

__all__ = ["contracts", "guards", "sparse_memory", "params", "skills", "driver"]
