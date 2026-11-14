"""RSI-R1.2 T0（AC01）：版本化 profile 一致性、解析规则与公开合同单一真值。

覆盖：

* 两条 profile 的 id/schema/namespace/帽值逐项；
* ``resolve_profile``：无声明 → legacy；声明 R1.2 → 新 profile；未声明 R1.2 根、
  id 未知、声明与请求冲突 → 拒（绝不静默回退）；
* 公开合同 v2 由 profile 派生，缺段/改数值即拒；
* R1.2 资源数值自洽（124 = 12+16+48+48；族 403；aux 五信封 2,000,000）。
"""

from __future__ import annotations

import pytest

from orchestrator.rsi_r1 import profiles as P


def test_profile_ids_are_frozen_pair():
    assert P.PROFILES == ("rsi-r1-legacy", "rsi-r1.2")
    assert P.profile_by_id("rsi-r1-legacy") is P.PROFILE_LEGACY_DEF
    assert P.profile_by_id("rsi-r1.2") is P.PROFILE_R12_DEF


def test_legacy_profile_matches_lanes_single_source():
    from orchestrator.rsi_r1 import lanes

    legacy = P.PROFILE_LEGACY_DEF
    assert legacy.lane_order == lanes.LANE_ORDER
    assert dict(legacy.lane_post_caps) == dict(lanes.LANE_POST_CAPS)
    assert legacy.hard_cap == lanes.RSI_R1_HARD_CAP == 144
    assert legacy.normal_budget == lanes.RSI_R1_NORMAL_BUDGET == 120
    assert legacy.namespace == 2903
    assert legacy.consent_scope == "R1"
    assert legacy.total_posts() == 144


def test_r12_profile_numbers():
    r12 = P.PROFILE_R12_DEF
    assert r12.profile_id == "rsi-r1.2"
    assert r12.lane_order == ("probe", "practice", "study_A", "study_B")
    assert dict(r12.lane_post_caps) == {
        "probe": 12, "practice": 16, "study_A": 48, "study_B": 48}
    assert r12.total_posts() == 124 == r12.hard_cap == r12.normal_budget
    assert r12.namespace == 2904
    assert r12.consent_scope == "R1.2"
    assert r12.judge_version == "r1.2"
    assert r12.family_cap == 403
    assert r12.family_history_posts == 31
    assert "repair_reserve" not in r12.lane_post_caps
    assert r12.budget_schema == P.BUDGET_SCHEMA_V2
    assert r12.consent_schema == "simpraxis.rsi_r12.consent.v1"
    assert r12.data_root_name == "rsi_r12"


def test_r12_table_consistency_selfcheck():
    P.assert_r12_table_consistent()
    assert P.task_caps_sum_r12() == 124
    assert sum(P.AUX_SUBENVELOPES.values()) == P.AUX_SUBENVELOPE_TOTAL
    assert sum(int(s["call_rows"]) for s in P.HISTORY_SOURCES) == 31
    assert P.FAMILY_CAP == (P.FAMILY_HISTORY_POSTS + P.FAMILY_CLOSED_ROUND_POSTS
                            + P.RSI_R12_HARD_CAP)


def test_resolve_profile_legacy_by_default():
    assert P.resolve_profile(None).profile_id == "rsi-r1-legacy"
    assert P.resolve_profile({}).profile_id == "rsi-r1-legacy"
    assert P.resolve_profile({"schema": "x"}).profile_id == "rsi-r1-legacy"


def test_resolve_profile_declared_r12():
    profile = P.resolve_profile({"profile_id": "rsi-r1.2"})
    assert profile.profile_id == "rsi-r1.2"


def test_resolve_profile_rejects_unknown():
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"profile_id": "rsi-r1.3"})
    assert exc.value.code == "PROFILE_UNKNOWN"
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"profile_id": "rsi-r1.3"}, requested_campaign="rsi-r1.3")
    assert exc.value.code == "PROFILE_UNKNOWN"
    with pytest.raises(P.ProfileError):
        P.resolve_profile({"profile_id": 42})


