"""RSI-R1.3 campaign：v13 合同、validation LOCK 三签与 T5→paid 绑定（零物理）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import contracts_r13 as C13
from orchestrator.rsi_r1 import lock_r13 as L13
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import runtime as RT
from orchestrator.rsi_r1 import task_specs as TS

IMAGE = "sim-praxis/sandbox@sha256:" + "a" * 64


@pytest.fixture()
def ctx(tmp_path):
    root = tmp_path / "camp"
    for name in ("contract", "private", "public", "quota", "observations"):
        (root / name).mkdir(parents=True, exist_ok=True)
    c = RT.CampaignContext(envelope_id="t5test", root=root, protected_roots=())
    budget = C13.default_budget_v13("t5test", hard_cap_steps=11900000,
                                    aux_subenvelopes={"x": 1})
    c.write_json("contract/budget.json", budget.to_dict())
    (root / "contract" / "taskset_manifest.json").write_text("{}\n")
    (root / "contract" / "resource_plan.json").write_text("{}\n")
    c.write_json("contract/t5_summary.json", {"all_ok": True, "studies": []})
    return c


def test_v13_contracts_and_ledger_namespace():
    budget = C13.default_budget_v13("env", hard_cap_steps=11900000)
    assert budget.total_posts == 132
    assert dict(budget.posts_by_lane) == dict(P.LANE_POST_CAPS_R13)
    parsed = C.budget_from_dict(budget.to_dict())
    assert parsed.sha256 == budget.sha256
    consent = C13.ConsentV13(
        decision=C13.COST_AUTHORIZED, paid_authorized=True,
        envelope_id="env", profile_id=P.PROFILE_R13, plan_path="p",
        plan_sha256="a" * 64, budget_sha256=budget.sha256, scope="R1.3",
        max_posts=132, review_calibration_posts=4, review_final_posts=4,
        family_cap=863, exposure_note="x", authorized_at_utc="2026-11-15T00:00:00Z",
        authorization={"source": "user"}, risk={"usd": "UNKNOWN"})
    assert C.consent_from_dict(consent.to_dict()).max_posts == 132
    assert QL.RSI_R13_NAMESPACE.hard_cap == 132
    assert QL.RSI_R13_TASK_CAPS[4] == 4 and QL.RSI_R13_TASK_CAPS[5] == 4


def test_validation_lock_requires_t5_and_three_distinct_actors(ctx):
    lock = L13.build_validation_lock(ctx, image_pin=IMAGE)
    assert len(lock["sha256"]) == 64
    for role in L13.VALIDATION_ROLES:
        L13.sign_validation(ctx, role=role, actor=f"actor-{role}",
                            decision="APPROVE", rationale="evidence checked")
    out = L13.verify_validation(ctx)
    assert out["ok"]
    binding = L13.build_binding(ctx)
    L13.verify_binding(binding)
    assert binding["validation_lock_doc_sha256"] == lock["sha256"]


def test_validation_lock_rejects_reused_actor(ctx):
    L13.build_validation_lock(ctx, image_pin=IMAGE)
    for role in L13.VALIDATION_ROLES:
        L13.sign_validation(ctx, role=role, actor="same-actor",
                            decision="APPROVE", rationale="x")
    with pytest.raises(L13.Lock13Error) as exc:
        L13.verify_validation(ctx)
    assert exc.value.code == "SIGNATURE_ACTOR_REUSED"


def test_validation_lock_rejects_t5_not_passed(tmp_path):
    root = tmp_path / "c2"
    for name in ("contract",):
        (root / name).mkdir(parents=True, exist_ok=True)
    c = RT.CampaignContext(envelope_id="x", root=root, protected_roots=())
    (root / "contract" / "taskset_manifest.json").write_text("{}\n")
    (root / "contract" / "resource_plan.json").write_text("{}\n")
    c.write_json("contract/t5_summary.json", {"all_ok": False})
    with pytest.raises(L13.Lock13Error) as exc:
        L13.build_validation_lock(c, image_pin=IMAGE)
    assert exc.value.code == "T5_NOT_PASSED"
