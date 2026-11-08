"""RSI-R0 路径族与账本封存测试（AC03/AC05/AC06，零联网）。

全部在 tmp 根下跑：不读不写真实 /mnt 账。
"""
import json
import os
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as QL


@pytest.fixture(autouse=True)
def _r0_roots(tmp_path, monkeypatch):
    """把 R0 家族根与 campaign 都指到临时目录；六份封存根也一并隔离。"""
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "c_test")
    yield


def test_campaign_unset_fails_closed(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_RSI_R0_CAMPAIGN", raising=False)
    with pytest.raises(rp.RsiR0CampaignUnsetError):
        rp.rsi_r0_campaign_root()
    with pytest.raises(rp.RsiR0CampaignUnsetError):
        QL.QuotaLedger.rsi_r0()


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "café", "a b"])
def test_campaign_id_rejects_unsafe_values(monkeypatch, bad):
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", bad)
    with pytest.raises(rp.RsiR0CampaignUnsetError):
        rp.rsi_r0_campaign_root()


def test_campaign_root_layout_and_permissions(tmp_path):
    root = rp.rsi_r0_campaign_root()
    assert root == tmp_path / "rsi_r0" / "c_test"
    quota = rp.rsi_r0_quota_dir()
    apps = rp.rsi_r0_approvals_dir()
    res = rp.rsi_r0_reservations_dir()
    for d in (quota, apps, res):
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700
    assert rp.rsi_r0_ledger_path().parent == quota
    assert rp.rsi_r0_gateway_state_dir().parent == root


def test_roots_isolation_passes_for_sibling_tree():
    rp.assert_rsi_r0_roots_isolated()  # 不抛即通过


def test_roots_isolation_rejects_nesting(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "phase281" / "sub"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "c_test")
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_rsi_r0_roots_isolated()


def test_roots_isolation_rejects_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "phase25"))
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_rsi_r0_roots_isolated()


def test_sealed_set_now_includes_phase281():
    sealed = rp.sealed_ledger_paths()
    assert len(sealed) == 6
    assert rp.phase281_ledger_path().resolve(strict=False) in sealed
    with pytest.raises(rp.SealedLedgerError):
        rp.open_ledger_append(rp.phase281_ledger_path())
    # R0 自己的账不被封存（唯一可写）
    rp.open_ledger_append(rp.rsi_r0_ledger_path()).close()


def test_r0_namespace_caps_and_hard_cap():
    ns = QL.RSI_R0_NAMESPACE
    assert ns.phase == QL.LEDGER_RSI_R0 == 2900
    assert dict(ns.task_caps) == {0: 6, 1: 72, 2: 72, 3: 72}
    assert ns.hard_cap == 222
    assert sum(ns.task_caps.values()) == ns.hard_cap
    assert ns.normal_budget == ns.hard_cap
    assert ns.in_sealed_set() and ns.uses_batch_occupancy()
    assert ns.in_sealed_set_for_read()


def test_approval_task_must_belong_to_r0_namespace():
    with pytest.raises(QL.QuotaError):
        QL.write_approval(batch_id="R0-P", task=5, stage="coding_debug",
                          approved=1, beyond_normal=False, note="neg",
                          namespace=QL.RSI_R0_NAMESPACE)


def test_cross_namespace_approval_is_rejected():
    """AC06：phase281 的批准件不能授权 R0 的任何一次调用。"""
    p281 = QL.write_approval(batch_id="P281-B1", task=1, stage="coding_debug",
                             approved=10, beyond_normal=False, note="p281",
                             namespace=QL.PHASE281_NAMESPACE)
    assert Path(p281).parent == rp.phase281_approvals_dir()
    # R0 账看不到 phase281 的批准件 ⇒ preflight 拦停并写 seq=null 审计行
    lg = QL.QuotaLedger.rsi_r0()
    with pytest.raises(QL.GatewayPausedError):
        lg.preflight("coding_debug", task=1, batch_id="P281-B1")
    rows = lg._read_rows()
    assert len(rows) == 1
    assert rows[0]["seq"] is None
    assert rows[0]["event"] == "preflight_blocked"


def test_cross_campaign_approval_is_rejected(monkeypatch):
    """同一家族的另一个 campaign 的票据同样不生效（campaign 是独立账套）。"""
    QL.write_approval(batch_id="R0-P", task=0, stage="coding_debug", approved=6,
                      beyond_normal=False, note="campaign A",
                      namespace=QL.RSI_R0_NAMESPACE)
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "c_other")
    lg = QL.QuotaLedger.rsi_r0()
    with pytest.raises(QL.GatewayPausedError):
        lg.preflight("coding_debug", task=0, batch_id="R0-P")


def test_r0_ledger_is_isolated_from_old_ledgers(tmp_path):
    lg = QL.QuotaLedger.rsi_r0()
    QL.write_approval(batch_id="R0-P", task=0, stage="coding_debug", approved=6,
                      beyond_normal=False, note="ok", namespace=QL.RSI_R0_NAMESPACE)
    lg.preflight("coding_debug", task=0, batch_id="R0-P")
    lg.record("coding_debug", task=0, batch_id="R0-P", ok=True, model="m",
              tokens_in=3, tokens_out=2)
    # 新账只写自己；六本封存账保持不存在/未变
    assert rp.rsi_r0_ledger_path().exists()
    for name in ("phase1", "phase2", "phase25", "phase26", "phase27", "phase281"):
        p = tmp_path / name / "quota" / "ledger.jsonl"
        assert not p.exists(), f"{name} 被写穿：{p}"
    assert os.stat(rp.rsi_r0_ledger_path()).st_mode & 0o777 == 0o600
