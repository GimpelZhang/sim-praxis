"""S2 候选包装载器（v1 臂；不换 worktree——候选文件按包名装载）。

装载对象（全部来自**候选包**，维护侧只提供注入点）：

* ``orchestrator/hk/guards.py`` → 取 ``g_report``，包装成维护侧 ``GuardViolation``
  后注册为 ``GUARDS["G-REPORT"]``（异常类身份统一，driver 照常捕获）；
* ``orchestrator/hk/touchpoints.py`` → 取 ``TOUCHPOINT_INSTRUCTIONS_V2`` 注入
  ``HeadlessSessionRunner(instructions=...)``；
* ``orchestrator/hk/nc_toolkit.py`` → 路径交给 runner（进 K1 工作区）。

装载前必须已过 ``evolution.validate_candidate``（本模块不重复机检，只做装载）；
任何缺件/导入失败即 ``CandidateLoadError``——v1 臂宁可不跑，也不回退到 v0 行为。
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.hk import guards as _guards

__all__ = ["CandidateLoadError", "CandidateHarness", "load_candidate"]


class CandidateLoadError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CandidateHarness:
    package_dir: Path
    guard_name: str | None
    instructions: Mapping[str, str] | None
    toolkit_path: Path | None
    diff_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"package_dir": str(self.package_dir),
                "guard_name": self.guard_name,
                "instructions_keys": sorted(self.instructions or {}),
                "toolkit_path": (str(self.toolkit_path)
                                 if self.toolkit_path else None),
                "diff_sha256": self.diff_sha256}


def _load_module(name: str, path: Path):
    """按 ``orchestrator.hk.<name>`` 的包上下文装载候选文件（相对导入可解析）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CandidateLoadError("CANDIDATE_IMPORT", f"{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 候选代码坏 = fail-closed
        sys.modules.pop(name, None)
        raise CandidateLoadError("CANDIDATE_IMPORT",
                                 f"{path}: {type(exc).__name__}: {exc}") from None
    return module


def _wrap_guard(fn: Callable[[Any], None]) -> Callable[[Any], None]:
    """把候选守卫的异常统一成维护侧 ``GuardViolation``（异常类身份一致）。"""

    def wrapped(ctx):
        try:
            fn(ctx)
        except _guards.GuardViolation:
            raise
        except Exception as exc:  # noqa: BLE001
            code = str(getattr(exc, "code", "REPORT_INCOMPLETE"))
            detail = str(getattr(exc, "detail", "") or exc)
            raise _guards.GuardViolation("g_report", code, detail) from None

    return wrapped


def load_candidate(package_dir: Path, *, diff_sha256: str = "") -> CandidateHarness:
    root = Path(package_dir)
    guard_name: str | None = None
    instructions: Mapping[str, str] | None = None
    toolkit_path: Path | None = None

    guards_path = root / "guards.py"
    if guards_path.is_file():
        module = _load_module("orchestrator.hk.guards_candidate", guards_path)
        fn = getattr(module, "g_report", None)
        if not callable(fn):
            raise CandidateLoadError("CANDIDATE_IMPORT",
                                     "候选 guards.py 缺 g_report")
        _guards.register_guard("G-REPORT", _wrap_guard(fn))
        guard_name = "G-REPORT"

    touches_path = root / "touchpoints.py"
    if touches_path.is_file():
        module = _load_module("orchestrator.hk.touchpoints_candidate", touches_path)
        table = getattr(module, "TOUCHPOINT_INSTRUCTIONS_V2", None)
        if not isinstance(table, Mapping):
            raise CandidateLoadError("CANDIDATE_IMPORT",
                                     "候选 touchpoints.py 缺 TOUCHPOINT_INSTRUCTIONS_V2")
        # 候选模板覆盖 S1 默认；未覆盖的触点（write_analysis/diagnose_fail）
        # 回退到 S1 原文——两臂在**非变异触点**上的指令完全一致（防混淆）。
        from orchestrator.hk.touchpoints import TOUCHPOINT_INSTRUCTIONS

        instructions = {**TOUCHPOINT_INSTRUCTIONS,
                         **{str(k): str(v) for k, v in table.items()}}

    toolkit = root / "nc_toolkit.py"
    if toolkit.is_file():
        toolkit_path = toolkit

    if guard_name is None:
        raise CandidateLoadError("CANDIDATE_IMPORT",
                                 "候选包缺机制实现（guards.py 的 G-REPORT）")
    return CandidateHarness(package_dir=root, guard_name=guard_name,
                            instructions=instructions, toolkit_path=toolkit_path,
                            diff_sha256=diff_sha256)
