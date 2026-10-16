"""pending_gates 持久层（详细计划 §1.2 逐字 DDL）。

DB 默认 $SIMPRAXIS_PHASE1_ROOT/state/pending_gates.db：
  - 目录 0700、数据库文件 0600（umask 077 + 显式 chmod）；
  - 全部 SQL 参数化；
  - 写库绝不记录 app_secret / tenant_access_token（本表无该列，调用方也不得写入）；
  - 重启语义：同主键已有 PENDING 行时，upsert 不覆盖已发的
    card_message_id / media_message_id / open_chat_id（COALESCE 仅在缺省时填回），
    且 ON CONFLICT 的 UPDATE 带 WHERE status='PENDING'，已 DECIDED/EXPIRED 行不被重开。

schema 演进（H-1/H-6，2026-10-16）：对**已存在的生产库**纯增量迁移——
  - user_version 0→1：pending_gates 追加 reminder_count / last_reminder_at /
    task_title 三列（ALTER ADD，列序追加，原 13 列一字不动）；
  - 新增 inbound_messages 表：通道② P2P 入站消息去重（message_id 主键，
    与意见追加在同一事务内写入，重投不产生重复意见）。
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

# 锁内适配（Task7，2026-10-15 真实 Gate2 打回实证）：计划 D10 允许 Gate1/Gate2
# 各打回 2 次（打回后重跑再发新卡片），而 §1.2 单行主键使 upsert 无法重开
# DECIDED 行——实测后果：唯一一次真人「打回+补充说明」被第二轮 _await_decision
# 读到的旧行瞬时算作第二次打回，线程误终 human_failure。处置：主表 DDL 一字
# 不动，新一轮请求前把终态行整体归档到仅追加的 history 表（纯增量迁移），
# 主表再插新 PENDING 行；旧卡片点击因匹配不到 PENDING 行而失效。
_DDL_HISTORY = """
CREATE TABLE IF NOT EXISTS pending_gates_history(
  thread_id TEXT NOT NULL,
  gate INTEGER NOT NULL,
  status TEXT NOT NULL,
  choice INTEGER,
  decision TEXT,
  human_notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  card_message_id TEXT,
  media_message_id TEXT,
  open_chat_id TEXT,
  operator_open_id TEXT,
  created_at INTEGER NOT NULL,
  decided_at INTEGER,
  archived_at INTEGER NOT NULL
);
"""

# H-1：通道②入站消息去重表。message_id 为飞书消息唯一 ID（重投/SDK 重放时
# 不变）；与 human_notes 追加在同一 BEGIN IMMEDIATE 事务内写入。
_DDL_INBOUND = """
CREATE TABLE IF NOT EXISTS inbound_messages(
  message_id TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL,   -- epoch ms
  thread_id TEXT,
  gate INTEGER
);
"""

# 当前 schema 版本（PRAGMA user_version）。每个版本只做纯增量 ALTER/CREATE。
_USER_VERSION = 1

_HISTORY_COLS = (
    "thread_id, gate, status, choice, decision, human_notes, source, "
    "card_message_id, media_message_id, open_chat_id, operator_open_id, "
    "created_at, decided_at"
)

# PENDING 行允许被回填的消息列（只允许 NULL -> 非 NULL，禁止覆盖，重启重挂用）
_FILLABLE = ("card_message_id", "media_message_id", "open_chat_id")

# purge_thread_like 的 LIKE 逃逸符（与 probes/probe_c_form.py 历史裸 DELETE 同款）
_LIKE_ESCAPE = "\\"


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
        # isolation_level=None（autocommit）：多语句原子性由本类显式
        # BEGIN IMMEDIATE/COMMIT 控制（`with conn` 在 autocommit 下**不会**
        # 自动开事务，H-3 已实证）。
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # H-3：写锁竞争时最多等 5s，而非立即抛 database is locked。
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_DDL)
        self._conn.execute(_DDL_HISTORY)
        self._migrate()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        """user_version 守护的纯增量迁移；老生产库原地 ALTER，绝不删表重建。"""
        version = self._conn.execute("PRAGMA user_version;").fetchone()[0]
        if version >= _USER_VERSION:
            return
        if version < 1:
            # 老库（user_version=0）为原始 13 列；三列全部带默认值/可空，
            # ALTER ADD 在 SQLite 上是元数据级操作，不重写既有行。
            self._conn.execute(
                "ALTER TABLE pending_gates "
                "ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute(
                "ALTER TABLE pending_gates ADD COLUMN last_reminder_at INTEGER"
            )
            self._conn.execute(
                "ALTER TABLE pending_gates ADD COLUMN task_title TEXT"
            )
            self._conn.execute(_DDL_INBOUND)
        self._conn.execute(f"PRAGMA user_version = {_USER_VERSION};")

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
        task_title: "str | None" = None,
        created_at_ms: "int | None" = None,
    ) -> None:
        """插入 PENDING 行；同主键 PENDING 行只回填缺失消息列，绝不覆盖已发消息。

        已 DECIDED/EXPIRED 的同主键行：WHERE 守卫使 UPDATE 成为 no-op，绝不重开
        历史决策。Gate 打回后的新一轮卡片必须先调 archive_decided 归档，再走本
        插入（Task7 D10 多轮语义，见 _DDL_HISTORY 注释）。
        task_title 同样仅在缺省时回填（H-6 提醒卡片需要任务名）。
        """
        created_at = created_at_ms if created_at_ms is not None else now_ms()
        # source NOT NULL 但决策前无来源：插入空串占位，mark_decided 写真实来源。
        self._conn.execute(
            """
            INSERT INTO pending_gates(
                thread_id, gate, status, choice, decision, human_notes, source,
                card_message_id, media_message_id, open_chat_id,
                operator_open_id, created_at, decided_at, task_title)
            VALUES (?, ?, 'PENDING', NULL, NULL, '', '', ?, ?, ?, NULL, ?, NULL, ?)
            ON CONFLICT(thread_id, gate) DO UPDATE SET
                card_message_id = COALESCE(pending_gates.card_message_id,
                                           excluded.card_message_id),
                media_message_id = COALESCE(pending_gates.media_message_id,
                                            excluded.media_message_id),
                open_chat_id = COALESCE(pending_gates.open_chat_id,
                                        excluded.open_chat_id),
                task_title = COALESCE(pending_gates.task_title,
                                      excluded.task_title)
            WHERE pending_gates.status = 'PENDING'
            """,
            (thread_id, gate, card_message_id, media_message_id,
             open_chat_id, created_at, task_title),
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
        message_id: "str | None" = None,
        open_chat_id: "str | None" = None,
        received_at_ms: "int | None" = None,
    ) -> bool:
        """通道②：向 PENDING 行的 human_notes 追加一行（换行分隔）。

        H-1 收紧后的语义：
          - 只允许追加到 **PENDING** 行（DECIDED/EXPIRED/无行一律不落库，
            调用方改回歧义/失效提示）——决策后补话绝不改变落定结果；
          - message_id 给定时，与 inbound_messages 去重插入在**同一个
            BEGIN IMMEDIATE 事务**内完成：飞书重投/SDK 重放同一消息不再产生
            重复意见（幂等返回 False）；
          - 同时可 COALESCE 回填 open_chat_id。
        """
        received_at = received_at_ms if received_at_ms is not None else now_ms()
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            row = self._conn.execute(
                "SELECT status, human_notes, open_chat_id FROM pending_gates "
                "WHERE thread_id = ? AND gate = ?",
                (thread_id, gate),
            ).fetchone()
            if row is None or row["status"] != "PENDING":
                self._conn.execute("ROLLBACK;")
                return False
            if message_id is not None:
                dup = self._conn.execute(
                    "SELECT 1 FROM inbound_messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if dup is not None:
                    self._conn.execute("ROLLBACK;")
                    return False
            merged = row["human_notes"] or ""
            if merged:
                merged += "\n"
            merged += note_line
            if open_chat_id is not None and not row["open_chat_id"]:
                self._conn.execute(
                    """
                    UPDATE pending_gates
                       SET human_notes = ?, open_chat_id = COALESCE(open_chat_id, ?)
                     WHERE thread_id = ? AND gate = ? AND status = 'PENDING'
                    """,
                    (merged, open_chat_id, thread_id, gate),
                )
            else:
                self._conn.execute(
                    "UPDATE pending_gates SET human_notes = ? "
                    "WHERE thread_id = ? AND gate = ? AND status = 'PENDING'",
                    (merged, thread_id, gate),
                )
            if message_id is not None:
                try:
                    self._conn.execute(
                        "INSERT INTO inbound_messages"
                        "(message_id, received_at, thread_id, gate) "
                        "VALUES (?, ?, ?, ?)",
                        (message_id, received_at, thread_id, gate),
                    )
                except sqlite3.IntegrityError:
                    # BEGIN IMMEDIATE 已串行化，仍保留对极端并发重放的防御。
                    self._conn.execute("ROLLBACK;")
                    return False
            self._conn.execute("COMMIT;")
            return True
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    def archive_decided(self, thread_id: str, gate: int) -> bool:
        """Gate 多轮重发（Task7 D10）：把终态行（DECIDED/EXPIRED）整体移入
        pending_gates_history（仅追加），删主表行，给新一轮 PENDING 行腾位。

        PENDING 行绝不动（上一轮仍在等真人，重发由 upsert 的「不重发」分支
        处理）；无终态行时 no-op 返回 False。

        H-3：连接为 isolation_level=None（autocommit），``with self._conn:``
        在此模式下**不会**开启事务（评审 Python 3.10 实证 INSERT 即刻可见），
        必须显式 BEGIN IMMEDIATE 把 INSERT history + DELETE 主表行钉成一个原子
        步骤；中途异常 ROLLBACK，两行都不留下半截状态。
        """
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = self._conn.execute(
                f"""
                INSERT INTO pending_gates_history({_HISTORY_COLS}, archived_at)
                SELECT {_HISTORY_COLS}, ?
                  FROM pending_gates
                 WHERE thread_id = ? AND gate = ?
                   AND status IN ('DECIDED', 'EXPIRED')
                """,
                (now_ms(), thread_id, gate),
            )
            moved = cur.rowcount
            self._conn.execute(
                "DELETE FROM pending_gates WHERE thread_id = ? AND gate = ? "
                "AND status IN ('DECIDED', 'EXPIRED')",
                (thread_id, gate),
            )
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise
        return moved > 0

    def list_history(self, thread_id: str, gate: int) -> "list[dict[str, Any]]":
        cur = self._conn.execute(
            "SELECT * FROM pending_gates_history WHERE thread_id = ? AND gate = ? "
            "ORDER BY archived_at ASC",
            (thread_id, gate),
        )
        return [dict(r) for r in cur.fetchall()]

    def decision_exists(
        self, thread_id: str, gate: int, decision: str = "approve"
    ) -> bool:
        """H-5：主表 UNION history 查终态决策（重发守卫不得只看主表——已归档
        的 approve 同样要拦住二次推送）。"""
        row = self._conn.execute(
            """
            SELECT 1 FROM (
                SELECT decision, status FROM pending_gates
                 WHERE thread_id = ? AND gate = ?
                UNION ALL
                SELECT decision, status FROM pending_gates_history
                 WHERE thread_id = ? AND gate = ?
            )
             WHERE status = 'DECIDED' AND decision = ?
             LIMIT 1
            """,
            (thread_id, gate, thread_id, gate, decision),
        ).fetchone()
        return row is not None

    def claim_pending(
        self,
        thread_id: str,
        gate: int,
        *,
        task_title: "str | None" = None,
        created_at_ms: "int | None" = None,
    ) -> bool:
        """H-5：发送卡片前在事务内占位（claim）。

        主表同主键无任何行时插入空 PENDING 占位行并返回 True；已存在 PENDING
        行（另一个并发调用方正在发卡）或终态行时返回 False，调用方立即退出、
        绝不重复推视频。BEGIN IMMEDIATE 保证两个并发调用方被串行裁决。
        """
        created_at = created_at_ms if created_at_ms is not None else now_ms()
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            exists = self._conn.execute(
                "SELECT 1 FROM pending_gates WHERE thread_id = ? AND gate = ?",
                (thread_id, gate),
            ).fetchone()
            if exists is not None:
                self._conn.execute("ROLLBACK;")
                return False
            self._conn.execute(
                """
                INSERT INTO pending_gates(
                    thread_id, gate, status, choice, decision, human_notes, source,
                    card_message_id, media_message_id, open_chat_id,
                    operator_open_id, created_at, decided_at, task_title)
                VALUES (?, ?, 'PENDING', NULL, NULL, '', '', NULL, NULL, NULL,
                        NULL, ?, NULL, ?)
                """,
                (thread_id, gate, created_at, task_title),
            )
            self._conn.execute("COMMIT;")
            return True
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK;")
            return False
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    def get_reminder_state(
        self, thread_id: str, gate: int
    ) -> "tuple[int, int | None]":
        """H-6：返回 (已发提醒次数, 上次提醒 epoch ms)；无行时 (0, None)。"""
        row = self._conn.execute(
            "SELECT reminder_count, last_reminder_at FROM pending_gates "
            "WHERE thread_id = ? AND gate = ? AND status = 'PENDING'",
            (thread_id, gate),
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["reminder_count"] or 0), row["last_reminder_at"]

    def bump_reminder(
        self,
        thread_id: str,
        gate: int,
        *,
        at_ms: "int | None" = None,
    ) -> "int | None":
        """H-6：提醒计数 +1 并记录时间（重启后 8h/16h 节奏不重置）。

        返回递增后的计数；PENDING 行不存在（已决策/重发归档）时返回 None。
        """
        at = at_ms if at_ms is not None else now_ms()
        cur = self._conn.execute(
            "UPDATE pending_gates SET reminder_count = reminder_count + 1, "
            "last_reminder_at = ? "
            "WHERE thread_id = ? AND gate = ? AND status = 'PENDING'",
            (at, thread_id, gate),
        )
        if cur.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT reminder_count FROM pending_gates "
            "WHERE thread_id = ? AND gate = ?",
            (thread_id, gate),
        ).fetchone()
        return int(row["reminder_count"])

    def purge_thread_like(self, pattern: str) -> int:
        """H-17：同一事务删除主表 + history + inbound 中 thread_id LIKE 的行。

        仅供探针等**自有字面前缀**清理（替代 probes 里的裸 DELETE，后者漏清
        history/inbound）。ESCAPE '\\\\' 与历史语句一致，调用方须自行转义
        %/_ 通配符。返回主表删除行数。
        """
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = self._conn.execute(
                "DELETE FROM pending_gates WHERE thread_id LIKE ? ESCAPE ?",
                (pattern, _LIKE_ESCAPE),
            )
            deleted = cur.rowcount
            self._conn.execute(
                "DELETE FROM pending_gates_history "
                "WHERE thread_id LIKE ? ESCAPE ?",
                (pattern, _LIKE_ESCAPE),
            )
            self._conn.execute(
                "DELETE FROM inbound_messages WHERE thread_id LIKE ? ESCAPE ?",
                (pattern, _LIKE_ESCAPE),
            )
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise
        return deleted

    def mark_expired(self, thread_id: str, gate: int) -> bool:
        cur = self._conn.execute(
            "UPDATE pending_gates SET status = 'EXPIRED', decided_at = ? "
            "WHERE thread_id = ? AND gate = ? AND status = 'PENDING'",
            (now_ms(), thread_id, gate),
        )
        return cur.rowcount > 0
