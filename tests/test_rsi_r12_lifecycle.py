"""RSI-R1.2 T3：attempt 生命周期与看门狗握手（Plan §3.2；AC27/28/29）。

覆盖：

* AC27——SIGTERM 走受控退出并写终态报告；SIGKILL（进程被强杀）由看门狗写
  ``crash_*.json`` 补记，补记 ``worker_authored=False``，绝不伪造 worker 完成。
* AC28——有效完成握手后 pid 退出仍是 ``COMPLETED`` 而非 ``DEATH``；没有完成
  握手的死亡仍判 ``DEATH`` 并暂停。
* AC29——旧 attempt 的心跳不能给新 attempt 的完成件作证；同根未解 pause
  不得被新 attempt/新看门狗自动清除。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import attempts as ATT
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import watchdog as W
from tests.rsi_r1_helpers import make_ctx

PROFILE = P.PROFILE_R12_DEF
LANE = "practice"


def _ctx(tmp_path):
    return make_ctx(tmp_path)


def _spawn_and_reap() -> int:
    """真起一个子进程并回收：返回一个**已退出**的 pid（死亡探针输入）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    return pid


def _beat(state_dir: Path, *, attempt_id: str, pid: int, lane: str = LANE,
          envelope: str = "r1_test", ts: "str | None" = None) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "simpraxis.rsi_r1.heartbeat.v1", "schema_version": 1,
           "owner": "worker", "pid": pid, "ts_iso": ts or W.utc_now_z(),
           "seq": 1, "phase": "run", "envelope_id": envelope, "lane": lane,
           "attempt_id": attempt_id, "detail": {}}
    (state_dir / W.STATE_HEARTBEAT).write_text(
        json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def _watcher(state_dir: Path) -> W.Watcher:
    return W.Watcher(W.WatchConfig(state_dir=state_dir, poll_s=0.01))


def _make_attempt(tmp_path, *, thread_ok: bool = True):
    ctx = _ctx(tmp_path)
    with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
        att.finish({"ok": thread_ok})
    return ctx, att.attempt_id


# ---------------------------------------------------------------------------
# AC27：SIGTERM 受控收尾
# ---------------------------------------------------------------------------

def test_sigterm_writes_terminal_report_and_teardown(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ATT.AttemptTerminated):
        with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
            att.record_request("req-1")
            signal.raise_signal(signal.SIGTERM)
    report = json.loads((Path(tmp_path) / "attempts" / LANE / att.attempt_id
                        / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == ATT.STATUS_TERMINATED
    assert report["result"]["code"] == ATT.AttemptTerminated.code
    assert report["reason"].startswith(ATT.AttemptTerminated.code)
    assert report["request_refs"] == ["req-1"]
    assert report["teardown"]["recorded"] is True
    assert report["posts"]["delta"] == 0
    # 旧 handler 已恢复（进程语义不被测试污染）
    assert signal.getsignal(signal.SIGTERM) is not (ATT.AttemptTerminated)


def test_death_without_handshake_gets_watcher_crash_note(tmp_path):
    state_dir = Path(tmp_path) / "state"
    dead = _spawn_and_reap()
    _beat(state_dir, attempt_id="att-001", pid=dead)
    result = _watcher(state_dir).tick()
    assert result["state"] == W.DEATH
    note = json.loads((state_dir / "crash_att-001.json").read_text(
        encoding="utf-8"))
    assert note["schema"] == W.CRASH_SCHEMA
    assert note["worker_authored"] is False and note["authored_by"] == "watchdog"
    assert note["attempt_id"] == "att-001"
    # 不伪造 worker 完成：没有 completed 件，也没有 worker 报告
    assert not (state_dir / W.STATE_COMPLETED).exists()
    assert not (Path(tmp_path) / "attempts").exists()
    assert W.read_pause(state_dir) is not None


# ---------------------------------------------------------------------------
# AC28：有效完成握手阻止 DEATH；未完成死亡仍 DEATH
# ---------------------------------------------------------------------------

def test_valid_completion_prevents_death_after_pid_exit(tmp_path):
    ctx, attempt_id = _make_attempt(tmp_path)
    state_dir = Path(tmp_path) / "state"
    # pid 已退出（用已回收 pid 重建完成件），但完成握手引用了真实报告字节
    dead = _spawn_and_reap()
    _beat(state_dir, attempt_id=attempt_id, pid=dead)
    completion = json.loads((state_dir / W.STATE_COMPLETED).read_text(
        encoding="utf-8"))
    completion["pid"] = dead
    (state_dir / W.STATE_COMPLETED).write_text(
        json.dumps(completion, sort_keys=True) + "\n", encoding="utf-8")
    result = _watcher(state_dir).tick()
    assert result["state"] == W.COMPLETED
    assert result["paused"] is False
    assert W.read_pause(state_dir) is None
    assert not (state_dir / "crash_att-001.json").exists()


def test_completion_with_tampered_report_sha_falls_back_to_death(tmp_path):
    ctx, attempt_id = _make_attempt(tmp_path)
    state_dir = Path(tmp_path) / "state"
    dead = _spawn_and_reap()
    _beat(state_dir, attempt_id=attempt_id, pid=dead)
    completion = json.loads((state_dir / W.STATE_COMPLETED).read_text(
        encoding="utf-8"))
    completion["pid"] = dead
    completion["report_sha256"] = "0" * 64
    (state_dir / W.STATE_COMPLETED).write_text(
        json.dumps(completion, sort_keys=True) + "\n", encoding="utf-8")
    result = _watcher(state_dir).tick()
    assert result["state"] == W.DEATH
    note = json.loads((state_dir / f"crash_{attempt_id}.json").read_text(
        encoding="utf-8"))
    assert note["completion_reason"] == "report_sha_mismatch"


def test_unfinished_death_still_death(tmp_path):
    state_dir = Path(tmp_path) / "state"
    dead = _spawn_and_reap()
    _beat(state_dir, attempt_id="att-001", pid=dead)
    result = _watcher(state_dir).tick()
    assert result["state"] == W.DEATH
    assert W.read_pause(state_dir)["reason"] == "death"


def test_watcher_run_exits_cleanly_on_completion(tmp_path):
    ctx, attempt_id = _make_attempt(tmp_path)
    state_dir = Path(tmp_path) / "state"
    _beat(state_dir, attempt_id=attempt_id, pid=os.getpid())
    teardown = _watcher(state_dir).run(max_ticks=5)
    assert teardown["reason"] == "worker_completed"
    assert W.read_pause(state_dir) is None


# ---------------------------------------------------------------------------
# AC29：旧心跳不绑定新 attempt；未解 pause 不被自动清除
# ---------------------------------------------------------------------------

def test_old_heartbeat_cannot_testify_for_new_attempt(tmp_path):
    ctx, attempt_id = _make_attempt(tmp_path)
    assert attempt_id == "att-001"
    state_dir = Path(tmp_path) / "state"
    dead = _spawn_and_reap()
    # 心跳仍停留在旧 attempt att-000，完成件却属于 att-001
    _beat(state_dir, attempt_id="att-000", pid=dead)
    result = _watcher(state_dir).tick()
    assert result["state"] == W.DEATH
    note = json.loads((state_dir / "crash_att-000.json").read_text(
        encoding="utf-8"))
    assert note["completion_reason"] == "attempt_mismatch"


def test_pid_reuse_does_not_bind_old_heartbeat(tmp_path):
    ctx, attempt_id = _make_attempt(tmp_path)
    state_dir = Path(tmp_path) / "state"
    beat = _beat(state_dir, attempt_id=attempt_id, pid=os.getpid())
    beat["process_start"] = "1"  # 与完成件里的真实启动 tick 不同 → pid 复用
    (state_dir / W.STATE_HEARTBEAT).write_text(
        json.dumps(beat, sort_keys=True) + "\n", encoding="utf-8")
    result = _watcher(state_dir).tick()
    assert result["state"] != W.COMPLETED
    assert W.watcher_state(state_dir)["completion_reason"] == \
        "process_start_mismatch"


def test_new_attempt_does_not_clear_unresolved_pause(tmp_path):
    state_dir = Path(tmp_path) / "state"
    W.pause_dispatch(state_dir, reason="unknown", source="reconcile")
    # 新 attempt 正常开始/结束：不读也不清除 pause
    with ATT.AttemptSession(_ctx(tmp_path), LANE, PROFILE) as att:
        att.finish({"ok": True})
    assert W.read_pause(state_dir) is not None
    assert W.read_pause(state_dir)["source"] == "reconcile"
    # 只有显式 resume（必须 actor/reason）才能解除
    with pytest.raises(ValueError):
        W.resume_dispatch(state_dir, actor="", reason="x")
    W.resume_dispatch(state_dir, actor="human", reason="已核对")
    assert W.read_pause(state_dir) is None


def test_watcher_restart_preserves_pause(tmp_path):
    state_dir = Path(tmp_path) / "state"
    W.pause_dispatch(state_dir, reason="silence", source="watchdog")
    dead = _spawn_and_reap()
    _beat(state_dir, attempt_id="att-009", pid=dead)
    _watcher(state_dir).tick()
    # 第二个看门狗看到的是同一个未解 pause，不改写原因
    assert W.read_pause(state_dir)["reason"] == "silence"
