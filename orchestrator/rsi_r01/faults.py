"""RSI-R0.1 D 族故障表（v2 修订，2026-11-09 用户批准重设计）。

v1 校准（campaign r01，DESIGN_NOT_READY）证明：闭环 P 控制会把
``friction_scale`` / ``scene_tilt`` / ``force_scale`` 这类"瞬态型"故障在稳态
几乎抹平（三个故障参考的末位置两两差距 0.0004/0.0067/0.0071 m < 预注册
0.01 m，且故障下仍全部落在 L4 容差内），因此 v1 的 D 族在公开实验域
**不可辨识**。v2 只用能在稳态留下可测偏置的三个互异机制：

* ``external_force_bias``——施加层恒定外力（N）。P 控制稳态误差 = |bias|/kp，
  增大 kp 即可修复；偏置方向可区分实例。
* ``friction_scale``——地面摩擦系数放大（倍率）。静止门槛 = μ·m·g·scale，
  稳态误差 ≈ 门槛/kp，增大 kp 即可修复；机制与偏置互异（比例门槛 vs 外加力）。
* ``actuator_deadzone``——执行器死区（N）：指令模长低于死区即归零。
  稳态误差 ≈ deadzone/kp，增大 kp 即可修复；机制是**指令死区**而不是外力。

三个机制都作用在施加层、都不改动候选可见的请求参数（宿主注入），都可在公开
参数域 [kp≤5000] 内修复。v1 的 ``mass_scale`` 依旧禁止（稳态不变，不可辨识）。
"""
from __future__ import annotations

from typing import Any, Mapping

from orchestrator.rsi_r0.faults import FaultError, apply_fault as _r0_apply

#: v2 允许的故障机制（三者互异，且都在稳态留下可测偏置）。
KINDS = ("external_force_bias", "friction_scale", "actuator_deadzone")

DESCRIPTIONS = {
    "external_force_bias": "施加层恒定外力偏置（N）；稳态误差 ≈ |bias|/kp",
    "friction_scale": "地面摩擦系数放大（倍率）；稳态误差 ≈ μ·m·g·scale/kp",
    "actuator_deadzone": "执行器死区（N）；指令模长 < deadzone 即归零，稳态误差 ≈ deadzone/kp",
}

#: 每个机制的合法幅度区间（超出即任务表不可用，不是候选失败）。
BOUNDS: dict[str, tuple[float, float]] = {
    "external_force_bias": (5.0, 100.0),   # 作用在 |bias| = hypot(bx, by)
    "friction_scale": (1.5, 10.0),
    "actuator_deadzone": (5.0, 100.0),
}


def validate_fault(fault: Mapping[str, Any]) -> dict[str, Any]:
    """校验并返回规范化故障描述；非法即 :class:`FaultError`。"""
    if not isinstance(fault, Mapping):
        raise FaultError("故障必须是对象")
    kind = fault.get("kind")
    if kind not in KINDS:
        raise FaultError(f"未知故障机制 {kind!r}（v2 只允许 {KINDS}）")
    lo, hi = BOUNDS[str(kind)]
    if kind == "external_force_bias":
        vals = []
        for key in ("bias_x", "bias_y"):
            value = fault.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FaultError(f"external_force_bias 需要数值 {key}")
            vals.append(float(value))
        norm = (vals[0] ** 2 + vals[1] ** 2) ** 0.5
        if not (lo <= norm <= hi):
            raise FaultError(f"外力偏置模长 {norm:.3f} N 越界 [{lo}, {hi}]")
        return {"kind": kind, "bias_x": vals[0], "bias_y": vals[1]}
    scale = fault.get("scale") if kind == "friction_scale" else fault.get("deadzone")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise FaultError(f"{kind} 需要数值幅度")
    if not (lo <= float(scale) <= hi):
        raise FaultError(f"{kind} 幅度 {scale} 越界 [{lo}, {hi}]")
    if kind == "friction_scale":
        return {"kind": kind, "scale": float(scale)}
    return {"kind": kind, "deadzone": float(scale)}


def apply_scene_fault(scene_xml: str, fault: Mapping[str, Any]) -> str:
    """场景级部分：只有 ``friction_scale`` 改 XML，其余由宿主注入请求字段。"""
    f = validate_fault(fault)
    if f["kind"] == "friction_scale":
        return _r0_apply(scene_xml, {"kind": "friction_scale", "scale": f["scale"]})
    return scene_xml


def host_injections(fault: Mapping[str, Any]) -> dict[str, float]:
    """宿主私有注入项：写进可信服务请求，**不出现**在候选提交体里。"""
    f = validate_fault(fault)
    if f["kind"] == "external_force_bias":
        return {"force_bias_x": float(f["bias_x"]), "force_bias_y": float(f["bias_y"])}
    if f["kind"] == "actuator_deadzone":
        return {"deadzone": float(f["deadzone"])}
    return {}


def steady_state_offset(fault: Mapping[str, Any], *, kp: float) -> float:
    """解析**量级**估计（校准报告用；**不作判据**，判据只看实测）。

    外力偏置/死区两项与实测吻合；``friction_scale`` 受 MuJoCo 接触摩擦
    合成（box 与 plane 两个 geom 共同决定）影响，实测约为
    μ·m·g·scale/kp 的 1/2.5，故该值只当量级参考。
    """
    import math

    f = validate_fault(fault)
    if kp <= 0:
        raise FaultError("kp 必须为正")
    if f["kind"] == "external_force_bias":
        return math.hypot(f["bias_x"], f["bias_y"]) / kp
    if f["kind"] == "actuator_deadzone":
        return f["deadzone"] / kp
    return 9.81 * 0.25 * f["scale"] / kp   # μ·m·g·scale/kp（m=1 kg 参考实例）
