"""AC31 对账与付费路径接线的回归网（评审 N-2 / N-3 要求的测试缺口）。

全离线：不联网、不付费、不启容器。覆盖：

* 空账（未开跑）与正常成对；
* 删 attempts / 删账本 / **三份一起删但报告记过 seq** 三种抹账路径；
* seq 复用（旧版 ``setdefault`` 静默丢弃第二条）；
* blocked preflight 逐 ``(stage, task, batch_id)`` 配对；
* ``supersede_lock`` 在「账本有付费 call 行而 attempts 被删」时被卡住；
* 付费路径真的抢 campaign 锁（不是只有锁原语有测试）；
* 协议报告写一次：异内容拒写。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import protocol_probe as pp
from orchestrator.rsi_r0 import reconcile as rc
from orchestrator.rsi_r0 import runner as rn
from orchestrator.rsi_r0.attempts import (
    AttemptJournal,
    BudgetReservations,
    ConcurrentCampaignError,
    campaign_lock,
)
from orchestrator.rsi_r0.contracts import ResourceCaps

BATCH = "R0-P"


# ---------------------------------------------------------------- 世界
def _world(tmp_path):
    """账本 / attempts / 预留三件套（都在 tmp，绝不触真实 campaign）。"""
    quota = tmp_path / "quota"
    quota.mkdir(parents=True, exist_ok=True)
    ledger = quota / "ledger.jsonl"
    reservations = BudgetReservations(ResourceCaps(), quota / "resource_reservations")
    journal = AttemptJournal(quota / "attempts.jsonl", reservations)
    return ledger, journal, reservations


def _ledger_call(ledger, seq: int, *, batch: str = BATCH, task: int = 1) -> None:
    row = {"seq": seq, "event": "call", "ok": True, "stage": "macro_ideation",
           "task": task, "batch_id": batch, "ts_iso": "2026-11-08T00:00:00Z"}
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _ledger_blocked(ledger, *, batch: str, task: int) -> None:
    row = {"seq": None, "event": "preflight_blocked", "ok": False,
           "stage": "macro_ideation", "task": task, "batch_id": batch,
           "ts_iso": "2026-11-08T00:00:00Z"}
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _settled(journal, *, seq: int, batch: str = BATCH, task: int = 1):
    rec = journal.reserve(slot_id=f"{batch}-01", batch_id=batch, task_id=task,
                          stage="macro_ideation", tokens_in=10, tokens_out=5,
                          usd=0.01)
    journal.mark_sent(rec, seq=seq)
    journal.settle_ok(rec, usage_in=10, usage_out=5, stop_reason="end_turn",
                      block_types=("text",), provider_request_id="pr_1",
                      body_sha256="b" * 64, usd=0.01)
    return rec


def _blocked(journal, *, batch: str = BATCH, task: int = 1):
    rec = journal.reserve(slot_id=f"{batch}-02", batch_id=batch, task_id=task,
                          stage="macro_ideation", tokens_in=10, tokens_out=5,
                          usd=0.01)
    journal.block_preflight(rec, reason="paused")
    return rec


def _drift(report) -> set[str]:
    return {k for k, v in report["drift"].items() if v}


# ---------------------------------------------------------------- 基本形态
def test_empty_campaign_is_ok_because_nothing_ever_ran(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is True and _drift(report) == set()
    assert report["files_present"]["ledger"] is False


def test_settled_pair_reconciles_clean(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is True, report["why"]
    assert report["ledger"]["seqs"] == [1]


# ---------------------------------------------------------------- 抹账
def test_deleting_attempts_is_detected(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    journal.path.unlink()
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is False
    # 账本那条 call 找不到 attempt；它的预留文件还在 → 同时报 reservation_without_attempt。
    assert _drift(report) == {"ledger_seq_without_attempt",
                              "reservation_without_attempt"}


def test_deleting_ledger_is_detected(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    ledger.unlink()
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is False
    assert _drift(report) == {"attempt_seq_without_ledger"}


def test_all_three_deleted_is_caught_by_the_report_seqs(tmp_path):
    """三份权威一起删 → 旧版判真（"未开跑"与"抹账"不可区分）；报告里的 seq 抓得住。"""
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    blind = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert blind["ok"] is True          # 如实记录：没有第四份痕迹时确实看不出来
    ledger.unlink()
    journal.path.unlink()
    for path in (tmp_path / "quota" / "resource_reservations").glob("*.json"):
        path.unlink()
    caught = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations,
                          report_seqs=[("protocol", [1])])
    assert caught["ok"] is False
    assert _drift(caught) == {"report_seq_without_ledger"}
    assert caught["drift"]["report_seq_without_ledger"] == [{"source": "protocol", "seq": 1}]


# ---------------------------------------------------------------- seq 复用
def test_two_attempts_claiming_one_seq_are_detected(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _settled(journal, seq=1)            # 复用一个 seq：旧版 setdefault 会静默吞掉
    _ledger_call(ledger, 1)
    report = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert report["ok"] is False
    assert "duplicate_seq" in _drift(report)
    assert report["drift"]["duplicate_seq"][0]["seq"] == 1


# ---------------------------------------------------------------- 预留与 blocked
def test_attempt_without_reservation_is_detected(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    rec = _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    reservations.path_for(rec.request_id).unlink()
    report = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert report["ok"] is False
    assert _drift(report) == {"attempt_without_reservation"}


def test_blocked_preflight_pairs_on_identity_not_just_count(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _blocked(journal, batch="R0-D-E", task=1)
    _ledger_blocked(ledger, batch="R0-V-E1", task=2)      # 个数相同、批次不同
    report = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert report["ok"] is False
    assert _drift(report) == {"blocked_preflight_mismatch"}
    mismatch = report["drift"]["blocked_preflight_mismatch"]
    assert mismatch["ledger_only"] and mismatch["attempts_only"]


def test_blocked_preflight_matching_identity_is_ok(tmp_path):
    ledger, journal, reservations = _world(tmp_path)
    _blocked(journal, batch="R0-D-E", task=1)
    _ledger_blocked(ledger, batch="R0-D-E", task=1)
    report = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert report["ok"] is True, report["why"]


# ---------------------------------------------------------------- supersede 守卫
@pytest.fixture()
def bare_runner(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r0"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "bare")
    yield rn.R0Runner(root / "bare")
    for var in ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN"):
        monkeypatch.delenv(var, raising=False)


def test_supersede_is_allowed_when_nothing_was_spent(bare_runner):
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    out = bare_runner.supersede_lock("封存环换期需要把新脚本纳入代码集（单测）")
    assert out["new_lock_sha256"]


def test_supersede_is_blocked_when_the_ledger_shows_a_paid_call(bare_runner, tmp_path):
    """评审 MAJOR-5 残留的复现：账本有付费 call 行 + attempts.jsonl 被删。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _ledger_call(rp_ledger := bare_runner.root / "quota" / "ledger.jsonl", 1)
    assert rp_ledger.exists()
    attempts = bare_runner.root / "quota" / "attempts.jsonl"
    attempts.unlink(missing_ok=True)
    ok, detail = bare_runner.reconciled()
    assert ok is False and detail["drift"]["ledger_seq_without_attempt"] == [1]
    with pytest.raises(rn.RunnerError, match="对账"):
        bare_runner.supersede_lock("试图用删 attempts 的办法绕过发网守卫（单测）")


