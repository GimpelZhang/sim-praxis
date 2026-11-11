"""RSI-R1 T6 修复储备（Plan §8.1 / AC34）：一次性授权 + 整轮不归零。

本文件的每一条都对着计划那一段的**可机判事实**：

* 授权件一次且仅一次（存在即拒绝重写；被改写即 ``AUTH_TAMPERED``）；
* 旧 root 必须同 envelope、无正式 study 实呼、无既往 repair 占用、确有
  probe/practice 实呼（「模型有效失败不自动解锁储备」的对面：只有已证仪器
  缺陷才值得重做，且必须真的跑过协议）；
* 旧账**逐字节**搬入新 root（旧文件 sha 不变），seq 经生产 ``QuotaLedger``
  续到 4——AC34「整轮新 root 不归零」；
* lane 归属：重做只记 ``repair_reserve``（task 4），门位 probe→FROZEN、
  practice→PROBED，practice 需同 lane 的 probe PASS 在先。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.gateway import quota_ledger as QL
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import repair as RPR
from orchestrator.rsi_r1 import state as S
from tests import rsi_r1_helpers as H

ENVELOPE = H.ENVELOPE
PROBE_TASK = LANES.LANE_TASK_ID["probe"]
PRACTICE_TASK = LANES.LANE_TASK_ID["practice"]
REPAIR_TASK = LANES.LANE_TASK_ID["repair_reserve"]
STUDY_A_TASK = LANES.LANE_TASK_ID["study_A"]


# ---------------------------------------------------------------------------
# 夹具：旧 root（只读证据）+ 新 root（env 与 ctx 同址，供 QuotaLedger 复核）
# ---------------------------------------------------------------------------

def _row(seq: int, task: int, *, ok: bool = True, batch: str = "P26-B1") -> dict:
    return {"seq": seq, "ts_iso": "2026-11-11T00:00:00Z", "task": task,
            "stage": "coding_debug", "batch_id": batch, "ok": ok,
            "global_used_after": seq}


def _old_root(tmp_path: Path, rows: "list[dict] | None" = None,
              *, envelope: str = ENVELOPE,
              reservations: "str | None" = None) -> Path:
    root = tmp_path / "old" / ENVELOPE
    (root / "contract").mkdir(parents=True)
    (root / "quota").mkdir(parents=True)
    (root / "contract" / "envelope.json").write_text(
        json.dumps({"envelope_id": envelope}), encoding="utf-8")
    body = rows if rows is not None else [_row(1, PROBE_TASK),
                                          _row(2, PROBE_TASK),
                                          _row(3, PROBE_TASK)]
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in body)
    (root / "quota" / "ledger.jsonl").write_text(text, encoding="utf-8")
    if reservations is not None:
        (root / "quota" / f"reservations_{RPR.LANE}.jsonl").write_text(
            reservations, encoding="utf-8")
    return root


@pytest.fixture
def new_root(tmp_path, monkeypatch):
    family = tmp_path / "family"
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_ROOT", str(family))
    monkeypatch.setenv("SIMPRAXIS_RSI_R1_CAMPAIGN", ENVELOPE)
    root = family / ENVELOPE
    H.make_dirs(root)
    return root


def _ctx(new_root: Path):
    return H.make_ctx(new_root)


def _auth(ctx, prior: Path, *, decision: str = "授权重开（接 sandbox argv 缺陷）"):
    return RPR.authorize(
        ctx, prior_root=prior, decision_text=decision,
        evidence={"argv_defect": _evidence_file(ctx)},
        clock=lambda: "2026-11-11T00:00:00Z")


def _evidence_file(ctx) -> Path:
    """缺陷证据：真实写一份小文件（sha/bytes 入授权件）。"""
    path = Path(ctx.root) / "defect_repro.txt"
    path.write_text("IndexError: list index out of range\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# authorize：一次性 + 继承
# ---------------------------------------------------------------------------

def test_authorize_carries_old_ledger_byte_for_byte(new_root) -> None:
    old = _old_root(Path(new_root).parents[1])
    prior_raw = (old / "quota" / "ledger.jsonl").read_bytes()
    prior_sha = hashlib.sha256(prior_raw).hexdigest()

    ctx = _ctx(new_root)
    doc = _auth(ctx, old)

    carried = (new_root / "quota" / "ledger.jsonl").read_bytes()
    assert carried == prior_raw, "搬入必须逐字节相等"
    assert hashlib.sha256((old / "quota" / "ledger.jsonl").read_bytes()
                          ).hexdigest() == prior_sha, "旧账只读，不得被改写"
    sc = stat.S_IMODE(os.stat(new_root / "quota" / "ledger.jsonl").st_mode)
    assert sc == 0o600

    manifest = ctx.read_json(RPR.CARRYOVER_RELPATH)
    assert manifest["source_ledger_sha256"] == prior_sha
    assert manifest["target_ledger_sha256"] == prior_sha
    assert manifest["carried_rows"] == 3 and manifest["carried_calls"] == 3
    assert manifest["calls_by_task"] == {str(PROBE_TASK): 3}
    assert "AC34" in manifest["reason"]

    assert doc["lane"] == RPR.LANE and doc["lane_task_id"] == REPAIR_TASK
    assert doc["cap_posts"] == 24
    assert doc["protocols"] == ["practice", "probe"]
    assert doc["posts_by_protocol"] == {"probe": 8, "practice": 16}
    assert doc["prior_calls"] == 3
    # 自锚：重算 body sha 必须等于 document_sha256
    body = {k: v for k, v in doc.items() if k != "document_sha256"}
    assert doc["document_sha256"] == RPR.canonical_sha(body)


def test_authorization_is_single_use_and_tamper_evident(new_root) -> None:
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    _auth(ctx, old)
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, old)
    assert exc.value.code == "REPAIR_ALREADY_AUTHORIZED"

    # 改写任何一个字段（哪怕只加一个空格进 note）都必须被自锚发现
    doc = ctx.read_json(RPR.AUTH_RELPATH)
    doc["note"] = "tampered"
    ctx.write_json(RPR.AUTH_RELPATH, doc)
    with pytest.raises(RPR.RepairError) as exc:
        RPR.load_authorization(ctx)
    assert exc.value.code == "AUTH_TAMPERED"


def test_envelope_mismatch_and_prior_root_checks(new_root, tmp_path) -> None:
    ctx = _ctx(new_root)
    foreign = _old_root(tmp_path, envelope="r1_other")
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, foreign)
    assert exc.value.code == "ENVELOPE_MISMATCH"

    same = Path(new_root)
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, same)
    assert exc.value.code == "PRIOR_ROOT_SAME"

    nested = Path(new_root) / "sub"
    nested.mkdir()
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, nested)
    assert exc.value.code in {"PRIOR_ROOT_INVALID", "PRIOR_ROOT_NESTED"}

    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, tmp_path / "no_such_root")
    assert exc.value.code == "PRIOR_ROOT_MISSING"


def test_study_posts_disqualify_repair(new_root, tmp_path) -> None:
    old = _old_root(tmp_path, [_row(1, PROBE_TASK), _row(2, STUDY_A_TASK)])
    with pytest.raises(RPR.RepairError) as exc:
        _auth(_ctx(new_root), old)
    assert exc.value.code == "STUDY_ALREADY_STARTED"


def test_prior_repair_use_disqualifies_retry(new_root, tmp_path) -> None:
    old = _old_root(tmp_path, [_row(1, PROBE_TASK), _row(2, REPAIR_TASK)])
    with pytest.raises(RPR.RepairError) as exc:
        _auth(_ctx(new_root), old)
    assert exc.value.code == "REPAIR_ALREADY_USED"

    old2 = _old_root(tmp_path / "v2",
                     [_row(1, PROBE_TASK)], reservations='{"slot":1}\n')
    with pytest.raises(RPR.RepairError) as exc:
        _auth(_ctx(new_root), old2)
    assert exc.value.code == "REPAIR_ALREADY_USED"


def test_nothing_to_repair_and_carry_cap(new_root, tmp_path) -> None:
    empty = _old_root(tmp_path / "empty", [])
    with pytest.raises(RPR.RepairError) as exc:
        _auth(_ctx(new_root), empty)
    assert exc.value.code == "NOTHING_TO_REPAIR"

    many = _old_root(tmp_path / "many",
                     [_row(i, PROBE_TASK) for i in range(1, 26)])
    with pytest.raises(RPR.RepairError) as exc:
        _auth(_ctx(new_root), many)
    assert exc.value.code == "CARRY_TOO_LARGE"


def test_authorize_requires_init_and_empty_ledger(new_root, tmp_path) -> None:
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    # 非 INIT 位：先推进到 AUTHORIZED
    S.ResearchState(ctx).advance_root(S.ROOT_AUTHORIZED, reason="test")
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx, old)
    assert exc.value.code == "ROOT_NOT_INIT"

    # 空账前提：新 root 已有账行则拒绝，且不得留下授权件（另起一个干净 root）
    fresh = H.make_dirs(tmp_path / "fresh" / ENVELOPE)
    ctx2 = H.make_ctx(fresh)
    (fresh / "quota" / "ledger.jsonl").write_text(
        json.dumps(_row(1, PROBE_TASK)) + "\n", encoding="utf-8")
    with pytest.raises(RPR.RepairError) as exc:
        _auth(ctx2, old)
    assert exc.value.code == "CARRYOVER_TARGET_NOT_EMPTY"
    assert not ctx2.exists(RPR.AUTH_RELPATH)


def test_carryover_keeps_ledger_seq_continuous(new_root) -> None:
    """AC34 的机判面：搬入 3 行后，生产 ``QuotaLedger`` 的下一笔是 seq=4。"""
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    _auth(ctx, old)

    ledger = QL.QuotaLedger.rsi_r1()          # 走 env 解析，与搬入同址
    assert ledger._ledger_path() == new_root / "quota" / "ledger.jsonl"
    row = ledger.record("coding_debug", task=REPAIR_TASK, batch_id="P26-R1",
                        ok=True, model="m", thread_id="t")
    assert row["seq"] == 4, f"seq 必须整轮连续，实得 {row['seq']}"
    assert row["global_used_after"] == 4
    rows = ledger._read_rows()
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["task"] == PROBE_TASK, "旧行的 task 不得被重写"


# ---------------------------------------------------------------------------
# 门：协议配对 + 单次顺序
# ---------------------------------------------------------------------------

def test_repair_gate_pairs_protocol_with_root(new_root) -> None:
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    with pytest.raises(RPR.RepairError) as exc:
        RPR.require_repair_gate(ctx, root=S.ROOT_FROZEN, protocol="probe")
    assert exc.value.code == "REPAIR_NOT_AUTHORIZED"

    _auth(ctx, old)
    assert RPR.require_repair_gate(ctx, root=S.ROOT_FROZEN,
                                   protocol="probe")["lane"] == RPR.LANE
    assert RPR.require_repair_gate(ctx, root=S.ROOT_PROBED,
                                   protocol="practice")["lane"] == RPR.LANE
    for root, proto, code in (
            (S.ROOT_PROBED, "probe", "ROOT_GATE"),
            (S.ROOT_FROZEN, "practice", "ROOT_GATE"),
            (S.ROOT_CALIBRATED, None, "ROOT_GATE"),
            (S.ROOT_STUDY_A, None, "ROOT_GATE"),
            (S.ROOT_FROZEN, "study_A", "PROTOCOL_UNKNOWN")):
        with pytest.raises(RPR.RepairError) as exc:
            RPR.require_repair_gate(ctx, root=root, protocol=proto)
        assert exc.value.code == code, (root, proto)


def test_practice_requires_same_lane_probe_pass(new_root) -> None:
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    _auth(ctx, old)
    with pytest.raises(RPR.RepairError) as exc:
        RPR.require_protocol_sequence(ctx, protocol="practice")
    assert exc.value.code == "REPAIR_SEQUENCE"

    # 普通 probe（billing=probe）的 PASS 不能给修复 practice 背书
    ctx.write_json("protocol/report_probe.json",
                   {"verdict": "PASS", "billing_lane": "probe"})
    with pytest.raises(RPR.RepairError) as exc:
        RPR.require_protocol_sequence(ctx, protocol="practice")
    assert exc.value.code == "REPAIR_SEQUENCE"

    ctx.write_json("protocol/report_probe.json",
                   {"verdict": "PASS", "billing_lane": RPR.LANE})
    RPR.require_protocol_sequence(ctx, protocol="practice")   # 不抛即通过
    # probe 自身没有前置顺序要求
    RPR.require_protocol_sequence(ctx, protocol="probe")


def test_authorization_can_exclude_a_protocol(new_root) -> None:
    old = _old_root(Path(new_root).parents[1])
    ctx = _ctx(new_root)
    doc = _auth(ctx, old)
    doc["protocols"] = ["probe"]
    doc["document_sha256"] = RPR.canonical_sha(
        {k: v for k, v in doc.items() if k != "document_sha256"})
    ctx.write_json(RPR.AUTH_RELPATH, doc)
    RPR.require_repair_gate(ctx, root=S.ROOT_FROZEN, protocol="probe")
    with pytest.raises(RPR.RepairError) as exc:
        RPR.require_repair_gate(ctx, root=S.ROOT_PROBED, protocol="practice")
    assert exc.value.code == "REPAIR_NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# CLI 面：--repair 与 repair-authorize 必须在命令表里
# ---------------------------------------------------------------------------

def test_cli_exposes_repair_surface() -> None:
    from orchestrator.rsi_r1 import cli as CLI

    parser = CLI.build_parser()
    args = parser.parse_args(["probe", "--envelope", ENVELOPE, "--batch-id", "b",
                              "--repair"])
    assert args.repair is True
    assert parser.parse_args(["practice", "--envelope", ENVELOPE,
                              "--batch-id", "b"]).repair is False
    auth = parser.parse_args(["repair-authorize", "--envelope", ENVELOPE,
                              "--prior-root", "/tmp/old",
                              "--decision-file", "/tmp/d.txt",
                              "--evidence", "a=/tmp/a", "--evidence", "b=/tmp/b"])
    assert auth.func is CLI.cmd_repair_authorize
    assert auth.evidence == ["a=/tmp/a", "b=/tmp/b"]
