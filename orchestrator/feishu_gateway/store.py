"""pending_gates 持久层（详细计划 §1.2 逐字 DDL）。

DB 默认 $SIMPRAXIS_PHASE1_ROOT/state/pending_gates.db：
  - 目录 0700、数据库文件 0600（umask 077 + 显式 chmod）；
  - 全部 SQL 参数化；
  - 写库绝不记录 app_secret / tenant_access_token（本表无该列，调用方也不得写入）；
  - 重启语义：同主键已有 PENDING 行时，upsert 不覆盖已发的
    card_message_id / media_message_id / open_chat_id（COALESCE 仅在缺省时填回），
    且 ON CONFLICT 的 UPDATE 带 WHERE status='PENDING'，已 DECIDED/EXPIRED 行不被重开。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# 详细计划 §1.2 逐字 DDL（列序、CHECK、PRIMARY KEY(thread_id, gate) 不得改动）
_DDL = """
CREATE TABLE IF NOT EXISTS pending_gates(
  thread_id TEXT NOT NULL,
  gate INTEGER NOT NULL CHECK(gate IN (1,2)),
  status TEXT NOT NULL CHECK(status IN ('PENDING','DECIDED','EXPIRED')),
  choice INTEGER,                 -- Gate1: 方案索引；Gate2: NULL
  decision TEXT,                  -- approve | reject_with_comments
  human_notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,           -- card_form | p2p_text | cli
  card_message_id TEXT,
  media_message_id TEXT,
  open_chat_id TEXT,
  operator_open_id TEXT,
  created_at INTEGER NOT NULL,    -- epoch ms
  decided_at INTEGER,
  PRIMARY KEY(thread_id, gate)
);
"""

# PENDING 行允许被回填的消息列（只允许 NULL -> 非 NULL，禁止覆盖，重启重挂用）
_FILLABLE = ("card_message_id", "media_message_id", "open_chat_id")


def default_db_path() -> Path:
    """$SIMPRAXIS_PHASE1_ROOT/state/pending_gates.db；未导出时回退数据根。"""
    root = os.environ.get("SIMPRAXIS_PHASE1_ROOT")
    if not root:
        root = os.path.join(
            os.environ.get("SIMPRAXIS_DATA_ROOT", "/mnt/artifacts/simpraxis"),
            "phase1",
        )
    return Path(root) / "state" / "pending_gates.db"


def now_ms() -> int:
    return int(time.time() * 1000)


class PendingStore:
    """pending_gates 表的唯一访问点；每次操作短连接（sqlite，线程间可重入）。"""

    def __init__(self, db_path: "str | Path | None" = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        # check_same_thread=False：WS 回调线程与阻塞等待线程都会访问；
        # 写操作均为毫秒级单行 SQL，依靠 sqlite 自身行锁串行化。
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_DDL)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PendingStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def upsert_pending(
        self,
        thread_id: str,
        gate: int,
        *,
        card_message_id: "str | None" = None,
        media_message_id: "str | None" = None,
        open_chat_id: "str | None" = None,
        created_at_ms: "int | None" = None,
    ) -> None:
        """插入 PENDING 行；同主键 PENDING 行只回填缺失消息列，绝不覆盖已发消息。

        已 DECIDED/EXPIRED 的同主键行：WHERE 守卫使 UPDATE 成为 no-op（业务侧不应
        对同一 thread_id+gate 再次请求；这里静默保护不重开历史决策）。
        """
        created_at = created_at_ms if created_at_ms is not None else now_ms()
        # source NOT NULL 但决策前无来源：插入空串占位，mark_decided 写真实来源。
        self._conn.execute(
            """
            INSERT INTO pending_gates(
                thread_id, gate, status, choice, decision, human_notes, source,
                card_message_id, media_message_id, open_chat_id,
                operator_open_id, created_at, decided_at)
            VALUES (?, ?, 'PENDING', NULL, NULL, '', '', ?, ?, ?, NULL, ?, NULL)
            ON CONFLICT(thread_id, gate) DO UPDATE SET
                card_message_id = COALESCE(pending_gates.card_message_id,
                                           excluded.card_message_id),
                media_message_id = COALESCE(pending_gates.media_message_id,
                                            excluded.media_message_id),
                open_chat_id = COALESCE(pending_gates.open_chat_id,
                                        excluded.open_chat_id)
            WHERE pending_gates.status = 'PENDING'
            """,
            (thread_id, gate, card_message_id, media_message_id,
             open_chat_id, created_at),
        )

    def attach_message_ids(
        self,
        thread_id: str,
        gate: int,
        *,
        card_message_id: "str | None" = None,
        media_message_id: "str | None" = None,
        open_chat_id: "str | None" = None,
    ) -> None:
        """消息发送成功后回填（同样只 COALESCE，不覆盖）。"""
        sets, vals = [], []
        params: dict[str, Optional[str]] = {
            "card_message_id": card_message_id,
            "media_message_id": media_message_id,
            "open_chat_id": open_chat_id,
        }
        for col in _FILLABLE:
            if params[col] is not None:
                sets.append(f"{col} = COALESCE({col}, :{col})")
        if not sets:
            return
        sql = (
            "UPDATE pending_gates SET " + ", ".join(sets)
            + " WHERE thread_id = :thread_id AND gate = :gate AND status = 'PENDING'"
        )
        params.update({"thread_id": thread_id, "gate": gate})
        self._conn.execute(sql, params)

    def get_pending(self, thread_id: str, gate: int) -> "dict[str, Any] | None":
        cur = self._conn.execute(
            "SELECT * FROM pending_gates WHERE thread_id = ? AND gate = ?",
            (thread_id, gate),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def list_pending(
        self,
        *,
        status: "str | None" = None,
        open_chat_id: "str | None" = None,
    ) -> "list[dict[str, Any]]":
        sql = "SELECT * FROM pending_gates WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if open_chat_id is not None:
            sql += " AND open_chat_id = ?"
            params.append(open_chat_id)
        sql += " ORDER BY created_at ASC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mark_decided(
        self,
        thread_id: str,
        gate: int,
        *,
        decision: str,
        source: str,
        choice: "int | None" = None,
        human_notes: "str | None" = None,
        operator_open_id: "str | None" = None,
    ) -> bool:
        """落决策。仅当前状态为 PENDING 时生效（幂等：重复点击/竞态不覆盖）。

        返回是否实际落库。human_notes 为 None 时保留既有文本（通道②已追加的意见），
        非 None 时做一次合并：既有文本在前、卡片反馈在后，去重重叠前缀。
        """
        if decision not in ("approve", "reject_with_comments"):
            raise ValueError(f"非法 decision: {decision!r}")
        if source not in ("card_form", "p2p_text", "cli"):
            raise ValueError(f"非法 source: {source!r}")
        existing = self.get_pending(thread_id, gate)
        if existing is None or existing["status"] != "PENDING":
            return False
        notes = existing["human_notes"] or ""
        if human_notes is not None and human_notes.strip():
            piece = human_notes.strip()
            if piece not in notes:
                notes = (notes + "\n" + piece).strip()
        cur = self._conn.execute(
            """
            UPDATE pending_gates
               SET status = 'DECIDED', decision = ?, choice = ?, source = ?,
                   human_notes = ?, operator_open_id = COALESCE(operator_open_id, ?),
                   decided_at = ?
             WHERE thread_id = ? AND gate = ? AND status = 'PENDING'
            """,
            (decision, choice, source, notes, operator_open_id, now_ms(),
             thread_id, gate),
        )
        return cur.rowcount > 0

    def append_human_note(
        self,
        thread_id: str,
        gate: int,
        note_line: str,
        *,
        open_chat_id: "str | None" = None,
    ) -> bool:
        """通道②：向 human_notes 追加一行（换行分隔）。

        PENDING/DECIDED 行都允许追加（已 DECIDED 仅留痕，绝不改决策）；
        EXPIRED 不追加。同时可 COALESCE 回填 open_chat_id。返回是否写入。
        """
        row = self.get_pending(thread_id, gate)
        if row is None or row["status"] == "EXPIRED":
            return False
        merged = (row["human_notes"] or "")
        if merged:
            merged += "\n"
        merged += note_line
        if open_chat_id is not None and not row["open_chat_id"]:
            self._conn.execute(
                """
                UPDATE pending_gates
                   SET human_notes = ?, open_chat_id = COALESCE(open_chat_id, ?)
                 WHERE thread_id = ? AND gate = ?
                """,
                (merged, open_chat_id, thread_id, gate),
            )
        else:
            self._conn.execute(
                "UPDATE pending_gates SET human_notes = ? "
                "WHERE thread_id = ? AND gate = ?",
                (merged, thread_id, gate),
            )
        return True

    def mark_expired(self, thread_id: str, gate: int) -> bool:
        cur = self._conn.execute(
            "UPDATE pending_gates SET status = 'EXPIRED', decided_at = ? "
            "WHERE thread_id = ? AND gate = ? AND status = 'PENDING'",
            (now_ms(), thread_id, gate),
        )
        return cur.rowcount > 0
