"""Phase 2.5 Task 2 — 看门狗 SOP 核心件（AC-T2-1..8；0 付费、默认 0 触网）。

两件薄壳 ``scripts/ops/run_watched.sh`` / ``scripts/ops/watch_loop.sh`` 分别
exec 本地的 ``run-watched`` / ``watch-loop`` 子命令；元监控经
``watch_loop.sh guard``（meta-guard 子命令），不新增第三个 shell。

设计要点（对应 .omc/handoffs/phase25-t2-watchdog.md）：
- run-watched 七件套（pid/start_iso/cmd_sha256/stdout/stderr/heartbeat/
  exit_code）落 watchdog/runs/<name>-<ts>/；心跳由独立 toucher 小程序每
  HEARTBEAT_INTERVAL_S 刷新，toucher 经 preexec 钩上的
  prctl(PR_SET_PDEATHSIG, SIGKILL) 与父进程内核级同死（AC-T2-1）。
- watch-loop 每轮同时取成功签名与失败/死亡签名；双缺且超静默时限 = 静默。
  失败关键字、良性白名单、静默时限全部集中在本模块常量区，每条来源理由
  随行（AC-T2-2 / AC-T2-5）。
- 触发动作序固定：①P2P 通知（默认 Feishu 免费文本通道；测试一律
  fake/none，真实发送只在 Task 8 drill）→ ②七项排查快照包 →
  ③alerts.jsonl 0600 原子追加；退出码 3=死亡 / 4=静默（AC-T2-3）。
- meta-guard 是「看着看门狗的看门狗」：监控 watch-loop 进程与独立元心跳
  键，watch-loop 被 kill 后发 meta_death 告警/快照并重启，状态全部从
  run-dir 重新推导故可续判（AC-T2-8）。
- main_thread_watch() 把观察线程的超时/异常回投到调用方主线程（AC-T2-7，
  t3 critic 尾债：AssertionError 不再只炸在子线程）。
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.common import fsutil
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.secrets import redact_text

# ---------------------------------------------------------------------------
# 常量区（AC-T2-5：静默时限改动只许在此常量 + silence_limits.json，并随票）
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL_S = 15          # run-watched 心跳节拍（计划 §0.2）
WATCH_POLL_S = 30                  # watch-loop 默认轮询节拍
GUARD_POLL_S = 30                  # meta-guard 默认轮询节拍
# 元心跳超时 = 连续丢 3 拍：30s 轮询下真实 kill 演练落 60–120s 窗（AC-T2-8）
GUARD_MISSED_TICKS = 3
# 被监控 run 心跳超时（toucher 15s 一拍；连续 3 拍 +5s = supervisor 已死）
RUN_HEARTBEAT_TIMEOUT_S = 3 * HEARTBEAT_INTERVAL_S + 5
DEFAULT_SILENCE_S = 600           # 缺省取 GPU 步/评测档（见下表明细）
LOG_SCAN_TAIL_BYTES = 65536       # 失败关键字只扫日志尾部 64KiB
LOG_TAIL_LINES = 100              # 快照包日志尾部行数（AC-T2-3）
DOCKER_LOG_TAIL = 200             # 快照包 docker logs 行数
TOOL_TIMEOUT_S = 15
# kill -0 返回 ESRCH 与 supervisor 写完 exit_code 之间存在 wait()→原子写
# 调度窗：快速成功退出可能先被看见「pid 消失无 exit_code」。process_vanished
# 必须先宽限复查、再跨拍持久确认才准判死（生产 30s 拍 +2s 宽限≈32s，
# 仍在 AC-T2-3 的 60s 触发预算内；测试可经环境变量压缩）。
VANISH_GRACE_S = float(os.environ.get("SIMPRAXIS_WATCHDOG_VANISH_GRACE_S", "2.0"))

# 静默时限表（计划 §0.2 预注册；watchdog/silence_limits.json 由本表播种）
SILENCE_LIMITS: "dict[str, int]" = {
    # 方舟单呼：供应侧已知 120s 超时，外层 5min 无输出即异常。
    "ark_single_call_s": 5 * 60,
    # 容器内 GPU 步/评测：常态步间停顿上限 10min。
    "gpu_step_eval_s": 10 * 60,
    # HF/镜像下载：大文件拉取 15min 无进度才算静默。
    "download_s": 15 * 60,
    # 单轮出生帽：Complete G-4 本地 CPU 出生 605s 量级先例，帽 1200s。
    "single_birth_s": 1200,
}
SILENCE_LIMIT_REASONS: "dict[str, str]" = {
    "ark_single_call_s": "计划 §0.2：供应侧 120s 超时，外层 5min 即异常",
    "gpu_step_eval_s": "计划 §0.2：容器 GPU 步/评测 10min",
    "download_s": "计划 §0.2：HF/镜像下载 15min",
    "single_birth_s": "计划 §0.2 + Complete G-4：CPU 出生 605s 先例，帽 1200s",
}

EXIT_OK = 0
EXIT_DEATH = 3          # 死亡签名（AC-T2-3）
EXIT_SILENCE = 4        # 静默签名（AC-T2-3）
EXIT_PENDING = 5        # 轮询到 max-ticks/停指仍无终态（测试/编排夹具）
EXIT_GUARD_GIVEUP = 6   # meta-guard 重启超限

SEVEN_PIECES = ("pid", "start_iso", "cmd_sha256", "stdout", "stderr",
                "heartbeat", "exit_code")

# 失败关键字（AC-T2-2，收窄带边界；每条带来源理由）。name/正则/理由。
FAILURE_PATTERNS: "tuple[tuple[str, re.Pattern, str], ...]" = (
    ("traceback",
     re.compile(r"\bTraceback \(most recent call last\)"),
     "Python 未捕获异常栈头（Phase 2 长跑多次死亡现场首行）"),
    ("cuda_oom",
     re.compile(r"CUDA out of memory"),
     "PyTorch CUDA OOM 原文（GPU 步死亡签名）"),
    ("killed",
     re.compile(r"(?<![A-Za-z])Killed(?![A-Za-z])"),
     # NF-4：前后边界收窄；不命中 KilledWorker 等复合良性词
     "shell/内核死亡通告（NF-4：边界收窄，KilledWorker 不命中）"),
    ("oom_kill",
     re.compile(r"OOM-kill(?:ed)?"),
     "内核 OOM killer 行文（dmesg/cgroup：OOM-kill/OOM-killed）"),
    ("out_of_memory_error",
     re.compile(r"\bOutOfMemoryError\b"),
     "JVM/框架 OOM 异常类名（torch.cuda/java/lang 共用）"),
    ("exit_code_nonzero",
     re.compile(r"exit code [1-9][0-9]*"),
     # 首位收非 0 数字，不命中 “exit code 0” 成功行
     "runner/docker 非零退出通告（首位非 0，不命中 exit code 0）"),
)

# 良性白名单（行级抑制；每条带来源理由）。命中失败关键字的同一行先过白名单。
_BENIGN_RECOVERED = re.compile(r"recovered|retried OK", re.IGNORECASE)
_BENIGN_DOC_PREFIXES = ('#', '//', '"""', "'''", '*', '---')
BENIGN_RULES: "tuple[tuple[str, object, str], ...]" = (
    ("same_line_recovered",
     _BENIGN_RECOVERED,
     "AC-T2-2：同行含 recovered|retried OK —— 应用层已捕获并继续"),
    ("doc_or_comment_line",
     _BENIGN_DOC_PREFIXES,
     "AC-T2-4⑦：docstring/注释里的 Error/exit code 是源码文本不是故障"),
    # 限制（评审 L4）：只豁免行首即 `"""`/`#` 等前缀的行；docstring 内部
    # 无该前缀的缩进入文档行不豁免。真实 runner stderr 不会产出缩进入文档行，
    # 故漏豁免面可接受，且宁可误报也不放行伪装成文档的失败行。
)

