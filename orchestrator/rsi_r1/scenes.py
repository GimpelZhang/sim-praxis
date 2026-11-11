"""RSI-R1 两场景的公开合同与参数应用（AC05/AC07 的接口半边）。

纪律（Plan §3.1–§3.3、§5.3）：

* 场景**只**暴露 typed 参数（id/单位/域/语义），不收自由 Python、MJCF 或
  XML；候选无法覆盖容器镜像、solver、重力或步长类型；
* 本模块**不含参考解、不含判决答案**，也不 import mujoco——真实物理只在
  T3 的固定镜像容器里发生（``sandbox.py``）；
* Tc/dt 用**整数微秒 ticks** 判整除，绝不做浮点模；步数由整数除得出；
* 公开域与固定量集中在本文件，``render_public_contract`` 只从这里渲染。

错误码全部映射到公开条款（``REFUSAL_CLAUSES``），候选据此可自修。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from orchestrator.rsi_r1.lanes import MAX_STEPS_PER_RUN
from orchestrator.rsi_r1.rubric import METRICS

SCHEMA_PREFIX = "simpraxis.rsi_r1"

SCENE_A = "A_contact_friction"
SCENE_B = "B_sampling_integration"

#: 仿真时长与允许的积分步长（秒 → 整数微秒）。
DURATION_S = Decimal("4.0")
ALLOWED_DT_S = (Decimal("0.0005"), Decimal("0.001"), Decimal("0.002"))

#: Study B 的控制采样周期（秒）。
ALLOWED_TC_S = (Decimal("0.004"), Decimal("0.008"), Decimal("0.016"), Decimal("0.032"))


class SceneError(ValueError):
    """参数/设计不合法（可自修，附公开条款 id）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ParamSpec:
    """一个公开可设计参数。``kind`` : float｜int｜enum。"""

    id: str
    unit: str
    kind: str
    lo: "Decimal | None" = None
    hi: "Decimal | None" = None
    values: "tuple[Any, ...]" = ()
    role: str = "physical"      # physical｜initial_state｜controller
    description: str = ""

    def domain_text(self) -> str:
        if self.kind == "enum":
            return "/".join(str(v) for v in self.values)
        return f"[{self.lo}, {self.hi}]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "id": self.id,
            "unit": self.unit,
            "kind": self.kind,
            "domain": ([str(v) for v in self.values] if self.kind == "enum"
                       else [str(self.lo), str(self.hi)]),
            "role": self.role,
            "description": self.description,
        }


@dataclass(frozen=True)
class FixedSpec:
    """公开但不可设计的量（覆盖即拒）。"""

    id: str
    value: Any
    unit: str
    description: str = ""

    def to_dict(self) -> "dict[str, Any]":
        return {"id": self.id, "value": str(self.value), "unit": self.unit,
                "description": self.description}


@dataclass(frozen=True)
class SceneDoc:
    scene_id: str
    study: str
    version: int
    title: str
    description: str
    params: "tuple[ParamSpec, ...]"
    fixed: "tuple[FixedSpec, ...]"
    metrics: "tuple[str, ...]"

    def param(self, pid: str) -> ParamSpec:
        for p in self.params:
            if p.id == pid:
                return p
        raise SceneError("UNKNOWN_PARAM", f"{self.scene_id} 无参数 {pid!r}")

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": f"{SCHEMA_PREFIX}.scene.v1",
            "schema_version": 1,
            "scene_id": self.scene_id,
            "study": self.study,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "fixed": [f.to_dict() for f in self.fixed],
            "metrics": list(self.metrics),
        }


# ---------------------------------------------------------------------------
# Study A：水平面上的单刚体（box，避免滚动混杂）
# ---------------------------------------------------------------------------

