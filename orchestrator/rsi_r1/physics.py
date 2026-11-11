"""RSI-R1 T1b：两场景 MJCF 的宿主侧构造与容器 spec 文档（Plan §3.1/§3.2/§3.3）。

分工（§5.3「候选代码与可信执行分离」）：

- 本模块在**宿主**构造 MJCF 与 spec 文档，只读 ``ExperimentSpec`` 的宿主字段；
  不 import mujoco、不执行任何物理。
- 容器内的固定脚本 ``experiment_service.py`` 只认 ``spec.json``（本模块产物），
  从不接受候选代码，也不读仓库。
- 候选只能挑参数；XML/solver/重力/几何/步长类型都在这里固定。

两处 T1b 校准期登记的关键约定（写入 ``calibration_prereg.json``，先于任何 raw）：

1. **A 的起滑约定**：MuJoCo 软接触下刚体在零间隙释放会出现亚毫米级数值弹跳
   （实测 z 振幅 ≤ 0.14 mm、最坏 1.25% 步无接触）。仪器先在原地整定
   ``A_SETTLE_STEPS`` 步（v=0），再施加注册初速并从 t=0 记录 4 s。整定是
   仪器初始化，不是可设计参数。
2. **pair friction 五元组**：显式 ``<pair>`` 的 ``friction`` 依次是
   tangent1 / tangent2 / spin / roll1 / roll2（MuJoCo 3.15.0 头文件
   ``pair_friction`` 5 槽）。只给 3 个值会把 torsional 落进 roll 槽并让
   spin 变 0——因此本模块恒发 5 元组，几何块 ``friction`` 只用 3 元组。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from orchestrator.rsi_r1 import scenes as S
from orchestrator.rsi_r1.experiments import ExperimentSpec

#: 镜像内 MuJoCo 版本（冻结件；服务端启动时自检）。
MUJOCO_VERSION = "3.15.0"

#: 容器 spec 文档 schema。
EXPERIMENT_SPEC_SCHEMA = f"{S.SCHEMA_PREFIX}.experiment_spec.v1"

#: A 的整定步数（2 s @ dt=0.0005）：只用于建立静止接触，不进入记录。
A_SETTLE_STEPS = 2000

#: 显式 contact pair：solref=0.02 s（MuJoCo 默认时间常数）/ 阻尼比 1；
#: solimp 收紧使滑行期弹跳压到亚微米级（校准实测最坏 gap 10 步）。
CONTACT_SOLREF = "0.02 1"
CONTACT_SOLIMP = "0.99 0.999 0.0001"

#: roll 摩擦的固定小值（5 元组第 4/5 槽；避免把 torsional 写错槽位）。
ROLL_FRICTION = "0.0001"

#: A 的固定几何（公开合同；本轮不换 mesh）。
A_BOX_HALF_EXTENTS = "0.1 0.1 0.05"
A_FLOOR_HALF_EXTENTS = "3 3 0.1"
A_BOX_START_Z = "0.05"

#: B 的固定量（scenes.SCENE_B_DOC.fixed 的镜像；本模块只做数值化）。
B_INERTIA_BASE = Decimal("0.05")
B_DAMPING = Decimal("0.05")
B_TORQUE_LIMIT = Decimal("2.0")

_SERVICE_PATH = Path(__file__).with_name("experiment_service.py")


class PhysicsError(RuntimeError):
    """宿主侧场景构造拒绝（不可修复的内部错误，不是候选可自修码）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _dec(value: Any, name: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except Exception:  # noqa: BLE001 - Decimal 的异常类型随输入而异
        raise PhysicsError("PARAM_INVALID", f"{name}={value!r}") from None
    if not out.is_finite():
        raise PhysicsError("PARAM_INVALID", f"{name}={value!r} 非有限")
    return out


def _plain(value: Decimal) -> str:
    """Decimal → 最短十进制字面量（喂 MJCF，禁用科学计数法的意外形式）。"""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def scene_a_xml(*, dt_s: Decimal, mass_kg: Decimal, sliding_friction: Decimal,
                torsional_friction_m: Decimal) -> str:
    """Study A：水平面试块 + 显式 contact pair（condim=3，五元组摩擦）。"""
    mu = _plain(sliding_friction)
    tors = _plain(torsional_friction_m)
    pair_friction = f"{mu} {mu} {tors} {ROLL_FRICTION} {ROLL_FRICTION}"
    geom_friction = f"{mu} {mu} {tors}"
    return (
        "<mujoco model=\"rsi_r1_A\">"
        f"<option timestep=\"{_plain(dt_s)}\" gravity=\"0 0 -9.81\" "
        "integrator=\"implicitfast\" cone=\"elliptic\"/>"
        "<worldbody>"
        f"<geom name=\"floor\" type=\"plane\" size=\"{A_FLOOR_HALF_EXTENTS}\" "
        f"friction=\"0 0 0\" condim=\"3\" solref=\"{CONTACT_SOLREF}\" "
        f"solimp=\"{CONTACT_SOLIMP}\"/>"
        f"<body name=\"box\" pos=\"0 0 {A_BOX_START_Z}\">"
        "<freejoint name=\"box_free\"/>"
        f"<geom name=\"box_geom\" type=\"box\" size=\"{A_BOX_HALF_EXTENTS}\" "
        f"mass=\"{_plain(mass_kg)}\" friction=\"{geom_friction}\" condim=\"3\" "
        f"solref=\"{CONTACT_SOLREF}\" solimp=\"{CONTACT_SOLIMP}\"/>"
        "</body></worldbody>"
        "<contact>"
        f"<pair name=\"box_floor\" geom1=\"box_geom\" geom2=\"floor\" "
        f"condim=\"3\" friction=\"{pair_friction}\" solref=\"{CONTACT_SOLREF}\" "
        f"solimp=\"{CONTACT_SOLIMP}\"/>"
        "</contact></mujoco>"
    )


def scene_b_xml(*, dt_s: Decimal, inertia_ratio: Decimal) -> str:
    """Study B：无接触单关节（重力关闭、粘性阻尼固定、单 hinge DOF）。"""
    inertia = B_INERTIA_BASE * inertia_ratio
    return (
        "<mujoco model=\"rsi_r1_B\">"
        f"<option timestep=\"{_plain(dt_s)}\" gravity=\"0 0 0\" "
        "integrator=\"implicitfast\"/>"
        "<worldbody><body name=\"arm\" pos=\"0 0 0\">"
        f"<inertial pos=\"0 0 0\" mass=\"1\" "
        f"diaginertia=\"{_plain(inertia)} {_plain(inertia)} {_plain(inertia)}\"/>"
        "<joint name=\"hinge\" type=\"hinge\" axis=\"0 0 1\" "
        f"damping=\"{_plain(B_DAMPING)}\"/>"
        "</body></worldbody></mujoco>"
    )


def build_scene_xml(spec: ExperimentSpec) -> str:
    """按 spec 的 study/参数构造 MJCF（宿主可信；同一 spec 恒同字节）。"""
    dt_s = _dec(spec.dt_s, "dt_s")
    if spec.study_id == "A":
        p = spec.physical_params
        if p.get("mass_kg") is None or p.get("sliding_friction") is None:
            raise PhysicsError("PARAM_MISSING", "A 缺 mass/friction")
        return scene_a_xml(
            dt_s=dt_s,
            mass_kg=_dec(p["mass_kg"], "mass_kg"),
            sliding_friction=_dec(p["sliding_friction"], "sliding_friction"),
            torsional_friction_m=_dec(p["torsional_friction_m"],
                                      "torsional_friction_m"),
        )
    if spec.study_id == "B":
        return scene_b_xml(dt_s=dt_s,
                           inertia_ratio=_dec(spec.physical_params["inertia_ratio"],
                                              "inertia_ratio"))
    raise PhysicsError("STUDY_UNKNOWN", f"study_id={spec.study_id!r}")


def build_spec_document(spec: ExperimentSpec) -> "dict[str, Any]":
    """容器 spec.json：只含宿主字段；``spec_hash`` 仍是宿主 spec 的哈希。"""
    if spec.study_id == "A":
        payload: "dict[str, Any]" = {
            "kind": "A",
            "settle_steps": A_SETTLE_STEPS,
            "initial_vx_mps": spec.physical_params["v0_mps"],
        }
    elif spec.study_id == "B":
        ctl = spec.controller_params
        payload = {
            "kind": "B",
            "tc_ticks_us": int(spec.ticks["tc_ticks_us"]),
            "dt_ticks_us": int(spec.ticks["dt_ticks_us"]),
            "target_amplitude_rad": spec.physical_params["target_amplitude_rad"],
            "target_freq_hz": spec.physical_params["target_freq_hz"],
            "controller": ctl["controller"],
            "kp": ctl["kp"],
            "kd": ctl["kd"],
            "torque_limit_nm": _plain(B_TORQUE_LIMIT),
        }
    else:
        raise PhysicsError("STUDY_UNKNOWN", f"study_id={spec.study_id!r}")
    doc = {
        "schema": EXPERIMENT_SPEC_SCHEMA,
        "schema_version": 1,
        "run_id": spec.run_id,
        "spec_hash": spec.spec_hash,
        "study_id": spec.study_id,
        "scene_id": spec.scene_id,
        "block_id": spec.block_id,
        "dt_s": spec.dt_s,
        "duration_s": spec.duration_s,
        "nsteps": spec.nsteps,
        "ticks": dict(spec.ticks),
        "params_applied": {**dict(spec.physical_params),
                           **dict(spec.controller_params)},
        "units": dict(spec.units),
        "mujoco_version": MUJOCO_VERSION,
        "scene_xml": build_scene_xml(spec),
        "run": payload,
    }
    return doc


def service_source() -> bytes:
    """容器内固定服务脚本的字节（宿主读取并核对摘要后才挂载）。"""
    return _SERVICE_PATH.read_bytes()


#: 服务脚本期望的摘要前缀由测试/校准报告对照实时字节计算，这里不写死——
#: 写死一次就会在每次无害编辑后变成"锁内适配"噪音；冻结由 T5 的 LOCK 做。
def service_sha256() -> str:
    import hashlib

    return hashlib.sha256(service_source()).hexdigest()
