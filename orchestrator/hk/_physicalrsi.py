"""把 pin 死的 physicalrsi 0.3.0 载入内核进程（单一真值，不 vendored、不改上游）。

为何需要这层：
- 设计族把 H_k 通用技能机放在引擎仓（py3.10，`/mnt/venvs/simpraxis`），而
  physicalrsi 声明 `requires-python>=3.11`（其 `__init__` 导入 `tomllib`），
  生产执行面跑在独立 py3.11 venv（`/mnt/venvs/physicalrsi`，进程隔离）。
- 2026-11-18 实测：本包消费的模块（skills.protocol / skills.compose /
  skills.registry / PhysicalRSI_core.contracts / search_space / selection）
  在 py3.10 下唯一不满足的是 `tomllib`，用已装 `tomli` 顶替即可全部导入。

锁内适配（计划 §2 未涉及版本桥）：py3.10 时仅在导入路径上打两处补丁——
① `sys.modules["tomllib"] = tomli`；② 把引擎源码根加入 `sys.path`（优先用
已安装发行版；未安装时用 `$SIMPRAXIS_PHYSICALRSI_SRC`，缺省为已审计克隆
`/mnt/assets/simpraxis/PhysicalRSI/PhysicalRSI`）。载入后强校验版本 0.3.0，
防 pin 漂移（硬规则 8）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

EXPECTED_VERSION = "0.3.0"
DEFAULT_SRC = Path("/mnt/assets/simpraxis/PhysicalRSI/PhysicalRSI")


def _shim_tomllib() -> None:
    if sys.version_info >= (3, 11) or "tomllib" in sys.modules:
        return
    try:
        import tomllib  # noqa: F401  # py3.11 正常路径
    except ModuleNotFoundError:
        import tomli as tomllib  # 主 venv 已装 tomli 2.x
        sys.modules["tomllib"] = tomllib


def _ensure_importable() -> None:
    try:
        import physicalrsi  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    root = Path(os.environ.get("SIMPRAXIS_PHYSICALRSI_SRC", str(DEFAULT_SRC)))
    if not (root / "src" / "physicalrsi").is_dir():
        raise ImportError(
            "physicalrsi==0.3.0 不可用：未安装发行版，且 SIMPRAXIS_PHYSICALRSI_SRC "
            f"（{root}）不是有效源码根。S0 依赖该 pin（硬规则 8）")
    for entry in (str(root / "src"), str(root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


_shim_tomllib()
_ensure_importable()

import physicalrsi  # noqa: E402  # 载入后立即校验 pin

if getattr(physicalrsi, "__version__", None) != EXPECTED_VERSION:
    raise ImportError(
        f"physicalrsi 版本 {getattr(physicalrsi, '__version__', None)!r} != "
        f"pin {EXPECTED_VERSION!r}（硬规则 8；换版本须新审计+用户裁决）")

from PhysicalRSI_core.contracts import Contract, contract_from, contract_payload, mismatches  # noqa: E402
from PhysicalRSI_core.self_harness.search_space import ParameterSpace, SearchField  # noqa: E402
from physicalrsi.skills.compose import adapt, check_composition, compose_sequence  # noqa: E402
from physicalrsi.skills.protocol import (  # noqa: E402
    CallableSkill, CompositionTypeError, SkillDescriptor)

__all__ = [
    "EXPECTED_VERSION",
    "Contract", "contract_from", "contract_payload", "mismatches",
    "SearchField", "ParameterSpace",
    "SkillDescriptor", "CallableSkill", "CompositionTypeError",
    "compose_sequence", "adapt", "check_composition",
]