# ---------------------------------------------------------------- 付费路径接线
def test_protocol_run_really_takes_the_campaign_lock(campaign, monkeypatch):
    from tests.test_rsi_r0_protocol import _arm as arm_protocol

    runner = arm_protocol(campaign)
    probe = pp.ProtocolProbe(runner, _SilentGateway())
    with campaign_lock(rp_quota := runner.root / "quota"):
        assert rp_quota.exists()
        with pytest.raises(ConcurrentCampaignError):
            probe.run()
    assert probe._used == 0


def test_validation_run_really_takes_the_campaign_lock(campaign, monkeypatch):
    from orchestrator.rsi_r0 import validate as val
    from tests.test_rsi_r0_validate import _arm as arm_validate
    from tests.test_rsi_r0_develop import FakeGateway, _factory

    runner = arm_validate(campaign, monkeypatch)
    with campaign_lock(runner.root / "quota"):
        with pytest.raises(ConcurrentCampaignError):
            val.run_validation(runner, FakeGateway([]), session_factory=_factory())


def test_report_is_write_once(campaign, monkeypatch):
    from tests.test_rsi_r0_protocol import _arm as arm_protocol

    runner = arm_protocol(campaign)
    first = {"schema": pp.REPORT_SCHEMA, "verdict": "PROTOCOL_BLOCKED"}
    pp.write_report(first)
    assert pp.write_report(first).exists()            # 同内容幂等
    with pytest.raises(pp.ProbeError, match="拒绝覆盖"):
        pp.write_report({**first, "verdict": "PROTOCOL_OK"})


class _SilentGateway:
    """被锁挡住时一次都不该被调用；真被调用就直接失败，免得分不清是锁还是别的。"""

    def call(self, *a, **kw):  # pragma: no cover —— 正常路径不会走到
        raise AssertionError("持锁冲突时不应发网")


