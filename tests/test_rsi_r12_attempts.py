"""RSI-R1.2 T3：attempt 唯一报告（Plan §3.2；AC25/26）。

覆盖：

* AC25——**每一次尝试都有唯一报告**：0 呼闸门失败、解析错、网关异常、
  普通异常各自留下独立报告；报告在闸门之前就以 ``started`` 落盘。
* AC26——报告内的 ``posts`` 与账本行数增量一致；最新指针只是 hash 指针，
  旧 0 呼报告不会覆盖后续有呼失败的历史报告。

全部离线：不构造 gateway/sender，不发任何 POST。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator.gateway.router import GatewayError
from orchestrator.rsi_r1 import attempts as ATT
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from tests.rsi_r1_helpers import make_ctx

PROFILE = P.PROFILE_R12_DEF
LANE = "practice"


def _ctx(tmp_path: Path):
    return make_ctx(tmp_path)


def _report(ctx, lane: str, attempt_id: str) -> dict:
    path = Path(ctx.root) / ATT.ATTEMPTS_SUBDIR / lane / attempt_id / "report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _append_ledger(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for _ in range(n):
            fh.write('{"event": "call"}\n')


# ---------------------------------------------------------------------------
# AC25：0 呼闸门/解析/Gateway/普通异常都留下独立报告
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,expect_status", [
    (PT.ProtocolError("COST_GATE_BLOCKED", "paid 未授权"), ATT.STATUS_TERMINATED),
    (PT.ProtocolError("MALFORMED_JSON", "回复不可解析"), ATT.STATUS_TERMINATED),
    (GatewayError("empty response"), ATT.STATUS_INSTRUMENT_FAILURE),
    (ValueError("unexpected"), ATT.STATUS_INSTRUMENT_FAILURE),
])
def test_every_attempt_leaves_exactly_one_report(tmp_path, exc, expect_status):
    ctx = _ctx(tmp_path)
    with pytest.raises(type(exc)):
        with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
            attempt_id = att.attempt_id
            raise exc
    report = _report(ctx, LANE, attempt_id)
    assert report["schema"] == ATT.REPORT_SCHEMA
    assert report["attempt_id"] == attempt_id
    assert report["lane"] == LANE
    assert report["status"] == expect_status
    assert report["posts"]["delta"] == 0 and report["posts"]["after"] == 0
    assert report["teardown"]["recorded"] is True
    assert report["ended_at_utc"] is not None
    if expect_status == ATT.STATUS_TERMINATED:
        assert report["result"]["code"] == exc.code
        assert report["reason"].startswith(exc.code)
    else:
        assert report["result"]["code"] == ATT.CODE_INSTRUMENT_FAILURE
        assert report["exception_type"].startswith("orchestrator.") or \
            report["exception_type"].startswith("builtins.")


def test_started_report_is_written_before_any_gate(tmp_path):
    ctx = _ctx(tmp_path)
    with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
        # 闸门（费用/LOCK/paused/tokenizer）都在这之后；先落盘的报告证明
        # 提前阻塞的尝试同样"发生过"，不是 0 呼即无记录。
        early = _report(ctx, LANE, att.attempt_id)
        assert early["status"] == ATT.STATUS_STARTED
        assert early["posts"]["delta"] is None  # 尚未收尾，不填 0 冒充完成
        assert early["worker_authored"] is True
        att.finish({"ok": True})
    assert _report(ctx, LANE, att.attempt_id)["status"] == ATT.STATUS_COMPLETED


def test_illegal_lane_creates_no_attempt(tmp_path):
    # 路径/身份合法性验证先于报告：非法 lane 不产生任何报告目录。
    ctx = _ctx(tmp_path)
    with pytest.raises(ATT.AttemptError) as exc:
        with ATT.AttemptSession(ctx, "not-a-lane", PROFILE):
            pass
    assert exc.value.code == "LANE_UNKNOWN"
    assert not (Path(ctx.root) / ATT.ATTEMPTS_SUBDIR).exists()


def test_attempt_ids_do_not_collide(tmp_path):
    ctx = _ctx(tmp_path)
    ids = []
    for _ in range(3):
        with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
            ids.append(att.attempt_id)
            att.finish({"ok": False, "code": "X"})
    assert ids == ["att-001", "att-002", "att-003"]
    assert len(ATT.attempt_reports(ctx, LANE)) == 3


# ---------------------------------------------------------------------------
# AC26：ledger delta 与报告一致；旧报告不覆盖新失败
# ---------------------------------------------------------------------------

def test_ledger_delta_matches_report(tmp_path):
    ctx = _ctx(tmp_path)
    ledger = Path(tmp_path) / "quota" / "ledger.jsonl"
    _append_ledger(ledger, 3)
    with ATT.AttemptSession(ctx, LANE, PROFILE, ledger_path=ledger) as att:
        _append_ledger(ledger, 2)  # 模拟两次实呼落账
        att.finish({"ok": True, "verdict": "PASS"})
    report = _report(ctx, LANE, att.attempt_id)
    assert report["posts"] == {"before": 3, "after": 5, "delta": 2,
                               "ledger_rows_after": 5}
    assert report["status"] == ATT.STATUS_COMPLETED


def test_old_zero_post_report_cannot_cover_later_failure(tmp_path):
    ctx = _ctx(tmp_path)
    ledger = Path(tmp_path) / "quota" / "ledger.jsonl"
    # 第一次尝试：0 呼成功（旧报告）
    with ATT.AttemptSession(ctx, LANE, PROFILE, ledger_path=ledger) as first:
        first.finish({"ok": True})
    # 第二次尝试：2 呼后失败
    with pytest.raises(PT.ProtocolError):
        with ATT.AttemptSession(ctx, LANE, PROFILE, ledger_path=ledger) as second:
            _append_ledger(ledger, 2)
            raise PT.ProtocolError("GATEWAY_EXHAUSTED", "两次后失败")
    first_report = _report(ctx, LANE, first.attempt_id)
    second_report = _report(ctx, LANE, second.attempt_id)
    assert first_report["posts"]["delta"] == 0
    assert second_report["posts"]["delta"] == 2
    # latest.json 只是 hash 指针：指向最新，且 sha 与文件字节一致
    pointer = ATT.latest_pointer(ctx, LANE)
    assert pointer["attempt_id"] == second.attempt_id
    path = Path(ctx.root) / pointer["report_relpath"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == \
        pointer["report_sha256"]
    # closeout 遍历全部 attempt：总 POST = 0 + 2，失败的那次无法被 0 呼报告冲掉
    reports = ATT.attempt_reports(ctx, LANE)
    assert len(reports) == 2
    assert sum(r["posts"]["delta"] for r in reports) == 2
    assert any(r["status"] == ATT.STATUS_TERMINATED for r in reports)


def test_handshake_file_quotes_terminal_report(tmp_path):
    ctx = _ctx(tmp_path)
    with ATT.AttemptSession(ctx, LANE, PROFILE) as att:
        att.finish({"ok": True})
    doc = json.loads((Path(tmp_path) / "state" / "worker_completed.json")
                     .read_text(encoding="utf-8"))
    assert doc["schema"] == ATT.COMPLETION_SCHEMA
    assert doc["attempt_id"] == att.attempt_id
    report_path = Path(ctx.root) / doc["report_relpath"]
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == \
        doc["report_sha256"]
    assert doc["status"] == ATT.STATUS_COMPLETED
