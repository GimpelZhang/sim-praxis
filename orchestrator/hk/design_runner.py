"""S2c S2c-2：Q 池开放设计前端（k0c 冻结 → k1d 提议 → G-DESIGN 五折 → 修复 ≤2 → compile）。

纯逻辑 + 注入 actor（触点）与三类宿主回调（NC/试点效应/判别力）。把 DomainBrief
经 System 2 提议出的设计走到 ExperimentSpec；物理回放在此不做（由战役调用方注入）。

设计前端产 status：
* DESIGN_READY      —— 过门并 compile 成功；
* DESIGN_NOT_READY  —— 修复 ≤2 后仍不过门（终点码，计为结果，不重跑，一轮为限）。

世界变体数值只在 GateReport.private（审查者通道），public 回给 actor。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .design_compiler import DesignCompileError, compile_design
from .design_gate import GateReport, check_design
from .design_space import DomainBrief

__all__ = ["run_design", "DesignRunResult"]

#: 触点契约名（仅作 actor 调用的标识；请求键由 touchpoints.REQUIRED_INPUT_KEYS 校验）
_CONTRACT = "domain_brief"


class DesignRunResult(dict):
    """dict 子类：{"status","design","gate_reports","spec","repairs"}。"""

    @property
    def status(self) -> str:
        return self.get("status", "")


def run_design(
    brief: DomainBrief,
    actor: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]],
    *,
    nc_check: "Callable[[Mapping[str, Any]], tuple[bool, str]] | None" = None,
    pilot_effect: "Callable[[Mapping[str, Any]], float] | None" = None,
    discriminability: "Callable[[Mapping[str, Any]], Mapping[str, Any]] | None" = None,
    delta: float = 0.001,
    max_repair: int = 2,
) -> DesignRunResult:
    """跑开放设计前端。

    actor(name, contract, value)：System 2 触点（HeadlessActor 同接口）。
      * name="propose_design"：value={"domain_brief": brief.public_document()}；
      * name="propose_design_repair"：value 另含 previous_design + gate_report(public)。
    """
    reports: "list[dict]" = []
    # k0c：冻结简报（登记公开面身份；brief 已由 campaign 预注册，捕获即可）
    frozen = {"brief_id": brief.brief_id, "scene_id": brief.scene_id,
              "public_sha256_scope": "DomainBrief.public_document"}
    public_doc = brief.public_document()
    design = dict(actor("propose_design", _CONTRACT,
                        {"domain_brief": public_doc, "_k0c_frozen": frozen}))
    gate = check_design(design, brief, nc_check=nc_check, pilot_effect=pilot_effect,
                        discriminability=discriminability, delta=delta)
    reports.append(gate.to_dict())
    repairs = 0
    while not gate.passed and repairs < max_repair:
        repairs += 1
        design = dict(actor("propose_design_repair", _CONTRACT,
                            {"domain_brief": public_doc,
                             "previous_design": design,
                             "gate_report": gate.public,
                             "_k0c_frozen": frozen}))
        gate = check_design(design, brief, nc_check=nc_check, pilot_effect=pilot_effect,
                            discriminability=discriminability, delta=delta)
        reports.append(gate.to_dict())

    result = DesignRunResult(status="DESIGN_NOT_READY", design=design,
                             gate_reports=reports, spec=None, repairs=repairs)
    if not gate.passed:
        return result
    try:
        spec = compile_design(design, brief)
    except DesignCompileError as exc:
        result["status"] = "DESIGN_NOT_READY"
        result["compile_error"] = {"code": exc.code, "detail": exc.detail}
        return result
    result["status"] = "DESIGN_READY"
    result["spec"] = spec
    return result
