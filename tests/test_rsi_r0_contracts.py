"""RSI-R0 合同层纯函数测试（零联网、零付费、零 IO）。

覆盖计划 §3.2/§3.3/§4.1/§9.1 的固定常量与判决全序（AC29/AC30 的纯函数半边）。
"""
import pytest

from orchestrator.rsi_r0 import contracts as C


def test_nine_instances_and_fixed_order():
    assert len(C.INSTANCE_IDS) == 9
    assert set(C.INSTANCE_IDS) == {
        "E-dev", "E-v1", "E-v2", "D-dev", "D-v1", "D-v2", "R-dev", "R-v1", "R-v2",
    }
    # 计划 §3.3：验证顺序逐字固定，不得按即时成功情况选题
    assert C.VALIDATION_ORDER == ("E-v1", "D-v1", "R-v1", "E-v2", "D-v2", "R-v2")
    assert C.DEVELOPMENT_ORDER == ("E-dev", "D-dev", "R-dev")
    assert C.SLOT_REQUEST_CAP == 24
    assert C.CHECKPOINTS == (8, 16, 24)


def test_task_ids_protocol_e_d_r():
    assert C.TASK_ID_PROTOCOL == 0
    assert C.instance_task_id("protocol") == 0
    assert C.instance_task_id("E-v1") == 1
    assert C.instance_task_id("D-dev") == 2
    assert C.instance_task_id("R-v2") == 3


def test_family_parsing_and_role():
    assert C.instance_family("E-v1") is C.Family.E
    assert C.instance_family("R-dev") is C.Family.R
    assert C.is_development("D-dev") and not C.is_development("D-v1")
    with pytest.raises(ValueError):
        C.instance_family("X-v1")


def test_l5_not_applicable_for_engineering_families():
    assert C.Layer.L5 not in C.applicable_layers(C.Family.E)
    assert C.Layer.L5 not in C.applicable_layers(C.Family.D)
    assert C.Layer.L5 in C.applicable_layers(C.Family.R)
    for fam in C.Family:
        assert C.applicable_layers(fam)[0] is C.Layer.L0


def test_verdict_priority_ordering_matches_plan():
    order = C.CALIBRATION_VERDICT_PRIORITY
    # §9.1：安全优先于一切
    assert order[0] is C.CalibrationVerdict.SAFETY_INVALID
    # §9.1 第 5 类（外部中断）在第 6 类（协议实测失败）之前
    assert order.index(C.CalibrationVerdict.ABORTED_INCOMPLETE) < \
        order.index(C.CalibrationVerdict.PROTOCOL_BLOCKED)
    # ASSISTED_ONLY 优先于第 8/9 类
    assert order.index(C.CalibrationVerdict.ASSISTED_ONLY) < \
        order.index(C.CalibrationVerdict.CEILING_WARNING)
    assert order.index(C.CalibrationVerdict.ASSISTED_ONLY) < \
        order.index(C.CalibrationVerdict.CALIBRATED_PILOT)


def test_pick_primary_verdict_keeps_all_reason_codes():
    primary, others = C.pick_primary_verdict([
        C.CalibrationVerdict.RESEARCH_FLOOR,
        C.CalibrationVerdict.SAFETY_INVALID,
        C.CalibrationVerdict.BLOCKED_COST,
    ])
    assert primary is C.CalibrationVerdict.SAFETY_INVALID
    assert others == (C.CalibrationVerdict.BLOCKED_COST,
                      C.CalibrationVerdict.RESEARCH_FLOOR)


def test_pick_primary_verdict_empty_is_unclassified():
    primary, others = C.pick_primary_verdict([])
    assert primary is C.CalibrationVerdict.UNCLASSIFIED
    assert others == ()


