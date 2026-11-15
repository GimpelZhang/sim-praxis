"""R1.3 科学审查量表（六维）与双审合并（无第三票）。

与 R1.2 的五项量表**并行存在**（旧结果不改判）：R1.3 把它拆成两件更容易
机判、也更贴合 §16 复盘教训的事：

1. 探索声明一致性（``exploration_declaration``）与确认控制
   （``confirmation_controls``）分开——"确认控制有效"不能豁免"探索期没执行
   声明"；
2. 主张范围（``claim_scope``）与结果驱动局限/下一步
   （``result_driven_limitations_next_steps``）分开——整体平均 SUPPORTED 不推出
   每个条件 SUPPORTED。

合并语义（Plan §5）：

* 恰好两名/两通道独立审查同一冻结包；两份全部 PASS 才 ``REVIEW_PASS``；
* 任一明确 FAIL 记录为失败原因；分歧/UNVERIFIABLE 仍 ``REVIEW_INCONCLUSIVE``；
* **不追第三票**；主科学结论的 INCONCLUSIVE 与审查合并的 INCONCLUSIVE 是
  两个字段，不互推。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import research_report as RR

REVIEW_SCHEMA = f"{P.SCHEMA_PREFIX_V13}.review.v1"
VERDICT_SCHEMA = P.REVIEW_VERDICT_SCHEMA_V13

ITEMS_V13: "tuple[str, ...]" = (
    "problem_and_hypothesis",
    "exploration_declaration",
    "confirmation_controls",
    "statistics_and_censoring",
    "claim_scope",
    "result_driven_limitations_next_steps",
)
VERDICTS = ("PASS", "FAIL", "UNVERIFIABLE")


class ReviewError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


@dataclass(frozen=True)
class CombinedReview:
    verdict: str            # REVIEW_PASS｜REVIEW_FAIL｜REVIEW_INCONCLUSIVE
    items: "dict[str, str]"
    reasons: "tuple[str, ...]"
    reviewers: "tuple[str, ...]"
    science_inconclusive: bool
    codes: "tuple[str, ...]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": VERDICT_SCHEMA,
            "schema_version": 1,
            "verdict": self.verdict,
            "items": dict(self.items),
            "reasons": list(self.reasons),
            "reviewers": list(self.reviewers),
            "science_inconclusive": self.science_inconclusive,
            "codes": list(self.codes),
            "third_vote": False,
        }


def validate_review_v13(doc: Any, *, evidence: Mapping[str, bytes],
                        expected_packet_sha256: str) -> "dict[str, Any]":
    """结构 + 引用解析；语义 PASS/FAIL 由审查者判断，机器不代判。"""
    if not isinstance(doc, Mapping):
        raise ReviewError("WRONG_TYPE", "review 必须是对象")
    if set(doc) != {"schema", "review_id", "reviewer_id", "packet_sha256",
                    "items"}:
        raise ReviewError("KEY_MISMATCH", f"未知/缺键：{sorted(set(doc))}")
    if doc["schema"] != REVIEW_SCHEMA:
        raise ReviewError("SCHEMA_MISMATCH", str(doc["schema"]))
    if not str(doc["review_id"]).strip():
        raise ReviewError("REVIEW_ID", "review_id 为空")
    if not str(doc["reviewer_id"]).strip():
        raise ReviewError("REVIEWER_ID", "reviewer_id 为空")
    if doc["packet_sha256"] != expected_packet_sha256:
        raise ReviewError("PACKET_MISMATCH", "review.packet_sha256 不符")
    items = doc["items"]
    if not isinstance(items, Mapping) or set(items) != set(ITEMS_V13):
        raise ReviewError("ITEMS_MISMATCH", "六维必须逐项给出")
    out: "dict[str, Any]" = {}
    for name in ITEMS_V13:
        row = items[name]
        if not isinstance(row, Mapping) or set(row) != {"verdict", "reason",
                                                        "refs"}:
            raise ReviewError("ITEMS_MISMATCH", f"{name} 字段集")
        if row["verdict"] not in VERDICTS:
            raise ReviewError("VERDICT_INVALID", f"{name}={row['verdict']!r}")
        if not str(row["reason"]).strip():
            raise ReviewError("REASON_EMPTY", name)
        refs = row["refs"]
        if not isinstance(refs, (list, tuple)) or not refs:
            raise ReviewError("REFS_EMPTY", name)
        for i, locator in enumerate(refs):
            try:
                # 审查者的引用只需解析到包内位置；包字节由收据绑定。模型常把
                # packet_sha256 字段当文件 sha 抄进来，这里不让它误伤解析。
                clean = {k: v for k, v in locator.items() if k != "sha256"}
                RR.resolve_locator(clean, evidence=evidence,
                                   default_file="packet.json"
                                   if "packet.json" in evidence else None)
            except RR.ReportError as exc:
                raise ReviewError(exc.code, f"{name}.refs[{i}]: {exc.detail}") from None
        out[name] = {"verdict": row["verdict"],
                     "reason": str(row["reason"]),
                     "refs": list(refs)}
    return {"review_id": str(doc["review_id"]),
            "reviewer_id": str(doc["reviewer_id"]),
            "packet_sha256": doc["packet_sha256"], "items": out}


def combine_reviews(reviews: Sequence[Any], *,
                    receipts: Sequence[Mapping[str, Any]],
                    expected_packet_sha256: str,
                    evidence: Mapping[str, bytes],
                    rubric: "Sequence[str]" = ITEMS_V13,
                    science_inconclusive: bool = False) -> CombinedReview:
    """两份全部 PASS 才 REVIEW_PASS；任一分歧/UNVERIFIABLE → INCONCLUSIVE。"""
    if len(reviews) != 2:
        raise ReviewError("REVIEW_COUNT", f"必须恰好两审，实得 {len(reviews)}")
    if len(receipts) != 2:
        raise ReviewError("RECEIPT_COUNT", "必须恰好两份审查收据")
    parsed = [validate_review_v13(r, evidence=evidence,
                                  expected_packet_sha256=expected_packet_sha256)
              for r in reviews]
    ids = {p["review_id"] for p in parsed}
    who = {p["reviewer_id"] for p in parsed}
    if len(ids) != 2 or len(who) != 2:
        raise ReviewError("REVIEW_NOT_INDEPENDENT",
                          "review_id/reviewer_id 必须两两不同（复制旧件不算独立）")
    # 收据必须绑定同一 packet、同一 reviewer、双向不可见另一审
    for receipt, parsed_row in zip(receipts, parsed):
        _verify_receipt(receipt, parsed_row, expected_packet_sha256)
    items: "dict[str, str]" = {}
    reasons: "list[str]" = []
    failed = False
    uncertain = False
    for name in rubric:
        verdicts = [p["items"][name]["verdict"] for p in parsed]
        if all(v == "PASS" for v in verdicts):
            items[name] = "PASS"
            continue
        if any(v == "FAIL" for v in verdicts):
            items[name] = "FAIL"
            failed = True
            reasons.append(f"{name}: " + "；".join(
                f"{p['reviewer_id']}={p['items'][name]['reason']}"
                for p in parsed if p["items"][name]["verdict"] == "FAIL"))
            continue
        items[name] = "UNVERIFIABLE"
        uncertain = True
        reasons.append(f"{name}: " + "；".join(
            f"{p['reviewer_id']}={p['items'][name]['verdict']}:"
            f"{p['items'][name]['reason']}" for p in parsed))
    if failed:
        verdict = "REVIEW_FAIL"
        codes = ("REVIEW_FAIL",)
    elif uncertain:
        verdict = "REVIEW_INCONCLUSIVE"
        codes = ("REVIEW_INCONCLUSIVE",)
    else:
        verdict = "REVIEW_PASS"
        codes = ("REVIEW_PASS",)
    return CombinedReview(verdict=verdict, items=items, reasons=tuple(reasons),
                          reviewers=tuple(sorted(who)),
                          science_inconclusive=bool(science_inconclusive),
                          codes=codes)


def _verify_receipt(receipt: Mapping[str, Any], parsed: Mapping[str, Any],
                    packet_sha: str) -> None:
    from orchestrator.rsi_r1 import review_receipt as RRc

    RRc.verify_receipt(receipt, review_id=parsed["review_id"],
                       reviewer_id=parsed["reviewer_id"],
                       packet_sha256=packet_sha)


def public_rubric() -> "dict[str, Any]":
    return {
        "schema": VERDICT_SCHEMA,
        "items": list(ITEMS_V13),
        "verdicts": list(VERDICTS),
        "combine": {
            "count": 2,
            "third_vote": False,
            "pass_rule": "两份全部 PASS",
            "fail_rule": "任一明确 FAIL 记录原因",
            "disagreement": "UNVERIFIABLE/分歧 → REVIEW_INCONCLUSIVE",
            "science_vs_review": "主科学结论 INCONCLUSIVE 与审查合并 INCONCLUSIVE 分字段",
        },
    }
