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


def _mutated(monkeypatch, mutate):
    """在快照的 code 字典上做任意改动（加键/删键/改非包内模块）。"""
    real = rn.R0Runner.lock_snapshot

    def fake(self):
        snap = real(self)
        code = dict(snap["code"])
        mutate(code)
        return {**snap, "code": code}
    monkeypatch.setattr(rn.R0Runner, "lock_snapshot", fake)


def test_instrument_repair_refuses_a_scripts_surface_change(bare_runner, monkeypatch):
    """`scripts.rsi_r0.*` 产出的是门要读的产物：改它换锁 = 改实验，不是修仪器。

    第三份评审的 BLOCKER：旧冻结名单只列举包内 11 个模块，五个脚本明明被
    `lock_snapshot()` 声明为实验定义却不在名单里——改 `t3_controls.py` 造一份
    21/0 报告再换锁可以一路通行。现在冻结面是「白名单求补集」，必须拒。
    """
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _mutated(monkeypatch,
             lambda code: code.__setitem__("scripts.rsi_r0.t3_controls", "f" * 64))
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock("假装修仪器，实际改控制矩阵生成脚本（单测）",
                                   instrument_repair=True)
    assert bare_runner.lock_path().exists()


def test_instrument_repair_refuses_an_added_code_key(bare_runner, monkeypatch):
    """新增入锁键一律拒：新键不在白名单里，等于在花钱后拓展实验定义面。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _mutated(monkeypatch, lambda code: code.__setitem__("scripts.rsi_r0.t9_new", "f" * 64))
    with pytest.raises(rn.RunnerError, match="新增键"):
        bare_runner.supersede_lock("换锁时顺手把一个新脚本塞进 LOCK（单测）",
                                   instrument_repair=True)


def test_instrument_repair_refuses_a_removed_code_key(bare_runner, monkeypatch):
    """删键同样拒：把判据/事实面模块从 LOCK 里摘掉，等于让它以后不再被校验。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _mutated(monkeypatch, lambda code: code.pop("gates", None))
    with pytest.raises(rn.RunnerError, match="删除键"):
        bare_runner.supersede_lock("换锁时把门定义从 LOCK 里摘掉（单测）",
                                   instrument_repair=True)


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
    # 回滚要连旧锁副本一起撤：不然归档名被永久占住，重试只会撞「归档件已存在」。
    assert not (bare_runner.lock_path().parent
                / f"t4_lock.superseded.{old_sha[:8]}.json").exists()


def test_instrument_repair_refuses_to_archive_a_review_bound_to_another_lock(
        bare_runner, monkeypatch):
    """只归档确实绑在旧锁上的评审件：来源不明的评审件归档进来 = 给它伪造历史。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    bare_runner.write_independent_review(
        verdict="APPROVE", reviewer_lane="lane-x", reviewed_commit="deadbeef",
        findings={"major": 0, "critical": 0},
        basis="旧代码集上的独立评审（单测夹具）——故意把绑定改成另一把锁。")
    path = bare_runner.review_path()
    doc = json.loads(path.read_text())
    doc["reviewed_lock_sha256"] = "0" * 64
    doc["review_sha256"] = rn.sha256_of({k: v for k, v in doc.items()
                                         if k != "review_sha256"})
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    _bumped(monkeypatch, "develop")
    with pytest.raises(rn.RunnerError, match="没有绑定当前这把锁"):
        bare_runner.supersede_lock(
            "换锁时试图归档一份绑在别的锁上的旧评审件，必须被拒（单测）",
            instrument_repair=True)
    assert path.exists()                                 # 评审件原封不动


def test_instrument_repair_archives_the_stale_independent_review(bare_runner, monkeypatch):
    """评审件写一次即锁：不作废旧件，就永远没法为新代码集落评审（G-P 会卡死）。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    bare_runner.write_independent_review(
        verdict="APPROVE", reviewer_lane="lane-x", reviewed_commit="deadbeef",
        findings={"major": 0, "critical": 0},
        basis="旧代码集上的独立评审（单测夹具）——换锁后必须作废归档。")
    old_review = json.loads(bare_runner.review_path().read_text())
    _bumped(monkeypatch, "develop")
    out = bare_runner.supersede_lock(
        "修仪器换锁会作废旧评审件，须归档后才能落新评审（单测）", instrument_repair=True)
    assert out["new_lock_sha256"]
    assert not bare_runner.review_path().exists()
    archived = bare_runner.review_path().parent / (
        f"independent_review.superseded.{old_review['review_sha256'][:8]}.json")
    assert json.loads(archived.read_text())["review_sha256"] == old_review["review_sha256"]
    # 归档后新评审件可写（内容不同不再被「已存在」挡住），且绑定新代码集。
    fresh = bare_runner.write_independent_review(
        verdict="APPROVE", reviewer_lane="lane-x", reviewed_commit="cafe1234",
        findings={"major": 0, "critical": 0},
        basis="新代码集上的独立评审（单测夹具）——旧件已归档，写新件不该被拦。")
    assert fresh["reviewed_code_set_sha256"] == bare_runner.lock_snapshot()["code_set_sha256"]


# ---------------------------------------------------------------- 协议修订通道
# 2026-11-09 用户逐字授权：T6 实跑暴露 R 族 L5/C1 结构性不可达（判据要求
# `prereg_sha256`，v1 协议动作集里没有「登记计划」动作）。修它必须改协议本体，
# 即「改实验定义后重锁」。通道只认两个键、必须附用户授权原文、其余冻结面照旧。
AUTHORIZATION = ("2026-11-09 用户裁决「真修协议：加登记计划动作」——"
                 "授权修订 develop 协议并在付费后重锁（单测夹具原文）")


