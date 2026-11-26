"""S2c S2c-2：DomainBrief v1（开放设计的领域简报；actor 只拿简报自提设计）。

世界变体（competiing_explanations 的宿主私有数值）**只落私有路径、只进审查者通道**，
永不出现在 actor 可见的 brief 面（泄漏负控见 tests/milestoneS2c/test_design_gate.py）。

schema ``simpraxis.hk.domain_brief.v1``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA = "simpraxis.hk.domain_brief.v1"

__all__ = ["SCHEMA", "DomainBrief", "BriefError", "check_within_bounds"]


class BriefError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


@dataclass(frozen=True)
class DomainBrief:
    """领域简报：问题 + 竞争解释（公开名）+ 因子轴 + 可选指标 + 约束 + 试点配额。

    - ``competing_explanations`` 只给公开描述（id 对应宿主私有世界变体文件，数值不进
      本对象）；≥2 个（防循环论证）。
    - ``factor_axes``：actor 可选的因子轴（name/values/scene_param）。
    - ``metrics``：取自量表登记指标（δ 由量表钉死，简报里不可改）。
    - ``constraints``：max_conditions ≤6（保证 32 区块 ≤192）。
    - ``pilot_quota``：max_iterations=3, runs_per_iteration ≤16。
    """

    brief_id: str
    question: str
    competing_explanations: "tuple[Mapping[str, str], ...]"
    factor_axes: "tuple[Mapping[str, Any], ...]"
    metrics: "tuple[str, ...]"
    scene_id: str
    constraints: "Mapping[str, Any]" = field(default_factory=lambda: {"max_conditions": 6})
    pilot_quota: "Mapping[str, int]" = field(
        default_factory=lambda: {"max_iterations": 3, "runs_per_iteration": 16})

    def __post_init__(self) -> None:
        if len(self.competing_explanations) < 2:
            raise BriefError("EXPLANATIONS_INSUFFICIENT",
                             "简报必须 ≥2 个竞争解释（防循环论证）")
        if len(self.metrics) < 1:
            raise BriefError("METRIC_EMPTY", "简报至少一个可选指标")
        if not self.factor_axes:
            raise BriefError("AXES_EMPTY", "至少一个因子轴")
        if int(self.constraints.get("max_conditions", 0)) > 6:
            raise BriefError("MAX_CONDITIONS_TOO_HIGH", "max_conditions 超过 6")

    def public_document(self) -> dict:
        """actor 可见面：**绝不含世界变体数值**。"""
        return {
            "schema": SCHEMA, "brief_id": self.brief_id,
            "question": self.question,
            "competing_explanations": [dict(e) for e in self.competing_explanations],
            "factor_axes": [dict(a) for a in self.factor_axes],
            "metrics": list(self.metrics),
            "scene_id": self.scene_id,
            "constraints": dict(self.constraints),
            "pilot_quota": dict(self.pilot_quota),
        }


def check_within_bounds(design: Mapping[str, Any], brief: DomainBrief) -> "list[str]":
    """设计是否落在简报界内（因子值∈轴、指标∈列表、条件数≤上限）。空列表=界内。"""
    problems: "list[str]" = []
    axes = {str(a["name"]): a for a in brief.factor_axes}
    conditions = design.get("conditions") or []
    if not isinstance(conditions, list) or not conditions:
        problems.append("DESIGN_OUT_OF_SPACE: conditions 为空")
        return problems
    max_c = int(brief.constraints.get("max_conditions", 6))
    if len(conditions) > max_c:
        problems.append(f"DESIGN_OUT_OF_SPACE: 条件数 {len(conditions)} > {max_c}")
    for cond in conditions:
        factors = (cond.get("factors") or {}) if isinstance(cond, Mapping) else {}
        for fname, fval in factors.items():
            axis = axes.get(str(fname))
            if axis is None:
                problems.append(f"DESIGN_OUT_OF_SPACE: 未知因子 {fname!r}")
                continue
            allowed = [str(v) for v in axis.get("values", [])]
            if str(fval) not in allowed:
                problems.append(
                    f"DESIGN_OUT_OF_SPACE: {fname}={fval!r} 不在轴 {allowed}")
    metric = design.get("metric")
    if metric is not None and str(metric) not in brief.metrics:
        problems.append(f"DESIGN_OUT_OF_SPACE: 指标 {metric!r} 不在简报列表")
    return problems
