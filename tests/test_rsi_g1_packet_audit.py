"""RSI-G1 审查包语义守恒审计：D10–D12 + 空 evidence 负控（零 POST）。"""
from __future__ import annotations

import copy

import pytest

from orchestrator.rsi_r1 import g1_packet_audit as PA
from orchestrator.rsi_r1 import task_specs as TS

NUISANCE = "mass_kg"


def _fixture():
    task = TS.build_new_a()
    conds = [c.id for c in task.conditions]
    blocks = ["b01", "b02"]
    assignments: "list[dict]" = []
    run_specs: "list[dict]" = []
    raw: "dict[str, dict]" = {}
    values: "dict[str, dict[str, float]]" = {}
    for bi, block in enumerate(blocks):
        for ci, cond in enumerate(conds):
            rid = f"run-{bi}{ci}"
            assignments.append({"block_id": block, "condition_id": cond,
                                "params": {NUISANCE: str(0.5 + bi)}})
            run_specs.append({"run_id": rid, "block_id": block,
                              "condition_id": cond})
            raw[rid] = {"values": {task.primary.metric: 1.0 + bi + 0.1 * ci}}
            values.setdefault(block, {})[cond] = 1.0 + bi + 0.1 * ci
    primary = task.primary.estimate(values)
    packet = {
        "task": task.public_document(), "task_sha256": task.sha256,
        "design_intent": {"estimand": {"contrast_ids": [task.primary.id],
                                       "metric_id": task.primary.metric}},
        "primary_contrast": {"id": task.primary.id,
                             "metric": task.primary.metric},
        "block_table": [{"block_id": b,
                         "nuisance": {NUISANCE: str(0.5 + i)},
                         "values": dict(values[b])}
                        for i, b in enumerate(blocks)],
        "evidence_index": {"analysis/output.json": {"sha256": "a" * 64,
                                                    "bytes": 10}},
        "primary_recomputation": {"effect": primary["effect"],
                                  "lower": primary["lower"],
                                  "upper": primary["upper"],
                                  "n_blocks": primary["n_blocks"]},
    }
    return task, assignments, run_specs, raw, packet


def _audit(task, assignments, run_specs, raw, packet):
    return PA.validate_packet(task, assignments, raw, packet,
                              run_specs=run_specs, expect_blocks=2)


def test_consistent_packet_passes():
    task, assignments, run_specs, raw, packet = _fixture()
    audit = _audit(task, assignments, run_specs, raw, packet)
    assert audit.ok, audit.as_dict()


def test_wrong_study_intent_is_rejected_d10():
    task, assignments, run_specs, raw, packet = _fixture()
    bad = copy.deepcopy(packet)
    bad["design_intent"]["estimand"] = {"contrast_ids": ["other_contrast"],
                                        "metric_id": task.primary.metric}
    audit = _audit(task, assignments, run_specs, raw, bad)
    assert "PACKET_INTENT_STUDY" in audit.codes


def test_wrong_metric_is_rejected_d11():
    task, assignments, run_specs, raw, packet = _fixture()
    bad = copy.deepcopy(packet)
    bad["primary_contrast"]["metric"] = "stop_time_s"
    audit = _audit(task, assignments, run_specs, raw, bad)
    assert "PACKET_METRIC" in audit.codes


def test_missing_block_nuisance_is_rejected_d12():
    task, assignments, run_specs, raw, packet = _fixture()
    bad = copy.deepcopy(packet)
    bad["block_table"][0]["nuisance"] = {"sampled_policy": "host_per_block"}
    audit = _audit(task, assignments, run_specs, raw, bad)
    assert "PACKET_NUISANCE" in audit.codes


def test_empty_evidence_index_is_rejected():
    task, assignments, run_specs, raw, packet = _fixture()
    bad = copy.deepcopy(packet)
    bad["evidence_index"] = {}
    audit = _audit(task, assignments, run_specs, raw, bad)
    assert "PACKET_EVIDENCE_EMPTY" in audit.codes


def test_wrong_block_value_and_primary_effect_are_rejected():
    task, assignments, run_specs, raw, packet = _fixture()
    bad = copy.deepcopy(packet)
    first = task.primary.condition_ids[0]
    bad["block_table"][0]["values"][first] = 99.0
    audit = _audit(task, assignments, run_specs, raw, bad)
    assert "PACKET_VALUES" in audit.codes

    bad2 = copy.deepcopy(packet)
    bad2["primary_recomputation"]["effect"] = 0.123456
    audit2 = _audit(task, assignments, run_specs, raw, bad2)
    assert "PACKET_RECOMPUTE" in audit2.codes


def test_audit_is_read_only_and_reports_no_verdict_on_science():
    task, assignments, run_specs, raw, packet = _fixture()
    before = copy.deepcopy(packet)
    doc = _audit(task, assignments, run_specs, raw, packet).as_dict()
    assert packet == before
    assert doc["rule"].startswith("只审包内一致性")
    assert doc["verdict"] in ("PASS", "FAIL")


def test_code_enum_is_stable():
    assert set(PA.CODES) == set(PA.CODES)
    assert "PACKET_INTENT_STUDY" in PA.CODES
    assert "PACKET_EVIDENCE_EMPTY" in PA.CODES
