"""RSI-R1.3 T0：profile/路径隔离/族账对账/预算/审查容量。

覆盖 Plan §9 AC01/02/03/04/05 的零 POST 部分：

* AC01 新 profile 显式选择、未知拒绝、旧两条 profile 数值不变；
* AC02 新根/命名空间隔离，写入旧根被拒；
* AC03 族账逐账本事件去重（R1.3 历史 = 31 + 514 + 9），未决 reservation 拒绝；
* AC04 候选 124 + 审查 8 = 132，族帽 704；
* AC05 preflight/基线零 POST 留证；
* §5 T0 审查包容量测量方法（真实 tokenizer；超限即 REVIEW_PACKET_TOO_LARGE）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import cli_r13
from orchestrator.rsi_r1 import family as F
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import review_capacity as RC


# ---------------------------------------------------------------------------
# AC01 profile
# ---------------------------------------------------------------------------

def test_r13_profile_is_explicit_and_numbered():
    r13 = P.profile_by_id("rsi-r1.3")
    assert r13.is_r13 and not r13.is_r12 and not r13.is_legacy
    assert r13.namespace == 2905
    assert r13.consent_scope == "R1.3"
    assert r13.hard_cap == 132 == r13.normal_budget
    assert r13.family_cap == 704
    assert r13.family_history_posts == 554
    assert r13.data_root_name == "rsi_r13"
    assert r13.closure_groups_key == "r13"
    assert P.PROFILES == ("rsi-r1-legacy", "rsi-r1.2", "rsi-r1.3")


def test_old_profiles_unchanged():
    legacy = P.PROFILE_LEGACY_DEF
    r12 = P.PROFILE_R12_DEF
    assert legacy.namespace == 2903 and legacy.hard_cap == 144
    assert r12.namespace == 2904 and r12.hard_cap == 124
    assert r12.family_cap == 637 and r12.family_history_posts == 31
    assert r12.output_tokens == 32768


def test_unknown_profile_rejected_exact_match_only():
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"profile_id": "rsi-r1.30"})
    assert exc.value.code == "PROFILE_UNKNOWN"
    # startswith 不得命中新 profile
    assert P.resolve_profile({"profile_id": "rsi-r1.3"}).profile_id == "rsi-r1.3"
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"profile_id": "rsi-r1.3"},
                          requested_campaign="rsi-r1.2")
    assert exc.value.code == "PROFILE_MISMATCH"


def test_r13_requires_explicit_declaration():
    with pytest.raises(P.ProfileError) as exc:
        P.resolve_profile({"schema": "simpraxis.rsi_r13.envelope.v1"},
                          requested_campaign="rsi-r1.3")
    assert exc.value.code == "PROFILE_MISMATCH"


# ---------------------------------------------------------------------------
# AC04 budget
# ---------------------------------------------------------------------------

def test_r13_budget_split_and_cap():
    P.assert_r13_table_consistent()
    caps = dict(P.LANE_POST_CAPS_R13)
    assert sum(caps[l] for l in ("probe", "practice", "study_A", "study_B")) == 124
    assert sum(caps[l] for l in ("review_calibration", "review_final")) == 8
    assert sum(caps.values()) == 132
    assert P.R13_FAMILY_CAP == 554 + 18 + 132 == 704
    assert P.R13_FAMILY_CAP != 637 + 132  # 不结转旧余量


def test_r13_task_ids_are_explicit():
    assert dict(P.LANE_TASK_ID_R13) == {
        "probe": 0, "practice": 1, "study_A": 2, "study_B": 3,
        "review_calibration": 4, "review_final": 5,
    }


# ---------------------------------------------------------------------------
# AC02 isolation
# ---------------------------------------------------------------------------

def test_r13_root_default_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMPRAXIS_RSI_R13_ROOT", raising=False)
    assert RP.rsi_r13_root() == Path("/mnt/artifacts/simpraxis/rsi_r13")
    monkeypatch.setenv("SIMPRAXIS_RSI_R13_ROOT", str(tmp_path / "r13"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R13_CAMPAIGN", "env-round1")
    assert RP.rsi_r13_root() == tmp_path / "r13"
    assert RP.rsi_r13_campaign_root() == tmp_path / "r13" / "env-round1"
    assert RP.rsi_r13_subdir("quota") == tmp_path / "r13" / "env-round1" / "quota"
    with pytest.raises(RP.RsiR13CampaignUnsetError):
        RP.rsi_r13_subdir("not-a-subdir")


def test_r13_campaign_unset_is_fail_closed(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_RSI_R13_CAMPAIGN", raising=False)
    with pytest.raises(RP.RsiR13CampaignUnsetError):
        RP.rsi_r13_campaign_id()


def test_r13_roots_must_not_alias_old_roots(monkeypatch, tmp_path):
    old = tmp_path / "rsi_r12"
    (old / "family").mkdir(parents=True)
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(old))
    monkeypatch.setenv("SIMPRAXIS_RSI_R13_ROOT", str(old / "nested"))
    monkeypatch.setenv("SIMPRAXIS_RSI_R13_CAMPAIGN", "alias")
    with pytest.raises(RP.PhaseRootAliasedError):
        RP.assert_rsi_r13_roots_isolated()


def test_r13_protected_roots_include_r12():
    roots = RP.rsi_r13_protected_write_roots()
    assert "rsi_r12" in roots and "rsi_r1" in roots
    assert len(roots) >= 10


# ---------------------------------------------------------------------------
# AC03 family history reconciliation
# ---------------------------------------------------------------------------

def _stub_history(total: int = 554) -> dict:
    return {
        "imported_at_utc": "2026-11-15T00:00:00Z",
        "total_call_rows": total,
        "total_ok_rows": 0,
        "total_fail_rows": 0,
        "sources": [],
        "note": "stub",
    }


def _fake_ledger(path: Path, calls: int, *, ok: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"event": "call", "seq": i + 1, "ok": ok}
        for i in range(calls)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    path.chmod(0o600)


def _fake_r12_root(tmp_path: Path, members: "list[dict]") -> "tuple[Path, Path]":
    root = tmp_path / "rsi_r12"
    family_doc = root / "family" / "family.json"
    family_doc.parent.mkdir(parents=True)
    doc = {
        "schema": F.FAMILY_SCHEMA,
        "family_cap": 637,
        "history": {"total_call_rows": 31},
        "members": members,
    }
    family_doc.write_text(json.dumps(doc), encoding="utf-8")
    family_doc.chmod(0o600)
    for m in members:
        if m["posts_used"]:
            _fake_ledger(root / m["envelope_id"] / "quota" / "ledger.jsonl",
                         int(m["posts_used"]))
        else:
            ledger = root / m["envelope_id"] / "quota" / "ledger.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text("", encoding="utf-8")
            ledger.chmod(0o600)
    return root, family_doc


def test_history_v13_recomputes_each_ledger(monkeypatch, tmp_path):
    members = [
        {"envelope_id": "a", "state": "closed", "posts_used": 300},
        {"envelope_id": "b", "state": "closed", "posts_used": 214},
        {"envelope_id": "c", "state": "closed", "posts_used": 0},
    ]
    root, doc = _fake_r12_root(tmp_path, members)
    monkeypatch.setattr(F, "import_history", lambda *a, **k: _stub_history(31))
    monkeypatch.setattr(P, "R13_FAMILY_CLOSED_ROUND_POSTS", 514)
    history = F.import_history_v13(r12_family_doc=doc, r12_root=root,
                                   require_pin=False)
    assert history["total_call_rows"] == 554
    assert history["ledger_call_rows"] == 545
    assert history["offledger"]["posts"] == 9
    assert len(history["sources"]) == 3


def test_history_v13_rejects_open_reservation(monkeypatch, tmp_path):
    members = [{"envelope_id": "a", "state": "registered", "posts_used": 0}]
    root, doc = _fake_r12_root(tmp_path, members)
    monkeypatch.setattr(F, "import_history", lambda *a, **k: _stub_history(31))
    with pytest.raises(F.FamilyError) as exc:
        F.import_history_v13(r12_family_doc=doc, r12_root=root, require_pin=False)
    assert exc.value.code == "HISTORY_DRIFT"


def test_history_v13_rejects_self_reported_usage_mismatch(monkeypatch, tmp_path):
    members = [{"envelope_id": "a", "state": "closed", "posts_used": 10}]
    root, doc = _fake_r12_root(tmp_path, members)
    # 账本只有 9 行（伪造自报 10）
    _fake_ledger(root / "a" / "quota" / "ledger.jsonl", 9)
    monkeypatch.setattr(F, "import_history", lambda *a, **k: _stub_history(31))
    with pytest.raises(F.FamilyError) as exc:
        F.import_history_v13(r12_family_doc=doc, r12_root=root, require_pin=False)
    assert exc.value.code == "HISTORY_DRIFT"


def test_history_v13_rejects_total_drift(monkeypatch, tmp_path):
    members = [{"envelope_id": "a", "state": "closed", "posts_used": 5}]
    root, doc = _fake_r12_root(tmp_path, members)
    monkeypatch.setattr(F, "import_history", lambda *a, **k: _stub_history(31))
    with pytest.raises(F.FamilyError) as exc:
        F.import_history_v13(r12_family_doc=doc, r12_root=root, require_pin=False)
    assert exc.value.code == "HISTORY_DRIFT"


def test_family_register_r13_reserves_candidate_plus_review(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "_fresh_history", lambda spec, now=None: _stub_history(554))
    root = tmp_path / "r13"
    permit = F.register(root, P.PROFILE_R13_DEF, "round1")
    assert permit["schema"] == P.FAMILY_PERMIT_SCHEMA_V13
    assert permit["posts_reserved"] == 132
    doc = F.load_family_doc(root, expected_schema=P.FAMILY_SCHEMA_V13)
    assert doc["family_cap"] == 704
    assert doc["history"]["total_call_rows"] == 554
    # 同 profile 第二轮在本轮关闭前被拒
    with pytest.raises(F.FamilyError) as exc:
        F.register(root, P.PROFILE_R13_DEF, "round2")
    assert exc.value.code == "FAMILY_ROUND_OPEN"
    # 许可可复核
    F.verify_permit(root, permit, P.PROFILE_R13_DEF, "round1",
                    fresh_history=_stub_history(554))
    # 关闭后可开下一轮
    F.close_member(root, "round1", reason="test")
    F.register(root, P.PROFILE_R13_DEF, "round2")


def test_family_r13_rejects_wrong_cap_profile(monkeypatch, tmp_path):
    import dataclasses

    monkeypatch.setattr(F, "_fresh_history", lambda spec, now=None: _stub_history(554))
    bad = dataclasses.replace(P.PROFILE_R13_DEF, family_cap=637)
    with pytest.raises(F.FamilyError) as exc:
        F.register(tmp_path / "r13", bad, "round1")
    assert exc.value.code == "FAMILY_CAP_MISMATCH"


# ---------------------------------------------------------------------------
# AC05 zero-POST baseline
# ---------------------------------------------------------------------------

def test_t0_baseline_is_zero_post_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_r13, "_environment_facts", lambda: {
        "disk_mnt": None, "containers_all": 0,
        "gpu_mem_used_mib": [0], "paid_env_allow": False})
    out = tmp_path / "baseline"
    import argparse

    code = cli_r13.cmd_t0_baseline(argparse.Namespace(
        out=str(out), plan=None, tokenizer=None))
    assert code == 0
    doc = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    assert doc["paid_posts"] == 0
    assert doc["schema"] == cli_r13.BASELINE_SCHEMA
    assert doc["profile_id"] == "rsi-r1.3"
    assert doc["profiles"]["rsi-r1.3"]["family_cap"] == 704
    assert doc["review_capacity"]["input_tokens"] == 20000
    assert (out / "baseline.json").stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# §5 T0 review packet capacity method
# ---------------------------------------------------------------------------

TOKENIZER = Path(RC.DEFAULT_TOKENIZER_PATH)


@pytest.mark.skipif(not TOKENIZER.is_file(), reason="本地 tokenizer 不存在")
def test_review_capacity_measures_with_real_tokenizer():
    tokenizer = RC.load_tokenizer()
    facts = RC.measure_request(system="sys", user="user", tokenizer=tokenizer,
                               label="small")
    assert facts["input_tokens"] > 0
    assert facts["schema"] == RC.CAPACITY_SCHEMA


@pytest.mark.skipif(not TOKENIZER.is_file(), reason="本地 tokenizer 不存在")
def test_review_capacity_rejects_oversized_packet():
    tokenizer = RC.load_tokenizer()
    with pytest.raises(RC.ReviewCapacityError) as exc:
        RC.measure_request(system="s", user="字" * 400_000, tokenizer=tokenizer,
                           packet_bytes_cap=1000, label="huge")
    assert exc.value.code == "REVIEW_PACKET_TOO_LARGE"


def test_review_capacity_registered_caps_come_from_profile():
    caps = RC.registered_caps(P.PROFILE_R13_DEF)
    assert caps["input_tokens"] == 20000
    assert caps["output_tokens"] == 32768
    assert caps["packet_bytes"] == 122_880
    with pytest.raises(RC.ReviewCapacityError):
        RC.registered_caps(P.PROFILE_R12_DEF)