AWAITING_HUMAN_NAME = "awaiting_human"     # 显式等 Gate 态标志文件名
META_NAME = "meta.json"
ALERTS_NAME = "alerts.jsonl"
LIMITS_NAME = "silence_limits.json"
META_DIR_NAME = "meta"
RUNS_DIR_NAME = "runs"
TRIAGE_DIR_NAME = "triage_bundles"

# 快照七项（AC-T2-3；顺序即 manifest.items 顺序，测试按名断言齐套）
TRIAGE_ITEMS = ("ps_tree", "nvidia_smi", "docker_logs", "log_tails",
                "ledger_last3", "quota_pause", "heartbeat_stat")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# toucher 小程序：自身再断言一次 prctl（主钩在父进程 preexec_fn），并每轮
# 核对 ppid，双保险防 fork/prctl 竞态窗口。经 -c 执行，argv：heartbeat 路径、
# 节拍秒、期望父 pid。
_TOUCHER_SRC = """
import ctypes, os, sys, time
hb, interval, expected_ppid = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
libc = ctypes.CDLL("libc.so.6", use_errno=True)
PR_SET_PDEATHSIG = 1
if libc.prctl(PR_SET_PDEATHSIG, 9, 0, 0, 0) != 0:  # 9 = SIGKILL
    os._exit(80)
while True:
    if os.getppid() != expected_ppid:
        os._exit(0)
    try:
        now = time.time()
        os.utime(hb, (now, now))
    except OSError:
        pass
    time.sleep(interval)
"""


# ---------------------------------------------------------------------------
# 纯函数件（故障注入/变异反证/合成日志测试全部直接打这一层，0 子进程）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class FailureHit:
    pattern: str
    line_no: int
    line: str


@dataclasses.dataclass
class ArtifactStatus:
    path: str
    exists: bool
    sha256: "str | None"
    sha_matches: "bool | None"


@dataclasses.dataclass
class ContainerStatus:
    state: str                 # running / exited / gone / unconfigured / error
    detail: str = ""


@dataclasses.dataclass
class Snapshot:
    taken_iso: str
    pid: "int | None"
    pid_alive: "bool | None"
    exit_code: "int | None"
    heartbeat_age_s: "float | None"
    newest_log_age_s: "float | None"
    failure_hits: "list[FailureHit]"
    success_grep_hit: bool
    success_marker: bool
    artifacts: "list[ArtifactStatus]"
    container: ContainerStatus
    awaiting_human: bool
    start_age_s: float
    silence_s: float = DEFAULT_SILENCE_S


@dataclasses.dataclass
class Verdict:
    status: str                # SUCCESS / DEATH / SILENCE / WAITING / PENDING
    reason: str = ""
    detail: dict = dataclasses.field(default_factory=dict)


def _is_benign(line: str) -> bool:
    stripped = line.strip()
    for _name, rule, _reason in BENIGN_RULES:
        if isinstance(rule, re.Pattern):
            if rule.search(line):
                return True
        elif stripped.startswith(rule):  # tuple[str, ...] 前缀表
            return True
    return False


def scan_failure_signatures(text: str) -> "list[FailureHit]":
    """扫日志文本（调用方负责取尾部），返回未被良性白名单抑制的失败命中。"""
    hits: "list[FailureHit]" = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        for name, rx, _reason in FAILURE_PATTERNS:
            if rx.search(line) and not _is_benign(line):
                hits.append(FailureHit(name, line_no, line[:500]))
    return hits


def _artifacts_ok(snap: Snapshot) -> bool:
    return all(a.exists and a.sha_matches for a in snap.artifacts)


def _death_verdict(snap: Snapshot,
                   heartbeat_timeout_s: float) -> "Verdict | None":
    """死亡签名族裁决（评审 M1：成功签名不得短路本族）。

    死亡签名集合：失败关键字非空 / exit_code 不在 (None,0) / pid 已确认消失
    （vanish 两阶段确认在轮询层，classify 见到的 False 即已确认）/
    命名容器 gone|exited / 心跳超时。无任何死亡签名返回 None。
    """
    if snap.failure_hits:
        return Verdict("DEATH", "failure_keyword",
                       {"hits": [dataclasses.asdict(h)
                                 for h in snap.failure_hits[:10]]})
    if snap.exit_code is not None and snap.exit_code != 0:
        return Verdict("DEATH", "exit_code", {"exit_code": snap.exit_code})
    if snap.pid is not None and snap.pid_alive is False and \
            snap.exit_code is None:
        # pid 消失且没留下 exit_code：supervisor 被硬杀一类。
        return Verdict("DEATH", "process_vanished", {"pid": snap.pid})
    if snap.container.state in ("gone", "exited"):
        return Verdict("DEATH", f"container_{snap.container.state}",
                       {"container": snap.container.detail})
    if snap.heartbeat_age_s is not None and \
            snap.heartbeat_age_s > heartbeat_timeout_s:
        # toucher 有 PDEATHSIG 内核兜底：心跳死即 supervisor 死。
        return Verdict("DEATH", "heartbeat_timeout",
                       {"heartbeat_age_s": round(snap.heartbeat_age_s, 1)})
    return None


