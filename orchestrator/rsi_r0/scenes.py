"""RSI-R0 场景构建：确定性 XML 生成（纯函数，无 IO、无随机）。

三个任务族共用两个**物理模板**，实例之间的差异落在参数与机制上（计划 §3.3
要求「不是只换 seed」）：

* :func:`build_push_scene` —— 平面 + 自由刚体箱（可带障碍柱），用于 E 族
  （把箱推到目标位）与 D 族（同一接口下注入**一个**未知故障）。
* :func:`build_pendulum_scene` —— 单摆 + 关节阻尼，用于 R 族（受控微研究：
  阻尼水平 → 沉降时间）。

约定：

* 场景 XML 是**公开任务输入**（候选可读）。真值（D 的故障种类/幅度、R 的
  预期方向与等效界、E/D 的通过容差）一律不进本模块，只落私有数据层。
* 所有数值参数经 :func:`validate_params` 白名单校验并夹在界内——可信服务
  只接受有界数值，不接受代码、路径、动态 import 或任意命令（计划 §4.4）。
"""
from __future__ import annotations

from typing import Any, Mapping

#: 参数白名单与硬界（超出即拒，不做静默夹取）。
PARAM_BOUNDS: Mapping[str, tuple[float, float]] = {
    "box_mass": (0.2, 20.0),
    "box_half": (0.02, 0.20),
    "friction": (0.02, 2.0),
    "target_x": (-2.0, 2.0),
    "target_y": (-2.0, 2.0),
    "obstacle_x": (-2.0, 2.0),
    "obstacle_r": (0.01, 0.30),
    "pendulum_length": (0.1, 2.0),
    "pendulum_mass": (0.05, 10.0),
    "damping": (0.0, 5.0),
    "timestep": (0.0005, 0.01),
    "tilt_deg": (-30.0, 30.0),
}


class SceneParamError(ValueError):
    """场景参数越界或含未声明键（fail-closed）。"""


def validate_params(params: Mapping[str, Any]) -> dict[str, float]:
    """只放行 :data:`PARAM_BOUNDS` 里的键；任一越界即抛，不夹取、不补默认。"""
    unknown = set(params) - set(PARAM_BOUNDS)
    if unknown:
        raise SceneParamError(f"未声明的场景参数：{sorted(unknown)}")
    out: dict[str, float] = {}
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SceneParamError(f"参数 {key} 必须是数值，收到 {type(value).__name__}")
        lo, hi = PARAM_BOUNDS[key]
        if not (lo <= float(value) <= hi):
            raise SceneParamError(f"参数 {key}={value} 越界 [{lo}, {hi}]")
        out[key] = float(value)
    return out


def _fmt(x: float) -> str:
    return f"{x:.6f}"


def build_push_scene(params: Mapping[str, Any]) -> str:
    """平面 + 自由刚体箱（可选圆障碍柱）。E/D 两族共用。

    必需参数：``box_mass`` ``box_half`` ``friction`` ``timestep``；
    可选：``obstacle_x`` + ``obstacle_r``（成对出现）。
    """
    p = validate_params(params)
    for req in ("box_mass", "box_half", "friction", "timestep"):
        if req not in p:
            raise SceneParamError(f"push 场景缺必需参数：{req}")
    half = _fmt(p["box_half"])
    tilt = p.get("tilt_deg", 0.0)
    import math as _m
    grav = (_fmt(9.81 * _m.sin(_m.radians(tilt))) if tilt else "0",
            _fmt(-9.81 * _m.cos(_m.radians(tilt))))
    gravity = f"{grav[0]} 0 {grav[1]}"
    obstacle = ""
    if ("obstacle_x" in p) != ("obstacle_r" in p):
        raise SceneParamError("obstacle_x 与 obstacle_r 必须成对给出")
    if "obstacle_x" in p:
        obstacle = (
            f'    <body name="obstacle" pos="{_fmt(p["obstacle_x"])} 0 0.30">'
            f'<geom name="obstacle" type="cylinder" size="{_fmt(p["obstacle_r"])} 0.30"'
            f' rgba="0.2 0.2 0.8 1"/></body>\n'
        )
    return f"""<mujoco model="rsi_r0_push">
  <option timestep="{_fmt(p['timestep'])}" gravity="{gravity}" integrator="RK4"/>
  <default>
    <geom friction="{_fmt(p['friction'])} 0.005 0.0001" condim="3"/>
  </default>
  <worldbody>
    <geom name="floor" type="plane" size="5 5 0.1" rgba="0.45 0.45 0.45 1"/>
{obstacle}    <body name="box" pos="0 0 {_fmt(p['box_half'])}">
      <freejoint name="box"/>
      <geom name="box" type="box" size="{half} {half} {half}"
            mass="{_fmt(p['box_mass'])}" rgba="0.9 0.2 0.2 1"/>
    </body>
  </worldbody>
</mujoco>
"""


def build_pendulum_scene(params: Mapping[str, Any]) -> str:
    """单摆（hinge + 阻尼）。R 族用：改 ``damping`` 观察沉降时间。

    必需参数：``pendulum_length`` ``pendulum_mass`` ``damping`` ``timestep``。

    .. warning::
       支点必须抬到地面之上（``z0 = L + 0.15``）。2026-11-08 实测：支点放在
       z=0 时整根杆埋在 floor plane 里，被接触力钉住，阻尼 0.3 的"沉降角"是
       4.70 rad（贴在接触位形上），量出来的不是摆动力学。抬升后才有真沉降。

    同时开 ``<flag energy="enable"/>``：``data.energy`` 不默认填充，不开启就恒
    报 0.0，等于给候选一个假的能量读数。
    """
    p = validate_params(params)
    for req in ("pendulum_length", "pendulum_mass", "damping", "timestep"):
        if req not in p:
            raise SceneParamError(f"pendulum 场景缺必需参数：{req}")
    length = float(p["pendulum_length"])
    z0 = length + 0.15                      # 支点高度：杆底离地 0.15 m
    half = 0.5 * length
    return f"""<mujoco model="rsi_r0_pendulum">
  <option timestep="{_fmt(p['timestep'])}" gravity="0 0 -9.81" integrator="RK4">
    <flag energy="enable"/>
  </option>
  <worldbody>
    <geom name="floor" type="plane" size="5 5 0.1" rgba="0.45 0.45 0.45 1"/>
    <body name="pivot" pos="0 0 {_fmt(z0)}">
      <body name="rod" pos="0 0 {_fmt(-half)}">
        <joint name="hinge" type="hinge" axis="0 1 0"
               damping="{_fmt(p['damping'])}" pos="0 0 {_fmt(half)}"/>
        <geom name="rod" type="capsule" size="0.01"
              fromto="0 0 {_fmt(half)} 0 0 {_fmt(-half)}"
              mass="{_fmt(p['pendulum_mass'])}" rgba="0.2 0.6 0.9 1"/>
      </body>
    </body>
  </worldbody>
</mujoco>
"""


#: 公开任务书里的模板登记（内容层与 candidates 都只认这两个键）。
SCENE_BUILDERS = {
    "push": build_push_scene,
    "pendulum": build_pendulum_scene,
}


def build_scene(template: str, params: Mapping[str, Any]) -> str:
    try:
        builder = SCENE_BUILDERS[template]
    except KeyError as exc:
        raise SceneParamError(f"未知场景模板：{template!r}（可选 {sorted(SCENE_BUILDERS)}）") from exc
    return builder(params)
