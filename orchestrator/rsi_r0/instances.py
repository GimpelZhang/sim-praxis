"""RSI-R0 九个冻结实例的**单一真值**（公开面 + 私有面分开存放）。

计划 §3.3：T2 在任何模型试次之前冻结九个 instance ID、输入、公开任务书、真值、
种子和顺序。本模块就是那份冻结表——脚本与测试都从这里取，禁止各自抄字面量。

隔离要求（§3.3「不是只换 seed」）：

* E 族三个实例在**机制**上不同：dev 为纯参数标定；v1 无遮挡直线；v2 带圆柱
  遮挡（必须绕过，不能只靠调大增益）。
* D 族三个实例各自注入**不同种类**的故障（质量/摩擦/执行增益），且都可从
  公开标称参数 + 有界探测**物理辨识**（不是猜）。
* R 族三个实例分别是：强效应（结论应为 SUPPORTED）、中等效应、以及效应低于
  事前等效界（诚实结论应为阴/INCONCLUSIVE）。

公开面写进 ``benchmarks/rsi_r0/tasks/<id>/task.yaml``；私有面（真值/容差/
预期方向/等效界）只写数据层 ``private/instances/<id>.json``，候选无权读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.rsi_r0.contracts import (
    INSTANCE_IDS,
    Family,
    instance_family,
    instance_task_id,
    is_development,
)


@dataclass(frozen=True)
class InstancePlan:
    instance_id: str
    template: str                 # "push" | "pendulum"
    order_index: int
    public: dict[str, Any]        # 公开任务书正文（进 task.yaml）
    private: dict[str, Any]       # 真值/容差/预期（只进数据层）

    @property
    def family(self) -> Family:
        return instance_family(self.instance_id)

    @property
    def role(self) -> str:
        return "development" if is_development(self.instance_id) else "validation"

    @property
    def task_id(self) -> int:
        return instance_task_id(self.instance_id)


_PUSH_IFACE = {
    "kind": "push_control",
    "keys": ["kp", "kd", "max_force"],
    "limits": {"kp": [0.0, 5000.0], "kd": [0.0, 500.0], "max_force": [0.0, 500.0]},
}

_PEND_IFACE = {
    "kind": "free_run",
    "keys": ["initial_angle"],
    "limits": {"initial_angle": [-3.2, 3.2], "steps": [50, 2000]},
}


def _push_public(name: str, params: dict, target: tuple[float, float],
                 steps_max: int, note: str) -> dict[str, Any]:
    return {
        "template": "push",
        "scene_params": params,
        "interface": dict(_PUSH_IFACE),
        "steps_max": steps_max,
        "goal": {
            "type": "reach_and_settle",
            "target": [target[0], target[1]],
            "description": name,
            "note": note,
        },
    }


def _pend_public(params: dict, note: str) -> dict[str, Any]:
    return {
        "template": "pendulum",
        "scene_params": params,
        "interface": dict(_PEND_IFACE),
        "steps_max": 2000,
        "goal": {
            "type": "controlled_experiment",
            "intervention": "两个自选阻尼水平（由维护侧注入到场景的 damping 属性）",
            "groups": "两个初始角（自选，|a| ≤ 3.2 rad）",
            "min_steps": 50,
            "description": note,
        },
    }


PLANS: tuple[InstancePlan, ...] = (
    # ---------------- E 族：真实执行 ----------------
    InstancePlan(
        "E-dev", "push", 0,
        _push_public("把箱子推到目标点并静止（标定用）",
                     {"box_mass": 1.5, "box_half": 0.05, "friction": 0.2,
                      "timestep": 0.002},
                     (0.5, 0.0), 3000,
                     "无遮挡；用于标定接口手感，不构成验证样本。"),
        {"tolerance": {"distance": 0.06, "final_speed": 0.02},
         "reference_gains": {"kp": 80.0, "kd": 10.0, "max_force": 40.0},
         "reference_steps": 2000},
    ),
    InstancePlan(
        "E-v1", "push", 1,
        _push_public("把箱子推到目标点并静止",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.3,
                      "timestep": 0.002},
                     (0.6, 0.0), 3000,
                     "无遮挡直线任务；目标在 +x 方向。"),
        {"tolerance": {"distance": 0.06, "final_speed": 0.02},
         "reference_gains": {"kp": 120.0, "kd": 12.0, "max_force": 60.0},
         "reference_steps": 2000},
    ),
    InstancePlan(
        "E-v2", "push", 2,
        _push_public("绕过障碍柱把箱子推到目标点并静止",
                     {"box_mass": 2.0, "box_half": 0.05, "friction": 0.4,
                      "timestep": 0.002, "obstacle_x": 0.3, "obstacle_r": 0.08},
                     (0.7, 0.22), 4000,
                     "x=0.3 处有圆柱障碍；直线加力会被挡住，必须分离目标方向。"),
        {"tolerance": {"distance": 0.07, "final_speed": 0.02},
         "reference_gains": {"kp": 150.0, "kd": 18.0, "max_force": 80.0},
         "reference_steps": 3000},
    ),
    # ---------------- D 族：单故障诊断 ----------------
    InstancePlan(
        "D-dev", "push", 3,
        _push_public("诊断：box 与地面之间的摩擦被按比例改变（标定用）",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": 0.0005},
                     (0.6, 0.0), 3000,
                     "提出假设：scene_tilt / friction_scale / force_scale 三者之一，"
                     "并给出幅度估计（标定实例：真值已公开）。"),
        {"fault": {"kind": "friction_scale", "scale": 2.0},
         "tolerance": {"kind": "exact", "scale_rel": 0.20},
         "probe_forces": {"pos": [13.0, 16.0], "neg": [-13.0, -16.0]},
         "probe_steps": 600,
         "nominal": {"mass": 1.0, "friction": 0.25, "g": 9.81},
         "classify": {"asymmetry_min": 0.5, "deviation_min": 0.15}},
    ),
    InstancePlan(
        "D-v1", "push", 4,
        _push_public("诊断：本实例的故障种类与幅度均不公开",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": 0.0005},
                     (0.6, 0.0), 3000,
                     "提出假设：scene_tilt / friction_scale / force_scale 三者之一，"
                     "并给出幅度估计。"),
        {"fault": {"kind": "scene_tilt", "scale": 6.0},
         "tolerance": {"kind": "exact", "scale_rel": 0.20},
         "probe_forces": {"pos": [13.0, 16.0], "neg": [-13.0, -16.0]},
         "probe_steps": 600,
         "nominal": {"mass": 1.0, "friction": 0.25, "g": 9.81},
         "classify": {"asymmetry_min": 0.5, "deviation_min": 0.15}},
    ),
    InstancePlan(
        "D-v2", "push", 5,
        _push_public("诊断：本实例的故障种类与幅度均不公开",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": 0.0005},
                     (0.6, 0.0), 3000,
                     "提出假设：scene_tilt / friction_scale / force_scale 三者之一，"
                     "并给出幅度估计。"),
        {"fault": {"kind": "force_scale", "scale": 0.4},
         "tolerance": {"kind": "exact", "scale_rel": 0.20},
         "probe_forces": {"pos": [13.0, 16.0], "neg": [-13.0, -16.0]},
         "probe_steps": 600,
         "nominal": {"mass": 1.0, "friction": 0.25, "g": 9.81},
         "classify": {"asymmetry_min": 0.5, "deviation_min": 0.15}},
    ),
    # ---------------- R 族：受控微研究 ----------------
    InstancePlan(
        "R-dev", "pendulum", 6,
        _pend_public({"pendulum_length": 0.5, "pendulum_mass": 0.5,
                      "damping": 0.05, "timestep": 0.002},
                     "研究问题：增大关节阻尼是否缩短单摆的沉降时间（标定用）。"),
        {"levels": [0.15, 0.45], "initial_angles": [1.0, -1.0],
         "equivalence_bound": 0.05,
         "expected": "SUPPORTED", "expected_direction": "damping_up_settling_down",
         "steps": 2000},
    ),
    InstancePlan(
        "R-v1", "pendulum", 7,
        _pend_public({"pendulum_length": 0.8, "pendulum_mass": 0.4,
                      "damping": 0.05, "timestep": 0.002},
                     "研究问题：增大关节阻尼是否缩短沉降时间。"),
        {"levels": [0.3, 0.6], "initial_angles": [0.9, -0.9],
         "equivalence_bound": 0.05, "support_threshold": 0.15,
         "expected": "SUPPORTED", "expected_direction": "damping_up_settling_down",
         "steps": 2000},
    ),
    InstancePlan(
        "R-v2", "pendulum", 8,
        _pend_public({"pendulum_length": 0.8, "pendulum_mass": 0.4,
                      "damping": 0.05, "timestep": 0.002},
                     "研究问题：把阻尼从 0.30 微调到 0.31，沉降时间是否**有意义地**变化？"),
        {"levels": [0.30, 0.31], "initial_angles": [0.9, -0.9],
         "equivalence_bound": 0.05, "support_threshold": 0.15,
         "expected": "INCONCLUSIVE", "expected_direction": "within_equivalence_bound",
         "steps": 2000},
    ),
)


def plan_for(instance_id: str) -> InstancePlan:
    for plan in PLANS:
        if plan.instance_id == instance_id:
            return plan
    raise KeyError(f"未知实例：{instance_id!r}")


def assert_frozen_table() -> None:
    """冻结表自检：九实例齐、顺序一致、公开面不得含私有键。"""
    ids = tuple(p.instance_id for p in PLANS)
    if ids != INSTANCE_IDS:
        raise AssertionError(f"实例表顺序与合同不符：{ids} != {INSTANCE_IDS}")
    secret_keys = {"fault", "expected", "expected_direction", "equivalence_bound",
                   "levels", "tolerance", "reference_gains"}
    for plan in PLANS:
        leaked = secret_keys & set(plan.public.get("goal", {})) | (secret_keys & set(plan.public))
        if leaked:
            raise AssertionError(f"{plan.instance_id} 公开面泄漏私有键：{sorted(leaked)}")
        if not plan.private:
            raise AssertionError(f"{plan.instance_id} 缺私有面")
    order = [p.order_index for p in PLANS]
    if order != sorted(order) or len(set(order)) != len(order):
        raise AssertionError(f"order_index 必须唯一且递增：{order}")
