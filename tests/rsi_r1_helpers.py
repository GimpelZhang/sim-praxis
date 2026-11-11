"""RSI-R1 测试共用夹具（不是测试文件本身，不含 ``test_`` 函数）。

单一来源：两 study 的合法条件集与计划文档构造器原先内联在
``test_rsi_r1_research_contract.py``；T2 起 ``state``/``loop`` 的测试同样需要
同一份合法计划，故集中到这里，避免第二份常量漂移。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import runtime as RT
from orchestrator.rsi_r1 import scenes as S

ENVELOPE = "r1_test"


def cond(cid: str, scene: str, **params) -> dict:
    return {"id": cid, "scene_id": scene, "params": params}


A_CONDITIONS = (
    cond("base", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
         sliding_friction="0.4", torsional_friction_m="0.01"),
    cond("sham", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
         sliding_friction="0.4", torsional_friction_m="0.02"),
    cond("low_mu", S.SCENE_A, mass_kg="0.5", v0_mps="0.4",
         sliding_friction="0.15", torsional_friction_m="0.01"),
    cond("heavy", S.SCENE_A, mass_kg="0.9", v0_mps="0.4",
         sliding_friction="0.4", torsional_friction_m="0.01"),
)

B_CONDITIONS = (
    cond("base", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
         target_freq_hz="1.0", tc_s="0.008", dt_s="0.001",
         controller="PD", kp="5", kd="0.1"),
    cond("tc_only", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
         target_freq_hz="1.0", tc_s="0.016", dt_s="0.001",
         controller="PD", kp="5", kd="0.1"),
    cond("dt_only", S.SCENE_B, inertia_ratio="1.0", target_amplitude_rad="0.2",
         target_freq_hz="1.0", tc_s="0.008", dt_s="0.0005",
         controller="PD", kp="5", kd="0.1"),
)

SHA = "a" * 64


def plan_doc(study: str, *, phase: str = "confirm", conditions=None,
             primary=None, negative=None, varying=None, block_count: int = 32,
             hypotheses=None, revision: int = 1, parent_sha=None,
             design_source_sha: "str | None" = None,
             analysis_source_sha: "str | None" = None) -> dict:
    conds = copy.deepcopy(list(A_CONDITIONS if study == "A" else B_CONDITIONS)) \
        if conditions is None else conditions
    if primary is None:
        if study == "A":
            primary = {"id": "primary", "kind": "primary",
                       "metric": "displacement_x_m",
                       "proposition": "decrease_gt", "delta": "0.001",
                       "condition_ids": ["base", "low_mu"]}
        else:
            primary = {"id": "primary", "kind": "primary",
                       "metric": "normalized_tracking_rmse",
                       "proposition": "increase_gt", "delta": "0.01",
                       "condition_ids": ["base", "tc_only"]}
    if negative is None and study == "A":
        negative = {"id": "negative", "kind": "negative",
                    "metric": "displacement_x_m_negative_equivalence",
                    "proposition": "equivalent", "delta": "0.001",
                    "condition_ids": ["base", "sham"]}
    if hypotheses is None:
        hypotheses = [
            {"id": "h1", "mechanism": "摩擦耗能主导减速",
             "predicted": "decrease_gt" if study == "A" else "increase_gt",
             "contrast_id": "primary", "falsification": "区间跨过 δ 即反证"},
            {"id": "h2", "mechanism": "扭转摩擦只影响旋转耦合",
             "predicted": "equivalent", "contrast_id":
                 "negative" if study == "A" else "primary",
             "falsification": "位移差区间越出等效界即反证"},
        ]
    if varying is None:
        varying = ({"mass_kg": ["0.2", "1.0"]} if study == "A"
                   else {"inertia_ratio": ["0.5", "2.0"]})
    return {
        "schema": C.PLAN_SCHEMA, "schema_version": 1,
        "study_id": study, "revision": revision, "parent_sha": parent_sha,
        "hypotheses": hypotheses, "conditions": conds,
        "nuisance_distribution": {"generator": "pcg64-host",
                                  "block_count": block_count,
                                  "varying": varying},
        "primary": primary, "negative_control": negative,
        "analysis_source_sha": (analysis_source_sha if analysis_source_sha is not None
                                else (SHA if phase == "confirm" else None)),
        "design_source_sha": (design_source_sha if design_source_sha is not None
                              else (SHA if phase == "confirm" else None)),
        "phase": phase,
    }


def make_ctx(root: "str | Path", envelope: str = ENVELOPE) -> RT.CampaignContext:
    """测试用 campaign 上下文：显式根、无旧根保护（不读 env）。"""
    return RT.CampaignContext(envelope_id=envelope, root=Path(root).resolve(),
                              protected_roots=())


def make_dirs(root: "str | Path") -> Path:
    root = Path(root)
    for name in ("contract", "state", "designs", "research_packets",
                 "observations", "trials", "confirm", "quota"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def doc_of(plan: "dict[str, Any]") -> "C.ResearchPlan":
    return C.ResearchPlan.from_dict(plan)
