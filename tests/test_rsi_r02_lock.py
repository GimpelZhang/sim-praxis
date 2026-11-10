"""RSI-R0.2 最终 LOCK 测试（AC21）。

判据：候选缺证据不得签；任一实现/共享依赖/模型配置/证据变动 ⇒ verify 报问题
（cli.run 据此拒绝付费 POST）；两份签名必须绑定同一 lock_sha256 才能冻结。
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pytest

from orchestrator.rsi_r02 import lock as L
from orchestrator.rsi_r02 import protocol_fixture as PF
from tests import rsi_r02_helpers as H

EVIDENCE = ("contract/spend_contract.json", "contract/task_contract.json",
            "contract/asset_reuse.json", "t3_control/report.json",
            "t3_control/closeout_rehearsal.json",
            "t3_control/records/proto_P1.json",
            "t3_control/records/proto_P2.json",
            "contract/protocol_contract.json")

YAML = """stage_models:
  macro_ideation: {model_id: macro-x}
  coding_debug: {model_id: coding-x}
  visual_rollout: {model_id: null}
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo_root"
    (root / "orchestrator/rsi_r02").mkdir(parents=True)
    (root / "scripts/rsi_r02").mkdir(parents=True)
    (root / "orchestrator/rsi_r02/alpha.py").write_text("A = 1\n", encoding="utf-8")
    (root / "scripts/rsi_r02/run.py").write_text("B = 2\n", encoding="utf-8")
    for rel in L.SHARED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".yaml"):
            path.write_text(YAML, encoding="utf-8")
        else:
            path.write_text(f"# {rel}\n", encoding="utf-8")
    return root


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    c = H.make_campaign(tmp_path)
    return c


def _spend_contract(**over) -> dict:
    text = "我人工授权立即解除G-S：按照$1.5/POST、整批上限$100.0继续"
    doc = {"schema": "simpraxis.rsi_r02.spend_contract.v1", "gate": "G-S",
           "status": "WAIVED_BY_USER",
           "authority": {"verbatim_text": text,
                         "verbatim_sha256": hashlib.sha256(
                             text.encode("utf-8")).hexdigest()},
           "per_post_reservation_usd": 1.5, "batch_cap_usd": 100.0,
           "claimed_as_supplier_guarantee": False}
    doc.update(over)
    return doc


def _evidence(ctx, *, skip=()):
    for rel in EVIDENCE:
        if rel in skip:
            continue
        if rel == "contract/spend_contract.json":
            ctx.write_json(rel, _spend_contract())
        elif rel == "contract/protocol_contract.json":
            # 真件（非占位）：协议合同由 T0 物化、被 LOCK 钉哈希（D-T4-2）。
            ctx.write_json(rel, PF.contract_payload(campaign_id=ctx.campaign_id))
        else:
            ctx.write_json(rel, {"rel": rel})


def test_candidate_refuses_missing_evidence(ctx, tmp_path):
    _evidence(ctx, skip=("t3_control/report.json",))
    with pytest.raises(L.LockError) as ei:
        L.build_candidate(ctx, repo_root=_repo(tmp_path))
    assert "t3_control/report.json" in str(ei.value)


