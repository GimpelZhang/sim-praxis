"""RSI-R1 T0：预算单真值、费用门、根隔离与写守卫、模型配置冻结（AC01–AC04）。

全部在 tmp 根下跑：不读不写真实 /mnt 账、不触网、不调模型、不跑仿真。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import cli, contracts as C, runtime
from orchestrator.rsi_r1.lanes import (
    LANE_ORDER,
    LANE_POST_CAPS,
    RSI_R1_HARD_CAP,
    RSI_R1_NORMAL_BUDGET,
    assert_lane_table_consistent,
)

ENVELOPE = "env1"


@pytest.fixture(autouse=True)
def _roots(tmp_path, monkeypatch):
    for name in ("PHASE1", "PHASE2", "PHASE25", "PHASE26", "PHASE27", "PHASE281"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    for name in ("RSI_R0", "RSI_R01", "RSI_R02", "RSI_R1"):
        monkeypatch.setenv(f"SIMPRAXIS_{name}_ROOT", str(tmp_path / name.lower()))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", ENVELOPE)
    yield


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _consent_doc(plan_sha: str, *, decision: str = C.COST_DECISION_WAIVER,
                 scope: str = "R1", verbatim: str = "我授权执行 R1",
                 envelope: str = ENVELOPE) -> dict:
    return {
        "schema": C.CONSENT_SCHEMA,
        "schema_version": 1,
        "envelope_id": envelope,
        "decision": decision,
        "scope": scope,
        "granted_at_utc": "2026-11-11T00:00:00Z",
        "granted_by": "user",
        "verbatim": verbatim,
        "plan_path": "/tmp/plan.md",
        "plan_sha256": plan_sha,
        "supersedes": [],
        "notes": "",
    }


# ---------------------------------------------------------------------------
# AC02：预算单真值与 144 硬帽
# ---------------------------------------------------------------------------

def test_lane_table_is_self_consistent():
    assert_lane_table_consistent()
    assert LANE_ORDER == ("probe", "practice", "study_A", "study_B", "repair_reserve")
    assert dict(LANE_POST_CAPS) == {"probe": 8, "practice": 16, "study_A": 48,
                                    "study_B": 48, "repair_reserve": 24}
    assert sum(LANE_POST_CAPS.values()) == RSI_R1_HARD_CAP == 144
    assert RSI_R1_NORMAL_BUDGET == 120


def test_namespace_2903_matches_lane_table():
    ns = QL.RSI_R1_NAMESPACE
    assert ns.phase == QL.LEDGER_RSI_R1 == 2903
    assert dict(ns.task_caps) == dict(QL.RSI_R1_TASK_CAPS)
    assert ns.hard_cap == 144 and ns.normal_budget == 120
    assert sum(ns.task_caps.values()) == ns.hard_cap
    assert ns.in_sealed_set() and ns.uses_batch_occupancy()
    assert ns.in_sealed_set_for_read()


def test_budget_defaults_and_single_truth():
    b = C.default_budget(ENVELOPE)
    assert b.total_posts == 144 and b.normal_posts == 120
    assert b.max_total_runs == 896
    assert b.max_steps == 10_000_000
    C.assert_single_truth(b, ledger_task_caps=QL.RSI_R1_TASK_CAPS,
                          ledger_hard_cap=QL.RSI_R1_HARD_CAP,
                          ledger_normal_budget=QL.RSI_R1_NORMAL_BUDGET)
    C.assert_run_budget_consistent()
    slots = b.slots()
    assert len(slots) == 144
    assert slots[0] == {"slot": 1, "lane": "probe", "lane_task_id": 0,
                        "lane_index": 1, "reserve": False}
    assert slots[-1]["lane"] == "repair_reserve" and slots[-1]["reserve"] is True
    assert len({s["slot"] for s in slots}) == 144  # 无重复槽号


def test_budget_roundtrip_and_hash_is_stable():
    b = C.default_budget(ENVELOPE)
    b2 = C.BudgetSpec.from_dict(json.loads(b.to_json()))
    assert b2.sha256 == b.sha256
    assert b2.to_dict() == b.to_dict()


@pytest.mark.parametrize("bad_posts", [
    {"probe": 8, "practice": 16, "study_A": 72, "study_B": 72, "repair_reserve": 12},  # 228 混入
    {"probe": 8, "practice": 16, "study_A": 72, "study_B": 72, "repair_reserve": 12 - 4},  # 224 混入
    {"probe": 8, "practice": 16, "study_A": 48, "study_B": 48},                        # 缺 lane
    {"probe": 8, "practice": 16, "study_A": 48, "study_B": 48,
     "repair_reserve": 24, "extra_lane": 1},                                           # 多 lane
])
def test_budget_rejects_drift(bad_posts):
    with pytest.raises(C.ContractError):
        C.BudgetSpec(
            schema=C.BUDGET_SCHEMA, envelope_id=ENVELOPE,
            posts_by_lane=bad_posts, max_usd=C.MAX_USD_LITERAL,
            max_runs_by_lane={"explore": 64, "confirm": 192, "replay": 192},
            max_steps=10_000_000)


def test_budget_rejects_bool_and_float_money():
    base = C.default_budget(ENVELOPE).to_dict()
    with pytest.raises(C.ContractError) as ei:
        C.BudgetSpec.from_dict({**base, "posts_by_lane": {**base["posts_by_lane"],
                                                          "probe": True}})
    assert ei.value.code in ("BOOL_AS_NUMBER", "BUDGET_DRIFT")
    with pytest.raises(C.ContractError) as ei:
        C.BudgetSpec.from_dict({**base, "max_usd": 100.0})
    assert ei.value.code == "USD_INVALID"


def test_budget_rejects_unknown_key_and_missing_key():
    base = C.default_budget(ENVELOPE).to_dict()
    with pytest.raises(C.ContractError) as ei:
        C.BudgetSpec.from_dict({**base, "sneaky": 1})
    assert ei.value.code == "UNKNOWN_KEY"
    missing = dict(base)
    missing.pop("max_steps")
    with pytest.raises(C.ContractError) as ei:
        C.BudgetSpec.from_dict(missing)
    assert ei.value.code == "MISSING_KEY"


# ---------------------------------------------------------------------------
# AC01：费用门与旧豁免不承继
# ---------------------------------------------------------------------------

def test_consent_rejects_old_r02_scope():
    with pytest.raises(C.ContractError) as ei:
        C.Consent.from_dict(_consent_doc("a" * 64, scope="R0.2"))
    assert ei.value.code == "CONSENT_STALE"


def test_consent_rejects_copied_r02_document():
    """R0.2 的豁免书 schema 不同，必须原样拒收（旧豁免不承继）。"""
    old = {"schema": "simpraxis.rsi_r02.spend_contract.v1", "gate": "G-S",
           "status": "WAIVED_BY_USER"}
    with pytest.raises(C.ContractError) as ei:
        C.Consent.from_dict(old)
    assert ei.value.code in ("MISSING_KEY", "UNKNOWN_KEY")


def test_waiver_requires_verbatim():
    with pytest.raises(C.ContractError) as ei:
        C.Consent.from_dict(_consent_doc("a" * 64, verbatim="   "))
    assert ei.value.code == "PAID_NOT_AUTHORIZED"


def test_blocked_consent_cannot_send():
    c = C.Consent.from_dict(_consent_doc("a" * 64, decision=C.COST_DECISION_BLOCKED))
    assert c.paid_authorized is False
    with pytest.raises(C.ContractError) as ei:
        C.assert_paid_authorized(c)
    assert ei.value.code == "PAID_NOT_AUTHORIZED"


def test_supply_guarantee_is_authorized_without_verbatim():
    c = C.Consent.from_dict(_consent_doc("a" * 64, decision=C.COST_DECISION_SUPPLY,
                                         verbatim=""))
    assert c.paid_authorized is True
    C.assert_paid_authorized(c)


def test_consent_must_bind_current_plan_bytes():
    c = C.Consent.from_dict(_consent_doc("a" * 64))
    with pytest.raises(C.ContractError) as ei:
        C.verify_consent_binds_plan(c, plan_path="/p", plan_sha256="b" * 64)
    assert ei.value.code == "CONSENT_STALE"
    C.verify_consent_binds_plan(c, plan_path="/p", plan_sha256="a" * 64)


def test_consent_rejects_non_hex_plan_sha():
    with pytest.raises(C.ContractError) as ei:
        C.Consent.from_dict(_consent_doc("z" * 64))
    assert ei.value.code == "CONSENT_STALE"


# ---------------------------------------------------------------------------
# AC03：根隔离与旧根只读
# ---------------------------------------------------------------------------

def test_namespace_isolation_rejects_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_ROOT", str(tmp_path / "phase26"))
    with pytest.raises(rp.PhaseRootAliasedError):
        rp.assert_rsi_r1_roots_isolated()


def test_protected_roots_include_all_three_old_families():
    names = set(rp.rsi_r1_protected_write_roots())
    assert {"phase1", "phase2", "phase25", "phase26", "phase27", "phase281",
            "rsi_r0", "rsi_r01", "rsi_r02"} <= names


def test_write_outside_campaign_root_refused(tmp_path):
    ctx = runtime.create_context(ENVELOPE)
    with pytest.raises(runtime.CampaignWriteRefused):
        ctx.write_text("../escape.json", "{}\n")
    with pytest.raises(runtime.CampaignWriteRefused):
        ctx.write_text(tmp_path / "absolute.json", "{}\n")


def test_write_into_old_family_root_refused(tmp_path):
    old = tmp_path / "rsi_r02" / "old_root"
    old.mkdir(parents=True)
    (old / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    ctx = runtime.create_context(ENVELOPE)
    with pytest.raises(runtime.CampaignWriteRefused):
        ctx.write_text(old / "ledger.jsonl", "poison\n")
    assert (old / "ledger.jsonl").read_text(encoding="utf-8") == "{}\n"


def test_write_through_symlink_component_refused(tmp_path):
    old = tmp_path / "rsi_r01" / "old_root"
    old.mkdir(parents=True)
    ctx = runtime.create_context(ENVELOPE)
    link = ctx.root / "contract" / "old_link"
    link.symlink_to(old, target_is_directory=True)
    with pytest.raises(runtime.CampaignWriteRefused):
        ctx.write_text("contract/old_link/ledger.jsonl", "poison\n")
    assert not (old / "ledger.jsonl").exists()


def test_account_exposure_keeps_old_unknown_visible():
    doc = cli._account_exposure(ENVELOPE)
    assert doc["count"] == 1
    row = doc["pending_unknown"][0]
    assert row["campaign"] == "r02_20261110T1218Z"
    assert row["status"] == "UNKNOWN_SENT" and row["ledger_seq"] == 26
    assert doc["pending_unknown_usd"] == "1.5"


# ---------------------------------------------------------------------------
# CLI init/check 端到端（AC01–AC04 的装配面）
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("# RSI-R1 test plan\n", encoding="utf-8")
    model = tmp_path / "stage_models.yaml"
    model.write_text("stages: {}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "PLAN_PATH", plan)
    monkeypatch.setattr(cli, "MODEL_CONFIG", model)
    consent = tmp_path / "consent.json"
    consent.write_text(json.dumps(_consent_doc(_sha(plan))), encoding="utf-8")
    return {"plan": plan, "model": model, "consent": consent}


def test_cli_init_then_check_pass(cli_env):
    assert cli.main(["init", "--envelope", ENVELOPE,
                     "--consent", str(cli_env["consent"])]) == 0
    assert cli.main(["check", "--envelope", ENVELOPE]) == 0
    root = Path(rp.rsi_r1_campaign_root())
    env_doc = json.loads((root / "contract" / "envelope.json").read_text())
    assert env_doc["hard_cap"] == 144 and env_doc["normal_budget"] == 120
    assert env_doc["ledger_namespace"] == 2903
    assert env_doc["old_waiver_inherited"] is False
    assert env_doc["plan_sha256"] == _sha(cli_env["plan"])


def test_cli_init_refuses_double_init(cli_env):
    assert cli.main(["init", "--envelope", ENVELOPE,
                     "--consent", str(cli_env["consent"])]) == 0
    assert cli.main(["init", "--envelope", ENVELOPE,
                     "--consent", str(cli_env["consent"])]) == 2


def test_cli_init_refuses_consent_for_other_envelope(cli_env, tmp_path):
    bad = tmp_path / "bad.json"
    doc = _consent_doc(_sha(cli_env["plan"]), envelope="other")
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert cli.main(["init", "--envelope", ENVELOPE, "--consent", str(bad)]) == 2


def test_cli_init_refuses_stale_plan_binding(cli_env, tmp_path):
    bad = tmp_path / "stale.json"
    bad.write_text(json.dumps(_consent_doc("c" * 64)), encoding="utf-8")
    assert cli.main(["init", "--envelope", ENVELOPE, "--consent", str(bad)]) == 2


def test_cli_check_detects_model_config_drift(cli_env):
    assert cli.main(["init", "--envelope", ENVELOPE,
                     "--consent", str(cli_env["consent"])]) == 0
    cli_env["model"].write_text("stages: {changed: 1}\n", encoding="utf-8")
    assert cli.main(["check", "--envelope", ENVELOPE]) == 2


def test_cli_init_refuses_blocked_cost_for_paid_only(cli_env, tmp_path):
    """BLOCKED 仍可初始化（设计/校准可做），但 check 必须报出来。"""
    doc = _consent_doc(_sha(cli_env["plan"]), decision=C.COST_DECISION_BLOCKED,
                       verbatim="")
    blocked = tmp_path / "blocked.json"
    blocked.write_text(json.dumps(doc), encoding="utf-8")
    assert cli.main(["init", "--envelope", ENVELOPE, "--consent", str(blocked)]) == 0
    assert cli.main(["check", "--envelope", ENVELOPE]) == 2


# ---------------------------------------------------------------------------
# AC04：模型不变（T0 可测部分）
# ---------------------------------------------------------------------------

def test_envelope_records_model_config_hash(cli_env):
    assert cli.main(["init", "--envelope", ENVELOPE,
                     "--consent", str(cli_env["consent"])]) == 0
    root = Path(rp.rsi_r1_campaign_root())
    env_doc = json.loads((root / "contract" / "envelope.json").read_text())
    assert env_doc["model_config_sha256"] == _sha(cli_env["model"])


def test_dependency_index_covers_all_seams():
    idx = cli.build_dependency_index()
    paths = {f["path"] for f in idx["files"]}
    assert paths == set(cli.SEAM_FILES)
    assert all(f["exists"] for f in idx["files"])
    # Plan §1.1 的十个接缝文件 + R0.1/R0.2 自锁文件。
    plan_seams = [p for p in paths
                  if p.startswith("orchestrator/rsi_r0") and not p.endswith("/lock.py")]
    assert len(plan_seams) == 10
    assert len([p for p in paths if p.endswith("/lock.py")]) == 2
    assert any("quota_ledger" in p for p in paths)
    assert any("runtime_paths" in p for p in paths)
