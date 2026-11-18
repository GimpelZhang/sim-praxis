"""S0-5：K0–K8 骨架、compose 一次通过、不兼容拒、假 actor 空壳走通。"""
from __future__ import annotations

import pytest

from orchestrator.hk import contracts as C
from orchestrator.hk._physicalrsi import (CallableSkill, CompositionTypeError,
                                         SkillDescriptor, compose_sequence,
                                         mismatches)
from orchestrator.hk.skills import (SkillEngineNotWired, build_research_loop,
                                    build_skills, default_analyzer,
                                    default_confirmer)

from ._fakes import (fake_analyzer, fake_actor, fake_batch_runner,
                     fake_confirmer, spec)


def _loop(**kwargs):
    return build_research_loop(actor=kwargs.pop("actor", fake_actor()),
                               batch_runner=kwargs.pop("batch_runner", fake_batch_runner),
                               analyzer=kwargs.pop("analyzer", fake_analyzer),
                               confirmer=kwargs.pop("confirmer", fake_confirmer),
                               **kwargs)


def test_compose_passes_once_with_nine_skills():
    loop = _loop()
    assert [node.skill for node in loop.composition.nodes] == [
        f"k{i}_" + suffix for i, suffix in enumerate(
            ["register_spec", "propose_plan", "prepare_batch", "run_batch",
             "write_analysis", "analyze", "freeze_confirm", "run_confirm", "submit_claim"])]


def test_incompatible_fake_skill_raises_composition_type_error():
    skills = build_skills(actor=fake_actor(), batch_runner=fake_batch_runner,
                          analyzer=fake_analyzer, confirmer=fake_confirmer)
    bad = CallableSkill(
        SkillDescriptor(id="bad_stage", revision="s0",
                        input=C.STUDY_PLAN,  # 应为 experiment_spec
                        output=C.STUDY_PLAN),
        lambda value, ctx: value)
    with pytest.raises(CompositionTypeError) as excinfo:
        compose_sequence("bad", skills["K0"], bad)
    assert "bad_stage" in str(excinfo.value)


def test_fake_actor_drives_k0_to_k8_with_contract_shaped_outputs():
    skills = build_skills(actor=fake_actor(), batch_runner=fake_batch_runner,
                          analyzer=fake_analyzer, confirmer=fake_confirmer)
    value = dict(spec())
    outputs = {}
    order = [f"K{i}" for i in range(9)]
    for key in order:
        value = skills[key].execute(value)
        outputs[key] = value
        assert isinstance(value, dict)
    assert set(outputs["K0"]) >= {"spec", "registered", "sha256"}
    assert outputs["K1"]["conditions"] == ["c1", "c2"]
    assert outputs["K2"]["runs"] == 8
    assert outputs["K3"]["runs"] and outputs["K3"]["batch_sha256"]
    assert outputs["K4"]["sha256"]
    assert outputs["K5"]["contrasts"]
    assert outputs["K6"]["frozen"] is True
    assert outputs["K7"]["plan_sha256"]
    assert outputs["K8"]["verdict"] in ("SUPPORTED", "REFUTED", "INCONCLUSIVE")
    # 相邻边静态兼容（compose 已校验；这里再显式断言一次，防测试空转）
    for left, right in zip(order, order[1:]):
        assert skills[left].descriptor.output == skills[right].descriptor.input


def test_sequence_execution_runs_full_chain():
    loop = _loop()
    claim = loop.execute(dict(spec()))
    assert claim["verdict"] == "INCONCLUSIVE"


def test_default_analyzer_delegates_to_r1_statistics():
    manifest = fake_batch_runner({"conditions": ["c1", "c2"], "n_block": 4})
    manifest["confirm_plan"] = {"pairs": {"c1v2": [0.1, 0.12, 0.08, 0.11]}}
    result = default_analyzer(manifest)
    contrast = result["contrasts"][0]
    assert contrast["n_blocks"] == 4
    assert contrast["ci_low"] <= contrast["estimate"] <= contrast["ci_high"]
    assert contrast["claim"] in ("SUPPORTED", "REFUTED", "INCONCLUSIVE")
    assert result["confirm_plan"]["pairs"]


def test_default_analyzer_without_confirm_plan_fails_honestly():
    manifest = fake_batch_runner({"conditions": ["c1", "c2"], "n_block": 4})
    with pytest.raises(SkillEngineNotWired) as excinfo:
        default_analyzer(manifest)
    assert "confirm_plan" in str(excinfo.value)


def test_default_confirmer_requires_frozen_plan():
    with pytest.raises(SkillEngineNotWired):
        default_confirmer({"pairs": {"c": [0.1, 0.2]}})
    confirmed = default_confirmer({"frozen": True,
                                   "pairs": {"c": [0.1, 0.2, 0.3, 0.4]}})
    assert confirmed["contrasts"][0]["n_blocks"] == 4


def test_batch_runner_not_wired_is_honest_failure():
    skills = build_skills(actor=fake_actor(), analyzer=fake_analyzer,
                          confirmer=fake_confirmer)
    value = {"registered": True, "sha256": "0" * 64, "spec": dict(spec())}
    for key in ("K1", "K2"):
        value = skills[key].execute(value)
    with pytest.raises(SkillEngineNotWired):
        skills["K3"].execute(value)
