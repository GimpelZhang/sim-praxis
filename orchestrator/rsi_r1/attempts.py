"""RSI-R1.2 T3：attempt 生命周期与唯一报告（Plan §3.2；AC25/26）。

三条纪律：

1. **每一次尝试都有唯一报告**：``CLI`` 通过路径/身份验证后**立即**原子写
   ``started`` 报告，然后再过费用/LOCK/paused/tokenizer 闸；提前阻塞的尝试
   同样是零 POST 的完整报告，不是没发生。
2. **完成判定看终态证据**：正常结束先写 ``completed`` 握手件并引用终态报告的
   sha256；崩溃/异常由 ``__exit__`` 如实分类（认识的宿主错误保留原错误码，
   不认识的异常一律 ``INSTRUMENT_FAILURE``）；``SIGKILL`` 由 watcher 生成
   crash 补记，**不伪造** worker 完成时间。
3. **历史不可覆盖**：报告按 ``attempts/<lane>/<attempt_id>/report.json`` 分目录
   留档；``attempts/<lane>/latest.json`` 只是「最新报告」的 hash 指针，关闭
   遍历全部 attempt 的报告集合，不拿早先 0 呼报告覆盖后续有呼失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ATTEMPTS_SUBDIR = "attempts"
REPORT_SCHEMA = "simpraxis.rsi_r1.attempt_report.v1"
POINTER_SCHEMA = "simpraxis.rsi_r1.attempt_pointer.v1"
COMPLETION_SCHEMA = "simpraxis.rsi_r1.worker_completed.v1"
SCHEMA_VERSION = 1

STATUS_STARTED = "started"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TERMINATED = "terminated"
STATUS_INSTRUMENT_FAILURE = "instrument_failure"

CODE_INSTRUMENT_FAILURE = "INSTRUMENT_FAILURE"

CLEAN_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_TERMINATED)


class AttemptError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class AttemptTerminated(BaseException):
    """SIGTERM/SIGINT 的受控退出信号：走 ``__exit__`` 写终态报告后再传播。

    继承 ``BaseException``：worker 内部的 ``except Exception`` 不得把它吞掉。
    """

    code = "TERMINATED_BY_SIGNAL"

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        super().__init__(f"TERMINATED_BY_SIGNAL: {self.signum}")


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> "dict[str, Any] | None":
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def process_start_identity(pid: "int | None" = None) -> str:
    """``/proc/<pid>/stat`` 第 22 字段（进程启动 tick）：区分 pid 复用。"""
    target = int(pid if pid is not None else os.getpid())
    try:
        raw = Path(f"/proc/{target}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    # comm 字段可能含空格/括号，按最后一个 ')' 切
    rest = raw[raw.rfind(")") + 1:].split()
    return rest[19] if len(rest) > 19 else ""


def ledger_row_count(path: "Path | str") -> int:
    p = Path(path)
    if not p.is_file():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines()
               if line.strip())


def attempts_dir(ctx: Any, lane: "str | None" = None) -> Path:
    rel = ATTEMPTS_SUBDIR if lane is None else f"{ATTEMPTS_SUBDIR}/{lane}"
    return Path(ctx.relative(rel))


def list_attempt_ids(ctx: Any, lane: str) -> "list[str]":
    base = attempts_dir(ctx, lane)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and (p / "report.json").is_file())


def next_attempt_id(ctx: Any, lane: str) -> str:
    existing = list_attempt_ids(ctx, lane)
    used = 0
    for name in existing:
        tail = name.rsplit("-", 1)[-1]
        if tail.isdigit():
            used = max(used, int(tail))
    return f"att-{used + 1:03d}"


def attempt_reports(ctx: Any, lane: "str | None" = None) -> "list[dict[str, Any]]":
    """关闭遍历：全部 attempt 的**终态**报告（started 空壳也保留，明标未完成）。"""
    if lane is not None:
        lanes = [lane]
    else:
        base = attempts_dir(ctx)
        lanes = sorted(p.name for p in base.iterdir() if p.is_dir()) \
            if base.is_dir() else []
    docs: "list[dict[str, Any]]" = []
    for name in lanes:
        for aid in list_attempt_ids(ctx, name):
            doc = _read_json(attempts_dir(ctx, name) / aid / "report.json")
            if doc is not None:
                docs.append(doc)
    return docs


def latest_pointer(ctx: Any, lane: str) -> "dict[str, Any] | None":
    return _read_json(attempts_dir(ctx, lane) / "latest.json")


def _known_exception(exc: BaseException) -> "str | None":
    """认识的宿主错误：带 ``code`` 且来自本仓模块时如实保留错误码。

    其余（KeyError/TypeError/第三方异常）一律 ``INSTRUMENT_FAILURE``——
    不把未知异常伪装成协议失败。
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code.strip():
        return None
    module = type(exc).__module__
    if not module.startswith("orchestrator."):
        return None
    return code