SCENE_A_DOC = SceneDoc(
    scene_id=SCENE_A,
    study="A",
    version=1,
    title="水平面单刚体滑动（接触/摩擦作用条件）",
    description=(
        "一个固定几何的长方体在水平面上、受重力、给定初始平移速度；"
        "候选只能改质量、初速与摩擦参数。场景地面/重力/solver/几何固定。"),
    params=(
        ParamSpec("mass_kg", "kg", "float", Decimal("0.2"), Decimal("1.0"),
                  role="physical", description="刚体质量"),
        ParamSpec("v0_mps", "m/s", "float", Decimal("0.1"), Decimal("0.6"),
                  role="initial_state", description="初始沿 +x 的平移速度"),
        ParamSpec("sliding_friction", "1", "float", Decimal("0.1"), Decimal("0.8"),
                  role="physical", description="显式 contact pair 的滑动摩擦系数"),
        ParamSpec("torsional_friction_m", "m", "float", Decimal("0.001"),
                  Decimal("0.05"), role="physical",
                  description="显式 contact pair 的扭转摩擦半径"),
    ),
    fixed=(
        FixedSpec("condim", 3, "1", "contact 维度恒为 3（实际每 contact 逐一核对）"),
        FixedSpec("duration_s", DURATION_S, "s", "仿真时长"),
        FixedSpec("gravity_mps2", Decimal("9.81"), "m/s^2", "重力（+z 向下）"),
        FixedSpec("body_shape", "box", "1", "刚体外形固定；本轮不换 mesh"),
        FixedSpec("solver", "mujoco_default_pinned", "1", "solver 由镜像固定"),
    ),
    metrics=("displacement_x_m", "stop_time_s"),
)

#: 校准阴性控制（§3.2）：基准与 sham 只差 torsional friction，condim 两边恒 3。
SCENE_A_NEGATIVE_CONTROL = {
    "schema": f"{SCHEMA_PREFIX}.negative_control.v1",
    "scene_id": SCENE_A,
    "allowed_diff": ["torsional_friction_m"],
    "contact_dim": 3,
    "metric": "displacement_x_m_negative_equivalence",
    "note": ("sham 与基准除 torsional_friction_m 外逐字段相同；"
             "阴性不能建立在物体没动/没接触/两边无效上"),
}


# ---------------------------------------------------------------------------
# Study B：无接触单关节（开放问题：Tc × dt 耦合）
# ---------------------------------------------------------------------------

SCENE_B_DOC = SceneDoc(
    scene_id=SCENE_B,
    study="B",
    version=1,
    title="无接触单关节跟踪（控制采样 Tc 与积分步长 dt）",
    description=(
        "单关节、重力关闭、粘性阻尼固定；目标 q*=A sin(2πft)；"
        "控制器 P 或 PD，扭矩每次更新后限幅。候选自主选择家族/参数/扫描/配对条件；"
        "本场景不给出优劣方向。"),
    params=(
        ParamSpec("inertia_ratio", "1", "float", Decimal("0.5"), Decimal("2.0"),
                  role="physical", description="I/I0（I0=0.05 kg·m^2）"),
        ParamSpec("target_amplitude_rad", "rad", "float", Decimal("0.1"),
                  Decimal("0.4"), role="physical", description="目标幅值 A"),
        ParamSpec("target_freq_hz", "Hz", "float", Decimal("0.2"), Decimal("2.0"),
                  role="physical", description="目标频率 f"),
        ParamSpec("tc_s", "s", "enum", values=ALLOWED_TC_S, role="controller",
                  description="控制采样周期 Tc（Tc/dt 必须为整数）"),
        ParamSpec("dt_s", "s", "enum", values=ALLOWED_DT_S, role="controller",
                  description="物理积分步长 dt"),
        ParamSpec("controller", "1", "enum",
                  values=("P", "PD"), role="controller", description="控制器家族"),
        ParamSpec("kp", "Nm/rad", "float", Decimal("0.1"), Decimal("20"),
                  role="controller", description="比例增益 Kp"),
        ParamSpec("kd", "Nm·s/rad", "float", Decimal("0"), Decimal("2"),
                  role="controller", description="微分增益 Kd（P 控制时恒 0）"),
    ),
    fixed=(
        FixedSpec("inertia_base_kgm2", Decimal("0.05"), "kg·m^2", "基准惯量 I0"),
        FixedSpec("damping_nms_rad", Decimal("0.05"), "Nm·s/rad", "粘性阻尼 b"),
        FixedSpec("torque_limit_nm", Decimal("2.0"), "Nm", "力矩限幅 ±2 Nm"),
        FixedSpec("gravity", "off", "1", "重力关闭"),
        FixedSpec("duration_s", DURATION_S, "s", "仿真时长"),
        FixedSpec("solver", "mujoco_default_pinned", "1", "solver 由镜像固定"),
    ),
    metrics=("normalized_tracking_rmse",),
)


