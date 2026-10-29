"""Phase 2.5 Task 2 — 看门狗 SOP 故障注入反证（AC-T2-1..8；0 付费、hermetic）。

七类注入反证（AC-T2-4）：
  ① kill -9 目标子进程 → rc=3，告警动作序固定，快照包七项齐；
  ② 注入伪 OOM 行 → failure_keyword 死亡签名；
  ③ 日志只读/停更致静默 → rc=4；
  ④ 命名容器强删（伪造 docker inspect 夹具）→ container_gone；
  ⑤ 变异反证（系列化）：四类注入真实 Snapshot 下，只盯成功标志的退化监控器
     全程亮绿，真实监控器全程判死/静默；
  ⑥ rc=0 成功不误报、awaiting_human 不告警；成功 grep/marker 不即终态
     （评审 M1：须进程达干净终态且无死亡签名，否则 PENDING/DEATH）；
  ⑦ 合成日志 ≥6 良性零告警 + ≥6 真失败必告警（NF-4 KilledWorker）。
另含：PDEATHSIG 孤儿率实测、元看门狗真实 kill 演练（加速档）、
AC-T2-7 超时信号回主线程。

纪律：不触网（通知一律 FakeNotifier/fake:PATH），故障注入只杀本测试
自己 fork 的进程；轮询/心跳用短时限夹具加速（poll 0.5s、heartbeat 1s）。
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orchestrator.common import watched
from orchestrator.common.watched import (
    EXIT_DEATH,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_SILENCE,
    SILENCE_LIMITS,
    ContainerStatus,
    FakeNotifier,
    Snapshot,
    WatchConfig,
    WatchTimeout,
    classify,
    scan_failure_signatures,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_WRAPPED = REPO_ROOT / "scripts" / "ops" / "run_watched.sh"
WATCH_WRAPPER = REPO_ROOT / "scripts" / "ops" / "watch_loop.sh"
VENV_PY = "/mnt/venvs/simpraxis/bin/python"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def wd(tmp_path: Path) -> Path:
    root = tmp_path / "watchdog"
    (root / "runs").mkdir(parents=True)
    (root / "triage_bundles").mkdir()
    return root


@pytest.fixture()
def fakebin(tmp_path: Path) -> Path:
    """伪造 ps/nvidia-smi/docker：快照包七项在无 GPU/无真 docker 下可齐套。"""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "ps").write_text(
        "#!/usr/bin/env bash\n"
        "echo '  PID  PPID  PGID STAT ELAPSED COMMAND'\n"
        "echo '    1     0     1 Ss    10:00 [fake ps tree]'\n")
    (bin_dir / "nvidia-smi").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'fake nvidia-smi: no GPU processes (hermetic fixture)'\n")
    # FAKE_DOCKER_STATE=running|exited|gone，默认 running
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "state=\"${FAKE_DOCKER_STATE:-running}\"\n"
        "if [ \"$1\" = \"inspect\" ]; then\n"
        "  case \"$state\" in\n"
        "    running) echo true; exit 0;;\n"
        "    exited) echo false; exit 0;;\n"
        "    gone) echo 'Error response from daemon: No such object: fakebox' "
        ">&2; exit 1;;\n"
        "  esac\n"
        "fi\n"
        "if [ \"$1\" = \"logs\" ]; then echo '[fake docker logs] tail'; exit 0; fi\n"
        "exit 0\n")
    for name in ("ps", "nvidia-smi", "docker"):
        os.chmod(bin_dir / name, 0o755)
    return bin_dir


def _env(fakebin: "Path | None" = None, **extra) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["SIMPRAXIS_WATCHDOG_HEARTBEAT_S"] = "1"
    if fakebin is not None:
        env["PATH"] = f"{fakebin}{os.pathsep}" + env.get("PATH", "")
    env.update(extra)
    return env


def _wait_for(path: Path, timeout: float = 10.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return path
        time.sleep(0.05)
    raise AssertionError(f"等不到 {path}")


def _find_run_dir(wd: Path, name: str) -> Path:
    deadline = time.time() + 10.0
    while time.time() < deadline:
        hits = sorted((wd / "runs").glob(f"{name}-*"))
        if hits:
            return hits[-1]
        time.sleep(0.05)
    raise AssertionError(f"等不到 run dir {name}-*")


class WatchedRun:
    """run-watched 子进程句柄；finalizer 保证 supervisor/目标/toucher 全回收。"""

    def __init__(self, proc: subprocess.Popen, run_dir: Path,
                 supervisor_pid: int):
        self.proc = proc
        self.run_dir = run_dir
        self.supervisor_pid = supervisor_pid

    @property
    def target_pid(self) -> int:
        return int((self.run_dir / "pid").read_text().strip())

    def kill_target(self, sig: int = signal.SIGKILL) -> None:
        try:
            os.kill(self.target_pid, sig)
        except ProcessLookupError:
            pass

    def kill_supervisor(self, sig: int = signal.SIGKILL) -> None:
        try:
            os.kill(self.supervisor_pid, sig)
        except ProcessLookupError:
            pass

    def reap_tree(self) -> None:
        for pid in (self.target_pid, self.supervisor_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def start_run(name: str, cmd: list, wd: Path, *, fakebin: "Path | None" = None,
              options: "list | None" = None) -> WatchedRun:
    argv = [sys.executable, "-m", "orchestrator.common.watched", "run-watched",
            name, "--watchdog-root", str(wd), "--silence-s", "1200"]
    argv += options or []
    argv += ["--"] + cmd
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), env=_env(fakebin))
    run_dir = _find_run_dir(wd, name)
    _wait_for(run_dir / "pid")
    run = WatchedRun(proc, run_dir, proc.pid)
    return run


def _make_cfg(rd: Path, wd: Path, fakebin: Path, **kw) -> WatchConfig:
    ledger = wd / "ledger.jsonl"
    ledger.write_text("\n".join(
        json.dumps({"seq": i, "task": 25, "stage": "coding_debug", "ok": True})
        for i in (1, 2, 3)) + "\n")
    pause = wd / "quota_pause.json"
    pause.write_text(json.dumps({"paused": False}))
    cfg = WatchConfig(
        run_dir=rd, watchdog_root=wd,
        alerts_path=wd / "alerts.jsonl",
        triage_root=wd / "triage_bundles",
        notifier=FakeNotifier(wd / "p2p.jsonl"),
        ledger_paths=[ledger], pause_paths=[pause],
        tool_env=_env(fakebin),
        poll_s=0.5, silence_s=1200, heartbeat_timeout_s=10.0)
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _run_loop_thread(cfg: WatchConfig) -> tuple[threading.Thread, dict]:
    holder: dict = {}

    def runner() -> None:
        try:
            holder["rc"] = watched.run_watch_loop(cfg)
        except BaseException as exc:  # 真实监控器异常也要让测试看见
            holder["exc"] = exc

    t = threading.Thread(target=runner, daemon=True, name="watch-loop-test")
    t.start()
    return t, holder


def _join_rc(holder: dict, t: threading.Thread, timeout: float = 30.0):
    t.join(timeout)
    if "exc" in holder:
        raise holder["exc"]
    assert "rc" in holder, f"监控线程 {timeout}s 未终态"
    return holder["rc"]


def _alerts(wd: Path) -> list[dict]:
    path = wd / "alerts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _p2p(wd: Path) -> list[dict]:
    path = wd / "p2p.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# AC-T2-1：七件套 + 权限 + cmd_sha256 + 时限播种
# ---------------------------------------------------------------------------

def test_run_watched_seven_pieces_and_modes(wd):
    run = start_run("seven", ["bash", "-c", "echo hi"], wd)
    rc = run.proc.wait(timeout=15)
    assert rc == 0
    d = run.run_dir
    for piece in watched.SEVEN_PIECES:
        p = d / piece
        assert p.exists(), f"缺七件套：{piece}"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, piece
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert (d / "stdout").read_text() == "hi\n"
    assert (d / "exit_code").read_text() == "0\n"
    assert (d / "pid").read_text().strip() == str(run.target_pid)
    start_iso = (d / "start_iso").read_text().strip()
    assert start_iso.endswith("Z")
    # cmd_sha256 与 meta.argv 规范哈希逐字节一致
    meta = json.loads((d / "meta.json").read_text())
    import hashlib
    want = hashlib.sha256("\u0000".join(meta["argv"]).encode()).hexdigest()
    assert (d / "cmd_sha256").read_text().strip() == want
    # 静默时限播种（AC-T2-5：300/600/900/1200，常量即唯一真值）
    limits = json.loads((wd / "silence_limits.json").read_text())
    assert limits["limits"] == {
        "ark_single_call_s": 300, "gpu_step_eval_s": 600,
        "download_s": 900, "single_birth_s": 1200}
    assert limits["limits"] == SILENCE_LIMITS
    assert stat.S_IMODE((wd / "silence_limits.json").stat().st_mode) == 0o600


def test_run_watched_signal_exit_code(wd):
    # kill -9 目标 → supervisor 落 137；shell 包装器透传同一退出码
    run = start_run("sig", ["bash", "-c", "kill -9 $$"], wd)
    assert run.proc.wait(timeout=15) == 137
    assert (run.run_dir / "exit_code").read_text() == "137\n"


def test_run_watched_via_shell_wrapper(wd):
    env = _env()
    env["SIMPRAXIS_PHASE25_ROOT"] = str(wd.parent)
    proc = subprocess.run(
        [str(RUN_WRAPPED), "wraptest", "--watchdog-root", str(wd),
         "--", "bash", "-c", "echo wrap"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    d = _find_run_dir(wd, "wraptest")
    assert (d / "stdout").read_text() == "wrap\n"


def test_name_validation():
    with pytest.raises(ValueError):
        watched.validate_name("../escape")
    watched.validate_name("task5.run_1-2")


# ---------------------------------------------------------------------------
# PDEATHSIG 孤儿率实测（Rejected：无内核钩的心跳是假活）
# ---------------------------------------------------------------------------

def test_toucher_dies_with_supervisor_pdeathsig(wd):
    run = start_run("orphan",
                    ["bash", "-c", "for i in $(seq 1 120); do echo $i; sleep 1; done"],
                    wd)
    hb = run.run_dir / "heartbeat"
    _wait_for(hb)
    # 找到 toucher（其 argv 含 heartbeat 路径）
    def toucher_pids():
        cp = subprocess.run(["pgrep", "-f", str(hb)], capture_output=True,
                            text=True)
        return {int(x) for x in cp.stdout.split()}

    deadline = time.time() + 8.0
    while not toucher_pids() and time.time() < deadline:
        time.sleep(0.1)
    assert toucher_pids(), "toucher 从未起来"
    # 硬杀 supervisor：内核 PDEATHSIG 必须同杀 toucher
    run.kill_supervisor(signal.SIGKILL)
    deadline = time.time() + 5.0
    while toucher_pids() and time.time() < deadline:
        time.sleep(0.1)
    assert not toucher_pids(), "supervisor 死后 toucher 变孤儿（PDEATHSIG 失效）"
    # 目标进程不挂 deathsig（元监控重启后续判的语义），由测试自己回收
    assert watched._pid_alive(run.target_pid) is True
    run.reap_tree()


# ---------------------------------------------------------------------------
# 注入 ① kill -9 → rc=3 + 动作序 + 快照七项齐
# ---------------------------------------------------------------------------

def test_inject_kill9_death_rc3_bundle_seven(wd, fakebin):
    run = start_run(
        "kill9",
        ["bash", "-c", "for i in $(seq 1 600); do echo line$i; sleep 1; done"],
        wd, fakebin=fakebin, options=["--container", "fakebox"])
    t0 = time.time()
    cfg = _make_cfg(run.run_dir, wd, fakebin, container="fakebox")
    t, holder = _run_loop_thread(cfg)
    time.sleep(1.0)                       # 等监控器进入轮询
    run.kill_target(signal.SIGKILL)
    rc = _join_rc(holder, t, timeout=30.0)
    elapsed = time.time() - t0
    assert rc == EXIT_DEATH
    assert elapsed < 90.0                # AC：60–90s 窗内（加速轮询只会更快）

    alerts = _alerts(wd)
    assert len(alerts) == 1
    rec = alerts[0]
    assert rec["kind"] == "death"
    assert rec["reason"] in ("exit_code", "process_vanished")
    assert rec["exit_code"] == 137
    assert rec["p2p_message_id"].startswith("fake-")

    # 动作序：①P2P 记录 ≤ ②bundle manifest ≤ ③alerts.jsonl
    p2p_mt = (wd / "p2p.jsonl").stat().st_mtime_ns
    bundle = Path(rec["bundle_dir"])
    manifest_mt = (bundle / "manifest.json").stat().st_mtime_ns
    alerts_mt = (wd / "alerts.jsonl").stat().st_mtime_ns
    assert p2p_mt <= manifest_mt <= alerts_mt
    assert stat.S_IMODE((wd / "alerts.jsonl").stat().st_mode) == 0o600

    # 快照七项齐且在伪造工具下全部 ok（含命名容器 docker logs）
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [i["item"] for i in manifest["items"]] == list(watched.TRIAGE_ITEMS)
    assert all(i["ok"] for i in manifest["items"]), \
        [i["item"] for i in manifest["items"] if not i["ok"]]
    assert "fake docker logs" in (bundle / "docker_logs.txt").read_text()
    assert "fake ps tree" in (bundle / "ps_tree.txt").read_text()
    ledger = json.loads((bundle / "ledger_last3.json").read_text())
    assert len(next(iter(ledger.values()))) == 3

    p2p = _p2p(wd)
    assert len(p2p) == 1 and "进程死亡" in p2p[0]["text"]
    run.reap_tree()


# ---------------------------------------------------------------------------
# 注入 ② 伪 OOM 行
# ---------------------------------------------------------------------------

def test_inject_fake_oom_line_keyword_death(wd, fakebin):
    run = start_run("oomline", ["bash", "-c", "sleep 120"], wd,
                    fakebin=fakebin)
    cfg = _make_cfg(run.run_dir, wd, fakebin)
    t, holder = _run_loop_thread(cfg)
    time.sleep(1.0)
    with (run.run_dir / "stdout").open("a") as f:
        f.write("torch.cuda.OutOfMemoryError: CUDA out of memory. "
                "Tried to allocate 2.00 GiB\n")
    rc = _join_rc(holder, t, timeout=20.0)
    assert rc == EXIT_DEATH
    rec = _alerts(wd)[0]
    assert rec["kind"] == "death" and rec["reason"] == "failure_keyword"
    names = {h["pattern"] for h in rec["failure_hits"]}
    assert {"cuda_oom", "out_of_memory_error"} <= names
    run.reap_tree()


# ---------------------------------------------------------------------------
# 注入 ③ 只读致日志静默 → rc=4
# ---------------------------------------------------------------------------

def test_inject_readonly_silence_rc4(wd, fakebin):
    # Linux 下已打开 fd 不受 chmod 影响，故夹具目标自行停更（挂起态），
    # chmod 0555 复现「日志目录只读事故」（也阻止新文件/exit_code 写入）；
    # 被验证的签名是日志 mtime 静默——心跳仍由 toucher 持续刷新。
    # 锁内适配（Phase 2.5）：注入手法与合同字面「只读目录致静默」略有差异
    # （已开 fd 仍可写），实际被验签名＝日志 mtime 超静默帽且心跳新鲜
    # （rc=4 静默，非 rc=3 死亡，双签名区分见下方断言）。
    run = start_run("silent",
                    ["bash", "-c", "echo a; echo b; sleep 120"], wd,
                    fakebin=fakebin)
    d = run.run_dir
    _wait_for(d / "stdout")
    deadline = time.time() + 5.0
    while (d / "stdout").read_text().count("\n") < 2 and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(1.2)                       # 让日志年龄超过 3s 静默帽
    os.chmod(d, 0o555)
    try:
        cfg = _make_cfg(d, wd, fakebin, silence_s=3)
        t, holder = _run_loop_thread(cfg)
        rc = _join_rc(holder, t, timeout=20.0)
        assert rc == EXIT_SILENCE
        rec = _alerts(wd)[0]
        assert rec["kind"] == "silence"
        assert rec["reason"] == "log_mtime_stale"
        assert rec["newest_log_age_s"] >= 3.0
        # 心跳是新鲜的：rc=4 来自日志静默而非进程死亡（双签名区分）
        assert rec["heartbeat_age_s"] is not None
        assert rec["heartbeat_age_s"] < 10.0
        assert _p2p(wd)[0]["text"].startswith("[SimPraxis 看门狗] 进程静默")
    finally:
        os.chmod(d, 0o700)
        run.reap_tree()


# ---------------------------------------------------------------------------
# 注入 ④ 容器消失：默认 docker_inspect 打伪造 docker 夹具
# ---------------------------------------------------------------------------

def test_docker_inspect_fixture_states(fakebin):
    for state, want in (("running", "running"), ("exited", "exited"),
                        ("gone", "gone")):
        env = _env(fakebin, FAKE_DOCKER_STATE=state)
        got = watched.docker_inspect("fakebox", env=env)
        assert got.state == want, (state, got)


def test_inject_container_gone_death(wd, fakebin):
    run = start_run("box", ["bash", "-c", "sleep 120"], wd, fakebin=fakebin,
                    options=["--container", "fakebox"])
    env_gone = _env(fakebin, FAKE_DOCKER_STATE="gone")

    cfg = _make_cfg(run.run_dir, wd, fakebin, container="fakebox")
    cfg.tool_env = env_gone
    cfg.docker_probe = lambda c: watched.docker_inspect(c, env=env_gone)
    snap = watched.take_snapshot(cfg)
    assert snap.container.state == "gone"
    verdict = classify(snap, heartbeat_timeout_s=10.0)
    assert verdict.status == "DEATH"
    assert verdict.reason == "container_gone"

    # exited（非 running 但对象还在）同样是死亡签名
    env_exited = _env(fakebin, FAKE_DOCKER_STATE="exited")
    cfg.tool_env = env_exited
    cfg.docker_probe = lambda c: watched.docker_inspect(c, env=env_exited)
    snap2 = watched.take_snapshot(cfg)
    assert snap2.container.state == "exited"
    assert classify(snap2, heartbeat_timeout_s=10.0).reason == \
        "container_exited"
    run.reap_tree()


# ---------------------------------------------------------------------------
# 注入 ⑤ 变异反证：退化的成功-only 监控器在死亡时仍亮绿
# ---------------------------------------------------------------------------

def _snap(**overrides) -> Snapshot:
    """合成健康在跑快照基线（纯函数级 M1 用）。"""
    import dataclasses
    base = dict(
        taken_iso="2026-10-29T00:00:00Z", pid=4242, pid_alive=True,
        exit_code=None, heartbeat_age_s=1.0, newest_log_age_s=1.0,
        failure_hits=[], success_grep_hit=False, success_marker=False,
        artifacts=[], container=ContainerStatus("unconfigured"),
        awaiting_human=False, start_age_s=2.0, silence_s=600.0)
    base.update(overrides)
    return Snapshot(**base)


def dataclasses_replace(snap, **kw):
    import dataclasses
    return dataclasses.replace(snap, **kw)


# 退化监控器（变异体）：只认成功标志；没有成功标志时也不看失败签名族，
# 照样亮绿——即评审 L1 所指「安慰剂」姿态。
def _degraded_success_only(snap: Snapshot) -> str:
    if snap.success_marker or snap.success_grep_hit:
        return "SUCCESS"
    return "GREEN"


def test_mutation_serialized_four_injection_real_snapshots(wd, fakebin):
    """L1 系列化变异反证：①kill-9 ②伪 OOM ③静默 ④容器消失的 *真实*
    take_snapshot 产出整批喂退化器：退化器全程亮绿，真实 classify 全程
    判死/静默（不再只凭 1 个手工快照）。三个死亡场景预置「成功 grep 已
    命中」（成功行先于死亡，M1 场景），退化器在死亡现场亮 SUCCESS。"""
    # ① kill -9 目标 → 真实 exit_code=137
    r1 = start_run("mut-kill", ["bash", "-c", "sleep 120"], wd,
                   fakebin=fakebin)
    c1 = _make_cfg(r1.run_dir, wd, fakebin, success_grep=r"WJ_REPORT_READY")
    r1.kill_target(signal.SIGKILL)
    ec1 = _wait_for(r1.run_dir / "exit_code")
    assert ec1.read_text().strip() == "137"
    s1 = watched.take_snapshot(c1)
    r1.reap_tree()

    # ② 进程仍活但日志被注入伪 OOM 行
    r2 = start_run("mut-oom", ["bash", "-c", "sleep 120"], wd,
                   fakebin=fakebin)
    c2 = _make_cfg(r2.run_dir, wd, fakebin, success_grep=r"WJ_REPORT_READY")
    with (r2.run_dir / "stdout").open("a") as f:
        f.write("CUDA out of memory. Tried to allocate 2.00 GiB\n")
    deadline = time.time() + 5.0
    s2 = watched.take_snapshot(c2)
    while not s2.failure_hits and time.time() < deadline:
        time.sleep(0.1)
        s2 = watched.take_snapshot(c2)
    assert s2.failure_hits and s2.pid_alive is True
    r2.reap_tree()

    # ③ 目标自行停更：日志 mtime 超 1s 帽，心跳仍新鲜（静默非死亡）
    r3 = start_run("mut-sil", ["bash", "-c", "echo a; sleep 120"], wd,
                   fakebin=fakebin)
    _wait_for(r3.run_dir / "stdout")
    c3 = _make_cfg(r3.run_dir, wd, fakebin, silence_s=1)
    deadline = time.time() + 5.0
    s3 = watched.take_snapshot(c3)
    while (s3.newest_log_age_s is None or s3.newest_log_age_s <= 1.0) \
            and time.time() < deadline:
        time.sleep(0.2)
        s3 = watched.take_snapshot(c3)
    assert s3.newest_log_age_s > 1.0 and s3.heartbeat_age_s < 10.0
    r3.reap_tree()

    # ④ 命名容器被强删（伪造 docker inspect gone，不触真 docker）
    r4 = start_run("mut-box", ["bash", "-c", "sleep 120"], wd,
                   fakebin=fakebin, options=["--container", "fakebox"])
    env_gone = _env(fakebin, FAKE_DOCKER_STATE="gone")
    c4 = _make_cfg(r4.run_dir, wd, fakebin, container="fakebox",
                   success_grep=r"WJ_REPORT_READY")
    c4.tool_env = env_gone
    c4.docker_probe = lambda c: watched.docker_inspect(c, env=env_gone)
    s4 = watched.take_snapshot(c4)
    assert s4.container.state == "gone"
    r4.reap_tree()

    series = (
        ("kill9", dataclasses_replace(s1, success_grep_hit=True), "DEATH",
         "SUCCESS"),
        ("oom", dataclasses_replace(s2, success_grep_hit=True), "DEATH",
         "SUCCESS"),
        ("silence", s3, "SILENCE", "GREEN"),
        ("container_gone", dataclasses_replace(s4, success_grep_hit=True),
         "DEATH", "SUCCESS"),
    )
    for name, snap, want_status, want_light in series:
        verdict = classify(snap, heartbeat_timeout_s=10.0, silence_s=1.0)
        assert verdict.status == want_status, (name, verdict)
        light = _degraded_success_only(snap)
        assert light == want_light, (name, light, verdict)

    # 反向保真：健康在跑真实快照下真实监控器 PENDING、退化器也亮绿
    healthy = _snap()
    assert classify(healthy, silence_s=600).status == "PENDING"
    assert _degraded_success_only(healthy) == "GREEN"


# ---------------------------------------------------------------------------
# 注入 ⑥ 成功不误报 / awaiting_human 不告警 / 成功 grep / 产物 sha
# ---------------------------------------------------------------------------

def test_success_exit_zero_no_alert(wd, fakebin):
    run = start_run("ok", ["bash", "-c", "echo done; exit 0"], wd,
                    fakebin=fakebin)
    cfg = _make_cfg(run.run_dir, wd, fakebin)
    t, holder = _run_loop_thread(cfg)
    assert _join_rc(holder, t) == EXIT_OK
    assert _alerts(wd) == [] and _p2p(wd) == []
    run.proc.wait(timeout=5)


def test_inject_supervisor_hardkill_vanished_two_stage_confirm(wd, fakebin):
    """supervisor 被硬杀：pid 消失且无 exit_code，宽限+跨拍两阶段确认后判死。

    真签名必须仍能发红；两阶段确认只过滤「ESRCH 早于 exit_code 落盘」的竞态窗。
    """
    run = start_run("vanish", ["bash", "-c", "sleep 60"], wd, fakebin=fakebin)
    cfg = _make_cfg(run.run_dir, wd, fakebin, poll_s=0.3,
                    vanish_grace_s=0.3, heartbeat_timeout_s=30.0)
    t, holder = _run_loop_thread(cfg)
    time.sleep(1.0)                       # 等监控器进入轮询
    assert not (run.run_dir / "exit_code").exists()
    run.kill_supervisor(signal.SIGKILL)   # toucher 由 PDEATHSIG 内核同死
    run.kill_target(signal.SIGKILL)       # 只留 pid 文件、绝无 exit_code
    rc = _join_rc(holder, t, timeout=30.0)
    assert rc == EXIT_DEATH
    alerts = _alerts(wd)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "death"
    assert alerts[0]["reason"] == "process_vanished"
    assert not (run.run_dir / "exit_code").exists()
    run.reap_tree()


def test_rapid_success_exit_never_cries_death(wd, fakebin):
    """回归：快速成功退出在 ESRCH/exit_code 落盘窗内不得误报死亡。

    旧单拍逻辑在 poll 0.05s 下约半数假红（全量套机重负时实测翻车一次）；
    宽限复查 + 跨拍确认后连续 8 次快速成功必须零告警、全 rc=0。
    """
    for i in range(8):
        run = start_run(f"rapid{i}", ["bash", "-c", "echo done; exit 0"],
                        wd, fakebin=fakebin)
        cfg = _make_cfg(run.run_dir, wd, fakebin, poll_s=0.05,
                        vanish_grace_s=0.2)
        t, holder = _run_loop_thread(cfg)
        assert _join_rc(holder, t, timeout=15.0) == EXIT_OK
        run.proc.wait(timeout=5)
    assert _alerts(wd) == [] and _p2p(wd) == []


def test_awaiting_human_suppresses_alerts(wd, fakebin):
    run = start_run("gate", ["bash", "-c", "sleep 60"], wd, fakebin=fakebin)
    (run.run_dir / "awaiting_human").write_text("gate1\n")
    try:
        # 日志静默帽 1s + 心跳超时 2s，等 3 拍：显式等 Gate 态必须零告警
        cfg = _make_cfg(run.run_dir, wd, fakebin, silence_s=1,
                        heartbeat_timeout_s=2, poll_s=0.2, max_ticks=3)
        t, holder = _run_loop_thread(cfg)
        assert _join_rc(holder, t, timeout=15.0) == watched.EXIT_PENDING
        assert _alerts(wd) == [] and _p2p(wd) == []
    finally:
        run.reap_tree()


# ---------------------------------------------------------------------------
# 评审 M1：成功签名不得压过晚期死亡签名（纯函数两拍序列 a–d）
# ---------------------------------------------------------------------------

def test_m1_grep_hit_with_late_sigkill_is_death():
    """a) grep 成功行已出现，清理段裸 Killed + rc=137 → DEATH（旧码返 SUCCESS）。"""
    hits = scan_failure_signatures("Killed\n")
    assert {h.pattern for h in hits} == {"killed"}
    snap = _snap(pid_alive=False, exit_code=137, success_grep_hit=True,
                 failure_hits=hits)
    v = classify(snap)
    assert v.status == "DEATH" and v.reason == "failure_keyword"


def test_m1_marker_with_vanished_pid_is_death():
    """b) marker 存在 + pid 已确认消失且无 exit_code → DEATH（旧码返 SUCCESS）。"""
    snap = _snap(pid_alive=False, exit_code=None, success_marker=True)
    v = classify(snap)
    assert v.status == "DEATH" and v.reason == "process_vanished"


def test_m1_grep_alive_pending_then_rc0_success():
    """c) grep 命中但进程仍活 → PENDING；随后拍 rc=0 → SUCCESS（不增假红）。"""
    beat1 = _snap(success_grep_hit=True)
    v1 = classify(beat1)
    assert v1.status == "PENDING"
    assert v1.reason == "success_grep_await_terminal"
    # marker 同理（仍活不终态）
    assert classify(_snap(success_marker=True)).reason == \
        "success_marker_await_terminal"
    beat2 = dataclasses_replace(beat1, pid_alive=False, exit_code=0)
    assert classify(beat2).status == "SUCCESS"


def test_m1_grep_alive_pending_then_sigsegv_death():
    """d) grep 命中但仍活 → PENDING；随后拍 rc=139 → DEATH（不漏晚期死亡）。"""
    beat1 = _snap(success_grep_hit=True)
    assert classify(beat1).status == "PENDING"
    beat2 = dataclasses_replace(beat1, pid_alive=False, exit_code=139)
    v2 = classify(beat2)
    assert v2.status == "DEATH" and v2.reason == "exit_code"


def test_m1_any_death_signature_overrides_grep():
    """grep 命中 + 死亡签名集合各成员，一律 DEATH（marker 同规）。"""
    cases = (
        (_snap(success_grep_hit=True, exit_code=137), "exit_code"),
        (_snap(success_grep_hit=True, heartbeat_age_s=99.0),
         "heartbeat_timeout"),
        (_snap(success_grep_hit=True,
               container=ContainerStatus("gone", "No such object")),
         "container_gone"),
        (_snap(success_marker=True,
               container=ContainerStatus("exited", "Exited (1)")),
         "container_exited"),
    )
    for snap, reason in cases:
        v = classify(snap, heartbeat_timeout_s=45.0)
        assert v.status == "DEATH" and v.reason == reason, (reason, v)


def test_success_grep_requires_clean_terminal_m1(wd, fakebin):
    """评审 M1 集成（原 test_success_grep_terminal 改语义，断言不弱化）：
    grep 不再即终态。场景 1 成功行后 SIGKILL → 死亡告警；场景 2 成功行后
    干净 exit 0 → SUCCESS 零告警。"""
    run = start_run(
        "grepdie",
        ["bash", "-c", "echo WJ_REPORT_READY; kill -9 $$"], wd,
        fakebin=fakebin)
    cfg = _make_cfg(run.run_dir, wd, fakebin,
                    success_grep=r"WJ_REPORT_READY")
    t, holder = _run_loop_thread(cfg)
    assert _join_rc(holder, t, timeout=30.0) == EXIT_DEATH
    death_alerts = _alerts(wd)
    assert len(death_alerts) == 1 and death_alerts[0]["kind"] == "death"
    assert death_alerts[0]["exit_code"] == 137
    run.reap_tree()

    run2 = start_run(
        "grepok",
        ["bash", "-c", "echo WJ_REPORT_READY; exit 0"], wd,
        fakebin=fakebin)
    cfg2 = _make_cfg(run2.run_dir, wd, fakebin,
                     success_grep=r"WJ_REPORT_READY")
    t2, holder2 = _run_loop_thread(cfg2)
    assert _join_rc(holder2, t2, timeout=30.0) == EXIT_OK
    run2.proc.wait(timeout=5)
    # 不增假红：全程仅场景 1 的一条死亡告警
    assert _alerts(wd) == death_alerts
    assert _p2p(wd) and "进程死亡" in _p2p(wd)[0]["text"]
    run2.reap_tree()


def test_artifact_sha_success_and_mismatch_death(tmp_path):
    art = tmp_path / "report.json"
    art.write_text('{"score": 0.91}')
    import hashlib
    good = hashlib.sha256(art.read_bytes()).hexdigest()

    ok = Snapshot("2026-10-29T00:00:00Z", 1, True, 0, 0.1, 0.1, [], False,
                  False, [watched.ArtifactStatus(str(art), True, good, True)],
                  ContainerStatus("unconfigured"), False, 5.0)
    assert classify(ok).reason == "rc0_artifact_sha"

    bad = dataclasses_replace(
        ok, artifacts=[watched.ArtifactStatus(str(art), True, "deadbeef",
                                              False)])
    verdict = classify(bad)
    assert verdict.status == "DEATH" and verdict.reason == "artifact_mismatch"


def test_heartbeat_timeout_is_death():
    snap = Snapshot("2026-10-29T00:00:00Z", 1, True, None, 99.0, 1.0,
                    [], False, False, [], ContainerStatus("unconfigured"),
                    False, 100.0)
    assert classify(snap, heartbeat_timeout_s=45).reason == "heartbeat_timeout"


# ---------------------------------------------------------------------------
# 注入 ⑦ 合成日志误报反证（≥6 良性零告警 + ≥6 真失败必告警，NF-4）
# ---------------------------------------------------------------------------

BENIGN_LINES = [
    # NF-4：KilledWorker 是调度器复合词，不命中边界 Killed
    "INFO scheduler: KilledWorker(tcp://10.0.0.3:42) handled, replica replaced",
    # 同行 retried OK：应用层已捕获并继续
    "WARN transient Traceback (most recent call last): in call; retried OK",
    # 同行 recovered
    "WARN replica saw OutOfMemoryError on one shard but recovered next round",
    # docstring 中的异常类名是源码文本
    '    """Raises OutOfMemoryError when the arena cannot grow."""',
    # 注释里的 exit code
    "# legacy runner printed 'exit code 7' on retryable abort",
    # 注释里的 CUDA OOM 文档指引
    "    // CUDA out of memory: see ops/runbook.md section 9",
    # 成功行不允许误命中 exit code 非零
    "runner finished: exit code 0 after 312.4s",
    "INFO KilledWorker-evt coalesced, recovered after reschedule",
]

FAILURE_LINES = [
    ("traceback", "Traceback (most recent call last):"),
    ("cuda_oom", "torch.cuda.OutOfMemoryError: CUDA out of memory. "
                 "Tried to allocate 2.00 GiB"),
    ("killed", "Killed"),
    ("killed", "/usr/bin/bash: line 1:  7721 Killed                  ./train.sh"),
    ("oom_kill", "Memory cgroup out of memory: OOM-killed process 7721 (python3)"),
    ("out_of_memory_error", "java.lang.OutOfMemoryError: Java heap space"),
    ("exit_code_nonzero", "docker: Container failed with exit code 137"),
    ("oom_kill", "kernel: memory cgroup invoked OOM-kill for cgroup /train"),
]


def test_synthetic_logs_benign_zero_alerts():
    assert len(BENIGN_LINES) >= 6
    text = "\n".join(BENIGN_LINES) + "\n"
    assert scan_failure_signatures(text) == []
    # NF-4 单点：KilledWorker 与裸 Killed 边界对照
    assert not scan_failure_signatures("x KilledWorker y")
    assert {h.pattern for h in scan_failure_signatures("x Killed y")} == \
        {"killed"}


def test_synthetic_logs_real_failures_all_alert():
    assert len(FAILURE_LINES) >= 6
    by_name: dict[str, int] = {}
    for expected, line in FAILURE_LINES:
        hits = scan_failure_signatures(line)
        assert hits, f"真失败未告警：{line}"
        assert expected in {h.pattern for h in hits}, (line, hits)
        for h in hits:
            by_name[h.pattern] = by_name.get(h.pattern, 0) + 1
    # 六种失败模式全部至少被一条真失败触发
    assert set(by_name) == {
        "traceback", "cuda_oom", "killed", "oom_kill",
        "out_of_memory_error", "exit_code_nonzero"}


def test_constants_have_source_reasons():
    for name, _rx, reason in watched.FAILURE_PATTERNS:
        assert reason and name
    for name, _rule, reason in watched.BENIGN_RULES:
        assert reason and name


# ---------------------------------------------------------------------------
# L3：silence_limits.json 已存在不覆盖；缺失才播种默认四值
# ---------------------------------------------------------------------------

def test_seed_silence_limits_preserves_existing_and_defaults(tmp_path):
    root = tmp_path / "watchdog"
    root.mkdir(mode=0o700)
    path = root / "silence_limits.json"
    custom = '{"limits": {"ark_single_call_s": 1}}'
    path.write_text(custom)
    mtime_ns = path.stat().st_mtime_ns
    time.sleep(0.02)
    watched.seed_silence_limits(root)
    assert path.read_text() == custom          # 内容不变
    assert path.stat().st_mtime_ns == mtime_ns  # mtime 不变

    fresh = tmp_path / "w2"
    fresh.mkdir()
    seeded = watched.seed_silence_limits(fresh)
    data = json.loads(seeded.read_text())
    assert data["limits"] == {
        "ark_single_call_s": 300, "gpu_step_eval_s": 600,
        "download_s": 900, "single_birth_s": 1200}
    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# L2：watch-loop --once 真子进程 JSON 输出 + 退出码映射（含宽限分支）
# ---------------------------------------------------------------------------

def _once(rd: Path, wd: Path, *extra: str):
    argv = ["--run-dir", str(rd), "--watchdog-root", str(wd),
            "--notifier", "none", "--once", *extra]
    rc = watched._main_watch_loop(argv)
    return rc


def test_once_verdict_json_and_exit_mapping(wd, fakebin, capsys):
    # 死亡：exit 3 → 退出码 3
    run = start_run("once-dead", ["bash", "-c", "exit 3"], wd,
                    fakebin=fakebin)
    run.proc.wait(timeout=10)
    _wait_for(run.run_dir / "exit_code")
    rc = _once(run.run_dir, wd)
    out = json.loads(capsys.readouterr().out)
    assert rc == EXIT_DEATH
    assert out["verdict"]["status"] == "DEATH"
    assert out["verdict"]["reason"] == "exit_code"
    assert out["snapshot"]["exit_code"] == 3
    run.reap_tree()

    # 静默：日志超帽、心跳新鲜 → 退出码 4
    run = start_run("once-sil", ["bash", "-c", "echo a; sleep 60"], wd,
                    fakebin=fakebin)
    _wait_for(run.run_dir / "stdout")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        age = watched.take_snapshot(_make_cfg(run.run_dir, wd, fakebin,
                                              silence_s=1)).newest_log_age_s
        if age is not None and age > 1.0:
            break
        time.sleep(0.2)
    rc = _once(run.run_dir, wd, "--silence-s", "1")
    out = json.loads(capsys.readouterr().out)
    assert rc == EXIT_SILENCE
    assert out["verdict"]["status"] == "SILENCE"
    run.reap_tree()

    # 健康成功：exit 0 → 退出码 0
    run = start_run("once-ok", ["bash", "-c", "echo hi; exit 0"], wd,
                    fakebin=fakebin)
    run.proc.wait(timeout=10)
    _wait_for(run.run_dir / "exit_code")
    rc = _once(run.run_dir, wd)
    out = json.loads(capsys.readouterr().out)
    assert rc == EXIT_OK and out["verdict"]["status"] == "SUCCESS"
    run.reap_tree()

    # 在跑无终态 → PENDING 映射 5
    run = start_run("once-run", ["bash", "-c", "sleep 60"], wd,
                    fakebin=fakebin)
    time.sleep(0.5)
    rc = _once(run.run_dir, wd)
    out = json.loads(capsys.readouterr().out)
    assert rc == EXIT_PENDING and out["verdict"]["status"] == "PENDING"
    run.reap_tree()

    # --once 是探针：任何分支都不发告警/不建 alerts.jsonl
    assert not (wd / "alerts.jsonl").exists()


def test_once_vanish_runs_grace_recheck_then_death(wd, fakebin):
    """L2 宽限分支：pid 消失无 exit_code，--once 必须先宽限复查再判死。"""
    run = start_run("once-vanish", ["bash", "-c", "sleep 120"], wd,
                    fakebin=fakebin)
    time.sleep(0.5)
    run.kill_supervisor(signal.SIGKILL)   # toucher 经 PDEATHSIG 同死
    run.kill_target(signal.SIGKILL)
    # 等内核回收（含经 subreaper 的僵尸窗）：生产 30s 拍天然跨过，单测显式等
    deadline = time.time() + 5.0
    tpid = run.target_pid
    while time.time() < deadline:
        try:
            os.kill(tpid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("目标 kill -9 后 5s 仍可 kill -0")
    assert not (run.run_dir / "exit_code").exists()
    t0 = time.time()
    rc = _once(run.run_dir, wd)
    elapsed = time.time() - t0
    assert rc == EXIT_DEATH
    assert elapsed >= 1.0  # 生产默认宽限 2.0s，确证走过复查而非单拍即判
    run.reap_tree()


# ---------------------------------------------------------------------------
# AC-T2-7：超时信号必须回主线程（t3 critic 尾债）
# ---------------------------------------------------------------------------

def test_main_thread_watch_timeout_raised_on_main_thread():
    errors = []

    def _hook(args):
        errors.append(args)

    old_hook = threading.excepthook
    threading.excepthook = _hook
    try:
        t0 = time.time()
        with pytest.raises(WatchTimeout, match="信号已回主线程"):
            watched.main_thread_watch(lambda: False, 0.1, what="永真失败条件")
        assert time.time() - t0 < 2.0
        # 子线程没有把 AssertionError 炸进 threading.excepthook
        assert errors == []

        # 子线程内任意异常同样回投主线程重抛
        def boom():
            raise AssertionError("子线程断言必须可见")

        with pytest.raises(AssertionError, match="子线程断言必须可见"):
            watched.main_thread_watch(boom, 1.0)

        # 正常成立不抛
        watched.main_thread_watch(lambda: True, 0.5)
    finally:
        threading.excepthook = old_hook


# ---------------------------------------------------------------------------
# AC-T2-8：元看门狗真实 kill 演练（shell 壳 + 加速档；生产 30s 档另做前台演练）
# ---------------------------------------------------------------------------

def test_meta_guard_real_kill_restart_and_continue(wd, fakebin):
    run = start_run(
        "metaguard",
        ["bash", "-c", "for i in $(seq 1 600); do echo $i; sleep 1; done"],
        wd, fakebin=fakebin)
    ledger = wd / "ledger.jsonl"
    ledger.write_text(json.dumps({"seq": 1}) + "\n")
    pause = wd / "quota_pause.json"
    pause.write_text(json.dumps({"paused": False}))
    p2p = wd / "p2p.jsonl"
    meta_dir = wd / "meta" / run.run_dir.name

    env = _env(fakebin)
    guardian = subprocess.Popen(
        [str(WATCH_WRAPPER), "guard",
         "--run-dir", str(run.run_dir), "--watchdog-root", str(wd),
         "--poll-s", "1", "--guard-poll-s", "1",
         "--notifier", f"fake:{p2p}",
         "--ledger", str(ledger), "--pause-state", str(pause),
         "--silence-s", "1200"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_status():
        deadline = time.time() + 15.0
        while time.time() < deadline:
            sp = meta_dir / "status.json"
            if sp.exists():
                try:
                    return json.loads(sp.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.1)
        raise AssertionError("等不到 guardian status.json")

    try:
        st = wait_status()
        child1 = st["child_pid"]
        # 元心跳键与 run 心跳是两个文件
        meta_hb = meta_dir / "watch_loop.heartbeat"
        _wait_for(meta_hb)
        assert meta_hb.resolve() != (run.run_dir / "heartbeat").resolve()
        assert watched._pid_alive(child1) is True

        # 真实 kill -9 watch-loop 子监控器
        t0 = time.time()
        os.kill(child1, signal.SIGKILL)

        deadline = time.time() + 30.0
        rec = None
        while time.time() < deadline:
            for a in _alerts(wd):
                if a["kind"] == "meta_death":
                    rec = a
                    break
            if rec:
                break
            time.sleep(0.2)
        elapsed = time.time() - t0
        assert rec is not None, "watch_loop 被 kill 后无 meta_death 告警"
        assert rec["reason"] == "watch_loop_dead"
        assert any("监控器已死" in m["text"] for m in _p2p(wd))
        assert Path(rec["bundle_dir"]).is_dir()
        assert elapsed < 30.0  # 加速档（1s 轮询）；生产档窗口 60–120s 由前台演练证

        # 已重启：child pid 变更，restarts 计数
        st2 = wait_status()
        deadline = time.time() + 10.0
        while st2.get("restarts", 0) < 1 and time.time() < deadline:
            st2 = json.loads((meta_dir / "status.json").read_text())
            time.sleep(0.2)
        assert st2["restarts"] >= 1
        child2 = st2["child_pid"]
        assert child2 != child1
        assert watched._pid_alive(child2) is True

        # 续判：原被监控目标此时仍存活并持续出日志
        assert watched._pid_alive(run.target_pid) is True

        # 现在杀目标：重启后的子监控器必须照样判死亡，guardian 以 3 退出
        run.kill_target(signal.SIGKILL)
        rc = guardian.wait(timeout=30.0)
        assert rc == EXIT_DEATH, guardian.stdout.read()
        kinds = [a["kind"] for a in _alerts(wd)]
        assert kinds.count("meta_death") == 1
        assert "death" in kinds
    finally:
        if guardian.poll() is None:
            guardian.kill()
            guardian.wait(timeout=5)
        run.reap_tree()

    # 无遗留：toucher/监控器全部回收
    cp = subprocess.run(["pgrep", "-f", str(run.run_dir)],
                        capture_output=True, text=True)
    assert cp.stdout.strip() == "", f"遗留进程：{cp.stdout}"
