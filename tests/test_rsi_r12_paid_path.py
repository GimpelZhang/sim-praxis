"""RSI-R1.2 T6 准入路径（零 POST）：费用门 → 校准验签导入 → 付费冻结绑定。

覆盖（Plan §6.1 顺序、L212/L247、AC45/AC55）：

* ``init --mode paid`` 只建 BLOCKED 门；``authorize --consent`` 是唯一解锁点，
  旧 schema（R1 豁免）一律拒、授权件不可覆盖、重复执行为 no-op；
* ``freeze`` 目的分派：paid 根必须 ``--validation-report``（T5 单向绑定），
  离线根不得携带；``build_validation_binding`` 对非 T5 根 fail-closed；
* 付费 LOCK 摘要：带 ``validation_binding`` 时整份进摘要（改绑定即换 sha），
  旧付费 LOCK 无该键时口径逐字节不变；
* 付费命令的三道闸次序：环境闸→费用门（consent 字节漂移）→campaign 根一致；
* AC45：root 只在 PASS 时推进，失败停门——probe 失败后 root 仍 FROZEN，
  practice 的 ``_check_gate`` 不可达。

全程零 socket / 零模型 POST。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import cli_r12
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import state as ST
from orchestrator.rsi_r1 import study as STU

FROZEN_LEDGERS = tuple(str(s["ledger_sha256"]) for s in P.HISTORY_SOURCES)


def _init(tmp_path: Path, monkeypatch, *, envelope: str = "env_t6",
          mode: str = "paid") -> tuple[Path, Path, Path]:
    family = tmp_path / "rsi_r12"
    family.mkdir()
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(family))
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", envelope)
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    plan = tmp_path / "plan.md"
    plan.write_text("plan bytes", encoding="utf-8")
    rc = cli_r12.main(["init", "--envelope", envelope, "--mode", mode,
                       "--plan", str(plan)])
    assert rc == 0
    return family / envelope, plan, family


def _envelope(root: Path) -> "dict[str, object]":
    return json.loads((root / "contract" / "envelope.json").read_text(
        encoding="utf-8"))


def _consent_doc(envelope: "dict[str, object]", plan: Path, **over: object
                 ) -> "dict[str, object]":
    doc: "dict[str, object]" = {
        "schema": C.CONSENT_SCHEMA_V2,
        "schema_version": P.SCHEMA_VERSION,
        "envelope_id": envelope["envelope_id"],
        "profile_id": P.PROFILE_R12,
        "decision": C.COST_DECISION_SUPPLY_V2,
        "scope": "R1.2",
        "granted_at_utc": "2026-11-12T00:00:00Z",
        "granted_by": "user",
        "verbatim": "",
        "plan_path": str(plan),
        "plan_sha256": envelope["plan_sha256"],
        "family_cap": P.FAMILY_CAP,
        "history_posts": P.FAMILY_HISTORY_POSTS,
        "history_ledger_sha256": list(FROZEN_LEDGERS),
        "risk_reserve_usd": "0",
        "exposure_note": "",
        "supersedes": [],
        "notes": "",
    }
    doc.update(over)
    return doc


def _write_consent(tmp_path: Path, doc: "dict[str, object]",
                   name: str = "consent.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# authorize：唯一解锁点
# ---------------------------------------------------------------------------

def test_authorize_unlocks_cost_gate_and_advances_root(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    assert env0["cost_gate"]["paid_authorized"] is False
    consent = _write_consent(tmp_path, _consent_doc(env0, plan))

    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(consent)]) == 0
    env1 = _envelope(root)
    assert env1["cost_gate"]["paid_authorized"] is True
    assert env1["cost_gate"]["decision"] == C.COST_DECISION_SUPPLY_V2
    written = root / "contract" / "consent.json"
    assert env1["cost_gate"]["consent_sha256"] == hashlib.sha256(
        written.read_bytes()).hexdigest()
    ctx = cli_r12._r12_ctx(root, "env_t6")
    assert ST.ResearchState(ctx).root_state() == ST.ROOT_AUTHORIZED
    # 幂等：同一授权件重复执行 no-op（不覆盖、不重复记 history）。
    history = root / "contract" / "cost_gate_history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(consent)]) == 0
    assert history.read_text(encoding="utf-8").splitlines() == lines


def test_authorize_refuses_different_consent_bytes(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    first = _write_consent(tmp_path, _consent_doc(env0, plan), "c1.json")
    second = _write_consent(tmp_path, _consent_doc(
        env0, plan, granted_at_utc="2026-11-13T00:00:00Z"), "c2.json")
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(first)]) == 0
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(second)]) == 2


def test_authorize_refuses_legacy_consent_schema(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    legacy = _write_consent(tmp_path, {
        "schema": C.CONSENT_SCHEMA,       # 旧 R1 豁免：新 scope 不承继
        "envelope_id": env0["envelope_id"],
        "decision": "SUPPLY_HARD_GUARANTEE",
        "granted_at_utc": "2026-11-12T00:00:00Z",
        "plan_path": str(plan),
        "plan_sha256": env0["plan_sha256"],
    })
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(legacy)]) == 2


def test_authorize_refuses_stale_plan_binding(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    stale = _write_consent(tmp_path, _consent_doc(env0, plan, plan_sha256="a" * 64))
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(stale)]) == 2
    assert _envelope(root)["cost_gate"]["paid_authorized"] is False


# ---------------------------------------------------------------------------
# freeze：目的分派与 T5 单向绑定
# ---------------------------------------------------------------------------

def test_paid_freeze_requires_validation_report(tmp_path, monkeypatch):
    root, _, _ = _init(tmp_path, monkeypatch)
    assert cli_r12.main(["freeze", "--root", str(root)]) == 2
    assert cli_r12.main(["freeze", "--root", str(root),
                         "--purpose", "validation"]) == 2
    assert not (root / "contract" / "lock.json").exists()


def test_offline_freeze_rejects_paid_binding(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch, mode="offline_validation")
    assert cli_r12.main(["freeze", "--root", str(root),
                         "--validation-report", str(plan)]) == 2
    assert not (root / "contract" / "lock.json").exists()


def test_build_validation_binding_fails_closed_on_non_t5_root(tmp_path):
    empty = tmp_path / "not_a_root"
    empty.mkdir()
    with pytest.raises(LK.LockError):
        LK.build_validation_binding(empty)
    # 有 envelope 但没验证 LOCK：同样拒（不许把任意根当 T5 来源）。
    (empty / "contract").mkdir()
    (empty / "contract" / "envelope.json").write_text(
        json.dumps({"envelope_id": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(LK.LockError):
        LK.build_validation_binding(empty)


def test_paid_lock_digest_covers_validation_binding():
    base = {"schema": L.LOCK_SCHEMA, "envelope_id": "e",
            "budget_sha256": "a" * 64, "stage": "validate", "phase": "p",
            "dependencies": {"orchestrator/x.py": "b" * 64}}
    legacy = L.LockDocument.from_doc(base)
    assert legacy.purpose == "paid"
    assert legacy.sha256 == L.LockDocument.from_doc(dict(base)).sha256

    one = L.LockDocument.from_doc(dict(base, validation_binding={
        "validation_root": "r12-t5-offline-a", "lock_sha256": "c" * 64}))
    two = L.LockDocument.from_doc(dict(base, validation_binding={
        "validation_root": "r12-t5-offline-b", "lock_sha256": "c" * 64}))
    assert one.purpose == "paid"
    assert one.sha256 != legacy.sha256          # 绑定进入锚点
    assert one.sha256 != two.sha256             # 换源即换 sha

    # 旧付费 LOCK（无 validation_binding）摘要口径逐字节不变：加无关键不动 sha。
    assert L.LockDocument.from_doc(dict(base, note="x")).sha256 == legacy.sha256


# ---------------------------------------------------------------------------
# 付费命令三道闸（环境 → 费用门 → campaign 根）；一切在触网/落批准件之前
# ---------------------------------------------------------------------------

def test_paid_probe_requires_env_gate(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    consent = _write_consent(tmp_path, _consent_doc(env0, plan))
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(consent)]) == 0
    rc = cli_r12.main(["probe", "--root", str(root), "--batch-id", "b0",
                       "--tokenizer", "/nonexistent", "--tokenizer-sha256",
                       "0" * 64])
    assert rc == 2
    # 环境闸先于任何副作用：批准件/协议报告均不得出现。
    approvals = root / "quota" / "approvals"
    assert not approvals.exists() or not list(approvals.iterdir())
    assert not list((root / "protocol").iterdir())


def test_paid_probe_refuses_consent_byte_drift(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    consent = _write_consent(tmp_path, _consent_doc(env0, plan))
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(consent)]) == 0
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    written = root / "contract" / "consent.json"
    written.write_text(written.read_text(encoding="utf-8") + " ",
                       encoding="utf-8")
    assert cli_r12.main(["probe", "--root", str(root), "--batch-id", "b0",
                         "--tokenizer", "/nonexistent", "--tokenizer-sha256",
                         "0" * 64]) == 2
    assert not list((root / "protocol").iterdir())


def test_paid_probe_refuses_campaign_root_mismatch(tmp_path, monkeypatch):
    root, plan, _ = _init(tmp_path, monkeypatch)
    env0 = _envelope(root)
    consent = _write_consent(tmp_path, _consent_doc(env0, plan))
    assert cli_r12.main(["authorize", "--root", str(root),
                         "--consent", str(consent)]) == 0
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_CAMPAIGN", "another-envelope")
    assert cli_r12.main(["probe", "--root", str(root), "--batch-id", "b0",
                         "--tokenizer", "/nonexistent", "--tokenizer-sha256",
                         "0" * 64]) == 2
    assert not list((root / "protocol").iterdir())


# ---------------------------------------------------------------------------
# AC45：失败停门（root 只在 PASS 时推进）
# ---------------------------------------------------------------------------

def test_failed_lane_never_reaches_next_gate(tmp_path, monkeypatch):
    """probe 未 PASS ⇒ root 停在 FROZEN，practice 的 ROOT_GATE 不可达。

    ``run_protocol`` 只在 verdict PASS 时 ``advance_root``（协议语义由
    ``tests/test_rsi_r12_protocol.py`` 的有界链用例覆盖）；这里钉的是门位：
    study-A 需要 PRACTICED，故单 lane 失败即禁 A/B（Plan AC45）。
    """
    root, _, _ = _init(tmp_path, monkeypatch)
    ctx = cli_r12._r12_ctx(root, "env_t6")
    st = ST.ResearchState(ctx)
    assert st.root_state() == ST.ROOT_INIT
    st.advance_root(ST.ROOT_AUTHORIZED)
    st.advance_root(ST.ROOT_CALIBRATED)
    st.advance_root(ST.ROOT_FROZEN)
    assert st.root_state() == ST.ROOT_FROZEN
    assert STU.STUDY_GATE["A"] == ST.ROOT_PRACTICED
    with pytest.raises(ST.StateError):
        st.advance_root(ST.ROOT_PRACTICED)      # 只能 +1 步，不由测试代推
    assert st.root_state() == ST.ROOT_FROZEN


# ---------------------------------------------------------------------------
# AC55：R1.2 付费 LOCK 必须携带 T5 绑定（freeze 与 verify 两侧都拒）
# ---------------------------------------------------------------------------

def _r12_rehearsal(tmp_path, name="r12base"):
    """排演根（真闭包/真双签），envelope 标成 rsi-r1.2 后返回 driver。"""
    from tests.rsi_r1_rehearsal import RehearsalDriver

    drv = RehearsalDriver(tmp_path / name)
    drv.setup()
    env_path = drv.ctx.root / "contract" / "envelope.json"
    doc = json.loads(env_path.read_text(encoding="utf-8"))
    doc["profile_id"] = P.PROFILE_R12
    env_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    return drv


def test_r12_paid_freeze_refuses_without_binding(tmp_path):
    from tests.rsi_r1_rehearsal import REHEARSAL_IMAGE

    drv = _r12_rehearsal(tmp_path)
    with pytest.raises(LK.LockError) as exc:
        LK.freeze_lock(drv.ctx, repo_root=LK.REPO_ROOT,
                       image_pin=REHEARSAL_IMAGE)
    assert exc.value.code == "PAID_BINDING_MISSING"
    assert not (drv.ctx.root / "contract" / "lock.json").exists()


def test_r12_paid_lock_without_binding_refused_at_verify(tmp_path):
    """旧式（未绑定 T5）付费 LOCK 被搬进 R1.2 根：verify 一律拒。"""
    import shutil

    from tests import rsi_r1_helpers as H
    from tests.rsi_r1_rehearsal import RehearsalDriver

    legacy = RehearsalDriver(tmp_path / "legacy")
    legacy.setup()
    legacy.freeze()                      # 旧口径付费 LOCK + 双签
    copy = tmp_path / "as_r12"
    shutil.copytree(legacy.ctx.root, copy)
    env_path = copy / "contract" / "envelope.json"
    doc = json.loads(env_path.read_text(encoding="utf-8"))
    doc["profile_id"] = P.PROFILE_R12
    env_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    ctx = H.make_ctx(copy, H.ENVELOPE)
    with pytest.raises(LK.LockError) as exc:
        LK.verify_lock(ctx, repo_root=LK.REPO_ROOT)
    assert exc.value.code == "PAID_BINDING_MISSING"
    # 旧 R1 根（无 profile_id）不受影响：同一 LOCK 在朋友圈里仍然有效。
    assert LK.verify_lock(legacy.ctx, repo_root=LK.REPO_ROOT)["ok"] is True
