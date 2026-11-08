"""RSI-R0 T3 控制矩阵契约：21 负控逐类逐层被拒、6 项 mutation 全被杀、判据冻结。

全部离线（合成基线 + 纯函数）。真实重放的正控在 ``scripts/rsi_r0/t3_controls.py``，
产物 ``protocol/t3_controls_report.json``——那里才启容器。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from orchestrator.rsi_r0 import controls
from orchestrator.rsi_r0.contracts import Layer, LayerStatus, ScientificVerdict
from orchestrator.rsi_r0.evaluator import (
    EvaluationInput,
    matches_expected,
    scientific_axis,
    evaluate,
)
from orchestrator.rsi_r0.instances import PLANS, plan_for

CASES = {c.case_id: c for c in controls.negative_cases()}


# ---------------------------------------------------------------------------
# 负控矩阵的形状
# ---------------------------------------------------------------------------
def test_matrix_has_three_cases_per_category():
    assert len(CASES) == 21
    by_cat = {}
    for case in controls.negative_cases():
        by_cat.setdefault(case.category, []).append(case.case_id)
    assert set(by_cat) == set(controls.NEGATIVE_CATEGORIES)
    assert all(len(v) == 3 for v in by_cat.values()), by_cat


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_every_negative_control_is_rejected_at_its_expected_layer(case_id):
    case = CASES[case_id]
    rejected, code = controls.status_is_rejection(case.build(), case)
    assert rejected, f"{case_id}（{case.description}）未在 {case.expected_layer} 被拒，实得 {code}"


def test_negative_controls_cover_every_layer_except_the_research_ready_path():
    layers = {c.expected_layer for c in controls.negative_cases()}
    assert layers == {Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L4, Layer.L5}


def test_a_control_must_fail_at_the_expected_layer_not_merely_fail_somewhere():
    """把同样的输入配到错误的期望层上，判定必须不成立——层是断言的一部分。"""
    case = CASES["N01"]
    wrong_layer = dataclasses.replace(case, expected_layer=Layer.L5,
                                      expected_code="C5_independent_analysis")
    rejected, _code = controls.status_is_rejection(wrong_layer.build(), wrong_layer)
    assert not rejected


# ---------------------------------------------------------------------------
# mutation：每条判据真的在承重
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(controls.MUTANTS))
def test_every_mutant_is_killed_by_at_least_one_target_case(name):
    """杀灭 = 摘掉该判据后负控**整条被放行**，不是"换层仍被拒"。"""
    rows = []
    for case_id in controls.MUTANTS[name]["killed_by"]:
        case = CASES[case_id]
        baseline, _ = controls.status_is_rejection(case.build(), case)
        run = controls.run_mutant(name, case)
        rows.append({"case": case_id, "baseline": baseline,
                     "released": run["released_under_mutation"],
                     "execution": run["execution"]})
    assert any(r["baseline"] and r["released"] for r in rows), rows


def test_a_mutant_that_only_pushes_the_case_to_another_layer_is_not_a_kill():
    """反面对照：变异后仍被别层拦下 ⇒ 不算杀灭（这条口径正是之前的漏算点）。"""
    case = CASES["N16"]
    run = controls.run_mutant("M6_accept_claimed_verdict", case)
    assert run["released_under_mutation"] is False
    assert run["rejected_at_expected_layer"] is False


def test_mutating_an_unrelated_predicate_does_not_kill_the_target_case():
    """反面对照：M2（跳重放）不该让 N01（伪造 metrics）漏检——否则杀灭无从归因。"""
    run = controls.run_mutant("M2_skip_replay_check", CASES["N01"])
    assert run["released_under_mutation"] is False


def test_every_mutant_names_a_patchable_predicate():
    for name, meta in controls.MUTANTS.items():
        assert meta["patch"].startswith("evaluator."), name
        assert meta["killed_by"], name
        assert all(k in CASES for k in meta["killed_by"]), name


# ---------------------------------------------------------------------------
# 判据冻结（AC20）
# ---------------------------------------------------------------------------
def test_criteria_hash_is_deterministic_and_sensitive_to_the_thresholds():
    baseline = controls.criteria_hash(PLANS)
    assert baseline == controls.criteria_hash(PLANS)

    loosened = [dataclasses.replace(
        plan_for("D-v1"),
        private={**plan_for("D-v1").private,
                 "tolerance": {"kind": "exact", "scale_rel": 0.9}})]
    assert controls.criteria_hash(loosened) != baseline

    # 实例集合本身也是判据的一部分：少一个实例哈希就必须变
    assert controls.criteria_hash(PLANS[:-1]) != baseline


def test_freeze_refuses_a_changed_definition(tmp_path):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "t3_controls", root / "scripts" / "rsi_r0" / "t3_controls.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload, digest = mod.freeze_criteria(tmp_path, PLANS)
    assert payload["criteria_hash"] == digest
    on_disk = json.loads((tmp_path / "t3_criteria.json").read_text(encoding="utf-8"))
    assert on_disk["criteria_hash"] == digest
    assert mod.freeze_criteria(tmp_path, PLANS)[1] == digest        # 幂等

    tampered = tmp_path / "t3_criteria.json"
    tampered.write_text(json.dumps({**on_disk, "criteria_hash": "0" * 64}),
                        encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.freeze_criteria(tmp_path, PLANS)


def test_criteria_definition_carries_the_negative_and_mutation_lists():
    definition = controls.criteria_definition(PLANS)
    assert definition["negative_categories"] == list(controls.NEGATIVE_CATEGORIES)
    assert set(definition["mutants"]) == set(controls.MUTANTS)
    for name, meta in definition["mutants"].items():
        assert meta["patch"] == controls.MUTANTS[name]["patch"]
        assert meta["killed_by"] == list(controls.MUTANTS[name]["killed_by"])
    assert set(definition["negative_cases"]) == set(CASES)
    assert set(definition["instances"]) == {p.instance_id for p in PLANS}


def test_criteria_hash_covers_private_truth_not_only_the_public_surface():
    """改私有真值（预期结论/容差/干预水平……）同样必须让判据哈希变。"""
    base = controls.criteria_hash(PLANS)
    for field, value in (("expected", "REFUTED"),
                         ("tolerance", {"kind": "exact", "scale_rel": 0.99}),
                         ("levels", [0.1, 0.9]),
                         ("fault", {"axis": "y"})):
        plan = plan_for("R-v1")
        if field not in plan.private and field not in ("expected", "tolerance"):
            plan = plan_for("D-v1")
        if field not in plan.private:
            continue
        patched = dataclasses.replace(
            plan, private={**plan.private, field: value})
        assert controls.criteria_hash(
            [p if p.instance_id != plan.instance_id else patched for p in PLANS]
        ) != base, field


def test_criteria_hash_covers_case_and_mutant_definitions():
    """负控的期望层/期望码、以及 mutation 的承重负控清单，都算判据。"""
    definition = controls.criteria_definition(PLANS)
    mutated = dict(definition)
    assertion = {k: dict(v) for k, v in definition["negative_cases"].items()}
    assertion["N01"] = {**assertion["N01"], "layer": Layer.L5.value}
    mutated["negative_cases"] = assertion
    assert controls.sha256_of(mutated) != controls.sha256_of(definition)

    mutants = {k: {"patch": v["patch"], "killed_by": list(v["killed_by"])}
               for k, v in definition["mutants"].items()}
    mutants["M1_trust_claimed_metrics"]["killed_by"] = ["N03"]
    mutated2 = {**definition, "mutants": mutants}
    assert controls.sha256_of(mutated2) != controls.sha256_of(definition)


# ---------------------------------------------------------------------------
# 判决口径
# ---------------------------------------------------------------------------
def test_inconclusive_subtypes_are_accepted_only_for_an_inconclusive_preregistration():
    assert matches_expected("INCONCLUSIVE_EQUIVALENT", "INCONCLUSIVE")
    assert matches_expected("INCONCLUSIVE", "INCONCLUSIVE")
    assert matches_expected("SUPPORTED", "SUPPORTED")
    # 删失不是"不确定"的子类：它是没测出来
    assert not matches_expected("CENSORED", "INCONCLUSIVE")
    assert not matches_expected("REFUTED", "SUPPORTED")
    assert not matches_expected("INCONCLUSIVE_EQUIVALENT", "SUPPORTED")


def test_scientific_axis_folds_subtypes_without_promoting_them():
    assert scientific_axis("SUPPORTED") is ScientificVerdict.SUPPORTED
    assert scientific_axis("REFUTED") is ScientificVerdict.REFUTED
    assert scientific_axis("INCONCLUSIVE_EQUIVALENT") is ScientificVerdict.INCONCLUSIVE
    assert scientific_axis("CENSORED") is ScientificVerdict.INCONCLUSIVE


def test_undeclared_verdict_string_is_a_protocol_violation():
    inp = controls.synthetic_research()
    bad = controls._answer(inp, verdict="PROBABLY_TRUE")
    res = evaluate(bad)
    assert res.layers[Layer.L0].status is LayerStatus.FAIL
    assert "未声明的结论类型" in res.layers[Layer.L0].reason_code


# ---------------------------------------------------------------------------
# 场景身份：宿主按调用重建，R 族每个阻尼一个场景
# ---------------------------------------------------------------------------
def _scene_switch(inp: EvaluationInput, scenes: dict[str, str]) -> EvaluationInput:
    return dataclasses.replace(inp, scene_for=lambda call: scenes[call["run_id"]])


def test_per_call_scene_expectation_accepts_one_scene_per_damping_level():
    base = controls.synthetic_research()
    scenes = {c["run_id"]: str(c["params"]["damping"]) for c in base.calls}
    calls = [{**c, "scene_sha256": scenes[c["run_id"]]} for c in base.calls]
    res = evaluate(dataclasses.replace(base, calls=calls,
                                       scene_for=lambda call: scenes[call["run_id"]]))
    assert res.layers[Layer.L2].status is LayerStatus.PASS
    assert len(res.layers[Layer.L2].evidence) == 2       # 两个阻尼两个场景


def test_per_call_scene_expectation_catches_a_recorded_scene_swap():
    base = controls.synthetic_research()
    want = {c["run_id"]: f"{c['params']['damping']}" for c in base.calls}
    swapped = [{**c, "scene_sha256": "f" * 64} if i == 0 else c
               for i, c in enumerate(base.calls)]
    res = evaluate(_scene_switch(dataclasses.replace(base, calls=swapped), want))
    assert res.layers[Layer.L2].status is LayerStatus.FAIL
    assert res.layers[Layer.L2].reason_code == "SCENE_MISMATCH"


def test_slot_scene_constant_defaults_to_the_task_book_hash():
    """不给 scene_sha256 时退回任务书的公开哈希（E/D 的默认路径）。"""
    inp = controls.synthetic_execute()
    res = evaluate(dataclasses.replace(inp, scene_sha256=None))
    assert res.layers[Layer.L2].status is LayerStatus.PASS


# ---------------------------------------------------------------------------
# 正控的判据（不含容器）：不通过就必须是可见的失败原因
# ---------------------------------------------------------------------------
def test_positive_reference_answers_are_the_honest_self_reports():
    """参考交卷必须只含合同允许的字段，且 R 族把八项判据与适用范围写满。"""
    e_answer = controls.synthetic_execute().answer
    assert set(e_answer) == {"family", "run_id", "claim"}

    r_answer = controls.synthetic_research().answer
    assert set(r_answer["criteria"]) == set(controls.RESEARCH_CRITERIA)
    assert len(r_answer["scope"].strip()) >= 8
    assert r_answer["diagnostics"]


# ---------------------------------------------------------------------------
# AC19：run_id 与事件绑定，只有重放没有原实验事件必拒
# ---------------------------------------------------------------------------
def test_an_answer_without_any_original_event_is_rejected_even_if_replay_works():
    """最严的一条：重放是**宿主**做的、也确实跑出了正确序列，但候选引用的事件
    根本不在调用记录里——这种"只有重放"的表述必须拒（AC19）。"""
    inp = controls.synthetic_execute()
    replay = inp.replay
    empty = dataclasses.replace(inp, calls=[])
    res = evaluate(empty)
    assert replay({"params": {"steps": 10}})["events_sha256"] == "ev1"
    assert res.layers[Layer.L0].status is LayerStatus.FAIL
    assert "run_id" in res.layers[Layer.L0].reason_code


def test_candidate_cannot_supply_or_route_a_run_id():
    from orchestrator.rsi_r0.trusted import CandidateRequestError, ExperimentSession

    sess = ExperimentSession(kind="push_control", image_digest="sha256:" + "0" * 64,
                             scene_builder=lambda _p: "<mujoco/>", max_steps=100)
    for key in ("run_id", "events_sha256", "series", "scene_sha256"):
        with pytest.raises(CandidateRequestError):
            sess.run({key: "mine", "steps": 10})
    assert sess.calls == []


def test_answer_schema_has_no_field_that_could_carry_events_or_a_replay():
    """候选交卷的结构里没有任何字段能塞进序列/事件哈希/重放函数。"""
    from orchestrator.rsi_r0.contracts import Family, answer_schema
    from orchestrator.rsi_r0.evaluator import ANSWER_EXTRA_ALLOWANCE
    forbidden = {"series", "events", "events_sha256", "replay", "scene_xml",
                 "scene_sha256", "force_scale", "fault", "private"}
    for family in Family:
        schema = answer_schema(family)
        fields = set(schema["required"]) | set(ANSWER_EXTRA_ALLOWANCE)
        assert not (fields & forbidden), (family, fields & forbidden)


def test_requesting_more_steps_than_the_instance_cap_is_rejected():
    """原先挂在 N08 上的"步数超上限"改由用例覆盖（N08 换成真正的场景篡改）。"""
    base = controls.synthetic_execute()
    call = {**base.calls[0], "params": {**base.calls[0]["params"], "steps": 9999},
            "steps_run": 9999}
    res = evaluate(controls._calls(base, [call]))
    assert res.layers[Layer.L3].status is LayerStatus.FAIL
    assert res.layers[Layer.L3].reason_code == "STEPS_MISMATCH"


# ---------------------------------------------------------------------------
# AC20：R 八项判据逐项正反例 + INCONCLUSIVE 防滥用
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", controls.RESEARCH_CRITERIA)
def test_each_of_the_eight_research_criteria_is_load_bearing(missing):
    full = controls.synthetic_research()
    assert evaluate(full).layers[Layer.L5].status is LayerStatus.PASS

    crippled = {c: "evidence" for c in controls.RESEARCH_CRITERIA if c != missing}
    res = evaluate(controls._answer(full, criteria=crippled))
    assert res.layers[Layer.L0].status is LayerStatus.FAIL
    assert controls.RESEARCH_CRITERIA and missing in res.layers[Layer.L0].reason_code


def test_claiming_equivalence_when_the_effect_is_huge_is_rejected():
    """把大效应用"等效/无差异"糊过去——必须失败（INCONCLUSIVE 防滥用）。"""
    inp = controls.synthetic_research(lo=2000, hi=1000)          # 真实效应 50%
    abuse = controls._answer(inp, verdict="INCONCLUSIVE_EQUIVALENT", effect=0.005,
                             diagnostics=["看起来差不多"])
    res = evaluate(abuse)
    assert res.layers[Layer.L5].status is LayerStatus.FAIL
    assert res.scientific is ScientificVerdict.INCONCLUSIVE


def test_claiming_support_when_the_effect_is_inside_the_equivalence_bound_is_rejected():
    """把落在等效界内的微小效应说成"支持"——同样必须失败。"""
    inp = controls.synthetic_research(lo=2000, hi=1990,
                                      expected="INCONCLUSIVE")   # 真实效应 0.5%
    abuse = controls._answer(inp, verdict="SUPPORTED", effect=0.5, diagnostics=[])
    res = evaluate(abuse)
    assert res.layers[Layer.L5].status is LayerStatus.FAIL


def test_honest_equivalence_passes_both_layers():
    inp = controls.synthetic_research(lo=2000, hi=1990, expected="INCONCLUSIVE")
    got = evaluate(controls._answer(inp, verdict="INCONCLUSIVE_EQUIVALENT",
                                    effect=0.005))
    assert got.layers[Layer.L4].status is LayerStatus.PASS
    assert got.layers[Layer.L5].status is LayerStatus.PASS
    assert got.execution.value == "COMPLETE"
    assert got.scientific is ScientificVerdict.INCONCLUSIVE
