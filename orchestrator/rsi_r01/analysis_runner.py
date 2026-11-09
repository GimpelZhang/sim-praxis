"""RSI-R0.1 受限分析容器：候选提交 Python 源码，宿主在固定镜像里跑一次。

计划 §5.1 的逐项落地：

* 源码 UTF-8 ≤32 KiB，固定入口 ``prog.py``；候选**不能**选解释器、宿主路径、
  环境变量或 docker 参数；
* 非 root、无网络、只读根、cap-drop ALL、no-new-privileges、无 docker.sock；
  公开观测只读挂载在 ``/data``，程序只读挂载在 ``/work/prog.py``；
* 默认 1 CPU、512 MiB、pids 64、单次 ≤30 s、stdout+stderr 合计 ≤16 KiB；
  每 trial ≤8 次分析；依赖只有冻结的 python/numpy，不临时 pip；
* 输出只允许 ``analysis.json``，单文件 ≤64 KiB；不跟随符号链接，拒绝目录穿越、
  硬链接与超额文件；
* 超时/越权保留事件，**不自动多执行一次**。

隔离适配（锁内适配，2026-11-09）：计划写「独立 tmpfs 输出」。容器 ``--rm`` 且
输出在 tmpfs 里时宿主无法取回结果，故改为**每次新建的空宿主目录**挂到 ``/out``
（0700，容器只写这一处，宿主随后逐件校验文件名/大小/链接/硬链接）。隔离语义
（候选碰不到其他 trial 数据、输出与只读挂载物理分离）不变，且比 tmpfs 更可审计。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01.observations import IMAGE_REF


class AnalysisPolicyViolation(RuntimeError):
    """候选程序违反隔离/配额（该次分析记事件；命中即停该 trial 的进一步分析）。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code


@dataclass
class AnalysisEvent:
    analysis_id: str
    source_sha256: str
    run_ids: list[str]
    exit_code: int | None
    timed_out: bool
    stream_bytes: int
    stream_over_cap: bool
    stdout_preview: str
    stderr_preview: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"schema": S.SCHEMA_ANALYSIS_RESULT,
                "analysis_id": self.analysis_id,
                "source_sha256": self.source_sha256,
                "run_ids": list(self.run_ids),
                "exit_code": self.exit_code,
                "timed_out": self.timed_out,
                "stream_bytes": self.stream_bytes,
                "stream_over_cap": self.stream_over_cap,
                "stdout_preview": self.stdout_preview,
                "stderr_preview": self.stderr_preview,
                "outputs": list(self.outputs),
                "violations": list(self.violations),
                "seconds": round(self.seconds, 3)}


