"""paired-permutation 选择器接线接口（S2 消费；S0 只落透传 + 单测）。

不重写统计器：直接透传 `PhysicalRSI_core.self_harness.selection.select_survivor`
（默认 paired-permutation 家族；本模块不设置任何阈值/种子）。
"""
from __future__ import annotations

from orchestrator.hk import _physicalrsi  # noqa: F401  # pin 校验 + py3.10 tomllib 垫片
from PhysicalRSI_core.self_harness import selection as _selection

__all__ = ["select_survivor", "selection_identity"]

#: 单一真值：与上游同一函数对象，禁止本地重实现
select_survivor = _selection.select_survivor


def selection_identity() -> dict:
    return {
        "select_survivor": f"{_selection.select_survivor.__module__}.{_selection.select_survivor.__qualname__}",
        "module": _selection.__name__,
        "reimplements_upstream": False,
    }
