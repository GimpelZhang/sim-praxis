"""T6 开发轨迹的零付费单测：假网关 + 假宿主会话，验证协议/记账/评测接线。

真实仿真与真实发网不在单测范围（那属于 T6 实跑）；这里只证明：

* 24 请求帽、8/16/24 检查点、无重试、逐请求记账按预期走；
* 候选交卷后评测器真的被调用、首失败层由评测结果推；
* G-V 事实只从报告事实推，不看自述。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import budget_contract, develop as dv, gates
from orchestrator.rsi_r0 import runner as rn
from orchestrator.rsi_r0.contracts import sha256_of


def sha256_of_text(text: str) -> str:
    """与 T2/trusted 同一口径：场景身份用 contracts.sha256_of（规范 JSON 哈希）。"""
    return sha256_of(text)


class FakeSession:
    """假的宿主会话：结果来自请求参数，事件哈希确定性。"""

    def __init__(self, scene_xml: str) -> None:
        self.scene_xml = scene_xml
        self.scene_sha256 = sha256_of_text(scene_xml)
        self.calls: list[dict] = []

    def run(self, params):
        clean = {k: float(v) for k, v in params.items()}
        steps = int(clean.get("steps") or 100)
        events = f"steps={steps};kp={clean.get('kp')}"
        run_id = f"run_{len(self.calls):03d}"
        result = {
            "run_id": run_id, "steps_run": steps, "events_sha256": sha256_of_text(events),
            "distance": 0.0, "final_pos": [0.5, 0.0], "final_speed": 0.0,
            "target": [0.5, 0.0], "series": [[0, 0.0, 0.0, 0.0, 0.0],
                                            [steps, 0.5, 0.0, 0.0, 0.0]],
        }
        self.calls.append({"params": clean, "run_id": run_id,
                           "events_sha256": result["events_sha256"],
                           "scene_sha256": self.scene_sha256, "steps_run": steps})
        return result


class FakeGateway:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.rows = 0

    DEFAULT = json.dumps({"action": "run_experiment", "params": {"steps": 10}})

    def call(self, stage, messages, *, max_tokens, temperature, timeout, capture=None):
        text = self.replies.pop(0) if self.replies else self.DEFAULT
        self.rows += 1
        rp.rsi_r0_ledger_path().parent.mkdir(parents=True, exist_ok=True)
        with rp.rsi_r0_ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": self.rows, "ok": True, "stage": stage}) + "\n")
        if capture is not None:
            capture.update({"stop_reason": "end_turn", "attempts_http": 1,
                            "provider_model": "fake-model", "body_sha256": "b" * 64})
        return text, (100, 50)


TASK = {
    "schema": "rsi_r0.task.v1", "family": "E", "instance_id": "E-dev",
    "template": "push", "steps_max": 3000, "role": "development",
    "goal": {"target": [0.5, 0.0], "type": "reach_and_settle"},
    "interface": {"kind": "push_control",
                  "keys": ["kp", "kd", "max_force"],
                  "limits": {"kp": [0, 5000], "kd": [0, 500]}},
    "scene_params": {"box_half": 0.05, "box_mass": 1.5, "friction": 0.2,
                     "timestep": 0.002},
    "answer": {"required": ["family", "run_id", "claim"],
               "shape": {"claim": {"distance": "<m>", "final_speed": "<m/s>"}}},
}
PRIVATE = {"tolerance": {"distance": 0.02, "final_speed": 0.05}}


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r0"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "t6")
    yield root / "t6"
    for var in ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN",
                "SIMPRAXIS_ALLOW_PAID"):
        monkeypatch.delenv(var, raising=False)


def _arm(campaign, monkeypatch):
    """复用 T5 的武装方式，再给三个开发批次各写门批准件 + 账本票。"""
    from orchestrator.gateway import quota_ledger as ql
    from tests.test_rsi_r0_protocol import _arm as arm_protocol

    runner = arm_protocol(campaign)
    for key, batch in dv.DEV_BATCHES.items():
        ql.write_approval(batch_id=batch, task=1, stage="coding_debug",
                          approved=dv.DEV_CAP, beyond_normal=False,
                          note=f"T6 unit fixture {key}", namespace=ql.RSI_R0_NAMESPACE)
        gates.write_approval(runner.gate_approval_path(batch), gates.approval_body(
            batch_id=batch, task_cap=dv.DEV_CAP, hard_cap=ql.RSI_R0_HARD_CAP,
            currency="USD", usd_ceiling=100.0, approved_by="user",
            approved_at_utc="2026-11-08T00:00:00Z", scope="development"))
    monkeypatch.setattr(dv, "load_task", lambda _id: TASK)
    monkeypatch.setattr(dv, "load_private", lambda _id: PRIVATE)
    monkeypatch.setattr(dv, "build_scene", lambda _t, _p: "<mujoco/>")
    return runner


def _factory(scene_xml="<mujoco/>"):
    def make(instance_id):
        return FakeSession(scene_xml), TASK, scene_xml
    return make


def test_development_trial_submits_and_evaluates(campaign, monkeypatch):
    env_backup = monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    replies = [
        json.dumps({"action": "run_experiment",
                    "params": {"kp": 100.0, "kd": 10.0, "max_force": 50.0, "steps": 100}}),
        json.dumps({"action": "submit", "family": "E", "run_id": "run_000",
                    "claim": {"distance": 0.0, "final_speed": 0.0}}),
    ]
    gateway = FakeGateway(replies)
    trial = dv.DevelopmentTrial(runner, gateway, "E", session_factory=_factory(),
                                clock=lambda: "2026-11-08T00:00:00Z")
    row = trial.run()
    assert row["state"] == "SUBMITTED", row.get("blocked_reason")
    assert row["requests_used"] == 2
    assert row["evaluation"]["layers"]["L4"]["status"] == "PASS", row["evaluation"]
    assert row["first_failure_layer"] is None
    assert row["ledger_seqs"] == [1, 2]


def test_request_cap_is_enforced(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    gateway = FakeGateway([json.dumps({"action": "run_experiment",
                                       "params": {"steps": 10}})] * 30)
    trial = dv.DevelopmentTrial(runner, gateway, "E", session_factory=_factory())
    row = trial.run()
    assert row["state"] == "NO_SUBMISSION"
    assert row["requests_used"] == dv.DEV_CAP == 24
    assert [c["at_request"] for c in row["checkpoints"]] == [8, 16, 24]


def test_invalid_json_is_fed_back_once_and_does_not_crash(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    gateway = FakeGateway(["不是 JSON", '{"action":"nope"}'])
    trial = dv.DevelopmentTrial(runner, gateway, "E", session_factory=_factory())
    row = trial.run()
    assert row["requests_used"] == dv.DEV_CAP          # 错动作不中断轨迹，但也不补测
    assert [e["action"]["kind"] for e in row["log"]][:2] == ["invalid_json", "unknown_action"]
    assert row["state"] == "NO_SUBMISSION"


def test_gate_v_facts_come_from_the_report(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    gateway = FakeGateway([
        json.dumps({"action": "submit", "family": "E", "run_id": "run_000",
                    "claim": {"distance": 0.0, "final_speed": 0.0}}),
    ])
    trial = dv.DevelopmentTrial(runner, gateway, "E", session_factory=_factory())
    row = trial.run()
    report = {"families": [row]}
    facts = dv.gate_v_facts(report)
    assert facts["development_report_present"] is False      # 只有一族
    assert facts["development_gate_met"] is False            # 缺 D/R
