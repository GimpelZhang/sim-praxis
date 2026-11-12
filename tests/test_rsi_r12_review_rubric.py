"""RSI-R1.2 T4：双科学审查量表校准（AC44 的审查面；Plan §5 末段）。

证明：缺签/证据不指实际产物/审查者不独立/出现第三票 一律不晋升；
一致全 PASS 才 PASS，分歧记 REVIEW_INCONCLUSIVE。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.rsi_r1 import review_rubric as RR


def _artifact(root: Path, name: str = "report.json") -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"ok": true}\n', encoding="utf-8")
    return target


def _review(reviewer: str, verdicts: "dict[str, str]", root: Path,
            *, relpath: str = "report.json") -> dict:
    return {
        "schema": RR.SCHEMA,
        "schema_version": RR.SCHEMA_VERSION,
        "study_id": "A",
        "reviewer_id": reviewer,
        "items": [
            {"item": item, "verdict": verdicts[item],
             "evidence": [{"path": relpath, "locator": f"{item} §1"}]}
            for item, _ in RR.ITEMS
        ],
    }


def _all(verdict: str) -> "dict[str, str]":
    return {item: verdict for item, _ in RR.ITEMS}


def test_items_are_exactly_five_fixed(tmp_path) -> None:
    assert len(RR.ITEMS) == 5
    assert RR.ITEM_IDS == tuple(item for item, _ in RR.ITEMS)
    assert RR.SCHEMA == "simpraxis.rsi_r1.science_review.v1"


def test_two_independent_all_pass_reviews_pass(tmp_path) -> None:
    _artifact(tmp_path)
    first = _review("reviewer-1", _all("PASS"), tmp_path)
    second = _review("reviewer-2", _all("PASS"), tmp_path)
    assert RR.validate_review(first, root=tmp_path, study_id="A") == []
    RR.assert_review_set([first, second])
    combined = RR.combine_reviews(first, second, root=tmp_path, study_id="A")
    assert combined["verdict"] == RR.REVIEW_PASS
    assert combined["disagreements"] == []
    assert combined["third_vote_allowed"] is False


def test_disagreement_is_inconclusive_not_majority(tmp_path) -> None:
    _artifact(tmp_path)
    first = _review("reviewer-1", _all("PASS"), tmp_path)
    verdicts = _all("PASS")
    verdicts["statistics_and_censoring"] = "FAIL"
    second = _review("reviewer-2", verdicts, tmp_path)
    combined = RR.combine_reviews(first, second, root=tmp_path, study_id="A")
    assert combined["verdict"] == RR.REVIEW_INCONCLUSIVE
    assert combined["disagreements"] == ["statistics_and_censoring"]


def test_agreed_fail_is_fail_not_inconclusive(tmp_path) -> None:
    _artifact(tmp_path)
    verdicts = _all("PASS")
    verdicts["claims_within_evidence"] = "FAIL"
    first = _review("reviewer-1", verdicts, tmp_path)
    second = _review("reviewer-2", verdicts, tmp_path)
    combined = RR.combine_reviews(first, second, root=tmp_path, study_id="A")
    assert combined["verdict"] == RR.REVIEW_FAIL


def test_rubber_stamp_without_evidence_rejected(tmp_path) -> None:
    _artifact(tmp_path)
    doc = _review("reviewer-1", _all("PASS"), tmp_path)
    del doc["items"][0]["evidence"]
    problems = RR.validate_review(doc, root=tmp_path, study_id="A")
    assert any(p.startswith("REVIEW_EVIDENCE_MISSING") for p in problems)
    with pytest.raises(RR.ReviewError):
        RR.combine_reviews(doc, _review("reviewer-2", _all("PASS"), tmp_path),
                           root=tmp_path, study_id="A")


def test_evidence_must_point_to_real_artifact_inside_root(tmp_path) -> None:
    _artifact(tmp_path)
    doc = _review("reviewer-1", _all("PASS"), tmp_path,
                  relpath="missing/report.json")
    problems = RR.validate_review(doc, root=tmp_path, study_id="A")
    assert all(p.startswith("REVIEW_EVIDENCE_ABSENT") for p in
               [p for p in problems if p.startswith("REVIEW_EVIDENCE")])
    outside = _review("reviewer-1", _all("PASS"), tmp_path,
                      relpath="/etc/hostname")
    problems2 = RR.validate_review(outside, root=tmp_path, study_id="A")
    assert any(p.startswith("REVIEW_EVIDENCE_OUTSIDE_ROOT") for p in problems2)


def test_evidence_requires_locator(tmp_path) -> None:
    _artifact(tmp_path)
    doc = _review("reviewer-1", _all("PASS"), tmp_path)
    for item in doc["items"]:
        item["evidence"][0]["locator"] = "  "
    problems = RR.validate_review(doc, root=tmp_path, study_id="A")
    assert len([p for p in problems if p.startswith("REVIEW_LOCATOR_MISSING")]) == 5


def test_same_reviewer_breaks_independence(tmp_path) -> None:
    _artifact(tmp_path)
    first = _review("reviewer-1", _all("PASS"), tmp_path)
    second = _review("reviewer-1", _all("PASS"), tmp_path)
    with pytest.raises(RR.ReviewError) as exc:
        RR.combine_reviews(first, second, root=tmp_path, study_id="A")
    assert exc.value.code == "REVIEW_NOT_INDEPENDENT"


def test_third_vote_rejected(tmp_path) -> None:
    _artifact(tmp_path)
    docs = [_review(f"reviewer-{i}", _all("PASS"), tmp_path) for i in range(3)]
    with pytest.raises(RR.ReviewError) as exc:
        RR.assert_review_set(docs)
    assert exc.value.code == "REVIEW_COUNT_INVALID"
    with pytest.raises(RR.ReviewError) as exc2:
        RR.assert_review_set(docs[:1])
    assert exc2.value.code == "REVIEW_INCOMPLETE"


def test_contaminated_review_rejected(tmp_path) -> None:
    _artifact(tmp_path)
    doc = _review("reviewer-1", _all("PASS"), tmp_path)
    doc["saw_other_review"] = True
    problems = RR.validate_review(doc, root=tmp_path, study_id="A")
    assert "REVIEW_CONTAMINATED:saw_other_review" in problems


def test_wrong_study_and_duplicate_item_rejected(tmp_path) -> None:
    _artifact(tmp_path)
    doc = _review("reviewer-1", _all("PASS"), tmp_path)
    doc["study_id"] = "B"
    problems = RR.validate_review(doc, root=tmp_path, study_id="A")
    assert "REVIEW_STUDY_MISMATCH" in problems
    dup = _review("reviewer-1", _all("PASS"), tmp_path)
    dup["items"].append(dict(dup["items"][0]))
    problems2 = RR.validate_review(dup, root=tmp_path, study_id="A")
    assert any(p.startswith("REVIEW_ITEM_DUPLICATE") for p in problems2)
