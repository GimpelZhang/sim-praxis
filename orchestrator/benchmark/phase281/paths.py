"""Phase 2.8.1 数据根的唯一解析入口（D0-1）。

优先级：``$SIMPRAXIS_PHASE281_ROOT`` > ``phase1_root().parent/"phase281"``，
即缺省 ``/mnt/artifacts/simpraxis/phase281``（大文件只落 ``/mnt``）。

三层隔离纪律（每次 ``ensure_layout()`` 之前必调 ``assert_isolated()``）：

1. 与五个相位根两两互异且互不嵌套 —— 调 ``runtime_paths.assert_phase281_roots_isolated()``
   （唯一实现，不在此重写判定）；
2. 与**引擎仓**（``intake.REPO_ROOT``）、**KB 仓**（``intake.kb_root()``）互不嵌套 ——
   否则 0700 数据根会被建进公开仓或引擎仓；
3. ``SIMPRAXIS_BENCHMARK_ROOT`` 在本阶段任何进程里**必须未设置** ——
   ``orchestrator.benchmark.paths._phase_roots()`` 是 Phase 2.8.0 的模块，
   不在本阶段白名单内，不认识 phase281，指错时它的 ``assert_isolated()`` 不拦。
   本阶段用入口断言兜住（AC-P281-0-13），残差登记在 P281-9 的 Open Items。

**只读语义**：``read_only=True`` 时不建目录、不 ``ensure_private_dir``；根不在场
即抛 ``Phase281PathError``。测试与只读消费方必须拿到清晰的「不在场」，而不是被
mkdir 副作用掩盖成缺件（同 Phase 2.8.0 评审 W5-08 的口径）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import (
    assert_phase281_roots_isolated,
    phase281_approvals_dir,
    phase281_counters_path,
    phase281_gateway_state_dir,
    phase281_gateway_state_path,
    phase281_ledger_path,
    phase281_quota_dir,
    phase281_root as _rp_phase281_root,
    phase281_runs_root,
)

__all__ = (
    "BENCHMARK_ROOT_ENV",
    "Phase281PathError",
    "TrialPaths",
    "analysis_dir",
    "approvals_dir",
    "assert_isolated",
    "assert_no_benchmark_root_env",
    "candidate_cache_dir",
    "closeout_dir",
    "counters_path",
    "dryrun_dir",
    "ensure_layout",
    "evaluator_results_dir",
    "evidence_pack_dir",
    "gateway_state_dir",
    "gateway_state_path",
    "intake_dir",
    "ledger_path",
    "phase281_root",
    "pre_registration_dir",
    "private_dir",
    "quota_dir",
    "references_dir",
    "reports_dir",
    "runs_dir",
    "trial_dir",
    "trial_paths",
    "watchdog_dir",
)

#: Phase 2.8.0 的数据根 env：本阶段进程里**必须未设置**（见模块 docstring 第 3 条）
BENCHMARK_ROOT_ENV = "SIMPRAXIS_BENCHMARK_ROOT"

#: 数据根下必建的目录族（§2.3；顺序即人读顺序）
_LAYOUT_SUBDIRS = (
    "quota",
    "intake",
    "pre_registration",
    "dryrun",
    "watchdog",
    "runs",
    "candidate_cache",
    "references",
    "evaluator_results",
    "analysis",
    "reports",
    "evidence_pack",
    "closeout",
    "private",
)


class Phase281PathError(PermissionError):
    """数据根违规（嵌套仓/设置了 2.8.0 的 env/只读时不在场）。"""


def assert_no_benchmark_root_env() -> None:
    """``SIMPRAXIS_BENCHMARK_ROOT`` 已设置即抛（AC-P281-0-13 的正例）。

    紧凑断言而非静默清理：**不允许**本阶段顺手把别人的 env 删掉——那会让
    调用方以为设置生效了，实际被改写。设错了就 fail-fast，让操作者自己修。
    """
    raw = os.environ.get(BENCHMARK_ROOT_ENV)
    if raw:
        raise Phase281PathError(
            f"{BENCHMARK_ROOT_ENV} 必须未设置（实得 {raw!r}）：Phase 2.8.0 的"
            "数据根模块不认识 phase281，指错时它的隔离断言不会拦")


def phase281_root(*, read_only: bool = False) -> Path:
    """数据根（计划 §4.5.2 的签名；唯一实现仍在 ``runtime_paths``）。

    ``read_only=True``：不建目录、不 ``ensure_private_dir``，根不在场即抛
    ``Phase281PathError``（同本模块 ``_subdir`` 的只读口径）。
    """
    root = _rp_phase281_root()
    if read_only and not root.is_dir():
        raise Phase281PathError(f"数据根不在场：{root}（read_only）")
    return root


def _roots_not_nested(*, target: Path) -> None:
    """数据根不得与引擎仓/KB 仓同体或互嵌套（只读引用 intake 的两个根）。"""
    from orchestrator.benchmark import intake as _intake

    named = (("引擎仓", Path(_intake.REPO_ROOT)),
             ("KB 仓", Path(_intake.kb_root())))
    rp = target.resolve(strict=False)
    for label, other in named:
        op = other.resolve(strict=False)
        if rp == op:
            raise Phase281PathError(f"Phase 2.8.1 数据根不得与{label}同体：{rp}")
        for a, b in ((rp, op), (op, rp)):
            try:
                a.relative_to(b)
            except ValueError:
                continue
            raise Phase281PathError(
                f"Phase 2.8.1 数据根不得与{label}互相嵌套：{rp} vs {b}")


def assert_isolated() -> Path:
    """三条隔离纪律的唯一卡口；返回 resolve 后的数据根。"""
    assert_no_benchmark_root_env()
    root = phase281_root()
    assert_phase281_roots_isolated()
    _roots_not_nested(target=root)
    return root.resolve(strict=False)


def _subdir(name: str, *, read_only: bool = False) -> Path:
    root = phase281_root()
    if read_only:
        if not root.is_dir():
            raise Phase281PathError(f"数据根不在场：{root}（read_only）")
        return root / name
    assert_isolated()
    return ensure_private_dir(root / name)


# ---- 目录族 ----

def quota_dir() -> Path:
    return ensure_private_dir(phase281_quota_dir())


def approvals_dir() -> Path:
    return ensure_private_dir(phase281_approvals_dir())


def gateway_state_dir() -> Path:
    return ensure_private_dir(phase281_gateway_state_dir())


def intake_dir(*, read_only: bool = False) -> Path:
    return _subdir("intake", read_only=read_only)


def pre_registration_dir(*, read_only: bool = False) -> Path:
    return _subdir("pre_registration", read_only=read_only)


def dryrun_dir() -> Path:
    return _subdir("dryrun")


def watchdog_dir() -> Path:
    return _subdir("watchdog")


def runs_dir(*, read_only: bool = False) -> Path:
    return _subdir("runs", read_only=read_only)


def references_dir() -> Path:
    return _subdir("references")


def evaluator_results_dir() -> Path:
    return _subdir("evaluator_results")


def analysis_dir() -> Path:
    return _subdir("analysis")


def reports_dir() -> Path:
    return _subdir("reports")


def evidence_pack_dir() -> Path:
    return _subdir("evidence_pack")


def closeout_dir() -> Path:
    return _subdir("closeout")


def private_dir() -> Path:
    return _subdir("private")


# ---- 账本类路径（薄转发 runtime_paths 的单点实现） ----

def ledger_path() -> Path:
    return phase281_ledger_path()


def counters_path() -> Path:
    return phase281_counters_path()


def gateway_state_path() -> Path:
    return phase281_gateway_state_path()


# ---- Trial 树 ----

@dataclass(frozen=True)
class TrialPaths:
    """一个 Trial 的全部落点（计划 §2.3 唯一布局）。

    目录布局 ``runs/<task_id>/<arm_id>/rep<k>/`` 由 ``trial_id`` 拆出；
    ``trial_id`` 本身不带任何前缀（§1.4.3），``thread_id``/``run_id`` 逐字等于它。
    """

    trial_id: str
    task_id: str
    arm_id: str
    rep: int
    root: Path

    @property
    def workdir(self) -> Path:
        return self.root / "workdir"

    @property
    def telemetry(self) -> Path:
        return self.root / "telemetry.jsonl"

    @property
    def birth(self) -> Path:
        return self.root / "birth.json"

    @property
    def evaluator(self) -> Path:
        return self.root / "evaluator.json"

    @property
    def result(self) -> Path:
        return self.root / "result.json"

    @property
    def manifest(self) -> Path:
        return self.root / "candidate_trial_manifest.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def stdout(self) -> Path:
        return self.logs_dir / "stdout.txt"

    @property
    def stderr(self) -> Path:
        return self.logs_dir / "stderr.txt"

    @property
    def cache_dir(self) -> Path:
        return candidate_cache_dir(self.trial_id)


def _split_trial_id(trial_id: str) -> "tuple[str, str, int]":
    parts = trial_id.split("__")
    if len(parts) != 3 or not parts[2].startswith("rep"):
        raise Phase281PathError(
            f"trial_id 形态非法：{trial_id!r}（应为 <task_id>__<arm_id>__rep<k>）")
    task_id, arm_id, rep_raw = parts
    try:
        rep = int(rep_raw[3:])
    except ValueError as exc:  # pragma: no cover - 防御性
        raise Phase281PathError(f"trial_id 的 rep 段非整数：{trial_id!r}") from exc
    # 反向校验：用 constants 重建一次，防止 ``__`` 在 task/arm 名里出现时蒙混过关
    from orchestrator.benchmark.phase281.constants import trial_id as _mk

    if _mk(task_id, arm_id, rep) != trial_id:
        raise Phase281PathError(f"trial_id 非法（重建不一致）：{trial_id!r}")
    return task_id, arm_id, rep


def trial_dir(trial_id: str, *, read_only: bool = False) -> Path:
    task_id, arm_id, rep = _split_trial_id(trial_id)
    base = runs_dir(read_only=read_only) / task_id / arm_id / f"rep{rep}"
    if read_only:
        return base
    return ensure_private_dir(base)


def trial_paths(trial_id: str, *, read_only: bool = False) -> TrialPaths:
    task_id, arm_id, rep = _split_trial_id(trial_id)
    return TrialPaths(trial_id=trial_id, task_id=task_id, arm_id=arm_id,
                      rep=rep, root=trial_dir(trial_id, read_only=read_only))


def candidate_cache_dir(trial_id: str) -> Path:
    """每 Trial 独立的 CallReplay 缓存目录（INV-4：互不相同、不得互命中）。"""
    _split_trial_id(trial_id)
    return ensure_private_dir(_subdir("candidate_cache") / trial_id)


def ensure_layout() -> Path:
    """建齐 §2.3 的目录族（0700）；返回数据根。**不写任何账本行**。"""
    root = assert_isolated()
    ensure_private_dir(root)
    for name in _LAYOUT_SUBDIRS:
        ensure_private_dir(root / name)
    ensure_private_dir(root / "quota" / "approvals")
    ensure_private_dir(root / "quota" / "gateway_state")
    return root
