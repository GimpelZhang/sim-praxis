"""RSI-G2.1 独立脚本消费者（maintainer_scripted_consumer；零网络、零仓库 import）。

协议（Plan §3.1/§3.2）：从 stdin 读**实际最终 Request 的公开投影**
``{"system", "messages", "max_tokens"}``，向 stdout 写**单条**动作 JSON。

它只消费 request 里本来就公开的东西：

1. 结果视图（envelope）来自独立消息（``### 活动结果视图`` / ``### 结果视图``）；
2. 任务规格来自 system 里的公开任务块（task_id/条件/主对比/δ/阴性控制）；
3. 候选产物只经 ``read_result_artifact`` 分页回包获得——不读宿主任何路径，
   不从宿主闭包取值；
4. 报告 locator 的 sha256 全部取自 envelope 的 artifact 台账。

动作序（RESULT_RELEASED 之后唯一合法面）：read 全部分页 → 校验整件 sha →
submit_claim（claims 逐字取 envelope 主对比；report_text 与 research_report
按公开 schema 构造，render 规则与项目确定性 renderer 逐字同构）。

本脚本不计候选模型成功：桥侧以 ``maintainer_scripted_consumer`` 标注。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

CONTEXT_HEADERS = (
    "### 活动结果视图（result_context；g2_atomic_v1，不可裁剪）\n",
    "### 结果视图（g2_atomic_v1 最小交付单元，不可裁剪）\n",
)
REPORT_SCHEMA = "simpraxis.rsi_r13.research_report.v1"


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_envelope(messages):
    for message in reversed(messages):
        content = message.get("content") or ""
        for header in CONTEXT_HEADERS:
            index = content.find(header)
            if index < 0:
                continue
            try:
                doc = json.loads(content[index + len(header):])
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict) and "envelope_sha256" in doc:
                return doc
    return None


def read_pages(messages):
    pages = []
    for message in messages:
        content = message.get("content") or ""
        try:
            doc = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if (isinstance(doc, dict) and doc.get("ok") is True
                and "artifact_sha256" in doc and "next_cursor" in doc
                and "offset" in doc):
            pages.append(doc)
    return pages


def task_id_of(system):
    match = re.search(r"任务规格（冻结）：(\S+)", system or "")
    if match:
        return match.group(1)
    match = re.search(r'"task_id":\s*"([^"]+)"', system or "")
    return match.group(1) if match else ""


def hypotheses_of(messages):
    for message in reversed(messages):
        content = message.get("content") or ""
        if "### 当前可见状态" not in content:
            continue
        body = content.split("\n", 1)[1] if "\n" in content else ""
        fragment = body.split("\n\n请给出你的下一个动作")[0]
        try:
            doc = json.loads(fragment)
        except json.JSONDecodeError:
            continue
        plan = doc.get("plan") or {}
        return [str(item.get("id")) for item in plan.get("hypotheses") or []
                if isinstance(item, dict) and item.get("id")]
    return []


def render_report(doc):
    """与 ``orchestrator.rsi_r1.research_report.render_report`` 逐字同构。"""
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


def _artifact(envelope, artifact_id):
    for item in envelope.get("artifacts") or []:
        if item.get("artifact_id") == artifact_id:
            return item
    return None


def _primary_index(output, contrast_id):
    raw = output.get("contrasts")
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, dict) and str(item.get("contrast_id")) == contrast_id:
                return index
    return 0


def build_submit(envelope, system, messages):
    artifact = _artifact(envelope, "analysis/output.json")
    receipt = _artifact(envelope, "analysis/receipt.json")
    output = None
    pages = read_pages(messages)
    data = "".join(page.get("data") or "" for page in
                   sorted(pages, key=lambda p: int(p.get("offset") or 0)))
    try:
        output = json.loads(data)
    except json.JSONDecodeError:
        return None
    primary_id = str(envelope.get("primary_contrast_id")
                     or (envelope.get("primary") or {}).get("contrast_id")
                     or "")
    record = next((item for item in envelope.get("contrasts") or []
                   if str(item.get("contrast_id")) == primary_id), None)
    if record is None:
        return None
    task_id = task_id_of(system)
    scope = envelope.get("scope") or {}
    locator = {
        "kind": "json_pointer", "file": "analysis/output.json",
        "sha256": str(artifact.get("sha256") if artifact else ""),
        "pointer": f"/contrasts/{_primary_index(output, primary_id)}/estimate",
    }
    diag_locator = dict(locator, pointer="/diagnostics")
    report = {
        "schema": REPORT_SCHEMA, "schema_version": 1, "task_id": task_id,
        "question_and_hypotheses": (
            f"在 study {envelope.get('study_id')} 的冻结主对比 {primary_id} 上，"
            f"检验 {record.get('proposition')}（δ={record.get('delta')}）。"),
        "methods_and_assignment_responsibility": (
            "条件与 nuisance 分配逐字沿用宿主冻结任务；block 由宿主分配，"
            f"聚合={scope.get('aggregation')}，权重={scope.get('weighting')}，"
            f"覆盖={scope.get('coverage')}，目标总体={scope.get('target_population')}；"
            "研究者只声明 estimand、对比权重与判定命题。"),
        "primary_results": [{
            "contrast_id": primary_id,
            "analysis_receipt_sha": str(receipt.get("sha256") if receipt else ""),
            "result_locator": locator}],
        "heterogeneity_and_scope": [{
            "claim": (f"点估计 {record.get('estimate')}，"
                      f"区间 [{record.get('lower')}, {record.get('upper')}]，"
                      f"覆盖 {record.get('n_blocks')} 个配对 block。"),
            "evidence_refs": [diag_locator],
            "applicability": ("仅限冻结任务登记的块总体与条件水平；"
                              "不跨 metric 或跨 study 外推。")}],
        "explore_confirm_comparison": {
            "estimands": (f"两阶段同为 {scope.get('aggregation')} / "
                          f"{scope.get('weighting')} 的配对 block 对比，"
                          f"metric={record.get('metric_id')}。"),
            "comparable": True,
            "explanation": ("探索与确认共用同一 estimand 定义与 block 单位；"
                            "确认批为宿主 32 block 抽样。"),
            "evidence_refs": [diag_locator]},
        "alternative_explanations": [
            "数值阻尼/求解器迭代差异在临界滑停附近产生非物理伪交互。",
            "接触点转动耦合在高速侧更强，贡献部分对照差异。"],
        "limitations": [{
            "statement": "只覆盖冻结任务登记的条件对与窗口。",
            "evidence_refs": [dict(locator, pointer="/contrasts/0/estimate")],
            "consequence": "外推到其他参数区间时效应量级不可直接沿用。"}],
        "next_steps": [{
            "unresolved_question": "主对比之外是否存在未登记的交互。",
            "discriminating_experiment": "在冻结参数域内增加速度水平并重跑确认批。",
            "possible_outcomes": ["交互随速度单调变化", "交互在各速度水平持平"]}],
    }
    claim_proposition = str(record.get("proposition") or "")
    claim_metric = str(record.get("metric_id") or "")
    delta = float(record.get("delta"))
    claims = []
    for hid in hypotheses_of(messages) or ["h_interaction"]:
        claims.append({"hypothesis_id": hid, "proposition": claim_proposition,
                       "metric_id": claim_metric, "delta": delta,
                       "estimate": float(record.get("estimate")),
                       "lower": float(record.get("lower")),
                       "upper": float(record.get("upper"))})
    return {"action": "submit_claim", "claims": claims,
            "report_text": render_report(report), "research_report": report}


def decide(payload):
    system = str(payload.get("system") or "")
    messages = payload.get("messages") or []
    envelope = find_envelope(messages)
    if envelope is None:
        print(json.dumps({"error": "NO_ENVELOPE"}))
        return 2
    artifact = _artifact(envelope, "analysis/output.json")
    if artifact is None:
        print(json.dumps({"error": "NO_ARTIFACT"}))
        return 2
    pages = read_pages(messages)
    if not pages:
        action = {"action": "read_result_artifact",
                  "artifact_id": "analysis/output.json",
                  "output_sha256": str(artifact.get("sha256")),
                  "cursor": 0, "page_bytes": 4096}
        print(json.dumps(action, ensure_ascii=False, sort_keys=True))
        return 0
    last = max(pages, key=lambda p: int(p.get("offset") or 0))
    if last.get("next_cursor") is not None:
        action = {"action": "read_result_artifact",
                  "artifact_id": "analysis/output.json",
                  "output_sha256": str(artifact.get("sha256")),
                  "cursor": int(last["next_cursor"]), "page_bytes": 4096}
        print(json.dumps(action, ensure_ascii=False, sort_keys=True))
        return 0
    data = "".join(page.get("data") or "" for page in
                   sorted(pages, key=lambda p: int(p.get("offset") or 0)))
    if sha_bytes(data.encode("utf-8")) != str(artifact.get("sha256")):
        print(json.dumps({"error": "ARTIFACT_SHA_MISMATCH"}))
        return 2
    submit = build_submit(envelope, system, messages)
    if submit is None:
        print(json.dumps({"error": "SUBMIT_BUILD_FAILED"}))
        return 2
    print(json.dumps(submit, ensure_ascii=False, sort_keys=True))
    return 0


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print(json.dumps({"error": "BAD_INPUT"}))
        return 2
    return decide(payload)


if __name__ == "__main__":
    sys.exit(main())
