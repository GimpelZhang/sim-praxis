"""RSI-R1 T5：进程监护/心跳/暂停面（Plan §8.4，AC24）。

全部离线：临时目录 + 注入时钟；死亡用**自己的**子进程模拟并显式回收
（不残留后台进程），静默用伪造的旧心跳时间模拟。绝不触碰他方进程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import watchdog as W


class Clock:
    def __init__(self, t: str = "2026-11-11T00:00:00Z") -> None:
        self.t = t

    def set(self, t: str) -> "Clock":
        self.t = t
        return self

    def __call__(self) -> str:
        return self.t


def _cfg(tmp_path: Path, clock: Clock, **kw) -> W.WatchConfig:
    return W.WatchConfig(state_dir=tmp_path, now=clock, poll_s=0.01, **kw)


def _beat(tmp_path: Path, *, pid: int, ts: str, phase: str = "run") -> None:
    (tmp_path / W.STATE_HEARTBEAT).write_text(json.dumps({
        "schema": "simpraxis.rsi_r1.heartbeat.v1", "schema_version": 1,
        "owner": "worker", "pid": pid, "ts_iso": ts, "seq": 1,
        "phase": phase, "detail": {}}), encoding="utf-8")


def _events(tmp_path: Path) -> "list[dict]":
    p = tmp_path / W.EVENTS
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# 常量与心跳
# ---------------------------------------------------------------------------

def test_contract_constants():
    assert W.HEARTBEAT_INTERVAL_S == 60
    assert W.SILENCE_LIMIT_S == 300


def test_heartbeat_writes_observable_doc(tmp_path):
    clock = Clock("2026-11-11T09:00:00Z")
    hb = W.Heartbeat(tmp_path, now=clock)
    doc = hb.beat(phase="batch", detail={"run_id": "r-01", "i": 1, "n": 32})
    path = tmp_path / W.STATE_HEARTBEAT
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == doc
    assert doc["pid"] == os.getpid()
    assert doc["ts_iso"] == "2026-11-11T09:00:00Z"
    assert doc["phase"] == "batch"
    assert doc["detail"] == {"run_id": "r-01", "i": 1, "n": 32}
    assert hb.beat(phase="idle")["seq"] == 2


# ---------------------------------------------------------------------------
# 分类：ALIVE / MISSING / SILENCE / DEATH
# ---------------------------------------------------------------------------

def test_alive_when_fresh_heartbeat_of_live_pid(tmp_path):
    clock = Clock("2026-11-11T10:00:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T09:59:30Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    out = w.tick()
    assert out["state"] == W.ALIVE
    assert out["paused"] is False
    assert not (tmp_path / W.STATE_PAUSE).exists()
    state = W.watcher_state(tmp_path)
    assert state["pid"] == os.getpid()
    assert state["ticks"] == 1
    assert state["state"] == W.ALIVE


def test_missing_before_first_heartbeat_records_but_does_not_pause(tmp_path):
    clock = Clock("2026-11-11T10:00:00Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    out = w.tick()
    assert out["state"] == W.MISSING
    assert out["paused"] is False
    assert [e["event"] for e in _events(tmp_path)] == ["missing"]


def test_silence_after_five_minutes_pauses_and_alerts(tmp_path):
    clock = Clock("2026-11-11T10:06:00Z")
    # pid 活着（本进程），只是心跳停在 6 分钟前 —— 典型「进程卡死」。
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T10:00:00Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    out = w.tick()
    assert out["state"] == W.SILENCE
    assert out["paused"] is True
    pause = W.read_pause(tmp_path)
    assert pause["reason"] == "silence" and pause["source"] == "watchdog"
    assert pause["evidence"]["age_s"] == 360.0
    kinds = [e for e in _events(tmp_path) if e["event"] == "silence"]
    assert kinds and kinds[0]["detail"]["age_s"] == 360.0


def test_silence_boundary_is_strictly_greater_than_limit(tmp_path):
    clock = Clock("2026-11-11T10:05:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T10:00:00Z")  # 恰 300s
    w = W.Watcher(_cfg(tmp_path, clock))
    assert w.tick()["state"] == W.ALIVE


def test_silence_grows_without_new_heartbeat_and_pause_is_idempotent(tmp_path):
    clock = Clock("2026-11-11T10:06:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T10:00:00Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    w.tick()
    first = W.read_pause(tmp_path)
    clock.set("2026-11-11T10:07:00Z")
    out = w.tick()
    assert out["state"] == W.SILENCE
    assert W.read_pause(tmp_path) == first  # 不覆盖首次暂停原因
    assert any(e["event"] == "pause_repeated" for e in _events(tmp_path))


def test_death_of_own_child_recorded_immediately(tmp_path):
    # 自己的子进程：先跑起来写进心跳，退出后由**本测试** wait() 回收。
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(0.05)"])
    try:
        _beat(tmp_path, pid=child.pid, ts="2026-11-11T10:59:59Z")
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    clock = Clock("2026-11-11T11:00:00Z")  # 仅 1 秒后：静默窗远未到
    w = W.Watcher(_cfg(tmp_path, clock))
    out = w.tick()
    assert out["state"] == W.DEATH
    assert out["paused"] is True
    assert W.read_pause(tmp_path)["reason"] == "death"
    death = [e for e in _events(tmp_path) if e["event"] == "death"]
    assert death and death[0]["detail"]["pid"] == child.pid


def test_never_signals_processes_on_death(tmp_path):
    """AC24「不擅杀他方进程」：源码只允许 ``os.kill(pid, 0)`` 探活。"""
    src = Path(W.__file__).read_text(encoding="utf-8")
    assert "SIGKILL" not in src and "SIGTERM" not in src
    assert src.count("os.kill(") == 1
    assert "os.kill(int(pid), 0)" in src


# ---------------------------------------------------------------------------
# 监护自身可观测 + 回收（teardown）
# ---------------------------------------------------------------------------

def test_watcher_thread_records_heartbeat_and_reaps_on_stop(tmp_path):
    clock = Clock("2026-11-11T12:00:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T12:00:00Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    w.start()
    try:
        for _ in range(200):
            if W.watcher_state(tmp_path) is not None:
                break
            time.sleep(0.01)
        assert W.watcher_state(tmp_path)["pid"] == os.getpid()
    finally:
        teardown = w.stop(reason="unit-test")
    assert w._thread is None
    assert teardown["reason"] == "unit-test"
    assert teardown["pid"] == os.getpid()
    assert W.teardown_doc(tmp_path)["reason"] == "unit-test"
    assert any(e["event"] == "teardown" for e in _events(tmp_path))


def test_teardown_on_normal_completion(tmp_path):
    clock = Clock("2026-11-11T12:00:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T12:00:00Z")
    w = W.Watcher(_cfg(tmp_path, clock))
    doc = w.run(max_ticks=2)
    assert doc["reason"] == "normal"
    assert doc["ticks"] == 2


@pytest.mark.parametrize("exc,reason", [
    (KeyboardInterrupt(), "sigint"),
    (RuntimeError("boom"), "exception"),
])
def test_teardown_on_sigint_and_exception(tmp_path, exc, reason):
    class Boom(W.Watcher):
        def tick(self):
            raise self._exc

    w = Boom(_cfg(tmp_path, Clock()))
    w._exc = exc
    doc = w.run(max_ticks=5)
    assert doc["reason"] == reason
    assert doc["ticks"] == 0
    assert W.teardown_doc(tmp_path)["reason"] == reason


def test_second_live_watcher_refused(tmp_path, monkeypatch):
    w = W.Watcher(_cfg(tmp_path, Clock()))
    (tmp_path / W.STATE_WATCHER).write_text(json.dumps(
        {"pid": 999999, "ticks": 3}), encoding="utf-8")
    monkeypatch.setattr(W, "pid_alive", lambda pid: True)
    with pytest.raises(W.WatcherAlreadyRunning):
        w.start()


def test_restart_after_stop_preserves_pause(tmp_path):
    """模拟监护者重启：旧暂停**不得**被新监护者静默清空。"""
    clock = Clock("2026-11-11T13:06:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T13:00:00Z")
    first = W.Watcher(_cfg(tmp_path, clock))
    assert first.tick()["state"] == W.SILENCE
    first.stop(reason="crash-sim")
    assert W.read_pause(tmp_path) is not None

    clock.set("2026-11-11T13:10:00Z")
    _beat(tmp_path, pid=os.getpid(), ts="2026-11-11T13:09:59Z")  # worker 恢复心跳
    second = W.Watcher(_cfg(tmp_path, clock))
    out = second.tick()
    assert out["state"] == W.ALIVE
    assert out["paused"] is True                      # 暂停仍在
    assert W.teardown_doc(tmp_path)["reason"] == "crash-sim"
    second.stop(reason="unit-test")


# ---------------------------------------------------------------------------
# 暂停面：准入闸 + 显式恢复留痕
# ---------------------------------------------------------------------------

def test_pause_gate_and_explicit_resume(tmp_path):
    clock = Clock("2026-11-11T14:00:00Z")
    assert W.read_pause(tmp_path) is None
    W.pause_dispatch(tmp_path, reason="manual", source="user", now=clock)
    assert W.read_pause(tmp_path)["paused"] is True
    with pytest.raises(ValueError):
        W.resume_dispatch(tmp_path, actor="", reason="x", now=clock)
    with pytest.raises(ValueError):
        W.resume_dispatch(tmp_path, actor="user", reason=" ", now=clock)
    out = W.resume_dispatch(tmp_path, actor="user", reason="已修复心跳",
                            now=clock)
    assert out["resumed"] is True
    assert W.read_pause(tmp_path) is None
    hist = (tmp_path / "pause_history.jsonl").read_text(encoding="utf-8")
    assert json.loads(hist.splitlines()[-1])["actor"] == "user"
    assert W.resume_dispatch(tmp_path, actor="user", reason="x",
                             now=clock) == {"resumed": False,
                                            "detail": "not_paused"}
