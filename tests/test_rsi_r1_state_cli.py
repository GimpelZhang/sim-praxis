"""RSI-R1 T6：``cli state`` root 推进入口的前置条件与幂等性。

背景（诚实记录）：T0/T1/T5 的产物面当初都由各 Task 的 CLI 落件，但**没有任何
操作者入口把 root 从 INIT 推到 FROZEN**——状态机只在协议链 PASS 时推进
（``protocol.advance_root``），于是活 campaign 的 root 恒为 INIT，T6 的
``ROOT_GATE`` 永远过不去。本文件钉死补上的入口：

* 只允许 AUTHORIZED / CALIBRATED / FROZEN 三步（PROBED 起只能由协议链或
  研究闭环推进，操作者无权代劳）；
* 每一步都有**语义前置条件**且 fail-closed：AUTHORIZED 要
  ``envelope.paid_authorized is true`` 且 consent 字节 sha 与 envelope 登记
  一致；CALIBRATED 要校准报告判决 ``CALIBRATION_PASS``；FROZEN 要
  ``verify_lock`` 全量复核（闭包/依赖/校准/双签）通过；
* 幂等：已是目标状态时重复调用 rc=0 且不追加第二条事件。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from orchestrator.rsi_r1 import cli as CLI
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import state as S
from tests import rsi_r1_helpers as H
from tests.rsi_r1_rehearsal import REHEARSAL_IMAGE, RehearsalDriver

CAMPAIGN = "t6state"


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RehearsalDriver:
    """把排演 campaign 挂到 env 根上，并补 ``contract/scope.json``。

    ``runtime.load_context`` 只认「已初始化」的 campaign（scope.json 存在），
    所以 CLI 路径测试必须补这一件；envelope 仍是排演件，按需在测试里改写。
    """
    family = tmp_path / "rsi_r1_family"
    driver = RehearsalDriver(family / CAMPAIGN)
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_ROOT", str(family))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", CAMPAIGN)
    driver.ctx.write_json("contract/scope.json", {
        "schema": "simpraxis.rsi_r1.scope.v1", "schema_version": 1,
        "envelope_id": H.ENVELOPE, "root_state": "INIT"})
    return driver


def _authorize(driver: RehearsalDriver) -> None:
    """写付费授权 + consent 字节，使 AUTHORIZED 前置条件成立。"""
    consent = {"schema": "simpraxis.rsi_r1.consent.v1", "schema_version": 1,
               "envelope_id": H.ENVELOPE, "paid_authorized": True}
    path = driver.ctx.write_json("contract/consent.json", consent)
    doc = driver.ctx.read_json("contract/envelope.json")
    doc["paid_authorized"] = True
    doc["consent_document_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    driver.ctx.write_json("contract/envelope.json", doc)


def _advance(driver: RehearsalDriver, to: str, reason: str = "") -> int:
    argv = ["state", "--envelope", H.ENVELOPE, "--to", to]
    if reason:
        argv += ["--reason", reason]
    return CLI.main(argv)


def _root(driver: RehearsalDriver) -> str:
    return S.ResearchState(driver.ctx).root_state()


def _events(driver: RehearsalDriver) -> "list[Any]":
    return [ev for ev in S.Journal(driver.ctx).events()
            if ev.event == "root_transition"]


def test_authorized_requires_paid_authorization_and_consent_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env(tmp_path, monkeypatch)
    # 排演 envelope 没有 paid_authorized ⇒ 拒绝（rc=2，状态机不动）
    assert _advance(driver, "AUTHORIZED") == 2
    assert _root(driver) == S.ROOT_INIT
    # 有 paid_authorized，但 consent 字节 sha 与登记不符 ⇒ 拒绝
    _authorize(driver)
    doc = driver.ctx.read_json("contract/envelope.json")
    doc["consent_document_sha256"] = "0" * 64
    driver.ctx.write_json("contract/envelope.json", doc)
    assert _advance(driver, "AUTHORIZED") == 2
    assert _root(driver) == S.ROOT_INIT
    # 修正登记后通过
    _authorize(driver)
    assert _advance(driver, "AUTHORIZED", reason="T0 envelope 授权补记") == 0
    assert _root(driver) == S.ROOT_AUTHORIZED
    # 幂等：重复调用 rc=0，且不追加第二条事件
    assert _advance(driver, "AUTHORIZED") == 0
    assert len(_events(driver)) == 1


def test_calibrated_requires_pass_report(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env(tmp_path, monkeypatch)
    _authorize(driver)
    assert _advance(driver, "AUTHORIZED") == 0
    # 非 PASS 判决 ⇒ 拒绝
    driver.ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r1.calibration_report.v2", "schema_version": 2,
        "verdict": "DESIGN_NOT_READY", "design_version": 2})
    assert _advance(driver, "CALIBRATED") == 2
    assert _root(driver) == S.ROOT_AUTHORIZED
    # PASS ⇒ 通过
    driver.ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r1.calibration_report.v2", "schema_version": 2,
        "verdict": "CALIBRATION_PASS", "design_version": 2,
        "container_image": REHEARSAL_IMAGE})
    assert _advance(driver, "CALIBRATED", reason="T1b-v2 CALIBRATION_PASS 补记") == 0
    assert _root(driver) == S.ROOT_CALIBRATED


def test_frozen_requires_verified_double_signed_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env(tmp_path, monkeypatch)
    _authorize(driver)
    driver.ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r1.calibration_report.v2", "schema_version": 2,
        "verdict": "CALIBRATION_PASS", "design_version": 2,
        "container_image": REHEARSAL_IMAGE})
    driver.ctx.write_json("contract/instrument_prereg.json", {
        "schema": "simpraxis.rsi_r1.rehearsal_prereg.v1", "schema_version": 1,
        "rehearsal": True})
    assert _advance(driver, "AUTHORIZED") == 0
    assert _advance(driver, "CALIBRATED") == 0
    # 没有 LOCK ⇒ verify_lock 抛 LOCK_MISSING（BLOCKED rc=2），root 不动
    assert _advance(driver, "FROZEN") == 2
    assert _root(driver) == S.ROOT_CALIBRATED
    # 冻结 + 双签 + 复核（与 T5 排演同法）后，CLI 推进入口才放行
    LK.freeze_lock(driver.ctx, repo_root=LK.REPO_ROOT, image_pin=REHEARSAL_IMAGE)
    LK.sign_lock(driver.ctx, role="reviewer", actor="state-reviewer",
                 decision="APPROVE", rationale="state cli 测试")
    LK.sign_lock(driver.ctx, role="verifier", actor="state-verifier",
                 decision="APPROVE", rationale="state cli 测试")
    assert _advance(driver, "FROZEN", reason="LOCK 双签通过") == 0
    assert _root(driver) == S.ROOT_FROZEN
    assert LK.verify_lock(driver.ctx)["ok"] is True


def test_operator_cannot_skip_states_or_advance_past_frozen(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _env(tmp_path, monkeypatch)
    _authorize(driver)
    # INIT → CALIBRATED 是跳步：StateError(STATE_SKIP) ⇒ BLOCKED rc=2
    assert _advance(driver, "CALIBRATED") == 2
    assert _root(driver) == S.ROOT_INIT
    # PROBED 不在操作者选项内：argparse 直接拒绝（SystemExit(2)）
    with pytest.raises(SystemExit) as excinfo:
        CLI.main(["state", "--envelope", H.ENVELOPE, "--to", "PROBED"])
    assert excinfo.value.code == 2
    assert _root(driver) == S.ROOT_INIT


def test_watch_command_is_observable_and_leaves_teardown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC24：外部监护者能起、能观测对象、能收尾（teardown 与 operator 件）。"""
    from orchestrator.rsi_r1 import watchdog as W

    driver = _env(tmp_path, monkeypatch)
    assert CLI.main(["watch", "--envelope", H.ENVELOPE,
                     "--poll", "0.01", "--max-ticks", "2"]) == 0
    op = driver.ctx.read_json("state/watchdog_operator.json")
    assert op["campaign_root"] == str(driver.ctx.root)
    assert op["ledger_path"] == str(driver.ctx.root / "quota" / "ledger.jsonl")
    assert op["pause_path"] == str(driver.ctx.root / "state" / "pause.json")
    watch = W.watcher_state(driver.ctx.relative("state"))
    assert watch is not None and watch["pid"] == os.getpid()
    teardown = W.teardown_doc(driver.ctx.relative("state"))
    assert teardown["reason"] == "normal" and teardown["ticks"] == 2
    # 没有心跳不等于立即违约：两 tick 内不应写暂停件
    assert W.read_pause(driver.ctx.relative("state")) is None


def test_state_doc_records_mirrored_root_state_on_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """推进事件必须真落 append-only 日志（重开 campaign 仍读得到）。"""
    driver = _env(tmp_path, monkeypatch)
    _authorize(driver)
    assert _advance(driver, "AUTHORIZED") == 0
    journal = driver.ctx.relative(S.JOURNAL_RELPATH)
    assert journal.is_file()
    rows = [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["payload"]["to"] for r in rows if r["event"] == "root_transition"] \
        == [S.ROOT_AUTHORIZED]
