"""RSI-G1 决策门（G4）：多轴报告 + 旁路结案聚合（零 POST）。

Plan §9：**不输出互斥单标签**，五个轴各自成字段：

* ``history_observability = COMPLETE | PARTIAL | UNOBSERVABLE``
* ``delivery_integrity    = CONFIRMED | DIVERGED | UNKNOWN``
* ``packet_integrity      = PASS | FAIL | UNKNOWN``
* ``compiler_fixture_status = PASS | FAIL``
* ``model_comparison      = NOT_RUN | <B 层判据>``

并附 Plan G1-16 的失败分类矩阵（CANDIDATE / DELIVERY / INSTRUMENT /
UNOBSERVABLE + 证据字段）。旁路结案（G1-10/G1-11）对 study 做 **AND** 聚合，
且把 claim tuple / 科学方向 / 审查判决分成三个字段——不复用旧 closeout 的
「逐 study 覆盖」写法，也不改旧结案件。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "simpraxis.rsi_g1.decision.v1"
SIDECAR_SCHEMA = "simpraxis.rsi_g1.closeout_bypass.v1"

HISTORY_COMPLETE = "COMPLETE"
HISTORY_PARTIAL = "PARTIAL"
HISTORY_UNOBSERVABLE = "UNOBSERVABLE"
DELIVERY_CONFIRMED = "CONFIRMED"
DELIVERY_DIVERGED = "DIVERGED"
DELIVERY_UNKNOWN = "UNKNOWN"
PACKET_PASS = "PASS"
PACKET_FAIL = "FAIL"
PACKET_UNKNOWN = "UNKNOWN"
MODEL_NOT_RUN = "NOT_RUN"


def _trace_axes(traces: Sequence[Mapping[str, Any]]) -> "tuple[str, str, list[str]]":
    """从 trace 摘要推 observability / delivery 两轴（缺省 fail-closed）。"""
    problems: "list[str]" = []
    if not traces:
        return HISTORY_UNOBSERVABLE, DELIVERY_UNKNOWN, ["无 trace"]
    incomplete = 0
    suspected: "list[str]" = []
    for t in traces:
        gaps = list(t.get("gaps") or [])
        if gaps:
            incomplete += 1
            problems.extend(gaps)
        if t.get("suspected"):
            suspected.append(str(t["suspected"]))
    history = HISTORY_COMPLETE if incomplete == 0 else HISTORY_PARTIAL
    # 归因等级：只有 OBSERVED 相邻跳才能给 CONFIRMED/DIVERGED；
    # 本计划的轮 O 历史请求未落盘 ⇒ 一律 UNKNOWN（Plan §5.1）。
    levels = {str(t.get("attribution_level") or "") for t in traces}
    if levels == {"OBSERVED"} and not suspected:
        delivery = DELIVERY_CONFIRMED
    elif levels != {"OBSERVED"} and suspected:
        delivery = DELIVERY_UNKNOWN
    elif levels == {"OBSERVED"} and suspected:
        delivery = DELIVERY_DIVERGED
    else:
        delivery = DELIVERY_UNKNOWN
    return history, delivery, sorted(set(problems))


def _packet_axis(audits: Sequence[Mapping[str, Any]]) -> "tuple[str, list[str]]":
    if not audits:
        return PACKET_UNKNOWN, ["无包审计"]
    failing = [a for a in audits if not a.get("ok")]
    if failing:
        codes: "list[str]" = []
        for a in failing:
            codes.extend(str(c) for c in (a.get("codes") or []))
        return PACKET_FAIL, sorted(set(codes))
    return PACKET_PASS, []


def _compiler_axis(cases: Sequence[Mapping[str, Any]]) -> "tuple[str, list[str]]":
    if not cases:
        return "FAIL", ["无 case"]
    bad = [c for c in cases if not c.get("ok")]
    return ("PASS" if not bad else "FAIL"), [str(c.get("case_id")) for c in bad]


def classification_matrix(*, history: str, delivery: str, packet: str,
                          suspected: Sequence[str]) -> "list[dict[str, Any]]":
    """Plan G1-16：按证据给出可能根因与**证据字段**（可并存，不互斥）。"""
    rows: "list[dict[str, Any]]" = []
    rows.append({
        "class": "UNOBSERVABLE",
        "applies": history != HISTORY_COMPLETE,
        "evidence": "请求字节未归档 / 视图仅 RECONSTRUCTED（Plan §5.1）",
    })
    rows.append({
        "class": "INSTRUMENT",
        "applies": packet == PACKET_FAIL,
        "evidence": "审查包语义守恒审计 FAIL（D10–D12 负控可复现）",
    })
    rows.append({
        "class": "DELIVERY",
        "applies": delivery == DELIVERY_DIVERGED,
        "evidence": "相邻 OBSERVED 跳出现数值变化",
    })
    rows.append({
        "class": "DELIVERY_SUSPECTED",
        "applies": delivery == DELIVERY_UNKNOWN and bool(suspected),
        "evidence": f"重建视图显示可疑跳 {sorted(set(suspected))}（不得据此归因候选）",
    })
    rows.append({
        "class": "CANDIDATE",
        "applies": False if delivery != DELIVERY_CONFIRMED else True,
        "evidence": "仅当全链 OBSERVED 且模型可见正确数值仍提交错误时成立",
    })
    return rows


def aggregate_diagnostics(*, diagnostic_id: str,
                          traces: Sequence[Mapping[str, Any]],
                          packet_audits: Sequence[Mapping[str, Any]],
                          compiler_cases: Sequence[Mapping[str, Any]],
                          probe: "Mapping[str, Any] | None" = None,
                          notes: Sequence[str] = ()) -> "dict[str, Any]":
    history, delivery, gaps = _trace_axes(traces)
    packet, packet_codes = _packet_axis(packet_audits)
    compiler, failed_cases = _compiler_axis(compiler_cases)
    suspected = [str(t["suspected"]) for t in traces if t.get("suspected")]
    axes = {
        "history_observability": history,
        "delivery_integrity": delivery,
        "packet_integrity": packet,
        "compiler_fixture_status": compiler,
        "model_comparison": ((probe or {}).get("overall") or MODEL_NOT_RUN),
    }
    doc = {
        "schema": SCHEMA, "schema_version": 1,
        "diagnostic_id": diagnostic_id,
        "axes": axes,
        "evidence": {
            "trace_gaps": gaps,
            "suspected_hops": sorted(set(suspected)),
            "packet_codes": packet_codes,
            "failed_cases": failed_cases,
            "probe": dict(probe) if probe else None,
        },
        "classification": classification_matrix(
            history=history, delivery=delivery, packet=packet,
            suspected=suspected),
        "notes": list(notes),
        "rule": ("A 层不输出模型能力优劣；model_comparison 只有 B 层才有值。"
                 "衍生/重建字节只用于发现潜在变化，不升级历史证据等级。"),
    }
    return doc


# ---------------------------------------------------------------------------
# 旁路结案（AND 聚合；三字段分离）
# ---------------------------------------------------------------------------

def build_sidecar_closeout(studies: Sequence[Mapping[str, Any]]) -> "dict[str, Any]":
    """输入每 study 的 {study, claim_status, scientific_direction, review_verdict}。

    与旧 ``closeout_r13`` 的区别（Plan §4 表 / G1-10/G1-11）：
    * 逐 study 收集后 **AND** 聚合，不用后写覆盖先写；
    * ``claim_tuple`` / ``scientific_direction`` / ``review_verdict`` 三字段分开；
    * 科学 REFUTED/INCONCLUSIVE 不因标签名被判 invalid（由调用方传方向，不重算）。
    """
    rows: "list[dict[str, Any]]" = []
    for item in studies:
        row = {"study": str(item.get("study")),
               "claim_status": str(item.get("claim_status") or "UNKNOWN"),
               "scientific_direction": str(item.get("scientific_direction") or "UNKNOWN"),
               "review_verdict": str(item.get("review_verdict") or "UNKNOWN")}
        row["claim_match"] = row["claim_status"].upper() == "MATCH"
        row["review_pass"] = row["review_verdict"].upper() == "REVIEW_PASS"
        rows.append(row)
    claim_tuple = [r["claim_status"] for r in rows]
    direction = [r["scientific_direction"] for r in rows]
    review = [r["review_verdict"] for r in rows]
    all_match = bool(rows) and all(r["claim_match"] for r in rows)
    all_pass = bool(rows) and all(r["review_pass"] for r in rows)
    doc = {
        "schema": SIDECAR_SCHEMA, "schema_version": 1,
        "studies": rows, "n_studies": len(rows),
        "claim_tuple": claim_tuple,
        "scientific_direction": direction,
        "review_verdict": review,
        "and_aggregation": {
            "claims_all_match": all_match,
            "reviews_all_pass": all_pass,
            "combined": bool(all_match and all_pass),
        },
        "rule": ("旁路件只做聚合与分字段，不重判旧 study；不改旧 closeout、"
                 "不触发重判（G1-09/G1-19）"),
    }
    return doc
