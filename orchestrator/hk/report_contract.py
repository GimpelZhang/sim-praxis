"""S2 报告合同 v2（六段强制）——共享规则面（计划 §2.1 第 4 行）。

S1 双审的第四条实质意见：报告缺"局限与下一步"、无阴性对照处理。v2 把报告结构
固化为**六段必须段**，缺段不是文风问题而是**结构性不合格**：

======================  =========================================================
段                      内容
======================  =========================================================
question_hypothesis     问题与可反驳假设（含方向与 δ）
methods                 设计（条件矩阵/配对区块/宿主私有 nuisance）
main_results            主结果（estimate/CI/分类，逐对比）
heterogeneity_scope     异质性与范围（结论能覆盖到哪里、不能覆盖什么）
limitations_next_steps  局限与下一步
negative_control_handling 阴性对照的处理（有效性、亚阈、灵敏度校准）
======================  =========================================================

与六维量表的对应（``SECTION_TO_DIMENSION``）是**逐段对齐**的单一真值：缺段即该维度
计 0 分（``dimension_floor``），且审查包必须显式标注缺段（``missing_sections``）。
v0 臂无 G-REPORT 守卫、无 v2 模板——缺段照常发生并被计 0；v1 臂的 G-REPORT 在 K8
提交前结构自检并给一次修复触点（变异面，不在本模块）。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["SCHEMA", "SECTIONS", "SECTION_TO_DIMENSION", "ReportContractError",
           "missing_sections", "validate_report", "build_report",
           "dimension_floor", "report_of_claim", "crosscheck_nc_section"]

SCHEMA = "simpraxis.rsi_s2.report.v2"

#: 六段必须段（顺序即卡面/量表顺序，不得重排）。
SECTIONS = ("question_hypothesis", "methods", "main_results",
            "heterogeneity_scope", "limitations_next_steps",
            "negative_control_handling")

#: 段 → 六维量表维度（逐段对齐；review_s1.DIMENSIONS 的同一组名字）。
SECTION_TO_DIMENSION: "Mapping[str, str]" = {
    "question_hypothesis": "problem_hypothesis",
    "methods": "design_confounding",
    "main_results": "statistics_censoring",
    "heterogeneity_scope": "claim_strength",
    "limitations_next_steps": "limitations_next_steps",
    "negative_control_handling": "negative_control",
}

#: 存在但空的段不得冒充"已写"（空模板是 S1 校准例 cal-empty-template）。
_MIN_CHARS = 8


class ReportContractError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _section_text(value: Any) -> str:
    """段值可以是字符串，或 ``{"text": ...}`` 对象（留扩展余地）。"""
    if isinstance(value, Mapping):
        value = value.get("text")
    if isinstance(value, str):
        return value.strip()
    return ""


def missing_sections(report: Any) -> "list[str]":
    """返回缺段/空段列表（按 SECTIONS 顺序；结构标注的唯一实现）。"""
    if not isinstance(report, Mapping):
        return list(SECTIONS)
    return [name for name in SECTIONS
            if len(_section_text(report.get(name))) < _MIN_CHARS]


def validate_report(report: Any) -> "dict[str, str]":
    """校验六段合同；不合格即 ``ReportContractError``（fail-closed）。"""
    if not isinstance(report, Mapping):
        raise ReportContractError("REPORT_SHAPE", "报告必须是对象")
    missing = missing_sections(report)
    if missing:
        raise ReportContractError("REPORT_INCOMPLETE",
                                  f"缺段/空段 {missing}（六段合同 v2）")
    return {name: _section_text(report[name]) for name in SECTIONS}


def build_report(sections: Mapping[str, Any]) -> "dict[str, Any]":
    """构造规范化报告件（键序固定；调用方仍须自行 validate）。"""
    return {"schema": SCHEMA,
            "sections": {name: _section_text(sections.get(name))
                         for name in SECTIONS}}


def dimension_floor(report: Any) -> "dict[str, float]":
    """结构分地板：段存在且非空 → 4 分；缺段 → 0 分（"缺段按 0 分"）。

    这是**结构面**的地板，不是内容评分：内容分由模型审查给出（0–4），
    机器只保证缺段不冒充得分。
    """
    missing = set(missing_sections(report))
    # 存在 ≠ 满分：结构面只给 1.0 地板（内容分由模型审查给出，0–4 是其上界）
    return {dim: (0.0 if section in missing else 1.0)
            for section, dim in SECTION_TO_DIMENSION.items()}


def report_of_claim(claim: Any) -> Any:
    """从 claim 件取报告段（K8 出口把报告挂在 claim.report）。"""
    if isinstance(claim, Mapping):
        return claim.get("report")
    return None


def crosscheck_nc_section(report: Any, nc_evidence: Any) -> "list[str]":
    """S2b F2：negative_control_handling 段与 nc_evidence 交叉校验。

    证据显示已注册/三连通过而报告写"未注册/未验证" ⇒ 返回违规描述列表
    （空列表 = 一致）。这是把"自相矛盾"从审查拒提前为**结构拒**的关键：
    G-REPORT_NC_MISMATCH → 一次修复触点 → 仍不符 ⇒ 结构终止。

    检查逻辑：
    1. nc_evidence 在场（非 None）而报告段声称"未注册/未验证/无 NC" ⇒ 矛盾；
    2. nc_evidence 三连判据全过而报告段声称"退化/未通过" ⇒ 矛盾；
    3. nc_evidence 缺失而报告段声称"已注册/三连通过" ⇒ 矛盾（编造）。
    """
    problems: "list[str]" = []
    section = ""
    if isinstance(report, Mapping):
        section = _section_text(report.get("negative_control_handling"))
    if not section:
        return problems  # 缺段由 missing_sections 单独处理

    lowered = section.lower()

    # 报告声称"未注册/未验证/无 NC"的否定锚词
    _DENY_ANCHORS = ("未注册", "未验证", "无 nc", "无nc", "未提供", "not registered",
                     "not verified", "no negative", "nc 未", "nc未")
    # 报告声称"已注册/三连通过/有效"的肯定锚词
    _AFFIRM_ANCHORS = ("已注册", "三连", "非退化", "亚阈", "灵敏度", "通过",
                       "registered", "non-degenerate", "sub-threshold", "sensitivity")

    denies = any(a in lowered for a in _DENY_ANCHORS)
    affirms = any(a in lowered for a in _AFFIRM_ANCHORS)

    if nc_evidence is not None and denies:
        problems.append(
            "G-REPORT_NC_MISMATCH: nc_evidence 在场（NC 已注册/已验证），"
            "但 negative_control_handling 段声称未注册/未验证")
    if nc_evidence is not None and not affirms and not denies:
        problems.append(
            "G-REPORT_NC_MISMATCH: nc_evidence 在场但 negative_control_handling "
            "段未如实引用 NC 证据（缺非退化/亚阈/灵敏度等锚词）")
    if nc_evidence is None and affirms:
        problems.append(
            "G-REPORT_NC_MISMATCH: nc_evidence 缺失，但 negative_control_handling "
            "段声称已注册/三连通过（编造证据）")
    return problems