def test_resolve_profile_rejects_mismatch():
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"profile_id": "rsi-r1.2"},
                          requested_campaign="rsi-r1-legacy")
    assert exc.value.code == "PROFILE_MISMATCH"
    # R1.2 根必须显式声明（envelope 存在但无 profile_id）
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"envelope_id": "x"}, requested_campaign="rsi-r1.2")
    assert exc.value.code == "PROFILE_MISMATCH"


def test_public_contract_derives_from_profile():
    r12 = P.PROFILE_R12_DEF
    doc = P.public_contract_document(r12, envelope_id="env1",
                                     budget_sha256="a" * 64)
    P.assert_public_contract_v2_complete(doc)
    assert doc["posts"]["per_lane"] == {
        "probe": 12, "practice": 16, "study_A": 48, "study_B": 48}
    assert doc["posts"]["total"] == 124
    assert doc["family"]["cap_posts"] == 403
    assert doc["family"]["history_posts"] == 31
    assert doc["compute"]["explore"]["max_runs_per_study"] == 64
    assert doc["compute"]["explore"]["max_batches_per_study"] == 8
    assert doc["compute"]["explore"]["cell"] == ["condition_id", "block_id"]
    assert doc["compute"]["confirm"]["blocks"] == 32
    assert doc["compute"]["confirm"]["max_conditions"] == 6
    assert doc["aux_subenvelopes"]["total"] == P.AUX_SUBENVELOPE_TOTAL
    assert doc["recovery"]["completion"].startswith("规范动作游标完整")
    text = P.render_public_contract_v2(doc)
    assert "64 是每 study 累计上限" in text
    assert "不得自动截断" in text


def test_public_contract_rejects_tamper():
    r12 = P.PROFILE_R12_DEF
    doc = P.public_contract_document(r12, envelope_id="env1",
                                     budget_sha256="b" * 64)
    broken = dict(doc)
    broken["posts"] = dict(doc["posts"])
    broken["posts"]["total"] = 143
    with pytest.raises(P.ProfileError) as exc:
        P.assert_public_contract_v2_complete(broken)
    assert exc.value.code == "PUBLIC_CONTRACT_INVALID"
    missing = {k: v for k, v in doc.items() if k != "units"}
    with pytest.raises(P.ProfileError):
        P.assert_public_contract_v2_complete(missing)


def test_public_contract_only_for_r12():
    legacy = P.PROFILE_LEGACY_DEF
    with pytest.raises(P.ProfileError):
        P.public_contract_document(legacy, envelope_id="x",
                                   budget_sha256="c" * 64)


def test_profile_to_dict_is_hashable_document():
    from orchestrator.rsi_r1.contracts import document_hash

    for profile in (P.PROFILE_LEGACY_DEF, P.PROFILE_R12_DEF):
        doc = profile.to_dict()
        assert doc["schema"] == P.PROFILE_SCHEMA
        assert doc["schema_version"] == P.SCHEMA_VERSION
        assert len(document_hash(doc)) == 64
        assert profile.is_legacy or profile.is_r12


def test_policy_error_classes_disjoint_and_terminal_contains_transport():
    assert not set(P.RECOVERABLE_ERROR_CODES) & set(P.TERMINAL_ERROR_CODES)
    for code in ("TRANSPORT_EMPTY_RESPONSE", "TRANSPORT_TIMEOUT", "SEND_UNKNOWN",
                 "LOCK_DRIFT", "INSTRUMENT_FAILURE", "BUDGET_EXHAUSTED"):
        assert code in P.TERMINAL_ERROR_CODES
    for code in ("RESOURCE_EXCEEDED", "SOURCE_SHA_INVALID", "MALFORMED_JSON"):
        assert code in P.RECOVERABLE_ERROR_CODES


def test_output_token_budget_covers_reasoning():
    """D-10：R1.2 输出预算 32768（含思考 token），legacy 保持 8192。"""
    assert P.PROFILE_R12_DEF.output_tokens == P.RSI_R12_OUTPUT_TOKENS == 32768
    assert P.PROFILE_LEGACY_DEF.output_tokens == 8192
    doc = P.public_contract_document(
        P.PROFILE_R12_DEF, envelope_id="env-x", budget_sha256="0" * 64)
    assert "32768" in doc["units"]["post"]
