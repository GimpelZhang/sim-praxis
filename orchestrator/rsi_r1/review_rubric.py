"""RSI-R1.2 T4：双科学审查的五条固定量表（Plan §5 末段、AC44 的审查面）。

规则（逐字来自预注册计划）：

* 五项量表固定，不可增删/改名；
* 每项必须给 PASS/FAIL **且引用实际产物位置**——只勾选总 PASS 不算；
* 两名审查者互相独立（不同 actor、互不看对方意见/汇总成功标签）；
* 意见不一致记 ``REVIEW_INCONCLUSIVE``，**不追加第三票**强过；
* 本模块只做结构校验与合并，不生成/代替审查意见，也不调用模型。

任何一项 FAIL 或缺少有效签名都会阻止研究结论晋升为“R1 闭环”。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "simpraxis.rsi_r1.science_review.v1"
SCHEMA_VERSION = 1

REVIEW_PASS = "REVIEW_PASS"
REVIEW_FAIL = "REVIEW_FAIL"
REVIEW_INCONCLUSIVE = "REVIEW_INCONCLUSIVE"

VERDICTS = ("PASS", "FAIL")

#: 五项固定量表（id → 人类可读要求）。顺序即渲染/报告顺序，不得增删。
ITEMS: "tuple[tuple[str, str], ...]" = (
    ("problem_and_hypothesis",
     "问题/可反驳假设明确：研究问题与可证伪假设逐字可对照"),
    ("design_controls",
     "单因素/配对与混淆控制有效：block 配对、条件隔离、阴性/灵敏度控制到位"),
    ("statistics_and_censoring",
     "统计与删失处理正确：配对 bootstrap、删失规则与区间水平符合冻结方法"),
    ("claims_within_evidence",
     "结论力度不超证据：主张分类与区间一致，不外推、不把 INCONCLUSIVE 当阳性"),
    ("limitations_and_next_steps",
     "局限及下一步有据：局限引用实际结果，下一步不靠换 seed/改界翻案"),
)
ITEM_IDS: "tuple[str, ...]" = tuple(item for item, _ in ITEMS)

#: 审查件里禁止出现的键——防“看过对方意见/汇总成功标签”的污染。
FORBIDDEN_KEYS = ("other_review", "other_reviewer", "other_reviewer_id",
                  "aggregate_success", "summary_success", "saw_other_review")


class ReviewError(RuntimeError):
    """审查件不合规（结构/证据引用/独立性）；带机器码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _resolved_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        base = root.resolve(strict=False)
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def validate_review(doc: Any, *, root: "Path | str",
                    study_id: "str | None" = None) -> "list[str]":
    """校验单份审查件；返回问题码列表（空 = 合法）。

    证据引用必须是 ``root`` 内**真实存在**的文件路径（相对路径或根内绝对
    路径），并可附带 locator（行号/符号/字段），杜绝只勾选总 PASS。
    """
    problems: "list[str]" = []
    if not isinstance(doc, Mapping):
        return ["REVIEW_NOT_OBJECT"]
    if doc.get("schema") != SCHEMA:
        problems.append("REVIEW_SCHEMA_MISMATCH")
    reviewer = str(doc.get("reviewer_id") or "")
    if not reviewer:
        problems.append("REVIEWER_MISSING")
    if study_id is not None and str(doc.get("study_id") or "") != str(study_id):
        problems.append("REVIEW_STUDY_MISMATCH")
    for key in FORBIDDEN_KEYS:
        if key in doc:
            problems.append(f"REVIEW_CONTAMINATED:{key}")
    items = doc.get("items")
    if not isinstance(items, list):
        return problems + ["REVIEW_ITEMS_MISSING"]
    seen: "dict[str, Mapping[str, Any]]" = {}
    for entry in items:
        if not isinstance(entry, Mapping):
            problems.append("REVIEW_ITEM_NOT_OBJECT")
            continue
        item_id = str(entry.get("item") or "")
        if item_id not in ITEM_IDS:
            problems.append(f"REVIEW_ITEM_UNKNOWN:{item_id}")
            continue
        if item_id in seen:
            problems.append(f"REVIEW_ITEM_DUPLICATE:{item_id}")
            continue
        seen[item_id] = entry
    missing = [i for i in ITEM_IDS if i not in seen]
    if missing:
        problems.append(f"REVIEW_ITEM_MISSING:{','.join(missing)}")
    base = Path(root)
    for item_id, entry in sorted(seen.items()):
        verdict = str(entry.get("verdict") or "")
        if verdict not in VERDICTS:
            problems.append(f"REVIEW_VERDICT_INVALID:{item_id}")
        refs = entry.get("evidence")
        if not isinstance(refs, list) or not refs:
            problems.append(f"REVIEW_EVIDENCE_MISSING:{item_id}")
            continue
        for ref in refs:
            if not isinstance(ref, Mapping):
                problems.append(f"REVIEW_EVIDENCE_NOT_OBJECT:{item_id}")
                continue
            rel = str(ref.get("path") or "")
            if not rel:
                problems.append(f"REVIEW_EVIDENCE_PATH_MISSING:{item_id}")
                continue
            target = Path(rel) if Path(rel).is_absolute() else base / rel
            if not _resolved_within(target, base):
                problems.append(f"REVIEW_EVIDENCE_OUTSIDE_ROOT:{item_id}:{rel}")
                continue
            if not target.is_file():
                problems.append(f"REVIEW_EVIDENCE_ABSENT:{item_id}:{rel}")
                continue
            if not str(ref.get("locator") or "").strip():
                problems.append(f"REVIEW_LOCATOR_MISSING:{item_id}:{rel}")
    return problems


