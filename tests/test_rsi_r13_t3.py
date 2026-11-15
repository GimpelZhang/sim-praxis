"""RSI-R1.3 T3：候选诊断与研究报告来源/引用（AC19–AC25）。"""

from __future__ import annotations

import hashlib
import json

import pytest

from orchestrator.rsi_r1 import diagnostics as D
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import task_specs as TS


def _task():
    return TS.build_new_a()


def _raw():
    # 两个 block、五个条件；交互项 = 0.055
    base = {"hi_base": 1.0, "hi_low_mu": 1.1, "lo_base": 0.5,
            "lo_low_mu": 0.55, "sham": 1.0}
    return {f"b{i:02d}": dict(base) for i in range(2)}


def _output(task, raw, **over):
    est = task.primary.estimate(raw)
    blocks = task.primary.block_effects(raw)
    out = {
        "schema": D.DIAG_SCHEMA,
        "schema_version": 1,
        "phase": "confirm",
        "study_id": "A",
        "task_id": task.task_id,
        "input_sha256": "i" * 64,
        "source_sha256": "s" * 64,
        "contrasts": [{
            "contrast_id": task.primary.id,
            "effect": est["effect"], "lower": est["lower"],
            "upper": est["upper"], "n_blocks": est["n_blocks"],
            "block_effects": [{"block_id": b, "effect": e}
                              for b, e in sorted(blocks.items())],
        }],
        "coverage": {"blocks": sorted(raw), "run_count": 10,
                     "unique_design_count": 10, "independent_unit_count": 2},
        "summaries": [],
        "threshold_counts": [{
            "contrast_id": task.primary.id,
            "above_delta": 2, "below_minus_delta": 0, "within": 0,
        }],
        "phase_comparability": {
            "explore_target": "explore grid", "confirm_target": "32 blocks",
            "relation": "different_target",
            "rationale": "explore 是设计空间扫描，confirm 是宿主 block 抽样"},
    }
    out.update(over)
    return out


def test_valid_diagnostics_recomputed():
    task, raw = _task(), _raw()
    verdict = D.validate_diagnostics(
        _output(task, raw), task=task, raw=raw, expected_phase="confirm",
        expected_input_sha256="i" * 64, expected_source_sha256="s" * 64,
        design_counts={"run_count": 10, "unique_design_count": 10,
                       "independent_unit_count": 2})
    assert verdict.ok
    assert verdict.codes == ("DIAGNOSTICS_RECOMPUTED",)


def test_host_filled_wrong_values_rejected():
    task, raw = _task(), _raw()
    bad = _output(task, raw)
    bad["contrasts"][0]["effect"] = 9.99
    with pytest.raises(D.DiagnosticError) as exc:
        D.validate_diagnostics(bad, task=task, raw=raw, expected_phase="confirm",
                               expected_input_sha256="i" * 64,
                               expected_source_sha256="s" * 64)
    assert exc.value.code == "DIAGNOSTIC_MISMATCH"


def test_missing_block_is_not_dropped():
    task, raw = _task(), _raw()
    bad = _output(task, raw)
    bad["coverage"]["blocks"] = ["b00"]
    with pytest.raises(D.DiagnosticError) as exc:
        D.validate_diagnostics(bad, task=task, raw=raw, expected_phase="confirm",
                               expected_input_sha256="i" * 64,
                               expected_source_sha256="s" * 64)
    assert exc.value.code == "COVERAGE"


def test_duplicate_and_unregistered_contrast_rejected():
    task, raw = _task(), _raw()
    bad = _output(task, raw)
    row = dict(bad["contrasts"][0])
    bad["contrasts"] = [row, dict(row)]
    with pytest.raises(D.DiagnosticError) as exc:
        D.validate_diagnostics(bad, task=task, raw=raw, expected_phase="confirm",
                               expected_input_sha256="i" * 64,
                               expected_source_sha256="s" * 64)
    assert exc.value.code == "DUPLICATE_CONTRAST"


def test_preregistered_group_claim_rejected_when_task_has_none():
    task, raw = _task(), _raw()
    bad = _output(task, raw)
    bad["summaries"] = [{"group_id": "g1", "metric": "displacement_x_m",
                         "n": 2, "mean": 0.1, "median": 0.1,
                         "min": 0.1, "max": 0.1, "preregistered": True}]
    with pytest.raises(D.DiagnosticError) as exc:
        D.validate_diagnostics(bad, task=task, raw=raw, expected_phase="confirm",
                               expected_input_sha256="i" * 64,
                               expected_source_sha256="s" * 64)
    assert exc.value.code == "GROUP_NOT_PREREGISTERED"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _evidence():
    doc = {"values": {"displacement_x_m": 1.0},
           "blocks": [{"block_id": "b00", "effect": 0.055}]}
    data = (json.dumps(doc, sort_keys=True) + "\n").encode()
    return {"analysis/receipt.json": data}