SCENE_REGISTRY: "Mapping[str, SceneDoc]" = {
    SCENE_A: SCENE_A_DOC,
    SCENE_B: SCENE_B_DOC,
}


def scene_doc(scene_id: str) -> SceneDoc:
    try:
        return SCENE_REGISTRY[scene_id]
    except KeyError:
        raise SceneError("UNKNOWN_SCENE", f"未知场景 {scene_id!r}") from None


# ---------------------------------------------------------------------------
# 数值规范化
# ---------------------------------------------------------------------------

def _decimal(raw: Any, pid: str) -> Decimal:
    if isinstance(raw, bool):
        raise SceneError("BOOL_AS_NUMBER", f"{pid} 不接受布尔值当数值")
    if isinstance(raw, float):
        # float 入参一律拒绝：避免二进制小数污染 ticks/整除判定（§5.3）。
        raise SceneError("FLOAT_INPUT",
                         f"{pid} 必须用字符串或整数十进制字面量，不接受 float")
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise SceneError("NON_NUMERIC", f"{pid}={raw!r} 不是十进制数") from None
    if not d.is_finite():
        raise SceneError("NONFINITE", f"{pid}={raw!r} 为 NaN/Inf")
    return d


def _micros(seconds: Decimal, what: str) -> int:
    """秒 → 整数微秒（精确，不容忍亚微秒余数）。"""
    scaled = seconds * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise SceneError("TICK_NONDIVISIBLE",
                         f"{what}={seconds} s 不是整数微秒，无法精确 ticks")
    return int(scaled)


def run_shape(dt_s: Decimal, *, tc_s: "Decimal | None" = None,
              duration_s: Decimal = DURATION_S) -> "dict[str, int]":
    """由秒制参数推出整数 ticks 形状；整除/步数帽就在这里判。"""
    dt_us = _micros(dt_s, "dt_s")
    dur_us = _micros(duration_s, "duration_s")
    if dt_us <= 0:
        raise SceneError("DOMAIN", "dt_s 必须 > 0")
    if dur_us % dt_us != 0:
        raise SceneError("TICK_NONDIVISIBLE",
                         f"duration {duration_s}s 不能被 dt {dt_s}s 整除")
    nsteps = dur_us // dt_us
    if nsteps > MAX_STEPS_PER_RUN:
        raise SceneError("STEP_LIMIT",
                         f"nsteps={nsteps} 超过每 run 上限 {MAX_STEPS_PER_RUN}")
    shape = {"dt_ticks_us": dt_us, "duration_ticks_us": dur_us, "nsteps": nsteps}
    if tc_s is not None:
        tc_us = _micros(tc_s, "tc_s")
        if tc_us % dt_us != 0:
            raise SceneError("TICK_NONDIVISIBLE", f"Tc {tc_s}s 不是 dt {dt_s}s 的整数倍")
        shape["tc_ticks_us"] = tc_us
        shape["control_updates"] = dur_us // tc_us
    return shape