# ---------------------------------------------------------------- 账行认领
def test_new_row_prefers_this_batch_over_the_last_line(campaign):
    """MAJOR-4 的回归网：并发执行器时不能无条件拿 ``rows[-1]``。"""
    assert campaign.name == "t-recon"           # 夹具确实把 env 指到 tmp
    ledger = rp.rsi_r0_ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"seq": 1, "event": "call", "batch_id": "R0-X"},
            {"seq": 2, "event": "call", "batch_id": pp.PROTOCOL_BATCH},
            {"seq": 3, "event": "call", "batch_id": "R0-X"}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    picked = pp.ProtocolProbe._new_row(1)
    assert picked is not None and picked["seq"] == 2, picked
    assert pp.ProtocolProbe._new_row(3) is None   # 本次没新行


def test_call_row_with_the_wrong_identity_is_detected(tmp_path):
    """第三份评审 MINOR-2：只按 seq 配对会放过「A 批的账算到 B 批头上」。"""
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1, batch=BATCH, task=1)
    _ledger_call(ledger, 1, batch="R0-OTHER", task=9)      # 同一个 seq，身份不同
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is False
    assert _drift(report) == {"call_identity_mismatch"}
    assert report["drift"]["call_identity_mismatch"][0]["seq"] == 1


def test_history_line_hash_matches_the_previous_line(tmp_path):
    """第三份评审 MINOR-1：新旧哈希必须同口径，否则 ``changed`` 恒为真。"""
    target = tmp_path / "artifact.json"
    history = tmp_path / "history.jsonl"
    payload = {"primary_verdict": "ABORTED_INCOMPLETE", "posts": None}
    rn.write_artifact_with_history(target, payload, history)
    payload2 = {**payload, "primary_verdict": "INVALID_INSTRUMENT"}
    rn.write_artifact_with_history(target, payload2, history)
    lines = [json.loads(x) for x in history.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[1]["previous_sha256"] == lines[0]["sha256"]   # 同口径可衔接
    assert lines[0]["changed"] is False                        # 首写没有旧件
    assert lines[1]["changed"] is True
    rn.write_artifact_with_history(target, payload2, history)   # 同内容再写
    last = json.loads(history.read_text(encoding="utf-8").splitlines()[-1])
    assert last["changed"] is False, "同内容重写不该报成覆盖"


def test_one_sided_identity_fields_are_reported_as_unverified(tmp_path):
    """第三份评审增量意见 i：一边有值、另一边 null，该字段不得静默退出比较。"""
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1, batch=BATCH, task=1)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 1, "event": "call", "ok": True,
                             "ts_iso": "2026-11-08T00:00:00Z"}) + "\n")
    report = rc.reconcile(ledger_path=ledger, journal=journal,
                          reservations=reservations)
    assert report["ok"] is True             # 核不了 ≠ 漂移，但也 ≠ 核过
    assert report["identity_unverified_seqs"] == [1]


def test_history_digest_ignores_a_self_reported_hash(tmp_path):
    """第三份评审增量意见 ii：自报 ``report_sha256`` 与正文不符时按正文重算。"""
    target = tmp_path / "artifact.json"
    history = tmp_path / "history.jsonl"
    payload = {"verdict": "APPROVE", "report_sha256": "f" * 64}
    line = rn.write_artifact_with_history(target, payload, history)
    body = {k: v for k, v in payload.items() if k != "report_sha256"}
    from orchestrator.rsi_r0.contracts import sha256_of

    assert line["sha256"] == sha256_of(body)
    assert line["sha256"] != "f" * 64
    again = rn.write_artifact_with_history(target, payload, history)
    assert again["changed"] is False        # 同正文重写不算覆盖


def test_protocol_report_is_written_while_still_holding_the_lock(campaign, monkeypatch):
    """第三份评审 LOW-1：报告若在锁外写，锁释放到落盘之间有「再发一批」的窗口。"""
    from orchestrator.rsi_r0.cli import EXIT_OK, main as cli_main

    rn.R0Runner(campaign).ensure_campaign()
    runner = rn.R0Runner(campaign)
    probe = pp.ProtocolProbe(runner, _SilentGateway())

    def _row(trial_id: str) -> dict:
        return {"trial_id": trial_id, "passed": True, "checks": {"ok": True},
                "requests": [], "requests_used": 0}

    monkeypatch.setattr(probe, "preconditions", lambda: {
        "model_id": "fake-model", "gate": {"gate": "G-P"},
        "ticket": {"batch_id": "R0-P", "task": 0, "stage": "coding_debug",
                   "approved": 6, "created_at_iso": "2026-11-08T00:00:00Z"},
        "pricing_contract_sha256": "0" * 64})
    monkeypatch.setattr(probe, "trial_plain", _row)
    monkeypatch.setattr(probe, "trial_tool", _row)
    report = probe._run_locked()
    on_disk = json.loads(pp.report_path().read_text(encoding="utf-8"))
    assert on_disk["report_sha256"] == report["report_sha256"]   # 锁内已落盘
    # CLI 的第二次同内容写是幂等的，且再跑一批被报告存在性挡住。
    assert cli_main(["protocol", "--campaign", str(campaign)]) != EXIT_OK
    with pytest.raises(pp.ProbeError, match="一次 campaign 只允许一次"):
        pp.ProtocolProbe(runner, _SilentGateway()).run()