def test_candidate_is_self_consistent_and_pins_manifests(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    doc = L.build_candidate(ctx, repo_root=repo)
    assert doc["schema"] == L.SCHEMA_LOCK
    assert doc["lock_sha256"] == L.core_sha256(doc)
    assert "orchestrator/rsi_r02/alpha.py" in doc["implementation"]
    assert "scripts/rsi_r02/run.py" in doc["implementation"]
    assert "orchestrator/gateway/router.py" in doc["implementation"]
    assert doc["stage_models"]["models"] == {"macro_ideation": "macro-x",
                                             "coding_debug": "coding-x",
                                             "visual_rollout": None}
    assert set(doc["evidence"]) == {"t3_control_report", "t3_closeout_rehearsal",
                                    "protocol_P1", "protocol_P2",
                                    "spend_contract", "task_contract",
                                    "asset_reuse", "protocol_contract"}


def test_dependency_closure_follows_shared_imports(ctx, tmp_path):
    """AC21：不在 rsi_r02 目录的共享模块也必须进 LOCK（手写清单漏过 TrialRunner）。"""
    repo = _repo(tmp_path)
    (repo / "orchestrator/rsi_r01").mkdir(parents=True, exist_ok=True)
    (repo / "orchestrator/rsi_r01/trial.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "orchestrator/rsi_r01/__init__.py").write_text("", encoding="utf-8")
    (repo / "orchestrator/rsi_r02/alpha.py").write_text(
        "from orchestrator.rsi_r01 import trial\nA = 1\n", encoding="utf-8")
    impl = L.implementation_manifest(repo)
    assert "orchestrator/rsi_r01/trial.py" in impl
    before = dict(impl)
    (repo / "orchestrator/rsi_r01/trial.py").write_text("X = 2\n", encoding="utf-8")
    assert L.implementation_manifest(repo)["orchestrator/rsi_r01/trial.py"] != \
        before["orchestrator/rsi_r01/trial.py"]


def test_dependency_closure_unresolvable_import_is_an_error(ctx, tmp_path):
    repo = _repo(tmp_path)
    (repo / "orchestrator/rsi_r02/alpha.py").write_text(
        "import orchestrator.ghost.module\nA = 1\n", encoding="utf-8")
    with pytest.raises(L.LockError) as ei:
        L.implementation_manifest(repo)
    assert "orchestrator.ghost.module" in str(ei.value)


def test_candidate_hash_changes_when_implementation_changes(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    first = L.build_candidate(ctx, repo_root=repo)["lock_sha256"]
    (repo / "orchestrator/rsi_r02/alpha.py").write_text("A = 2\n", encoding="utf-8")
    assert L.build_candidate(ctx, repo_root=repo)["lock_sha256"] != first


def test_sign_rejects_bad_role_and_non_approve(ctx, tmp_path):
    _evidence(ctx)
    L.write_candidate(ctx, repo_root=_repo(tmp_path))
    with pytest.raises(L.LockError):
        L.sign(ctx, role="chairman", signer="x", statement="s")
    with pytest.raises(L.LockError):
        L.sign(ctx, role="reviewer", signer="x", statement="s", verdict="REJECT")


def test_freeze_requires_both_signatures_on_same_candidate(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    cand = L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    with pytest.raises(L.LockError):
        L.freeze(ctx, repo_root=repo)
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    final = L.freeze(ctx, repo_root=repo)
    assert final["lock_sha256"] == cand["lock_sha256"]
    assert set(final["signatures"]) == {"reviewer", "verifier"}
    assert L.verify(ctx, repo_root=repo)["ok"] is True


def test_verify_catches_drift_and_missing_signature(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)

    (repo / "orchestrator/rsi_r02/alpha.py").write_text("A = 3\n", encoding="utf-8")
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert "CHANGED: orchestrator/rsi_r02/alpha.py" in out["problems"]

    (repo / "scripts/rsi_r02/new.py").write_text("N = 1\n", encoding="utf-8")
    assert "ADDED: scripts/rsi_r02/new.py" in L.verify(ctx, repo_root=repo)["problems"]

    ctx.relative("contract/signatures/verifier.json").unlink()
    assert any("SIGNATURE_MISSING_OR_CHANGED: verifier" in p
               for p in L.verify(ctx, repo_root=repo)["problems"])


def test_verify_catches_model_config_change(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)
    (repo / "orchestrator/gateway/stage_models.yaml").write_text(
        YAML.replace("coding-x", "coding-y"), encoding="utf-8")
    out = L.verify(ctx, repo_root=repo)
    assert "MODEL_CONFIG_CHANGED" in out["problems"]
    assert "CHANGED: orchestrator/gateway/stage_models.yaml" in out["problems"]


def test_verify_rejects_forged_final_lock_with_stale_signatures(ctx, tmp_path):
    """AC21 绕过复现（评审 H2）：重写 final_lock.json 并自洽后必须被签名绑定拦下。

    攻击面：把实现面 hash 改成当前值、重算 ``lock_sha256``——文件自证一致，
    ``signatures`` 里的 sha 也一个字节没动。若 verify 只比签名文件 sha，就
    放行了「冻结后改实现再付费」。必须重读签名内容，发现绑定的是另一版 LOCK。
    """
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)
    assert L.verify(ctx, repo_root=repo)["ok"] is True

    # 实现被改 + final_lock 同步重写（攻击者能重算 core_sha256）
    (repo / "orchestrator/rsi_r02/alpha.py").write_text("A = 9\n", encoding="utf-8")
    forged = L.build_candidate(ctx, repo_root=repo)
    forged["signatures"] = dict(
        json.loads(ctx.relative(L.FINAL_FILENAME).read_text(encoding="utf-8"))
        ["signatures"])
    forged["frozen_utc"] = "2026-11-10T00:00:00Z"
    ctx.write_json(L.FINAL_FILENAME, forged)
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert "SIGNATURE_BINDS_OTHER_LOCK: reviewer" in out["problems"]
    assert "SIGNATURE_BINDS_OTHER_LOCK: verifier" in out["problems"]


def test_verify_rejects_signature_reenacted_with_non_approve(ctx, tmp_path):
    """签名文件内容与 sha 同步被换（能改文件就能改 signatures 表）也要看判词。"""
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)

    path = ctx.relative(f"{L.SIG_DIR}/verifier.json")
    sig = json.loads(path.read_text(encoding="utf-8"))
    sig["verdict"] = "REJECT"
    ctx.write_json(f"{L.SIG_DIR}/verifier.json", sig)
    final = json.loads(ctx.relative(L.FINAL_FILENAME).read_text(encoding="utf-8"))
    final["signatures"]["verifier"] = hashlib.sha256(
        path.read_bytes()).hexdigest()
    ctx.write_json(L.FINAL_FILENAME, final)
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert "SIGNATURE_NOT_APPROVE: verifier" in out["problems"]


def test_freeze_rejects_same_signer(ctx, tmp_path):
    """评审 M2：同一人占两角色等于没有第二道门——冻结必须拒绝。"""
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="same-person", statement="ok")
    L.sign(ctx, role="verifier", signer="same-person", statement="ok")
    with pytest.raises(L.LockError) as ei:
        L.freeze(ctx, repo_root=repo)
    assert "签署人不得相同" in str(ei.value)
    assert not ctx.relative(L.FINAL_FILENAME).exists()


def test_freeze_rejects_blank_signer(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="   ", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    with pytest.raises(L.LockError):
        L.freeze(ctx, repo_root=repo)


def test_verify_detects_plan_and_consent_change(ctx, tmp_path):
    """评审 M1：plan/consent 的 sha 只写进 LOCK 不复核等于冻结后还能换计划。"""
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)
    assert L.verify(ctx, repo_root=repo)["ok"] is True

    Path(ctx.plan_path).write_text("# plan v2\n", encoding="utf-8")
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert "PLAN_CHANGED" in out["problems"]

    Path(ctx.consent_path).write_text("authorized v2\n", encoding="utf-8")
    out = L.verify(ctx, repo_root=repo)
    assert "CONSENT_CHANGED" in out["problems"]


def test_verify_reports_unreadable_final_lock(ctx, tmp_path):
    """评审 L1：最终 LOCK 读不了要如实报问题，而不是抛异常掀翻 run。"""
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)
    ctx.relative(L.FINAL_FILENAME).write_text("{broken", encoding="utf-8")
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert any(p.startswith("FINAL_LOCK_UNREADABLE") for p in out["problems"])


