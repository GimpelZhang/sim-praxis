"""RSI-R0.2 T4 协议准入脚本的离线测试（零付费、无容器）。

付费路径（LOCK/G-P/真实网关）不在单测里跑；这里钉死两件事：

* 协议链在**专用公开 fixture 文档**下仍能被确定性 actor 走通——即
  ``run_trial(public_doc=fixture.task_doc())`` 这条路与真实模型将要走的
  完全一致（锁内适配 D-T3-13）；
* 干净链仍是 4 次发送（4 必做动作），但槽表给每链 6 个（+2 自修余量）；证据里
  逐 POST 记本地计数，链判据含终态与槽绑定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts" / "rsi_r02"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import t4_protocol as T4  # noqa: E402

from orchestrator.rsi_r02 import protocol_fixture as PF  # noqa: E402
from orchestrator.rsi_r02 import state as ST  # noqa: E402
from tests import rsi_r02_helpers as H  # noqa: E402

PROTO_ROUNDS = ("preregister", "run_experiment", "analyze", "submit", None, None)


def _write_proto_slots(camp) -> None:
    """与 T0 同形：4 必做槽带 ``required_action``，后 2 槽是自修余量（None）。"""
    slots = [{"slot_id": f"P-{chain}-{i}", "phase": "protocol",
              "family": "protocol", "chain": chain, "round": i,
              "required_action": PROTO_ROUNDS[i - 1], "post_cap": 1,
              "status": ST.SLOT_UNUSED}
             for chain in ("A", "B") for i in range(1, 7)]
    camp.write_json("quota/slots.json", {
        "schema": ST.SCHEMA_SLOTBOOK,
        "recorded_at_utc": "2026-11-09T00:00:00Z",
        "counts": {"total": len(slots)}, "slots": slots})


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    H.set_env(tmp_path, monkeypatch)
    camp = H.make_campaign(tmp_path)
    _write_proto_slots(camp)
    # 协议 trial 复用 E-dev 的物理实例：它的公开文档也必须存在（缺省路径用）
    H.write_public_task(camp, "E-dev", family="E")
    return camp


def test_run_chain_uses_protocol_doc_and_sends_exactly_four(ctx):
    """干净链（actor 从公开文档读字段）恰好四次发送即完成四动作。"""
    fixture = PF.make_fixture(campaign_id=ctx.campaign_id, fixture_id="P1",
                              slot_id="P-A-1", instance_id="E-dev")
    stub = PF.ProtocolStub(fixture=fixture)
    rec = T4.run_chain(ctx, "P1", caller=stub, counter=lambda t: 100, gpus=False)
    assert rec["chain_ok"] is True
    assert rec["status"] == ST.TRIAL_SUBMITTED
    assert rec["posts_recorded"] == rec["posts_sent"] == 4
    # 模型确实看到了协议文档（nonce 进系统提示），不是凭内存答
    assert fixture.nonce in stub.requests[0].system
    for post in rec["posts"]:
        # 本地计数必须**真的量到东西**（自我相等是恒真断言，评审 MINOR-1）
        assert isinstance(post["local"]["n_bytes"], int)
        assert post["local"]["n_bytes"] > 0
        assert post["local"]["local_tokens"] > 0
        assert len(post["local"]["request_sha256"]) == 64
        assert post["provider_reported"]["attempts_http"] == 1
    assert all(item["ok"] for item in rec["slot_binding"])


def test_usage_rows_keep_local_counts_and_reported_values(ctx):
    fixture = PF.make_fixture(campaign_id=ctx.campaign_id, fixture_id="P2",
                              slot_id="P-B-1", instance_id="E-dev")
    T4.run_chain(ctx, "P2", caller=PF.ProtocolStub(fixture=fixture),
                 counter=lambda t: 100, gpus=False)
    events = ST.Journal(ctx).for_trial("proto_P2")
    rows = T4._usage_rows(events)
    assert len(rows) == 4
    assert {r["slot_id"] for r in rows} == {"P-B-1", "P-B-2", "P-B-3", "P-B-4"}
    assert all(r["local"]["n_bytes"] > 0 for r in rows)
    assert all(r["provider_reported"]["provider_model"] == "protocol_stub"
               for r in rows)


def test_failed_chain_cancels_its_own_unused_slots_too(ctx):
    """§6.1「剩余槽如实取消」必须含**失败链自身**未发送的轮次（评审 MINOR-2）。

    只取消后继链时，P1 若在第 1 轮就失败，P-A-2..4 会永远停在 ``UNUSED``，
    槽账看上去像「还打算发」——与「不补考」的结论相反。已发送的槽（这里把
    P-A-1 手动置为 SUBMITTED 模拟）绝不能被改写成取消。
    """
    ST.SlotBook(ctx).claim("P-A-1", trial_id="proto_P1", call_id="c1")

    cancelled = T4.cancel_pending_slots(ctx, "P1")
    assert cancelled == ["P-A-2", "P-A-3", "P-A-4", "P-A-5", "P-A-6",
                         "P-B-1", "P-B-2", "P-B-3", "P-B-4", "P-B-5", "P-B-6"]

    book = ST.SlotBook(ctx)
    assert book.status("P-A-1") == ST.SLOT_SENT      # 已发槽不被改写成取消
    assert all(book.status(sid) == ST.SLOT_CANCELLED for sid in cancelled)


# ---------------------------------------------------------------------------
# 批次凭据自检（T4 首跑事故的修复：凭据缺口必须在第一次 POST 之前暴露）
# ---------------------------------------------------------------------------

def _write_credential(ctx, **over) -> None:
    cred = {"schema": "simpraxis.quota.batch_approval.v1",
            "batch_id": "R02-T4-1", "task": 0, "stage": "coding_debug",
            "approved": 12, "beyond_normal": False,
            "approved_by": "agent_self_approval",
            "note": "T4 协议准入 12 POST（4 必做 + 2 余量，两链；G-S 豁免 $1.5/POST，批帽 $100）"}
    cred.update(over)
    ctx.write_json("quota/approvals/R02-T4-1.json", cred)


def test_batch_credential_missing_refuses_before_any_post(ctx):
    out = T4.check_batch_credential(ctx, "R02-T4-1")
    assert out["ok"] is False
    assert out["problems"] and out["problems"][0].startswith("CREDENTIAL_MISSING")
    assert out["credential"] is None


def test_batch_credential_requires_task_stage_and_coverage(ctx):
    _write_credential(ctx, task=1)
    problems = T4.check_batch_credential(ctx, "R02-T4-1")["problems"]
    assert problems == ["CREDENTIAL_TASK_MISMATCH: 1 != 0"]

    _write_credential(ctx, stage="visual_rollout")
    problems = T4.check_batch_credential(ctx, "R02-T4-1")["problems"]
    assert problems == ["CREDENTIAL_STAGE_MISMATCH: visual_rollout != coding_debug"]

    _write_credential(ctx)
    out = T4.check_batch_credential(ctx, "R02-T4-1")
    assert out["ok"] is True and out["credential"]["approved"] == 12

    _write_credential(ctx, approved=11)
    problems = T4.check_batch_credential(ctx, "R02-T4-1")["problems"]
    assert problems == ["CREDENTIAL_COVERAGE: approved=11 < 本批 12 次"]

    # bool 是 int 子类：{"approved": true} 不得被当成 1 次书面批准——load_approval
    # 在更早一层就拒（凭据损坏/字段不合法 ⇒ 当缺件），本检查保持同一结论。
    _write_credential(ctx, approved=True)
    problems = T4.check_batch_credential(ctx, "R02-T4-1")["problems"]
    assert problems and problems[0].startswith("CREDENTIAL_MISSING")


def test_verdict_fail_with_submitted_status_is_recorded_as_fact(ctx):
    """submit 被收下但统计错 ⇒ trial 仍是 SUBMITTED，判负必须落成事件。

    评审 HIGH 的真实形态：``candidate`` 在 submit 结构合法时立刻把 trial 置为终态
    ``SUBMITTED``（终态不许互改），``verify_run`` 的统计/nonce 判负发生在之后。
    若宿主只把 chain_ok 写进 g_d，closeout 只看 status 就会把这条 §10.2 明列的
    「统计错」协议 FAIL 印成 ``PROTOCOL_OK / measurement=VALID``。本用例证明：
    判负现在写进 journal（``code=PROTOCOL_FAIL``），closeout 按事件判 BLOCKED。
    """
    import dataclasses
    import json as _json

    from orchestrator.rsi_r02 import closeout as CO

    class _BadStatStub(PF.ProtocolStub):
        def __call__(self, request):
            reply = super().__call__(request)
            try:
                doc = _json.loads(reply.text)
            except ValueError:
                return reply
            if doc.get("action") == "submit":
                doc["stat"] = float(doc.get("stat") or 0.0) + 1.0   # 统计错但不缺字段
                return dataclasses.replace(reply, text=_json.dumps(
                    doc, ensure_ascii=False, sort_keys=True))
            return reply

    fixture = PF.make_fixture(campaign_id=ctx.campaign_id, fixture_id="P1",
                              slot_id="P-A-1", instance_id="E-dev")
    stub = _BadStatStub(fixture=fixture)
    rec = T4.run_chain(ctx, "P1", caller=stub, counter=lambda t: 100, gpus=False)

    assert rec["chain_ok"] is False
    # 关键：trial 状态看不出判负（submit 结构上被收下，candidate 已置终态）
    assert rec["status"] == ST.TRIAL_SUBMITTED
    assert any("统计与分析产物不符" in p or "stat" in p
               for p in rec["verdict"]["problems"])

    events = ST.Journal(ctx).for_trial("proto_P1")
    fails = [e for e in events if e.get("code") == ST.EV_PROTOCOL_FAIL]
    assert len(fails) == 1
    assert fails[0]["action"] == "verify" and fails[0]["ok"] is False
    assert fails[0]["payload"]["problems"] == rec["verdict"]["problems"]

    verdict = CO.judge(CO.read_facts(ctx))
    assert verdict["protocol"]["status"] == CO.PROTOCOL_BLOCKED
    assert verdict["protocol"]["failed"] == ["proto_P1"]
    assert verdict["measurement"]["status"] == CO.MEAS_INCOMPLETE
    assert "PROTOCOL_BLOCKED" in verdict["measurement"]["reasons"]
