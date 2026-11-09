"""RSI-R0.2 T4 协议准入脚本的离线测试（零付费、无容器）。

付费路径（LOCK/G-P/真实网关）不在单测里跑；这里钉死两件事：

* 协议链在**专用公开 fixture 文档**下仍能被确定性 actor 走通——即
  ``run_trial(public_doc=fixture.task_doc())`` 这条路与真实模型将要走的
  完全一致（锁内适配 D-T3-13）；
* 每链恰好 4 次发送，证据里逐 POST 记本地计数，链判据含终态与槽绑定。
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

PROTO_ROUNDS = ("preregister", "run_experiment", "analyze", "submit")


def _write_proto_slots(camp) -> None:
    """与 T0 同形：协议槽带 ``required_action``（槽绑定才有对照物）。"""
    slots = [{"slot_id": f"P-{chain}-{i}", "phase": "protocol",
              "family": "protocol", "chain": chain, "round": i,
              "required_action": PROTO_ROUNDS[i - 1], "post_cap": 1,
              "status": ST.SLOT_UNUSED}
             for chain in ("A", "B") for i in range(1, 5)]
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
        assert post["local"]["n_bytes"] == post["local"]["n_bytes"]  # 已记录
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
