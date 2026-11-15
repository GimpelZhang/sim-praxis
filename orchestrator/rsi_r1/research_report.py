"""R1.3 研究报告契约：结构/来源/引用解析（**不代替科学语义审查**）。

Plan §4：``ResearchReportV13`` 必须由候选生成并绑定 reply/action/source，
不能由宿主根据判器填表；结构校验只判格式/引用/数字来源，科学语义保留双审。

定位符用 typed locator，不用自由文本"见文件"：

* ``json_pointer``：``{"file","sha256","pointer":"/values/displacement_x_m"}``
* ``byte_range``：``{"file","sha256","byte_range":[a,b]}``
* ``line_range``：``{"file","sha256","line_range":[a,b]}``

``file`` 必须是证据根内的**相对路径**（禁绝对路径/``..``/符号链接穿越）；
``sha256`` 必须与证据字节逐字节相符；指针/区间必须解析到实际内容。

渲染函数只排版候选内容，不补结论/局限；若候选同时提交 ``report_text``，
必须与确定性 render 逐字相等，否则 ``REPORT_REPRESENTATION_MISMATCH``
（不允许两份文字任选有利的一份）。
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import task_specs as TS

REPORT_SCHEMA = P.RESEARCH_REPORT_SCHEMA_V13

TOP_KEYS = frozenset({
    "schema", "schema_version", "task_id", "question_and_hypotheses",
    "methods_and_assignment_responsibility", "primary_results",
    "heterogeneity_and_scope", "explore_confirm_comparison",
    "alternative_explanations", "limitations", "next_steps",
})


class ReportError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


@dataclass(frozen=True)
class ReportValidation:
    ok: bool
    codes: "tuple[str, ...]"
    resolved_refs: int
    checks: "tuple[dict[str, Any], ...]"

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": f"{P.SCHEMA_PREFIX_V13}.report_validation.v1",
                "schema_version": 1, "ok": self.ok, "codes": list(self.codes),
                "resolved_refs": self.resolved_refs,
                "checks": [dict(c) for c in self.checks]}


def _safe_relpath(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ReportError("LOCATOR_INVALID", "file 必须是非空字符串")
    if name.startswith("/") or name.startswith("~") or "\\" in name:
        raise ReportError("LOCATOR_UNSAFE", f"绝对/反斜杠路径不允许：{name!r}")
    norm = posixpath.normpath(name)
    if norm.startswith("..") or norm == "." or "/../" in f"/{norm}/":
        raise ReportError("LOCATOR_UNSAFE", f"路径穿越：{name!r}")
    return norm


def _resolve_pointer(body: Any, pointer: str) -> bool:
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    node = body
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                return False
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return True


def resolve_locator(locator: Any, *, evidence: Mapping[str, bytes],
                    default_file: "str | None" = None) -> str:
    """解析一个 typed locator；返回规范化文件路径（失败抛 ReportError）。

    ``default_file`` 只用于审查包（review 者常写 ``type`` 与相对 pointer，
    省略文件名）；候选报告仍必须显式给 ``kind``/``file``。
    """
    if not isinstance(locator, Mapping):
        raise ReportError("LOCATOR_INVALID", "locator 必须是对象")
    kind = locator.get("kind") or locator.get("type")
    file = locator.get("file") or default_file
    if not file:
        raise ReportError("LOCATOR_INVALID", "locator 缺 file")
    path = _safe_relpath(file)
    if path not in evidence:
        raise ReportError("LOCATOR_UNRESOLVED", f"证据根无文件 {path}")
    data = evidence[path]
    digest = hashlib.sha256(data).hexdigest()
    # 候选报告必须给 sha256（逐字节绑定）；审查包内的引用可只给 pointer
    # （包字节本身由 ReviewRunReceipt.packet_sha256 绑定，解析仍指向实际内容）。
    if locator.get("sha256") is not None and locator.get("sha256") != digest:
        raise ReportError("LOCATOR_SHA_MISMATCH", f"{path} sha256 不符")
    if kind == "json_pointer":
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReportError("LOCATOR_UNRESOLVED", f"{path} 不是 JSON") from None
        pointer = locator.get("pointer")
        if not isinstance(pointer, str) or not _resolve_pointer(body, pointer):
            raise ReportError("LOCATOR_UNRESOLVED", f"{path}#{pointer!r}")
        return path
    if kind == "byte_range":
        rng = locator.get("byte_range")
        if not (isinstance(rng, list) and len(rng) == 2
                and all(isinstance(v, int) for v in rng)):
            raise ReportError("LOCATOR_INVALID", "byte_range 必须是 [a,b]")
        a, b = rng
        if not (0 <= a <= b <= len(data)):
            raise ReportError("LOCATOR_UNRESOLVED", f"byte_range {rng} 越界")
        return path
    if kind == "line_range":
        rng = locator.get("line_range")
        if not (isinstance(rng, list) and len(rng) == 2
                and all(isinstance(v, int) for v in rng)):
            raise ReportError("LOCATOR_INVALID", "line_range 必须是 [a,b]")
        a, b = rng
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not (1 <= a <= b <= len(lines)):
            raise ReportError("LOCATOR_UNRESOLVED", f"line_range {rng} 越界")
        return path
    raise ReportError("LOCATOR_INVALID", f"未知 kind={kind!r}")


def _text(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError("FIELD_EMPTY", where)
    return value


def _refs(value: Any, *, where: str, evidence: Mapping[str, bytes]) -> int:
    if not isinstance(value, (list, tuple)) or not value:
        raise ReportError("EVIDENCE_MISSING", f"{where} 至少一条引用")
    resolved = 0
    for i, locator in enumerate(value):
        try:
            resolve_locator(locator, evidence=evidence)
        except ReportError as exc:
            raise ReportError(exc.code, f"{where}[{i}]: {exc.detail}") from None
        resolved += 1
    return resolved


def validate_report(doc: Any, *, task: TS.TaskSpecV13,
                    receipts: Mapping[str, Mapping[str, Any]],
                    evidence: Mapping[str, bytes],
                    candidate_origin: Mapping[str, Any],
                    expected_reply_sha256: str,
                    report_text: "str | None" = None) -> ReportValidation:
    """结构/来源/引用校验；任何一条不满足即拒（科学语义另由双审判断）。"""
    if not isinstance(doc, Mapping):
        raise ReportError("WRONG_TYPE", "report 必须是对象")
    unknown = sorted(set(doc) - TOP_KEYS)
    missing = sorted(TOP_KEYS - set(doc))
    if unknown or missing:
        raise ReportError("KEY_MISMATCH", f"未知 {unknown} 缺 {missing}")
    if doc["schema"] != REPORT_SCHEMA:
        raise ReportError("SCHEMA_MISMATCH", f"schema={doc['schema']!r}")
    if doc["task_id"] != task.task_id:
        raise ReportError("TASK_MISMATCH", str(doc["task_id"]))
    # 来源：必须由候选 reply 产生，宿主/维护改稿即 assisted
    if not isinstance(candidate_origin, Mapping):
        raise ReportError("REPORT_NOT_CANDIDATE_ORIGIN", "缺来源件")
    if candidate_origin.get("producer") != "candidate":
        raise ReportError("REPORT_NOT_CANDIDATE_ORIGIN",
                          f"producer={candidate_origin.get('producer')!r}")
    if candidate_origin.get("reply_sha256") != expected_reply_sha256:
        raise ReportError("REPORT_NOT_CANDIDATE_ORIGIN", "reply sha 不符")
    if candidate_origin.get("assisted") is True:
        raise ReportError("REPORT_ASSISTED", "人工代写必须标 assisted，不能计自主成功")

    checks: "list[dict[str, Any]]" = []
    refs = 0

    _text(doc["question_and_hypotheses"], where="question_and_hypotheses")
    _text(doc["methods_and_assignment_responsibility"],
          where="methods_and_assignment_responsibility")

    primary = doc["primary_results"]
    if not isinstance(primary, (list, tuple)) or not primary:
        raise ReportError("PRIMARY_RESULTS_EMPTY", "primary_results 为空")
    for i, row in enumerate(primary):
        if not isinstance(row, Mapping) or set(row) != {
                "contrast_id", "analysis_receipt_sha", "result_locator"}:
            raise ReportError("PRIMARY_RESULTS_INVALID", f"primary_results[{i}]")
        if row["contrast_id"] != task.primary.id:
            raise ReportError("PRIMARY_RESULTS_INVALID",
                              f"contrast_id={row['contrast_id']!r}")
        sha = row["analysis_receipt_sha"]
        if sha not in receipts:
            raise ReportError("RECEIPT_MISSING", f"receipt {sha!r} 未封存")
        refs += 1
        try:
            resolve_locator(row["result_locator"], evidence=evidence)
        except ReportError as exc:
            raise ReportError(exc.code, f"primary_results[{i}]: {exc.detail}") from None
        checks.append({"primary": row["contrast_id"], "receipt": sha, "ok": True})

    hetero = doc["heterogeneity_and_scope"]
    if not isinstance(hetero, (list, tuple)) or not hetero:
        raise ReportError("HETEROGENEITY_MISSING",
                          "必须报告适用范围（无异性也须明示覆盖）")
    for i, row in enumerate(hetero):
        if not isinstance(row, Mapping) or set(row) != {
                "claim", "evidence_refs", "applicability"}:
            raise ReportError("HETEROGENEITY_INVALID", f"heterogeneity[{i}]")
        _text(row["claim"], where=f"heterogeneity[{i}].claim")
        _text(row["applicability"], where=f"heterogeneity[{i}].applicability")
        refs += _refs(row["evidence_refs"],
                      where=f"heterogeneity[{i}].evidence_refs", evidence=evidence)

    comp = doc["explore_confirm_comparison"]
    if not isinstance(comp, Mapping) or set(comp) != {
            "estimands", "comparable", "explanation", "evidence_refs"}:
        raise ReportError("COMPARISON_INVALID", "explore_confirm_comparison")
    if comp["comparable"] not in (True, False):
        raise ReportError("COMPARISON_INVALID", "comparable 必须是布尔")
    _text(comp["explanation"], where="explore_confirm_comparison.explanation")
    refs += _refs(comp["evidence_refs"],
                  where="explore_confirm_comparison.evidence_refs", evidence=evidence)

    alternatives = doc["alternative_explanations"]
    if not isinstance(alternatives, (list, tuple)) or not alternatives:
        raise ReportError("ALTERNATIVES_MISSING", "至少一条替代解释")
    for i, text in enumerate(alternatives):
        _text(text, where=f"alternative_explanations[{i}]")

    limits = doc["limitations"]
    if not isinstance(limits, (list, tuple)) or not limits:
        raise ReportError("LIMITATIONS_MISSING", "limitations 为空")
    for i, row in enumerate(limits):
        if not isinstance(row, Mapping) or set(row) != {
                "statement", "evidence_refs", "consequence"}:
            raise ReportError("LIMITATIONS_INVALID", f"limitations[{i}]")
        _text(row["statement"], where=f"limitations[{i}].statement")
        _text(row["consequence"], where=f"limitations[{i}].consequence")
        refs += _refs(row["evidence_refs"],
                      where=f"limitations[{i}].evidence_refs", evidence=evidence)

    steps = doc["next_steps"]
    if not isinstance(steps, (list, tuple)) or not steps:
        raise ReportError("NEXT_STEPS_MISSING", "next_steps 为空")
    for i, row in enumerate(steps):
        if not isinstance(row, Mapping) or set(row) != {
                "unresolved_question", "discriminating_experiment",
                "possible_outcomes"}:
            raise ReportError("NEXT_STEPS_INVALID", f"next_steps[{i}]")
        for key in ("unresolved_question", "discriminating_experiment"):
            _text(row[key], where=f"next_steps[{i}].{key}")
        outcomes = row["possible_outcomes"]
        if not isinstance(outcomes, (list, tuple)) or len(outcomes) < 2:
            raise ReportError("NEXT_STEPS_INVALID",
                              f"next_steps[{i}].possible_outcomes 至少两种")
        for j, outcome in enumerate(outcomes):
            _text(outcome, where=f"next_steps[{i}].possible_outcomes[{j}]")

    if report_text is not None and report_text != render_report(doc):
        raise ReportError(
            "REPORT_REPRESENTATION_MISMATCH",
            "report_text 与确定性 render 不逐字相等（不允许两份文字任选）")
    return ReportValidation(ok=True, codes=("REPORT_STRUCTURE_OK",),
                            resolved_refs=refs, checks=tuple(checks))


def render_report(doc: Mapping[str, Any]) -> str:
    """只排版候选内容；不补结论/局限（确定性 render，逐字节可复现）。"""
    lines = [f"# Research Report — {doc.get('task_id', '')}", ""]
    lines += ["## Question and hypotheses",
              str(doc.get("question_and_hypotheses", "")), ""]
    lines += ["## Methods and assignment responsibility",
              str(doc.get("methods_and_assignment_responsibility", "")), ""]
    lines += ["## Primary results"]
    for row in doc.get("primary_results") or []:
        loc = row.get("result_locator") or {}
        lines.append(f"- {row.get('contrast_id')} (receipt {row.get('analysis_receipt_sha')}) "
                     f"@ {loc.get('file')}#{loc.get('pointer') or loc.get('byte_range') or loc.get('line_range')}")
    lines.append("")
    lines += ["## Heterogeneity and scope"]
    for row in doc.get("heterogeneity_and_scope") or []:
        lines.append(f"- {row.get('claim')}（适用：{row.get('applicability')}）")
    lines.append("")
    comp = doc.get("explore_confirm_comparison") or {}
    lines += ["## Explore/confirm comparison",
              f"- estimands: {comp.get('estimands')}",
              f"- comparable: {comp.get('comparable')}",
              f"- explanation: {comp.get('explanation')}", ""]
    lines += ["## Alternative explanations"]
    for text in doc.get("alternative_explanations") or []:
        lines.append(f"- {text}")
    lines.append("")
    lines += ["## Limitations"]
    for row in doc.get("limitations") or []:
        lines.append(f"- {row.get('statement')}（后果：{row.get('consequence')}）")
    lines.append("")
    lines += ["## Next steps"]
    for row in doc.get("next_steps") or []:
        lines.append(f"- {row.get('unresolved_question')} → "
                     f"{row.get('discriminating_experiment')} → "
                     f"{'/'.join(str(x) for x in row.get('possible_outcomes') or [])}")
    return "\n".join(lines) + "\n"


def public_schema() -> "dict[str, Any]":
    return {
        "schema": REPORT_SCHEMA,
        "keys": sorted(TOP_KEYS),
        "locator": {
            "kinds": ["json_pointer", "byte_range", "line_range"],
            "fields": ["kind", "file", "sha256", "pointer|byte_range|line_range"],
            "rules": "file 必须相对证据根；sha256 逐字节；禁止自由文本'见文件'",
        },
        "rules": {
            "origin": "report 必须由候选 reply 产生；宿主/维护改稿标 assisted",
            "primary_results": "每条必须引用已封存 receipt 与可解析 locator",
            "limitations": "每条必须有 statement/evidence_refs/consequence",
            "render": "report_text 若提供，必须与确定性 render 逐字相等",
        },
    }