# ---------------------------------------------------------------- 坏行与原子写
def test_corrupt_ledger_line_is_reported_as_drift_not_a_traceback(tmp_path):
    """第三份评审 LOW-3：坏行必须以漂移形式回报，而不是让门抛出 JSONDecodeError。"""
    ledger, journal, reservations = _world(tmp_path)
    _settled(journal, seq=1)
    _ledger_call(ledger, 1)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")
    report = rc.reconcile(ledger_path=ledger, journal=journal, reservations=reservations)
    assert report["ok"] is False
    corrupt = report["drift"]["corrupt_records"]
    assert corrupt and corrupt[0]["source"] == "ledger" and corrupt[0]["line"] == 2


def test_private_write_is_atomic_and_0600(tmp_path):
    """第三份评审 LOW-4：先 0600 建临时件再 replace，不留半截文件与临时件。"""
    from orchestrator.rsi_r0.contracts import private_write

    target = tmp_path / "artifact.json"
    private_write(target, "hello")
    assert (target.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".*tmp*"))
    private_write(target, "world")                       # 覆盖也是原子换名
    assert target.read_text(encoding="utf-8") == "world"


def test_tampered_review_hash_is_not_reused(campaign):
    """第三份评审 LOW-6：幂等分支也要自哈希核验。"""
    runner = rn.R0Runner(campaign)
    runner.ensure_campaign()
    runner.write_lock()
    kw = dict(verdict="APPROVE", reviewer_lane="lane-x", reviewed_commit="deadbeef",
              findings={"major": 0, "critical": 0},
              basis="第三份评审：全量读 diff，结论 APPROVE（单测）。")
    runner.write_independent_review(**kw)
    path = runner.review_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["review_sha256"] = "0" * 64                      # 只动自哈希（volatile 字段）
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(rn.RunnerError, match="自哈希"):
        runner.write_independent_review(**kw)


# ---------------------------------------------------------------- cli gates / review
def test_cli_gates_computes_the_three_gates_and_refuses_when_unresolved(campaign):
    from orchestrator.rsi_r0.cli import EXIT_REFUSED, main as cli_main

    rc_ = cli_main(["gates", "--campaign", str(campaign)])
    assert rc_ == EXIT_REFUSED                      # 空 campaign：三门全不成立
    doc = json.loads((rp.rsi_r0_protocol_dir() / "gates.json").read_text(encoding="utf-8"))
    assert set(doc) >= {"G-M", "G-P", "G-V"}       # 机生成结构，不是手写清单
    assert doc["G-M"]["state"] == "PENDING"        # 未武装：如实记 PENDING 而非通过
    assert doc["G-P"]["missing"]


def test_cli_review_rejects_bad_input_and_locks_the_first_approval(campaign):
    from orchestrator.rsi_r0.cli import EXIT_OK, EXIT_REFUSED, main as cli_main

    rn.R0Runner(campaign).ensure_campaign()
    rn.R0Runner(campaign).write_lock()
    base = ["review", "--campaign", str(campaign), "--verdict", "APPROVE",
            "--lane", "reviewer-3", "--reviewed-commit", "deadbeef"]
    assert cli_main([*base, "--findings", "{not json", "--basis", "x" * 30]) == EXIT_REFUSED
    assert cli_main([*base, "--findings", "{}", "--basis", "太短"]) == EXIT_REFUSED
    assert cli_main([*base, "--findings", '{"major":0,"critical":0}',
                     "--basis", "第三份独立评审：全量读 diff，结论 APPROVE。"]) == EXIT_OK
    ok, detail = rn.R0Runner(campaign).independent_review()
    assert ok is True, detail
    # 写一次即锁：同内容幂等，异内容拒覆盖。
    assert cli_main([*base, "--findings", '{"major":0,"critical":0}',
                     "--basis", "第三份独立评审：全量读 diff，结论 APPROVE。"]) == EXIT_OK
    assert cli_main([*base, "--findings", '{"major":1,"critical":0}',
                     "--basis", "第三份独立评审：全量读 diff，结论 APPROVE。"]) == EXIT_REFUSED