def test_relative_import_is_followed(ctx, tmp_path):
    """评审 L2：``from .x import y`` 不解析就会漏锁——必须进闭包。"""
    repo = _repo(tmp_path)
    (repo / "orchestrator/rsi_r02/alpha.py").write_text(
        "from . import beta\nA = 1\n", encoding="utf-8")
    (repo / "orchestrator/rsi_r02/beta.py").write_text("B = 1\n",
                                                       encoding="utf-8")
    impl = L.implementation_manifest(repo)
    assert "orchestrator/rsi_r02/beta.py" in impl


def test_relative_import_outside_package_is_an_error(ctx, tmp_path):
    repo = _repo(tmp_path)
    (repo / "orchestrator/rsi_r02/alpha.py").write_text(
        "from ... import ghost\nA = 1\n", encoding="utf-8")
    with pytest.raises(L.LockError):
        L.implementation_manifest(repo)


def test_verify_unfrozen_is_allowed_only_when_not_required(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    assert L.verify(ctx, repo_root=repo)["ok"] is False
    out = L.verify(ctx, repo_root=repo, require=False)
    assert out["ok"] is True and out["problems"] == ["FINAL_LOCK_MISSING"]


def test_candidate_tamper_is_detected_on_load(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    path = ctx.relative(L.CANDIDATE_FILENAME)
    doc = path.read_text(encoding="utf-8")
    path.write_text(doc.replace('"freeze_before_paid": true',
                                '"freeze_before_paid": false'), encoding="utf-8")
    with pytest.raises(L.LockError):
        L.load_candidate(ctx)


def test_gs_recheck_rejects_bad_waiver_and_cap_mismatch(ctx, tmp_path):
    repo = _repo(tmp_path)
    _evidence(ctx)
    ctx.write_json("contract/spend_contract.json",
                   _spend_contract(batch_cap_usd=50.0))
    with pytest.raises(L.LockError) as ei:
        L.build_candidate(ctx, repo_root=repo)
    assert "GS_BATCH_CAP_MISMATCH" in str(ei.value)
    bad = _spend_contract()
    bad["authority"]["verbatim_sha256"] = "0" * 64
    ctx.write_json("contract/spend_contract.json", bad)
    with pytest.raises(L.LockError) as ei:
        L.build_candidate(ctx, repo_root=repo)
    assert "GS_WAIVER_SHA_MISMATCH" in str(ei.value)


def test_verify_reports_gs_recheck_failure_after_freeze(ctx, tmp_path):
    _evidence(ctx)
    repo = _repo(tmp_path)
    L.write_candidate(ctx, repo_root=repo)
    L.sign(ctx, role="reviewer", signer="rev", statement="ok")
    L.sign(ctx, role="verifier", signer="ver", statement="ok")
    L.freeze(ctx, repo_root=repo)
    ctx.write_json("contract/spend_contract.json",
                   _spend_contract(claimed_as_supplier_guarantee=True))
    out = L.verify(ctx, repo_root=repo)
    assert out["ok"] is False
    assert any("GS_RECHECK_FAILED" in p for p in out["problems"])
    assert "EVIDENCE_CHANGED" in out["problems"]
