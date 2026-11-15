"""RSI-R1.3 T1：任务规格、近重复门、线性对比与资源计划。

覆盖 Plan §9 AC07/09/10/11 的零 POST 部分：

* AC07 两留出机制不同；只换 seed/参数随机数不构成新任务；
* AC09 非法干预/不支持指标在任务规格期拒绝；
* AC10 计算按实际 dt/步数/重复数求和（A 固定 0.0005 ⇒ 8000，B 0.001 ⇒ 4000）；
* AC11 维护参考、资格、正式、replay、边界套全部入账；共享执行只计一次。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.rsi_r1 import contrasts as X
from orchestrator.rsi_r1 import resource_plan as RP
from orchestrator.rsi_r1 import task_specs as TS


# ---------------------------------------------------------------------------
# AC07 / AC09 task specs
# ---------------------------------------------------------------------------

def test_holdout_tasks_are_different_mechanisms_and_not_near_duplicates():
    old_a, old_b = TS.build_old_a(), TS.build_old_b()
    new_a, new_b = TS.build_new_a(), TS.build_new_b()
    assert new_a.mechanism_id != new_b.mechanism_id
    TS.assert_not_near_duplicate(old_a, new_a)
    TS.assert_not_near_duplicate(old_b, new_b)
    assert new_a.primary.kind == X.KIND_DID
    assert new_b.primary.kind == X.KIND_DID
    assert new_a.intervention_signature() != old_a.intervention_signature()
    assert new_b.intervention_signature() != old_b.intervention_signature()


def test_seed_only_change_is_rejected():
    old = TS.build_old_a()
    clone = TS.build_old_a()
    with pytest.raises(TS.TaskSpecError) as exc:
        TS.assert_not_near_duplicate(old, clone)
    assert exc.value.code == "TASK_NEAR_DUPLICATE"


def test_did_requires_full_factorial():
    spec = TS.build_new_a()
    # 删掉一个角，四条件不再全因子
    import dataclasses
    broken = dataclasses.replace(
        spec, conditions=tuple(c for c in spec.conditions if c.id != "lo_low_mu"))
    with pytest.raises(TS.TaskSpecError) as exc:
        broken.validate()
    assert exc.value.code in ("DID_NOT_FACTORIAL", "CONDITIONS_INVALID")


def test_out_of_domain_intervention_rejected_at_task_time():
    import dataclasses
    spec = TS.build_new_a()
    bad = spec.conditions[0].to_dict()
    bad["params"]["sliding_friction"] = "0.95"   # 场景域上限 0.8
    from orchestrator.rsi_r1 import contracts as C
    with pytest.raises(C.ContractError):
        C.Condition.from_dict(bad, where="bad")


def test_negative_control_single_factor_enforced():
    import dataclasses
    spec = TS.build_new_a()
    nc = dict(spec.negative_control_spec)
    contrast = dict(nc["contrast"])
    terms = [dict(t) for t in contrast["terms"]]
    # 让 sham 对比同时改 v0：不再单因子
    terms[1]["condition_id"] = "lo_base"
    contrast["terms"] = terms
    nc["contrast"] = contrast
    broken = dataclasses.replace(spec, negative_control_spec=nc)
    with pytest.raises(TS.TaskSpecError):
        broken.validate()


# ---------------------------------------------------------------------------
# contrasts
# ---------------------------------------------------------------------------

def test_pairwise_contrast_estimate_matches_statistics():
    c = X.ContrastV13(
        id="p", kind=X.KIND_PAIRWISE, metric="displacement_x_m",
        proposition="increase_gt", delta=Decimal("0.001"),
        terms=(("t", Decimal("1")), ("c", Decimal("-1")))).validate()
    values = {f"b{i}": {"t": str(2.0 + i * 0.1), "c": "1.0"} for i in range(4)}
    est = c.estimate(values)
    assert est["n_blocks"] == 4
    assert est["effect"] == pytest.approx(1.15)
    assert est["contrast_kind"] == X.KIND_PAIRWISE


def test_did_contrast_estimates_interaction():
    c = X.ContrastV13(
        id="d", kind=X.KIND_DID, metric="displacement_x_m",
        proposition="increase_gt", delta=Decimal("0.001"),
        terms=X.did_terms(treatment_high="th", treatment_low="tl",
                          control_high="ch", control_low="cl")).validate()
    # 交互 = 1.0（高速度下摩擦效应比低速度大 1.0）
    values = {f"b{i}": {"th": "3.0", "tl": "2.0", "ch": "2.0", "cl": "2.0"}
              for i in range(3)}
    est = c.estimate(values)
    assert est["effect"] == pytest.approx(1.0)


def test_contrast_rejects_delta_drift_and_bad_weights():
    with pytest.raises(X.ContrastError) as exc:
        X.ContrastV13(id="p", kind=X.KIND_PAIRWISE, metric="displacement_x_m",
                      proposition="increase_gt", delta=Decimal("0.5"),
                      terms=(("t", Decimal("1")), ("c", Decimal("-1")))).validate()
    assert exc.value.code == "DELTA_DRIFT"
    with pytest.raises(X.ContrastError) as exc:
        X.ContrastV13(id="p", kind=X.KIND_PAIRWISE, metric="displacement_x_m",
                      proposition="increase_gt", delta=Decimal("0.001"),
                      terms=(("t", Decimal("1")), ("c", Decimal("1")))).validate()
    assert exc.value.code == "WEIGHTS_NOT_CENTERED"


def test_missing_block_condition_is_not_silently_dropped():
    c = X.ContrastV13(
        id="p", kind=X.KIND_PAIRWISE, metric="displacement_x_m",
        proposition="increase_gt", delta=Decimal("0.001"),
        terms=(("t", Decimal("1")), ("c", Decimal("-1")))).validate()
    with pytest.raises(X.ContrastError) as exc:
        c.block_effects({"b1": {"t": "1.0"}, "b2": {"t": "1.0", "c": "0.5"}})
    assert exc.value.code == "BLOCK_INCOMPLETE"


# ---------------------------------------------------------------------------
# AC10 / AC11 resource plan
# ---------------------------------------------------------------------------

def test_steps_follow_actual_spec_not_old_assumption():
    a = TS.build_new_a()
    b = TS.build_new_b()
    assert RP.steps_per_run(a.conditions[0], scene_id="A_contact_friction") == 8000
    by_id = {c.id: c for c in b.conditions}
    assert RP.steps_per_run(by_id["s_low"], scene_id="B_sampling_integration") == 4000


def test_resource_plan_covers_all_suites_and_shared_execution():
    plan = RP.build_plan((TS.build_new_a(), TS.build_new_b()))
    ids = {e.suite_id for e in plan.entries}
    for tid in (TS.NEW_A_TASK_ID, TS.NEW_B_TASK_ID):
        assert f"maint-confirm-{tid}" in ids
        assert f"maint-replay-{tid}" in ids
        assert f"maint-pilot-{tid}" in ids
        assert f"cand-explore-{tid}" in ids
        assert f"cand-confirm-{tid}" in ids
        assert f"cand-replay-{tid}" in ids
    assert "qualification-toy-chains" in ids
    assert "boundary-fault-suites" in ids
    # 同一 execution_id 不得重复计步
    seen: "dict[str, int]" = {}
    for entry in plan.entries:
        if entry.execution_id in seen:
            assert seen[entry.execution_id] == entry.steps
        seen[entry.execution_id] = entry.steps
    assert plan.total_steps == sum(e.steps for e in plan.entries)
    assert plan.hard_cap_steps >= plan.total_steps
    assert plan.hard_cap_steps % 100_000 == 0


def test_resource_plan_matches_hand_computation():
    plan = RP.build_plan((TS.build_new_a(), TS.build_new_b()))
    # A: 5 条件 × 32 block × 8000 = 1,280,000；确认+replay = 2,560,000
    # B: 4 条件 × 32 block × 4000 = 512,000；确认+replay = 1,024,000
    assert plan.subenvelopes[RP.ENV_MAINTENANCE] == 3_584_000
    assert plan.subenvelopes[RP.ENV_CANDIDATE] == 3_584_000
    # 探索：A 64×8000 + B 64×8000（最坏 dt）
    assert plan.subenvelopes[RP.ENV_EXPLORE] == 1_024_000
    # pilot：A 4×5×8000 + B 4×4×4000
    assert plan.subenvelopes[RP.ENV_PILOT] == 224_000
    assert plan.subenvelopes[RP.ENV_QUALIFICATION] == 3 * 96 * 8000
    assert plan.subenvelopes[RP.ENV_BOUNDARY] == 240_000
    assert plan.total_steps == 10_960_000
    assert plan.hard_cap_steps == 11_900_000


def test_task_resource_sha_is_stable_and_task_specific():
    plan = RP.build_plan((TS.build_new_a(), TS.build_new_b()))
    sha_a = plan.task_sha256(TS.NEW_A_TASK_ID)
    assert sha_a == plan.task_sha256(TS.NEW_A_TASK_ID)
    assert sha_a != plan.task_sha256(TS.NEW_B_TASK_ID)
    assert len(sha_a) == 64


def test_taskset_binds_both_splits_and_difference_table():
    plan = RP.build_plan((TS.build_new_a(), TS.build_new_b()))
    doc = TS.build_taskset(
        a_commitment="a" * 64, b_commitment="b" * 64,
        a_resource_sha=plan.task_sha256(TS.NEW_A_TASK_ID),
        b_resource_sha=plan.task_sha256(TS.NEW_B_TASK_ID),
        difference_table_sha="d" * 64)
    assert doc["schema"] == "simpraxis.rsi_r13.taskset_manifest.v1"
    assert set(doc["holdout_task_ids"]) == {TS.NEW_A_TASK_ID, TS.NEW_B_TASK_ID}
    assert set(doc["development_task_ids"]) == {TS.OLD_A_TASK_ID,
                                                TS.OLD_B_TASK_ID}
    assert all(c["ok"] for c in doc["near_duplicate_checks"])
    assert len(TS.taskset_sha256(doc)) == 64
