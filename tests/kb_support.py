"""kb_integration 测试开关（Task0b）。

与 paid marker 同构（见 tests/conftest.py pytest_configure 与
tests/milestone1/test_hitl_drill.py:148 的函数体内自 skip）：默认裸跑 pytest
时，读活 KB 仓/活数据层的测试一律 skip，不是 test.skip 放水、不弱化任何
断言；只有显式 SIMPRAXIS_KB_INTEGRATION=1 且活 KB 在场时才执行。

用法：
    @pytest.mark.kb_integration
    def test_x():
        kb = require_kb_integration()        # 活内容层（solutions/）
        ...
        kb = require_kb_integration(data=True)  # 另要求 data/lancedb 在场
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

DEFAULT_KB_ROOT = "/home/vipuser/embodied-ai-sim-knowledge-base"
ENV_FLAG = "SIMPRAXIS_KB_INTEGRATION"
# 迁移提交：KB 仓 solutions/ 首次写入（Task0b 回填）。
MIGRATION_KB_SHA = "c5712e9f73fc17cd2649cae359aad216e8f3bd07"


def kb_root() -> Path:
    return Path(os.environ.get("KB_ROOT", DEFAULT_KB_ROOT))


def require_kb_integration(*, data: bool = False) -> Path:
    """活 KB 前置；不满足即 skip（与 paid 双守卫同构，非放水）。"""
    if os.environ.get(ENV_FLAG) != "1":
        pytest.skip(f"需显式 {ENV_FLAG}=1（读活 KB 仓/活数据层，默认 skip）")
    root = kb_root()
    if not (root / "solutions").is_dir():
        pytest.skip(f"活 KB 内容层不存在：{root / 'solutions'}")
    if data:
        # 必须是含 kb_chunks.lance 的已迁移库——空 data/lancedb 目录可能只是
        # VectorEngine() 默认 uri 被 mkdir 的副产物，不算活数据层。
        lance = root / "data" / "lancedb" / "kb_chunks.lance"
        if not lance.is_dir():
            pytest.skip(f"活数据层未迁移：{lance} 不存在（先跑 scripts/kb/migrate_data_layer.py）")
    return root