def analysis_argv(*, work_dir: Path, data_dir: Path, out_dir: Path) -> list[str]:
    """分析容器 argv：比实验容器更窄（1 CPU/512 MiB/pids64/无 GPU）。"""
    return [
        "docker", "run", "--rm",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={S.ANALYSIS_PIDS}",
        f"--memory={S.ANALYSIS_MEMORY}",
        f"--cpus={S.ANALYSIS_CPUS}",
        "--read-only",
        "--tmpfs=/tmp:rw,size=64m",
        "--user=1000:1000",
        "-e", "HOME=/tmp",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{work_dir}:/work:ro",
        "-v", f"{data_dir}:/data:ro",
        "-v", f"{out_dir}:/out:rw",
        "-w", "/out",
        IMAGE_REF, "python3", "/work/prog.py",
    ]


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AnalysisRunner:
    """一个 trial 的分析次数与产物记账。"""

    def __init__(self, *, trial_dir: Path, data_dir: Path,
                 max_analyses: int = S.ANALYSIS_MAX_PER_TRIAL):
        self.trial_dir = Path(trial_dir)
        self.data_dir = Path(data_dir)
        self.max_analyses = int(max_analyses)
        self.events: list[AnalysisEvent] = []
        self._counter = 0

    @property
    def used(self) -> int:
        return len(self.events)

    def remaining(self) -> int:
        return self.max_analyses - self.used

    def run(self, source: str, run_ids: Iterable[str], *,
            timeout_s: int = S.ANALYSIS_TIMEOUT_S) -> AnalysisEvent:
        if self.remaining() <= 0:
            raise AnalysisPolicyViolation(
                "ANALYSIS_CAP_EXHAUSTED",
                f"本 trial 已用满 {self.max_analyses} 次分析（不自动追加）")
        raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
        if len(raw) > S.ANALYSIS_MAX_SOURCE_BYTES:
            raise AnalysisPolicyViolation(
                "SOURCE_TOO_LARGE",
                f"源码 {len(raw)} B > {S.ANALYSIS_MAX_SOURCE_BYTES} B")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AnalysisPolicyViolation("SOURCE_NOT_UTF8", str(exc)) from exc
        run_ids = [str(r) for r in run_ids]
        if not run_ids:
            raise AnalysisPolicyViolation("NO_RUN_IDS", "分析必须声明所用 run_id")
        for rid in run_ids:
            # 只允许引用本 trial 已公开的副本名（防目录穿越/跨 trial 读）
            if "/" in rid or "\\" in rid or ".." in rid or not rid.startswith("run_"):
                raise AnalysisPolicyViolation("RUN_ID_ILLEGAL", f"非法 run_id {rid!r}")
            if not (self.data_dir / f"{rid}.json").is_file():
                raise AnalysisPolicyViolation(
                    "RUN_NOT_IN_TRIAL", f"{rid} 不在本 trial 公开观测中")

        self._counter += 1
        aid = f"a{self._counter:02d}"
        root = self.trial_dir / "analysis" / aid
        work = root / "work"
        out = root / "out"
        for d in (work, out):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        prog = work / S.ANALYSIS_ENTRY_NAME
        prog.write_text(text, encoding="utf-8")
        os.chmod(prog, 0o400)

        argv = analysis_argv(work_dir=work, data_dir=self.data_dir, out_dir=out)
        timed_out = False
        exit_code: int | None = None
        stdout = b""
        stderr = b""
        import time as _time

        t0 = _time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout_s)
            stdout, stderr = proc.stdout, proc.stderr
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
        seconds = _time.monotonic() - t0
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()

        stream_total = len(stdout) + len(stderr)
        over = stream_total > S.ANALYSIS_MAX_STREAM_BYTES
        violations: list[str] = []
        if over:
            violations.append("STREAM_OVER_CAP")
        outputs, out_violations = self._collect_outputs(out)
        violations.extend(out_violations)
        event = AnalysisEvent(
            analysis_id=aid, source_sha256=_sha_bytes(raw), run_ids=run_ids,
            exit_code=exit_code, timed_out=timed_out, stream_bytes=stream_total,
            stream_over_cap=over,
            stdout_preview=stdout[:S.ANALYSIS_MAX_STREAM_BYTES // 2].decode(
                "utf-8", "replace"),
            stderr_preview=stderr[:S.ANALYSIS_MAX_STREAM_BYTES // 2].decode(
                "utf-8", "replace"),
            outputs=outputs, violations=violations, seconds=seconds)
        self.events.append(event)
        # 分析目录与事件一并留档（源码审计件；0600）
        (root / "analysis_event.json").write_text(
            S.canonical_json(event.as_dict()) + "\n", encoding="utf-8")
        os.chmod(root / "analysis_event.json", 0o600)
        return event

    def _collect_outputs(self, out: Path) -> tuple[list[dict[str, Any]], list[str]]:
        """逐件校验：只允许固定文件名、无符号链接/硬链接、单件与总量有界。"""
        violations: list[str] = []
        records: list[dict[str, Any]] = []
        entries = sorted(out.iterdir())
        allowed = set(S.ANALYSIS_OUTPUT_NAMES)
        if len(entries) > len(allowed):
            violations.append("TOO_MANY_OUTPUT_FILES")
        byte_total = 0
        for entry in entries:
            st = entry.lstat()
            if stat.S_ISLNK(st.st_mode):
                violations.append(f"SYMLINK_OUTPUT:{entry.name}")
                continue
            if not stat.S_ISREG(st.st_mode):
                violations.append(f"NON_REGULAR_OUTPUT:{entry.name}")
                continue
            if st.st_nlink != 1:
                violations.append(f"HARDLINK_OUTPUT:{entry.name}")
                continue
            if entry.name not in allowed:
                violations.append(f"UNEXPECTED_OUTPUT:{entry.name}")
                continue
            if st.st_size > S.ANALYSIS_MAX_OUTPUT_FILE_BYTES:
                violations.append(f"OUTPUT_OVER_CAP:{entry.name}")
                continue
            blob = entry.read_bytes()
            byte_total += len(blob)
            if byte_total > S.ANALYSIS_MAX_OUTPUT_FILE_BYTES * len(allowed):
                violations.append("OUTPUT_TOTAL_OVER_CAP")
                break
            records.append({"name": entry.name, "bytes": len(blob),
                            "sha256": _sha_bytes(blob)})
            os.chmod(entry, 0o400)
        return records, violations

    def read_output(self, analysis_id: str, name: str = "analysis.json"
                    ) -> dict[str, Any] | None:
        """读候选自报的 analysis.json（**只作候选声明**，判据不信其数值）。"""
        path = self.trial_dir / "analysis" / analysis_id / "out" / name
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return S.read_json(path)
        except (json.JSONDecodeError, OSError):
            return None


def analysis_container_argv_template(*, data_dir: Path) -> list[str]:
    """证据包里复现一次分析所需的 argv 骨架（不含候选源码内容）。"""
    return analysis_argv(work_dir=Path("/work"), data_dir=data_dir,
                         out_dir=Path("/out"))


def docker_available() -> bool:
    return shutil.which("docker") is not None