@pytest.fixture()
def campaign(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r0"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "t-recon")
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    yield root / "t-recon"
    for var in ("SIMPRAXIS_RSI_R0_ROOT", "SIMPRAXIS_RSI_R0_CAMPAIGN",
                "SIMPRAXIS_ALLOW_PAID"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- 仪器修复换锁
def _spend_one(runner) -> None:
    """让 campaign 真的有 1 条发网 attempt + 1 条账本 call 行（对账干净）。"""
    journal = AttemptJournal(
        rp.rsi_r0_attempts_path(),
        BudgetReservations(runner.caps, rp.rsi_r0_reservations_dir()))
    _settled(journal, seq=1)
    _ledger_call(runner.root / "quota" / "ledger.jsonl", 1)


def _bumped(monkeypatch, module: str):
    """把快照里某个模块的哈希改掉，模拟"宿主代码变了"。"""
    real = rn.R0Runner.lock_snapshot

    def fake(self):
        snap = real(self)
        return {**snap, "code": {**snap["code"], module: "f" * 64}}
    monkeypatch.setattr(rn.R0Runner, "lock_snapshot", fake)


def test_supersede_after_paid_calls_needs_the_instrument_repair_channel(
        bare_runner, monkeypatch):
    """计划 §「修仪器、全控制重跑、新criteria锁」：花钱后换锁只走显式通道。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _bumped(monkeypatch, "develop")
    reason = "T6 首跑仪器接线缺陷修复（族 task 号与目标点注入），换锁后全控制重跑"
    with pytest.raises(rn.RunnerError, match="instrument-repair"):
        bare_runner.supersede_lock(reason)
    out = bare_runner.supersede_lock(reason, instrument_repair=True)
    assert out["new_lock_sha256"]
    hist = [json.loads(x) for x in (bare_runner.root / "protocol" /
                                    "lock_history.jsonl").read_text(
                                        encoding="utf-8").splitlines()]
    last = hist[-1]
    assert last["instrument_repair"] is True
    assert last["sent_attempts_at_supersede"] == 1
    assert last["ledger_seqs_at_supersede"] == [1]
    assert last["code_keys_changed"] == ["develop"]      # 由代码生成，不信手写
    assert last["frozen_fields_checked"]


def test_instrument_repair_refuses_a_judgment_surface_change(bare_runner, monkeypatch):
    """改评测器/判据/门定义换锁 = 改实验，不是修仪器：一律拒。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _bumped(monkeypatch, "evaluator")
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock(
            "假装是仪器修复，实际改评测器让结果更好看（单测）", instrument_repair=True)
    assert bare_runner.lock_path().exists()              # 旧锁原封不动


def test_instrument_repair_refuses_a_non_code_experiment_field(bare_runner, monkeypatch):
    """行情/镜钉/任务集等非代码字段同样冻结：改了就拒。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    real = rn.R0Runner.lock_snapshot
    monkeypatch.setattr(rn.R0Runner, "lock_snapshot",
                        lambda self: {**real(self), "image": "sha256:" + "0" * 64})
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock("换镜像钉不是修仪器（单测理由要够长才不被长度挡）",
                                   instrument_repair=True)


def test_instrument_repair_rechecks_the_written_lock_for_toctou(bare_runner, monkeypatch):
    """校验用的是校验时刻的快照，落锁用的是随后再算的一份：不等价必须撤新锁放回旧锁。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    old_sha = json.loads(bare_runner.lock_path().read_text())["lock_sha256"]
    real = rn.R0Runner.lock_snapshot
    calls = {"n": 0}

    def drifting(self):
        snap = real(self)
        calls["n"] += 1
        # 第 1 次（校验）只有 develop 变；第 2 次（落锁）连 evaluator 也变了 —— 模拟
        # 校验与落锁之间有人动了判据面。
        code = {**snap["code"], "develop": "f" * 64}
        if calls["n"] >= 2:
            code["evaluator"] = "e" * 64
        return {**snap, "code": code}

    monkeypatch.setattr(rn.R0Runner, "lock_snapshot", drifting)
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock("TOCTOU 复核：落锁前后快照必须等价（单测）",
                                   instrument_repair=True)
    now = json.loads(bare_runner.lock_path().read_text())
    assert now["lock_sha256"] == old_sha        # 旧锁原封放回，没有留下被污染的新锁