def test_layer_helpers_first_failure_and_all_pass():
    fam = C.Family.E
    ok = {ly: C.LayerResult(ly, C.LayerStatus.PASS, "ok") for ly in
          C.applicable_layers(fam)}
    assert C.first_failure(fam, ok) is None
    assert C.all_applicable_pass(fam, ok) is True

    bad = dict(ok)
    bad[C.Layer.L2] = C.LayerResult(C.Layer.L2, C.LayerStatus.FAIL, "load_failed")
    assert C.first_failure(fam, bad) is C.Layer.L2
    assert C.all_applicable_pass(fam, bad) is False

    # 缺层也算未过（不能靠缺席被当成通过）
    missing = dict(ok)
    del missing[C.Layer.L4]
    assert C.first_failure(fam, missing) is C.Layer.L4
    assert C.all_applicable_pass(fam, missing) is False


def test_l5_outside_engineering_ladder_never_blocks():
    """E/D 的层梯止于 L4：L5 不适用时既不缺席也不算失败。"""
    fam = C.Family.E
    layers = {ly: C.LayerResult(ly, C.LayerStatus.PASS, "ok")
              for ly in C.applicable_layers(fam)}
    layers[C.Layer.L5] = C.LayerResult(C.Layer.L5, C.LayerStatus.NOT_APPLICABLE, "n/a")
    assert C.first_failure(fam, layers) is None

    # R 族的 L5 **是**适用层：把它标 NOT_APPLICABLE 属自相矛盾，按未过处理。
    r_layers = {ly: C.LayerResult(ly, C.LayerStatus.PASS, "ok")
                for ly in C.applicable_layers(C.Family.R)}
    r_layers[C.Layer.L5] = C.LayerResult(C.Layer.L5, C.LayerStatus.NOT_APPLICABLE, "n/a")
    assert C.first_failure(C.Family.R, r_layers) is C.Layer.L5


def test_research_criteria_completeness_guard():
    assert len(C.RESEARCH_CRITERIA) == 8
    C.assert_all_criteria_present(C.RESEARCH_CRITERIA)
    with pytest.raises(C.CriteriaError):
        C.assert_all_criteria_present(C.RESEARCH_CRITERIA[:-1])


def test_summary_keeps_cancelled_in_denominator_and_never_p_values():
    def make(iid, state, execution=C.ExecutionVerdict.COMPLETE):
        return C.TrialResult(slot_id=iid, instance_id=iid,
                             family=C.instance_family(iid), role="validation",
                             state=state, execution=execution)

    results = [
        make("E-v1", C.SlotState.COMPLETED),
        make("E-v2", C.SlotState.CANCELLED),
        make("D-v1", C.SlotState.COMPLETED),
        make("R-v1", C.SlotState.COMPLETED),
    ]
    summary = C.summarize_metric(results)
    assert summary["all_assigned"] == 4
    assert summary["families"]["E"]["assigned"] == 2
    assert summary["families"]["E"]["completed"] == 1
    assert summary["families"]["E"]["execution_complete"] == 1
    assert "p_value" not in summary and "ci" not in summary
    assert "n=2/族" in summary["note"]


def test_canonical_hash_is_order_insensitive_and_stable():
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert C.canonical_json(a) == C.canonical_json(b)
    assert C.sha256_of(a) == C.sha256_of(b)
    assert len(C.sha256_of(a)) == 64


def test_resource_caps_match_plan_arithmetic():
    caps = C.ResourceCaps()
    assert caps.per_request_input_tokens * 222 == caps.cumulative_input_tokens
    assert caps.per_request_output_tokens * 222 == caps.cumulative_output_tokens
    assert caps.cumulative_input_tokens + caps.revalidation_input_tokens == 4_520_000
    assert caps.cumulative_output_tokens + caps.revalidation_output_tokens == 1_851_392
    assert caps.total_usd_equivalent == 100.0
    assert caps.trial_wall_s == 45 * 60
    assert caps.request_timeout_s == 180
    assert caps.stage_online_wall_s == 12 * 3600
    assert caps.stage_sim_steps == 10_000_000
