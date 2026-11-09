"""RSI-R0.1 九实例任务合同：公开任务书与私有真值的唯一出处。

计划 §4.1 的落地。两类信息物理隔离：

* **公开**（进 prompt、进候选可读目录）：场景参数、可提交的数值键与边界、
  steps 上限、目标/问题描述、公开假设方向 ``h``；
* **私有**（只落 ``/mnt`` 数据层，永不进 prompt/挂载）：D 的故障种类与幅度、
  E/D 的通过容差与参考增益、R 的期望方向与四格参数矩阵。

R 族参数矩阵按计划 §4.1 表格**固定**（不是让候选挑最容易通过的参数）。R-dev
借用旧 R0 已公开诊断参数，明确标注为回归锚、非独立新证据；两个验证组合为本
计划拟定，T2 必须先做物理校准（四格完整观测、收敛、可判别），未满足即
STOP_TASK_DESIGN。

场景与故障的实现复用 R0 已校准纯组件（``scenes.py`` / ``faults.py``），不重写。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r0.faults import apply_fault, validate_fault
from orchestrator.rsi_r0.scenes import build_pendulum_scene, build_push_scene

#: R 族冻结的物理常量（长度/质量/时间步；阻尼按格注入，不写死在这里）。
R_SCENE = {"pendulum_length": 0.5, "pendulum_mass": 0.5, "timestep": S.DT_S}

#: R 固定参数矩阵（计划 §4.1 表；顺序即为公开文档顺序）。
R_MATRIX: Mapping[str, Mapping[str, Any]] = {
    "R-dev": {"damping_low": 0.15, "damping_high": 0.45,
              "theta0_low": -0.8, "theta0_high": 0.8, "window_s": 4.0, "h": 1,
              "evidence_grade": "regression_anchor_from_R0"},
    "R-val-1": {"damping_low": 0.12, "damping_high": 0.35,
                "theta0_low": -0.6, "theta0_high": 0.6, "window_s": 4.0, "h": 1,
                "evidence_grade": "new_combination_unmeasured"},
    "R-val-2": {"damping_low": 0.18, "damping_high": 0.50,
                "theta0_low": -1.0, "theta0_high": 1.0, "window_s": 4.0, "h": -1,
                "evidence_grade": "new_combination_unmeasured"},
}


class TaskContractError(ValueError):
    """任务表不合法（维护侧 bug，须落 DESIGN_NOT_READY 而不是候选失败）。"""


@dataclass(frozen=True)
class Instance:
    instance_id: str
    family: str
    role: str
    template: str
    public: dict[str, Any]
    private: dict[str, Any]

    def as_public_dict(self) -> dict[str, Any]:
        return {"instance_id": self.instance_id, "family": self.family,
                "role": self.role, "template": self.template,
                "public": dict(self.public)}

    def as_private_dict(self) -> dict[str, Any]:
        return {"instance_id": self.instance_id, "family": self.family,
                "role": self.role, "template": self.template,
                "private": dict(self.private)}

    def scene_xml(self, params: Mapping[str, Any] | None = None) -> str:
        """构造**注入故障前**的场景（故障在 :meth:`faulted_scene` 里加）。"""
        if self.template == "pendulum":
            base = dict(R_SCENE)
            base.update(params or {})
            base.setdefault("damping", 0.05)
            return build_pendulum_scene(base)
        base = dict(self.public["scene_params"])
        base.update(params or {})
        return build_push_scene(base)

    def faulted_scene(self, params: Mapping[str, Any] | None = None) -> str:
        """公开场景 + 宿主注入故障（只有 D 族有故障；E/R 原样返回）。"""
        scene = self.scene_xml(params)
        fault = self.private.get("fault")
        if not fault:
            return scene
        return apply_fault(scene, fault)

    @property
    def host_force_scale(self) -> float | None:
        """``force_scale`` 故障的乘子由宿主注入请求；候选请求体里没有该项。"""
        fault = self.private.get("fault")
        if fault and fault.get("kind") == "force_scale":
            return float(fault["scale"])
        return None

    @property
    def param_bounds(self) -> dict[str, tuple[float, float]]:
        return {k: (float(v[0]), float(v[1]))
                for k, v in self.private["param_bounds"].items()}

    @property
    def max_steps(self) -> int:
        return int(self.private["steps_max"])

    @property
    def tolerance(self) -> dict[str, float]:
        return dict(self.private["tolerance"])

    @property
    def r_cells(self) -> list[dict[str, Any]]:
        """四格合同（damping × 初角）；顺序 low/plus, low/minus, high/plus, high/minus。"""
        m = R_MATRIX[self.instance_id]
        return [
            {"damping": m["damping_low"], "theta0": m["theta0_high"], "cell": "low/plus"},
            {"damping": m["damping_low"], "theta0": m["theta0_low"], "cell": "low/minus"},
            {"damping": m["damping_high"], "theta0": m["theta0_high"], "cell": "high/plus"},
            {"damping": m["damping_high"], "theta0": m["theta0_low"], "cell": "high/minus"},
        ]


_PUSH_KEYS = {"kp": (0.0, 5000.0), "kd": (0.0, 500.0), "max_force": (0.0, 500.0)}
_RUN_KEYS = {"damping": (0.0, 5.0), "initial_angle": (-3.2, 3.2), "steps": (1.0, 2000.0)}


def _push_public(name: str, scene: dict[str, Any], target: tuple[float, float],
                 steps_max: int, note: str) -> dict[str, Any]:
    return {"task": name, "scene_params": scene,
            "interface": {"kind": "push_control", "keys": sorted(_PUSH_KEYS),
                          "limits": {k: list(v) for k, v in _PUSH_KEYS.items()},
                          "steps_max": steps_max},
            "goal": {"type": "reach_and_settle", "target": list(target),
                     "note": note}}


def _r_public(name: str, h: int, matrix: Mapping[str, Any], note: str) -> dict[str, Any]:
    return {"task": name,
            "scene_params": dict(R_SCENE, damping=0.05),
            "interface": {"kind": "free_run",
                          "keys": ["damping", "initial_angle", "steps"],
                          "limits": {k: list(v) for k, v in _RUN_KEYS.items()},
                          "steps_max": S.N_STEPS},
            "research": {
                "question": "在同一初角下，较高关节阻尼是否使摆更快进入并保持在静止带内？",
                "h": int(h), "h_meaning": ("+1 = 高阻尼缩短沉降时间；−1 = 高阻尼延长沉降时间"),
                "damping_levels": [matrix["damping_low"], matrix["damping_high"]],
                "initial_angles": [matrix["theta0_high"], matrix["theta0_low"]],
                "window_s": matrix["window_s"],
                "band": {"theta_rad": S.THETA_BAND, "omega_rad_s": S.OMEGA_BAND,
                         "strict": True},
                "effect_definition": "(t_low − t_high)/t_low；两角度算术平均",
                "delta": S.DELTA,
                "note": note,
            }}


def _instances() -> tuple[Instance, ...]:
    out: list[Instance] = []

    # ---------------- E：真实执行（开发 + 两个验证） ----------------
    out.append(Instance(
        "E-dev", "E", "dev", "push",
        _push_public("把箱子推到目标点并静止（标定用）",
                     {"box_mass": 1.5, "box_half": 0.05, "friction": 0.2,
                      "timestep": S.DT_S}, (0.5, 0.0), 3000,
                     "无遮挡；用于标定接口手感，不构成验证样本。"),
        {"tolerance": {"distance": 0.06, "final_speed": 0.02},
         "reference_gains": {"kp": 80.0, "kd": 10.0, "max_force": 40.0},
         "reference_steps": 2000, "param_bounds": dict(_PUSH_KEYS), "steps_max": 3000}))
    out.append(Instance(
        "E-val-1", "E", "validation", "push",
        _push_public("把箱子推到目标点并静止",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.3,
                      "timestep": S.DT_S}, (0.6, 0.0), 3000,
                     "无遮挡直线任务；目标在 +x 方向。"),
        {"tolerance": {"distance": 0.06, "final_speed": 0.02},
         "reference_gains": {"kp": 120.0, "kd": 12.0, "max_force": 60.0},
         "reference_steps": 2000, "param_bounds": dict(_PUSH_KEYS), "steps_max": 3000}))
    out.append(Instance(
        "E-val-2", "E", "validation", "push",
        _push_public("绕过障碍柱把箱子推到目标点并静止",
                     {"box_mass": 2.0, "box_half": 0.05, "friction": 0.4,
                      "timestep": S.DT_S, "obstacle_x": 0.3, "obstacle_r": 0.08},
                     (0.7, 0.22), 4000,
                     "x=0.3 处有圆柱障碍；直线加力会被挡住，必须分离目标方向。"),
        {"tolerance": {"distance": 0.07, "final_speed": 0.02},
         "reference_gains": {"kp": 150.0, "kd": 18.0, "max_force": 80.0},
         "reference_steps": 3000, "param_bounds": dict(_PUSH_KEYS), "steps_max": 4000}))

    # ---------------- D：单故障诊断（dev + 两个不同验证故障） ----------------
    d_common = {"tolerance": {"distance": 0.06, "final_speed": 0.02},
                "reference_steps": 2000,
                "param_bounds": dict(_PUSH_KEYS), "steps_max": 3000}
    out.append(Instance(
        "D-dev", "D", "dev", "push",
        _push_public("把箱子推到目标点并静止（本实例存在一个未公开的施加层故障）",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": S.DT_S}, (0.6, 0.0), 3000,
                     "标定实例：故障种类与幅度已在本轮公开（friction_scale 2.0）。"),
        dict(d_common, fault={"kind": "friction_scale", "scale": 2.0},
             fault_public_in_dev=True,
             reference_gains={"kp": 120.0, "kd": 12.0, "max_force": 60.0})))
    out.append(Instance(
        "D-val-1", "D", "validation", "push",
        _push_public("把箱子推到目标点并静止（本实例存在一个未公开的施加层故障）",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": S.DT_S}, (0.6, 0.0), 3000,
                     "故障种类与幅度均不公开；通过公开实验与数值响应辨识。"),
        dict(d_common, fault={"kind": "scene_tilt", "scale": 6.0},
             fault_public_in_dev=False,
             reference_gains={"kp": 120.0, "kd": 12.0, "max_force": 60.0})))
    out.append(Instance(
        "D-val-2", "D", "validation", "push",
        _push_public("把箱子推到目标点并静止（本实例存在一个未公开的施加层故障）",
                     {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25,
                      "timestep": S.DT_S}, (0.6, 0.0), 3000,
                     "故障种类与幅度均不公开；通过公开实验与数值响应辨识。"),
        dict(d_common, fault={"kind": "force_scale", "scale": 0.4},
             fault_public_in_dev=False,
             reference_gains={"kp": 120.0, "kd": 12.0, "max_force": 60.0})))

    # ---------------- R：受控微研究（固定矩阵） ----------------
    for iid, role in (("R-dev", "dev"), ("R-val-1", "validation"),
                      ("R-val-2", "validation")):
        m = R_MATRIX[iid]
        note = ("回归锚：参数借自旧 R0 已公开诊断，不构成独立新证据。" if iid == "R-dev"
                else "本轮拟定组合，T2 须先完成物理校准与可判别性检查。")
        out.append(Instance(
            iid, "R", role, "pendulum",
            _r_public(f"阻尼摆受控研究（{iid}）", int(m["h"]), m, note),
            {"param_bounds": dict(_RUN_KEYS), "steps_max": S.N_STEPS,
             "cells": m, "expected_mechanism": "higher_damping_shorter_settle"
             if m["h"] == 1 else "higher_damping_longer_settle",
             "settle_band": {"theta": S.THETA_BAND, "omega": S.OMEGA_BAND}}))
    return tuple(out)


INSTANCES: tuple[Instance, ...] = _instances()
BY_ID: Mapping[str, Instance] = {i.instance_id: i for i in INSTANCES}

DEV_IDS = ("E-dev", "D-dev", "R-dev")
VALIDATION_IDS = ("E-val-1", "D-val-1", "R-val-1", "E-val-2", "D-val-2", "R-val-2")


def get(instance_id: str) -> Instance:
    try:
        return BY_ID[instance_id]
    except KeyError as exc:
        raise TaskContractError(f"未知实例 {instance_id!r}") from exc


def by_family(family: str, role: str | None = None) -> list[Instance]:
    rows = [i for i in INSTANCES if i.family == family]
    if role is not None:
        rows = [i for i in rows if i.role == role]
    return rows


def assert_matrix_complete() -> list[str]:
    """9 实例齐全、字段无缺、私有真值不得出现在公开面（AC10）。"""
    problems: list[str] = []
    if len(INSTANCES) != 9:
        problems.append(f"实例数 {len(INSTANCES)} ≠ 9")
    for inst in INSTANCES:
        for key in ("instance_id", "family", "role", "template", "public", "private"):
            if key not in inst.as_public_dict() and key not in inst.as_private_dict():
                problems.append(f"{inst.instance_id}: 缺 {key}")
        if inst.family not in S.Family.ALL:
            problems.append(f"{inst.instance_id}: 未知族 {inst.family}")
        if inst.role not in S.Tariff.ALL:
            problems.append(f"{inst.instance_id}: 未知角色 {inst.role}")
        if inst.family == "R":
            if inst.instance_id not in R_MATRIX:
                problems.append(f"{inst.instance_id}: 缺 R 矩阵")
            elif len(inst.r_cells) != 4:
                problems.append(f"{inst.instance_id}: 四格不齐")
        else:
            if "tolerance" not in inst.private:
                problems.append(f"{inst.instance_id}: 缺容差")
            if "param_bounds" not in inst.private:
                problems.append(f"{inst.instance_id}: 缺参数边界")
        secret = _secret_keys(inst)
        leaked = _leaks(inst.public, secret)
        if leaked:
            problems.append(f"{inst.instance_id}: 公开面泄漏私有键 {leaked}")
    for fam in S.Family.ALL:
        if len(by_family(fam, "dev")) != 1:
            problems.append(f"{fam} 族开发实例不是恰好 1 个")
        if len(by_family(fam, "validation")) != 2:
            problems.append(f"{fam} 族验证实例不是恰好 2 个")
    return problems


_SECRET_KEYS = frozenset({"fault", "tolerance", "reference_gains", "expected",
                          "expected_mechanism", "cells", "settle_band",
                          "fault_public_in_dev"})


def _secret_keys(inst: Instance) -> frozenset[str]:
    if inst.family == "D":
        return _SECRET_KEYS
    return _SECRET_KEYS - {"fault"}


def _leaks(obj: Any, secret: frozenset[str], path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in secret:
                found.append(f"{path}.{k}".lstrip("."))
            found.extend(_leaks(v, secret, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_leaks(v, secret, f"{path}[{i}]"))
    return found


# ---------------------------------------------------------------------------
# 导出：公开面 -> public/，私有面 -> private/（物理隔离）
# ---------------------------------------------------------------------------


def task_matrix() -> dict[str, Any]:
    return {"schema": S.SCHEMA_TASK_MATRIX,
            "post_caps": {"protocol": S.PROTOCOL_POSTS,
                          "dev_per_family": S.DEV_POSTS_PER_FAMILY,
                          "val_per_family": S.VAL_POSTS_PER_FAMILY,
                          "total": S.TOTAL_NORMAL_POSTS},
            "dev_order": list(DEV_IDS),
            "validation_order": list(VALIDATION_IDS),
            "r_matrix": {k: dict(v) for k, v in R_MATRIX.items()},
            "instances": [i.as_public_dict() for i in INSTANCES]}


def private_matrix() -> dict[str, Any]:
    return {"schema": S.SCHEMA_TASK_MATRIX,
            "visibility": "PRIVATE_DO_NOT_SHIP",
            "instances": [i.as_private_dict() for i in INSTANCES]}


def measurement_contract() -> dict[str, Any]:
    """数值合同的可机读副本（判据常量单一真值仍在 :mod:`rsi_r01.schemas`）。"""
    return {"schema": S.SCHEMA_MEASUREMENT_CONTRACT,
            "dt_s": S.DT_S, "n_steps": S.N_STEPS, "expected_rows": S.EXPECTED_ROWS,
            "t_end_s": S.T_END_S,
            "band": {"theta_rad": S.THETA_BAND, "omega_rad_s": S.OMEGA_BAND,
                     "strict": True},
            "settling_definition": "最早 i 使 ∀j≥i 均在带内；末样本越界即删失",
            "effect_definition": "e_a=(t_low,a−t_high,a)/t_low,a；mean=算术平均",
            "delta": S.DELTA,
            "claim_rule": {"SUPPORTED": "z≥δ", "REFUTED": "z≤−δ",
                           "INCONCLUSIVE": "otherwise", "z": "h×mean_effect"},
            "censoring_rule": "任一必要时间删失/t_low=0/数据不完整 ⇒ mean_effect=null",
            "computation_tolerance": {"effect_relative": 1e-12,
                                      "settling_index": 0},
            "physical_convergence": {"settle_abs_s": 0.02, "effect_abs": 0.01},
            "units": {"t": "s", "theta": "rad", "omega": "rad/s"}}


def prereg_expected_cells(instance_id: str) -> list[tuple[float, float]] | None:
    """R 实例的冻结四格；非 R 返回 None（不做四格校验）。"""
    inst = get(instance_id)
    if inst.family != "R":
        return None
    return [(c["damping"], c["theta0"]) for c in inst.r_cells]


def validate_fault_table() -> list[str]:
    """逐个 D 实例校验故障描述合法（越界/未知种类即任务表不可用）。"""
    problems: list[str] = []
    for inst in by_family("D"):
        try:
            validate_fault(inst.private["fault"])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{inst.instance_id}: 故障非法 {exc}")
    kinds = {i.private["fault"]["kind"] for i in by_family("D")}
    if len(kinds) != 3:
        problems.append(f"D 族故障种类必须是三个互异机制，收到 {sorted(kinds)}")
    if "mass_scale" in kinds:
        problems.append("禁止重新引入 mass_scale 不可辨识项")
    return problems
