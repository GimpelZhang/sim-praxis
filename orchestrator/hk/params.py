"""ParameterSpace 声明：工程容差可进化，科学判据**结构性不可达**（设计族 §4）。

- 六个字段用 physicalrsi 的 `SearchField`/`ParameterSpace` 声明（bounds/precision
  来自设计族 §4 的 yaml 逐行）。
- `validate_proposal()` 是实现面：LLM 只能提 JSON 数值；任何未声明字段（尤其
  δ/α/置信水平/判据方向/block 数/对比定义）→ `UNKNOWN_FIELD` fail-closed。
  这是硬规则 7「不抬阈值翻案」的结构性表达——判据字段不是"校验通过"，而是
  **根本不存在于空间里**。
"""
from __future__ import annotations

from ._physicalrsi import ParameterSpace, SearchField

__all__ = ["SPACE", "FIELDS", "INTEGER_FIELDS", "ParameterProposalError", "validate_proposal"]

#: 逻辑字段名 → (component, 相对文件, JSON 键路径, 下界, 上界, 小数精度)
_DECLARATIONS = {
    "exploration.max_batches": ("control", "config/exploration.json", ("max_batches",), 1.0, 8.0, 0),
    "exploration.runs_per_condition": ("control", "config/exploration.json",
                                       ("runs_per_condition",), 8.0, 64.0, 0),
    "analysis.bootstrap_resamples": ("tools", "config/analysis.json",
                                     ("bootstrap_resamples",), 1000.0, 100000.0, 0),
    "tolerance.settle_time_abs_s": ("control", "config/tolerance.json",
                                    ("settle_time_abs_s",), 0.005, 0.05, 3),
    "guard.noprog_rounds": ("control", "config/guards.json", ("noprog_rounds",), 1.0, 4.0, 0),
    "timing.analysis_timeout_s": ("control", "config/timing.json",
                                  ("analysis_timeout_s",), 30.0, 600.0, 0),
}

FIELDS = {
    name: SearchField(component=component, file=file, key=key,
                      lower=lower, upper=upper, precision=precision)
    for name, (component, file, key, lower, upper, precision) in _DECLARATIONS.items()
}

#: 计数/时长类字段必须是整数（物理语义；SearchField 的 precision 只管小数网格）
INTEGER_FIELDS = frozenset({
    "exploration.max_batches",
    "exploration.runs_per_condition",
    "analysis.bootstrap_resamples",
    "guard.noprog_rounds",
    "timing.analysis_timeout_s",
})

SPACE = ParameterSpace(tuple(FIELDS.values()))


class ParameterProposalError(ValueError):
    """提案被拒；`code` 为机器判据（UNKNOWN_FIELD / NON_NUMERIC / OUT_OF_BOUNDS / OFF_GRID）。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def validate_proposal(proposal: dict) -> dict[str, float]:
    """把提案 JSON 校验为 {字段名: 数值}；任何越界/越权即拒（fail-closed）。"""
    if not isinstance(proposal, dict) or not proposal:
        raise ParameterProposalError("EMPTY_PROPOSAL", "提案必须是至少一个字段的 JSON 对象")
    unknown = sorted(set(proposal) - set(FIELDS))
    if unknown:
        raise ParameterProposalError(
            "UNKNOWN_FIELD",
            f"字段 {unknown} 不在 ParameterSpace；科学判据（δ/α/置信水平/判据方向/"
            f"block 数/对比定义）结构性不可进化")
    accepted: dict[str, float] = {}
    for name, value in proposal.items():
        field = FIELDS[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterProposalError("NON_NUMERIC", f"{name}={value!r} 不是数值")
        if name in INTEGER_FIELDS and float(value) != int(value):
            raise ParameterProposalError("OFF_GRID", f"{name}={value!r} 须为整数")
        if not field.lower <= value <= field.upper:
            raise ParameterProposalError(
                "OUT_OF_BOUNDS",
                f"{name}={value!r} 超出 [{field.lower}, {field.upper}]")
        if round(value, field.precision) != value:
            raise ParameterProposalError(
                "OFF_GRID", f"{name}={value!r} 不在精度 {field.precision} 的网格上")
        accepted[name] = float(value)
    return accepted
