"""RSI-R1 T3：候选矩阵规范化、确认批分配与批执行（AC15/AC17/AC18 半边）。

全部离线：backend 是内存假实现，不触网、不启容器、不跑物理。
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S
from tests.rsi_r1_helpers import cond, doc_of, plan_doc

A_PARAMS = dict(mass_kg="0.5", v0_mps="0.4", sliding_friction="0.4",
                torsional_friction_m="0.01")
B_PARAMS = dict(inertia_ratio="1.0", target_amplitude_rad="0.2",
                target_freq_hz="1.0", tc_s="0.008", dt_s="0.001",
                controller="PD", kp="5", kd="0.1")


def _budget() -> C.BudgetSpec:
    return C.default_budget("r1_test")


def _plan(study: str = "A", **kw) -> C.ResearchPlan:
    kw.setdefault("phase", "explore")
    return doc_of(plan_doc(study, **kw))


def _cell(cid: str, block: str, params=None) -> dict:
    return {"condition_id": cid, "block_id": block,
            "params": copy.deepcopy(params or (A_PARAMS if cid != "base_b" else B_PARAMS))}


def _matrix(plan: C.ResearchPlan, cells) -> dict:
    return {"schema": E.MATRIX_SCHEMA, "schema_version": 1,
            "study_id": plan.study_id, "plan_sha": plan.sha256,
            "cells": list(cells)}


# ---------------------------------------------------------------------------
# compile_design
# ---------------------------------------------------------------------------

def test_compile_design_binds_identity_without_execution() -> None:
    contract_doc = {"schema": "x.v1", "schema_version": 1, "body": [1, 2]}
    src = hashlib.sha256(b"candidate design source").hexdigest()
    art = E.compile_design(src, contract_doc, "b" * 64)
    assert art.kind == "design"
    assert art.plan_sha256 == "b" * 64
    assert art.public_contract_sha256 == C.canonical_sha256(contract_doc)
    assert art.to_dict()["design_sha256"] == art.sha256
    # 同一输入同一 sha；hex 直传等价
    again = E.compile_design(src, art.public_contract_sha256, "b" * 64)
    assert again.sha256 == art.sha256
    with pytest.raises(E.ExperimentError) as exc:
        E.compile_design("A" * 64, contract_doc, "b" * 64)
    assert exc.value.code == "SOURCE_SHA_INVALID"


# ---------------------------------------------------------------------------
# AC15：探索矩阵逐字段验证 + host 重建
# ---------------------------------------------------------------------------

def test_validate_matrix_builds_trusted_specs_for_a() -> None:
    plan = _plan("A")
    specs = E.validate_matrix(_matrix(plan, [
        _cell("base", "b01"),
        _cell("low_mu", "b02", {**A_PARAMS, "sliding_friction": "0.15"}),
    ]), plan, _budget())
    assert len(specs) == 2
    first = specs[0]
    assert first.scene_id == S.SCENE_A
    assert first.dt_s == str(E.A_FIXED_DT_S) and first.tc_s is None
    assert first.ticks["nsteps"] == 8000
    assert set(first.physical_params) == set(A_PARAMS)
    assert first.controller_params == {}
    assert first.units["mass_kg"] == "kg"
    assert first.run_id == E.run_id_for(plan_sha256=plan.sha256, study_id="A",
                                        condition_id="base", block_id="b01")
    assert first.spec_hash == C.canonical_sha256(first.body())
    assert specs[0].run_id != specs[1].run_id


def test_validate_matrix_builds_ticks_for_b() -> None:
    plan = _plan("B")
    specs = E.validate_matrix(_matrix(plan, [_cell("tc_only", "b01", B_PARAMS)]),
                              plan, _budget())
    spec = specs[0]
    assert spec.ticks == {"dt_ticks_us": 1000, "duration_ticks_us": 4_000_000,
                          "nsteps": 4000, "tc_ticks_us": 8000,
                          "control_updates": 500}
    assert set(spec.controller_params) == {"tc_s", "dt_s", "controller", "kp", "kd"}
    assert set(spec.physical_params) == {"inertia_ratio", "target_amplitude_rad",
                                         "target_freq_hz"}
    assert spec.units["kp"] == "Nm/rad"
    assert spec.nsteps == 4000


def test_validate_matrix_rejects_candidate_reported_cost() -> None:
    plan = _plan("A")
    doc = _matrix(plan, [_cell("base", "b01")])
    doc["cells"][0]["nsteps"] = 8000          # 自报步数
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(doc, plan, _budget())
    assert exc.value.code == "UNKNOWN_KEY"

    doc = _matrix(plan, [_cell("base", "b01")])
    doc["budget"] = 999                       # 自报预算
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(doc, plan, _budget())
    assert exc.value.code == "UNKNOWN_KEY"


def test_validate_matrix_rejects_plan_and_study_drift() -> None:
    plan = _plan("A")
    doc = _matrix(plan, [_cell("base", "b01")])
    doc["plan_sha"] = "0" * 64
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(doc, plan, _budget())
    assert exc.value.code == "PLAN_MISMATCH"

    doc = _matrix(plan, [_cell("base", "b01")])
    doc["study_id"] = "B"
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(doc, plan, _budget())
    assert exc.value.code == "STUDY_MISMATCH"


def test_validate_matrix_rejects_unknown_condition_and_duplicates() -> None:
    plan = _plan("A")
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(_matrix(plan, [_cell("nope", "b01")]), plan, _budget())
    assert exc.value.code == "CONDITION_UNKNOWN"

    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(_matrix(plan, [_cell("base", "b01"),
                                         _cell("base", "b01")]), plan, _budget())
    assert exc.value.code == "DUPLICATE_ID"


def test_validate_matrix_rejects_bad_params_wholesale() -> None:
    plan = _plan("A")
    cases = [
        ({**A_PARAMS, "mass_kg": 0.5}, "FLOAT_INPUT"),
        ({**A_PARAMS, "mass_kg": "9.0"}, "DOMAIN"),
        ({**A_PARAMS, "unknown_p": "1"}, "UNKNOWN_PARAM"),
        ({k: v for k, v in A_PARAMS.items() if k != "v0_mps"}, "MISSING_PARAM"),
        ({**A_PARAMS, "mass_kg": "nan"}, "NONFINITE"),
    ]
    for params, code in cases:
        with pytest.raises(E.ExperimentError) as exc:
            E.validate_matrix(_matrix(plan, [_cell("base", "b01", params)]),
                              plan, _budget())
        assert exc.value.code == code, f"{params} 期望 {code}，实为 {exc.value.code}"

    plan_b = _plan("B")
    bad_p = {**B_PARAMS, "kd": "0.5"}          # P 控制 kd 必须 0
    bad_p["controller"] = "P"
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(_matrix(plan_b, [_cell("base", "b01", bad_p)]),
                          plan_b, _budget())
    assert exc.value.code == "DOMAIN"


def test_validate_matrix_enforces_explore_run_cap() -> None:
    plan = _plan("A")
    cells = [_cell("base", f"b{i:03d}") for i in range(1, 66)]  # 65 > 64
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(_matrix(plan, cells), plan, _budget())
    assert exc.value.code == "RESOURCE_EXCEEDED"


def test_validate_matrix_accepts_json_text_and_rejects_malformed() -> None:
    plan = _plan("A")
    text = json.dumps(_matrix(plan, [_cell("base", "b01")]))
    assert len(E.validate_matrix(text, plan, _budget())) == 1
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix("{not json", plan, _budget())
    assert exc.value.code == "MALFORMED_JSON"
    with pytest.raises(E.ExperimentError) as exc:
        E.validate_matrix(_matrix(plan, []), plan, _budget())
    assert exc.value.code == "WRONG_TYPE"


# ---------------------------------------------------------------------------
# AC18：确认批由 host 分配、单批、32 block、≤6 条件
# ---------------------------------------------------------------------------

def test_allocate_confirm_a_covers_32_blocks_per_condition() -> None:
    plan = doc_of(plan_doc("A"))               # confirm
    seed = "c" * 64
    manifest = E.allocate_confirm(plan, seed)
    assert manifest.block_count == R.CONFIRM_BLOCKS
    assert manifest.n_assignments == len(plan.conditions) * R.CONFIRM_BLOCKS
    assert [a.block_id for a in manifest.assignments[:32]] == \
        [f"b{i:02d}" for i in range(1, 33)]
    assert {a.condition_id for a in manifest.assignments} == \
        {c.id for c in plan.conditions}
    for a in manifest.assignments:
        assert set(a.params) == set(A_PARAMS)
        value = float(a.params["mass_kg"])
        assert 0.2 <= value <= 1.0
    # 非干预物理量的实际采样宽度必须达到 20%
    masses = [float(a.params["mass_kg"]) for a in manifest.assignments]
    assert max(masses) - min(masses) >= 0.20 * (1.0 - 0.2) * 0.99
    assert len(set(masses)) > 25, "32 个 block 不应塌成少数点"


def test_allocate_confirm_is_deterministic_and_seed_bound() -> None:
    plan = doc_of(plan_doc("B"))
    m1 = E.allocate_confirm(plan, "d" * 64)
    m2 = E.allocate_confirm(plan, "d" * 64)
    m3 = E.allocate_confirm(plan, "e" * 64)
    assert m1.sha256 == m2.sha256
    assert m1.sha256 != m3.sha256
    # manifest 不含原始 seed 字节，只含其摘要
    assert m1.keeper_seed_sha256 == hashlib.sha256(("d" * 64).encode()).hexdigest()
    assert "d" * 64 not in json.dumps(m1.to_dict())
    for a in m1.assignments:
        assert 0.5 <= float(a.params["inertia_ratio"]) <= 2.0


def test_allocate_confirm_requires_confirm_plan_and_32_blocks() -> None:
    explore = _plan("A")
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(explore, "c" * 64)
    assert exc.value.code == "PLAN_PHASE_INVALID"

    plan16 = doc_of(plan_doc("A", block_count=16))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(plan16, "c" * 64)
    assert exc.value.code == "CONFIRM_BLOCK_COUNT"

    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(doc_of(plan_doc("A")), "short")
    assert exc.value.code == "SEED_INVALID"


def test_allocate_confirm_rejects_degenerate_or_unsampleable_varying() -> None:
    narrow = doc_of(plan_doc("A", varying={"mass_kg": ["0.2", "0.25"]}))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(narrow, "c" * 64)
    assert exc.value.code == "DISTRIBUTION_DEGENERATE"

    outside = doc_of(plan_doc("A", varying={"mass_kg": ["0.1", "1.0"]}))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(outside, "c" * 64)
    assert exc.value.code == "DISTRIBUTION"

    enum = doc_of(plan_doc("B", varying={"tc_s": ["0.004", "0.008"]}))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(enum, "c" * 64)
    assert exc.value.code == "ENUM_NOT_SAMPLEABLE"

    point = doc_of(plan_doc("A", varying={"mass_kg": ["0.5", "0.5"]}))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(point, "c" * 64)
    assert exc.value.code == "DISTRIBUTION_DEGENERATE"


def test_allocate_confirm_caps_conditions() -> None:
    conds = [cond(f"c{i}", S.SCENE_A, **A_PARAMS) for i in range(7)]
    plan = doc_of(plan_doc("A", conditions=conds))
    with pytest.raises(E.ExperimentError) as exc:
        E.allocate_confirm(plan, "c" * 64)
    assert exc.value.code == "MAX_CONDITIONS"


# ---------------------------------------------------------------------------
# execute_batch：资源账 + 失败语义（AC17/AC18 半边）
# ---------------------------------------------------------------------------

class _Backend:
    def __init__(self, fail_at: "int | None" = None, *, kind: str = "infra",
                 stop_at: "int | None" = None):
        self.calls: "list[E.ExperimentSpec]" = []
        self.fail_at = fail_at
        self.kind = kind
        self.stop_at = stop_at

    def run(self, spec: E.ExperimentSpec):
        self.calls.append(spec)
        index = len(self.calls)
        if self.fail_at == index:
            if self.kind == "infra":
                raise E.BackendInfraError("容器退出码 137")
            raise E.BackendScientificStop("关节失稳", record={"run_id": spec.run_id,
                                                             "instability": True})
        if self.stop_at == index:
            raise E.BackendScientificStop("关节失稳", record={"run_id": spec.run_id,
                                                             "instability": True})
        return {"run_id": spec.run_id, "nsteps": spec.nsteps, "sim_done": True}


def _three_specs() -> "tuple[E.ExperimentSpec, ...]":
    plan = _plan("A")
    return E.validate_matrix(_matrix(plan, [
        _cell("base", "b01"), _cell("low_mu", "b02"), _cell("heavy", "b03"),
    ]), plan, _budget())


def test_execute_batch_runs_every_spec_and_charges_resources() -> None:
    specs = _three_specs()
    book = E.ResourceBook(max_runs=10, max_steps=10 ** 6)
    backend = _Backend()
    ev = E.execute_batch(specs, backend, book)
    assert not ev.blocked and ev.n_runs == 3 == ev.n_ok
    assert [c.run_id for c in backend.calls] == [s.run_id for s in specs]
    assert book.used_runs == 3 and book.used_steps == 3 * 8000
    assert ev.outcomes[0].record["sim_done"] is True
    assert ev.to_dict()["resources"]["used_runs"] == 3


def test_execute_batch_rejects_tampered_spec_before_any_run() -> None:
    specs = list(_three_specs())
    specs[1] = dataclasses.replace(specs[1], spec_hash="0" * 64)
    backend, book = _Backend(), E.ResourceBook(max_runs=10, max_steps=10 ** 6)
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch(specs, backend, book)
    assert exc.value.code == "SPEC_TAMPERED"
    assert backend.calls == [] and book.used_runs == 0

    fake_run_id = dataclasses.replace(specs[0], run_id="run-forged")
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch([fake_run_id], backend, book)
    assert exc.value.code == "SPEC_TAMPERED"


def test_execute_batch_infra_failure_blocks_without_padding() -> None:
    specs = _three_specs()
    book = E.ResourceBook(max_runs=10, max_steps=10 ** 6)
    backend = _Backend(fail_at=2)
    ev = E.execute_batch(specs, backend, book)
    assert ev.blocked and "INFRA_FAILURE" in (ev.reason or "")
    assert ev.n_runs == 2, "第三个 spec 不得执行（不补样本）"
    assert ev.outcomes[-1].ok is False and ev.outcomes[-1].error_code == "INFRA_FAILURE"
    assert len(backend.calls) == 2 and book.used_runs == 2


def test_execute_batch_scientific_stop_is_valid_data_and_continues() -> None:
    specs = _three_specs()
    book = E.ResourceBook(max_runs=10, max_steps=10 ** 6)
    backend = _Backend(stop_at=2)
    ev = E.execute_batch(specs, backend, book)
    assert not ev.blocked and ev.n_runs == 3
    stopped = ev.outcomes[1]
    assert stopped.ok is True and stopped.stop_reason and stopped.record
    assert stopped.record["instability"] is True
    assert ev.outcomes[2].ok is True


def test_execute_batch_duplicates_and_cross_study_are_rejected() -> None:
    specs = _three_specs()
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch([specs[0], specs[0]], _Backend(),
                        E.ResourceBook(max_runs=10, max_steps=10 ** 6))
    assert exc.value.code == "DUPLICATE_ID"

    plan_b = _plan("B")
    spec_b = E.validate_matrix(_matrix(plan_b, [_cell("base", "b01", B_PARAMS)]),
                               plan_b, _budget())[0]
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch([specs[0], spec_b], _Backend(),
                        E.ResourceBook(max_runs=10, max_steps=10 ** 6))
    assert exc.value.code == "STUDY_MISMATCH"

    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch([], _Backend(), E.ResourceBook(max_runs=1, max_steps=1))
    assert exc.value.code == "WRONG_TYPE"


def test_execute_batch_stops_on_exhausted_resource_book() -> None:
    specs = _three_specs()
    backend = _Backend()
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch(specs, backend,
                        E.ResourceBook(max_runs=1, max_steps=10 ** 6))
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert len(backend.calls) == 1

    backend = _Backend()
    with pytest.raises(E.ExperimentError) as exc:
        E.execute_batch(specs, backend,
                        E.ResourceBook(max_runs=10, max_steps=8000))
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert len(backend.calls) == 1


def test_backend_returning_non_mapping_is_evidence_not_silence() -> None:
    specs = _three_specs()

    class _Bad:
        def run(self, spec):
            return 42

    ev = E.execute_batch(specs, _Bad(), E.ResourceBook(max_runs=10,
                                                       max_steps=10 ** 6))
    assert ev.blocked and ev.n_runs == 1
    assert ev.outcomes[0].error_code == "BACKEND_ERROR"


def test_spec_has_only_host_fields() -> None:
    spec = _three_specs()[0]
    expected = {"schema", "schema_version", "study_id", "plan_sha256",
                "condition_id", "block_id", "scene_id", "physical_params",
                "controller_params", "dt_s", "tc_s", "duration_s", "units",
                "ticks", "run_id", "spec_hash"}
    assert set(spec.to_dict()) == expected
    assert "cost" not in spec.to_dict() and "nsteps" not in spec.to_dict()
    assert spec.ticks["nsteps"] == 8000
