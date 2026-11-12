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
    assert usage["remaining"] == 124

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
