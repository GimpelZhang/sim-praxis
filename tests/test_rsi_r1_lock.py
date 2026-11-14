"""RSI-R1 T5：闭包 LOCK 与双签（Plan §6.5，AC27）。

全部离线：临时仓库根（闭包/接缝文件真实复制）+ 临时 campaign 根。
「同 SHA 完整 LOCK 与独立判据先存在」逐条验证：八组闭包、镜像 digest 钉死、
依赖零漂移、reviewer/verifier 同 SHA 互异双签。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import cli as CLI
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from tests import rsi_r1_helpers as H

REPO = Path(__file__).resolve().parents[1]
IMAGE_PIN = "sim-praxis/sandbox@sha256:" + "5" * 64
NOW = "2026-11-11T15:00:00Z"


def _now() -> str:
    return NOW


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    rels = {rel for files in LK.CLOSURE_GROUPS.values() for rel in files}
    rels |= set(CLI.SEAM_FILES)
    for rel in sorted(rels):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dst)
    return root


def _seed(tmp_path: Path, *, verdict: str = "CALIBRATION_PASS",
          image: str = IMAGE_PIN) -> "tuple[object, Path]":
    ctx = H.make_ctx(tmp_path / "campaign")
    H.make_dirs(tmp_path / "campaign")
    budget = C.default_budget(H.ENVELOPE)
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/envelope.json", {
        "schema": "simpraxis.rsi_r1.envelope.v1", "schema_version": 1,
        "envelope_id": H.ENVELOPE, "budget_sha256": budget.sha256})
    ctx.write_json("contract/instrument_prereg.json", {
        "schema": "simpraxis.rsi_r1.instrument_prereg.v1", "schema_version": 1,
        "document_sha256": "b7" * 32})
    ctx.write_json("contract/calibration_report_v2.json", {
        "schema": "simpraxis.rsi_r1.calibration_report.v2", "schema_version": 2,
        "envelope_id": H.ENVELOPE, "verdict": verdict, "design_version": 2,
        "container_image": image, "prereg_sha256": "bd" * 32})
    return ctx, make_repo(tmp_path)


def _freeze(ctx, repo) -> dict:
    return LK.freeze_lock(ctx, repo_root=repo, now=_now)


def _sign_both(ctx) -> None:
    LK.sign_lock(ctx, role="reviewer", actor="alice", decision="APPROVE",
                 now=_now)
    LK.sign_lock(ctx, role="verifier", actor="bob", decision="APPROVE",
                 now=_now)


# ---------------------------------------------------------------------------
# freeze：不可变 + 八组闭包 + 镜像钉死
# ---------------------------------------------------------------------------

def test_freeze_writes_immutable_lock_and_sha(tmp_path):
    ctx, repo = _seed(tmp_path)
    out = _freeze(ctx, repo)
    lock_path = ctx.relative(LK.LOCK_RELPATH)
    sha_path = ctx.relative(LK.LOCK_SHA_RELPATH)
    assert lock_path.stat().st_mode & 0o777 == 0o600
    assert sha_path.stat().st_mode & 0o777 == 0o600
    assert sha_path.read_text(encoding="utf-8").strip() == out["sha256"]
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    assert doc["schema"] == L.LOCK_SCHEMA
    assert set(doc["closure"]["groups"]) == set(LK.CLOSURE_GROUPS)
    assert doc["image"]["pin"] == IMAGE_PIN
    assert doc["image"]["digest"] == "sha256:" + "5" * 64
    # 与运行期同一解析口径
    lock = L.LockDocument.from_doc(doc)
    assert lock.sha256 == out["sha256"]
    assert lock.phase == LK.PHASE and lock.stage == LK.STAGE
    assert len(lock.dependencies) == len(CLI.SEAM_FILES)


def test_lock_is_immutable(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    before = ctx.relative(LK.LOCK_RELPATH).read_bytes()
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "LOCK_EXISTS"
    assert ctx.relative(LK.LOCK_RELPATH).read_bytes() == before


def test_freeze_requires_passing_calibration(tmp_path):
    ctx, repo = _seed(tmp_path, verdict="DESIGN_NOT_READY")
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "CALIBRATION_NOT_READY"
    assert not ctx.exists(LK.LOCK_RELPATH)


def test_freeze_requires_calibration_document(tmp_path):
    ctx, repo = _seed(tmp_path)
    ctx.relative("contract/calibration_report_v2.json").unlink()
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "CALIBRATION_NOT_READY"


def test_freeze_regenerates_refusals(tmp_path):
    ctx, repo = _seed(tmp_path)
    assert not ctx.exists("contract/refusals.json")
    _freeze(ctx, repo)
    doc = ctx.read_json("contract/refusals.json")
    assert doc["clauses"] == dict(C.REFUSAL_CLAUSES)
    assert "R1-S5-01" in doc["clauses"].values()


def test_freeze_rejects_budget_drift(tmp_path):
    ctx, repo = _seed(tmp_path)
    env = ctx.read_json("contract/envelope.json")
    env["budget_sha256"] = "0" * 64
    ctx.write_json("contract/envelope.json", env)
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "BUDGET_DRIFT"


@pytest.mark.parametrize("bad", ["foo", "img@sha256:xyz", "@sha256:" + "a" * 64,
                                 "img@sha256:" + "A" * 64])
def test_freeze_rejects_bad_image_pin(tmp_path, bad):
    ctx, repo = _seed(tmp_path, image=bad)
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "IMAGE_PIN_INVALID"


def test_freeze_rejects_missing_closure_file(tmp_path):
    ctx, repo = _seed(tmp_path)
    (repo / "orchestrator/rsi_r1/replay.py").unlink()
    with pytest.raises(LK.LockError) as ei:
        _freeze(ctx, repo)
    assert ei.value.code == "CLOSURE_FILE_MISSING"


def test_freeze_binds_checker_and_report(tmp_path):
    ctx, repo = _seed(tmp_path)
    out = _freeze(ctx, repo)
    doc = out["doc"]
    checker = doc["closure"]["groups"]["checker"]["files"]
    assert [f["path"] for f in checker] == [
        "scripts/verification/check_rsi_r1_closure.py"]
    assert doc["calibration"]["verdict"] == "CALIBRATION_PASS"
    assert doc["calibration"]["design_version"] == 2
    assert doc["calibration"]["sha256"] == __import__("hashlib").sha256(
        ctx.relative("contract/calibration_report_v2.json").read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# 双签：同 SHA、APPROVE、actor 互异
# ---------------------------------------------------------------------------

def test_verify_needs_both_signatures(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "SIGNATURES_INCOMPLETE"
    LK.sign_lock(ctx, role="reviewer", actor="alice", decision="APPROVE",
                 now=_now)
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "SIGNATURES_INCOMPLETE"


def test_verify_ok_with_distinct_approvals(tmp_path):
    ctx, repo = _seed(tmp_path)
    out = _freeze(ctx, repo)
    _sign_both(ctx)
    result = LK.verify_lock(ctx, repo_root=repo)
    assert result["ok"] is True
    assert result["lock_sha256"] == out["sha256"]
    assert result["signatures"]["reviewer"]["actor"] == "alice"
    assert result["signatures"]["verifier"]["actor"] == "bob"
    lock, again = LK.require_frozen(ctx, repo_root=repo)
    assert lock.sha256 == out["sha256"] and again["ok"] is True


def test_same_actor_rejected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    LK.sign_lock(ctx, role="reviewer", actor="alice", decision="APPROVE",
                 now=_now)
    with pytest.raises(LK.LockError) as ei:
        LK.sign_lock(ctx, role="verifier", actor="alice", decision="APPROVE",
                     now=_now)
    assert ei.value.code == "SIGNATURE_ACTOR_NOT_DISTINCT"


def test_reject_decision_blocks(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    LK.sign_lock(ctx, role="reviewer", actor="alice", decision="APPROVE",
                 now=_now)
    LK.sign_lock(ctx, role="verifier", actor="bob", decision="REJECT",
                 now=_now)
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "SIGNATURE_REJECTED"


def test_stale_signature_rejected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    sig = ctx.read_json(f"{LK.SIG_DIR}/reviewer.json")
    sig["lock_sha256"] = "f" * 64
    ctx.write_json(f"{LK.SIG_DIR}/reviewer.json", sig)
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "SIGNATURE_STALE"


def test_sign_requires_frozen_lock(tmp_path):
    ctx, repo = _seed(tmp_path)
    with pytest.raises(LK.LockError) as ei:
        LK.sign_lock(ctx, role="reviewer", actor="alice", decision="APPROVE")
    assert ei.value.code == "LOCK_MISSING"


def test_sign_validates_role_and_decision(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    with pytest.raises(LK.LockError) as ei:
        LK.sign_lock(ctx, role="author", actor="alice", decision="APPROVE")
    assert ei.value.code == "SIGNATURE_ROLE"
    with pytest.raises(LK.LockError) as ei:
        LK.sign_lock(ctx, role="reviewer", actor="alice", decision="maybe")
    assert ei.value.code == "SIGNATURE_DECISION"
    with pytest.raises(LK.LockError) as ei:
        LK.sign_lock(ctx, role="reviewer", actor=" ", decision="APPROVE")
    assert ei.value.code == "SIGNATURE_ACTOR"


# ---------------------------------------------------------------------------
# 漂移/篡改检测
# ---------------------------------------------------------------------------

def test_closure_drift_detected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    target = repo / "orchestrator/rsi_r1/rubric.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n",
                      encoding="utf-8")
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "CLOSURE_DRIFT"


def test_dependency_drift_detected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    target = repo / "orchestrator/rsi_r02/candidate.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n",
                      encoding="utf-8")
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "DEPENDENCY_DRIFT"


def test_lock_json_tamper_detected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    doc = ctx.read_json(LK.LOCK_RELPATH)
    first = sorted(doc["dependencies"])[0]
    doc["dependencies"][first] = "e" * 64
    ctx.write_json(LK.LOCK_RELPATH, doc)
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "LOCK_TAMPERED"


def test_sha_file_tamper_detected(tmp_path):
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    ctx.relative(LK.LOCK_SHA_RELPATH).write_text("0" * 64 + "\n",
                                                 encoding="utf-8")
    with pytest.raises(LK.LockError) as ei:
        LK.verify_lock(ctx, repo_root=repo)
    assert ei.value.code == "LOCK_TAMPERED"


def test_runtime_sender_refuses_without_lock(tmp_path):
    ctx, repo = _seed(tmp_path)
    with pytest.raises(L.LoopError) as ei:
        L.load_lock(LK.lock_reader(ctx))
    assert ei.value.code == "LOCK_MISSING"
    assert ei.value.terminal is True


def test_closure_file_list_covers_all_groups(tmp_path):
    ctx, repo = _seed(tmp_path)
    out = _freeze(ctx, repo)
    listed = LK.closure_file_list(out["doc"])
    expected = sorted({rel for files in LK.CLOSURE_GROUPS.values()
                       for rel in files})
    assert sorted(listed) == expected


def test_verify_recomputes_instrument_prereg_sha(tmp_path):
    """CR-28：冻结后替换 instrument_prereg.json 必须报 PREREG_DRIFT。

    闭包组只收 .py，prereg 不在任何 closure group 里；旧 ``verify_lock`` 从
    不重算 LOCK 里登记的 sha，冻结后偷换预注册可零告警通过。
    """
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    ctx.write_json("contract/instrument_prereg.json", {
        "schema": "simpraxis.rsi_r1.instrument_prereg.v1", "schema_version": 1,
        "document_sha256": "c" * 32})
    with pytest.raises(LK.LockError) as exc:
        LK.verify_lock(ctx, repo_root=repo)
    assert exc.value.code == "PREREG_DRIFT"


def test_verify_recomputes_refusals_sha(tmp_path):
    """CR-28：冻结后替换 contract/refusals.json 必须报 REFUSALS_DRIFT。"""
    ctx, repo = _seed(tmp_path)
    _freeze(ctx, repo)
    _sign_both(ctx)
    ctx.write_json("contract/refusals.json", {"schema": "tampered",
                                              "schema_version": 1,
                                              "clauses": {}})
    with pytest.raises(LK.LockError) as exc:
        LK.verify_lock(ctx, repo_root=repo)
    assert exc.value.code == "REFUSALS_DRIFT"
