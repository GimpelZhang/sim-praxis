"""RSI-R0 故障注入（维护侧唯一实现，候选不可见）。

D 族的设计：候选只拿到**公开场景**并发出有界数值请求；真正的故障由宿主侧在把
场景交给可信服务**之前**注入（:func:`apply_fault`），因此候选的请求体里从不出现
故障种类或幅度。这样"诊断"才是真诊断，而不是读一眼请求字段就能答的题。

注入是确定性 XML 变换（``xml.etree``），只改 :data:`FAULT_KINDS` 声明的属性，
不做字符串替换（避免匹配到注释/其它元素）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Mapping

#: 支持的故障种类：``name`` → 作用点与含义（真值只在私有数据层）。
FAULT_KINDS: Mapping[str, str] = {
    "scene_tilt": "地面被倾斜（零外力时也会自行下滑，加速度与质量无关）",
    "friction_scale": "地面摩擦系数被放大（阻力变大、起滑门槛抬高）",
    "force_scale": "控制力被按比例削弱（执行器增益下降）",
}
#: scene_tilt 的 scale 是**角度（度）**，其余是倍率——两者的合法区间分开声明。
FAULT_SCALE_BOUNDS = (0.2, 10.0)
TILT_DEG_BOUNDS = (-30.0, 30.0)


class FaultError(ValueError):
    """故障描述非法（未知种类/越界/场景缺目标元素）。"""


def validate_fault(fault: Mapping[str, Any]) -> dict[str, Any]:
    kind = fault.get("kind")
    if kind not in FAULT_KINDS:
        raise FaultError(f"未知故障种类：{kind!r}（可选 {sorted(FAULT_KINDS)}）")
    scale = fault.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise FaultError("故障 scale 必须是数值")
    lo, hi = TILT_DEG_BOUNDS if kind == "scene_tilt" else FAULT_SCALE_BOUNDS
    if not (lo <= float(scale) <= hi):
        raise FaultError(f"故障 scale={scale} 越界 [{lo}, {hi}]")
    return {"kind": kind, "scale": float(scale)}


def apply_fault(scene_xml: str, fault: Mapping[str, Any]) -> str:
    """把故障注入公开场景，返回新 XML 文本（不修改入参）。"""
    f = validate_fault(fault)
    kind, scale = f["kind"], f["scale"]
    if kind == "force_scale":
        # 控制力削弱不改场景 XML：宿主侧把 ``fault["scale"]`` 作为请求字段
        # ``force_scale`` 传给可信服务（服务在施加外力时乘该系数）。候选请求体
        # 里不含该字段——宿主在转发前注入，回传只给可观测结果。
        return scene_xml
    root = ET.fromstring(scene_xml)
    if kind == "scene_tilt":
        import math
        opt = root.find("option")
        if opt is None:
            raise FaultError("场景缺少 <option>，无法注入 scene_tilt")
        g = 9.81
        rad = math.radians(float(scale))
        base = float(opt.get("gravity", "0 0 -9.81").split()[2])
        opt.set("gravity", f"{g * math.sin(rad):.6f} 0 {base * math.cos(rad):.6f}")
    elif kind == "friction_scale":
        for geom in root.iter("geom"):
            fric = geom.get("friction")
            if fric:
                parts = fric.split()
                parts[0] = f"{float(parts[0]) * scale:.6f}"
                geom.set("friction", " ".join(parts))
    return ET.tostring(root, encoding="unicode")
