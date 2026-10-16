"""Phase 1/2 兜底不变量：KB 分层钉点 + LanceDB 基线表零污染 + Qxx 原子块 104。

每个 Task 收尾必跑（gate 外的常规 pytest 收集即覆盖）。任何一条失败都说明
代码动了基线，必须立即 STOP 排查，禁止弱化断言。

Task0b（三层分离）后分两套（与 paid marker 同构，非 test.skip 放水）：
- 读活 KB 仓的检查打 kb_integration（需 SIMPRAXIS_KB_INTEGRATION=1）：
  HEAD≥迁移提交 + knowledge/ 32-md 哈希集、工作区干净、活数据层 1542/768、
  check_chunk_integrity --expect 104；
- 纯常量检查（表名）留在默认套。
纯本地 git/lancedb/子进程检查：不联网、0 付费。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from memory_flywheel import kb_pins
from memory_flywheel.scripts.ingest_kb import KB_ROOT
from memory_flywheel.vector_engine import TABLE_NAME, VectorEngine
from tests.kb_support import require_kb_integration

EXPECTED_KB_ROWS = 1542          # Phase 0 全量入库日志：kb_chunks 1542 行
EXPECTED_VECTOR_DIM = 768        # paraphrase-multilingual-mpnet-base-v2
EXPECTED_Q_CHUNKS = 104          # Qxx 原子块（check_chunk_integrity --expect 104）


@pytest.mark.kb_integration
def test_kb_layered_pins_descendant_and_hash_set():
    """Task0b 回填：HEAD 必须是迁移提交后代，且 knowledge/ 32-md 哈希集 == aac53313 基线。"""
    require_kb_integration()
    errors = kb_pins.check_layered_pins(KB_ROOT)
    assert not errors, "KB 分层不变量失败：\n" + "\n".join(errors)
    # 迁移提交自身在其后代关系上成立（is-ancestor 含等于）
    assert kb_pins.is_descendant(kb_pins.MIGRATION_KB_SHA, KB_ROOT)


@pytest.mark.kb_integration
def test_kb_worktree_clean():
    """KB 仓工作区必须零改动（不允许脏工作区；untracked 也不允许，data/ 须被 ignore）。"""
    require_kb_integration()
    status = subprocess.run(
        ["git", "-C", str(KB_ROOT), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"KB 仓出现改动：\n{status}"


@pytest.mark.kb_integration
def test_lancedb_baseline_table_rows_and_dim():
    """kb_chunks 基线：1542 行、vector list_size=768（学习只允许写新表）。"""
    require_kb_integration(data=True)
    eng = VectorEngine()
    table = eng.table()
    assert table.count_rows() == EXPECTED_KB_ROWS, (
        f"kb_chunks 行数 {table.count_rows()} != 基线 {EXPECTED_KB_ROWS}（基线表被污染？）"
    )
    dim = table.schema.field("vector").type.list_size
    assert dim == EXPECTED_VECTOR_DIM, f"vector 维度 {dim} != {EXPECTED_VECTOR_DIM}"


def test_baseline_table_name_is_kb_chunks():
    """基线表名必须仍为 kb_chunks；新学习表 phase1_learnings / solution_chunks 不得替代表。"""
    assert TABLE_NAME == "kb_chunks"


@pytest.mark.kb_integration
def test_qxx_atomic_chunks_subprocess():
    """子进程跑 check_chunk_integrity --expect 104，returncode 必须为 0。"""
    require_kb_integration(data=True)
    proc = subprocess.run(
        [sys.executable, "-m", "memory_flywheel.scripts.check_chunk_integrity",
         "--expect", str(EXPECTED_Q_CHUNKS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"check_chunk_integrity 失败（exit {proc.returncode}）：\n"
        f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
