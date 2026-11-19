"""S2 阴性对照（NC）注册与有效性三判据——共享规则面（计划 §2.1 第 3 行）。

S1 的两条实质意见：

* A_N 的 sham（torsional 0.01→0.02）在 condim=3、无扭转载荷下是**结构性 no-op**，
  逐 block 效应恒为 0——退化的阴性对照不验证任何东西（双审原文"阴性对照 [0,0] 退化"）；
* B_N **没有注册阴性对照**——无法排除非特异效应。

v2 规则：**每个研究强制注册 NC**，且 NC 必须同时满足三连判据：

1. **非退化**（non-degenerate）：逐 block 效应不得全为零，且区间宽度 > 0
   （结构性 no-op 不是阴性证据，是无效对照）；
2. **亚阈**（sub-threshold）：|estimate| < δ（对照干预的真实效应被设计在等效界内）；
3. **灵敏度校准**（sensitivity-calibrated）：同管线注入已知大效应必须可检出
   （校准报告 `sensitivity.detected=True`），否则"没检出"可能只是管线坏了。

退化/超阈/未校准 → 结构拒绝（``NC_DEGENERATE`` / ``NC_ABOVE_THRESHOLD`` /
``NC_UNCALIBRATED``），**不进确认批**。未注册 → ``NC_UNREGISTERED``。

**设计面**：任务构建器提供 *NC 槽*（允许扰动的参数维、扰动上界、候选条件、
等效指标与 δ、默认对照）；具体对照由 K1 的 NC 设计技能（变异面）在槽内选择，
**由 host 构造为 ContrastV13**（actor 只提名 base/sham，不写数值权重）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

__all__ = ["NC_SLOT_SCHEMA", "NC_CRITERIA", "NCRegistrationError",
           "validate_slot", "resolve_candidates", "build_nc_contrast",
           "validate_nc", "nc_rows"]

NC_SLOT_SCHEMA = "simpraxis.rsi_s2.nc_slot.v1"

#: 三连判据（机器码 → 人类可读语义；结构拒绝时的单一真值）。
NC_CRITERIA: "Mapping[str, str]" = {
    "non_degenerate": "逐 block 效应非全零且区间宽度 >0（结构性 no-op 不是阴性证据）",
    "sub_threshold": "|estimate| < δ（对照干预的真实效应在等效界内）",
    "sensitivity_calibrated": "同管线注入已知效应必可检出（校准报告 detected=True）",
}

#: 浮点零判定：逐 block 效应的最大绝对值低于此即视为结构性 no-op。
_DEGENERATE_EPS = 1e-15


class NCRegistrationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _dec(value: Any, *, where: str) -> Decimal:
    if isinstance(value, bool):
        raise NCRegistrationError("NC_SLOT_INVALID", f"{where} 不接受布尔")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise NCRegistrationError("NC_SLOT_INVALID", f"{where}={value!r}") from None


def validate_slot(slot: Any) -> "dict[str, Any]":
    """NC 槽结构校验（注册件/任务构建器的 fail-closed 面）。"""
    if not isinstance(slot, Mapping):
        raise NCRegistrationError("NC_SLOT_INVALID", "NC 槽必须是对象")
    if slot.get("schema") != NC_SLOT_SCHEMA:
        raise NCRegistrationError("NC_SLOT_INVALID", f"schema={slot.get('schema')!r}")
    allowed = slot.get("allowed_diff")
    if not isinstance(allowed, (list, tuple)) or not allowed:
        raise NCRegistrationError("NC_SLOT_INVALID", "allowed_diff 必须是非空列表")
    candidates = slot.get("candidates")
    if not isinstance(candidates, (list, tuple)) or len(candidates) < 2:
        raise NCRegistrationError("NC_SLOT_INVALID", "candidates 至少两项")
    for key in ("equivalence_metric", "delta", "ci_level"):
        if not slot.get(key):
            raise NCRegistrationError("NC_SLOT_INVALID", f"缺 {key}")
    delta = _dec(slot["delta"], where="slot.delta")
    if delta <= 0:
        raise NCRegistrationError("NC_SLOT_INVALID", f"δ 必须为正：{delta}")
    bounds = slot.get("max_perturbation")
    if not isinstance(bounds, Mapping) or not bounds:
        raise NCRegistrationError(
            "NC_SLOT_INVALID", "max_perturbation 必须是 {参数: 上界} 映射")
    for key, bound in bounds.items():
        if _dec(bound, where=f"slot.max_perturbation[{key}]") <= 0:
            raise NCRegistrationError(
                "NC_SLOT_INVALID", f"max_perturbation[{key}] 必须为正")
    default = slot.get("default")
    if default is not None and not isinstance(default, Mapping):
        raise NCRegistrationError("NC_SLOT_INVALID", "default 必须是对象或 null")
    return dict(slot)


def resolve_candidates(slot: Mapping[str, Any],
                       conditions: Sequence[Mapping[str, Any]]
                       ) -> "dict[str, Mapping[str, Any]]":
    """把槽里的候选条件 id 解析成条件文档；任一缺失即 ``NC_SLOT_INVALID``。"""
    by_id = {str(c.get("id")): c for c in conditions}
    missing = sorted(set(str(c) for c in slot["candidates"]) - set(by_id))
    if missing:
        raise NCRegistrationError("NC_SLOT_INVALID", f"候选条件未登记 {missing}")
    return {str(cid): by_id[str(cid)] for cid in slot["candidates"]}


def _params(condition: Mapping[str, Any]) -> "dict[str, str]":
    return {str(k): str(v) for k, v in dict(condition.get("params") or {}).items()}


def build_nc_contrast(slot: Mapping[str, Any], *, base: str, sham: str,
                      conditions: Sequence[Mapping[str, Any]]) -> "dict[str, Any]":
    """在槽内构造 host 侧 NC 对照（actor 只提名，不写权重/δ）。

    * base/sham 必须在槽的候选集内；
    * 两条件参数差集必须恰为 ``allowed_diff`` 的子集且至少一项；
    * 扰动幅度不得超过 ``max_perturbation``（亚阈设计的结构性上界）。
    """
    validate_slot(slot)
    candidates = resolve_candidates(slot, conditions)
    if base not in candidates or sham not in candidates:
        raise NCRegistrationError(
            "NC_OUT_OF_SLOT", f"base={base!r}/sham={sham!r} 不在候选集")
    if base == sham:
        raise NCRegistrationError("NC_OUT_OF_SLOT", "base 与 sham 不得相同")
    a, b = _params(candidates[base]), _params(candidates[sham])
    keys = sorted(set(a) | set(b))
    diff = [k for k in keys if a.get(k) != b.get(k)]
    allowed = [str(k) for k in slot["allowed_diff"]]
    extra = sorted(set(diff) - set(allowed))
    if extra or not diff:
        raise NCRegistrationError(
            "NC_OUT_OF_SLOT", f"允许扰动 {allowed}，实际差异 {diff}")
    bounds = {str(k): _dec(v, where=f"slot.max_perturbation[{k}]")
              for k, v in dict(slot["max_perturbation"]).items()}
    for key in diff:
        try:
            delta = abs(Decimal(str(b.get(key))) - Decimal(str(a.get(key))))
        except Exception:  # noqa: BLE001
            raise NCRegistrationError(
                "NC_OUT_OF_SLOT", f"{key} 不是可比较的十进制值") from None
        bound = bounds.get(key)
        if bound is None:
            raise NCRegistrationError(
                "NC_OUT_OF_SLOT", f"{key} 缺 max_perturbation 上界")
        if delta > bound:
            raise NCRegistrationError(
                "NC_OUT_OF_SLOT", f"{key} 扰动 {delta} 超上界 {bound}")
    return {
        "schema": "simpraxis.rsi_r13.contrast.v1",
        "schema_version": 1,
        "id": f"negative_{base}_vs_{sham}",
        "kind": "pairwise_difference",
        "metric": str(slot["equivalence_metric"]),
        "proposition": "equivalent",
        "delta": str(_dec(slot["delta"], where="slot.delta")),
        "terms": [{"condition_id": base, "weight": "1"},
                  {"condition_id": sham, "weight": "-1"}],
    }


def nc_rows(rows: Sequence[Mapping[str, Any]],
            contrast: Mapping[str, Any]) -> "list[float]":
    """取出 NC 对照的逐 block 线性值（与 host 复算同一实现）。"""
    from orchestrator.rsi_bridge.host_recompute import _linear_contrast_values

    return list(_linear_contrast_values(list(rows), dict(contrast)).values())


def validate_nc(*, contrast: Mapping[str, Any], block_values: Sequence[float],
                estimate: Any, ci_low: Any, ci_high: Any,
                sensitivity: Mapping[str, Any] | None) -> "dict[str, Any]":
    """三连判据（结构拒绝，fail-closed）。返回证据件。"""
    delta = _dec(contrast.get("delta"), where="contrast.delta")
    est = _dec(estimate, where="estimate")
    lo = _dec(ci_low, where="ci_low")
    hi = _dec(ci_high, where="ci_high")
    values = [float(v) for v in block_values]
    if not values:
        raise NCRegistrationError("NC_DEGENERATE", "没有逐 block 效应")
    max_abs = max(abs(v) for v in values)
    width = hi - lo
    degenerate = max_abs <= _DEGENERATE_EPS or width <= 0
    if degenerate:
        raise NCRegistrationError(
            "NC_DEGENERATE",
            f"逐 block 效应最大 |v|={max_abs:.3e}、区间宽={width}——"
            "结构性 no-op 不是阴性证据")
    if abs(est) >= delta:
        raise NCRegistrationError(
            "NC_ABOVE_THRESHOLD", f"|estimate|={abs(est)} ≥ δ={delta}（亚阈设计失败）")
    if not isinstance(sensitivity, Mapping) or sensitivity.get("detected") is not True:
        raise NCRegistrationError(
            "NC_UNCALIBRATED", "同管线灵敏度校准未通过（detected!=True）")
    return {
        "criteria": dict(NC_CRITERIA),
        "non_degenerate": {"max_abs_block_effect": max_abs,
                           "ci_width": float(width)},
        "sub_threshold": {"estimate": float(est), "delta": float(delta)},
        "sensitivity_calibrated": dict(sensitivity),
        "passed": True,
    }
