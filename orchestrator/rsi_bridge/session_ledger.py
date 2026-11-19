"""RSI-S1 headless 会话账（S0 残尾③；``rsi.session_ledger/v1``）。

背景：S0 的 headless ``claude -p`` 会话消耗不在网关 ledger（供应商面外耗），
只以「价格 UNKNOWN 单列登记」留痕。S1 起每个触点会话起止各写一行，形成
append-only + 哈希链的会话账；结案报表**双面并列**：

* **网关面**（受账实呼）：``QuotaLedger`` 的 call 行（本模块只读计数，不改账）；
* **会话面**（外部消耗）：本账的 session_start 行数与 transcript usage 转录。

两面分列不混算（计划 S1-1 第 3 条 / AC-18）；usage 缺省记 ``UNKNOWN``，**不补零**。

纪律：
- append-only：只追加（``O_APPEND``），覆写/截断入口不存在；
- 哈希链：每行 ``prev_sha256`` = 上一行规范化字节的 sha256；首行 0*64；
- 半行拒：文件不以 ``\\n`` 结尾（崩溃半行）→ 读取即 ``HALF_LINE``；
- 会话帽：``hard_cap_sessions``（计划 §6 会话面 20）；超帽即 ``SESSION_CAP``。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir

__all__ = ["SESSION_SCHEMA", "DEFAULT_SESSION_CAP", "SessionLedgerError",
           "SessionLedger"]

SESSION_SCHEMA = "simpraxis.rsi_s1.session_ledger.v1"
DEFAULT_SESSION_CAP = 20
ZERO_HASH = "0" * 64


class SessionLedgerError(RuntimeError):
    """会话账结构/纪律违规（带机器码）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical(row: Mapping[str, Any]) -> bytes:
    return json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _row_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(row)).hexdigest()


