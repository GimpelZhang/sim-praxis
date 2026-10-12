"""orchestrator.gateway 包初始化。

Python 3.10 兼容垫片（Phase 0 实测 2026-10-12）：
litellm==1.99.4 声明 Requires-Python>=3.10，但其被 litellm/__init__.py
急切导入的 experimental_pass_through/.../editors/compact.py 第 17 行
无条件 `from typing import NotRequired`（3.11+ 才入 typing；同包 19 个
兄弟模块正确使用 typing_extensions）。这是上游 py3.10 打包疏漏。

处置遵循 Phase_0 计划风险矩阵"锁版本内修适配代码、不升级大版本"：
不改钉版、不改 site-packages（不可重建），在导入本包时把 typing_extensions
中已存在的 3.11+ 名称按缺失项别名到 typing。任何经
`orchestrator.gateway.*` 导入 litellm 的路径都受保护。
"""
from __future__ import annotations

import sys
import typing

if sys.version_info < (3, 11):
    import typing_extensions as _typing_extensions

    for _name in (
        "NotRequired",
        "Required",
        "ReadOnly",
        "Self",
        "Unpack",
        "assert_never",
        "override",
        "TypeVarTuple",
        "Never",
        "assert_type",
    ):
        if not hasattr(typing, _name) and hasattr(_typing_extensions, _name):
            setattr(typing, _name, getattr(_typing_extensions, _name))