def combine_reviews(first: Mapping[str, Any], second: Mapping[str, Any], *,
                    root: "Path | str", study_id: "str | None" = None
                    ) -> "dict[str, Any]":
    """合并两份**独立**审查：一致且全 PASS 才 PASS；分歧记 INCONCLUSIVE。

    只接受恰好两份；不提供第三票入口（传第三份即 ``REVIEW_COUNT_INVALID``）。
    """
    p1 = validate_review(first, root=root, study_id=study_id)
    p2 = validate_review(second, root=root, study_id=study_id)
    if p1 or p2:
        raise ReviewError("REVIEW_INVALID", "; ".join(p1 + p2)[:400])
    if str(first.get("reviewer_id")) == str(second.get("reviewer_id")):
        raise ReviewError("REVIEW_NOT_INDEPENDENT",
                          "两名审查者必须是不同 actor")
    by1 = {str(e["item"]): e for e in first["items"]}
    by2 = {str(e["item"]): e for e in second["items"]}
    disagreements = [i for i in ITEM_IDS
                     if str(by1[i]["verdict"]) != str(by2[i]["verdict"])]
    if disagreements:
        verdict = REVIEW_INCONCLUSIVE
    elif all(str(by1[i]["verdict"]) == "PASS" for i in ITEM_IDS):
        verdict = REVIEW_PASS
    else:
        verdict = REVIEW_FAIL
    return {
        "schema": f"{SCHEMA}.combined",
        "schema_version": SCHEMA_VERSION,
        "study_id": (None if study_id is None else str(study_id)),
        "reviewers": [str(first.get("reviewer_id")),
                      str(second.get("reviewer_id"))],
        "items": {i: {"first": str(by1[i]["verdict"]),
                      "second": str(by2[i]["verdict"])} for i in ITEM_IDS},
        "disagreements": disagreements,
        "verdict": verdict,
        "third_vote_allowed": False,
        "note": "分歧记 REVIEW_INCONCLUSIVE；不追加第三票强过",
    }


def assert_review_set(docs: Sequence[Mapping[str, Any]]) -> None:
    """审查集合必须恰两份；任何额外“第三票”一律拒绝。"""
    if len(docs) == 1:
        raise ReviewError("REVIEW_INCOMPLETE", "只有一份审查，缺第二份独立签名")
    if len(docs) != 2:
        raise ReviewError("REVIEW_COUNT_INVALID",
                          f"只允许两名独立审查，收到 {len(docs)} 份")
    ids = {str(d.get("reviewer_id") or "") for d in docs}
    if len(ids) != 2:
        raise ReviewError("REVIEW_NOT_INDEPENDENT",
                          f"reviewer_id 必须互异：{sorted(ids)}")


__all__ = [
    "FORBIDDEN_KEYS", "ITEMS", "ITEM_IDS", "REVIEW_FAIL", "REVIEW_INCONCLUSIVE",
    "REVIEW_PASS", "ReviewError", "SCHEMA", "SCHEMA_VERSION", "VERDICTS",
    "assert_review_set", "combine_reviews", "validate_review",
]
