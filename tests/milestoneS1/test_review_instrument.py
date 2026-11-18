"""S1-7 双审仪器：包自足负控 / 六维解析 / 真值表 / 冻结防回改 / 校准 6 例答案表。"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_bridge import review_s1 as RV

PACK = RV.build_review_pack(
    study="A_N", question="摩擦是否影响位移？", methods="2×2 32 配对 block",
    results=[{"id": "friction_x_mass", "estimate": 0.0, "ci_low": -0.002,
              "ci_high": 0.002, "n_blocks": 32}],
    claim={"verdict": "INCONCLUSIVE", "statement": "交互不显著"},
    negative_control="sham 90% 落在 ±0.001 m")


def _review_text(verdict="accept"):
    return json.dumps({"dimension_scores": {d: 3 for d in RV.DIMENSIONS},
                       "verdict": verdict, "reason": "ok"})


def test_pack_self_contained_negative_controls():
    with pytest.raises(RV.ReviewError) as excinfo:
        RV.assert_pack_self_contained({**PACK, "other_review": "reviewer B says accept"})
    assert excinfo.value.code == "PACK_NOT_SELF_CONTAINED"
    with pytest.raises(RV.ReviewError) as excinfo:
        RV.assert_pack_self_contained(
            {**PACK, "claim": {"statement": "brought " + RV.SELF_CONTAINED_SENTINEL}})
    assert excinfo.value.code == "PACK_NOT_SELF_CONTAINED"
    with pytest.raises(RV.ReviewError) as excinfo:
        RV.assert_pack_self_contained({"question": "x"})
    assert excinfo.value.code == "PACK_INCOMPLETE"


def test_parse_review_requires_all_dimensions():
    doc = RV.parse_review(_review_text())
    assert doc["verdict"] == "accept"
    assert set(doc["dimension_scores"]) == set(RV.DIMENSIONS)
    bad = json.loads(_review_text())
    del bad["dimension_scores"]["negative_control"]
    with pytest.raises(RV.ReviewError) as excinfo:
        RV.parse_review(json.dumps(bad))
    assert excinfo.value.code == "REVIEW_SCHEMA"
    with pytest.raises(RV.ReviewError):
        RV.parse_review("no json here")


def test_truth_table_merge_has_no_third_vote():
    a = RV.parse_review(_review_text("accept"))
    b = RV.parse_review(_review_text("accept"))
    merged = RV.merge_truth_table(a, b)
    assert merged["verdict"] == "PASS" and merged["third_vote"] is None
    merged = RV.merge_truth_table(a, RV.parse_review(_review_text("reject")))
    assert merged["verdict"] == "FAIL" and merged["rule"] == "any_reject"


def test_frozen_report_tamper_detected():
    frozen = RV.freeze_report({"x": 1})
    assert RV.verify_frozen_report(frozen)
    frozen["x"] = 2
    with pytest.raises(RV.ReviewError) as excinfo:
        RV.verify_frozen_report(frozen)
    assert excinfo.value.code == "REPORT_TAMPERED"


def test_calibration_cases_six_with_mixed_answers():
    cases = RV.calibration_cases()
    assert len(cases) == 6
    answers = RV.calibration_expectations()
    assert list(answers.values()).count("accept") == 2
    assert list(answers.values()).count("reject") == 4
    for case in cases:
        assert case["summary"].strip()
    # 伪重复/过度概括/空模板/矛盾 四类必须各自在场（R1.3 轮次纪律的判别面）
    kinds = [c["id"] for c in cases]
    assert {"cal-pseudo-replication", "cal-over-generalization",
            "cal-empty-template", "cal-contradictory"} <= set(kinds)


def test_run_review_injects_pack_and_returns_run():
    seen = {}

    def call(prompt):
        seen["prompt"] = prompt
        return _review_text("reject"), (10, 5)

    run = RV.run_review(call, pack=PACK, channel="channel_a")
    assert run.parsed["verdict"] == "reject"
    assert run.pack_sha256 == PACK["pack_sha256"]
    assert PACK["question"] in seen["prompt"]
    assert "machine_verdict" not in seen["prompt"]
