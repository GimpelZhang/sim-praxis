"""T5 最终 LOCK 单测（AC26–AC27）：零网络、零付费、零容器。

真实 campaign 的 LOCK 由 T5 取证生成；这里用 tmp_path 合成 campaign
锁死**闸门语义**：自哈希、事实重算、双签绑定、任一字段变化即拒绝付费。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import lock as L

_TRIALS = ("E-dev", "E-val-1", "E-val-2", "D-dev", "D-val-1", "D-val-2",
           "R-dev", "R-val-1", "R-val-2")


def _fake_campaign(tmp_path: Path) -> C.CampaignContext:
    plan = tmp_path / "plan.md"
    consent = tmp_path / "consent.txt"
    plan.write_text("plan-bytes", encoding="utf-8")
    consent.write_text("consent-bytes", encoding="utf-8")
    ctx = C.create_campaign(campaign_id="rt01_lock", plan_path=plan,
                            consent_path=consent, repo_root=L.REPO_ROOT,
                            root=tmp_path / "campaigns")
    root = ctx.root
    old = tmp_path / "old_root"
    old.mkdir()
    (old / "artefact.json").write_text("{}", encoding="utf-8")
    sealed = {"schema": "simpraxis.rsi_r01.sealed_roots.v1",
              "rsi_r0": {"root": str(old), "tree": C.hash_tree(old)},
              "phase_ledgers": {}}
    (root / "contract" / "baseline.json").write_text(json.dumps(
        {"schema": "simpraxis.rsi_r01.baseline.v1",
         "pinned_expected": {"numpy": "1.26.4"},
         "model_change_this_campaign": False}), encoding="utf-8")
    (root / "contract" / "sealed_roots.json").write_text(
        json.dumps(sealed), encoding="utf-8")
    (root / "analysis" / "t1_container").mkdir(parents=True, exist_ok=True)
    (root / "analysis" / "t1_container" / "evidence.json").write_text(
        json.dumps({"all_pass": True,
                    "verdicts": {k: "PASS" for k in
                                 ("AC05", "AC06", "AC07", "AC08", "AC09")}}),
        encoding="utf-8")
    (root / "analysis" / "controls").mkdir(parents=True, exist_ok=True)
    (root / "analysis" / "controls" / "controls.json").write_text(
        json.dumps({"rejected_all": True, "total": 13,
                    "controls": [{"name": f"ctl_{i}", "rejected": True}
                                 for i in range(13)]}), encoding="utf-8")
    (root / "analysis" / "t3_oracle").mkdir(parents=True, exist_ok=True)
    (root / "analysis" / "t3_oracle" / "audit.json").write_text(
        json.dumps({"verdict": "PASS",
                    "checks": {"import_audit": True, "real_sequences": True,
                               "boundary_sequences": True, "t_low_zero": True}}),
        encoding="utf-8")
    (root / "closeout").mkdir(parents=True, exist_ok=True)
    (root / "closeout" / "t2_design_gate.json").write_text(json.dumps(
        {"campaign_verdict": "DESIGN_READY", "decision": "PROCEED_T3"}),
        encoding="utf-8")
    (root / "analysis" / "reference_actor").mkdir(parents=True, exist_ok=True)
    for trial in _TRIALS:
        (root / "analysis" / "reference_actor" / f"{trial}.json").write_text(
            json.dumps({"ok": True, "trial_id": trial}), encoding="utf-8")
    (root / "protocol" / "t2_tasks_lock.json").write_text(json.dumps({
        "task_matrix_sha256": "a" * 64,
        "measurement_contract_sha256": "b" * 64,
        "private_matrix_sha256": "c" * 64,
        "dev_order": ["E-dev", "D-dev", "R-dev"],
        "validation_order": ["E-val-1", "D-val-1", "R-val-1",
                             "E-val-2", "D-val-2", "R-val-2"],
        "instances": 9}), encoding="utf-8")
    return ctx


@pytest.fixture()
def price_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pricing_contract.json"
    path.write_text(json.dumps({"schema": "rsi_r0.pricing_contract.v1",
                                "price_per_1m_input_usd": 1.0,
                                "price_per_1m_output_usd": 4.0}),
                    encoding="utf-8")
    monkeypatch.setattr(L, "PRICE_SOURCE", path)
    return path


def _sign(ctx: C.CampaignContext, role: str, sha: str, who: str) -> None:
    candidate = json.loads((ctx.root / "contract" / L.CANDIDATE_FILENAME)
                           .read_text(encoding="utf-8"))
    sig = L.signature_document(
        role=role, verdict="APPROVE", binding_sha256=sha, signed_by=who,
        evidence_sha256=candidate["facts"]["g_i"]["evidence"])
    C.write_json(ctx, f"{L.SIG_DIR}/{role}.json", sig)


def _finalize(ctx: C.CampaignContext) -> dict:
    return L.finalize(ctx)


def test_core_hash_ignores_signatures_selfhash_and_timestamp():
    doc = {"schema": "x", "created_utc": "t0", "facts": {"a": 1},
           "signatures": {}, "lock_sha256": "deadbeef"}
    h = L.core_sha256(doc)
    doc["signatures"] = {"reviewer": {"x": 1}}
    doc["lock_sha256"] = "0" * 64
    doc["created_utc"] = "t1"
    assert L.core_sha256(doc) == h      # 可复算：重建候选 hash 不变
    doc["facts"]["a"] = 2
    assert L.core_sha256(doc) != h


def test_candidate_to_finalize_roundtrip(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    built = L.build_candidate(ctx)
    sha = built["lock_sha256"]
    # 无签名不得冻结
    with pytest.raises(L.LockError) as exc:
        _finalize(ctx)
    assert exc.value.code == "BLOCKED_APPROVAL"
    # 错绑定的签名不得冻结
    _sign(ctx, "reviewer", "0" * 64, "reviewer-a")
    with pytest.raises(L.LockError):
        _finalize(ctx)
    _sign(ctx, "reviewer", sha, "reviewer-a")
    _sign(ctx, "verifier", sha, "verifier-b")
    out = _finalize(ctx)
    assert out["lock_sha256"] == sha
    assert out["signers"] == {"reviewer": "reviewer-a",
                              "verifier": "verifier-b"}
    report = L.verify_lock(ctx)
    assert report["verdict"] == "PASS", report["problems"]
    doc = L.load_lock(ctx.root)
    assert L.assert_paid_allowed(ctx)["lock_sha256"] == sha
    assert doc["facts"]["budget_price"]["requests_total_cap"] == 232
    assert doc["facts"]["budget_price"]["worst_case_total_usd"] <= 100.0
    assert doc["facts"]["caps"]["allocation"] == {
        "protocol": 12, "development": 72, "validation": 144,
        "normal_total": 228, "conditional_validate_models": 4,
        "grand_total": 232}


def test_duplicate_signers_refused(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    _sign(ctx, "reviewer", sha, "same-person")
    _sign(ctx, "verifier", sha, "same-person")
    with pytest.raises(L.LockError) as exc:
        _finalize(ctx)
    assert any("签名人相同" in p for p in exc.value.problems)


def test_any_facts_field_change_refuses_paid(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    _sign(ctx, "reviewer", sha, "reviewer-a")
    _sign(ctx, "verifier", sha, "verifier-b")
    _finalize(ctx)
    path = ctx.root / "contract" / L.LOCK_FILENAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["facts"]["caps"]["trial_cap"] = 999
    path.write_text(json.dumps(doc), encoding="utf-8")
    report = L.verify_lock(ctx)
    assert report["verdict"] == "FAIL"
    assert any("自哈希" in p for p in report["problems"])
    with pytest.raises(L.LockError) as exc:
        L.assert_paid_allowed(ctx)
    assert exc.value.code == "LOCK_CHANGED"


def test_tampered_signature_refused_while_core_hash_holds(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    _sign(ctx, "reviewer", sha, "reviewer-a")
    _sign(ctx, "verifier", sha, "verifier-b")
    _finalize(ctx)
    path = ctx.root / "contract" / L.LOCK_FILENAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert L.core_sha256(doc) == sha          # 签名不在 core 内
    doc["signatures"]["reviewer"]["verdict"] = "REJECT"
    path.write_text(json.dumps(doc), encoding="utf-8")
    report = L.verify_lock(ctx)
    assert report["verdict"] == "FAIL"
    assert any("verdict != APPROVE" in p for p in report["problems"])


def test_live_evidence_drift_refuses_paid(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    _sign(ctx, "reviewer", sha, "reviewer-a")
    _sign(ctx, "verifier", sha, "verifier-b")
    _finalize(ctx)
    # 负控文件被改（只剩 12 项）：事实重算必须抓到。
    (ctx.root / "analysis" / "controls" / "controls.json").write_text(
        json.dumps({"rejected_all": True, "total": 12}), encoding="utf-8")
    report = L.verify_lock(ctx)
    assert report["verdict"] == "FAIL"
    assert any("13" in p or "事实已变" in p for p in report["problems"])
    with pytest.raises(L.LockError):
        L.assert_paid_allowed(ctx)


def test_price_over_ceiling_blocks(tmp_path, price_file,
                                   monkeypatch: pytest.MonkeyPatch):
    ctx = _fake_campaign(tmp_path)
    monkeypatch.setattr(L, "USD_CEILING", 1.0)
    with pytest.raises(L.LockError) as exc:
        L.build_candidate(ctx)
    assert exc.value.code == "BLOCKED_PRICE"
    assert any("货币帽" in p for p in exc.value.problems)


def test_missing_price_source_blocks(tmp_path, monkeypatch):
    ctx = _fake_campaign(tmp_path)
    monkeypatch.setattr(L, "PRICE_SOURCE", tmp_path / "absent.json")
    with pytest.raises(L.LockError) as exc:
        L.build_candidate(ctx)
    assert exc.value.code == "BLOCKED_PRICE"


def test_stage_models_endpoint_read_from_meta():
    doc = L.stage_models(Path(L.REPO_ROOT) / "orchestrator" / "gateway"
                         / "stage_models.yaml")
    assert doc["endpoint"] == "anthropic-messages"   # MEDIUM-1 回归
    assert doc["base_url_env"] == "ANTHROPIC_BASE_URL"


def test_live_plan_edit_refuses_paid(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    _sign(ctx, "reviewer", sha, "reviewer-a")
    _sign(ctx, "verifier", sha, "verifier-b")
    _finalize(ctx)
    Path(ctx.plan_path).write_text("plan-bytes-edited", encoding="utf-8")
    report = L.verify_lock(ctx)
    assert report["verdict"] == "FAIL"
    assert any("plan_sha256" in p for p in report["problems"])
    with pytest.raises(L.LockError):
        L.assert_paid_allowed(ctx)


def test_signature_evidence_mismatch_refused(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    sha = L.build_candidate(ctx)["lock_sha256"]
    bogus = L.signature_document(role="reviewer", verdict="APPROVE",
                                 binding_sha256=sha, signed_by="reviewer-a",
                                 evidence_sha256={"controls": "0" * 64})
    C.write_json(ctx, f"{L.SIG_DIR}/reviewer.json", bogus)
    _sign(ctx, "verifier", sha, "verifier-b")
    with pytest.raises(L.LockError) as exc:
        L.finalize(ctx)
    assert any("证据" in p for p in exc.value.problems)


def test_non_mapping_signature_file_raises_lock_error(tmp_path, price_file):
    ctx = _fake_campaign(tmp_path)
    L.build_candidate(ctx)
    C.write_json(ctx, f"{L.SIG_DIR}/reviewer.json", ["not", "a", "mapping"])
    with pytest.raises(L.LockError) as exc:
        L.load_signatures(ctx.root)
    assert exc.value.code == "BLOCKED_APPROVAL"


def test_signature_validation_rejects_malformed():
    good = L.signature_document(role="reviewer", verdict="APPROVE",
                                binding_sha256="a" * 64, signed_by="r",
                                evidence_sha256={"x": "y"})
    assert L.validate_signature(good, "reviewer", "a" * 64) == []
    assert L.validate_signature(good, "verifier", "a" * 64)
    assert L.validate_signature(dict(good, gate="G-V"), "reviewer", "a" * 64)
    assert L.validate_signature(dict(good, binding_sha256="b" * 64),
                                "reviewer", "a" * 64)
    assert L.validate_signature(dict(good, signed_by=""), "reviewer", "a" * 64)
