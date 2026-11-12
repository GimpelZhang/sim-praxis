"""RSI-R1.2 T0（AC08）：费用门 ConsentV2、schema 分派与 envelope v2 一致性。

覆盖：

* 三选一枚举（SUPPLY_HARD_GUARANTEE / R1_2_SCOPE_RISK_WAIVER / BLOCKED）；
* scope 必须是 ``R1.2``（旧 R0.x/R1 豁免不承继）；profile/family/history 逐项钉死；
* 风险豁免必须带原话、暴露处置与正的预留；
* ``assert_paid_authorized_v2``：BLOCKED 拒、无授权拒、有效授权放行；
* 分派：旧 schema → Consent，新 schema → ConsentV2，未知 → 拒；
* envelope v2：cost_gate 与 decision 一致、offline 不得带付费授权、未登记键拒。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import profiles as P

SCHEMA = C.CONSENT_SCHEMA_V2
FROZEN_LEDGERS = tuple(str(s["ledger_sha256"]) for s in P.HISTORY_SOURCES)
PLAN_SHA = "a" * 64


def _consent(**over):
    base = dict(
        schema=SCHEMA,
        envelope_id="env1",
        profile_id=P.PROFILE_R12,
        decision=C.COST_DECISION_SUPPLY_V2,
        scope="R1.2",
        granted_at_utc="2026-11-12T00:00:00Z",
        granted_by="user",
        verbatim="",
        plan_path="/home/vipuser/文档/RSI_R1.2_Plan_detailed.md",
        plan_sha256=PLAN_SHA,
        family_cap=P.FAMILY_CAP,
        history_posts=P.FAMILY_HISTORY_POSTS,
        history_ledger_sha256=FROZEN_LEDGERS,
        risk_reserve_usd=Decimal("0"),
        exposure_note="",
        supersedes=(),
        notes="",
    )
    base.update(over)
    return C.ConsentV2(**base)


def test_supply_guarantee_authorizes_and_roundtrips():
    consent = _consent()
    assert consent.paid_authorized
    C.assert_paid_authorized_v2(consent)
    doc = consent.to_dict()
    parsed = C.consent_from_dict(doc)
    assert isinstance(parsed, C.ConsentV2)
    assert parsed.sha256 == consent.sha256
    assert list(doc["history_ledger_sha256"]) == list(FROZEN_LEDGERS)
    assert len(doc) == 18


def test_waiver_requires_verbatim_note_and_reserve():
    with pytest.raises(C.ContractError) as exc:
        _consent(decision=C.COST_DECISION_WAIVER_V2,
                 risk_reserve_usd=Decimal("5"))
    assert exc.value.code == "PAID_NOT_AUTHORIZED"
    with pytest.raises(C.ContractError):
        _consent(decision=C.COST_DECISION_WAIVER_V2, verbatim="同意风险豁免",
                 exposure_note="", risk_reserve_usd=Decimal("5"))
    with pytest.raises(C.ContractError):
        _consent(decision=C.COST_DECISION_WAIVER_V2, verbatim="同意风险豁免",
                 exposure_note="每 POST 预留 0.5 USD", risk_reserve_usd=Decimal("0"))
    ok = _consent(decision=C.COST_DECISION_WAIVER_V2, verbatim="同意风险豁免",
                  exposure_note="每 POST 预留 0.5 USD，总暴露 ≤ 100 USD",
                  risk_reserve_usd=Decimal("5"))
    C.assert_paid_authorized_v2(ok)


def test_scope_and_profile_are_not_inheritable():
    with pytest.raises(C.ContractError) as exc:
        _consent(scope="R1")
    assert exc.value.code == "CONSENT_STALE"
    with pytest.raises(C.ContractError) as exc:
        _consent(scope="R0.2")
    assert exc.value.code == "CONSENT_STALE"
    with pytest.raises(C.ContractError) as exc:
        _consent(profile_id="rsi-r1-legacy")
    assert exc.value.code == "PROFILE_MISMATCH"


def test_family_and_history_figures_are_pinned():
    with pytest.raises(C.ContractError) as exc:
        _consent(family_cap=144)
    assert exc.value.code == "FAMILY_CAP_EXCEEDED"
    with pytest.raises(C.ContractError) as exc:
        _consent(history_posts=22)
    assert exc.value.code == "HISTORY_DRIFT"
    with pytest.raises(C.ContractError) as exc:
        _consent(history_ledger_sha256=FROZEN_LEDGERS[::-1])
    assert exc.value.code == "HISTORY_DRIFT"
    with pytest.raises(C.ContractError) as exc:
        _consent(history_ledger_sha256=(FROZEN_LEDGERS[0],))
    assert exc.value.code == "HISTORY_DRIFT"


def test_blocked_decision_and_assert():
    blocked = _consent(decision=C.COST_DECISION_BLOCKED_V2)
    assert not blocked.paid_authorized
    with pytest.raises(C.ContractError) as exc:
        C.assert_paid_authorized_v2(blocked)
    assert exc.value.code == "COST_GATE_BLOCKED"
    # 非 ConsentV2 一律拒（旧 Consent 不能被当新门授权）
    legacy = C.Consent(
        schema=C.CONSENT_SCHEMA, envelope_id="env1",
        decision=C.COST_DECISION_SUPPLY, scope="R1",
        granted_at_utc="2026-11-11T00:00:00Z", granted_by="user",
        verbatim="", plan_path="/x", plan_sha256="0" * 64,
        supersedes=(), notes="")
    with pytest.raises(C.ContractError) as exc:
        C.assert_paid_authorized_v2(legacy)  # type: ignore[arg-type]
    assert exc.value.code == "CONSENT_STALE"


def test_consent_dispatch_and_unknown_schema():
    legacy = C.Consent(
        schema=C.CONSENT_SCHEMA, envelope_id="env1",
        decision=C.COST_DECISION_SUPPLY, scope="R1",
        granted_at_utc="2026-11-11T00:00:00Z", granted_by="user",
        verbatim="", plan_path="/x", plan_sha256="0" * 64,
        supersedes=(), notes="")
    assert isinstance(C.consent_from_dict(legacy.to_dict()), C.Consent)
    with pytest.raises(C.ContractError) as exc:
        C.consent_from_dict({"schema": "simpraxis.rsi_r12.consent.v9"})
    assert exc.value.code == "SCHEMA_MISMATCH"


def test_consent_v2_rejects_extra_or_missing_keys():
    doc = _consent().to_dict()
    extra = dict(doc)
    extra["legacy_blessing"] = True
    with pytest.raises(C.ContractError) as exc:
        C.consent_from_dict(extra)
    assert exc.value.code == "UNKNOWN_KEY"
    missing = {k: v for k, v in doc.items() if k != "exposure_note"}
    with pytest.raises(C.ContractError) as exc:
        C.consent_from_dict(missing)
    assert exc.value.code == "MISSING_KEY"


def test_waiver_schema_version_enforced():
    doc = _consent().to_dict()
    doc["schema_version"] = 2
    with pytest.raises(C.ContractError) as exc:
        C.consent_from_dict(doc)
    assert exc.value.code == "SCHEMA_MISMATCH"


# ---------------------------------------------------------------------------
# envelope v2
# ---------------------------------------------------------------------------

def _envelope(**over):
    from orchestrator.rsi_r1 import profiles as P12

    budget = C.default_budget_v2("env1")
    doc = dict(
        schema=P12.ENVELOPE_SCHEMA_V2,
        schema_version=P12.SCHEMA_VERSION,
        profile_id=P12.PROFILE_R12,
        envelope_id="env1",
        mode="offline_validation",
        created_at_utc="2026-11-12T00:00:00Z",
        plan_path="/home/vipuser/文档/RSI_R1.2_Plan_detailed.md",
        plan_sha256=PLAN_SHA,
        budget_path="contract/budget.json",
        budget_sha256=budget.sha256,
        profile_sha256=C.document_hash(P12.PROFILE_R12_DEF.to_dict()),
        ledger_namespace=2904,
        hard_cap=124,
        normal_budget=124,
        lane_caps=dict(P12.LANE_POST_CAPS_R12),
        family_cap=P12.FAMILY_CAP,
        history_posts=P12.FAMILY_HISTORY_POSTS,
        family_permit_path="validation/family_permit.json",
        family_permit_sha256="b" * 64,
        cost_gate={"decision": "BLOCKED", "paid_authorized": False,
                   "reason": "T0 默认阻断"},
        public_science_scope_sha256="c" * 64,
        public_contract_sha256="d" * 64,
    )
    doc.update(over)
    return doc


def test_envelope_v2_valid_and_id_match():
    doc = _envelope()
    parsed = C.validate_envelope_v2(doc, envelope_id="env1")
    assert parsed["mode"] == "offline_validation"
    with pytest.raises(C.ContractError) as exc:
        C.validate_envelope_v2(doc, envelope_id="env2")
    assert exc.value.code == "PROFILE_MISMATCH"


def test_envelope_v2_offline_must_not_carry_paid():
    doc = _envelope(cost_gate={"decision": C.COST_DECISION_SUPPLY_V2,
                               "paid_authorized": True, "reason": "x"})
    with pytest.raises(C.ContractError) as exc:
        C.validate_envelope_v2(doc)
    assert exc.value.code == "OFFLINE_AUTH_REQUIRED"
    # 但 paid 模式下同一 cost_gate 合法
    doc["mode"] = "paid"
    assert C.validate_envelope_v2(doc)["mode"] == "paid"


def test_envelope_v2_cost_gate_consistency():
    doc = _envelope(cost_gate={"decision": "BLOCKED", "paid_authorized": True})
    with pytest.raises(C.ContractError) as exc:
        C.validate_envelope_v2(doc)
    assert exc.value.code == "PAID_NOT_AUTHORIZED"
    doc = _envelope(cost_gate={"decision": "NOT_A_DECISION",
                               "paid_authorized": False})
    with pytest.raises(C.ContractError) as exc:
        C.validate_envelope_v2(doc)
    assert exc.value.code == "PAID_NOT_AUTHORIZED"


def test_envelope_v2_drift_and_unknown_keys():
    for patch, code in (
            ({"hard_cap": 144}, "BUDGET_DRIFT"),
            ({"normal_budget": 120}, "BUDGET_DRIFT"),
            ({"ledger_namespace": 2903}, "BUDGET_DRIFT"),
            ({"lane_caps": {"probe": 8, "practice": 16, "study_A": 48,
                            "study_B": 48}}, "BUDGET_DRIFT"),
            ({"family_cap": 144}, "FAMILY_CAP_EXCEEDED"),
            ({"history_posts": 22}, "HISTORY_DRIFT"),
            ({"profile_id": "rsi-r1-legacy"}, "PROFILE_MISMATCH"),
            ({"mode": "whatever"}, "WRONG_TYPE"),
            ({"created_at_utc": "2026-11-12T00:00:00+08:00"}, "SCHEMA_MISMATCH"),
            ({"plan_sha256": "ABC"}, "SCHEMA_MISMATCH")):
        with pytest.raises(C.ContractError) as exc:
            C.validate_envelope_v2(_envelope(**patch))
        assert exc.value.code == code, patch
    extra = _envelope()
    extra["sneaky"] = 1
    with pytest.raises(C.ContractError) as exc:
        C.validate_envelope_v2(extra)
    assert exc.value.code == "UNKNOWN_KEY"


def test_envelope_v2_profile_hash_matches_profile_doc():
    doc = _envelope()
    assert doc["profile_sha256"] == C.document_hash(P.PROFILE_R12_DEF.to_dict())
    assert len(doc) == len(C.ENVELOPE_KEYS_V2) == 22
