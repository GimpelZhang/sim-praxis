"""S2c-2 design_runner 单测（假 actor，零付费零物理）。"""
import pytest

from orchestrator.hk.design_space import DomainBrief
from orchestrator.hk.design_runner import run_design

_BRIEF = DomainBrief(
    brief_id="t_brief", question="x 与 y 的交互强度？",
    competing_explanations=({"id": "e_weak", "desc": "弱交互"}, {"id": "e_strong", "desc": "强交互"}),
    factor_axes=({"name": "x", "values": ["lo", "hi"], "scene_param": "x_p"},
                 {"name": "y", "values": ["lo2", "hi2"], "scene_param": "y_p"}),
    metrics=("m",), scene_id="S",
    constraints={"max_conditions": 6}, pilot_quota={"max_iterations": 3, "runs_per_iteration": 16},
)


def _good_design():
    return {"conditions": [
        {"id": "c1", "factors": {"x": "hi", "y": "hi2"}},
        {"id": "c2", "factors": {"x": "hi", "y": "lo2"}},
        {"id": "c3", "factors": {"x": "lo", "y": "hi2"}},
        {"id": "c4", "factors": {"x": "lo", "y": "lo2"}}],
        "primary_contrast": {"id": "axb", "treatment_high": "c1", "treatment_low": "c3",
                             "control_high": "c2", "control_low": "c4", "proposition": "increase_gt"},
        "negative_control": {"base": "c1", "sham": "c3"}, "metric": "m"}


class _SeqActor:
    """按调用顺序返回预设设计，并记录触点调用。"""
    def __init__(self, designs):
        self._designs = list(designs); self.calls = []
    def __call__(self, name, contract, value):
        self.calls.append((name, dict(value)))
        return self._designs.pop(0)


def test_valid_first_try_ready():
    a = _SeqActor([_good_design()])
    r = run_design(_BRIEF, a, pilot_effect=lambda d: 0.5,
                   discriminability=lambda d: {"min_pair_diff": 0.01, "variants": ["w"]})
    assert r.status == "DESIGN_READY"
    assert r["repairs"] == 0
    assert r["spec"]["plan_sha256"]
    assert r["spec"]["scene_id"] == "S"
    assert len(r["gate_reports"]) == 1
    assert a.calls[0][0] == "propose_design"


def test_invalid_then_repair_ready():
    bad = _good_design(); bad["conditions"][0]["factors"]["x"] = "OUT"  # 界外
    a = _SeqActor([bad, _good_design()])
    r = run_design(_BRIEF, a, pilot_effect=lambda d: 0.5,
                   discriminability=lambda d: {"min_pair_diff": 0.01})
    assert r.status == "DESIGN_READY"
    assert r["repairs"] == 1
    assert r["gate_reports"][0]["code"] == "DESIGN_OUT_OF_SPACE"
    assert a.calls[1][0] == "propose_design_repair"
    assert "gate_report" in a.calls[1][1]


def test_always_invalid_not_ready_one_round_limit():
    bad = _good_design(); bad["conditions"][0]["factors"]["x"] = "OUT"
    a = _SeqActor([bad, bad, bad])  # 1 提议 + 2 修复
    r = run_design(_BRIEF, a, pilot_effect=lambda d: 0.5,
                   discriminability=lambda d: {"min_pair_diff": 0.01}, max_repair=2)
    assert r.status == "DESIGN_NOT_READY"
    assert r["repairs"] == 2
    assert len(r["gate_reports"]) == 3


def test_pilot_weak_rejected():
    a = _SeqActor([_good_design(), _good_design(), _good_design()])
    r = run_design(_BRIEF, a, pilot_effect=lambda d: 0.0001, delta=0.001)
    assert r.status == "DESIGN_NOT_READY"
    assert r["gate_reports"][0]["code"] == "DESIGN_PILOT_WEAK"


def test_non_discriminative_rejected_and_private_carries_worlds():
    a = _SeqActor([_good_design(), _good_design(), _good_design()])
    r = run_design(_BRIEF, a, pilot_effect=lambda d: 0.5,
                   discriminability=lambda d: {"min_pair_diff": 0.0, "variants": ["v_weak", "v_strong"]})
    assert r.status == "DESIGN_NOT_READY"
    assert r["gate_reports"][0]["code"] == "DESIGN_NON_DISCRIMINATIVE"
    # 世界变体只进 private（审查者通道），public 不得含 variants 数值
    assert r["gate_reports"][0]["private"]["variants"] == ["v_weak", "v_strong"]
    assert "variants" not in r["gate_reports"][0]["public"]


def test_compile_error_when_nc_missing():
    d = _good_design(); d.pop("negative_control")
    a = _SeqActor([d])
    r = run_design(_BRIEF, a)  # 无回调：fold①-② 过，compile 缺 NC
    assert r.status == "DESIGN_NOT_READY"
    assert r["compile_error"]["code"] == "NO_NC"
