"""RSI-R0 T4：组合根、零付费 e2e、门与批准件（AC21–AC24）。

全部零容器零模型：dry-run 用的是 mock 后端，报告自己声明
``evidence_scope="control_flow_only"``、``layers_verified=[]``——这里证明的是**控制流**
（顺序/门/记账/恢复/零发网），不是任何能力。
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from orchestrator.rsi_r0 import gates, runner as rn
from orchestrator.rsi_r0.cli import EXIT_OK, EXIT_REFUSED, main as cli_main
from orchestrator.rsi_r0.contracts import ResourceCaps

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    """每个测试一个全新 campaign；结束后把 R0 环境变量恢复原样。"""
    saved = {k: os.environ.get(k) for k in
             ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN")}
    path = tmp_path / "rsi_r0" / "c1"
    yield path
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------- AC21 显式 R0
def test_cli_never_imports_a_legacy_execution_entry():
    """新入口只允许 orchestrator.rsi_r0.* + 通用设施；旧阶段执行面一概不许进来。"""
    forbidden = ("orchestrator.graph", "orchestrator.agent_runner",
                 "orchestrator.agentic", "orchestrator.benchmark.evaluator",
                 "orchestrator.benchmark.controls")
    for name in ("cli.py", "runner.py"):
        tree = ast.parse((ROOT / "orchestrator" / "rsi_r0" / name).read_text(encoding="utf-8"))
        mods: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods.append(node.module or "")
        bad = [m for m in mods if any(m == f or m.startswith(f + ".") for f in forbidden)]
        assert not bad, (name, bad)


def test_campaign_binding_is_explicit_and_absolute(campaign, tmp_path):
    with pytest.raises(rn.RunnerError, match="绝对路径"):
        rn.R0Runner(Path("relative/c1"))
    runner = rn.R0Runner(campaign)
    from orchestrator.common import runtime_paths as rp
    assert rp.rsi_r0_campaign_root().resolve() == campaign.resolve()
    assert runner.root == campaign  # 之后一切路径都从这里派生


def test_paths_resolve_inside_the_given_campaign(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    from orchestrator.common import runtime_paths as rp
    for fn in (rp.rsi_r0_protocol_dir, rp.rsi_r0_private_dir, rp.rsi_r0_quota_dir,
               rp.rsi_r0_gateway_state_dir, rp.rsi_r0_closeout_dir,
               rp.rsi_r0_runs_root):
        assert str(fn()).startswith(str(campaign))
    assert (campaign.stat().st_mode & 0o777) == 0o700


def test_ensure_campaign_refuses_unsafe_campaign_name(campaign):
    from orchestrator.common.runtime_paths import RsiR0CampaignUnsetError
    with pytest.raises((RsiR0CampaignUnsetError, rn.RunnerError)):
        rn.R0Runner(campaign.parent / "bad name")


# ---------------------------------------------------------------- AC22 试次合同
def test_formal_trial_may_not_use_a_response_cache(campaign):
    actor = rn.actor_record("t", source="test")
    with pytest.raises(rn.RunnerError, match="禁用响应缓存"):
        rn.TrialSpec(trial_id="R0-P_P1", slot_id="P1", batch_id="R0-P",
                     actor=actor, cache_policy="replay_debug")
    # dry 批次允许（它永不触网），但报告里必须写清楚
    spec = rn.TrialSpec(trial_id="dry", slot_id="P1", batch_id="R0-DRY-P",
                        actor=actor, cache_policy="replay_debug")
    assert spec.cache_policy == "replay_debug"


def test_trial_requires_an_auditable_actor(campaign):
    with pytest.raises(rn.RunnerError, match="actor"):
        rn.TrialSpec(trial_id="t", slot_id="P1", batch_id="R0-P", actor={})
    with pytest.raises(rn.RunnerError, match="source"):
        rn.TrialSpec(trial_id="t", slot_id="P1", batch_id="R0-P",
                     actor={"name": "x"})
    with pytest.raises(rn.RunnerError, match="actor source"):
        rn.actor_record("x", source="somebody")


def test_every_trial_gets_a_fresh_session(campaign):
    actor = rn.actor_record("t", source="test")
    specs = rn.slot_specs("R0-P", actor)
    assert len({s.session_id for s in specs}) == len(specs)
    assert all(s.request_cap == 2 for s in specs)
    assert {s.request_cap for s in rn.slot_specs("R0-D-E", actor)} == {24}


def test_actor_record_carries_machine_checkable_provenance():
    rec = rn.actor_record("me", source="cli", extra={"argv": ["cli", "dry-run"]})
    assert rec["source"] == "cli" and rec["uid"] == os.getuid()
    assert rec["host"] and rec["pid"] and rec["recorded_at_utc"].endswith("Z")


# ---------------------------------------------------------------- AC23 零付费 e2e
def test_dry_run_covers_all_stages_with_zero_posts(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    report = runner.dry_run()
    assert report["posts"] == 0 and report["paid_usd"] == 0.0
    assert report["simulation_backend"] == "mock"
    assert report["layers_verified"] == []           # 不冒充任何验证
    stages = {b["stage"] for b in report["batching"]}
    assert stages == {"protocol", "development", "validation"}
    assert len(report["slots"]) == 4 + 3 + 6 == 13
    assert all(s["state"] == "COMPLETED" for s in report["slots"])
    assert all(s["steps"][0] == "reserve" and s["steps"][-1] == "report"
               for s in report["slots"])


@pytest.mark.parametrize("point", rn.FAULT_POINTS)
def test_each_stage_can_be_interrupted_and_resumed(campaign, point):
    runner = rn.R0Runner(campaign, faults={point: "test"})
    runner.ensure_campaign()
    first = runner.dry_run()
    interrupted = [s for s in first["slots"] if s["state"] == "INTERRUPTED"]
    assert interrupted, point
    assert all(point in s["interrupted_at"] for s in interrupted)
    assert first["posts"] == 0

    healed = rn.R0Runner(campaign)
    second = healed.dry_run()
    assert all(s["state"] == "COMPLETED" for s in second["slots"])
    assert any(s.get("resume_from") == "INTERRUPTED" for s in second["slots"])
    assert second["posts"] == 0


def test_resume_does_not_redo_completed_slots(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    first = runner.dry_run()
    stamps = {s["trial_id"]: s["updated_at_utc"] for s in first["slots"]}
    second = runner.dry_run()
    assert all(s.get("resumed") for s in second["slots"])
    assert {s["trial_id"]: s["updated_at_utc"] for s in second["slots"]} == stamps


def test_dry_run_marks_its_own_evidence_boundary(campaign):
    """报告必须自陈边界——把 mock 闭环当能力证据就是造假。"""
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    report = runner.dry_run()
    assert report["evidence_scope"] == "control_flow_only"
    assert all(s["verdict"]["mock"] for s in report["slots"])
    doc = json.loads((campaign / "closeout" / "dryrun_report.json").read_text())
    assert doc["report_sha256"] == report["report_sha256"]


def test_unknown_sent_blocks_the_paid_gate(campaign):
    """发网事实账里有一条 UNKNOWN_SENT ⇒ preflight 不 ok、G-P 断言必拒。"""
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    from orchestrator.common import runtime_paths as rp
    from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
    journal = AttemptJournal(rp.rsi_r0_attempts_path(),
                             BudgetReservations(ResourceCaps(),
                                                rp.rsi_r0_reservations_dir()))
    rec = journal.reserve(slot_id="P1", batch_id="R0-P", task_id=0,
                          stage="protocol", tokens_in=100, tokens_out=100, usd=0.01)
    journal.mark_sent(rec, seq=1)
    pre = runner.preflight()
    assert pre["no_unknown_attempts"] is False
    assert "UNKNOWN_SENT" in pre["unknown_detail"]
    with pytest.raises(gates.GateRefused, match="no_unknown_attempts"):
        runner.require_gate("G-P")
    assert runner.gate_facts("G-P")["no_unknown_attempts"] is False
    # preflight 的恢复只把 SENT 补成 UNKNOWN_SENT 终态事件：不重发、不释放预留
    latest = journal.latest_by_request()[rec.request_id]
    assert latest["state"] == "UNKNOWN_SENT" and latest["attempts_http"] == 1
    assert journal.recover()["count"] == 0          # 已核销过，再恢复不产生新事件
    with pytest.raises(RuntimeError, match="UNKNOWN_SENT"):
        journal.assert_no_unknown()                 # 未人工核销前始终挡付费批次


# ---------------------------------------------------------------- AC24 门与批准件
def test_gate_lists_every_missing_condition():
    verdict = gates.evaluate_gate("G-P", {})
    assert verdict["passed"] is False
    assert set(verdict["missing"]) == {c.key for c in gates.GATE_P}
    with pytest.raises(gates.GateRefused, match="G-P 未通过"):
        gates.assert_gate("G-P", {})


def test_gate_passes_only_when_every_condition_holds():
    facts = {c.key: True for c in gates.GATE_P}
    verdict = gates.evaluate_gate("G-P", facts)
    assert verdict["passed"] and verdict["missing"] == []
    facts["criteria_frozen"] = False
    assert gates.evaluate_gate("G-P", facts)["missing"] == ["criteria_frozen"]


def test_gate_definition_hash_changes_with_the_condition_list(monkeypatch):
    before = gates.gate_definition_hash("G-P")
    monkeypatch.setattr(gates, "GATE_P", gates.GATE_P + (
        gates.GateCondition("extra", "新增条件"),))
    assert gates.gate_definition_hash("G-P") != before


def test_approval_artifact_is_write_once(tmp_path):
    path = tmp_path / "approvals" / "R0-P.json"
    body = gates.approval_body(batch_id="R0-P", task_cap=6, hard_cap=222,
                               currency="USD", usd_ceiling=100.0,
                               approved_by="user", approved_at_utc="2026-11-08T00:00:00Z",
                               scope="protocol")
    first = gates.write_approval(path, body)
    assert first["created"] is True
    again = gates.write_approval(path, body)
    assert again["created"] is False and again["sha256"] == first["sha256"]
    loosened = gates.approval_body(batch_id="R0-P", task_cap=99, hard_cap=999,
                                   currency="USD", usd_ceiling=999.0,
                                   approved_by="user",
                                   approved_at_utc="2026-11-08T00:00:00Z",
                                   scope="protocol")
    with pytest.raises(gates.ApprovalError, match="拒绝覆盖"):
        gates.write_approval(path, loosened)
    assert json.loads(path.read_text())["task_cap"] == 6      # 原件未被改


def test_approval_self_hash_and_required_fields_are_checked(tmp_path):
    path = tmp_path / "a.json"
    body = gates.approval_body(batch_id="R0-P", task_cap=6, hard_cap=222,
                               currency="USD", usd_ceiling=100.0,
                               approved_by="user", approved_at_utc="2026-11-08T00:00:00Z",
                               scope="protocol")
    gates.write_approval(path, body)
    doc = json.loads(path.read_text())
    doc["task_cap"] = 999
    path.write_text(json.dumps(doc))
    with pytest.raises(gates.ApprovalError, match="自哈希不符"):
        gates.read_approval(path)
    with pytest.raises(gates.ApprovalError, match="缺字段"):
        gates.approval_body(batch_id="R0-P")
    assert gates.approval_ok(path, batch_id="R0-P") is False
    assert gates.approval_ok(tmp_path / "nope.json", batch_id="R0-P") is False


# ---------------------------------------------------------------- 快照锁
def test_lock_is_written_once_and_verified(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    snap = runner.write_lock()
    assert runner.write_lock()["lock_sha256"] == snap["lock_sha256"]     # 幂等
    assert runner.verify_lock()["ok"] is True
    doc = json.loads(runner.lock_path().read_text())
    doc["criteria_hash"] = "0" * 64
    runner.lock_path().write_text(json.dumps(doc))
    verdict = runner.verify_lock()
    assert verdict["ok"] is False and "criteria_hash" in verdict["drift"]
    with pytest.raises(rn.RunnerError, match="拒绝覆盖"):
        runner.write_lock()


def test_lock_snapshot_covers_code_config_and_criteria(campaign):
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    snap = runner.lock_snapshot()
    assert snap["image"].startswith("sim-praxis/sandbox@sha256:")
    assert snap["code_set_sha256"] == rn.sha256_of(snap["code"])
    assert {"contracts", "evaluator", "controls", "runner", "gates"} <= set(snap["code"])
    assert set(snap["gates"]) == {"G-M", "G-P", "G-V"}
    assert snap["slot_request_cap"] == 24
    assert snap["task_caps"] == {"E": 1, "D": 2, "R": 3}


# ---------------------------------------------------------------- CLI
def test_cli_preflight_dryrun_lock_closeout_end_to_end(campaign, capsys):
    assert cli_main(["preflight", "--campaign", str(campaign), "--prepare"]) == EXIT_REFUSED
    assert cli_main(["dry-run", "--campaign", str(campaign)]) == EXIT_OK
    assert cli_main(["lock", "--campaign", str(campaign)]) == EXIT_OK
    assert cli_main(["lock", "--campaign", str(campaign), "--verify"]) == EXIT_OK
    assert cli_main(["closeout", "--campaign", str(campaign)]) == EXIT_OK
    out = capsys.readouterr().out.strip().splitlines()
    report = json.loads(out[-1])["report"]
    assert report["primary_verdict"] == "ABORTED_INCOMPLETE"
    assert report["dryrun_posts"] == 0
    assert report["evidence_scope"] == "zero_paid_only"


def test_cli_refuses_paid_paths_without_the_env_guard(campaign, capsys, monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    for cmd in ("protocol", "develop", "validate"):
        assert cli_main([cmd, "--campaign", str(campaign)]) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "SIMPRAXIS_ALLOW_PAID" in payload["refused"]


def test_cli_paid_path_still_refuses_when_gates_are_missing(campaign, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert cli_main(["protocol", "--campaign", str(campaign)]) == EXIT_REFUSED


def test_cli_controls_reports_the_frozen_matrix(campaign, capsys):
    assert cli_main(["controls", "--campaign", str(campaign)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["negatives"]["passed"] == payload["negatives"]["total"] == 21
    assert payload["mutations"]["killed"] == payload["mutations"]["total"] == 6
    assert payload["criteria_matches_frozen"] is True
    frozen = json.loads((campaign / "protocol" / "t4_controls_latest.json").read_text())
    assert frozen["report_sha256"] == payload["report_sha256"]


def test_cli_rejects_a_relative_campaign(capsys):
    assert cli_main(["preflight", "--campaign", "rel/c1"]) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "绝对路径" in payload["error"]
