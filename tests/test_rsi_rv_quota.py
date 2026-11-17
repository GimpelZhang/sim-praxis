"""RV-17/RV-19/RV-20/RV-22：lane 帽、账期、槽不复用与全轮 aggregate 真值表。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_runtime as RT


def _permit(**kw):
    base = {"envelope_id": "round", "lock_sha256": "a" * 64,
            "budget_sha256": "b" * 64, "phase": "rv"}
    base.update(kw)
    return L.Permit(**base)


def test_lane_caps_are_the_single_source_of_truth():
    assert RV.RV_LANE_POST_CAPS == {
        "review_calibration": 4, "suffix_A": 8, "suffix_B": 8,
        "review_final": 4}
    assert sum(RV.RV_LANE_POST_CAPS.values()) == RV.RV_HARD_CAP == 24
    assert len(set(RV.RV_LANE_TASK_ID.values())) == 4
    assert RV.RV_TASK_CAPS == {0: 4, 1: 8, 2: 8, 3: 4}
    assert RV.LEDGER_RSI_RV == 2906


def test_ledger_namespace_binds_rv_phase(tmp_path):
    from orchestrator.gateway import quota_ledger as Q
    assert Q.LEDGER_RSI_RV == 2906
    assert Q.RSI_RV_HARD_CAP == 24
    assert Q.RSI_RV_TASK_CAPS == RV.RV_TASK_CAPS
    ledger = RT.rv_ledger(tmp_path / "round", "round")
    assert ledger.ns.phase == 2906
    assert ledger.ns.hard_cap == 24


def test_budget_refuses_drift():
    RT.RVBudget(envelope_id="r", posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    with pytest.raises(RV.RVError) as exc:
        RT.RVBudget(envelope_id="r",
                    posts_by_lane={**RV.RV_LANE_POST_CAPS,
                                   "suffix_A": 9})
    assert exc.value.code == "BUDGET_DRIFT"
    with pytest.raises(RV.RVError) as exc:
        RT.RVBudget(envelope_id="r", posts_by_lane={"suffix_A": 8})
    assert exc.value.code == "BUDGET_LANE_SET"


def test_slot_book_is_idempotent_and_caps_lane(tmp_path):
    budget = RT.RVBudget(envelope_id="r",
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    book = RT.RVSlotBook(RT.RVContext(tmp_path / "round"), budget)
    book.set_lane("review_final")
    permit = _permit()
    first = book.reserve_atomic(call_id="c1", permit=permit)
    again = book.reserve_atomic(call_id="c1", permit=permit)
    assert first.slot_id == again.slot_id
    assert book.count("review_final") == 1
    for index in range(2, 5):
        book.reserve_atomic(call_id=f"c{index}", permit=permit)
    with pytest.raises(L.LoopError) as exc:
        book.reserve_atomic(call_id="c5", permit=permit)
    assert exc.value.code == "BUDGET_REFUSED"


def _study(state="MODEL_REPORT_VALID", gates=None, **extra):
    base = {"source_valid": True, "actual_worker_isolated": True,
            "request_fidelity": True, "candidate_origin": True,
            "binding": True, "report_evidence": True,
            "review_verdict": True, "numeric_fidelity": True}
    if gates is not None:
        base.update(gates)
    return {"state": state, "gates": base, "problems": [], **extra}


def test_aggregate_truth_table_priority():
    ok = RV.aggregate_rv(studies={"A": _study(), "B": _study()})
    assert ok["overall"] == "MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE"
    # 1. 仪器/安全优先于一切
    blocked = RV.aggregate_rv(studies={
        "A": _study(state="PROVENANCE_UNKNOWN", send_unknown=True),
        "B": _study(security_violation=True)})
    assert blocked["overall"] == "INSTRUMENT_BLOCKED"
    # 2. 来源未知
    unknown = RV.aggregate_rv(studies={
        "A": _study(state="PROVENANCE_UNKNOWN", send_unknown=True),
        "B": _study()})
    assert unknown["overall"] == "PROVENANCE_UNKNOWN"
    # 3. 均未启动
    not_run = RV.aggregate_rv(studies={"A": _study(state="NOT_RUN"),
                                       "B": _study(state="NOT_RUN")})
    assert not_run["overall"] == "NOT_MEASURED"
    # 4. 任一合法失败
    one_fail = RV.aggregate_rv(studies={"A": _study(state="MODEL_FAILED",
                                                    gates={"binding": False}),
                                        "B": _study()})
    assert one_fail["overall"] == "MODEL_SUFFIX_NOT_ESTABLISHED"
    # 5. 门不完整不算通过
    incomplete = RV.aggregate_rv(studies={
        "A": {**_study(), "gates": {"binding": True}}, "B": _study()})
    assert incomplete["overall"] == "MODEL_SUFFIX_NOT_ESTABLISHED"
    # 6. 空集合不得靠 all() 晋升
    empty = RV.aggregate_rv(studies={})
    assert empty["overall"] == "NOT_MEASURED"


def test_aggregate_preserves_per_study_state_and_distinguishes_failures():
    verdict = RV.aggregate_rv(studies={
        "A": _study(state="MODEL_FAILED", gates={"binding": False},
                    problems=["CLAIM_INVALID"]),
        "B": _study(source_drift=True)})
    assert verdict["overall"] == "INSTRUMENT_BLOCKED"
    assert verdict["studies"]["A"]["state"] == "MODEL_FAILED"
    assert "CLAIM_INVALID" in verdict["studies"]["A"]["problems"]
    assert verdict["reasons"]


def test_slot_book_does_not_write_global_pause(tmp_path):
    budget = RT.RVBudget(envelope_id="r",
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    book = RT.RVSlotBook(RT.RVContext(tmp_path / "round"), budget)
    book.set_lane("suffix_A")
    book.resolve_or_pause(L.Reply(text="", response_id="",
                                  is_actionable=False, reason="BUDGET_REFUSED"))
    root = tmp_path / "round"
    assert not (root / "state" / "pause.json").exists()
    rows = (root / "quota" / "nonactionable.jsonl").read_text().splitlines()
    assert rows and "BUDGET_REFUSED" in rows[0]
