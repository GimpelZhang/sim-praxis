"""RV-09/RV-17/RV-18：真实 sender 的 intent/result 绑定与 loop 的重试策略。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_runtime as RT
from orchestrator.gateway.router import EmptyResponseError


class FakeLedger:
    def __init__(self) -> None:
        self.rows: "list[dict]" = []

    def _read_rows(self) -> "list[dict]":
        return list(self.rows)

    def append(self, **row) -> None:
        seq = max((r["seq"] for r in self.rows), default=0) + 1
        self.rows.append({"seq": seq, "event": "call", "ok": True,
                          "model": "fixture-model", "tokens_in": 11,
                          "tokens_out": 22, "task": row.get("task"),
                          "batch_id": row.get("batch_id"), **row})


class FakeGateway:
    max_attempts = 1
    unguarded = False

    def __init__(self, ledger: FakeLedger, *, attempts_http: int = 1) -> None:
        self.ledger = ledger
        self.attempts_http = attempts_http
        self.calls: "list[dict]" = []

    def model_id(self, stage: str):
        return "fixture-model"

    def call(self, stage, messages, *, max_tokens, temperature, timeout,
             capture=None):
        from orchestrator.gateway.quota_ledger import current_billing
        ctx = current_billing()
        self.calls.append({"stage": stage, "messages": messages,
                           "max_tokens": max_tokens,
                           "task": ctx.task if ctx else None,
                           "batch_id": ctx.batch_id if ctx else None})
        self.ledger.append(task=ctx.task if ctx else None,
                           batch_id=ctx.batch_id if ctx else None)
        if capture is not None:
            capture["attempts_http"] = self.attempts_http
            capture["content_bytes"] = 4
        return "okay", (11, 22)


class FakeAdmission:
    def __init__(self, permit: L.Permit) -> None:
        self.permit = permit

    def check_all(self, **kwargs):
        return self.permit


def _request(text: str = "hello") -> T.Request:
    serialized = json.dumps({"system": "s", "messages": [],
                             "max_tokens": RV.RV_OUTPUT_TOKENS},
                            sort_keys=True)
    return T.Request(system="s", messages=(),
                     max_tokens=RV.RV_OUTPUT_TOKENS, serialized=serialized,
                     sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                     n_bytes=len(serialized.encode()), local_tokens=3)


def _sender(tmp_path: Path, *, attempts_http: int = 1):
    ledger = FakeLedger()
    gateway = FakeGateway(ledger, attempts_http=attempts_http)
    transport = RT.RVTransport(gateway=gateway, lane="suffix_A",
                               batch_id="rv-test-suffix_A")
    permit = L.Permit(envelope_id="round", lock_sha256="a" * 64,
                      budget_sha256="b" * 64, phase="rv")
    sender = RT.RVSender(ctx=RT.RVContext(tmp_path), transport=transport,
                         admission=FakeAdmission(permit), ledger=ledger,
                         lane="suffix_A", study="A", round_id="round")
    return sender, permit, ledger, gateway


def test_transport_rejects_gateway_with_hidden_retry():
    gateway = SimpleNamespace(max_attempts=2, unguarded=False)
    with pytest.raises(RV.RVError) as exc:
        RT.RVTransport(gateway=gateway, lane="suffix_A", batch_id="b")
    assert exc.value.code == "HIDDEN_RETRY"


def test_transport_rejects_unknown_lane_and_empty_batch():
    gateway = SimpleNamespace(max_attempts=1)
    with pytest.raises(RV.RVError) as exc:
        RT.RVTransport(gateway=gateway, lane="probe", batch_id="b")
    assert exc.value.code == "LANE_UNKNOWN"
    with pytest.raises(RV.RVError) as exc:
        RT.RVTransport(gateway=gateway, lane="suffix_A", batch_id="")
    assert exc.value.code == "BATCH_MISSING"


def test_sender_records_intent_result_and_ledger_seq(tmp_path):
    sender, permit, ledger, gateway = _sender(tmp_path)
    request = _request()
    call_id = f"round:{request.sha256[:16]}"
    reservation = L.Reservation(call_id=call_id, permit=permit, slot_id="s#1")
    reply = sender.send_once(request, reservation)
    assert reply.text == "okay"
    intent = json.loads((tmp_path / "requests/intents.jsonl").read_text()
                        .splitlines()[0])
    result = json.loads((tmp_path / "requests/results.jsonl").read_text()
                        .splitlines()[0])
    assert intent["call_id"] == call_id
    assert intent["max_tokens"] == RV.RV_OUTPUT_TOKENS
    assert result["ledger_seq"] == ledger.rows[0]["seq"]
    assert result["attempts_http"] == 1
    assert result["max_tokens"] == RV.RV_OUTPUT_TOKENS
    assert gateway.calls[0]["task"] == RV.RV_LANE_TASK_ID["suffix_A"]


def test_sender_rejects_call_id_mismatch(tmp_path):
    sender, permit, _, _ = _sender(tmp_path)
    request = _request()
    reservation = L.Reservation(call_id="round:deadbeef", permit=permit,
                                slot_id="s#1")
    with pytest.raises(L.LoopError) as exc:
        sender.send_once(request, reservation)
    assert exc.value.code == "LOCK_MISMATCH"


def test_sender_unknown_send_is_terminal_and_blocks_retry(tmp_path):
    ledger = FakeLedger()

    class Boom:
        max_attempts = 1

        def call(self, *args, **kwargs):
            raise TimeoutError("socket hang")

    transport = RT.RVTransport(gateway=Boom(), lane="suffix_A",
                               batch_id="rv-test")
    permit = L.Permit(envelope_id="round", lock_sha256="a" * 64,
                      budget_sha256="b" * 64, phase="rv")
    sender = RT.RVSender(ctx=RT.RVContext(tmp_path), transport=transport,
                         admission=FakeAdmission(permit), ledger=ledger,
                         lane="suffix_A", study="A", round_id="round")
    request = _request()
    reservation = L.Reservation(call_id=f"round:{request.sha256[:16]}",
                                permit=permit, slot_id="s#1")
    with pytest.raises(TimeoutError):
        sender.send_once(request, reservation)
    with pytest.raises(RV.RVError) as exc:
        sender.send_once(request, reservation)
    assert exc.value.code == "STOP_UNKNOWN"


class _FakeStore:
    def __init__(self) -> None:
        self.stops: "list[tuple[str, str]]" = []
        self.replies: "list[str]" = []

    def stop_or_block(self, *, code: str, detail: str = "") -> None:
        self.stops.append((code, detail))

    def record_reply(self, **kwargs) -> None:
        self.replies.append(kwargs.get("text", ""))


class _RaisingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send_once(self, request, reservation):
        self.calls += 1
        raise EmptyResponseError("empty")


def _loop(tmp_path: Path, *, retry_policy: str):
    permit = L.Permit(envelope_id="round", lock_sha256="a" * 64,
                      budget_sha256="b" * 64, phase="rv")
    store = _FakeStore()
    sender = _RaisingSender()

    class Budget:
        def reserve_atomic(self, *, call_id, permit):
            return L.Reservation(call_id=call_id, permit=permit, slot_id="s")

        def resolve_or_pause(self, reply):
            return None

    loop = L.ResearchLoop(
        sender=sender, parser=L.parse_action,
        dispatcher=SimpleNamespace(execute_once=lambda a, s: {}),
        store=store,
        messages=lambda: _request(),
        counter=lambda text: 1, admission=FakeAdmission(permit),
        budget=Budget(), envelope=SimpleNamespace(envelope_id="round"),
        root_lock=None, spend_contract=None, retry_policy=retry_policy)
    return loop, sender, store


def test_loop_retry_policy_none_single_attempt(tmp_path):
    loop, sender, store = _loop(tmp_path, retry_policy="none")
    outcome = loop.turn()
    assert outcome.terminal is True
    assert outcome.code == "EmptyResponseError"
    assert sender.calls == 1
    assert store.stops[-1][0] == "EmptyResponseError"


def test_loop_default_retry_keeps_legacy_three_attempts(tmp_path):
    loop, sender, store = _loop(tmp_path, retry_policy="retry")
    outcome = loop.turn()
    assert outcome.terminal is True
    assert sender.calls == 3


def test_loop_rejects_unknown_retry_policy(tmp_path):
    with pytest.raises(L.LoopError) as exc:
        _loop(tmp_path, retry_policy="sometimes")
    assert exc.value.code == "RETRY_POLICY_UNKNOWN"


def test_sender_accepts_loop_call_id_with_attempt_suffix(tmp_path):
    sender, permit, _, _ = _sender(tmp_path)
    request = _request()
    call_id = f"round:{request.sha256[:16]}:a0"
    reservation = L.Reservation(call_id=call_id, permit=permit, slot_id="s#1")
    reply = sender.send_once(request, reservation)
    assert reply.text == "okay"


def test_sender_lineage_filters_by_lane(tmp_path):
    sender, permit, _, _ = _sender(tmp_path)
    # 伪造另一 lane 的行
    other = {"call_id": "x", "lane": "suffix_B", "study": "B",
             "resolved": True, "response_id": "other"}
    (tmp_path / "requests").mkdir(parents=True, exist_ok=True)
    with open(tmp_path / "requests" / "intents.jsonl", "a") as fh:
        fh.write(json.dumps({"call_id": "x", "lane": "suffix_B"}) + "\n")
    with open(tmp_path / "requests" / "results.jsonl", "a") as fh:
        fh.write(json.dumps(other) + "\n")
    request = _request()
    reservation = L.Reservation(call_id=f"round:{request.sha256[:16]}:a0",
                                permit=permit, slot_id="s#1")
    sender.send_once(request, reservation)
    lineage = sender.lineage()
    assert all(r.get("lane") == "suffix_A" for r in lineage["intents"])
    assert all(r.get("lane") == "suffix_A" for r in lineage["results"])
    assert len(lineage["results"]) == 1
