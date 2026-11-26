"""S2c S2c-2：design_compiler——过门设计 → ExperimentSpec（复用 rsi_r1 合同）。

过门后编译：conditions（从 design 因子矩阵）、主对比 DiD、NC 对比、plan_sha256。
随后走既有 K0 登记 → K2…K8（不在本模块执行物理）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .design_space import DomainBrief

__all__ = ["DesignCompileError", "compile_design"]


class DesignCompileError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _sha(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def compile_design(design: Mapping[str, Any], brief: DomainBrief) -> dict:
    """把过门设计编译成 ExperimentSpec（dict）。要求 design 已含：
    conditions（每项 {id, factors, scene_params}）、primary_contrast
    （{id, treatment_high, treatment_low, control_high, control_low, proposition}）、
    negative_control（{base, sham}）、metric。
    """
    conds = design.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise DesignCompileError("NO_CONDITIONS", "design.conditions 为空")
    pc = design.get("primary_contrast")
    if not isinstance(pc, Mapping):
        raise DesignCompileError("NO_PRIMARY", "缺 primary_contrast")
    for key in ("id", "treatment_high", "treatment_low", "control_high", "control_low"):
        if not pc.get(key):
            raise DesignCompileError("PRIMARY_INCOMPLETE", f"primary 缺 {key}")
    nc = design.get("negative_control")
    if not isinstance(nc, Mapping) or not nc.get("base") or not nc.get("sham"):
        raise DesignCompileError("NO_NC", "缺 negative_control base/sham")
    metric = design.get("metric")
    if metric is None:
        raise DesignCompileError("NO_METRIC", "缺 metric")

    spec = {
        "schema": "simpraxis.hk.study_design.v1",
        "brief_id": brief.brief_id,
        "scene_id": brief.scene_id,
        "metric": str(metric),
        "conditions": [{"id": str(c["id"]),
                        "factors": dict(c.get("factors") or {}),
                        "scene_params": dict(c.get("scene_params") or {})}
                       for c in conds],
        "primary_contrast": {"id": str(pc["id"]), "kind": "difference_in_differences",
                             "proposition": str(pc.get("proposition", "decrease_gt")),
                             "terms": {k: str(pc[k]) for k in
                                       ("treatment_high", "treatment_low",
                                        "control_high", "control_low")}},
        "negative_control": {"base": str(nc["base"]), "sham": str(nc["sham"])},
        "hypothesis": str(design.get("hypothesis", "")),
        "rationale": str(design.get("rationale", "")),
    }
    spec["plan_sha256"] = _sha({k: v for k, v in spec.items() if k != "plan_sha256"})
    return spec