class SessionLedger:
    """append-only 会话账；每次 start/finish 追加一行并维护哈希链。"""

    def __init__(self, path: Path | str, *,
                 hard_cap_sessions: int = DEFAULT_SESSION_CAP):
        self.path = Path(path)
        ensure_private_dir(self.path.parent, mode=0o700)
        if isinstance(hard_cap_sessions, bool) or hard_cap_sessions <= 0:
            raise SessionLedgerError("CAP_INVALID", f"{hard_cap_sessions!r}")
        self.hard_cap_sessions = int(hard_cap_sessions)
        self._rows = self._read_verified()

    # -- 读/验链 ---------------------------------------------------------
    def _read_verified(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise SessionLedgerError("HALF_LINE",
                                     f"{self.path} 末行不完整（崩溃半行），拒绝读取")
        rows: list[dict[str, Any]] = []
        prev = ZERO_HASH
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise SessionLedgerError(
                    "MALFORMED_ROW", f"第 {lineno} 行：{exc}") from None
            if not isinstance(row, dict):
                raise SessionLedgerError("MALFORMED_ROW",
                                         f"第 {lineno} 行不是对象")
            if row.get("schema") != SESSION_SCHEMA:
                raise SessionLedgerError("SCHEMA_MISMATCH", f"第 {lineno} 行")
            if row.get("prev_sha256") != prev:
                raise SessionLedgerError(
                    "CHAIN_BROKEN", f"第 {lineno} 行 prev 不等于链头")
            if int(row.get("seq", -1)) != len(rows) + 1:
                raise SessionLedgerError("SEQ_BROKEN", f"第 {lineno} 行 seq")
            prev = _row_hash(row)
            rows.append(row)
        return rows

    # -- 写 --------------------------------------------------------------
    def _append(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if self._rows:
            prev = _row_hash(self._rows[-1])
        else:
            prev = ZERO_HASH
        full = {"schema": SESSION_SCHEMA, "schema_version": 1,
                "seq": len(self._rows) + 1,
                **{k: v for k, v in row.items() if k != "seq"},
                "prev_sha256": prev, "ts_iso": utc_now_iso_z()}
        payload = _canonical(full)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            blob = payload + b"\n"
            while blob:  # 评审 LOW：短写循环，避免半行落盘
                written = os.write(fd, blob)
                blob = blob[written:]
        finally:
            os.close(fd)
        if new_file:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._rows.append(full)
        return full

    # -- 对外 API --------------------------------------------------------
    def start(self, *, session_id: str, model: str, task: str,
              touchpoint: str = "") -> dict[str, Any]:
        if not session_id or not model:
            raise SessionLedgerError("FIELD_EMPTY",
                                     "session_id/model 不能为空")
        if any(r.get("kind") == "session_start"
               and r.get("session_id") == session_id for r in self._rows):
            raise SessionLedgerError("DUPLICATE_START",
                                     f"session {session_id} 已登记开始（重复起点）")
        started = len(self.started())
        if started >= self.hard_cap_sessions:
            raise SessionLedgerError(
                "SESSION_CAP", f"会话数 {started} >= 硬帽 {self.hard_cap_sessions}")
        return self._append({"kind": "session_start", "session_id": session_id,
                             "model": model, "task": task,
                             "touchpoint": touchpoint})

    def finish(self, *, session_id: str, usage: "Mapping[str, Any] | None",
               exit_status: str) -> dict[str, Any]:
        started = [r for r in self._rows
                   if r.get("kind") == "session_start"
                   and r.get("session_id") == session_id]
        if not started:
            raise SessionLedgerError("UNKNOWN_SESSION",
                                     f"session {session_id} 无开始行")
        if any(r.get("kind") == "session_end"
               and r.get("session_id") == session_id for r in self._rows):
            raise SessionLedgerError("DUPLICATE_END",
                                     f"session {session_id} 已结束（拒绝二次结账）")
        if usage is None or not isinstance(usage, Mapping):
            usage_doc: dict[str, Any] = {"status": "UNKNOWN"}
        else:
            usage_doc = dict(usage)
            usage_doc.setdefault("status", "REPORTED")
        return self._append({"kind": "session_end", "session_id": session_id,
                             "usage": usage_doc, "exit_status": str(exit_status)})

    # -- 汇总 ------------------------------------------------------------
    def read(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows]

    def started(self) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("kind") == "session_start"]

    def finished(self) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("kind") == "session_end"]

    def summary(self, gateway_rows: "Sequence[Mapping[str, Any]] | None" = None
                ) -> dict[str, Any]:
        """双面口径：网关面与会话面**分列**（不求和、不互推）。

        ``gateway_rows`` 传 ``QuotaLedger`` 的 call 行（event=="call" 过滤由调用方
        负责或此处按需过滤）；不传则网关面记 ``None``（未知 ≠ 0）。
        """
        starts = self.started()
        ends = self.finished()
        open_ids = sorted({r["session_id"] for r in starts}
                          - {r["session_id"] for r in ends})
        reported: list[Mapping[str, Any]] = []
        unknown = 0
        for row in ends:
            usage = row.get("usage") or {}
            if usage.get("status") == "UNKNOWN":
                unknown += 1
            else:
                reported.append(usage)
        totals: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            values = [u.get(key) for u in reported if isinstance(u.get(key), int)]
            totals[key] = sum(values) if values else None
        gateway_face: dict[str, Any] = {"calls": None}
        if gateway_rows is not None:
            calls = [r for r in gateway_rows if r.get("event", "call") == "call"]
            gateway_face = {"calls": len(calls),
                            "models": sorted({str(r.get("model")) for r in calls
                                              if r.get("model")})}
        return {
            "schema": f"{SESSION_SCHEMA}.summary",
            "generated_at_utc": utc_now_iso_z(),
            "session_face": {
                "sessions_started": len(starts),
                "sessions_finished": len(ends),
                "open_sessions": open_ids,
                "hard_cap_sessions": self.hard_cap_sessions,
                "usage_reported": len(reported),
                "usage_unknown": unknown,
                "usage_totals": totals,
                "price": "UNKNOWN",
            },
            "gateway_face": gateway_face,
            "note": ("两面分列：网关面 = 入 ledger 的实呼；会话面 = headless 外部消耗。"
                     "任何相加口径都是错的（S0 gaps 教训）。"),
        }
