"""S2 主张词表 v2（五值）——共享规则面，先冻结后开战役（计划 §2.1）。

S1 的事故（双审 4/4 reject 的第一条实质意见）：主对比 95% CI 跨零却判
``REFUTED``，把"未检出"当成"证据否证"。v2 把三值扩为五值，语义**互斥且穷尽**：

* ``SUPPORTED``     ：方向主张被证据支持（CI 整段落在主张方向且越过 δ）；
* ``REFUTED``       ：证据与主张方向相反且越过 δ（CI 整段在对侧）；
* ``NOT_DETECTED``  ：CI 跨零（没有可检出的方向性效应，**不等于**否证）；
* ``EQUIVALENT``    ：CI 整段落进 ±δ 等效带（等效检验支持的"无可检出差异"）；
* ``INCONCLUSIVE``  ：其余（区间横跨 δ 边界、点区间、样本不足等）。

对 ``equivalent`` 型主张（阴性对照专用），词表退回三值语义：
``SUPPORTED``（CI ⊂ ±δ）/ ``REFUTED``（CI 全在带外）/ ``INCONCLUSIVE``（横跨边界）。

本模块是 S2 的**单一分类真值**：``skills.default_analyzer`` 与
``host_recompute.recompute`` 都从这里取分类；R1 旧分类器（``rubric.judge_proposition``
/ ``statistics.classify``）保持原样只读（"A_N 旧案不翻"）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["VERDICTS_V2", "VERDICT_SCHEMA", "VerdictError", "classify_v2",
           "is_equivalent_proposition"]

VERDICT_SCHEMA = "simpraxis.rsi_s2.verdict.v2"
VERDICTS_V2 = ("SUPPORTED", "REFUTED", "NOT_DETECTED", "EQUIVALENT",
               "INCONCLUSIVE")

#: 方向型主张（五值分类适用）；``equivalent`` 走三值语义。
_DIRECTIONAL = ("decrease_gt", "increase_gt")


class VerdictError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def is_equivalent_proposition(proposition: str) -> bool:
    return proposition == "equivalent"


def _decimal(value: Any, *, where: str) -> Decimal:
    if isinstance(value, bool):
        raise VerdictError("WRONG_TYPE", f"{where} 不接受布尔")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise VerdictError("WRONG_TYPE", f"{where}={value!r}") from None
    if not out.is_finite():
        raise VerdictError("NONFINITE", f"{where}={value!r}")
    return out


def classify_v2(proposition: str, delta: Any, lower: Any, upper: Any, *,
                n_blocks: int | None = None) -> str:
    """五值分类（host 唯一真值；claim.verdict 绑定照旧）。

    ``n_blocks`` 给出时：< 2 个独立 block 直接 ``INCONCLUSIVE``（样本不足不是证据）。
    退化点区间（``lower == upper``）不得冒充精确证据，同样 ``INCONCLUSIVE``
    （非退化性由 NC 结构判据另行强制，见 ``nc_registration``）。
    """
    if proposition not in (*_DIRECTIONAL, "equivalent"):
        raise VerdictError("PROPOSITION_INVALID",
                           f"{proposition!r} 不在 v2 词表（只支持 "
                           f"{(*_DIRECTIONAL, 'equivalent')}）")
    d = _decimal(delta, where="delta")
    lo = _decimal(lower, where="lower")
    hi = _decimal(upper, where="upper")
    if d <= 0:
        raise VerdictError("DELTA_INVALID", f"δ 必须为正，收到 {d}")
    if lo > hi:
        raise VerdictError("INTERVAL_INVERTED", f"L={lo} > U={hi}")
    if n_blocks is not None and int(n_blocks) < 2:
        return "INCONCLUSIVE"
    if lo == hi:
        return "INCONCLUSIVE"
    if proposition == "equivalent":
        if lo > -d and hi < d:
            return "SUPPORTED"
        if hi <= -d or lo >= d:
            return "REFUTED"
        return "INCONCLUSIVE"
    if proposition == "decrease_gt":
        if hi < -d:
            return "SUPPORTED"
        if lo > d:
            return "REFUTED"
        if lo > -d and hi < d:
            return "EQUIVALENT"
        if lo < 0 < hi:
            return "NOT_DETECTED"
        return "INCONCLUSIVE"
    # increase_gt
    if lo > d:
        return "SUPPORTED"
    if hi < -d:
        return "REFUTED"
    if lo > -d and hi < d:
        return "EQUIVALENT"
    if lo < 0 < hi:
        return "NOT_DETECTED"
    return "INCONCLUSIVE"
