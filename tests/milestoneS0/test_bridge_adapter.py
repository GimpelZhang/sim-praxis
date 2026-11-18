"""S0-6a：适配协议四方法 + receipt 带 sha + evolve 只暴露 ParameterSpace。"""
from __future__ import annotations

import pytest

from orchestrator.hk.params import FIELDS
from orchestrator.hk.skills import SkillEngineNotWired
from orchestrator.rsi_bridge.adapter import TASK_SCHEMA, AdapterError, SimPraxisTaskAdapter


def _adapter(**kwargs):
    calls = []

    def runner(batch):
        calls.append(batch)
        return {"runs": [{"block_id": "b00", "value": 0.1}], "batch": batch}

    return SimPraxisTaskAdapter(task_id="s0_dev", runner=kwargs.pop("runner", runner),
                                checker=kwargs.pop("checker", lambda _: True), **kwargs), calls


def test_descriptor_is_physicalrsi_task_v1():
    adapter, _ = _adapter()
    descriptor = adapter.descriptor()
    assert descriptor["schema"] == TASK_SCHEMA == "physicalrsi.task/v1"
    for method in ("check", "run", "evolve", "status"):
        assert method in descriptor["methods"]
        assert callable(getattr(adapter, method))
    assert adapter.entrypoint("other").task_id == "other"


def test_check_returns_public_verdict_only():
    adapter, _ = _adapter(checker=lambda ctx: ctx["task_id"] == "s0_dev")
    assert adapter.check() == {"task_id": "s0_dev", "verdict": "pass"}
    failing, _ = _adapter(checker=lambda _: False)
    assert failing.check()["verdict"] == "fail"
    broken, _ = _adapter(checker=lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    assert broken.check()["verdict"] == "error"


def test_run_returns_receipt_with_sha_and_status_tracks_run_idx():
    adapter, calls = _adapter()
    out = adapter.run({"n": 2})
    receipt = out["receipt"]
    assert receipt["schema"] == "physicalrsi.experiment-receipt/v2"
    assert len(receipt["sha256"]) == 64
    assert receipt["run_idx"] == 1
    assert calls == [{"n": 2}]
    second = adapter.run({})["receipt"]
    assert second["run_idx"] == 2
    status = adapter.status()
    assert [run["state"] for run in status["runs"]] == ["ok", "ok"]
    assert [run["run_idx"] for run in status["runs"]] == [1, 2]


def test_run_failure_is_recorded_then_reraised():
    def runner(_):
        raise RuntimeError("sim crashed")

    adapter, _ = _adapter(runner=runner)
    with pytest.raises(RuntimeError):
        adapter.run({"n": 1})
    assert adapter.status()["runs"][0]["state"] == "fail"


def test_evolve_exposes_only_parameter_space_fields():
    adapter, _ = _adapter()
    surface = adapter.evolve()
    assert surface["fields"] == sorted(FIELDS)
    assert surface["space_identity"]["fields"]
    serialized = str(surface)
    for forbidden in ("delta", "alpha", "confidence"):
        assert forbidden not in serialized


def test_unwired_runner_and_checker_fail_honestly():
    bare = SimPraxisTaskAdapter(task_id="t")
    with pytest.raises(SkillEngineNotWired):
        bare.run({})
    with pytest.raises(SkillEngineNotWired):
        bare.check()
    with pytest.raises(AdapterError):
        bare._assert_state("exploded")
