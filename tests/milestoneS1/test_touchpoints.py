"""S1-3/S1-4：Gate1 多 Stage 卡构造 + 触点执行器（无网络假 runner）。

- 卡：stages[] 必填字段、依赖闭包、重复/未知依赖拒绝、payload 映射单方案整链；
- 触点：沙箱命令结构（不挂 /mnt/venvs = 沙箱内无 mujoco）、response 形状拒绝、
  产者链（session_id+model）来自 transcript、会话账起止成对。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.hk import study_gate1_cards as G
from orchestrator.hk.touchpoints import (HeadlessActor, TouchpointError,
                                         TOUCHPOINT_INSTRUCTIONS,
                                         HeadlessSessionRunner)
from orchestrator.rsi_bridge.session_ledger import SessionLedger

STAGES = [
    {"stage_id": "A_N", "goal": "摩擦×质量 DiD + sham", "budget": "4 触点/160 run",
     "depends_on": [], "stop_rules": "校准失败改设计不改界"},
    {"stage_id": "B_N", "goal": "kp×tc 开放", "budget": "4 触点/128 run",
     "depends_on": ["A_N"], "stop_rules": "结论方向不换题"},
    {"stage_id": "REVIEW", "goal": "独立复跑 + 双审", "budget": "8 网关 POST",
     "depends_on": ["A_N", "B_N"], "stop_rules": "审查仪器误接收即停付"},
]


def test_goal_card_requires_complete_stages():
    card = G.build_goal_card(title="S1", stages=STAGES)
    assert [s["stage_id"] for s in card["stages"]] == ["A_N", "B_N", "REVIEW"]
    with pytest.raises(G.GoalCardError) as excinfo:
        G.build_goal_card(title="x", stages=[{"stage_id": "A"}])
    assert excinfo.value.code == "STAGE_INCOMPLETE"


def test_goal_card_rejects_duplicate_and_unknown_depends():
    with pytest.raises(G.GoalCardError) as excinfo:
        G.build_goal_card(title="x", stages=[STAGES[0], STAGES[0]])
    assert excinfo.value.code == "STAGE_DUPLICATE"
    bad = dict(STAGES[0], depends_on=["NOPE"])
    with pytest.raises(G.GoalCardError) as excinfo:
        G.build_goal_card(title="x", stages=[bad])
    assert excinfo.value.code == "STAGE_DEPENDS_UNKNOWN"


def test_gate1_payload_is_single_chain_proposal():
    card = G.build_goal_card(title="S1 研究链", stages=STAGES)
    payload = G.build_gate1_card_payload(card)
    assert payload["task_title"] == "S1 研究链"
    assert len(payload["proposals"]) == 1
    proposal = payload["proposals"][0]
    for stage in STAGES:
        assert stage["stage_id"] in proposal["focus"]
    assert "A_N≤" in proposal["artifacts"]


class FakeRunner:
    def __init__(self, ledger: SessionLedger, *, response: dict | None = None,
                 fail: str | None = None):
        self.ledger = ledger
        self.response = response
        self.fail = fail
        self.calls = []

    def run(self, *, touchpoint, workspace, request, host_label=""):
        self.calls.append((touchpoint, request))
        ws = Path(workspace)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "request.json").write_text(json.dumps(request))
        key = f"tp-{touchpoint}-x"
        self.ledger.start(session_id=key, model="m", task="t", touchpoint=touchpoint)
        transcript = ws / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "system", "session_id": "sess-1",
                                          "model": "deepseek-v4.1-flash-1017"}) + "\n")
        self.ledger.finish(session_id=key, usage={"status": "UNKNOWN"},
                           exit_status="rc=0")
        if self.fail:
            raise TouchpointError(self.fail, "fake")
        response = ws / "response.json"
        response.write_text(json.dumps(self.response if self.response is not None else {}))
        from orchestrator.hk.touchpoints import TouchpointRecord
        return TouchpointRecord(
            touchpoint=touchpoint, session_key=key, claude_session_id="sess-1",
            model="deepseek-v4.1-flash-1017", workspace=ws, transcript=transcript,
            usage={"status": "UNKNOWN"}, returncode=0, elapsed_s=0.0,
            started_at_utc="2026-01-01T00:00:00Z", response_path=response)


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "A_N_run001"
    run_dir.mkdir(parents=True)
    return run_dir


def test_actor_returns_response_and_records_producer(tmp_path):
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    runner = FakeRunner(ledger, response={"source": "# analysis\n"})
    actor = HeadlessActor(runner, evidence_root=tmp_path)
    run_dir = _run_dir(tmp_path)
    out = actor("write_analysis", "run_manifest",
                {"run_dir": str(run_dir), "run_manifest_summary": {"n_runs": 0}})
    assert out == {"source": "# analysis\n"}
    meta = json.loads((run_dir / "touchpoint_write_analysis.meta.json").read_text())
    assert meta["claude_session_id"] == "sess-1"
    assert meta["model"] == "deepseek-v4.1-flash-1017"
    assert len(ledger.started()) == 1 and len(ledger.finished()) == 1


def test_actor_rejects_missing_keys(tmp_path):
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    runner = FakeRunner(ledger, response={"verdict": "SUPPORTED"})
    actor = HeadlessActor(runner, evidence_root=tmp_path)
    run_dir = _run_dir(tmp_path)
    # S2b F3：请求缺 nc_evidence/results → REQUEST_INCOMPLETE
    with pytest.raises(TouchpointError) as excinfo:
        actor("submit_claim", "confirm_result", {"run_dir": str(run_dir)})
    assert excinfo.value.code == "REQUEST_INCOMPLETE"
    # 请求面完整但 response 缺键 → RESPONSE_INCOMPLETE
    with pytest.raises(TouchpointError) as excinfo:
        actor("submit_claim", "confirm_result",
              {"run_dir": str(run_dir), "nc_evidence": {}, "results": []})
    assert excinfo.value.code == "RESPONSE_INCOMPLETE"


def test_actor_requires_run_dir(tmp_path):
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    actor = HeadlessActor(FakeRunner(ledger, response={}), evidence_root=tmp_path)
    with pytest.raises(TouchpointError) as excinfo:
        actor("propose_plan", "experiment_spec", {})
    assert excinfo.value.code == "RUN_DIR_MISSING"


def test_runner_command_has_no_venv_and_has_resolve(tmp_path):
    """沙箱结构负控：不挂 /mnt/venvs（mujoco 不可达）、必须挂 /run/systemd/resolve。"""
    runner = HeadlessSessionRunner(sessions_root=tmp_path / "s",
                                   ledger=SessionLedger(tmp_path / "s.jsonl"))
    cmd = runner._command(tmp_path / "ws", tmp_path / "home", "propose_plan")
    joined = " ".join(cmd)
    assert "/mnt/venvs" not in joined
    assert "/run/systemd/resolve" in joined
    assert "--share-net" in cmd
    brief = cmd[cmd.index("-p") + 1]
    assert "propose_plan" in brief
    assert TOUCHPOINT_INSTRUCTIONS["propose_plan"] in brief


def test_unknown_touchpoint_rejected(tmp_path):
    runner = HeadlessSessionRunner(sessions_root=tmp_path / "s",
                                   ledger=SessionLedger(tmp_path / "s.jsonl"))
    with pytest.raises(TouchpointError) as excinfo:
        runner.run(touchpoint="ghost", workspace=tmp_path / "ws", request={})
    assert excinfo.value.code == "UNKNOWN_TOUCHPOINT"


# ---------------------------------------------------------------------------
# 回归（2026-11-18 事故）：driver → HeadlessActor 的运行上下文必须到位
# ---------------------------------------------------------------------------

def test_driver_passes_run_context_to_headless_actor(tmp_path):
    """事故形状：首个触点曾因 RUN_DIR_MISSING 崩掉（actor 只拿 value，拿不到 ctx）。

    本测试用真 driver + 真 HeadlessActor + FakeRunner（不触网）：actor 必须能
    从 ``value['_ctx']['run_dir']`` 取到本轮 run 目录，并把 meta 写进该目录。
    """
    from orchestrator.hk.driver import GateDecision, ResearchDriver
    from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                        default_confirmer)
    from orchestrator.rsi_r1 import task_specs as TS

    ledger = SessionLedger(tmp_path / "sessions.jsonl")

    class DynRunner(FakeRunner):
        def run(self, *, touchpoint, workspace, request, host_label=""):
            if touchpoint == "write_analysis":
                self.response = {"source": "# x\n"}
            if touchpoint == "diagnose_fail":
                self.response = {"diagnosis": "stub"}
            if touchpoint == "propose_plan":
                spec = (request.get("value") or {}).get("spec") or {}
                self.response = {"conditions": spec.get("conditions"),
                                 "n_block": spec.get("n_block"),
                                 "study": spec.get("study"),
                                 "plan_sha256": spec.get("plan_sha256")}
            if touchpoint == "submit_claim":
                rows = [{k: r[k] for k in ("id", "estimate", "ci_low", "ci_high",
                                           "n_blocks", "claim")}
                        for r in (request.get("value") or {}).get("contrasts", [])]
                host = (request.get("value") or {}).get("contrasts") or [{}]
                self.response = {"verdict": host[0].get("claim"),
                                 "statement": "s", "contrasts": rows}
            return super().run(touchpoint=touchpoint, workspace=workspace,
                               request=request, host_label=host_label)

    runner = DynRunner(ledger, response={"source": "# x\n"})
    actor = HeadlessActor(runner, evidence_root=tmp_path)
    task = TS.build_r1n_a()
    spec = {"schema": "x", "task_id": task.task_id, "study": "A_N",
            "plan_sha256": "e" * 64, "n_block": 2,
            "conditions": [c.to_dict() for c in task.conditions]}
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=actor, evidence_root=tmp_path / "ev",
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True)
    outcome = driver.run_campaign({"goal": "A_N"}, {"A_N": spec})
    assert outcome["status"] == "done", outcome
    run_dir = tmp_path / "ev" / "runs" / "A_N_run001"
    assert (run_dir / "touchpoint_propose_plan.meta.json").is_file()
    assert (run_dir / "touchpoint_write_analysis.meta.json").is_file()
    assert (run_dir / "touchpoint_submit_claim.meta.json").is_file()