def classify(snap: Snapshot, *, silence_s: "float | None" = None,
             heartbeat_timeout_s: float = RUN_HEARTBEAT_TIMEOUT_S) -> Verdict:
    """成功/失败双签名同时裁决；双缺且超静默时限 = 静默。"""
    silence = snap.silence_s if silence_s is None else silence_s

    # awaiting_human 是显式合法态：等真人 Gate 不告警（计划 §0.2 第 3 条），
    # 该态优先级最高（阻塞节点等待是合法态，日志静默/心跳均不告警）。
    if snap.awaiting_human:
        return Verdict("WAITING", "awaiting_human")

    # rc=0 路径（含预注册产物 sha fail-closed）保持既有语义，不在 M1 改动面。
    if snap.exit_code == 0:
        if not snap.artifacts:
            return Verdict("SUCCESS", "rc0")
        if _artifacts_ok(snap):
            return Verdict("SUCCESS", "rc0_artifact_sha")
        # rc=0 但预注册产物缺失/sha 不符：fail-closed，不许亮绿。
        return Verdict("DEATH", "artifact_mismatch",
                       {"bad": [a.path for a in snap.artifacts
                                if not (a.exists and a.sha_matches)]})

    death = _death_verdict(snap, heartbeat_timeout_s)

    # marker/grep 成功签名（评审 M1）：不再无条件压过死亡签名。
    # 目标可能先打成功行/写 marker、随后清理段崩溃（rc=137/139、OOM）。
    # 规则：命中 + 任一死亡签名 → 按死亡分类走；命中 + 无死亡签名但进程
    # 未达干净终态（仍活/状态未知，exit_code is None）→ PENDING 继续轮询；
    # 干净终态（exit_code==0）已在上方 rc0 路径返回 SUCCESS。
    if snap.success_marker or snap.success_grep_hit:
        why = "success_marker" if snap.success_marker else "success_grep"
        if death is not None:
            return death
        return Verdict("PENDING", f"{why}_await_terminal")

    if death is not None:
        return death

    # 成功与失败双缺 + 日志 mtime 静默超阈 = 静默（与死亡同级处置）。
    log_age = snap.newest_log_age_s
    if log_age is None:
        log_age = snap.start_age_s     # 从未出过日志：按 run 年龄计
    if log_age > silence:
        return Verdict("SILENCE", "log_mtime_stale",
                       {"log_age_s": round(log_age, 1)})
    return Verdict("PENDING")


# ---------------------------------------------------------------------------
# 原子追加 / 哈希 / 时限播种 / 路径
# ---------------------------------------------------------------------------

