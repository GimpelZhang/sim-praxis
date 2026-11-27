"""S2c S2c-3：Q 池设计 → TaskSpecV13 适配层（复用 build_s2b_c/d 形状）。

把 `design_compiler.compile_design` 产出的 study_design.v1 + 简报 + 真实 regime，
合成为可被 K2–K8 跑批的 TaskSpecV13：
- 条件 = actor 设计的 2×2 单元（factors→scene_param 映射 + 场景默认 + 真实 regime 注入）
  + **host 构建的 NC sham** 条件（actor 不可见 sham 参数 solimp/phase，故 host 造）；
- 主对比 = actor 的 primary_contrast（DiD）；
- NC 槽 = 场景 NC 链（C:solref_time nuisance/solimp sham；D:amplitude/phase），
  base = actor 指定的单元，sham = host 造的 sham 条件。
regime 与 NC-nuisance 是**不同参数**（rev3 解冲突的前提）。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1 import contrasts as X
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_r1.task_specs import TaskSpecV13

__all__ = ["OpenTaskError", "build_open_task", "scene_config"]

# 场景 NC 链配置：sham 参数 + 亚阈扰动 + 等效指标 + 块间变异 nuisance
_SCENE_CFG = {
    S.SCENE_C: {"study": "C_N", "metric_ne": "rebound_height_m_negative_equivalence",
                "sham_param": "solimp_damping", "sham_perturb": Decimal("0.005"),
                "nuisance_param": "solref_time_s", "nuisance_support": ["0.015", "0.025"]},
    S.SCENE_D: {"study": "D_N", "metric_ne": "steady_state_amplitude_rad_negative_equivalence",
                "sham_param": "drive_phase_rad", "sham_perturb": Decimal("0.01"),
                "nuisance_param": "drive_amplitude_nm", "nuisance_support": ["0.9", "1.1"]},
}

#: 场景默认（非轴、非 regime、非 sham 参数取此；与 pool_open._DEFAULTS 一致）
_DEFAULTS = {
    S.SCENE_C: {"solref_damping_ratio": "0.5", "v0_mps": "3.0", "solimp_damping": "0.9",
                "ball_mass_kg": "0.5", "solref_time_s": "0.02", "drop_height_m": "1.0"},
    S.SCENE_D: {"drive_freq_hz": "1.5", "joint_damping": "0.2", "drive_phase_rad": "0.0",
                "drive_amplitude_nm": "1.0", "spring_stiffness_nm_rad": "10.0"},
}


class OpenTaskError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def scene_config(scene_id: str) -> dict:
    try:
        return _SCENE_CFG[scene_id]
    except KeyError:
        raise OpenTaskError("SCENE_UNSUPPORTED", scene_id) from None


def _map_condition(cell: Mapping[str, Any], brief, scene_id: str,
                   true_regime: Mapping[str, str]) -> dict:
    params = dict(_DEFAULTS[scene_id])
    for axis in brief.factor_axes:
        if axis["name"] in cell.get("factors", {}):
            params[axis["scene_param"]] = str(cell["factors"][axis["name"]])
    # 仅接受场景合法参数键（actor 可能过度指定 scene_params 的物理细节/元信息，
    # 这些非法键会被物理层 UNKNOWN_PARAM 拒——过滤掉，不污染）。
    valid = {p.id for p in S.scene_doc(scene_id).params}
    for k, v in (cell.get("scene_params") or {}).items():
        if str(k) in valid:
            params[str(k)] = str(v)
    params.update({str(k): str(v) for k, v in true_regime.items()})  # 真实 regime（fixed）
    return params


def build_open_task(compiled: Mapping[str, Any], brief, *,
                    true_regime: Mapping[str, str],
                    task_id: str | None = None,
                    nc_base_id: "str | None" = None,
                    commitment: "str | None" = None) -> TaskSpecV13:
    """设计 → TaskSpecV13。compiled 来自 compile_design(study_design.v1)。

    只用 actor 的 **primary 2×2 四单元** + host 构建的物理 sham（忽略 actor 自由
    多出的条件与其自造 seed-sham——确定性物理下 seed-sham 退化）。nc_base_id
    由调用方从**原始设计**嵌套 negative_control 提取传入。
    """
    scene_id = str(compiled["scene_id"])
    cfg = scene_config(scene_id)
    metric = str(compiled["metric"])
    cells = compiled.get("conditions")
    if not isinstance(cells, list) or not cells:
        raise OpenTaskError("NO_CONDITIONS", "compiled.conditions 为空")
    pc = compiled["primary_contrast"]
    terms_src = pc["terms"] if isinstance(pc.get("terms"), Mapping) else pc
    four = [str(terms_src[k]) for k in
            ("treatment_high", "treatment_low", "control_high", "control_low")]
    by_id = {str(c["id"]): c for c in cells}
    missing = [i for i in four if i not in by_id]
    if missing:
        raise OpenTaskError("PRIMARY_CELL_MISSING", f"primary 单元缺 {missing}")
    nc = compiled["negative_control"]
    base = str(nc_base_id) if nc_base_id else (
        str(nc["base"].get("condition_id")) if isinstance(nc["base"], Mapping)
        else str(nc["base"]))
    if base not in four:
        # 若 base 不在 4 单元内，退回 treatment_low（一个真实单元）作 NC base
        base = four[1]
    # 条件 = 4 primary 单元（映射）+ host 构建的物理 sham
    base_params = _map_condition(by_id[base], brief, scene_id, true_regime)
    sham_id = "host_sham"
    sham_params = dict(base_params)
    sp = cfg["sham_param"]
    sham_params[sp] = str(Decimal(sham_params[sp]) + cfg["sham_perturb"])
    conditions = tuple(TS._condition(i, scene_id,
                                     _map_condition(by_id[i], brief, scene_id, true_regime))
                       for i in four) + (TS._condition(sham_id, scene_id, sham_params),)

    # 主对比：actor DiD（proposition 合法化：actor 偶把命题写成叙事长文；
    # 方向性 claim 不用于 R1-DESIGN 判词，regime 依赖由审查者+DiD 量级裁定）
    _VALID_PROP = {"increase_gt", "decrease_gt", "equivalent"}
    prop = str(pc.get("proposition", "decrease_gt"))
    if prop not in _VALID_PROP:
        prop = "decrease_gt"
    primary = X.ContrastV13(
        id=str(pc["id"]), kind=X.KIND_DID, metric=metric,
        proposition=prop,
        delta=R.metric_delta(metric),
        terms=X.did_terms(treatment_high=str(terms_src["treatment_high"]),
                          treatment_low=str(terms_src["treatment_low"]),
                          control_high=str(terms_src["control_high"]),
                          control_low=str(terms_src["control_low"])),
    ).validate()

    # NC 槽：场景 NC 链，base=actor 单元，sham=host_sham
    nc_slot = {
        "schema": "simpraxis.rsi_s2.nc_slot.v1",
        "allowed_diff": [sp],
        "max_perturbation": {sp: str(cfg["sham_perturb"])},
        "delta": str(R.metric_delta(cfg["metric_ne"])),
        "ci_level": "0.90",
        "equivalence_metric": cfg["metric_ne"],
        "candidates": four + [sham_id],
        "default": {"base": base, "sham": sham_id},
    }

    tid = task_id or ("q-" + _sha({"brief": brief.brief_id, "scene": scene_id,
                                   "cells": cells, "plan": compiled.get("plan_sha256"),
                                   "regime": dict(true_regime)})[:16])
    spec = TaskSpecV13(
        task_id=tid, mechanism_id=scene_id, split=TS.SPLIT_HOLDOUT,
        scene_version=S.scene_doc(scene_id).version,
        public_question=brief.question,
        allowed_design_space=TS._allowed_space(scene_id),
        estimand={"metric_id": metric, "contrast_ids": [primary.id],
                  "target_population": "32 个宿主分配的确认 block（块间 nuisance 变异）",
                  "aggregation": "paired_block_linear_contrast",
                  "weighting": "equal_block_weight"},
        nuisance_schema={cfg["nuisance_param"]:
                         {"mode": "varying", "support": cfg["nuisance_support"],
                          "assignment_owner": "host",
                          "note": "块间变异；NC 非退化的交互源（与 regime 不同参）"}},
        negative_control_spec=nc_slot,
        private_instance_commitment=commitment or ("0" * 64),
        metric_method_sha=TS._metric_method_sha(),
        resource_spec_sha=("0" * 64),
        conditions=conditions, primary=primary,
        fixed={**TS._fixed_of(scene_id),
               "confirm_blocks": str(P.MAX_CONFIRM_BLOCKS)},
    )
    return spec.validate()


def _sha(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
