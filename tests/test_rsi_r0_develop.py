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


#: v2 协议：首次实验前必须先登记计划（计划 L129 的 C1 通道）。
PLAN = {"hypothesis": "增大关节阻尼会缩短沉降时间",
        "expected_direction": "阻尼越大沉降越快",
        "variables": ["damping"], "controls": ["initial_angle", "steps"]}
PREREG = json.dumps({"action": "preregister", "plan": PLAN}, ensure_ascii=False)


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
        PREREG,
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
    assert row["requests_used"] == 3
    assert row["evaluation"]["layers"]["L4"]["status"] == "PASS", row["evaluation"]
    assert row["first_failure_layer"] is None
    assert row["ledger_seqs"] == [1, 2, 3]
    assert row["prereg"]["sha256"] == sha256_of(PLAN)


def test_request_cap_is_enforced(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    gateway = FakeGateway([PREREG] + [json.dumps({"action": "run_experiment",
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


# ---------------------------------------------------------------------------
# T6 首跑实跑暴露的两个接线缺陷（2026-11-08，0 重试已记账，修复后重跑）
# ---------------------------------------------------------------------------
def test_push_task_injects_the_task_book_target(campaign, monkeypatch):
    """E/D 任务书公开 keys 只有增益；target 由宿主按 goal.target 注入。

    不注入时容器 push_control 以 rc=2 拒绝"缺 target_x"，首跑因此把候选问题
    误记成仪器故障。此处断言：候选只给增益也能构造合法会话；候选自送 target
    会被任务书固定值覆盖（目标点是题目的一部分，不允许候选改）。
    """
    monkeypatch.setattr(dv, "load_task", lambda _id: TASK)
    monkeypatch.setattr(dv, "load_private", lambda _id: PRIVATE)
    monkeypatch.setattr(dv, "build_scene", lambda _t, _p: "<mujoco/>")
    session, task, _scene = dv.build_host_session("E-dev")
    clean = session.validate({"kp": 100.0, "kd": 10.0, "max_force": 50.0})
    assert clean["target_x"] == pytest.approx(0.5)
    assert clean["target_y"] == pytest.approx(0.0)
    hijack = session.validate({"target_x": -5.0, "target_y": -5.0, "kp": 1.0})
    assert hijack["target_x"] == pytest.approx(0.5)     # 候选改不动目标
    assert hijack["target_y"] == pytest.approx(0.0)


def test_service_rc2_is_a_candidate_error_not_an_instrument_failure(monkeypatch):
    """service.py 的候选请求失败码恒为 2：必须回喂给候选，不得终止整条轨迹。"""
    import subprocess
    from orchestrator.rsi_r0 import trusted as tr

    class _Proc:
        def __init__(self, rc: int) -> None:
            self.returncode = rc
            self.stdout = ('RESULT_JSON {"schema": "rsi_r0.service.v1", "ok": false, '
                           '"error": "字段 target_x 必须是数值"}\n')
            self.stderr = ""

    monkeypatch.setattr(tr.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **kw: _Proc(2))
    with pytest.raises(tr.CandidateRequestError):
        tr.call_service({"kind": "push_control"}, image_digest=dv.IMAGE_REF)
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **kw: _Proc(1))
    with pytest.raises(tr.TrustedServiceError):
        tr.call_service({"kind": "push_control"}, image_digest=dv.IMAGE_REF)


def test_development_trial_takes_the_task_id_from_the_family(campaign, monkeypatch):
    """D/R 的批次票 task=2/3；默认 task 1 会让账本按 batch_approval_mismatch 拒发。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    assert dv.DevelopmentTrial(runner, FakeGateway([]), "E").task_id == 1
    assert dv.DevelopmentTrial(runner, FakeGateway([]), "D").task_id == 2
    assert dv.DevelopmentTrial(runner, FakeGateway([]), "R").task_id == 3


def test_request_plan_never_exceeds_the_ticket_remaining(campaign, monkeypatch):
    """票据是跨进程累计的：已占行必须先扣，否则重跑会撞批次熔断写 pause 件。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    batch = dv.DEV_BATCHES["E"]
    rows = [{"seq": 1, "event": "call", "ok": True, "batch_id": batch, "task": 1},
            {"seq": None, "event": "preflight_blocked", "ok": False,
             "batch_id": batch, "task": 1},
            {"seq": 2, "event": "call", "ok": True, "batch_id": "R0-OTHER", "task": 1}]
    monkeypatch.setattr(dv.ql, "read_lines", lambda _path=None: list(rows))
    trial = dv.DevelopmentTrial(runner, FakeGateway([]), "E")
    facts = trial.preconditions()
    assert facts["batch_used"] == 2              # 只数本批（含 blocked 占位行）
    assert trial.request_cap == dv.DEV_CAP - 2




class _RaisingGateway(FakeGateway):
    """第 n 次调用抛指定异常：模拟提供方 HTTP200 空回答（无重试 ⇒ 直接终止本族）。"""

    def __init__(self, replies: list[str], *, fail_at: int) -> None:
        super().__init__(replies)
        self.fail_at = fail_at

    def call(self, *args, **kwargs):
        if self.rows + 1 == self.fail_at:
            self.rows += 1
            raise RuntimeError("stage=coding_debug 连续 1 次 HTTP 200 空回答")
        return super().call(*args, **kwargs)


def test_blocked_row_keeps_the_per_request_log(campaign, monkeypatch):
    """2026-11-08 实测缺陷：BLOCKED 行 log 恒空 ⇒ 事后分不清模型第几轮怎么作答。"""
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    good = json.dumps({"action": "run_experiment",
                       "params": {"kp": 100.0, "kd": 10.0, "max_force": 50.0,
                                  "steps": 100}})
    gateway = _RaisingGateway([PREREG, good], fail_at=3)
    monkeypatch.setattr(dv, "build_host_session", lambda _id: _factory()(_id))
    report = dv._run_development_locked(
        runner, gateway, ("E",), clock=lambda: "2026-11-08T00:00:00Z")
    row = report["families"][0]
    assert row["state"] == "BLOCKED"
    assert row["requests_used"] == 2
    # 每一轮的动作类型与终止原因必须留在现场里
    assert [e["action"]["kind"] for e in row["log"]] == ["preregister", "run_experiment"]
    assert row["log"][1]["index"] == 2
    assert row["log"][0]["stop_reason"] == "end_turn"
    # 两轮各 100/50（FakeGateway 固定用量），BLOCKED 行不许把它们吞掉
    assert row["tokens"] == {"in": 200, "out": 100}


# ---------------------------------------------------------------------------
# v2 协议：计划登记（preregistration）——修 R 的 L5/C1 结构性不可达
# ---------------------------------------------------------------------------
def _run(campaign, monkeypatch, replies, *, family="E"):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    runner = _arm(campaign, monkeypatch)
    gateway = FakeGateway(replies)
    trial = dv.DevelopmentTrial(runner, gateway, family, session_factory=_factory(),
                                clock=lambda: "2026-11-09T00:00:00Z")
    return trial, trial.run()


def test_experiment_before_prereg_is_refused(campaign, monkeypatch):
    """未登记就发实验：不收参数、不代跑，只回错误类别；登记后照常继续。"""
    run = json.dumps({"action": "run_experiment",
                      "params": {"kp": 100.0, "kd": 10.0, "max_force": 50.0,
                                 "steps": 100}})
    submit = json.dumps({"action": "submit", "family": "E", "run_id": "run_000",
                         "claim": {"distance": 0.0, "final_speed": 0.0}})
    trial, row = _run(campaign, monkeypatch, [run, PREREG, run, submit])
    kinds = [e["action"]["kind"] for e in row["log"]]
    assert kinds == ["prereg_required", "preregister", "run_experiment", "submit"]
    # 被拒的那一轮没有真的跑实验：整条轨迹只有一次 session.run
    assert row["calls"] == 1
    assert row["state"] == "SUBMITTED"
    assert trial._prereg["runs_before"] == 0


def test_prereg_cannot_be_replaced_after_experiments_ran(campaign, monkeypatch):
    """跑过实验再补登 = 事后解释：一律按 duplicate 拒，第一次登记不被覆盖。"""
    run = json.dumps({"action": "run_experiment",
                      "params": {"kp": 100.0, "kd": 10.0, "max_force": 50.0,
                                 "steps": 100}})
    other = json.dumps({"action": "preregister",
                        "plan": {"hypothesis": "事后改口", "expected_direction": "down"}})
    trial, row = _run(campaign, monkeypatch, [PREREG, run, other])
    kinds = [e["action"]["kind"] for e in row["log"]]
    assert kinds[:3] == ["preregister", "run_experiment", "prereg_duplicate"]
    assert row["prereg"]["plan"] == PLAN             # 早先那次登记不被覆盖
    assert row["prereg"]["request_index"] == 1


def test_duplicate_prereg_is_refused_and_first_one_stands(campaign, monkeypatch):
    first = json.dumps({"action": "preregister",
                        "plan": {"hypothesis": "A", "expected_direction": "up"}})
    second = json.dumps({"action": "preregister",
                         "plan": {"hypothesis": "B", "expected_direction": "down"}})
    trial, row = _run(campaign, monkeypatch, [first, second])
    assert [e["action"]["kind"] for e in row["log"]][:2] == ["preregister",
                                                             "prereg_duplicate"]
    assert row["prereg"]["plan"] == {"hypothesis": "A", "expected_direction": "up"}


def test_incomplete_plan_is_refused_then_can_be_retried(campaign, monkeypatch):
    weak = json.dumps({"action": "preregister", "plan": {"hypothesis": "只有假设"}})
    trial, row = _run(campaign, monkeypatch, [weak, PREREG])
    assert [e["action"]["kind"] for e in row["log"]][:2] == ["prereg_incomplete",
                                                             "preregister"]
    assert row["prereg"]["sha256"] == sha256_of(PLAN)


def test_prereg_hash_and_timestamp_are_fed_to_the_evaluator(campaign, monkeypatch):
    """C1 的硬证据：评测器拿到的 prereg_sha256 必须等于登记时算出的哈希。"""
    seen: list[dict] = []

    class _Result:
        def as_dict(self):
            return {"execution": "COMPLETE", "scientific": "NOT_APPLICABLE",
                    "layers": {}, "first_failure_layer": None}

    def _fake_evaluate(inp):
        seen.append({"prereg_sha256": inp.prereg_sha256, "slot_id": inp.slot_id})
        return _Result()

    monkeypatch.setattr(dv, "evaluate", _fake_evaluate)
    submit = json.dumps({"action": "submit", "family": "E", "run_id": "run_000"})
    trial, row = _run(campaign, monkeypatch, [PREREG, submit])
    assert seen and seen[0]["prereg_sha256"] == sha256_of(PLAN)
    assert row["prereg"]["sha256"] == sha256_of(PLAN)
    assert row["prereg"]["registered_at_utc"] == "2026-11-09T00:00:00Z"


def test_protocol_spec_v2_declares_preregistration():
    """协议自述与实现同源：改动作集就必须改规格哈希（评审/锁都看它）。"""
    spec = dv.PROTOCOL_SPEC
    assert spec["schema"] == "rsi_r0.dev_protocol.v2"
    assert spec["actions"] == ["preregister", "run_experiment", "submit"]
    assert spec["preregistration"]["required"] is True
    assert spec["preregistration"]["required_fields"] == list(dv.PREREG_REQUIRED_FIELDS)


# ---------------------------------------------------------------------------
# T6-REV-1 仪器修复（2026-11-09 独立评审判 INVALID_INSTRUMENT 后）
# 缺陷：R 族 damping 在宿主接线上从未进入场景；修复后逐调用重建并逐调用判场景身份。
# ---------------------------------------------------------------------------
PEND_TASK = {"interface": {"kind": "free_run"}, "steps_max": 2000,
             "goal": {"type": "controlled_experiment"}}


def test_pendulum_damping_is_wired_into_every_run(monkeypatch):
    """候选自选的 damping 必须改变场景 XML；旧接线两个水平跑同一场景。"""
    monkeypatch.setattr(dv, "load_task", lambda _id: PEND_TASK)
    monkeypatch.setattr(dv, "load_private", lambda _id: {})
    session, _task, base = dv.build_host_session("R-dev")
    assert 'damping="0.050000"' in base                      # 公开面默认值
    low = session.validate({"initial_angle": 0.8, "damping": 0.2, "steps": 2000})
    high = session.validate({"initial_angle": 0.8, "damping": 0.6, "steps": 2000})
    xml_low = session._build_scene(low)
    xml_high = session._build_scene(high)
    assert 'damping="0.200000"' in xml_low and 'damping="0.600000"' in xml_high
    assert sha256_of(xml_low) != sha256_of(xml_high)
    assert sha256_of(xml_low) != sha256_of(base)
    assert sha256_of(xml_high) != sha256_of(base)


def test_free_run_request_carries_the_candidate_damping(monkeypatch):
    """真正发给可信服务的 scene_xml 也要逐次不同（不只是 builder 返回值）。"""
    from orchestrator.rsi_r0 import trusted as tr

    monkeypatch.setattr(dv, "load_task", lambda _id: PEND_TASK)
    monkeypatch.setattr(dv, "load_private", lambda _id: {})
    session, _task, _base = dv.build_host_session("R-dev")
    seen: list[str] = []

    def _fake(request, **_kw):
        seen.append(request["scene_xml"])
        return {"run_id": f"run_{len(seen)}", "steps_run": request["steps"],
                "events_sha256": "e" * 64, "angle_series": [],
                "final_angle": 0.0, "final_angular_speed": 0.0}

    monkeypatch.setattr(tr, "call_service", _fake)
    session.run({"initial_angle": 0.8, "damping": 0.2, "steps": 100})
    session.run({"initial_angle": 0.8, "damping": 0.6, "steps": 100})
    assert 'damping="0.200000"' in seen[0] and 'damping="0.600000"' in seen[1]
    assert seen[0] != seen[1]
    assert (session.calls[0]["scene_sha256"]
            != session.calls[1]["scene_sha256"])


def test_scene_for_is_fed_to_the_evaluator(campaign, monkeypatch):
    """L2 场景身份必须逐调用判（evaluator 的 scene_for），不能只比一个常量。"""
    seen: list[dict] = []

    class _Result:
        def as_dict(self):
            return {"execution": "COMPLETE", "scientific": "NOT_APPLICABLE",
                    "layers": {}, "first_failure_layer": None}

    def _fake_evaluate(inp):
        seen.append({"scene_for": inp.scene_for, "scene_sha256": inp.scene_sha256})
        return _Result()

    monkeypatch.setattr(dv, "evaluate", _fake_evaluate)
    submit = json.dumps({"action": "submit", "family": "E", "run_id": "run_000"})
    _trial, _row = _run(campaign, monkeypatch, [PREREG, submit])
    assert seen and seen[0]["scene_for"] is not None
    assert seen[0]["scene_for"]({"params": {"kp": 1.0, "steps": 100}}) == sha256_of(
        "<mujoco/>")


def test_merge_report_carries_untouched_families_and_rehashes():
    """部分族重跑：未重跑的族逐字沿用，totals/报告哈希按合并正文重算。"""
    prior = {
        "report_sha256": "a" * 64,
        "families": [
            {"family": "E", "requests_used": 3,
             "tokens": {"in": 1, "out": 2}, "ledger_seqs": [1, 2, 3]},
            {"family": "D", "requests_used": 4,
             "tokens": {"in": 5, "out": 6}, "ledger_seqs": [4]},
            {"family": "R", "requests_used": 7,
             "tokens": {"in": 7, "out": 8}, "ledger_seqs": [9]},
        ],
    }
    new = {
        "report_sha256": "b" * 64,
        "families": [
            {"family": "R", "requests_used": 6,
             "tokens": {"in": 10, "out": 20}, "ledger_seqs": [40, 41, 42]},
        ],
    }
    merged = dv.merge_report(new, prior)
    assert [r["family"] for r in merged["families"]] == ["E", "D", "R"]
    assert merged["families"][0] is prior["families"][0]      # 逐字沿用
    assert merged["families"][1] is prior["families"][1]
    assert merged["families"][2]["requests_used"] == 6
    assert merged["totals"]["requests_used"] == 13
    assert merged["merged_from"] == {"prior_report_sha256": "a" * 64,
                                     "rerun_families": ["R"],
                                     "carried_families": ["E", "D"]}
    body = {k: v for k, v in merged.items() if k != "report_sha256"}
    assert merged["report_sha256"] == sha256_of(body)