def canonical_cmd_sha256(argv: "list[str]") -> str:
    import hashlib
    return hashlib.sha256("\u0000".join(argv).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_append_jsonl(path: Path, obj: dict, *, mode: int = 0o600,
                        newline: bool = True) -> None:
    """单行 O_APPEND 追加（多进程并发安全：单次 write 在本地盘上末尾原子）。

    alerts.jsonl/假网关记录可能被 watch-loop 与 meta-guard 并发追加，
    不能用读出-整写（互丢更新）；权限 0600 显式 fchmod。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, mode)
    try:
        os.fchmod(fd, mode)
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + \
            ("\n" if newline else "")
        data = memoryview(line.encode("utf-8"))
        # CR-41：os.write 可能短写（ENOSPC/EINTR 边界）或返回 0；丢弃返回值
        # 会留下截断行，卡死后续所有 JSONL 读取者。循环写满，0 字节即抛。
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError(f"jsonl 追加短写：写入 {written} 字节（{path}）")
            data = data[written:]
    finally:
        os.close(fd)


def seed_silence_limits(watchdog_root: Path) -> Path:
    """把常量区 SILENCE_LIMITS 播种为 watchdog/silence_limits.json（已存在不动）。"""
    path = watchdog_root / LIMITS_NAME
    if not path.exists():
        payload = {
            "schema": "simpraxis.watchdog_silence_limits.v1",
            "note": "改动须同时改 watched.py 常量并随票留痕（AC-T2-5；本 Task 0 票）",
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "limits": dict(SILENCE_LIMITS),
            "reasons": dict(SILENCE_LIMIT_REASONS),
        }
        fsutil.atomic_write_text(
            path, json.dumps(payload, ensure_ascii=False, indent=2,
                             sort_keys=True) + "\n", mode=0o600)
    return path


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(f"run name 只允许 [A-Za-z0-9_.-]：{name!r}")


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pid_alive(pid: int) -> "bool | None":
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True       # 进程在但不归我属：活
    except OSError:
        return None
    return True


def default_watchdog_root() -> Path:
    try:
        from orchestrator.common import runtime_paths as rp
        return rp.phase26_root() / "watchdog"
    except Exception:
        return Path(os.environ.get("SIMPRAXIS_PHASE26_ROOT", ".")) / \
            "watchdog"


# ---------------------------------------------------------------------------
# AC-T2-1：run-watched 包装器
# ---------------------------------------------------------------------------

def _prctl_pdeathsig_preexec(expected_ppid: int):
    """fork 后、exec 前在子进程里执行：内核级父死 SIGKILL 钩 + 竞态复核。

    先例 scripts/phase2/provision_v1_vram.py:226 同模式（内核 preexec 钩，
    bash 无法设 PR_SET_PDEATHSIG）。
    """
    def _hook() -> None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:
            os._exit(80)
        if os.getppid() != expected_ppid:
            # fork 与 prctl 之间父已死：立刻自毙，不留假活心跳
            os._exit(0)
    return _hook


def run_watched(name: str, cmd: "list[str]", *, cwd: "str | None" = None,
                silence_s: int = DEFAULT_SILENCE_S,
                success_grep: "str | None" = None,
                success_marker: "str | None" = None,
                container: "str | None" = None,
                artifacts: "list[tuple[str, str]] | None" = None,
                watchdog_root: "Path | None" = None) -> int:
    validate_name(name)
    if not cmd:
        raise ValueError("缺少被监控命令（-- 之后的 cmd）")
    wd_root = Path(watchdog_root) if watchdog_root else default_watchdog_root()
    runs_root = wd_root / RUNS_DIR_NAME
    runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runs_root, 0o700)
    seed_silence_limits(wd_root)

    ts = _utc_compact()
    run_dir = runs_root / f"{name}-{ts}"
    suffix = 2
    while run_dir.exists():                 # 同秒碰撞的极小兜底
        run_dir = runs_root / f"{name}-{ts}-{suffix}"
        suffix += 1
    run_dir.mkdir(mode=0o700)
    os.chmod(run_dir, 0o700)

    start_iso = utc_now_iso_z()
    fsutil.atomic_write_text(run_dir / "start_iso", start_iso + "\n", 0o600)
    fsutil.atomic_write_text(run_dir / "cmd_sha256",
                             canonical_cmd_sha256(cmd) + "\n", 0o600)
    meta = {
        "schema": "simpraxis.watchdog_run.v1",
        "name": name,
        "argv": cmd,
        "cwd": str(Path(cwd).resolve() if cwd else Path.cwd()),
        "start_iso": start_iso,
        "silence_s": silence_s,
        "success_grep": success_grep,
        "success_marker": success_marker,
        "container": container,
        "artifacts": [{"path": p, "sha256": s} for p, s in (artifacts or [])],
        "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
    }
    fsutil.atomic_write_text(run_dir / META_NAME,
                             json.dumps(meta, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", 0o600)

    out_f = (run_dir / "stdout").open("wb")
    err_f = (run_dir / "stderr").open("wb")
    for f in (out_f, err_f):
        try:
            os.fchmod(f.fileno(), 0o600)
        except OSError:
            pass

    env = dict(os.environ)
    env["WATCHDOG_RUN_DIR"] = str(run_dir)
    env["WATCHDOG_RUN_NAME"] = name

    interval = float(env.get("SIMPRAXIS_WATCHDOG_HEARTBEAT_S",
                             HEARTBEAT_INTERVAL_S))
    heartbeat = run_dir / "heartbeat"
    heartbeat.touch(mode=0o600, exist_ok=True)

    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=out_f, stderr=err_f)
    fsutil.atomic_write_text(run_dir / "pid", f"{proc.pid}\n", 0o600)

    # CR-14：toucher 启动与 signal.signal 也必须落在 try/finally 内——设置阶段
    # 任一异常（如非主线程调 signal.signal）都要回收被监管子进程与已开文件，
    # 不能留下孤儿 + fd 泄漏。
    toucher: "subprocess.Popen | None" = None
    toucher_err = None
    old_handlers: "dict[int, object]" = {}
    rc: "int | None" = None
    exit_code: "int | None" = None
    try:
        # toucher 独立进程：preexec 上 PDEATHSIG 内核钩（父死即被 SIGKILL）。
        toucher_err = (run_dir / "toucher.err").open("wb")
        try:
            os.fchmod(toucher_err.fileno(), 0o600)
        except OSError:
            pass
        toucher = subprocess.Popen(
            [sys.executable, "-c", _TOUCHER_SRC, str(heartbeat), str(interval),
             str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=toucher_err,
            preexec_fn=_prctl_pdeathsig_preexec(os.getpid()))

        def _forward(signum, _frame):
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

        old_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            # 逐个登记（而非推导式一次赋值）：中途失败时已装上的处理器也能
            # 在 finally 里按表还原，不留半装状态。
            old_handlers[sig] = signal.signal(sig, _forward)
        rc = proc.wait()
    finally:
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
        if rc is None:
            # 设置阶段失败：原异常即将穿出，先终止仍存活的被监管子进程。
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        else:
            exit_code = rc if rc >= 0 else 128 - rc
            try:
                fsutil.atomic_write_text(run_dir / "exit_code",
                                         f"{exit_code}\n", 0o600)
            except OSError:
                # 只读目录故障注入下可能写不进（AC-T2-4③）：不掩盖退出路径
                pass
        for f in (out_f, err_f):
            try:
                f.flush()
            except OSError:
                pass
            f.close()
        if toucher is not None:
            toucher.terminate()
            try:
                toucher.wait(timeout=3)
            except subprocess.TimeoutExpired:
                toucher.kill()
                toucher.wait(timeout=3)
        if toucher_err is not None:
            toucher_err.close()

    return exit_code


# ---------------------------------------------------------------------------
# 探针/通知件（AC-T2-2 容器签名 / AC-T2-3 P2P）
# ---------------------------------------------------------------------------

DockerProbe = Callable[[str], ContainerStatus]


def docker_inspect(container: str, *, timeout: float = TOOL_TIMEOUT_S,
                   env: "dict | None" = None) -> ContainerStatus:
    """docker inspect 命名容器：running / exited / gone / error。"""
    try:
        cp = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=timeout,
            env=env if env is not None else os.environ.copy())
    except FileNotFoundError:
        return ContainerStatus("error", "docker CLI missing")
    except subprocess.TimeoutExpired:
        return ContainerStatus("error", "docker inspect timeout")
    out, err = cp.stdout.strip(), cp.stderr.strip()
    if cp.returncode == 0:
        if out == "true":
            return ContainerStatus("running")
        if out == "false":
            return ContainerStatus("exited")
        return ContainerStatus("error", f"unexpected inspect output: {out[:80]}")
    if "No such object" in err or "No such container" in err:
        return ContainerStatus("gone", container)
    return ContainerStatus("error", err[:200])


class Notifier:
    """P2P 通知接口（假网关与真飞书同形）。"""
    def send(self, text: str) -> str:  # pragma: no cover - 接口
        raise NotImplementedError


class FakeNotifier(Notifier):
    """测试/drill 假网关：每次发送按序落 JSONL（验调用序），零触网。"""
    def __init__(self, path: "str | Path"):
        self.path = Path(path)

    def send(self, text: str) -> str:
        n = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                n = sum(1 for _ in f)
        mid = f"fake-{n + 1:04d}"
        atomic_append_jsonl(self.path,
                            {"message_id": mid, "ts": utc_now_iso_z(),
                             "text": text}, mode=0o600)
        return mid


class NullNotifier(Notifier):
    def send(self, text: str) -> str:
        return "null"


class FeishuP2PNotifier(Notifier):
    """复用 feishu_gateway 免费 P2P 文本通道（不经方舟）；start_ws=False。

    真实发送只在 Task 8 真人 drill；单元测试从不实例化本类（一律 fake/none）。
    """
    def __init__(self, watchdog_root: Path):
        from orchestrator.feishu_gateway.client import FeishuGateway
        app_id = os.environ["FEISHU_APP_ID"]
        app_secret = os.environ["FEISHU_APP_SECRET"]
        owner = os.environ["FEISHU_RECEIVE_ID"]
        evidence = watchdog_root / "feishu_evidence"
        evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._owner = owner
        self._gw = FeishuGateway(
            app_id, app_secret, owner, start_ws=False,
            db_path=str(evidence / "pending.db"),
            evidence_dir=str(evidence))

    def send(self, text: str) -> str:
        return self._gw.send_text(self._owner, text)


def make_notifier(spec: "str | None", watchdog_root: Path) -> Notifier:
    if not spec or spec == "feishu":
        return FeishuP2PNotifier(watchdog_root)
    if spec == "none":
        return NullNotifier()
    if spec.startswith("fake:"):
        return FakeNotifier(spec[len("fake:"):])
    raise ValueError(f"未知 notifier：{spec!r}（允许 feishu|none|fake:PATH）")


# ---------------------------------------------------------------------------
# 快照采集/裁决/触发动作
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class WatchConfig:
    run_dir: Path
    watchdog_root: Path
    silence_s: int = DEFAULT_SILENCE_S
    success_grep: "str | None" = None
    success_marker: "str | None" = None
    artifacts: "list[tuple[str, str]]" = dataclasses.field(default_factory=list)
    container: "str | None" = None
    poll_s: float = WATCH_POLL_S
    max_ticks: "int | None" = None
    heartbeat_timeout_s: float = RUN_HEARTBEAT_TIMEOUT_S
    vanish_grace_s: float = VANISH_GRACE_S
    alerts_path: "Path | None" = None
    triage_root: "Path | None" = None
    ledger_paths: "list[Path]" = dataclasses.field(default_factory=list)
    pause_paths: "list[Path]" = dataclasses.field(default_factory=list)
    notifier: "Notifier | None" = None
    notifier_spec: "str | None" = None
    docker_probe: "DockerProbe | None" = None
    tool_env: "dict | None" = None
    meta_heartbeat: "Path | None" = None
    stop_event: "threading.Event | None" = None


def _default_ledger_path() -> Path:
    try:
        from orchestrator.common import runtime_paths as rp
        return rp.phase26_ledger_path()
    except Exception:
        return Path(os.environ.get("SIMPRAXIS_PHASE26_ROOT", ".")) / \
            "quota" / "ledger.jsonl"


def _default_pause_path() -> Path:
    try:
        from orchestrator.common import runtime_paths as rp
        return rp.phase26_gateway_state_path()
    except Exception:
        return Path(os.environ.get("SIMPRAXIS_PHASE26_ROOT", ".")) / \
            "quota" / "gateway_state" / "quota_pause.json"


def config_from_run_dir(run_dir: Path, watchdog_root: Path,
                        **overrides) -> WatchConfig:
    """从 run-dir meta.json 恢复配置（元监控重启后续判的依据）。"""
    cfg = WatchConfig(
        run_dir=run_dir, watchdog_root=watchdog_root,
        alerts_path=watchdog_root / ALERTS_NAME,
        triage_root=watchdog_root / TRIAGE_DIR_NAME,
        ledger_paths=[_default_ledger_path()],
        pause_paths=[_default_pause_path()],
        meta_heartbeat=watchdog_root / META_DIR_NAME /
        Path(run_dir).name / "watch_loop.heartbeat")
    meta_path = run_dir / META_NAME
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cfg.silence_s = int(meta.get("silence_s", DEFAULT_SILENCE_S))
        cfg.success_grep = meta.get("success_grep")
        cfg.success_marker = meta.get("success_marker")
        cfg.container = meta.get("container")
        cfg.artifacts = [(a["path"], a["sha256"])
                         for a in meta.get("artifacts", [])]
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)
    if cfg.notifier is None and cfg.notifier_spec is not None:
        cfg.notifier = make_notifier(cfg.notifier_spec, watchdog_root)
    return cfg


def _read_tail_text(path: Path, max_bytes: int = LOG_SCAN_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _mtime_age(path: Path, now: float) -> "float | None":
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def take_snapshot(cfg: WatchConfig) -> Snapshot:
    run_dir = cfg.run_dir
    now = time.time()

    pid = None
    pid_alive = None
    try:
        pid = int((run_dir / "pid").read_text().strip())
        pid_alive = _pid_alive(pid)
    except (OSError, ValueError):
        pid_alive = None

    exit_code = None
    try:
        exit_code = int((run_dir / "exit_code").read_text().strip())
    except (OSError, ValueError):
        pass

    start_age = 0.0
    try:
        start_epoch = datetime.strptime(
            (run_dir / "start_iso").read_text().strip(),
            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        start_age = max(0.0, now - start_epoch)
    except (OSError, ValueError):
        pass

    hb_age = _mtime_age(run_dir / "heartbeat", now)

    log_paths = [run_dir / "stdout", run_dir / "stderr"]
    ages = [a for p in log_paths if (a := _mtime_age(p, now)) is not None]
    newest_log_age = min(ages) if ages else None

    tail = _read_tail_text(log_paths[0]) + "\n" + \
        _read_tail_text(log_paths[1])
    failure_hits = scan_failure_signatures(tail)
    success_grep_hit = False
    if cfg.success_grep:
        try:
            success_grep_hit = re.search(cfg.success_grep, tail) is not None
        except re.error:
            success_grep_hit = False

    success_marker = bool(cfg.success_marker and
                          Path(cfg.success_marker).exists())

    artifacts = []
    for raw_path, expected_sha in cfg.artifacts:
        p = Path(raw_path)
        if not p.exists():
            artifacts.append(ArtifactStatus(str(p), False, None, None))
            continue
        actual = sha256_file(p)
        artifacts.append(ArtifactStatus(str(p), True, actual,
                                        actual == expected_sha))

    if cfg.container:
        probe = cfg.docker_probe or docker_inspect
        container_status = probe(cfg.container)
    else:
        container_status = ContainerStatus("unconfigured")

    return Snapshot(
        taken_iso=utc_now_iso_z(), pid=pid, pid_alive=pid_alive,
        exit_code=exit_code, heartbeat_age_s=hb_age,
        newest_log_age_s=newest_log_age, failure_hits=failure_hits,
        success_grep_hit=success_grep_hit, success_marker=success_marker,
        artifacts=artifacts, container=container_status,
        awaiting_human=(run_dir / AWAITING_HUMAN_NAME).exists(),
        start_age_s=start_age, silence_s=float(cfg.silence_s))


def _run_tool(argv: "list[str]", *, env: "dict | None") -> "tuple[bool, str]":
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            timeout=TOOL_TIMEOUT_S,
                            env=env if env is not None else os.environ.copy())
        out = cp.stdout or ""
        if cp.stderr:
            out += "\n[stderr]\n" + cp.stderr
        return cp.returncode == 0, out
    except FileNotFoundError:
        return False, f"tool missing: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, f"tool timeout: {' '.join(argv[:2])}"
    except OSError as exc:
        return False, f"tool error: {exc}"


def _tail_lines(text: str, n: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-n:])


def gather_triage_bundle(cfg: WatchConfig, snapshot: Snapshot, bundle_dir: Path,
                         *, kind: str, reason: str,
                         extra: "dict | None" = None) -> Path:
    """七项排查快照包（AC-T2-3）；单项不可用也落 note，manifest 七名恒齐。"""
    bundle_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(bundle_dir, 0o700)
    items: "list[dict]" = []
    env = cfg.tool_env

    def _dump(item: str, ok: bool, text: str, fname: str, note: str = ""):
        p = bundle_dir / fname
        p.write_text(text, encoding="utf-8")
        os.chmod(p, 0o600)
        items.append({"item": item, "ok": ok, "file": fname, "note": note})

    # 1) ps 树（pid/ppid/pgid/etime，便于追孤儿）
    ok, out = _run_tool(["ps", "-eo", "pid,ppid,pgid,stat,etime,args",
                         "--forest"], env=env)
    _dump("ps_tree", ok, out, "ps_tree.txt",
          "" if ok else "ps 采集失败/缺失")

    # 2) nvidia-smi（只读；无 GPU 环境记不可用，不算失败）
    ok, out = _run_tool(["nvidia-smi"], env=env)
    _dump("nvidia_smi", ok, out, "nvidia_smi.txt",
          "" if ok else "nvidia-smi 不可用（非 GPU 机/未装）")

    # 3) docker logs --tail 200（无命名容器则显式记未配置）
    if cfg.container:
        ok, out = _run_tool(["docker", "logs", "--tail",
                             str(DOCKER_LOG_TAIL), cfg.container], env=env)
        _dump("docker_logs", ok, out, "docker_logs.txt",
              "" if ok else "docker logs 失败")
    else:
        _dump("docker_logs", False, "no --container configured\n",
              "docker_logs.txt", "本 run 未挂命名容器")

    # 4) 日志 tail 100
    tails = ""
    for logname in ("stdout", "stderr"):
        tails += f"===== {logname} tail {LOG_TAIL_LINES} =====\n"
        tails += _tail_lines(_read_tail_text(cfg.run_dir / logname)) + "\n"
    _dump("log_tails", True, tails, "log_tails.txt")

    # 5) ledger 末 3 seq（只读；缺账记错误，不触网不写账）
    ledger_dump = {}
    for lp in cfg.ledger_paths:
        try:
            lines = lp.read_text(encoding="utf-8").splitlines()
            ledger_dump[str(lp)] = lines[-3:]
        except OSError as exc:
            ledger_dump[str(lp)] = {"error": str(exc)}
    _dump("ledger_last3", True,
          json.dumps(ledger_dump, ensure_ascii=False, indent=2),
          "ledger_last3.json")

    # 6) quota pause 态（熔断状态原文复制，0600 不出本机）
    pause_dump = {}
    for pp in cfg.pause_paths:
        try:
            pause_dump[str(pp)] = pp.read_text(encoding="utf-8")
        except OSError as exc:
            pause_dump[str(pp)] = {"error": str(exc)}
    _dump("quota_pause", True,
          json.dumps(pause_dump, ensure_ascii=False, indent=2),
          "quota_pause.json")

    # 7) heartbeat mtime/年龄
    hb_stat = {"heartbeat_age_s": snapshot.heartbeat_age_s,
               "newest_log_age_s": snapshot.newest_log_age_s,
               "pid": snapshot.pid, "pid_alive": snapshot.pid_alive,
               "exit_code": snapshot.exit_code,
               "container": dataclasses.asdict(snapshot.container)}
    _dump("heartbeat_stat", True,
          json.dumps(hb_stat, ensure_ascii=False, indent=2),
          "heartbeat_stat.json")

    manifest = {
        "schema": "simpraxis.watchdog_triage.v1",
        "ts": utc_now_iso_z(), "kind": kind, "reason": reason,
        "run_dir": str(cfg.run_dir), "run_name": cfg.run_dir.name,
        "items": items, "extra": extra or {},
    }
    fsutil.atomic_write_text(
        bundle_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", 0o600)
    assert [i["item"] for i in items] == list(TRIAGE_ITEMS)
    return bundle_dir


def _new_bundle_dir(cfg: WatchConfig) -> Path:
    return cfg.triage_root / f"{cfg.run_dir.name}-{_utc_compact()}-" \
        f"{os.getpid()}-{threading.get_ident() % 10000:04d}"


def _alert_text(kind: str, reason: str, snapshot: Snapshot,
                bundle_dir: Path, meta: bool) -> str:
    who = "监控器已死（meta）" if meta else (
        "进程死亡" if kind == "death" else "进程静默")
    return (f"[SimPraxis 看门狗] {who}：run 快照包 {bundle_dir}；"
            f"reason={reason} pid={snapshot.pid} rc={snapshot.exit_code} "
            f"hb_age={snapshot.heartbeat_age_s} "
            f"log_age={snapshot.newest_log_age_s}")


def fire_alert(cfg: WatchConfig, snapshot: Snapshot, verdict: Verdict, *,
               kind: str, meta: bool = False) -> dict:
    """固定动作序：①P2P（含预定 bundle 路径）→ ②快照包 → ③alerts 原子行。

    CR-09：P2P 通道失败（网络/HTTP/令牌过期）绝不能吞掉证据。send 异常记入
    ``p2p_error``（脱敏）后**无条件**继续落快照包与 alerts.jsonl 行；否则一次
    真实运行死亡会零告警证据，且异常穿出 watch-loop 把两个看门狗一起打死。
    """
    notifier = cfg.notifier or NullNotifier()
    cfg.triage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle_dir = _new_bundle_dir(cfg)
    # ① 先通知（bundle 路径确定性生成，60s 内分诊包必可达）
    message_id = None
    p2p_error = None
    try:
        message_id = notifier.send(
            _alert_text(kind, verdict.reason, snapshot, bundle_dir, meta))
    except Exception as exc:  # noqa: BLE001 —— 告警通道失败不阻断证据落盘
        p2p_error = redact_text(f"{type(exc).__name__}: {exc}")[:300]
    # ② 快照包
    gather_triage_bundle(cfg, snapshot, bundle_dir, kind=kind,
                         reason=verdict.reason,
                         extra={"message_id": message_id,
                                "p2p_error": p2p_error})
    # ③ alerts.jsonl（0600 O_APPEND 原子追加）
    record = {
        "schema": "simpraxis.watchdog_alert.v1",
        "ts": utc_now_iso_z(),
        "kind": "meta_death" if meta else kind,
        "run_name": cfg.run_dir.name,
        "run_dir": str(cfg.run_dir),
        "reason": verdict.reason,
        "detail": verdict.detail,
        "pid": snapshot.pid,
        "exit_code": snapshot.exit_code,
        "container": dataclasses.asdict(snapshot.container),
        "heartbeat_age_s": snapshot.heartbeat_age_s,
        "newest_log_age_s": snapshot.newest_log_age_s,
        "failure_hits": [dataclasses.asdict(h)
                         for h in snapshot.failure_hits[:10]],
        "bundle_dir": str(bundle_dir),
        "p2p_message_id": message_id,
        "p2p_error": p2p_error,
    }
    atomic_append_jsonl(cfg.alerts_path, record, mode=0o600)
    return record


def _touch_meta_heartbeat(cfg: WatchConfig) -> None:
    if not cfg.meta_heartbeat:
        return
    p = cfg.meta_heartbeat
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        now = time.time()
        os.utime(p, (now, now))
    except OSError:
        try:
            p.touch(mode=0o600, exist_ok=True)
        except OSError:
            pass


def _vanish_grace_recheck(cfg: WatchConfig,
                          snap: Snapshot) -> "tuple[Snapshot, Verdict]":
    """process_vanished 首见：宽限后重采一轮，让 supervisor 落盘 exit_code。"""
    if cfg.stop_event is not None:
        if cfg.stop_event.wait(timeout=cfg.vanish_grace_s):
            return snap, Verdict("PENDING", "stop_event_set_during_grace")
    else:
        time.sleep(cfg.vanish_grace_s)
    snap2 = take_snapshot(cfg)
    return snap2, classify(snap2,
                           heartbeat_timeout_s=cfg.heartbeat_timeout_s)


def run_watch_loop(cfg: WatchConfig) -> int:
    ticks = 0
    vanish_armed = False
    while True:
        _touch_meta_heartbeat(cfg)
        snap = take_snapshot(cfg)
        verdict = classify(snap,
                           heartbeat_timeout_s=cfg.heartbeat_timeout_s)

        # process_vanished 两阶段确认：首见宽限复查（排除 wait()→exit_code
        # 落盘竞争窗），下一拍仍消失才判死（见 VANISH_GRACE_S 注释）。
        # CR-10：置位后必须睡一拍再复查——旧实现置位后直接落入下方通用 DEATH
        # 分支，vanish_armed 永远观察不到 True（宽限窗实际只剩 2s）。
        vanish_deferred = False
        if verdict.status == "DEATH" and verdict.reason == "process_vanished":
            if vanish_armed:
                fire_alert(cfg, snap, verdict, kind="death")
                return EXIT_DEATH
            snap, verdict = _vanish_grace_recheck(cfg, snap)
            if verdict.status == "DEATH" and \
                    verdict.reason == "process_vanished":
                vanish_armed = True
                vanish_deferred = True

        if verdict.status == "SUCCESS":
            return EXIT_OK
        if verdict.status in ("DEATH", "SILENCE") and not vanish_deferred:
            kind = "death" if verdict.status == "DEATH" else "silence"
            fire_alert(cfg, snap, verdict, kind=kind)
            return EXIT_DEATH if kind == "death" else EXIT_SILENCE
        # WAITING / PENDING（含已 arm 的消失首见）继续轮询；等 Gate 态不告警。
        ticks += 1
        if cfg.max_ticks is not None and ticks >= cfg.max_ticks:
            return EXIT_PENDING
        if cfg.stop_event is not None:
            if cfg.stop_event.wait(timeout=cfg.poll_s):
                return EXIT_PENDING
        else:
            time.sleep(cfg.poll_s)


# ---------------------------------------------------------------------------
# AC-T2-8：meta-guard（看门狗的看门狗）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class GuardConfig:
    cfg: WatchConfig
    guard_poll_s: float = GUARD_POLL_S
    missed_ticks: int = GUARD_MISSED_TICKS
    restart_limit: int = 3
    env: "dict | None" = None
    stop_event: "threading.Event | None" = None


def _guard_write_status(path: Path, payload: dict) -> None:
    fsutil.atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2,
                         sort_keys=True) + "\n", 0o600)


def _guard_child_argv(cfg: WatchConfig, hb: Path) -> "list[str]":
    argv = [sys.executable, "-m", "orchestrator.common.watched",
            "watch-loop",
            "--run-dir", str(cfg.run_dir),
            "--watchdog-root", str(cfg.watchdog_root),
            "--poll-s", str(cfg.poll_s),
            "--silence-s", str(cfg.silence_s),
            "--heartbeat-timeout-s", str(cfg.heartbeat_timeout_s),
            "--meta-heartbeat", str(hb)]
    if cfg.success_grep:
        argv += ["--success-grep", cfg.success_grep]
    if cfg.success_marker:
        argv += ["--success-marker", cfg.success_marker]
    if cfg.container:
        argv += ["--container", cfg.container]
    if cfg.notifier_spec:
        argv += ["--notifier", cfg.notifier_spec]
    for lp in cfg.ledger_paths:
        argv += ["--ledger", str(lp)]
    for pp in cfg.pause_paths:
        argv += ["--pause-state", str(pp)]
    for raw_path, sha in cfg.artifacts:
        argv += ["--artifact", f"{raw_path}:{sha}"]
    return argv


def run_meta_guard(gcfg: GuardConfig) -> int:
    cfg = gcfg.cfg
    child_env = gcfg.env if gcfg.env is not None else os.environ.copy()
    meta_dir = cfg.watchdog_root / META_DIR_NAME / cfg.run_dir.name
    meta_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(meta_dir, 0o700)
    hb = meta_dir / "watch_loop.heartbeat"
    status_path = meta_dir / "status.json"
    cfg.meta_heartbeat = hb

    def _spawn(seq: int) -> subprocess.Popen:
        log = (meta_dir / f"child-{seq}.log").open("wb")
        return subprocess.Popen(_guard_child_argv(cfg, hb),
                                stdout=log, stderr=subprocess.STDOUT,
                                env=child_env)

    child = _spawn(0)
    spawn_ts = time.time()
    restarts = 0
    while True:
        if gcfg.stop_event is not None:
            if gcfg.stop_event.wait(timeout=gcfg.guard_poll_s):
                child.terminate()
                return EXIT_PENDING
        else:
            time.sleep(gcfg.guard_poll_s)

        rc = child.poll()
        threshold = gcfg.missed_ticks * gcfg.guard_poll_s
        hb_age = _mtime_age(hb, time.time())
        # 子监控器若在首拍前就崩（心跳文件未建），用出生年龄兜底计时
        effective_age = hb_age if hb_age is not None else \
            max(0.0, time.time() - spawn_ts)
        _guard_write_status(status_path, {
            "guardian_pid": os.getpid(), "child_pid": child.pid,
            "child_exit": rc, "restarts": restarts,
            "last_tick_iso": utc_now_iso_z(),
            "heartbeat_age_s": hb_age, "death_threshold_s": threshold})

        if rc is not None:
            if rc == EXIT_OK:
                return EXIT_OK
            if rc in (EXIT_DEATH, EXIT_SILENCE):
                return rc          # 子监控器已完成告警动作，不重复发
        # 死亡判定以【独立元心跳键】为准（AC-T2-8）：rc 异常（信号杀死/崩溃）
        # 或进程仍在但心跳连续丢拍（监控循环卡死），都须等心跳过阈才动作。
        # 30s 档下这给出真实 kill 后 60–120s 告警窗；干净退出 0/3/4 立即传播。
        if effective_age < threshold:
            continue
        if rc is None:
            # 进程还在但监控循环卡死：硬杀后按死亡处置
            child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        hb_age = effective_age

        # meta 告警 + 分诊包（独立元心跳键，kind=meta_death），然后重启。
        restarts += 1
        snap = Snapshot(
            taken_iso=utc_now_iso_z(),
            pid=child.pid, pid_alive=False, exit_code=rc,
            heartbeat_age_s=hb_age, newest_log_age_s=None,
            failure_hits=[], success_grep_hit=False,
            success_marker=False, artifacts=[],
            container=ContainerStatus("unconfigured"),
            awaiting_human=False, start_age_s=0.0,
            silence_s=float(cfg.silence_s))
        verdict = Verdict("DEATH", "watch_loop_dead",
                          {"child_exit": rc, "restarts": restarts})
        fire_alert(cfg, snap, verdict, kind="death", meta=True)
        if restarts > gcfg.restart_limit:
            return EXIT_GUARD_GIVEUP
        # 重启：配置全部从 run-dir/meta 再推导，原 run 状态可续判。
        child = _spawn(restarts)
        spawn_ts = time.time()


# ---------------------------------------------------------------------------
# AC-T2-7：超时信号回主线程的观察原语（t3 critic 尾债收口）
# ---------------------------------------------------------------------------

class WatchTimeout(TimeoutError):
    """观察窗超时；保证在调用方（主）线程上抛出。"""


def main_thread_watch(predicate: "Callable[[], bool]", timeout_s: float, *,
                      tick_s: float = 0.01,
                      what: str = "condition") -> None:
    """等 predicate 真；超时/子线程异常一律在【调用方线程】重抛。

    旧 t3 watcher 的 AssertionError 只炸在子线程、主线程不可观测（Complete
    §5 尾债）。这里观察子线程只做等待与判定，任何失败经 Queue 回投，由调用
    方线程在 outcome.get 处接收，绝不依赖 threading.excepthook。
    """
    outcome: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
    stop = threading.Event()

    def _observe() -> None:
        try:
            while not stop.is_set():
                if predicate():
                    outcome.put(("ok", True))
                    return
                stop.wait(tick_s)
        except BaseException as exc:   # 含 AssertionError：回投不吞
            outcome.put(("error", exc))

    t = threading.Thread(target=_observe, daemon=True,
                         name="main-thread-watch")
    t.start()
    try:
        kind, payload = outcome.get(timeout=timeout_s)
    except queue.Empty:
        stop.set()
        t.join(timeout=1.0)
        raise WatchTimeout(
            f"等待 {what} 超时（{timeout_s}s）；信号已回主线程") from None
    stop.set()
    t.join(timeout=1.0)
    if kind == "error":
        raise payload  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main_run_watched(argv: "list[str]") -> int:
    # 合同用法是 `<name> [options] -- <cmd>`：name 在最前、选项在中间，
    # argparse REMAINDER 会从第一个选项起整段吞掉，故先在 "--" 处切分。
    if "--" not in argv:
        raise SystemExit("run-watched: 必须用 -- 分隔被监控命令")
    cut = argv.index("--")
    left, rest = argv[:cut], argv[cut + 1:]
    p = argparse.ArgumentParser(prog="watched run-watched")
    p.add_argument("name")
    p.add_argument("--cwd")
    p.add_argument("--silence-s", type=int, default=DEFAULT_SILENCE_S)
    p.add_argument("--success-grep", default=None)
    p.add_argument("--success-marker", default=None)
    p.add_argument("--container", default=None)
    p.add_argument("--artifact", action="append", default=[],
                   help="可重复 PATH:sha256（rc=0 且产物 sha 对才算成功）")
    p.add_argument("--watchdog-root", default=None)
    ns = p.parse_args(left)
    if not rest:
        p.error("缺少 -- <cmd>")
    artifacts = []
    for item in ns.artifact:
        if ":" not in item:
            p.error("--artifact 形式为 PATH:sha256")
        path, sha = item.rsplit(":", 1)
        artifacts.append((path, sha))
    root = Path(ns.watchdog_root) if ns.watchdog_root else None
    return run_watched(
        ns.name, rest, cwd=ns.cwd, silence_s=ns.silence_s,
        success_grep=ns.success_grep, success_marker=ns.success_marker,
        container=ns.container, artifacts=artifacts, watchdog_root=root)


def _add_watch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-dir", required=True)
    p.add_argument("--watchdog-root", default=None)
    p.add_argument("--silence-s", type=int, default=None)
    p.add_argument("--success-grep", default=None)
    p.add_argument("--success-marker", default=None)
    p.add_argument("--container", default=None)
    p.add_argument("--artifact", action="append", default=[])
    p.add_argument("--poll-s", type=float, default=WATCH_POLL_S)
    p.add_argument("--max-ticks", type=int, default=None)
    p.add_argument("--heartbeat-timeout-s", type=float,
                   default=RUN_HEARTBEAT_TIMEOUT_S)
    p.add_argument("--meta-heartbeat", default=None)
    p.add_argument("--notifier", default="feishu",
                   help="feishu（默认）| none | fake:PATH")
    p.add_argument("--ledger", action="append", default=None)
    p.add_argument("--pause-state", action="append", default=None)
    p.add_argument("--once", action="store_true",
                   help="只采一轮、打印裁决 JSON、映射退出码后退出")


def _build_cfg_from_args(ns: argparse.Namespace) -> WatchConfig:
    run_dir = Path(ns.run_dir)
    wd_root = Path(ns.watchdog_root) if ns.watchdog_root \
        else default_watchdog_root()
    artifacts = []
    for item in ns.artifact:
        if ":" not in item:
            raise SystemExit("--artifact 形式为 PATH:sha256")
        path, sha = item.rsplit(":", 1)
        artifacts.append((path, sha))
    overrides = {
        "silence_s": ns.silence_s,
        "success_grep": ns.success_grep,
        "success_marker": ns.success_marker,
        "container": ns.container,
        "artifacts": artifacts or None,
        "poll_s": ns.poll_s if ns.poll_s != WATCH_POLL_S else None,
        "max_ticks": ns.max_ticks,
        "heartbeat_timeout_s": ns.heartbeat_timeout_s
        if ns.heartbeat_timeout_s != RUN_HEARTBEAT_TIMEOUT_S else None,
        "notifier_spec": ns.notifier if ns.notifier != "feishu" else None,
        "ledger_paths": [Path(x) for x in ns.ledger] if ns.ledger else None,
        "pause_paths": [Path(x) for x in ns.pause_state]
        if ns.pause_state else None,
        "meta_heartbeat": Path(ns.meta_heartbeat) if ns.meta_heartbeat
        else None,
    }
    cfg = config_from_run_dir(run_dir, wd_root, **overrides)
    # CLI 默认真飞书（notifier_spec None 经覆盖表被跳过，这里显式建）
    if cfg.notifier is None and ns.notifier == "feishu":
        cfg.notifier = make_notifier("feishu", wd_root)
    return cfg


def _main_watch_loop(argv: "list[str]") -> int:
    p = argparse.ArgumentParser(prog="watched watch-loop")
    _add_watch_args(p)
    ns = p.parse_args(argv)
    cfg = _build_cfg_from_args(ns)
    if ns.once:
        _touch_meta_heartbeat(cfg)
        snap = take_snapshot(cfg)
        verdict = classify(snap,
                           heartbeat_timeout_s=cfg.heartbeat_timeout_s)
        # 单次探针同样排除 wait()→exit_code 落盘竞争窗（模板 C cron 防假红）。
        if verdict.status == "DEATH" and verdict.reason == "process_vanished":
            snap, verdict = _vanish_grace_recheck(cfg, snap)
        out = {"verdict": dataclasses.asdict(verdict),
               "snapshot": {k: v for k, v in dataclasses.asdict(snap).items()
                            if k not in ("failure_hits", "artifacts",
                                         "container")}}
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return {"SUCCESS": EXIT_OK, "DEATH": EXIT_DEATH,
                "SILENCE": EXIT_SILENCE, "WAITING": EXIT_PENDING,
                "PENDING": EXIT_PENDING}[verdict.status]
    return run_watch_loop(cfg)


def _main_meta_guard(argv: "list[str]") -> int:
    p = argparse.ArgumentParser(prog="watched meta-guard")
    _add_watch_args(p)
    p.add_argument("--guard-poll-s", type=float, default=GUARD_POLL_S)
    p.add_argument("--restart-limit", type=int, default=3)
    ns = p.parse_args(argv)
    cfg = _build_cfg_from_args(ns)
    gcfg = GuardConfig(cfg, guard_poll_s=ns.guard_poll_s,
                       missed_ticks=GUARD_MISSED_TICKS,
                       restart_limit=ns.restart_limit)
    return run_meta_guard(gcfg)


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: watched {run-watched|watch-loop|meta-guard} ...",
              file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "run-watched":
        return _main_run_watched(rest)
    if cmd == "watch-loop":
        return _main_watch_loop(rest)
    if cmd == "meta-guard":
        return _main_meta_guard(rest)
    print(f"未知子命令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
