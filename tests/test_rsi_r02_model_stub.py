"""RSI-R0.2 同路径 ModelStub 测试（T3/AC17，零付费、无容器）。

断言三件事：替身只吃渲染后的工具结果文本；R 的结论由本轮观测复算（不是读分析
产物）；E/D 按阶梯推进且只在命中自设判据后分析/提交。另断言替身绝不触碰
trial 的 judge/analyses（同路径约束的直接反例）。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import model_stub as MS
from orchestrator.rsi_r02 import state as ST
from tests import rsi_r02_helpers as H

R_PUBLIC = {
    "instance_id": "R1", "family": "R",
    "public": {"task": "阻尼是否缩短沉降",
               "research": {"question": "较高阻尼是否更快沉降", "h": 1,
                            "damping_levels": [0.15, 0.45],
                            "initial_angles": [0.8, -0.8],
                            "delta": 0.3}},
}
PUSH_PUBLIC = {
    "instance_id": "E1", "family": "E",
    "public": {"goal": "把推杆推到目标位置"},
}

LADDER = ({"kp": 80.0, "kd": 10.0, "max_force": 40.0},
          {"kp": 160.0, "kd": 14.0, "max_force": 80.0},
          {"kp": 320.0, "kd": 20.0, "max_force": 120.0})


class _ScriptedTrial:
    """按参数返回结构化结果的最小真值服务替身（不含 judge/analyses 属性）。"""

    def __init__(self, *, settle_by_cell=None, distance_by_kp=None):
        # 2026-11-10 R0.2 root G 文本审计 FALSE #11/#25 的连带修正：宿主解析面现在
        # 把「本实例非固定参数」全部当必填（trial._run_experiment 直接下标取值）。
        # 这个替身原先声明的是 R/E 两族参数的**并集**、且 fixed_params 为空——那既
        # 不是真实 pendulum 面（真实面只有 damping/initial_angle/steps，timestep
        # 由宿主注入），也不可能是宿主真正服务的面。按 family 给真面。
        push = distance_by_kp is not None
        self.surface = AC.ActionSurface(
            kind="push_control" if push else "pendulum_run",
            param_bounds=({"kp": (0, 5000), "kd": (0, 500), "max_force": (0, 500),
                           "steps": (1, 2000)} if push else
                          {"damping": (0, 5), "initial_angle": (-3.2, 3.2),
                           "steps": (1, 2000)}),
            fixed_params={"timestep": 0.002}, max_steps=2000)
        self.settle_by_cell = dict(settle_by_cell or {})
        self.distance_by_kp = dict(distance_by_kp or {})
        self.calls: "list[AC.Action]" = []
        self._n = 0

    def dispatch(self, action: AC.Action) -> "dict[str, Any]":
        self.calls.append(action)
        if isinstance(action, AC.Preregister):
            return {"ok": True, "action": "preregister", "prereg_sha256": "a" * 64}
        if isinstance(action, AC.RunExperiment):
            self._n += 1
            p = action.params
            if "damping" in p:
                cell = ("high" if float(p["damping"]) == 0.45 else "low") + "/" + \
                       ("plus" if float(p["initial_angle"]) > 0 else "minus")
                t = self.settle_by_cell.get(cell)
                settle = ({"time_s": t, "censored": False} if t is not None
                          else {"time_s": None, "censored": True})
                return {"ok": True, "action": "run_experiment",
                        "run_id": f"run-{self._n}", "cell": cell,
                        "summary": {"settle": settle}}
            d = self.distance_by_kp.get(float(p["kp"]))
            if d is None:
                return {"ok": True, "action": "run_experiment",
                        "run_id": f"run-{self._n}",
                        "summary": {"distance": 1.0, "final_speed": 1.0}}
            return {"ok": True, "action": "run_experiment", "run_id": f"run-{self._n}",
                    "summary": {"distance": d, "final_speed": 0.0}}
        if isinstance(action, AC.Analyze):
            return {"ok": True, "action": "analyze",
                    "analysis": {"analysis_id": f"a-{len(self.calls)}"},
                    "path": "analysis/a.json", "sha256": "b" * 64}
        if isinstance(action, AC.Submit):
            return {"ok": True, "action": "submit", "claim": action.claim}
        raise AssertionError(f"未知动作 {action!r}")

    def judge(self) -> "dict[str, Any]":
        return {"pass": True, "layer": "L5", "family": "E"}

    def __getattr__(self, name: str) -> "object":
        if name in ("analyses", "store", "runs", "submissions", "prereg"):
            raise AssertionError(f"ModelStub 不得触碰 trial.{name}（同路径约束）")
        raise AttributeError(name)


def _run(ctx, *, public, policy, trial, family, instance_id):
    slot_ids = [f"{instance_id}-0{i}" for i in range(1, 9)]
    H.write_slots(ctx, slot_ids, family=family)
    loop, model, store, journal, slots, book = H.build_loop(
        ctx, slot_ids=slot_ids,
        replies=[], trial=trial, family=family, instance_id=instance_id)
    stub = MS.ModelStub(public, policy=policy)
    loop.call_model = stub
    loop.public_doc = dict(public)
    loop.system = MS.M.build_system(loop.public_doc, trial.surface)
    out = loop.run()
    return out, stub, loop


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    return H.make_campaign(tmp_path)


# ---- R 族 ---------------------------------------------------------------


def test_r_stub_runs_four_cells_then_claims_supported(ctx):
    trial = _ScriptedTrial(settle_by_cell={"low/plus": 1.0, "high/plus": 0.5,
                                           "low/minus": 1.0, "high/minus": 0.5})
    out, stub, loop = _run(ctx, public=R_PUBLIC,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="R", instance_id="R1")
    assert out["outcome"] == "submitted"
    acts = [type(a).__name__ for a in trial.calls]
    assert acts == ["Preregister", "RunExperiment", "RunExperiment",
                    "RunExperiment", "RunExperiment", "Analyze", "Submit"]
    assert trial.calls[-1].claim == "SUPPORTED"
    # 结论确实来自观测复算：两个角度效应各 0.5 → mean=0.5, z=0.5 ≥ 0.3
    assert len(trial.calls[-1].run_ids) == 4


def test_r_stub_claim_is_refuted_when_h_negative_and_effect_positive(ctx):
    public = json.loads(json.dumps(R_PUBLIC))
    public["public"]["research"]["h"] = -1
    trial = _ScriptedTrial(settle_by_cell={"low/plus": 1.0, "high/plus": 0.5,
                                           "low/minus": 1.0, "high/minus": 0.5})
    out, stub, loop = _run(ctx, public=public,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="R", instance_id="R1")
    assert out["outcome"] == "submitted"
    assert trial.calls[-1].claim == "REFUTED"


def test_r_stub_censored_cell_is_inconclusive(ctx):
    trial = _ScriptedTrial(settle_by_cell={"low/plus": 1.0, "high/plus": 0.5,
                                           "low/minus": 1.0, "high/minus": None})
    out, stub, loop = _run(ctx, public=R_PUBLIC,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="R", instance_id="R1")
    assert out["outcome"] == "submitted"
    assert trial.calls[-1].claim == "INCONCLUSIVE"
    assert trial.calls[-1].mean_effect is None


# ---- E/D 族 -------------------------------------------------------------


def test_push_stub_stops_ladder_when_self_tol_hit(ctx):
    trial = _ScriptedTrial(distance_by_kp={80.0: 0.5, 160.0: 0.05, 320.0: 0.001})
    out, stub, loop = _run(ctx, public=PUSH_PUBLIC,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="E", instance_id="E1")
    assert out["outcome"] == "submitted"
    kps = [float(a.params["kp"]) for a in trial.calls
           if isinstance(a, AC.RunExperiment)]
    assert kps == [80.0, 160.0, 320.0]
    assert trial.calls[-1].claim == "SUPPORTED"


def test_push_stub_exhausts_ladder_and_submits_refuted(ctx):
    trial = _ScriptedTrial(distance_by_kp={80.0: 0.5, 160.0: 0.4, 320.0: 0.3})
    out, stub, loop = _run(ctx, public=PUSH_PUBLIC,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="E", instance_id="E1")
    assert out["outcome"] == "submitted"
    assert trial.calls[-1].claim == "REFUTED"


# ---- 同路径约束 ---------------------------------------------------------


def test_stub_only_sees_rendered_text(ctx):
    """每一轮都从 Request 的消息里读结果；更早轮次被压缩后仍不丢数值记忆。"""
    trial = _ScriptedTrial(settle_by_cell={"low/plus": 1.0, "high/plus": 0.5,
                                           "low/minus": 1.0, "high/minus": 0.5})
    out, stub, loop = _run(ctx, public=R_PUBLIC,
                           policy=MS.StubPolicy(ladder=LADDER), trial=trial,
                           family="R", instance_id="R1")
    assert len(stub.requests) == 7   # prereg + 四格 + analyze + submit
    # 第 6 轮（submit 前）历史已被压缩：更早轮次摘要里没有 summary 字段
    older = [m for m in stub.requests[-1].messages
             if m["role"] == "user" and m["content"].startswith("### 更早轮次")]
    assert older, "六轮后应有固定字段摘要"
    assert "settle" not in older[0]["content"]
    # 但替身自己记着四格沉降值，结论未受影响
    assert trial.calls[-1].claim == "SUPPORTED"
    assert stub.results[-1]["action"] == "analyze"


def test_stub_refuses_unknown_family():
    with pytest.raises(MS.StubError):
        MS.ModelStub({"instance_id": "X", "family": "Q", "public": {}},
                     policy=MS.StubPolicy(ladder=LADDER))


def test_stub_policy_requires_ladder():
    with pytest.raises(MS.StubError):
        MS.StubPolicy(ladder=())
