"""Task0b 三层分离：解题方案（内容层）解析与 stage。

内容层物理位置：``$KB_ROOT/solutions``（KB 公开仓），引擎仓只保留本解析器与
``templates/solution_manifest.example.yaml`` 契约模板。任何缺字段/sha256 不符/
路径 C4 违规都抛 :class:`SolutionError`（fail-closed：不网络兜底、不就地生成）。
"""
from orchestrator.solutions.resolver import (
    DEFAULT_KB_ROOT,
    SCHEMA_ID,
    SolutionError,
    SolutionFile,
    SolutionManifest,
    StagedSolution,
    load_manifest,
    resolve_solution,
    solutions_root,
)

__all__ = [
    "DEFAULT_KB_ROOT",
    "SCHEMA_ID",
    "SolutionError",
    "SolutionFile",
    "SolutionManifest",
    "StagedSolution",
    "load_manifest",
    "resolve_solution",
    "solutions_root",
]
