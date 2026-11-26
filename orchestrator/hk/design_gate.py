"""S2c S2c-2：G-DESIGN 结构门（开放设计过门判定；纯逻辑 + 注入物理）。

五折，依次（任一不过即拒，带机器码）：
① 界内（因子值∈轴、指标∈列表、条件数≤上限）→ DESIGN_OUT_OF_SPACE
② 形状（条件×32 ≤ 192）→ DESIGN_SHAPE_INVALID
③ NC 三连（复用 nc_registration，试点数据判非退化/亚阈）→ DESIGN_NC_INVALID
④ 试点可检出（试点批主对比 |est| > δ）→ DESIGN_PILOT_WEAK
⑤ 判别力（宿主在各私有世界变体重跑，指标两两之差>δ）→ DESIGN_NON_DISCRIMINATIVE

物理相关步骤（③④⑤）通过注入的回调求值，本模块保持纯逻辑、可单测。
GateReport 公开部分回给 actor；世界变体数值只进审查者通道。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .design_space import DomainBrief, check_within_bounds

SCHEMA = "simpraxis.hk.design_gate.v1"

__all__ = ["SCHEMA", "DesignGateError", "GateReport", "check_design"]


class DesignGateError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class GateReport:
    passed: bool
    code: str = "OK"
    detail: str = ""
    public: dict = field(default_factory=dict)  # 回给 actor 的公开部分
    private: dict = field(default_factory=dict)  # 只进审查者通道（世界变体数值）

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "passed": self.passed, "code": self.code,
                "detail": self.detail, "public": self.public,
                "private": self.private}


def _max_conditions_shape(n_conditions: int, blocks: int = 32) -> bool:
    return n_conditions * blocks <= 192


def check_design(
    design: Mapping[str, Any],
    brief: DomainBrief,
    *,
    nc_check: "Callable[[Mapping[str, Any]], tuple[bool, str]] | None" = None,
    pilot_effect: "Callable[[Mapping[str, Any]], float] | None" = None,
    discriminability: "Callable[[Mapping[str, Any]], Mapping[str, Any]] | None" = None,
    delta: float = 0.001,
) -> GateReport:
    """五折结构门。回调整由战役注入（pilot_effect: 试点主对比效应绝对值；
    discriminability: 返回 {"min_pair_diff": float, "variants": [...]}）。"""
    # ① 界内
    problems = check_within_bounds(design, brief)
    if problems:
        return GateReport(False, "DESIGN_OUT_OF_SPACE", "; ".join(problems),
                          public={"problems": problems})
    # ② 形状
    conds = design.get("conditions") or []
    if not _max_conditions_shape(len(conds)):
        return GateReport(False, "DESIGN_SHAPE_INVALID",
                          f"{len(conds)} 条件×32 > 192", public={"n_conditions": len(conds)})
    # ③ NC 三连
    if nc_check is not None:
        ok, detail = nc_check(design)
        if not ok:
            return GateReport(False, "DESIGN_NC_INVALID", detail, public={"nc": detail})
    # ④ 试点可检出
    if pilot_effect is not None:
        est = float(pilot_effect(design))
        if abs(est) <= delta:
            return GateReport(False, "DESIGN_PILOT_WEAK",
                              f"试点 |est|={abs(est):.6g} <= δ={delta}",
                              public={"pilot_abs_est": abs(est), "delta": delta})
    # ⑤ 判别力
    if discriminability is not None:
        disc = discriminability(design)
        min_diff = float(disc.get("min_pair_diff", 0.0))
        private = {"variants": disc.get("variants", []), "min_pair_diff": min_diff}
        if min_diff <= delta:
            return GateReport(False, "DESIGN_NON_DISCRIMINATIVE",
                              f"世界变体最小两两差 {min_diff:.6g} <= δ={delta}",
                              public={"min_pair_diff": min_diff, "delta": delta},
                              private=private)
        return GateReport(True, "OK", "", public={"all": "pass"}, private=private)
    return GateReport(True, "OK", "", public={})