def _locator(evidence, pointer="/values/displacement_x_m"):
    data = evidence["analysis/receipt.json"]
    return {"kind": "json_pointer", "file": "analysis/receipt.json",
            "sha256": hashlib.sha256(data).hexdigest(), "pointer": pointer}


def _report(task, evidence):
    loc = _locator(evidence)
    return {
        "schema": RR.REPORT_SCHEMA,
        "schema_version": 1,
        "task_id": task.task_id,
        "question_and_hypotheses": "摩擦效应是否随初速度改变（2×2 交互）",
        "methods_and_assignment_responsibility": "宿主分配质量 nuisance；候选只声明",
        "primary_results": [{"contrast_id": task.primary.id,
                             "analysis_receipt_sha": "r" * 64,
                             "result_locator": loc}],
        "heterogeneity_and_scope": [{"claim": "效应仅覆盖 v0∈{0.2,0.6}",
                                     "evidence_refs": [loc],
                                     "applicability": "两个速度水平"}],
        "explore_confirm_comparison": {
            "estimands": "same interaction", "comparable": True,
            "explanation": "两阶段都用同一线性对比",
            "evidence_refs": [loc]},
        "alternative_explanations": ["接触模型差异而非摩擦效应"],
        "limitations": [{"statement": "只覆盖 box 形状",
                         "evidence_refs": [loc],
                         "consequence": "不外推到其他几何"}],
        "next_steps": [{"unresolved_question": "更高速度是否继续放大",
                        "discriminating_experiment": "增加 v0=0.9 条件",
                        "possible_outcomes": ["继续增大", "饱和"]}],
    }


def _origin(sha="a" * 64):
    return {"producer": "candidate", "reply_sha256": sha, "assisted": False}


def test_valid_report_passes_with_resolved_refs():
    task, evidence = _task(), _evidence()
    verdict = RR.validate_report(_report(task, evidence), task=task,
                                 receipts={"r" * 64: {"ok": True}},
                                 evidence=evidence,
                                 candidate_origin=_origin(),
                                 expected_reply_sha256="a" * 64)
    assert verdict.ok and verdict.resolved_refs >= 4


def test_host_filled_report_rejected():
    task, evidence = _task(), _evidence()
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(_report(task, evidence), task=task,
                           receipts={"r" * 64: {}}, evidence=evidence,
                           candidate_origin={"producer": "host",
                                             "reply_sha256": "a" * 64},
                           expected_reply_sha256="a" * 64)
    assert exc.value.code == "REPORT_NOT_CANDIDATE_ORIGIN"


def test_locator_traversal_and_bad_pointer_rejected():
    task, evidence = _task(), _evidence()
    doc = _report(task, evidence)
    doc["primary_results"][0]["result_locator"]["file"] = "../secret.json"
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(doc, task=task, receipts={"r" * 64: {}},
                           evidence=evidence, candidate_origin=_origin(),
                           expected_reply_sha256="a" * 64)
    assert exc.value.code == "LOCATOR_UNSAFE"
    doc = _report(task, evidence)
    doc["primary_results"][0]["result_locator"]["pointer"] = "/nope"
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(doc, task=task, receipts={"r" * 64: {}},
                           evidence=evidence, candidate_origin=_origin(),
                           expected_reply_sha256="a" * 64)
    assert exc.value.code == "LOCATOR_UNRESOLVED"


def test_empty_limitations_and_report_text_mismatch_rejected():
    task, evidence = _task(), _evidence()
    doc = _report(task, evidence)
    doc["limitations"] = []
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(doc, task=task, receipts={"r" * 64: {}},
                           evidence=evidence, candidate_origin=_origin(),
                           expected_reply_sha256="a" * 64)
    assert exc.value.code == "LIMITATIONS_MISSING"
    doc = _report(task, evidence)
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(doc, task=task, receipts={"r" * 64: {}},
                           evidence=evidence, candidate_origin=_origin(),
                           expected_reply_sha256="a" * 64,
                           report_text="different text")
    assert exc.value.code == "REPORT_REPRESENTATION_MISMATCH"


def test_schema_is_single_source_for_candidate():
    assert RR.public_schema()["schema"] == RR.REPORT_SCHEMA
    assert D.public_schema()["schema"] == D.DIAG_SCHEMA
    assert RR.REPORT_SCHEMA == P.RESEARCH_REPORT_SCHEMA_V13
