"""Phase 2.8.0 数据根的唯一解析入口（D0-1）。

路径优先级：``$SIMPRAXIS_BENCHMARK_ROOT`` > ``phase27_root().parent/"benchmark"/"roadmap_phase0"``。
即缺省 ``/mnt/artifacts/simpraxis/benchmark/roadmap_phase0``。

为什么不在 ``scripts/00_env.sh`` 里加 ``SIMPRAXIS_BENCHMARK_ROOT``：该文件被
「纯加法等式」断言钉着（Phase 2.7 起），改它等于无谓打穿历史断言（C-05）。
本模块自解析，且**不修改** ``runtime_paths.py``。

隔离纪律：数据根必须与 phase1/phase2/phase25/phase26/phase27 五个相位根
**两两互异且互不嵌套**——否则 intake/证据包会落进封存树。任何目录创建之前先断言。
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import (
    phase1_root,
    phase2_root,
    phase25_root,
    phase26_root,
    phase27_root,
)

_ENV = "SIMPRAXIS_BENCHMARK_ROOT"
_SUBDIRS = (
    "intake",
    "specs",
    "fingerprints",
    "preregistration",
    "references",
    "negative_controls",
    "runs",
    "evaluator_results",
    "evidence_pack",
    "reports",
    "closeout",
    "private",
)
_PRIVATE_SUBDIRS = ("hidden", "references", "evaluator_keys")


class BenchmarkPathError(PermissionError):
    """数据根与相位根同体或互相嵌套（任何 mkdir 之前 fail-fast）。"""


def _phase_roots() -> "tuple[Path, ...]":
    return (
        phase1_root(),
        phase2_root(),
        phase25_root(),
        phase26_root(),
        phase27_root(),
    )


def bench_root() -> Path:
    """数据根（不存在时创建，0700）。"""
    raw = os.environ.get(_ENV)
    if raw:
        root = Path(raw)
    else:
        root = phase27_root().parent / "benchmark" / "roadmap_phase0"
    assert_isolated(root)
    return ensure_private_dir(root)


def assert_isolated(root: "str | Path" | None = None) -> Path:
    """数据根与五个相位根两两互异且互不嵌套，否则 ``BenchmarkPathError``。

    口径与 ``runtime_paths._assert_roots_isolated`` 一致：调用时点 env 解析、
    ``resolve(strict=False)`` 归一（已存在组件中的符号链接被解析）。
    """
    target = Path(root) if root is not None else _default_root()
    rp = target.resolve(strict=False)
    for phase_root in _phase_roots():
        other = Path(phase_root).resolve(strict=False)
        if rp == other:
            raise BenchmarkPathError(
                f"Benchmark 数据根不得与相位根同体：{rp}")
        for a, b in ((rp, other), (other, rp)):
            try:
                a.relative_to(b)
            except ValueError:
                continue
            raise BenchmarkPathError(
                f"Benchmark 数据根不得与相位根互相嵌套：{rp} vs {b}")
    return rp


def _default_root() -> Path:
    raw = os.environ.get(_ENV)
    if raw:
        return Path(raw)
    return phase27_root().parent / "benchmark" / "roadmap_phase0"


def _subdir(name: str) -> Path:
    return ensure_private_dir(bench_root() / name)


def intake_dir() -> Path:
    return _subdir("intake")


def cmd_logs_dir() -> Path:
    return ensure_private_dir(intake_dir() / "cmd_logs")


def specs_dir() -> Path:
    return _subdir("specs")


def fingerprints_dir() -> Path:
    return _subdir("fingerprints")


def preregistration_dir() -> Path:
    return _subdir("preregistration")


def references_dir() -> Path:
    return _subdir("references")


def negative_controls_dir() -> Path:
    return _subdir("negative_controls")


def runs_dir() -> Path:
    return _subdir("runs")


def evaluator_results_dir() -> Path:
    return _subdir("evaluator_results")


def evidence_pack_dir() -> Path:
    return _subdir("evidence_pack")


def reports_dir() -> Path:
    return _subdir("reports")


def closeout_dir() -> Path:
    return _subdir("closeout")


def private_dir() -> Path:
    """私有材料根（0700；永不入 git、不挂进 Agent 可写工作目录，G0-3）。"""
    return _subdir("private")


def ensure_layout() -> Path:
    """建齐 12 个子目录与 private 的三个子目录，返回数据根。"""
    root = bench_root()
    for name in _SUBDIRS:
        ensure_private_dir(root / name)
    private = ensure_private_dir(root / "private")
    for name in _PRIVATE_SUBDIRS:
        ensure_private_dir(private / name)
    ensure_private_dir(intake_dir() / "cmd_logs")
    return root


def all_subdirs() -> "tuple[str, ...]":
    """``ensure_layout()`` 负责的 12 个一级子目录名（供 AC 断言使用）。"""
    return _SUBDIRS
