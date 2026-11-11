"""RSI-R1 T5：进程监护与暂停面（Plan §8.4，AC24）。

设计要点（与 ``orchestrator/common/watched.py`` 的旧实现不同，本模块只服务
R1 campaign，语义按 Plan §8.4 重写；旧模块保持不动）：

* worker 每 **60 秒**写一次心跳（:class:`Heartbeat`）；心跳含 pid、UTC 时间、
  阶段与可选的进度（批次逐 run 心跳）。
* 监护者每个 tick 读心跳并分类：
  - ``ALIVE``：有心跳且 pid 活着、未超静默窗；
  - ``DEATH``：心跳里的 pid 已死 → **立即**记录（不等静默窗）；
  - ``SILENCE``：距最后心跳 > **5 分钟** → 告警并暂停新 dispatch；
  - ``MISSING``：从未见心跳（记事件，但从首次观察起算静默）。
* 120 秒请求硬截止是 **transport 层**的独立闸，和 5 分钟静默互不替代——本模块
  不碰发网，也不因静默去 kill 任何进程（**不擅杀他方进程**）。
* 监护者自己也有可观测的 pid/心跳文件，``stop()``/异常/SIGINT 都会写 teardown；
  重启后的新监护者能看到旧暂停并**保留**它（不静默清空预算/暂停态）。
* 暂停面 ``state/pause.json`` 是唯一的 dispatch 闸：准入层（assembly）读它；
  恢复必须显式 ``resume`` 并留痕。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

HEARTBEAT_INTERVAL_S = 60
SILENCE_LIMIT_S = 300

STATE_HEARTBEAT = "worker_heartbeat.json"
STATE_WATCHER = "watchdog.json"
STATE_TEARDOWN = "watchdog_teardown.json"
STATE_PAUSE = "pause.json"
EVENTS = "watchdog_events.jsonl"

ALIVE = "ALIVE"
DEATH = "DEATH"
SILENCE = "SILENCE"
MISSING = "MISSING"


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


def pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` 探活；权限不足视为活着（不擅自杀/不误判）。"""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _atomic_json(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, sort_keys=True, indent=1,
                  allow_nan=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _append_jsonl(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      allow_nan=False) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> "dict[str, Any] | None":
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------------------
# worker 心跳
# ---------------------------------------------------------------------------

class Heartbeat:
    """worker 侧心跳写入器：每 60 秒一次，批次内逐 run 也走这里。"""

    def __init__(self, state_dir: "Path | str", *, owner: str = "worker",
                 now: Callable[[], str] = utc_now_z) -> None:
        self.state_dir = Path(state_dir)
        self.owner = owner
        self.now = now
        self.path = self.state_dir / STATE_HEARTBEAT
        self._n = 0

    def beat(self, *, phase: str = "", detail: "Mapping[str, Any] | None" = None
             ) -> "dict[str, Any]":
        self._n += 1
        doc = {"schema": "simpraxis.rsi_r1.heartbeat.v1", "schema_version": 1,
               "owner": self.owner, "pid": os.getpid(),
               "ts_iso": self.now(), "seq": self._n, "phase": str(phase),
               "detail": dict(detail or {})}
        _atomic_json(self.path, doc)
        return doc


# ---------------------------------------------------------------------------
# 暂停面
# ---------------------------------------------------------------------------

def read_pause(state_dir: "Path | str") -> "dict[str, Any] | None":
    """当前暂停件（不存在即 None）。准入层只认这个文件，不认内存标志。"""
    return _read_json(Path(state_dir) / STATE_PAUSE)


def pause_dispatch(state_dir: "Path | str", *, reason: str, source: str,
                   evidence: "Mapping[str, Any] | None" = None,
                   now: Callable[[], str] = utc_now_z) -> "dict[str, Any]":
    """写暂停件（幂等：已有暂停不覆盖原因，只追加事件）。"""
    state = Path(state_dir)
    existing = read_pause(state)
    if existing is not None:
        _append_jsonl(state / EVENTS, {
            "ts_iso": now(), "event": "pause_repeated", "source": source,
            "reason": reason, "existing_reason": existing.get("reason")})
        return existing
    doc = {"schema": "simpraxis.rsi_r1.pause.v1", "schema_version": 1,
           "paused": True, "reason": str(reason), "source": str(source),
           "ts_iso": now(), "evidence": dict(evidence or {})}
    _atomic_json(state / STATE_PAUSE, doc)
    _append_jsonl(state / EVENTS, {"ts_iso": doc["ts_iso"], "event": "paused",
                                   "source": source, "reason": reason})
    return doc


def resume_dispatch(state_dir: "Path | str", *, actor: str, reason: str,
                    now: Callable[[], str] = utc_now_z) -> "dict[str, Any]":
    """显式恢复（必须留痕；没暂停时返回 not_paused 而不造新文件）。"""
    state = Path(state_dir)
    existing = read_pause(state)
    if existing is None:
        return {"resumed": False, "detail": "not_paused"}
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("resume 必须记录 actor")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("resume 必须记录 reason")
    path = state / STATE_PAUSE
    history = state / "pause_history.jsonl"
    _append_jsonl(history, {"ts_iso": now(), "event": "resumed", "actor": actor,
                            "reason": reason, "paused_at": existing.get("ts_iso"),
                            "paused_reason": existing.get("reason")})
    path.unlink()
    _append_jsonl(state / EVENTS, {"ts_iso": now(), "event": "resumed",
                                   "actor": actor, "reason": reason})
    return {"resumed": True, "actor": actor}


# ---------------------------------------------------------------------------
# 监护者
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchConfig:
    state_dir: Path
    silence_limit_s: float = SILENCE_LIMIT_S
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    poll_s: float = 1.0
    now: Callable[[], str] = utc_now_z
    #: 静默/死亡时是否写暂停件。AC24 与生产都要求 True；测试可用 False
    #: 单独验证「只告警不暂停」不是本实现的语义，故默认并强制 True。
    pause_on_silence: bool = True


@dataclass
class _WatchState:
    ticks: int = 0
    first_seen_utc: str = ""
    last_state: "str | None" = None
    observed_heartbeats: int = 0


class WatcherAlreadyRunning(RuntimeError):
    pass


class Watcher:
    """外部监护者：可观测 pid/心跳、可被回收、重启保留暂停态。"""

    def __init__(self, config: WatchConfig) -> None:
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.evidence_path = self.state_dir / EVENTS
        self.watcher_path = self.state_dir / STATE_WATCHER
        self.teardown_path = self.state_dir / STATE_TEARDOWN
        self._state = _WatchState()
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._teardown_written = False
        self._tick_error: "BaseException | None" = None

    # ---- 分类 ----

    def tick(self) -> "dict[str, Any]":
        with self._lock:
            return self._tick_locked()

    def _tick_locked(self) -> "dict[str, Any]":
        now_z = self.config.now()
        now = parse_z(now_z)
        self._state.ticks += 1
        if not self._state.first_seen_utc:
            self._state.first_seen_utc = now_z
        beat = _read_json(self.state_dir / STATE_HEARTBEAT)
        outcome = ALIVE
        detail: "dict[str, Any]" = {}
        last_beat_ts = None
        if beat is None:
            outcome = MISSING
            age = (now - parse_z(self._state.first_seen_utc)).total_seconds()
            detail["since_first_seen_s"] = age
        else:
            self._state.observed_heartbeats += 1
            last_beat_ts = str(beat.get("ts_iso") or "")
            try:
                age = (now - parse_z(last_beat_ts)).total_seconds()
            except (ValueError, TypeError):
                age = float("inf")
            detail.update({"pid": beat.get("pid"), "last_beat_utc": last_beat_ts,
                           "age_s": age, "phase": beat.get("phase")})
            pid = beat.get("pid")
            if isinstance(pid, int) and not pid_alive(pid):
                outcome = DEATH
            elif outcome != DEATH and age > float(self.config.silence_limit_s):
                outcome = SILENCE
            if outcome == MISSING:
                outcome = ALIVE if age <= float(self.config.silence_limit_s) else SILENCE

        if beat is None and outcome == MISSING and \
                detail.get("since_first_seen_s", 0.0) > float(self.config.silence_limit_s):
            outcome = SILENCE

        if outcome != self._state.last_state or outcome in (DEATH, SILENCE):
            _append_jsonl(self.evidence_path, {
                "ts_iso": now_z, "event": outcome.lower(), "detail": detail,
                "ticks": self._state.ticks})
        if outcome in (DEATH, SILENCE) and self.config.pause_on_silence:
            pause_dispatch(self.state_dir, reason=outcome.lower(),
                           source="watchdog", evidence=detail, now=self.config.now)
        self._state.last_state = outcome
        # 监护自身可观测：pid + 心跳 + 分类。
        _atomic_json(self.watcher_path, {
            "schema": "simpraxis.rsi_r1.watchdog_state.v1", "schema_version": 1,
            "pid": os.getpid(), "ts_iso": now_z, "ticks": self._state.ticks,
            "state": outcome, "detail": detail,
            "heartbeat_interval_s": self.config.heartbeat_interval_s,
            "silence_limit_s": self.config.silence_limit_s,
            "first_seen_utc": self._state.first_seen_utc})
        return {"state": outcome, "detail": detail, "ticks": self._state.ticks,
                "paused": read_pause(self.state_dir) is not None}

    # ---- 生命周期 ----

    def start(self) -> "Watcher":
        existing = _read_json(self.watcher_path)
        if existing is not None:
            pid = existing.get("pid")
            if isinstance(pid, int) and pid != os.getpid() and pid_alive(pid):
                raise WatcherAlreadyRunning(
                    f"已有监护者 pid={pid} 存活；拒绝第二个监护者")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rsi-r1-watchdog",
                                        daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        try:
            self.tick()
            while not self._stop.wait(self.config.poll_s):
                self.tick()
        except BaseException as exc:  # noqa: BLE001 —— 任何异常都必须 teardown
            self._tick_error = exc
            self._teardown("exception", detail=repr(exc))
            raise

    def stop(self, *, reason: str = "stopped") -> "dict[str, Any]":
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(5.0, self.config.poll_s * 3))
        teardown = self._teardown(reason)
        self._thread = None
        return teardown

    def run(self, *, max_ticks: "int | None" = None) -> "dict[str, Any]":
        """前台监护循环：SIGINT/异常/正常完成都写 teardown。"""
        try:
            while max_ticks is None or self._state.ticks < max_ticks:
                self.tick()
                if max_ticks is not None and self._state.ticks >= max_ticks:
                    break
                time.sleep(self.config.poll_s)
        except KeyboardInterrupt:
            return self._teardown("sigint")
        except BaseException as exc:  # noqa: BLE001
            return self._teardown("exception", detail=repr(exc))
        return self._teardown("normal")

    def _teardown(self, reason: str, detail: str = "") -> "dict[str, Any]":
        if self._teardown_written:
            return _read_json(self.teardown_path) or {}
        doc = {"schema": "simpraxis.rsi_r1.watchdog_teardown.v1",
               "schema_version": 1, "pid": os.getpid(),
               "ts_iso": self.config.now(), "reason": reason,
               "detail": detail, "ticks": self._state.ticks,
               "last_state": self._state.last_state,
               "pause_preserved": read_pause(self.state_dir) is not None}
        _atomic_json(self.teardown_path, doc)
        _append_jsonl(self.evidence_path, {"ts_iso": doc["ts_iso"],
                                           "event": "teardown",
                                           "reason": reason, "detail": detail})
        self._teardown_written = True
        return doc


def watcher_state(state_dir: "Path | str") -> "dict[str, Any] | None":
    return _read_json(Path(state_dir) / STATE_WATCHER)


def teardown_doc(state_dir: "Path | str") -> "dict[str, Any] | None":
    return _read_json(Path(state_dir) / STATE_TEARDOWN)
