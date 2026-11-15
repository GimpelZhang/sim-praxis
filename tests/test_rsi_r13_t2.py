"""RSI-R1.3 T2：设计声明与执行审计（AC12–AC17 零物理部分）。"""

from __future__ import annotations

import pytest

from orchestrator.rsi_r1 import design_intent as DI
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import task_specs as TS


def _task() -> TS.TaskSpecV13:
    return TS.build_new_a()


def _intent(role="distributional_exploration", relation="different_target",
            nuisance=None, contrast_ids=None):
    task = _task()
    return {
        "schema": P.DESIGN_INTENT_SCHEMA_V13,
        "schema_version": 1,
        "phase_role": role,
        "estimand": {
            "metric_id": task.estimand["metric_id"],
            "contrast_ids": contrast_ids or ["friction_x_speed"],
            "target_population": "32 host blocks",
            "aggregation": "paired_block_linear_contrast",
            "weighting": "equal_block_weight",
        },
        "nuisance": nuisance or {
            "mass_kg": {"mode": "varying", "support": ["0.2", "1.0"],
                        "assignment_owner": "host"}},
        "replication": {"unit_id": "block_id", "independent_unit": "block_id",
                        "deterministic_repeat_purpose": "repeatability"},
        "phase_comparability": {"relation": relation,
                                "rationale": "explore covers speed contrast"},
    }


def _cells(values):
    task = _task()
    cells = []
    for i, value in enumerate(values):
        cond = task.conditions[i % len(task.conditions)]
        cells.append({"condition_id": cond.id, "block_id": f"b{i:02d}",
                      "params": {"mass_kg": str(value)}})
    return cells


def test_fixed_pilot_scope_must_be_declared():
    task = _task()
    ok = DI.parse_design_intent(_intent(role="fixed_pilot",
                                        relation="different_target"),
                                profile=P.PROFILE_R13_DEF, task_spec=task)
    assert ok.phase_role == "fixed_pilot"
    with pytest.raises(DI.DesignError) as exc:
        DI.parse_design_intent(_intent(role="fixed_pilot", relation="same_target"),
                               profile=P.PROFILE_R13_DEF, task_spec=task)
    assert exc.value.code == "PILOT_SCOPE_UNDECLARED"


def test_placeholder_values_rejected_for_distributional():
    task = _task()
    intent = DI.parse_design_intent(_intent(), profile=P.PROFILE_R13_DEF,
                                    task_spec=task)
    with pytest.raises(DI.DesignError) as exc:
        DI.audit_design(intent, task=task,
                        proposed_cells=_cells(["1.0"] * 8))
    assert exc.value.code == "DESIGN_DECLARATION_INVALID"


def test_occasional_repeats_are_not_pseudo_replication():
    task = _task()
    intent = DI.parse_design_intent(_intent(), profile=P.PROFILE_R13_DEF,
                                    task_spec=task)
    audit = DI.audit_design(intent, task=task,
                            proposed_cells=_cells([0.3, 0.7, 0.7, 0.9]))
    assert audit.ok
    # 偶发相同值不被判伪重复（分布连续变量允许重复）
    assert audit.run_count == 4 and audit.independent_unit_count == 4


def test_candidate_cannot_claim_host_randomization():
    task = _task()
    with pytest.raises(DI.DesignError) as exc:
        DI.parse_design_intent(
            _intent(nuisance={"mass_kg": {"mode": "varying",
                                          "support": ["0.2", "1.0"],
                                          "assignment_owner": "candidate"}}),
            profile=P.PROFILE_R13_DEF, task_spec=task)
    assert exc.value.code == "ASSIGNMENT_OWNERSHIP_CLAIM"


def test_missing_nuisance_declaration_rejected():
    task = _task()
    doc = _intent()
    doc["nuisance"] = {}
    with pytest.raises(DI.DesignError) as exc:
        DI.parse_design_intent(doc, profile=P.PROFILE_R13_DEF, task_spec=task)
    assert exc.value.code == "NUISANCE_DECLARATION_MISSING"


def test_post_audit_requires_host_verified_manifest():
    task = _task()
    intent = DI.parse_design_intent(_intent(), profile=P.PROFILE_R13_DEF,
                                    task_spec=task)
    cells = _cells([0.3, 0.7])
    actual = [dict(c, params={"mass_kg": c["params"]["mass_kg"]}) for c in cells]
    with pytest.raises(DI.DesignError) as exc:
        DI.audit_design(intent, task=task, proposed_cells=cells,
                        actual_manifest=actual)
    assert exc.value.code == "EXECUTION_UNKNOWN"


def test_actual_mismatch_is_instrument_error():
    task = _task()
    intent = DI.parse_design_intent(_intent(), profile=P.PROFILE_R13_DEF,
                                    task_spec=task)
    cells = _cells([0.3, 0.7])
    actual = []
    for cell in cells:
        row = dict(cell, verified_by_host=True)
        actual.append(row)
    actual[1] = dict(actual[1], params={"mass_kg": "0.9"})
    with pytest.raises(DI.DesignError) as exc:
        DI.audit_design(intent, task=task, proposed_cells=cells,
                        actual_manifest=actual)
    assert exc.value.code == "INSTRUMENT_MISMATCH"


def test_post_audit_counts_and_success():
    task = _task()
    intent = DI.parse_design_intent(_intent(), profile=P.PROFILE_R13_DEF,
                                    task_spec=task)
    cells = _cells([0.3, 0.7])
    actual = [dict(c, verified_by_host=True) for c in cells]
    audit = DI.audit_design(intent, task=task, proposed_cells=cells,
                            actual_manifest=actual)
    assert audit.ok and audit.actual_verified
    assert audit.run_count == 2 == audit.independent_unit_count


def test_deterministic_repeats_do_not_expand_independent_units():
    doc = DI.counts_document(run_count=4, unique_design_count=2,
                             independent_unit_count=2)
    assert doc["independent_unit_count"] == 2
    with pytest.raises(DI.DesignError) as exc:
        DI.counts_document(run_count=4, unique_design_count=2,
                           independent_unit_count=4)
    assert exc.value.code == "COUNT_INVALID"


def test_phase_comparability_must_be_declared():
    task = _task()
    doc = _intent()
    doc["phase_comparability"] = {"relation": "same_target", "rationale": ""}
    with pytest.raises(DI.DesignError) as exc:
        DI.parse_design_intent(doc, profile=P.PROFILE_R13_DEF, task_spec=task)
    assert exc.value.code == "PHASE_COMPARABILITY"
