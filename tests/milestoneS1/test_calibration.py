"""S1-2 校准门五门：各一正一负（负控=改坏参数必须被拒）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[2] / "scripts" / "s1" / "calibrate_studies.py"


@pytest.fixture(scope="module")
def cal():
    spec = importlib.util.spec_from_file_location("s1_calibration", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sensitivity_gate_positive_and_negative(cal):
    assert cal.gate_sensitivity(0.0405)["ok"] is True
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_sensitivity(0.0005)
    assert excinfo.value.code == "INDISTINGUISHABLE"


def test_sham_gate_positive_and_negative(cal):
    assert cal.gate_sham(-0.0005, 0.0009)["ok"] is True
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_sham(-0.002, 0.0005)
    assert excinfo.value.code == "EQUIVALENCE_BREACH"
    with pytest.raises(cal.GateFailure):
        cal.gate_sham(-0.0005, 0.0015)


def test_settle_gate_positive_and_negative(cal):
    assert cal.gate_settle(0.1065, 0.1055, 0.02050, 0.02052)["ok"] is True
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_settle(0.1065, 0.1500, 0.0205, 0.0205)
    assert excinfo.value.code == "SETTLE_DRIFT"
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_settle(0.1065, 0.1055, 0.0205, 0.0400)
    assert excinfo.value.code == "EFFECT_DRIFT"


def test_shape_gate_positive_and_negative(cal):
    assert cal.gate_shape(5, 32, 160)["ok"] is True
    assert cal.gate_shape(4, 32, 128)["ok"] is True
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_shape(8, 32, 256)  # 自洽但超确认批上限（R1.1 形状墙）
    assert excinfo.value.code == "RESOURCE_EXCEEDED"
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_shape(5, 33, 165)
    assert excinfo.value.code == "BLOCK_MISMATCH"
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_shape(5, 32, 159)
    assert excinfo.value.code == "RUN_SHAPE"


def test_determinism_gate_positive_and_negative(cal):
    doc = {"run_id": "r1", "time_s": [0.001, 0.002],
           "qpos": [[0.0, 0, 0], [0.1, 0, 0]], "qvel": [[0.4, 0, 0], [0.39, 0, 0]]}
    ok = cal.gate_determinism(doc, doc, tol=0.0)
    assert ok["diffs"] == 0
    bad = {**doc, "qpos": [[0.0, 0, 0], [0.1000001, 0, 0]]}
    with pytest.raises(cal.GateFailure) as excinfo:
        cal.gate_determinism(doc, bad)
    assert excinfo.value.code == "NONDETERMINISTIC"
