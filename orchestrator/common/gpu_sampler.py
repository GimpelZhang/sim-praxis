#!/usr/bin/env python3
"""同法 VRAM 口径的设备级 GPU 显存采样 wrapper（V1 冻结回放与 V2 批次同包裹）。

Task6 ascent 的显存裁决只信**设备实测**：本 wrapper 前置于真实 batch 命令，
轮询设备 0 的 pynvml usedMemory 取峰值，子进程结束后把
``{"vram_peak_gb": 正有限实数, ...}`` 以 0600 原子写落 sidecar。V1 冻结回放
（scripts/phase2/provision_v1_vram.py）与 V2 batch（task6 工厂）使用**同一个
wrapper、同一台机器、同一个设备口径**，两侧峰值可比；冻结 V1 本体零仪器化
（禁止改冻结脚本），也不信任何子进程自报显存。

锁内适配（计划只规定 sidecar 契约 {"vram_peak_gb": 正有限实数}，未规定仪器
形态与采样点）：设备级轮询而非按 PID 归因——本机基线约 14 MiB 且无他方 GPU
使用者，设备口径两侧同法即可比；不需要 psutil。

安全/工程性质：
  - **自包含**：只依赖 stdlib + pynvml，绝不 import 本仓任何包。runner 的
    cwd=付费 workdir，本文件以绝对路径启动，不依赖 cwd/sys.path。
  - 先 nvmlInit/开设备，失败退出 3：绝不启动子进程、绝不写 sidecar。
  - 子进程以 start_new_session=True 独立进程组启动；SIGTERM/SIGINT 转发到
    整个进程组，runner 超时杀 wrapper 时不留孤儿子进程继续占 GPU。
  - 子进程无论何种 rc 结束，只要峰值是正有限实数就原子写 0600 sidecar，
    wrapper 以子进程 rc 退出（被信号杀折算 128+signum）；峰值非法不写
    sidecar、退出 4（fail-closed，task6 接缝缺件即 GraphStateError）。
  - Popen 失败（入口不存在，如 V2 出生前首跑）退出 127、不写 sidecar。

CLI：
  gpu_sampler.py --sidecar <path> [--interval 0.5] -- <child argv...>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

EXIT_USAGE = 2
EXIT_NVML = 3
EXIT_BAD_PEAK = 4
EXIT_NOT_FOUND = 127

_GIB = 1024 ** 3

# 契约主键（task6._vram_gb 只认真实 sidecar 的此键，正有限实数）。
VRAM_PEAK_KEY = "vram_peak_gb"


# ---------------------------------------------------------------------------
# 0600 原子写（与 orchestrator/common/fsutil 同一时序，但本模块禁止依赖本仓；
# runner cwd=workdir，本文件必须能独立以绝对路径启动）
# ---------------------------------------------------------------------------

def _atomic_write_0600(path: Path, payload: bytes) -> None:
    """mkstemp（创建即 0600）→ fchmod 0600 → 写/fsync → os.replace。"""
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"sidecar 父目录不存在：{parent}")
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=str(parent))
    try:
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        if os.path.exists(tmp):  # 极端异常窗口的防御性兜底
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 信号转发：handler 只负责 killpg；wait/写 sidecar 走主流程，逻辑不重入
# ---------------------------------------------------------------------------

def _forward_signal(proc: subprocess.Popen, signum: int) -> None:
    """把信号转发给子进程整个进程组（start_new_session 建立的独立 pgrp）。"""
    try:
        os.killpg(proc.pid, signum)
    except ProcessLookupError:
        # 子进程已退出：主流程的 wait 立刻收尾，无需再杀。
        pass


class _SampleSink:
    """daemon 采样线程与主线程之间的最小共享态。"""

    def __init__(self) -> None:
        self.baseline: int | None = None
        self.peak = 0
        self.samples = 0
        self.error: Exception | None = None

    def add(self, used_bytes: Any) -> None:
        # usedMemory.used 实测为 int；NaN/非数/负数一律视为仪器失败。
        try:
            used = int(used_bytes)
        except (TypeError, ValueError, OverflowError) as exc:
            self.error = exc
            return
        if used < 0:
            self.error = ValueError(f"usedMemory 为负：{used_bytes!r}")
            return
        if self.baseline is None:
            self.baseline = used
        self.peak = max(self.peak, used)
        self.samples += 1


def _sample_loop(proc: subprocess.Popen, nvml: Any, handle: Any,
                 interval: float, sink: _SampleSink) -> None:
    """先采一次再查存活：保证 samples>=1；子进程在则每 interval 采一次。"""
    while sink.error is None:
        try:
            used = nvml.nvmlDeviceGetMemoryInfo(handle).used
        except Exception as exc:  # 仪器失败 fail-closed：宁可缺 sidecar
            sink.error = exc
            return
        sink.add(used)
        if proc.poll() is not None:
            return
        time.sleep(interval)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpu_sampler.py",
        description="设备级 GPU 显存峰值采样 wrapper（V1/V2 同法口径）")
    parser.add_argument("--sidecar", required=True,
                        help="vram_peak.json sidecar 输出路径")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="采样间隔秒（默认 0.5）")
    parser.add_argument("child", nargs=argparse.REMAINDER,
                        help="`--` 后为被包裹的真实 batch 命令")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    # argparse REMAINDER 会把分隔符 '--' 原样留在首位。
    child_args = args.child[1:] if args.child[:1] == ["--"] else list(args.child)
    if not child_args:
        print("用法：gpu_sampler.py --sidecar <path> [--interval 0.5] "
              "-- <child argv...>（`--` 后命令为空）", file=sys.stderr)
        return EXIT_USAGE
    if not math.isfinite(args.interval) or args.interval <= 0.0:
        print(f"gpu_sampler: --interval 必须是正实数，收到 {args.interval!r}",
              file=sys.stderr)
        return EXIT_USAGE

    sidecar = Path(args.sidecar)

    # 延迟导入：先确认仪器可用，再启动任何子进程。测试以 sys.modules 注入
    # 假 pynvml（全 hermetic、禁真 GPU）。
    try:
        import pynvml  # noqa: PLC0415
    except ImportError as exc:
        print(f"gpu_sampler: pynvml 不可用：{exc}", file=sys.stderr)
        return EXIT_NVML
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        device_name = pynvml.nvmlDeviceGetName(handle)
    except Exception as exc:
        print(f"gpu_sampler: NVML 初始化/设备 0 打开失败：{exc}", file=sys.stderr)
        return EXIT_NVML
    if isinstance(device_name, bytes):
        device_name = device_name.decode("utf-8", "replace")

    started = False
    try:
        # 不指定 cwd/env：继承父进程（runner 已设 cwd=workdir、env_extra）；
        # stdout/stderr 继承父 fd，runner 的 capture_output 管道自然捕获。
        # start_new_session：独立进程组，信号按组转发，防孤儿占 GPU。
        try:
            proc = subprocess.Popen(child_args, start_new_session=True)
        except FileNotFoundError as exc:
            print(f"gpu_sampler: 被包裹命令不存在：{exc}", file=sys.stderr)
            return EXIT_NOT_FOUND
        started = True

        def _handler(signum: int, _frame: Any) -> None:
            _forward_signal(proc, signum)

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

        sink = _SampleSink()
        t0 = time.monotonic()
        sampler = threading.Thread(
            target=_sample_loop,
            args=(proc, pynvml, handle, args.interval, sink),
            daemon=True)
        sampler.start()

        raw_rc = proc.wait()
        sampler.join(timeout=max(args.interval + 1.0, 1.0))
        elapsed_s = time.monotonic() - t0
        child_rc = raw_rc if raw_rc >= 0 else 128 + (-raw_rc)

        if sink.error is not None:
            print(f"gpu_sampler: 设备显存采样失败，不写 sidecar：{sink.error}",
                  file=sys.stderr)
            return EXIT_BAD_PEAK
        peak_gb = sink.peak / _GIB
        if sink.samples < 1 or not math.isfinite(peak_gb) or peak_gb <= 0.0:
            print(
                f"gpu_sampler: 显存峰值非法（samples={sink.samples}, "
                f"peak={sink.peak} bytes），不写 sidecar", file=sys.stderr)
            return EXIT_BAD_PEAK
        baseline_gb = (sink.baseline or 0) / _GIB

        payload = {
            VRAM_PEAK_KEY: peak_gb,
            "vram_baseline_gb": baseline_gb,
            "samples": sink.samples,
            "device_name": str(device_name),
            "child_rc": child_rc,
            "elapsed_s": elapsed_s,
        }
        _atomic_write_0600(
            sidecar, json.dumps(payload, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8"))
        return child_rc
    finally:
        if started:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