class AttemptSession:
    """一次尝试的上下文管理器：started 报告 → 工作 → 终态报告 + 握手件。"""

    def __init__(self, ctx: Any, lane: str, profile: Any = None, *,
                 attempt_id: "str | None" = None,
                 clock: Callable[[], str] = utc_now_z,
                 ledger_path: "Path | str | None" = None,
                 root_state: "Callable[[], str] | None" = None,
                 lock_sha256: str = "",
                 handshake: bool = True) -> None:
        self.ctx = ctx
        self.lane = str(lane)
        self.profile = profile
        self.attempt_id = attempt_id
        self.clock = clock
        self.ledger_path = ledger_path
        self.root_state = root_state
        self.lock_sha256 = str(lock_sha256 or "")
        self.handshake = bool(handshake)
        self.doc: "dict[str, Any]" = {}
        self.report_path: "Path | None" = None
        self.finished = False
        self._result: "dict[str, Any] | None" = None
        self._reason = ""
        self._teardown: "dict[str, Any] | None" = None
        self._old_handlers: "dict[int, Any]" = {}

    # -- 路径 ------------------------------------------------------------
    @property
    def _dir_rel(self) -> str:
        return f"{ATTEMPTS_SUBDIR}/{self.lane}/{self.attempt_id}"

    @property
    def report_relpath(self) -> str:
        return f"{self._dir_rel}/report.json"

    # -- 生命周期 --------------------------------------------------------
    def __enter__(self) -> "AttemptSession":
        if self.profile is not None:
            lanes = tuple(getattr(self.profile, "lane_order", ()) or ())
            if lanes and self.lane not in lanes:
                raise AttemptError("LANE_UNKNOWN", repr(self.lane))
        elif not self.lane.strip():
            raise AttemptError("LANE_UNKNOWN", repr(self.lane))
        if self.attempt_id is None:
            self.attempt_id = next_attempt_id(self.ctx, self.lane)
        if not _safe_component(self.attempt_id):
            raise AttemptError("ATTEMPT_ID_INVALID", repr(self.attempt_id))
        self.posts_before = self._ledger_count()
        started = self.clock()
        self.doc = {
            "schema": REPORT_SCHEMA, "schema_version": SCHEMA_VERSION,
            "attempt_id": self.attempt_id, "lane": self.lane,
            "envelope_id": str(getattr(self.ctx, "envelope_id", "") or ""),
            "profile_id": str(getattr(self.profile, "profile_id", "") or ""),
            "status": STATUS_STARTED,
            "started_at_utc": started, "ended_at_utc": None,
            "pid": os.getpid(),
            "process_start": process_start_identity(),
            "root": {"before": self._root(), "after": None},
            "lock": {"sha256": self.lock_sha256},
            "request_refs": [], "actions": [], "result": {},
            "posts": {"before": self.posts_before, "after": None,
                      "delta": None, "ledger_rows_after": None},
            "first_error": None, "recovery": None,
            "exception_type": "", "reason": "",
            "teardown": {"recorded": False, "reason": "not_finished"},
            "worker_authored": True,
        }
        self._write()
        self._install_signal_handlers()
        return self

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT → :class:`AttemptTerminated`，让 ``__exit__`` 写报告。

        只在主线程安装；SIGKILL 不可捕获，由 watcher 写 crash 补记。
        旧 handler 在 ``__exit__`` 原样恢复（不改变进程其余语义）。
        """
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _raise_terminated)
            except (OSError, ValueError):
                self._old_handlers.pop(sig, None)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
        self._old_handlers.clear()

    def record_request(self, ref: str) -> None:
        self.doc["request_refs"].append(str(ref))

    def record_action(self, action: Mapping[str, Any]) -> None:
        self.doc["actions"].append(dict(action))

    def note_first_error(self, doc: "Mapping[str, Any] | None") -> None:
        if doc is not None:
            self.doc["first_error"] = dict(doc)

    def note_recovery(self, doc: "Mapping[str, Any] | None") -> None:
        if doc is not None:
            self.doc["recovery"] = dict(doc)

    def finish(self, result: "Mapping[str, Any] | None" = None, *,
               reason: str = "",
               teardown: "Mapping[str, Any] | None" = None) -> None:
        self.finished = True
        self._result = dict(result or {})
        self._reason = str(reason or "")
        if teardown is not None:
            self._teardown = dict(teardown)

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 无论成败都写终态报告；异常照常向上传播（返回 False）。
        self._restore_signal_handlers()
        self.doc["ended_at_utc"] = self.clock()
        self.doc["root"]["after"] = self._root()
        after = self._ledger_count()
        self.doc["posts"]["after"] = after
        self.doc["posts"]["delta"] = after - int(self.posts_before)
        self.doc["posts"]["ledger_rows_after"] = after
        if exc is not None:
            code = _known_exception(exc)
            self.doc["exception_type"] = f"{type(exc).__module__}." \
                                         f"{type(exc).__qualname__}"
            if code is not None:
                self.doc["status"] = STATUS_TERMINATED
                self.doc["reason"] = f"{code}: {exc}"
                self.doc["result"] = {"ok": False, "code": code}
            else:
                self.doc["status"] = STATUS_INSTRUMENT_FAILURE
                self.doc["reason"] = repr(exc)
                self.doc["result"] = {"ok": False,
                                      "code": CODE_INSTRUMENT_FAILURE}
        elif not self.finished:
            self.doc["status"] = STATUS_TERMINATED
            self.doc["reason"] = self._reason or "attempt 结束但未调用 finish"
        else:
            result = self._result or {}
            ok = bool(result.get("ok", result.get("verdict") == "PASS"))
            self.doc["status"] = STATUS_COMPLETED if ok else STATUS_FAILED
            self.doc["reason"] = self._reason or str(result.get("reason") or "")
            self.doc["result"] = result
        self.doc["teardown"] = self._teardown or {
            "recorded": True,
            "reason": "completed" if exc is None else "exception"}
        self._write()
        self._write_pointer()
        if self.handshake and self.doc["status"] in CLEAN_STATUSES:
            self._write_completion()
        return False

    # -- 内部 ------------------------------------------------------------
    def _root(self) -> "str | None":
        if self.root_state is None:
            return None
        try:
            return str(self.root_state())
        except Exception:  # noqa: BLE001 —— 读不到 root 就是缺证据，不编造
            return None

    def _ledger_count(self) -> int:
        path = self.ledger_path
        if path is None:
            path = Path(self.ctx.relative("quota/ledger.jsonl"))
        return ledger_row_count(path)

    def _write(self) -> None:
        self.report_path = Path(self.ctx.write_json(self.report_relpath, self.doc))

    def _write_pointer(self) -> None:
        # 文件字节 sha 与内存文档 sha 的一致性由写后读回判定
        sha = _sha256_file(self.report_path) if self.report_path else ""
        self.ctx.write_json(f"{ATTEMPTS_SUBDIR}/{self.lane}/latest.json", {
            "schema": POINTER_SCHEMA, "schema_version": SCHEMA_VERSION,
            "lane": self.lane, "attempt_id": self.attempt_id,
            "report_relpath": self.report_relpath,
            "report_sha256": sha, "updated_at_utc": self.clock()})

    def _write_completion(self) -> None:
        sha = _sha256_file(self.report_path) if self.report_path else ""
        self.ctx.write_json("state/worker_completed.json", {
            "schema": COMPLETION_SCHEMA, "schema_version": SCHEMA_VERSION,
            "attempt_id": self.attempt_id, "lane": self.lane,
            "envelope_id": self.doc["envelope_id"], "pid": os.getpid(),
            "process_start": process_start_identity(),
            "report_relpath": self.report_relpath,
            "report_sha256": sha,
            "status": self.doc["status"],
            "completed_at_utc": self.doc["ended_at_utc"]})


def _raise_terminated(signum: int, frame: Any) -> None:  # noqa: ARG001
    raise AttemptTerminated(signum)


def _safe_component(value: str) -> bool:
    text = str(value)
    return bool(text) and text not in (".", "..") and "/" not in text \
        and "\\" not in text and "\x00" not in text


__all__ = [
    "ATTEMPTS_SUBDIR", "AttemptError", "AttemptSession", "AttemptTerminated",
    "CODE_INSTRUMENT_FAILURE",
    "COMPLETION_SCHEMA", "CLEAN_STATUSES", "POINTER_SCHEMA", "REPORT_SCHEMA",
    "STATUS_COMPLETED", "STATUS_FAILED", "STATUS_INSTRUMENT_FAILURE",
    "STATUS_STARTED", "STATUS_TERMINATED", "attempt_reports", "attempts_dir",
    "latest_pointer", "ledger_row_count", "list_attempt_ids", "next_attempt_id",
    "process_start_identity", "utc_now_z",
]