def normalise_params(scene_id: str, params: Mapping[str, Any]) -> "dict[str, Any]":
    """把候选给的 typed 参数规范化；缺/多/越域/错类型一律带码拒绝。"""
    doc = scene_doc(scene_id)
    allowed = {p.id for p in doc.params}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise SceneError("UNKNOWN_PARAM", f"{scene_id} 不接受 {unknown}")
    out: "dict[str, Any]" = {}
    for spec in doc.params:
        if spec.id not in params:
            raise SceneError("MISSING_PARAM", f"{scene_id} 缺参数 {spec.id}")
        raw = params[spec.id]
        if spec.kind == "enum":
            if spec.id in ("tc_s", "dt_s"):
                value = _decimal(raw, spec.id)
                if value not in spec.values:
                    raise SceneError(
                        "ENUM_INVALID",
                        f"{spec.id}={raw} 不在允许集合 "
                        f"{[str(v) for v in spec.values]}")
                out[spec.id] = str(value.normalize()) if value != 0 else "0"
            else:
                if raw not in spec.values:
                    raise SceneError("ENUM_INVALID",
                                     f"{spec.id}={raw!r} 不在 {spec.values}")
                out[spec.id] = raw
            continue
        value = _decimal(raw, spec.id)
        if spec.kind == "int":
            if value != value.to_integral_value():
                raise SceneError("DOMAIN", f"{spec.id} 必须是整数")
        lo, hi = spec.lo, spec.hi
        if lo is not None and value < lo:
            raise SceneError("DOMAIN", f"{spec.id}={value} < 下界 {lo}")
        if hi is not None and value > hi:
            raise SceneError("DOMAIN", f"{spec.id}={value} > 上界 {hi}")
        out[spec.id] = str(value)
    if scene_id == SCENE_B:
        if out["controller"] == "P" and Decimal(out["kd"]) != 0:
            raise SceneError("DOMAIN", "P 控制下 kd 必须为 0")
    return out


def parameter_ranges(scene_id: str) -> "dict[str, tuple[Decimal, Decimal]]":
    """非枚举参数的公开域（confirm 分布非退化检查用）。"""
    out: "dict[str, tuple[Decimal, Decimal]]" = {}
    for spec in scene_doc(scene_id).params:
        if spec.kind != "enum" and spec.lo is not None and spec.hi is not None:
            out[spec.id] = (spec.lo, spec.hi)
    return out


def check_distribution_non_degenerate(scene_id: str,
                                      ranges: Mapping[str, Iterable[Any]]):
    """confirm 分布至少一个**非干预**物理量域宽 ≥ 20%（§3.4）。

    ``ranges``: 参数 id → (lo, hi) 的实际采样区间（已由 host 计划给出）。
    """
    from orchestrator.rsi_r1.rubric import MIN_PARAM_DOMAIN_FRACTION

    doc = scene_doc(scene_id)
    physical = {p.id for p in doc.params if p.role != "controller"}
    seen = []
    for pid, (lo, hi) in ranges.items():
        if pid not in physical:
            continue
        full = parameter_ranges(scene_id).get(pid)
        if full is None:
            continue
        lo, hi = Decimal(str(lo)), Decimal(str(hi))
        if lo > hi:
            raise SceneError("DISTRIBUTION", f"{pid} 区间倒置 [{lo},{hi}]")
        if hi < full[0] or lo > full[1]:
            raise SceneError("DISTRIBUTION", f"{pid} 区间越出公开域 {full}")
        width = hi - lo
        frac = width / (full[1] - full[0])
        seen.append((pid, frac))
        if frac >= MIN_PARAM_DOMAIN_FRACTION:
            return {"ok": True, "param": pid, "fraction": str(frac)}
    detail = ", ".join(f"{p}={f}" for p, f in seen) or "无物理量"
    raise SceneError("DISTRIBUTION_DEGENERATE",
                     f"没有任何非干预物理量域宽达到 {MIN_PARAM_DOMAIN_FRACTION}：{detail}")


def metric_ids(scene_id: str) -> "tuple[str, ...]":
    doc = scene_doc(scene_id)
    for m in doc.metrics:
        if m not in METRICS:
            raise SceneError("METRIC_UNREGISTERED", f"{scene_id} 指标 {m} 未在量表注册")
    return doc.metrics
