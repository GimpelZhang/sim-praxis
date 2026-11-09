"""RSI-R0.1 T0 基座测试：campaign 根隔离、写守卫、旧根只读、命名空间（AC01–AC04）。

全部在 tmp 根下跑：不读不写真实 /mnt 账、不触网。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r01 import campaign as camp
from orchestrator.rsi_r01 import cli as r01_cli

PLAN_TEXT = "# RSI-R0.1 test plan\n"
CONSENT_TEXT = "现在执行Plan：<verbatim user authorization>\n"


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r01"))
    yield


@pytest.fixture
def repo(tmp_path):
    """最小真 git 仓（只读调用 rev-parse 需要真实 HEAD）。"""
    r = tmp_path / "repo"
    r.mkdir()
    env = dict(os.environ,
               GIT_AUTHOR_NAME="Junchuan Zhang",
               GIT_AUTHOR_EMAIL="zjunchuan@gmail.com",
               GIT_COMMITTER_NAME="Junchuan Zhang",
               GIT_COMMITTER_EMAIL="zjunchuan@gmail.com")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True, env=env)
    (r / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "x.txt"], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True, env=env)
    return r


@pytest.fixture
def docs(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text(CONSENT_TEXT, encoding="utf-8")
    return plan, consent


def _create(repo, docs, cid="r01_T0", root=None):
    plan, consent = docs
    return camp.create_campaign(campaign_id=cid, plan_path=plan,
                                consent_path=consent, repo_root=repo,
                                root=root)


# ---- AC01：授权可追溯 ----


def test_init_requires_plan_and_consent(repo, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text(CONSENT_TEXT, encoding="utf-8")
    # 缺计划 / 缺授权 / 两者都缺：一律拒绝且不建目录
    for p, c in ((tmp_path / "nope.md", consent), (plan, tmp_path / "nope.txt"),
                 (tmp_path / "nope.md", tmp_path / "nope.txt")):
        with pytest.raises(camp.CampaignError):
            camp.create_campaign(campaign_id="r01_X", plan_path=p,
                                 consent_path=c, repo_root=repo)
    assert not (tmp_path / "rsi_r01" / "r01_X").exists()


def test_init_layout_permissions_and_context(repo, docs):
    ctx = _create(repo, docs)
    assert ctx.root == (ctx.root)
    for name in rp.RSI_R01_CAMPAIGN_SUBDIRS:
        d = ctx.root / name
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700
    ctx_file = ctx.root / "contract" / camp.CONTEXT_FILENAME
    assert ctx_file.is_file()
    assert (ctx_file.stat().st_mode & 0o777) == 0o600
    obj = json.loads(ctx_file.read_text())
    assert obj["campaign_id"] == "r01_T0"
    assert obj["namespace"] == 2901
    assert obj["consent_sha256"] == ctx.consent_sha256
    assert obj["plan_sha256"] == ctx.plan_sha256
    assert obj["code_commit"] == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()


def test_init_idempotent_but_rejects_drifted_consent(repo, docs, tmp_path):
    ctx = _create(repo, docs)
    again = _create(repo, docs)
    assert again.created_at_utc == ctx.created_at_utc
    plan, consent = docs
    consent.write_text("different authorization\n", encoding="utf-8")
    with pytest.raises(camp.CampaignError):
        _create(repo, docs)


def test_cli_init_end_to_end(repo, docs, tmp_path, capsys):
    plan, consent = docs
    rc = r01_cli.main(["init", "--campaign-id", "r01_CLI",
                       "--plan", str(plan), "--consent", str(consent),
                       "--repo-root", str(repo),
                       "--root", str(tmp_path / "rsi_r01")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["namespace"] == 2901
    assert Path(out["root"]).joinpath("contract", camp.CONTEXT_FILENAME).is_file()


# ---- AC02：新旧 campaign 不重合、namespace 唯一 ----


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "café", "a b"])
def test_campaign_id_rejects_unsafe(repo, docs, bad):
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs, cid=bad)


def test_root_alias_and_nesting_rejected(repo, docs, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r0"))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "phase281" / "sub"))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)


def test_symlink_alias_rejected(repo, docs, tmp_path, monkeypatch):
    (tmp_path / "rsi_r0").mkdir()
    link = tmp_path / "r01_link"
    link.symlink_to(tmp_path / "rsi_r0", target_is_directory=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(link))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)


def test_dynamic_env_drift_does_not_redirect_writes(repo, docs, tmp_path, monkeypatch):
    ctx = _create(repo, docs)
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "elsewhere"))
    target = ctx.write_text("contract/probe.json", "{}\n")
    assert target == ctx.root / "contract" / "probe.json"
    assert not (tmp_path / "elsewhere" / ctx.campaign_id).exists()
    with pytest.raises(camp.CampaignError):
        camp.load_campaign(campaign_id=ctx.campaign_id)


def test_namespace_caps_and_hard_cap():
    ns = QL.RSI_R01_NAMESPACE
    assert ns.phase == QL.LEDGER_RSI_R01 == 2901
    assert dict(ns.task_caps) == {0: 12, 1: 72, 2: 72, 3: 72}
    assert ns.hard_cap == 228
    assert sum(ns.task_caps.values()) == ns.hard_cap
    assert ns.normal_budget == ns.hard_cap
    assert ns.in_sealed_set() and ns.uses_batch_occupancy()
    assert ns.in_sealed_set_for_read()
    assert QL.RSI_R0_NAMESPACE.phase == 2900  # R0 账保留但已封存


def test_namespace_isolation_rejects_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "phase26"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_CAMPAIGN", "c")
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_rsi_r01_roots_isolated()


# ---- AC03：旧根只读 ----


def test_write_outside_campaign_root_refused(repo, docs, tmp_path):
    ctx = _create(repo, docs)
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text("../escape.json", "{}\n")
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text(tmp_path / "absolute.json", "{}\n")


def test_write_into_old_root_refused(repo, docs, tmp_path):
    old = tmp_path / "rsi_r0" / "r0_old"
    old.mkdir(parents=True)
    (old / "ledger.jsonl").write_text("{}\n")
    ctx = _create(repo, docs)
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text(old / "ledger.jsonl", "poison\n")
    assert (old / "ledger.jsonl").read_text() == "{}\n"


def test_write_through_symlink_component_refused(repo, docs, tmp_path):
    old = tmp_path / "rsi_r0" / "r0_old"
    old.mkdir(parents=True)
    ctx = _create(repo, docs)
    link = ctx.root / "contract" / "old_link"
    link.symlink_to(old, target_is_directory=True)
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text("contract/old_link/ledger.jsonl", "poison\n")
    assert not (old / "ledger.jsonl").exists()


def test_snapshot_and_verify_detects_drift(repo, docs, tmp_path):
    old = tmp_path / "rsi_r0" / "r0_old"
    old.mkdir(parents=True)
    (old / "a.json").write_text("a\n")
    (old / "b.json").write_text("b\n")
    for name in ("phase1", "phase2", "phase25", "phase26", "phase27", "phase281"):
        q = tmp_path / name / "quota"
        q.mkdir(parents=True)
        (q / "ledger.jsonl").write_text('{"seq": 1}\n')
        (q / "counters.json").write_text("{}\n")
        (q / "attempts.jsonl").write_text("")

    snap = camp.snapshot_protected()
    assert snap["rsi_r0"]["tree"]["n_files"] == 2
    assert snap["rsi_r0"]["tree"]["symlinks"] == []
    assert camp.verify_protected(snap) == []
    # 快照本身不写旧根：再算一次逐件相同
    assert camp.verify_protected(camp.snapshot_protected()) == []
    (old / "b.json").write_text("changed\n")
    diff = camp.verify_protected(snap)
    assert any("b.json" in d for d in diff)


def test_r01_ledger_writable_but_r0_root_protected(repo, docs, tmp_path,
                                                  monkeypatch):
    ctx = _create(repo, docs)
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_CAMPAIGN", ctx.campaign_id)
    approval = QL.write_approval(batch_id="R01-B1", task=0,
                                 stage="coding_debug", approved=1,
                                 beyond_normal=False, note="t0",
                                 namespace=QL.RSI_R01_NAMESPACE)
    assert Path(approval).parent == rp.rsi_r01_approvals_dir()
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text(Path(rp.rsi_r01_root()).parent / "rsi_r0" / "x", "x")
