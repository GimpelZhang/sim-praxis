"""RSI-R1.2 T0（AC02）：旧 profile/旧合同逐字节兼容，新代码不改变旧行为。

覆盖：

* 旧 lane 表数值、旧 BudgetSpec 默认件、旧 Consent（scope R1）行为不变；
* 无 profile 声明 / 显式 legacy → 旧路径；R1.2 表绝不回退到旧路径；
* 旧账本命名空间 2903 与 2904 互不复用；
* 旧闭合组（LOCK closure groups）名单未被 R1.2 改动（legacy 闭包哈希口径稳定）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lanes, lock as LK, profiles as P


def test_legacy_lane_table_values_frozen():
    assert lanes.LANE_ORDER == ("probe", "practice", "study_A", "study_B",
                                "repair_reserve")
    assert dict(lanes.LANE_POST_CAPS) == {
        "probe": 8, "practice": 16, "study_A": 48, "study_B": 48,
        "repair_reserve": 24}
    assert dict(lanes.RSI_R1_TASK_CAPS) == {0: 8, 1: 16, 2: 48, 3: 48, 4: 24}
    assert lanes.RSI_R1_HARD_CAP == 144
    assert lanes.RSI_R1_NORMAL_BUDGET == 120


def test_legacy_budgetspec_defaults_unchanged():
    budget = C.default_budget("legacy_env")
    assert budget.total_posts == 144
    assert budget.normal_posts == 120
    assert budget.posts_by_lane["repair_reserve"] == 24
    assert budget.max_total_runs == 896
    assert budget.max_steps == 10_000_000
    assert budget.sha256 == C.default_budget("legacy_env").sha256
    assert [s["slot"] for s in budget.slots()] == list(range(1, 145))
    assert sum(1 for s in budget.slots() if s["reserve"]) == 24


def test_legacy_budget_schema_unchanged():
    assert C.BUDGET_SCHEMA == "simpraxis.rsi_r1.budget.v1"
    assert C.ENVELOPE_SCHEMA == "simpraxis.rsi_r1.envelope.v1"
    assert C.CONSENT_SCHEMA == "simpraxis.rsi_r1.consent.v1"
    assert C.SCHEMA_VERSION == 1


def test_budget_dispatch_legacy_roundtrip():
    budget = C.default_budget("legacy_env")
    parsed = C.budget_from_dict(budget.to_dict())
    assert isinstance(parsed, C.BudgetSpec)
    assert parsed.sha256 == budget.sha256


def test_legacy_consent_scope_r1_still_works():
    consent = C.Consent(
        schema=C.CONSENT_SCHEMA, envelope_id="legacy_env",
        decision=C.COST_DECISION_SUPPLY, scope="R1",
        granted_at_utc="2026-11-11T00:00:00Z", granted_by="user",
        verbatim="", plan_path="/x",
        plan_sha256="0" * 64, supersedes=(), notes="")
    assert consent.paid_authorized
    assert isinstance(C.consent_from_dict(consent.to_dict()), C.Consent)
    C.assert_paid_authorized(consent)


def test_new_consent_schema_is_distinct():
    assert P.CONSENT_SCHEMA_V2 != C.CONSENT_SCHEMA
    assert P.BUDGET_SCHEMA_V2 != C.BUDGET_SCHEMA
    assert P.ENVELOPE_SCHEMA_V2 != C.ENVELOPE_SCHEMA


def test_ledger_namespaces_do_not_collide():
    assert QL.LEDGER_RSI_R1 == 2903
    assert QL.LEDGER_RSI_R12 == 2904
    assert QL.LEDGER_RSI_R12 not in {
        QL.LEDGER_PHASE, QL.LEDGER_PHASE25, QL.LEDGER_PHASE26, QL.LEDGER_PHASE27,
        QL.LEDGER_PHASE281, QL.LEDGER_RSI_R0, QL.LEDGER_RSI_R01,
        QL.LEDGER_RSI_R02, QL.LEDGER_RSI_R1,
    }
    assert dict(QL.RSI_R12_TASK_CAPS) == {0: 12, 1: 16, 2: 48, 3: 48}
    assert QL.RSI_R12_HARD_CAP == 124
    assert QL.RSI_R12_NORMAL_BUDGET == 124
    # 旧命名空间数值不变
    assert dict(QL.RSI_R1_TASK_CAPS) == {0: 8, 1: 16, 2: 48, 3: 48, 4: 24}
    assert QL.RSI_R1_HARD_CAP == 144
    assert QL.RSI_R1_NORMAL_BUDGET == 120


def test_legacy_closure_groups_untouched():
    # R1.2 不改变 legacy 闭包组的**成员清单**（组名与文件列表即旧 LOCK 口径）；
    # 共享模块内容演进会改变重算出的新哈希，但组成员/顺序保持不变。
    assert list(LK.CLOSURE_GROUPS) == ["runner", "shared", "scenes", "schema",
                                       "judge", "replay", "checker"]
    assert LK.CLOSURE_GROUPS["scenes"] == ("orchestrator/rsi_r1/scenes.py",)


def test_refusal_registry_keeps_legacy_and_adds_r12():
    for code in ("SCHEMA_MISMATCH", "BUDGET_DRIFT", "PAID_NOT_AUTHORIZED",
                 "CONSENT_STALE", "RESOURCE_EXCEEDED"):
        assert code in C.REFUSAL_CLAUSES
    for code in ("PROFILE_UNKNOWN", "PROFILE_MISMATCH", "FAMILY_CAP_EXCEEDED",
                 "HISTORY_DRIFT", "PUBLIC_CONTRACT_INVALID"):
        assert C.REFUSAL_CLAUSES[code].startswith("R12-")
    # R12 条款 id 在 R12 面内唯一，且与 legacy 条款 id 全集不相交
    r12 = {code: clause for code, clause in C.REFUSAL_CLAUSES.items()
           if clause.startswith("R12-")}
    # 17（T0 基座）+ 7（T1 资源账/最终请求审计条款 R12-S2-02..08）
    assert len(r12) == 24
    assert len(set(r12.values())) == len(r12)
    legacy_ids = {clause for code, clause in C.REFUSAL_CLAUSES.items()
                  if not clause.startswith("R12-")}
    assert not set(r12.values()) & legacy_ids
    # legacy 面唯一的 id 复用是既存别名对 NONFINITE / NONFINITE_PARAM（R1-S1-16），
    # 本阶段不改旧注册表，只把它钉成事实防止再漂。
    dup = [clause for clause in legacy_ids
           if sum(1 for v in C.REFUSAL_CLAUSES.values() if v == clause) > 1]
    assert dup == ["R1-S1-16"]


def test_legacy_budgetspec_refuses_v2_doc():
    budget = C.default_budget_v2("v2_env")
    with pytest.raises(C.ContractError) as exc:
        C.BudgetSpec.from_dict(budget.to_dict())
    assert exc.value.code in ("UNKNOWN_KEY", "SCHEMA_MISMATCH", "MISSING_KEY")


def test_v2_budget_refuses_legacy_doc():
    legacy = C.default_budget("legacy_env")
    with pytest.raises(C.ContractError):
        C.BudgetSpecV2.from_dict(legacy.to_dict())


def test_legacy_max_usd_literal_unchanged():
    assert C.MAX_USD_LITERAL == Decimal("100")
    assert P.MAX_USD_LITERAL_DECIMAL == "100"
