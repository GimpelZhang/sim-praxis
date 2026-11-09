"""RSI-R0.2 调度规格测试（§6.2/§6.3，AC23/AC26）。

断言的是**顺序与分母**：dev 固定 E→D→R、validation 固定 E1 D1 R1 E2 D2 R2、
每个 trial 恰好 24 槽（缺槽拒绝而不是静默缩帽）、已开跑的 trial 永不重选。
"""
from __future__ import annotations

import pytest

from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r02 import stages as SG
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    return H.make_campaign(tmp_path)


def test_dev_specs_fixed_order_and_cap():
    specs = SG.dev_specs()
    assert [s.instance_id for s in specs] == ["E-dev", "D-dev", "R-dev"]
    assert [s.family for s in specs] == ["E", "D", "R"]
    assert all(s.phase == "dev" and s.post_cap == 24 for s in specs)
    assert all(s.instance_id in TC.DEV_IDS for s in specs)
    assert specs[0].slot_prefix == "E-dev-"


def test_validation_specs_fixed_six():
    specs = SG.validation_specs()
    assert [s.instance_id for s in specs] == list(SG.VALIDATION_ORDER)
    assert [s.family for s in specs] == ["E", "D", "R", "E", "D", "R"]
    assert all(s.phase == "validation" for s in specs)
    assert all(s.instance_id in TC.VALIDATION_IDS for s in specs)


def test_specs_for_rejects_unknown_phase():
    with pytest.raises(SG.StageError):
        SG.specs_for("pilot")


def test_slot_ids_requires_exactly_24(ctx):
    H.write_trial_slots(ctx, SG.dev_specs()[:1])
    book = ST.SlotBook(ctx)
    ids = SG.slot_ids_for(book, SG.dev_specs()[0])
    assert len(ids) == 24 and ids[0] == "E-dev-01" and ids[-1] == "E-dev-24"


def test_slot_ids_refuse_short_book(ctx):
    H.write_slots(ctx, ["E-dev-01", "E-dev-02"])
    with pytest.raises(SG.StageError):
        SG.slot_ids_for(ST.SlotBook(ctx), SG.dev_specs()[0])


def test_next_trial_skips_started(ctx):
    H.write_trial_slots(ctx, SG.dev_specs())
    store = ST.StateStore(ctx)
    assert SG.next_trial(SG.dev_specs(), store).instance_id == "E-dev"
    store.start(trial_id="E-dev", instance_id="E-dev", family="E", phase="dev",
                slot_ids=[], post_cap=24)
    assert SG.next_trial(SG.dev_specs(), store).instance_id == "D-dev"
    store.start(trial_id="D-dev", instance_id="D-dev", family="D", phase="dev",
                slot_ids=[], post_cap=24)
    store.start(trial_id="R-dev", instance_id="R-dev", family="R", phase="dev",
                slot_ids=[], post_cap=24)
    assert SG.next_trial(SG.dev_specs(), store) is None
    assert SG.pending_trials(SG.dev_specs(), store) == []


def test_next_trial_does_not_reselect_terminal(ctx):
    H.write_trial_slots(ctx, SG.dev_specs()[:1])
    store = ST.StateStore(ctx)
    st = store.start(trial_id="E-dev", instance_id="E-dev", family="E",
                     phase="dev", slot_ids=[], post_cap=24)
    store.transition(st, status=ST.TRIAL_VALID_FAIL_CAP,
                     code=ST.EV_VALID_FAIL_CAP)
    assert SG.trial_status(store, SG.dev_specs()[0]) == ST.TRIAL_VALID_FAIL_CAP
    assert SG.next_trial(SG.dev_specs(), store).instance_id == "D-dev"


def test_trial_status_missing_is_none(ctx):
    store = ST.StateStore(ctx)
    assert SG.trial_status(store, SG.dev_specs()[0]) is None
