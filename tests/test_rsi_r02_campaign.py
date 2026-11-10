"""RSI-R0.2 T0 基座与决策件测试：campaign 隔离、命名空间、旧根只读、槽位算术（AC01–AC05）。

全部在 tmp 根下跑：不读不写真实 /mnt 账、不触网、不调模型。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r02 import campaign as camp

PLAN_TEXT = "# RSI-R0.2 test plan\n"
CONSENT_TEXT = "现在执行Plan：<verbatim user authorization>\n"
T0_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rsi_r02" / "t0_baseline.py"


def _load_t0():
    spec = importlib.util.spec_from_file_location("rsi_r02_t0_baseline", T0_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_ROOT", str(tmp_path / "rsi_r0"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R01_ROOT", str(tmp_path / "rsi_r01"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r02"))
    yield


@pytest.fixture
def repo(tmp_path):
    """最小真 git 仓（rev-parse 需要真实 HEAD）。"""
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


def _create(repo, docs, cid="r02_T0", root=None):
    plan, consent = docs
    return camp.create_campaign(campaign_id=cid, plan_path=plan,
                                consent_path=consent, repo_root=repo,
                                root=root)


# ---- AC01：G0 可追溯（计划/授权缺一即拒） ----


def test_init_requires_plan_and_consent(repo, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    consent = tmp_path / "consent.txt"
    consent.write_text(CONSENT_TEXT, encoding="utf-8")
    for p, c in ((tmp_path / "nope.md", consent), (plan, tmp_path / "nope.txt"),
                 (tmp_path / "nope.md", tmp_path / "nope.txt")):
        with pytest.raises(camp.CampaignError):
            camp.create_campaign(campaign_id="r02_X", plan_path=p,
                                 consent_path=c, repo_root=repo)
    assert not (tmp_path / "rsi_r02" / "r02_X").exists()


def test_init_layout_permissions_and_context(repo, docs):
    ctx = _create(repo, docs)
    for name in rp.RSI_R02_CAMPAIGN_SUBDIRS:
        d = ctx.root / name
        assert d.is_dir(), name
        assert (d.stat().st_mode & 0o777) == 0o700, name
    ctx_file = ctx.root / "contract" / camp.CONTEXT_FILENAME
    assert ctx_file.is_file()
    assert (ctx_file.stat().st_mode & 0o777) == 0o600
    obj = json.loads(ctx_file.read_text())
    assert obj["campaign_id"] == "r02_T0"
    assert obj["namespace"] == 2902
    assert obj["consent_sha256"] == ctx.consent_sha256
    assert obj["plan_sha256"] == ctx.plan_sha256
    assert obj["code_commit"] == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    # 旧根族必须在上下文里（rsi_r0 与 rsi_r01 两族都在）
    assert {"rsi_r0", "rsi_r01"} <= set(dict(ctx.protected_roots))


def test_init_idempotent_but_rejects_drifted_consent(repo, docs):
    ctx = _create(repo, docs)
    again = _create(repo, docs)
    assert again.created_at_utc == ctx.created_at_utc
    plan, consent = docs
    consent.write_text("different authorization\n", encoding="utf-8")
    with pytest.raises(camp.CampaignError):
        _create(repo, docs)


# ---- AC02：新旧路径/namespace 隔离 ----


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "café", "a b"])
def test_campaign_id_rejects_unsafe(repo, docs, bad):
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs, cid=bad)


def test_root_alias_and_nesting_rejected(repo, docs, tmp_path, monkeypatch):
    # 与 R0.1 家族根同体
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r01"))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)
    # 嵌套在封存期根内
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "phase281" / "sub"))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)


def test_symlink_alias_rejected(repo, docs, tmp_path, monkeypatch):
    (tmp_path / "rsi_r01").mkdir()
    link = tmp_path / "r02_link"
    link.symlink_to(tmp_path / "rsi_r01", target_is_directory=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(link))
    with pytest.raises(camp.CampaignRootRefused):
        _create(repo, docs)


def test_dynamic_env_drift_does_not_redirect_writes(repo, docs, tmp_path, monkeypatch):
    ctx = _create(repo, docs)
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "elsewhere"))
    target = ctx.write_text("contract/probe.json", "{}\n")
    assert target == ctx.root / "contract" / "probe.json"
    assert not (tmp_path / "elsewhere" / ctx.campaign_id).exists()
    with pytest.raises(camp.CampaignError):
        camp.load_campaign(campaign_id=ctx.campaign_id)


def test_namespace_caps_and_hard_cap():
    ns = QL.RSI_R02_NAMESPACE
    assert ns.phase == QL.LEDGER_RSI_R02 == 2902
    # 协议帽 12 = 2 链 ×（4 必做 + 2 自修余量），2026-11-10 用户裁决「开新票据」
    assert dict(ns.task_caps) == {0: 12, 1: 72, 2: 72, 3: 72}
    assert ns.hard_cap == 228
    assert sum(ns.task_caps.values()) == ns.hard_cap == 228
    assert ns.normal_budget == ns.hard_cap
    assert ns.in_sealed_set() and ns.uses_batch_occupancy()
    assert ns.in_sealed_set_for_read()
    # 旧账保留标识但已封存
    assert QL.RSI_R0_NAMESPACE.phase == 2900
    assert QL.RSI_R01_NAMESPACE.phase == 2901


def test_namespace_isolation_rejects_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_ROOT", str(tmp_path / "rsi_r01"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", "c")
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_rsi_r02_roots_isolated()


def test_quota_ledger_rsi_r02_routes_to_campaign(repo, docs, monkeypatch):
    ctx = _create(repo, docs)
    monkeypatch.setenv("SIMPRAXIS_RSI_R02_CAMPAIGN", ctx.campaign_id)
    ledger = QL.QuotaLedger.rsi_r02()
    assert ledger.ns.phase == 2902
    assert ledger._ledger_path() == rp.rsi_r02_ledger_path()
    assert str(ctx.root) in str(ledger._ledger_path())
    with pytest.raises(rp.RsiR02CampaignUnsetError):
        monkeypatch.delenv("SIMPRAXIS_RSI_R02_CAMPAIGN")
        rp.rsi_r02_campaign_id()


# ---- AC03：旧根只读（R0/R0.1 两族） ----


def test_write_outside_campaign_root_refused(repo, docs, tmp_path):
    ctx = _create(repo, docs)
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text("../escape.json", "{}\n")
    with pytest.raises(camp.CampaignWriteRefused):
        ctx.write_text(tmp_path / "absolute.json", "{}\n")


def test_write_into_old_family_root_refused(repo, docs, tmp_path):
    old = tmp_path / "rsi_r01" / "r02_old"
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


def test_snapshot_and_verify_covers_both_old_families(repo, docs, tmp_path):
    for fam in ("rsi_r0", "rsi_r01"):
        old = tmp_path / fam / "c0"
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
    assert set(snap["old_family_roots"]) == {"rsi_r0", "rsi_r01"}
    assert snap["old_family_roots"]["rsi_r0"]["tree"]["n_files"] == 2
    assert snap["old_family_roots"]["rsi_r01"]["tree"]["n_files"] == 2
    assert camp.verify_protected(snap) == []
    assert camp.verify_protected(camp.snapshot_protected()) == []
    (tmp_path / "rsi_r01" / "c0" / "b.json").write_text("changed\n")
    diff = camp.verify_protected(snap)
    assert any("rsi_r01" in d and "b.json" in d for d in diff), diff


def test_compare_tree_records_symlink_set_not_false_diff(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.json").write_text("a\n")
    (root / "link.json").symlink_to(root / "a.json")
    tree = camp.hash_tree(root)
    assert tree["symlinks"] == ["link.json"]
    # 链接仍在 → 无差异（R0.1 旧根内确有容器产物链接，不能因此永远报错）
    assert camp.compare_tree(root, tree) == []
    (root / "link.json").unlink()
    assert camp.compare_tree(root, tree) == ["symlink removed: link.json"]


# ---- T0 attach：早停根补写上下文（受限例外） ----


def test_attach_requires_reason(repo, docs, tmp_path):
    plan, consent = docs
    root = tmp_path / "rsi_r02" / "r02_attach"
    root.mkdir(parents=True)
    with pytest.raises(camp.CampaignError):
        camp.attach_campaign_context(campaign_id="r02_attach", plan_path=plan,
                                     consent_path=consent, repo_root=repo,
                                     root=tmp_path / "rsi_r02", allow_reason="  ")


def test_attach_refuses_root_with_running_evidence(repo, docs, tmp_path):
    plan, consent = docs
    root = tmp_path / "rsi_r02" / "r02_attach"
    (root / "quota").mkdir(parents=True)
    (root / "quota" / "ledger.jsonl").write_text('{"seq": 1}\n')
    with pytest.raises(camp.CampaignError):
        camp.attach_campaign_context(campaign_id="r02_attach", plan_path=plan,
                                     consent_path=consent, repo_root=repo,
                                     root=tmp_path / "rsi_r02",
                                     allow_reason="test")


def test_attach_accepts_early_stop_only_root(repo, docs, tmp_path):
    plan, consent = docs
    root = tmp_path / "rsi_r02" / "r02_attach"
    (root / "contract").mkdir(parents=True)
    early = root / "contract" / "scope_decision.json"
    early.write_text('{"g0": "GRANTED"}\n')
    before = early.read_bytes()
    ctx = camp.attach_campaign_context(campaign_id="r02_attach", plan_path=plan,
                                       consent_path=consent, repo_root=repo,
                                       root=tmp_path / "rsi_r02",
                                       allow_reason="G-S 早停后恢复实施")
    assert ctx.namespace == 2902
    assert (ctx.root / "contract" / camp.CONTEXT_FILENAME).is_file()
    assert early.read_bytes() == before          # 既有早停件逐字节不动
    assert (ctx.root / "quota" / "approvals").is_dir()
    again = camp.attach_campaign_context(campaign_id="r02_attach", plan_path=plan,
                                         consent_path=consent, repo_root=repo,
                                         root=tmp_path / "rsi_r02",
                                         allow_reason="idempotent")
    assert again.created_at_utc == ctx.created_at_utc


# ---- AC04：228 槽位表可执行且与计划表一致（协议 12＝带自修余量） ----


def test_slots_arithmetic_matches_plan():
    t0 = _load_t0()
    slots = t0.build_slots()
    assert slots["counts"]["total"] == 228
    assert slots["counts"]["by_phase"] == {"protocol": 12, "dev": 72,
                                           "validation": 144}
    assert slots["counts"]["by_family"] == {"E": 72, "D": 72, "R": 72}
    ids = [s["slot_id"] for s in slots["slots"]]
    assert len(ids) == len(set(ids)) == 228
    # 每条协议链：四轮必做动作次序固定（§6.1）+ 两个自修余量槽（required=None）
    per_chain = [[s for s in slots["slots"]
                  if s["phase"] == "protocol" and s["chain"] == chain]
                 for chain in ("A", "B")]
    for chain_slots in per_chain:
        assert [s["required_action"] for s in chain_slots] == [
            "preregister", "run_experiment", "analyze", "submit", None, None]
    # 验证顺序固定 E1,D1,R1,E2,D2,R2（§6.3）
    order = []
    for s in slots["slots"]:
        if s["phase"] == "validation" and s["index"] == 1:
            order.append(s["instance_id"])
    assert order == list(t0.VALIDATION_ORDER)


def test_slot_cap_equals_namespace_hard_cap():
    t0 = _load_t0()
    slots = t0.build_slots()
    assert slots["counts"]["total"] == QL.RSI_R02_HARD_CAP
    assert len(slots["slots"]) == QL.RSI_R02_NORMAL_BUDGET
    for s in slots["slots"]:
        assert s["post_cap"] == 1 and s["status"] == "UNUSED"


# ---- AC05：模型配置不变（不改 ID、不换供） ----


def test_stage_models_hash_and_no_null():
    t0 = _load_t0()
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "orchestrator" / "gateway" / "stage_models.yaml"
    info = t0.stage_models(path)
    assert set(info["models"]) == {"macro_ideation", "coding_debug",
                                   "visual_rollout"}
    assert all(v for v in info["models"].values())
    assert info["sha256"] == camp.sha256_file(path)
    assert info["endpoint"]
