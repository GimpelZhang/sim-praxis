"""S1-1 残尾③：headless 会话账——append-only / 半行拒 / 双面不混算。

AC-04：schema 校验、覆写拒绝、崩溃半行拒、usage 缺省 UNKNOWN 不补零、网关面与
会话面分列不求和。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_bridge.session_ledger import (DEFAULT_SESSION_CAP,
                                                    SessionLedger,
                                                    SessionLedgerError)


def test_start_finish_and_chain(tmp_path):
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    ledger.start(session_id="s1", model="m", task="A_N", touchpoint="K1")
    ledger.finish(session_id="s1", usage={"input_tokens": 10,
                                          "output_tokens": 5}, exit_status="ok")
    rows = ledger.read()
    assert [r["kind"] for r in rows] == ["session_start", "session_end"]
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["prev_sha256"] == "0" * 64
    assert rows[1]["prev_sha256"] != rows[0]["prev_sha256"]
    # 重新打开（读侧验链）不抛
    assert SessionLedger(tmp_path / "sessions.jsonl").read() == rows


def test_duplicate_start_and_finish_rejected(tmp_path):
    ledger = SessionLedger(tmp_path / "s.jsonl")
    ledger.start(session_id="s1", model="m", task="t")
    with pytest.raises(SessionLedgerError) as excinfo:
        ledger.start(session_id="s1", model="m", task="t")
    assert excinfo.value.code == "DUPLICATE_START"
    ledger.finish(session_id="s1", usage=None, exit_status="ok")
    with pytest.raises(SessionLedgerError) as excinfo:
        ledger.finish(session_id="s1", usage=None, exit_status="ok")
    assert excinfo.value.code == "DUPLICATE_END"
    with pytest.raises(SessionLedgerError):
        ledger.finish(session_id="ghost", usage=None, exit_status="ok")


def test_half_line_rejected(tmp_path):
    path = tmp_path / "s.jsonl"
    ledger = SessionLedger(path)
    ledger.start(session_id="s1", model="m", task="t")
    with open(path, "ab") as fh:  # 模拟崩溃半行
        fh.write(b'{"schema": "simpraxis.rsi_s1.session_ledger.v1"')
    with pytest.raises(SessionLedgerError) as excinfo:
        SessionLedger(path)
    assert excinfo.value.code == "HALF_LINE"


def test_chain_tamper_rejected(tmp_path):
    path = tmp_path / "s.jsonl"
    ledger = SessionLedger(path)
    ledger.start(session_id="s1", model="m", task="t")
    rows = path.read_text().splitlines()
    row = json.loads(rows[0])
    row["model"] = "tampered"
    path.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n"
                    + json.dumps({"schema": "x"}, ensure_ascii=False) + "\n")
    with pytest.raises(SessionLedgerError):
        SessionLedger(path)


def test_session_cap(tmp_path):
    ledger = SessionLedger(tmp_path / "s.jsonl", hard_cap_sessions=2)
    ledger.start(session_id="s1", model="m", task="t")
    ledger.finish(session_id="s1", usage=None, exit_status="ok")
    ledger.start(session_id="s2", model="m", task="t")
    ledger.finish(session_id="s2", usage=None, exit_status="ok")
    with pytest.raises(SessionLedgerError) as excinfo:
        ledger.start(session_id="s3", model="m", task="t")
    assert excinfo.value.code == "SESSION_CAP"


def test_summary_double_face_never_mixed(tmp_path):
    ledger = SessionLedger(tmp_path / "s.jsonl")
    ledger.start(session_id="s1", model="m", task="A_N", touchpoint="K1")
    ledger.finish(session_id="s1", usage={"input_tokens": 100,
                                          "output_tokens": 20}, exit_status="ok")
    ledger.start(session_id="s2", model="m", task="A_N", touchpoint="K4")
    ledger.finish(session_id="s2", usage=None, exit_status="ok")  # UNKNOWN
    summary = ledger.summary(gateway_rows=[
        {"event": "call", "model": "x"}, {"event": "call", "model": "x"},
        {"event": "selfcheck", "model": None}])
    sf = summary["session_face"]
    assert sf["sessions_started"] == 2 and sf["sessions_finished"] == 2
    assert sf["usage_unknown"] == 1 and sf["usage_reported"] == 1
    assert sf["usage_totals"]["input_tokens"] == 100  # UNKNOWN 不补零
    assert summary["gateway_face"] == {"calls": 2, "models": ["x"]}
    assert "session_face" in summary and "gateway_face" in summary
    # 两面不相加：summary 里没有任何"总费用"混算键
    assert "total_cost" not in json.dumps(summary)
    assert DEFAULT_SESSION_CAP == 20
