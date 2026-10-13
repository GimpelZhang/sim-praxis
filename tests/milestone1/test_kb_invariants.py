"""Phase 1 全程兜底不变量：KB 只读钉死 + LanceDB 基线表零污染 + Qxx 原子块 104。

每个 Task 收尾必跑（gate 外的常规 pytest 收集即覆盖）。任何一条失败都说明
Phase 1 代码动了只读基线，必须立即 STOP 排查，禁止弱化断言。

纯本地 git/lancedb/子进程检查：不联网、0 付费。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from memory_flywheel.scripts.ingest_kb import KB_BASELINE_SHA, KB_ROOT
from memory_flywheel.vector_engine import TABLE_NAME, VectorEngine

EXPECTED_KB_ROWS = 1542          # Phase 0 全量入库日志：kb_chunks 1542 行
EXPECTED_VECTOR_DIM = 768        # paraphrase-multilingual-mpnet-base-v2
EXPECTED_Q_CHUNKS = 104          # Qxx 原子块（check_chunk_integrity --expect 104）


def test_kb_head_pinned_to_baseline_sha():
    """KB 仓 HEAD 必须等于 ingest_kb.KB_BASELINE_SHA（禁止第四处字面量真值）。"""
    head = subprocess.run(
        ["git", "-C", str(KB_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == KB_BASELINE_SHA, (
        f"KB HEAD 已移动（只读违规）：{head} != 基线 {KB_BASELINE_SHA}"
    )


def test_kb_worktree_clean():
    """KB 仓工作区必须零改动（不提交、不修改；untracked 也不允许）。"""
    status = subprocess.run(
        ["git", "-C", str(KB_ROOT), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"KB 仓出现改动（只读违规）：\n{status}"


def test_lancedb_baseline_table_rows_and_dim():
    """kb_chunks 基线：1542 行、vector list_size=768（Phase 1 学习只允许写新表）。"""
    eng = VectorEngine()
    table = eng.table()
    assert table.count_rows() == EXPECTED_KB_ROWS, (
        f"kb_chunks 行数 {table.count_rows()} != 基线 {EXPECTED_KB_ROWS}（基线表被污染？）"
    )
    dim = table.schema.field("vector").type.list_size
    assert dim == EXPECTED_VECTOR_DIM, f"vector 维度 {dim} != {EXPECTED_VECTOR_DIM}"


def test_baseline_table_name_is_kb_chunks():
    """基线表名必须仍为 kb_chunks；Phase 1 新学习表 phase1_learnings 不得改名或替代表。"""
    assert TABLE_NAME == "kb_chunks"


def test_qxx_atomic_chunks_subprocess():
    """子进程跑 check_chunk_integrity --expect 104，returncode 必须为 0。"""
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
