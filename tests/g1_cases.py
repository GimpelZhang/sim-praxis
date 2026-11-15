"""RSI-G1 受控交付 case（Plan §5 的 1–6、8；7 在 trace 测试里）。

全部为**合成 fixture**：不发网、不跑物理、不访问隐藏答案。每 case 只改一处。
"""
from __future__ import annotations

import copy
from typing import Any

STUDY = "A"
PLAN_SHA = "1" * 64
RECEIPT_SHA = "2" * 64
EST = 0.040867675677873194
LO = 0.040860710191191175
HI = 0.04087292938493192


def analysis_doc(*, estimate: float = EST, lower: float = LO,
                 upper: float = HI, status: str = "SUPPORTED",
                 study_id: str = STUDY, plan_sha: str = PLAN_SHA,
                 receipt_sha: str = RECEIPT_SHA) -> "dict[str, Any]":
    return {
        "schema": "simpraxis.rsi_r1.analysis_output.v1", "schema_version": 1,
        "study_id": study_id, "plan_sha256": plan_sha,
        "analysis_receipt_sha": receipt_sha,
        "run_ids": ["run-a", "run-b"],
        "contrasts": [{
            "contrast_id": "friction_x_speed",
            "metric_id": "displacement_x_m",
            "proposition": "increase_gt",
            "estimate": estimate, "lower": lower, "upper": upper,
            "status": status, "n_blocks": 32}],
        "diagnostics": {"contrasts": [{"effect": estimate}], "coverage": {}},
    }


def _submission(*, interface: str, ref: "dict[str, Any]",
                values: bool, receipt: str = RECEIPT_SHA,
                study: str = STUDY, plan: str = PLAN_SHA,
                proposition: str = "increase_gt") -> "dict[str, Any]":
    claim: "dict[str, Any]" = {
        "hypothesis_id": "h_interaction", "proposition": proposition,
        "metric_id": "displacement_x_m", "delta": 0.001,
        "result_ref": dict(ref)}
    if values:
        claim.update({"estimate": EST, "lower": LO, "upper": HI})
    return {
        "schema": "simpraxis.rsi_g1.candidate_submission.v1", "schema_version": 1,
        "study_id": study, "plan_sha256": plan, "analysis_receipt_sha": receipt,
        "claims": [claim],
        "research_report": {"schema": "simpraxis.rsi_r13.research_report.v1",
                            "task_id": "r13-a-friction-speed-interaction"},
    }


REF = {"artifact_id": "analysis/output.json", "json_pointer": "/contrasts/0"}


def build_cases() -> "list[dict[str, Any]]":
    """8 个 case：每项只变一个因素。"""
    art = analysis_doc()
    cases: "list[dict[str, Any]]" = []

    # 1) 正确值 + 正确引用（verbatim 与 by_ref 同一结果）
    for iface in ("verbatim", "by_ref"):
        cases.append({"case_id": f"c01_{iface}", "interface": iface,
                      "artifacts": {"analysis/output.json": art},
                      "submission": _submission(interface=iface, ref=REF,
                                                values=(iface == "verbatim")),
                      "expect": {"ok": True}})

    # 2) 正确引用但报告四舍五入 → 数值保真失败
    sub = _submission(interface="verbatim", ref=REF, values=True)
    sub["claims"][0].update({"estimate": 0.0407, "lower": 0.0401, "upper": 0.0413})
    cases.append({"case_id": "c02_rounded", "interface": "verbatim",
                  "artifacts": {"analysis/output.json": art}, "submission": sub,
                  "expect": {"ok": False, "code": "CLAIM_NUMERIC_MISMATCH"}})

    # 3) 缺 receipt → CLAIM_INCOMPLETE
    sub = _submission(interface="by_ref", ref=REF, values=False, receipt="")
    cases.append({"case_id": "c03_no_receipt", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": art}, "submission": sub,
                  "expect": {"ok": False, "code": "CLAIM_INCOMPLETE"}})

    # 4) 固定 pilot：结构可过（范围由候选显式声明，语义归审查）
    cases.append({"case_id": "c04_fixed_point", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": art},
                  "submission": _submission(interface="by_ref", ref=REF,
                                            values=False),
                  "compile_options": {"population_scope": "fixed_point"},
                  "expect": {"ok": True}})

    # 5) 合法 REFUTED：不因标签非 SUPPORTED 被拒
    refuted = analysis_doc(status="REFUTED", estimate=-0.2, lower=-0.3, upper=-0.1)
    sub = _submission(interface="by_ref", ref=REF, values=False,
                      proposition="decrease_gt")
    cases.append({"case_id": "c05_refuted", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": refuted},
                  "submission": sub, "expect": {"ok": True}})

    # 6) 候选分析本身错（与隐藏 oracle 不一致）：编译器保真复制、不代填
    wrong = analysis_doc(estimate=-0.0042, lower=-0.0151, upper=0.0063)
    sub = _submission(interface="by_ref", ref=REF, values=False)
    cases.append({"case_id": "c06_wrong_value_faithful", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": wrong},
                  "submission": sub, "expect": {"ok": True},
                  "assert_claim_estimate": -0.0042})

    # 7) 跨 study 引用（artifact 声明 study B，提交声明 A）→ 绑定失败
    other = analysis_doc(study_id="B")
    cases.append({"case_id": "c07_cross_study", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": other},
                  "submission": _submission(interface="by_ref", ref=REF,
                                            values=False),
                  "expect": {"ok": False, "code": "CLAIM_BINDING_MISMATCH"}})

    # 8) 引用越权路径（试图读私有/裁判面）→ 拒绝
    bad_ref = {"artifact_id": "private/analysis/A-confirm.raw.json",
               "json_pointer": "/contrasts/0"}
    cases.append({"case_id": "c08_forbidden_store", "interface": "by_ref",
                  "artifacts": {"analysis/output.json": art},
                  "submission": _submission(interface="by_ref", ref=bad_ref,
                                            values=False),
                  "expect": {"ok": False, "code": "ARTIFACT_FORBIDDEN"}})
    return copy.deepcopy(cases)
