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


def bench_root(*, read_only: bool = False) -> Path:
    """数据根。

    ``read_only=True`` 时**不建目录**（评审 W5-08）：测试与只读消费方必须
    在数据根缺席时得到清晰的「不在场」，而不是被 mkdir 副作用掩盖成缺件。
    """
    raw = os.environ.get(_ENV)
    if raw:
        root = Path(raw)
    else:
        root = phase27_root().parent / "benchmark" / "roadmap_phase0"
    assert_isolated(root)
    if read_only:
        if not root.is_dir():
            raise BenchmarkPathError(f"数据根不在场：{root}（read_only）")
        return root
    return ensure_private_dir(root)


def _forbidden_roots() -> "tuple[tuple[str, Path], ...]":
    """除相位根外，绝对不得被数据根占用/嵌套的仓（评审 W1-07）。

    早先只对五个相位根设防：``SIMPRAXIS_BENCHMARK_ROOT`` 指到 KB 仓或引擎仓
    时一路放行，而 intake/private 全是 mkdir -p —— 一次环境变量笔误就能把
    0700 数据根、私有材料建进**公开 KB 仓**。这里把两处一并纳入禁止集合。
    """
    from orchestrator.benchmark import intake as _intake

    return (("引擎仓", Path(_intake.REPO_ROOT)),
            ("KB 仓", Path(_intake.kb_root())))


def assert_isolated(root: "str | Path" | None = None) -> Path:
    """数据根与五个相位根、引擎仓、KB 仓两两互异且互不嵌套。

    口径与 ``runtime_paths._assert_roots_isolated`` 一致：调用时点 env 解析、
    ``resolve(strict=False)`` 归一（已存在组件中的符号链接被解析）。
    """
    target = Path(root) if root is not None else _default_root()
    rp = target.resolve(strict=False)
    named = [("相位根", Path(p).resolve(strict=False)) for p in _phase_roots()]
    named += [(label, path.resolve(strict=False))
              for label, path in _forbidden_roots()]
    for label, other in named:
        if rp == other:
            raise BenchmarkPathError(
                f"Benchmark 数据根不得与{label}同体：{rp}")
        for a, b in ((rp, other), (other, rp)):
            try:
                a.relative_to(b)
            except ValueError:
                continue
            raise BenchmarkPathError(
                f"Benchmark 数据根不得与{label}互相嵌套：{rp} vs {b}")
    return rp


def _default_root() -> Path:
    raw = os.environ.get(_ENV)
    if raw:
        return Path(raw)
    return phase27_root().parent / "benchmark" / "roadmap_phase0"


RUN_IDENTITY = "run_identity.json"


def _subdir(name: str, *, read_only: bool = False) -> Path:
    root = bench_root(read_only=read_only)
    if read_only:
        return root / name
    return ensure_private_dir(root / name)


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


def runs_dir(*, read_only: bool = False) -> Path:
    return _subdir("runs", read_only=read_only)


def evaluator_results_dir(*, read_only: bool = False) -> Path:
    return _subdir("evaluator_results", read_only=read_only)


def pinned_run(root: "Path | None", task_id: str) -> Path:
    """被判 run 的**唯一**身份解析（评审 W4-11，单一真值）。

    优先 ``closeout/run_identity.json``（或 ``intake/run_identity.json``）的
    钉死登记；无登记时只在恰好一个候选时接受——多候选即抛错，禁止
    「字典序最大者被审」（后跑的诊断 run 会悄悄顶替被判对象）。
    """
    import json

    base = Path(root) if root else bench_root(read_only=True)
    candidates = [base / "closeout" / RUN_IDENTITY,
                  base / "intake" / RUN_IDENTITY]
    identity = next((p for p in candidates if p.is_file()), None)
    if identity is not None:
        doc = json.loads(identity.read_text(encoding="utf-8"))
        run_id = (doc.get("runs") or {}).get(task_id)
        if not run_id:
            raise BenchmarkPathError(f"{task_id}: {identity} 未登记该任务")
        run = base / "runs" / run_id
        if not run.is_dir():
            raise BenchmarkPathError(f"{task_id}: 钉死 run 不存在：{run}")
        return run
    runs = sorted(p for p in (base / "runs").glob(f"{task_id}-*")
                  if p.is_dir())
    if not runs:
        raise BenchmarkPathError(f"{task_id}: 缺 runs/{task_id}-*")
    if len(runs) > 1:
        raise BenchmarkPathError(
            f"{task_id}: runs/ 下有多份候选 {[p.name for p in runs]} 且无 "
            f"{RUN_IDENTITY} 钉死")
    return runs[0]


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
