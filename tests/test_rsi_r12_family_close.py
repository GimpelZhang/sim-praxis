"""RSI-R1.2 换轮闸：``family close`` 释放离线验证轮预留（零 POST）。

背景（Plan §5.2/§6.1）：T5 离线验证根率先以 ``registered`` 登记并占用本轮
124 预留；族账「同 profile 单活动成员」因此挡住 T6 付费根的登记
（``FAMILY_ROUND_OPEN``，错误文案本身即「本轮关闭前不得开第二轮」）。
``family close`` 是既定换轮路径：现场复核 validation LOCK（闭包/依赖/
校准/报告三签，``verify_lock``）+ 报告 verdict=PASS → root→CLOSED →
成员标 ``closed`` 并释放预留，族占用回到历史 31，付费轮方可登记。

全程零 socket / 零模型 POST。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import family as FAM
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import state as ST


@pytest.fixture()
def fam_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    return root


# ---------------------------------------------------------------------------
# 族账层：close 释放预留、单活动成员约束仍成立
# ---------------------------------------------------------------------------

def test_close_releases_reservation_and_allows_next_round(fam_root):
    doc0 = FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round1")
    assert doc0["posts_reserved"] == 124
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round2")
    assert exc.value.code == "FAMILY_ROUND_OPEN"

    doc1 = FAM.close_member(fam_root, "env-round1",
                            reason="T5 验证轮关闭换 T6",
                            evidence={"lock_sha256": "a" * 64})
    member = next(m for m in doc1["members"]
                  if m["envelope_id"] == "env-round1")
    assert member["state"] == FAM.MEMBER_CLOSED
    assert member["close_evidence"]["lock_sha256"] == "a" * 64
    assert member["closed_at_utc"].endswith("Z")
    usage = FAM.usage(fam_root)
    assert usage["reserved_total"] == 31          # 历史块仍在，124 已释放
    assert usage["remaining"] == 491 - 31

    doc2 = FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round2")
    assert doc2["posts_reserved"] == 124
    usage = FAM.usage(fam_root)
    assert usage["reserved_total"] == 155         # 31 + 新一轮 124
    assert usage["history_posts"] == 31           # 历史不重复计
    states = {m["envelope_id"]: m["state"] for m in usage["members"]}
    assert states == {"env-round1": FAM.MEMBER_CLOSED,
                      "env-round2": FAM.MEMBER_REGISTERED}


def test_close_is_idempotent_and_requires_reason(fam_root):
    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-x")
    first = FAM.close_member(fam_root, "env-x", reason="r")
    closed_at = next(m for m in first["members"]
                     if m["envelope_id"] == "env-x")["closed_at_utc"]
    again = FAM.close_member(fam_root, "env-x", reason="r2")
    assert next(m for m in again["members"]
                if m["envelope_id"] == "env-x")["closed_at_utc"] == closed_at
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.close_member(fam_root, "env-x", reason="   ")
    assert exc.value.code == "FAMILY_CLOSE_REASON_REQUIRED"
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.close_member(fam_root, "env-absent", reason="r")
    assert exc.value.code == "FAMILY_ABSENT"


def test_exhausted_member_still_blocks_round_open(fam_root):
    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-e")
    FAM.record_usage(fam_root, "env-e", 124)
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, P.PROFILE_R12_DEF, "env-next")
    assert exc.value.code == "FAMILY_ROUND_OPEN"   # 只有 closed 才释放


# ---------------------------------------------------------------------------
# CLI 层：范围/前置/幂等（LOCK/报告的实跑证据由真 T5 根覆盖）
# ---------------------------------------------------------------------------

def _init_offline(fam_root: Path, tmp_path: Path, envelope: str = "env-off"):
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    rc = cli_r12.main(["init", "--envelope", envelope,
                       "--mode", "offline_validation", "--plan", str(plan)])
    assert rc == 0
    return fam_root / envelope


def _fake_lock(monkeypatch, purpose="validation", sha="f" * 64):
    class _Lock:
        sha256 = sha

    monkeypatch.setattr(cli_r12.LK, "verify_lock",
                        lambda ctx, repo_root=None: {"purpose": purpose,
                                                     "ok": True})
    monkeypatch.setattr(cli_r12.LK, "load_frozen", lambda ctx: _Lock())


def test_family_close_refuses_paid_mode(fam_root, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    assert cli_r12.main(["init", "--envelope", "env-paid",
                         "--mode", "paid", "--plan", str(plan)]) == 0
    assert cli_r12.main(["family", "close", "--root", str(fam_root / "env-paid"),
                         "--reason", "x"]) == 2
    member = FAM.usage(fam_root)["members"][0]
    assert member["state"] == FAM.MEMBER_REGISTERED


def test_family_close_refuses_without_report_pass(fam_root, tmp_path, monkeypatch):
    root = _init_offline(fam_root, tmp_path)
    _fake_lock(monkeypatch)
    report = root / "contract" / "offline_validation_report.json"
    report.write_text(json.dumps({"verdict": "FAIL"}), encoding="utf-8")
    assert cli_r12.main(["family", "close", "--root", str(root),
                         "--reason", "x"]) == 2
    assert FAM.usage(fam_root)["members"][0]["state"] == FAM.MEMBER_REGISTERED
    ctx = cli_r12._r12_ctx(root, "env-off")
    assert ST.ResearchState(ctx).root_state() == ST.ROOT_INIT


def test_family_close_happy_path_closes_root_and_member(fam_root, tmp_path,
                                                        monkeypatch):
    root = _init_offline(fam_root, tmp_path)
    _fake_lock(monkeypatch)
    (root / "contract" / "offline_validation_report.json").write_text(
        json.dumps({"verdict": "PASS"}), encoding="utf-8")
    assert cli_r12.main(["family", "close", "--root", str(root),
                         "--reason", "T5→T6 换轮"]) == 0
    ctx = cli_r12._r12_ctx(root, "env-off")
    assert ST.ResearchState(ctx).root_state() == ST.ROOT_CLOSED
    member = FAM.usage(fam_root)["members"][0]
    assert member["state"] == FAM.MEMBER_CLOSED
    assert FAM.usage(fam_root)["reserved_total"] == 31
    # 换轮后付费轮可登记（同一 family 根、同 profile）。
    paid_permit = FAM.register(fam_root, P.PROFILE_R12_DEF, "env-paid-next")
    assert paid_permit["posts_reserved"] == 124
    # 幂等：重复 close no-op。
    assert cli_r12.main(["family", "close", "--root", str(root),
                         "--reason", "again"]) == 0


# ---------------------------------------------------------------------------
# 2026-11-13 用户裁决：族帽 155→160；已关闭轮的实花继续占额
# ---------------------------------------------------------------------------

def test_closed_round_usage_still_occupies_family_cap(fam_root):
    """关闭只归还**未用**预留；已真实花掉的 POST 不得从族账消失。"""
    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round1")
    FAM.record_usage(fam_root, "env-round1", 5)
    FAM.close_member(fam_root, "env-round1", reason="仪器缺陷轮结案")
    usage = FAM.usage(fam_root)
    assert usage["reserved_total"] == 31 + 5     # 历史 + 已关闭轮实花
    assert usage["remaining"] == 491 - 36

    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round2")
    usage = FAM.usage(fam_root)
    assert usage["reserved_total"] == 31 + 5 + 124 == 160  # 本轮只计已关闭轮 5
    assert usage["remaining"] == 491 - 160  # 帽 190
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round3")
    assert exc.value.code == "FAMILY_ROUND_OPEN"   # 先被单活动成员挡住

    for n in (2, 3, 4):
        env = f"env-round{n}"
        FAM.register(fam_root, P.PROFILE_R12_DEF, env)
        FAM.record_usage(fam_root, env, 124)
        FAM.close_member(fam_root, env, reason=f"第 {n} 轮用尽后关闭")
    with pytest.raises(FAM.FamilyError) as exc:
        FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round5")
    assert exc.value.code == "FAMILY_CAP_EXCEEDED"  # 31+5+124*3 + 124 > 475


def test_migrate_cap_backfills_old_members_and_is_idempotent(fam_root):
    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round1")
    path = fam_root / "family" / "family.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["family_cap"] = 475                      # 模拟迁移前的老文档
    for m in doc["members"]:
        m.pop("family_cap_at_registration", None)
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")

    migrated = FAM.migrate_cap(fam_root, reason="用户裁决 475→491",
                               evidence={"decision": "AskUserQuestion"})
    assert migrated["family_cap"] == 491
    assert migrated["members"][0]["family_cap_at_registration"] == 475
    assert migrated["cap_history"] == [{
        "from_cap": 475, "to_cap": 491, "reason": "用户裁决 475→491",
        "evidence": {"decision": "AskUserQuestion"},
        "at_utc": migrated["cap_history"][0]["at_utc"],
    }]
    again = FAM.migrate_cap(fam_root, reason="重复调用")
    assert again == migrated                     # 幂等：不再追加条目
    assert len(again["cap_history"]) == 1


def test_cli_family_migrate_cap(fam_root, capsys):
    FAM.register(fam_root, P.PROFILE_R12_DEF, "env-round1")
    path = fam_root / "family" / "family.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["family_cap"] = 475                      # 模拟迁移前的老文档
    for m in doc["members"]:
        m.pop("family_cap_at_registration", None)
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    args = type("A", (), {"reason": "用户裁决 155→160",
                          "decision": "AskUserQuestion 2026-11-13"})()
    assert cli_r12.cmd_family_migrate_cap(args) == 0
    out = capsys.readouterr().out
    assert "family migrate-cap ok" in out
    assert "475→491" in out
    doc = json.loads((fam_root / "family" / "family.json")
                     .read_text(encoding="utf-8"))
    assert doc["family_cap"] == 491
    assert doc["members"][0]["family_cap_at_registration"] == 475


def test_abandon_closes_failed_drill_without_signatures(fam_root, monkeypatch):
    """失败演练根可显式放弃（释放未用预留），PASS 或有签名的根一律拒绝。"""
    envelope = "r12-abandon-test"
    root = fam_root / envelope
    (root / "contract").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "contract" / "offline_validation_report.json").write_text(
        json.dumps({"verdict": "FAIL"}), encoding="utf-8")
    FAM.register(fam_root, P.PROFILE_R12_DEF, envelope)
    monkeypatch.setattr(
        cli_r12, "_load_envelope",
        lambda root_, envelope_id=None: {"envelope_id": envelope,
                                         "mode": "offline_validation"})
    args = type("A", (), {"root": str(root), "envelope": envelope,
                          "reason": "演练失败换新根", "abandon": True})()
    assert cli_r12.cmd_family_close(args) == 0
    member = next(m for m in FAM.usage(fam_root)["members"]
                  if m["envelope_id"] == envelope)
    assert member["state"] == "closed"
    assert FAM.usage(fam_root)["reserved_total"] == 31

    # PASS 报告不得按放弃处理
    envelope2 = "r12-abandon-pass"
    root2 = fam_root / envelope2
    (root2 / "contract").mkdir(parents=True)
    (root2 / "state").mkdir()
    (root2 / "contract" / "offline_validation_report.json").write_text(
        json.dumps({"verdict": "PASS"}), encoding="utf-8")
    FAM.register(fam_root, P.PROFILE_R12_DEF, envelope2)
    monkeypatch.setattr(
        cli_r12, "_load_envelope",
        lambda root_, envelope_id=None: {"envelope_id": envelope2,
                                         "mode": "offline_validation"})
    args2 = type("A", (), {"root": str(root2), "envelope": envelope2,
                           "reason": "不应允许", "abandon": True})()
    with pytest.raises(cli_r12.Cli12Error) as exc:
        cli_r12.cmd_family_close(args2)
    assert exc.value.code == "FAMILY_ABANDON_PASSED"
