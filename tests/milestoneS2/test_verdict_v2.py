"""S2 词表 v2：五值分类逐例（AC-02）+ 翻转负控 + 边界。"""
from __future__ import annotations

import pytest

from orchestrator.hk import verdicts as V


def test_vocabulary_is_five_values():
    assert V.VERDICTS_V2 == ("SUPPORTED", "REFUTED", "NOT_DETECTED",
                              "EQUIVALENT", "INCONCLUSIVE")


@pytest.mark.parametrize("proposition,lower,upper,expected", [
    # decrease_gt，δ=0.01：CI 整段低于 -δ → SUPPORTED
    ("decrease_gt", "-0.05", "-0.02", "SUPPORTED"),
    # CI 整段高于 +δ → REFUTED（证据否证：方向相反且越过 δ）
    ("decrease_gt", "0.02", "0.05", "REFUTED"),
    # CI 整段在带内（也跨零）→ EQUIVALENT（等效检验优先于跨零）
    ("decrease_gt", "-0.004", "0.003", "EQUIVALENT"),
    # CI 跨零但不整段在带内 → NOT_DETECTED（S1 叙事错配的修复点）
    ("decrease_gt", "-0.02", "0.003", "NOT_DETECTED"),
    # CI 整段落 ±δ 带 → EQUIVALENT
    ("decrease_gt", "-0.009", "0.009", "EQUIVALENT"),
    # 区间横跨 -δ 边界但不跨零 → INCONCLUSIVE
    ("decrease_gt", "-0.02", "-0.005", "INCONCLUSIVE"),
    # increase_gt 对称
    ("increase_gt", "0.02", "0.05", "SUPPORTED"),
    ("increase_gt", "-0.05", "-0.02", "REFUTED"),
    ("increase_gt", "-0.02", "0.004", "NOT_DETECTED"),
    ("increase_gt", "-0.009", "0.009", "EQUIVALENT"),
    # equivalent 主张：三值语义
    ("equivalent", "-0.009", "0.009", "SUPPORTED"),
    ("equivalent", "0.02", "0.05", "REFUTED"),
    ("equivalent", "-0.02", "0.005", "INCONCLUSIVE"),
])
def test_classify_v2_cases(proposition, lower, upper, expected):
    assert V.classify_v2(proposition, "0.01", lower, upper) == expected


def test_degenerate_point_interval_never_claims_evidence():
    """退化点区间（如 sham [0,0]）不得冒充精确证据——v2 判 INCONCLUSIVE。"""
    assert V.classify_v2("decrease_gt", "0.01", "0", "0") == "INCONCLUSIVE"
    assert V.classify_v2("equivalent", "0.01", "0", "0") == "INCONCLUSIVE"


def test_sample_size_guard():
    assert V.classify_v2("decrease_gt", "0.01", "-0.05", "-0.02",
                         n_blocks=1) == "INCONCLUSIVE"
    assert V.classify_v2("decrease_gt", "0.01", "-0.05", "-0.02",
                         n_blocks=2) == "SUPPORTED"


def test_invalid_inputs_fail_closed():
    with pytest.raises(V.VerdictError) as excinfo:
        V.classify_v2("no_such_proposition", "0.01", "-0.1", "0.1")
    assert excinfo.value.code == "PROPOSITION_INVALID"
    with pytest.raises(V.VerdictError) as excinfo:
        V.classify_v2("decrease_gt", "0", "-0.1", "0.1")
    assert excinfo.value.code == "DELTA_INVALID"
    with pytest.raises(V.VerdictError) as excinfo:
        V.classify_v2("decrease_gt", "0.01", "0.2", "0.1")
    assert excinfo.value.code == "INTERVAL_INVERTED"


def test_flip_negative_control():
    """负控：把 S1 的跨零结果翻成 REFUTED 必须与 v2 分类不同（HIGH-2 语义）。"""
    host = V.classify_v2("decrease_gt", "0.01", "-3.72e-6", "3.09e-6")
    # S1 的 A_N 主对比：95% CI 整段落进 ±0.001 带内 → v2 判 EQUIVALENT
    # （旧三值会判 REFUTED——「跨零/带内」都不是证据否证）。
    assert host == "EQUIVALENT"
    assert host != "REFUTED"
