"""RSI-R1.2 T0（AC03）：新根与旧根隔离、子目录白名单、campaign 必填。

覆盖：

* ``rsi_r12_*`` 路径族只落在 ``$SIMPRAXIS_RSI_R12_ROOT`` 下；
* 未设 campaign → fail-closed；子目录白名单拒绝路径穿越；
* 与封存期根、R0/R0.1/R0.2/R1 家族根嵌套 → ``PhaseRootAliasedError``；
* CLI 显式 ``--root`` 不落在族根内 → 拒；
* 族账目录固定在家族根，不随 campaign 漂移。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import cli_r12


@pytest.fixture()
def r12_root(tmp_path, monkeypatch):
    root = tmp_path / "rsi_r12"
    root.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(root))
    monkeypatch.delenv("SIMPRAXIS_RSI_R12_CAMPAIGN", raising=False)
    return root


def test_campaign_env_required(r12_root):
    with pytest.raises(RP.RsiR12CampaignUnsetError):
        RP.rsi_r12_campaign_root()


def test_subdir_whitelist(r12_root, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "cam1")
    for name in ("contract", "quota", "quota/approvals", "validation", "aux"):
        assert name in RP.RSI_R12_CAMPAIGN_SUBDIRS
    with pytest.raises(RP.RsiR12CampaignUnsetError):
        RP.rsi_r12_subdir("../escape")
    with pytest.raises(RP.RsiR12CampaignUnsetError):
        RP.rsi_r12_subdir("nope")


def test_paths_all_under_family_root(r12_root, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "cam1")
    fam = r12_root.resolve(strict=False)
    for fn in (RP.rsi_r12_ledger_path, RP.rsi_r12_counters_path,
               RP.rsi_r12_approvals_dir, RP.rsi_r12_gateway_state_path,
               RP.rsi_r12_protocol_dir, RP.rsi_r12_validation_dir,
               RP.rsi_r12_aux_dir, RP.rsi_r12_closeout_dir,
               RP.rsi_r12_evidence_pack_dir, RP.rsi_r12_family_doc_path):
        path = fn().resolve(strict=False)
        assert fam == path or fam in path.parents, path
    assert RP.rsi_r12_family_doc_path().name == "family.json"


def test_isolation_rejects_nesting_into_old_roots(r12_root, monkeypatch):
    # 把 R1.2 家族根放进 R1 家族根 → 任何写路径前即拒。
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(RP.rsi_r1_root()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "cam1")
    with pytest.raises(Exception) as exc:
        RP.assert_rsi_r12_roots_isolated()
    assert "R1.2" in str(exc.value)
    # R1.2 家族根与 R1 家族根相同 → 同样拒
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(RP.rsi_r1_root()))
    with pytest.raises(Exception):
        RP.assert_rsi_r12_roots_isolated()


def test_protected_roots_include_r1_family(r12_root):
    protected = RP.rsi_r12_protected_write_roots()
    assert set(protected) >= {"phase1", "phase2", "phase25", "phase26",
                              "phase27", "phase281", "rsi_r0", "rsi_r01",
                              "rsi_r02", "rsi_r1"}


def test_isolation_accepts_fresh_root(r12_root, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "cam1")
    RP.assert_rsi_r12_roots_isolated()


def test_cli_rejects_root_outside_family(r12_root, tmp_path, monkeypatch):
    r12_root.mkdir(exist_ok=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(r12_root))
    tmphome = tmp_path / "elsewhere"
    tmphome.mkdir()
    plan = tmphome / "plan.md"
    plan.write_text("fake plan", encoding="utf-8")
    rc = cli_r12.main([
        "init", "--envelope", "cam1", "--root", str(tmphome / "cam1"),
        "--plan", str(plan)])
    assert rc == 2


def test_cli_requires_root_or_envelope(r12_root):
    rc = cli_r12.main(["status"])
    assert rc == 2


def test_symlinked_campaign_into_old_root_rejected(r12_root, tmp_path, monkeypatch):
    """AC03：软链写旧根——软链解析后落进 R1 家族根即 fail-closed。"""
    old = Path(str(RP.rsi_r1_root()))
    old.mkdir(parents=True, exist_ok=True)
    link = r12_root / "evil"
    link.symlink_to(old, target_is_directory=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(r12_root))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "evil")
    with pytest.raises(Exception) as exc:
        RP.assert_rsi_r12_roots_isolated()
    assert "R1.2" in str(exc.value)