def _protocol_amended(monkeypatch, mutate):
    """模拟协议本体被修订：快照里的 develop_protocol 两键随之改变。"""
    from orchestrator.rsi_r0.contracts import sha256_of
    real = rn.R0Runner.lock_snapshot

    def fake(self):
        snap = real(self)
        spec = dict(snap["develop_protocol"])
        mutate(spec)
        return {**snap, "develop_protocol": spec,
                "develop_protocol_sha256": sha256_of(spec)}
    monkeypatch.setattr(rn.R0Runner, "lock_snapshot", fake)


def _history(runner) -> list[dict]:
    return [json.loads(x) for x in (runner.root / "protocol" / "lock_history.jsonl")
            .read_text(encoding="utf-8").splitlines()]


def test_protocol_amendment_requires_the_instrument_repair_channel(
        bare_runner, monkeypatch):
    """改实验定义后重锁不能伪装成常规换锁：必须显式走仪器修复通道。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _protocol_amended(monkeypatch, lambda s: s.__setitem__("schema", "v9"))
    with pytest.raises(rn.RunnerError, match="instrument_repair=True"):
        bare_runner.supersede_lock(
            "协议修订必须在仪器修复通道上显式登记，不能伪装成常规换锁（单测）",
            amendment=("develop_protocol", "develop_protocol_sha256"),
            amendment_authorization=AUTHORIZATION)
    assert bare_runner.lock_path().exists()


def test_protocol_amendment_requires_the_authorization_text(bare_runner, monkeypatch):
    """没有用户授权原文就写不出审计行：换锁前一律拒。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _protocol_amended(monkeypatch, lambda s: s.__setitem__("schema", "v9"))
    with pytest.raises(rn.RunnerError, match="授权原文"):
        bare_runner.supersede_lock(
            "协议修订必须附用户授权原文，否则审计行写不出来（单测）", instrument_repair=True,
            amendment=("develop_protocol",), amendment_authorization="太短")
    assert bare_runner.lock_path().exists()


def test_protocol_amendment_refuses_any_other_frozen_field(bare_runner, monkeypatch):
    """修订通道只认两个协议键：想把判据面一起"顺带修订"一律拒。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _protocol_amended(monkeypatch, lambda s: s.__setitem__("schema", "v9"))
    with pytest.raises(rn.RunnerError, match="只认"):
        bare_runner.supersede_lock(
            "试图把判据面也一起塞进协议修订通道，这属于改实验（单测）", instrument_repair=True,
            amendment=("develop_protocol", "criteria_hash"),
            amendment_authorization=AUTHORIZATION)
    assert bare_runner.lock_path().exists()


def test_protocol_amendment_still_refuses_a_judgment_surface_change(
        bare_runner, monkeypatch):
    """豁免的只有协议本体：同一次换锁里动评测器照样整单拒。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _protocol_amended(monkeypatch, lambda s: s.__setitem__("schema", "v9"))
    _bumped(monkeypatch, "evaluator")
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock(
            "协议修订顺手改评测器就是改实验，必须整单拒（单测）", instrument_repair=True,
            amendment=("develop_protocol", "develop_protocol_sha256"),
            amendment_authorization=AUTHORIZATION)
    assert bare_runner.lock_path().exists()


def test_protocol_amendment_records_the_authorization_and_new_values(
        bare_runner, monkeypatch):
    """放行时审计行必须能第三方复核：改了哪两键、授权原文、前后取值。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    old_spec = json.loads(bare_runner.lock_path().read_text())["develop_protocol"]
    _protocol_amended(monkeypatch, lambda s: s.__setitem__("schema", "v9"))

    # 不带修订的同一份快照必须被冻结面拒掉——证明放行确实来自这次授权。
    with pytest.raises(rn.RunnerError, match="实验定义面"):
        bare_runner.supersede_lock(
            "协议本体已改但没走修订通道（单测应被拒）", instrument_repair=True)

    out = bare_runner.supersede_lock(
        "T6 实跑暴露 R 族 L5/C1 结构性不可达，按用户授权加登记计划动作后重锁（单测）",
        instrument_repair=True,
        amendment=("develop_protocol", "develop_protocol_sha256"),
        amendment_authorization=AUTHORIZATION)
    assert out["amendment_fields"] == ["develop_protocol", "develop_protocol_sha256"]
    last = _history(bare_runner)[-1]
    assert last["amendment_authorization"] == AUTHORIZATION.strip()
    assert last["amendment_fields"] == ["develop_protocol", "develop_protocol_sha256"]
    assert last["amendment_values"]["develop_protocol"]["old"] == old_spec
    assert last["amendment_values"]["develop_protocol"]["new"]["schema"] == "v9"
    assert last["frozen_fields_checked"]                       # 其余冻结面照旧逐项校验
    assert bare_runner.verify_lock()["ok"] is True


def test_protocol_amendment_refuses_a_declared_but_unchanged_field(
        bare_runner, monkeypatch):
    """没变却声明"被修订"= 审计造假：宁可拒，不许在 lock_history 记假账。"""
    bare_runner.ensure_campaign()
    bare_runner.write_lock()
    _spend_one(bare_runner)
    _bumped(monkeypatch, "develop")          # 只动接线模块，协议本体没变
    with pytest.raises(rn.RunnerError, match="并未变化"):
        bare_runner.supersede_lock(
            "声明协议被修订但其实没动，属于在审计行记假账（单测）",
            instrument_repair=True,
            amendment=("develop_protocol", "develop_protocol_sha256"),
            amendment_authorization=AUTHORIZATION)
    assert bare_runner.lock_path().exists()
