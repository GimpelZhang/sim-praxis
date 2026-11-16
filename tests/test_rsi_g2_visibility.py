"""RSI-G2 可见性：真实 turn 边界捕获、最小交付与历史固定携带（零 POST）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import g2_cli as CLI
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import g1_trace as GT
from orchestrator.rsi_r1 import loop as L

PLAN = "d" * 64


def _envelope(case_id: str = "vis", n_contrasts: int = 1) -> "dict":
    from tests.test_rsi_g2_contract import contract
    ids = [f"k{i}" for i in range(n_contrasts)]
    contrasts = [{"contrast_id": cid, "metric_id": "m1",
                  "proposition": "increase_gt", "delta": 0.001,
                  "estimate": 0.05, "lower": 0.01, "upper": 0.09,
                  "level": 0.9, "science_status": "SUPPORTED",
                  "n_blocks": 4, "dropped": {"count": 0, "ids": [],
                                             "ids_sha256": ""}}
                 for cid in ids]
    return G2.build_envelope(
        study_id=case_id, phase="confirm", plan_sha256=PLAN,
        analysis_receipt_sha256="c" * 64, analysis_output_sha256="b" * 64,
        scope_contract=contract(), required_contrast_ids=ids,
        contrasts=contrasts, primary_contrast_id=ids[0], denominator=4,
        dropped={"count": 0, "ids": [], "ids_sha256": ""},
        diagnostics={"status": "OBSERVED"},
        artifacts=[{"artifact_id": "analysis/output.json",
                    "relative_path": "analysis/output.json",
                    "sha256": "b" * 64, "bytes": 10,
                    "access_mode": "read_result_artifact"}],
        evidence=[{"artifact_id": "analysis/output.json",
                   "json_pointer": "/contrasts/0/estimate",
                   "field_sha256": G2.field_sha(0.05)}])


class _Store:
    def __init__(self) -> None:
        self.turns: "list[T.Turn]" = []
        self.last_text = ""

    def history(self) -> "list[T.Turn]":
        return list(self.turns)

    def record_reply(self, *, response_id: str, text: str, phase: str,
                     nonce: str = "") -> None:
        self.last_text = text

    def record_action(self, *, response_id: str, action: object) -> None:
        return None

    def record_result(self, *, response_id: str, result: object) -> None:
        self.turns.append(T.Turn(model_text=self.last_text,
                                 result=dict(result)))

    def stop_or_block(self, *, code: str, detail: str = "") -> None:
        self.turns.append(T.Turn(model_text="", result={"code": code}))


class _Dispatcher:
    def __init__(self, result: "dict") -> None:
        self.result = result

    def execute_once(self, action: object, store: object) -> "dict":
        return dict(self.result)


class _Sender:
    OFFLINE = True

    def __init__(self, replies: "list[str]") -> None:
        self.replies = list(replies)
        self.requests: "list[T.Request]" = []

    def send_once(self, request: T.Request, reservation: object) -> object:
        self.requests.append(request)
        text = self.replies.pop(0)
        return L.Reply(text=text, response_id=f"r{len(self.requests)}",
                       is_actionable=True)


class _Admission:
    def check_all(self, **kwargs: object) -> object:
        from types import SimpleNamespace
        return SimpleNamespace(envelope_id="g2-test")


class _Budget:
    def reserve_atomic(self, *, call_id: str, permit: object) -> object:
        from types import SimpleNamespace
        return SimpleNamespace(call_id=call_id)

    def resolve_or_pause(self, reply: object) -> None:
        return None


def _loop(tmp_path: Path, *, envelope: "dict", store: _Store,
          sender: _Sender, captures: "list[dict] | None" = None) -> L.ResearchLoop:
    dispatcher = _Dispatcher({"ok": True, "action": "run_confirm",
                              T.G2_ENVELOPE_KEY: dict(envelope),
                              "n_runs": 160})

    def messages() -> T.Request:
        context = envelope if captures is None else None
        return T.build_request(
            system="g2 real-turn", task_brief="边界捕获；无模型",
            turns=store.history(), counter=lambda s: max(1, len(s) // 4),
            delivery_policy=T.POLICY_G2_ATOMIC, result_context=context)

    return L.ResearchLoop(sender=sender, parser=L.parse_action,
                          dispatcher=dispatcher, store=store,
                          messages=messages, counter=lambda s: 1,
                          admission=_Admission(), budget=_Budget(),
                          envelope=None, root_lock=None, spend_contract=None,
                          phase="confirm")


def test_real_turn_captures_request_and_envelope_at_boundary(tmp_path):
    envelope = _envelope()
    store = _Store()
    sender = _Sender([json.dumps({"action": "run_confirm",
                                  "confirm_token": "tok"})])
    recorder = G2.G2CaptureRecorder(tmp_path, diagnostic_id="vis",
                                    scope_contract_sha=envelope[
                                        "scope_contract_sha256"])
    loop = L.ResearchLoop(sender=sender, parser=L.parse_action,
                          dispatcher=_Dispatcher(
                              {"ok": True, "action": "run_confirm",
                               "result_envelope": dict(envelope)}),
                          store=store,
                          messages=lambda: T.build_request(
                              system="g2 real-turn", task_brief="边界捕获；无模型",
                              turns=store.history(),
                              counter=lambda s: max(1, len(s) // 4),
                              delivery_policy=T.POLICY_G2_ATOMIC),
                          counter=lambda s: 1, admission=_Admission(),
                          budget=_Budget(), envelope=None, root_lock=None,
                          spend_contract=None, phase="confirm",
                          capture=recorder)
    outcome = loop.turn()
    assert outcome.ok
    assert sender.requests, "真实 turn 必须构造并记录请求"
    recorder.record_request(sender.requests[0], phase="confirm")
    recorder.record_gateway_missing()
    manifest = recorder.write()
    for kind in ("request_serialized", "gateway_capture", "response_raw",
                 "action_parsed", "dispatch_return", "clipped_view"):
        assert kind in manifest["kinds"], kind
    verify = GT.verify_trace(tmp_path / "trace" / "g2")
    assert verify["ok"], verify


def test_next_request_carries_atomic_envelope_verbatim():
    envelope = _envelope()
    store = _Store()
    sender = _Sender([json.dumps({"action": "run_confirm",
                                  "confirm_token": "tok"})])
    loop = _loop(Path("/tmp"), envelope=envelope, store=store, sender=sender)
    assert loop.turn().ok
    request = T.build_request(
        system="g2", task_brief="b", turns=store.history(),
        counter=lambda s: 1, delivery_policy=T.POLICY_G2_ATOMIC,
        result_context=envelope)
    text = G2.canonical(envelope).decode()
    joined = "\n".join(m["content"] for m in request.messages)
    assert text in joined
    assert joined.count(text) == 1          # 不重复叠加
    assert G2.first_divergence(envelope, joined) == "NONE"


def test_legacy_result_loses_primary_before_request():
    legacy = json.loads(json.dumps(CLI._legacy_confirm_result(160)))
    turns = [T.Turn(model_text='{"action":"run_confirm"}', result=legacy)]
    request = T.build_request(system="legacy", task_brief="b", turns=turns,
                              counter=lambda s: 1,
                              delivery_policy=T.POLICY_LEGACY)
    marker = "0.040867675677873194"
    assert marker not in request.serialized
    assert G2.first_divergence(None, request.serialized) == "CANDIDATE_OUTPUT"


def test_history_shift_requires_result_context():
    envelope = _envelope()
    turns = [T.Turn(model_text="{}", result={"ok": True, "action": "analyze"})]
    turns[0] = T.Turn(model_text="{}",
                      result={"ok": True, "action": "run_confirm",
                              T.G2_ENVELOPE_KEY: dict(envelope)})
    for i in range(4):
        turns.append(T.Turn(model_text="{}",
                            result={"ok": True, "action": "analyze",
                                    "analysis_id": f"a{i}"}))
    with pytest.raises(T.ResultViewIncomplete):
        T.build_request(system="g2", task_brief="b", turns=turns,
                        counter=lambda s: 1,
                        delivery_policy=T.POLICY_G2_ATOMIC)
    request = T.build_request(system="g2", task_brief="b", turns=turns,
                              counter=lambda s: 1,
                              delivery_policy=T.POLICY_G2_ATOMIC,
                              result_context=envelope)
    joined = "\n".join(m["content"] for m in request.messages)
    assert G2.canonical(envelope).decode() in joined


def test_cross_study_envelope_does_not_leak():
    a, b = _envelope("a"), _envelope("b")
    turns = [T.Turn(model_text="{}",
                    result={"ok": True, "action": "run_confirm",
                            T.G2_ENVELOPE_KEY: dict(a)})]
    for i in range(4):
        turns.append(T.Turn(model_text="{}",
                            result={"ok": True, "action": "analyze",
                                    "analysis_id": f"c{i}"}))
    request = T.build_request(system="g2", task_brief="b", turns=turns,
                              counter=lambda s: 1,
                              delivery_policy=T.POLICY_G2_ATOMIC,
                              result_context=b)
    joined = "\n".join(m["content"] for m in request.messages)
    assert G2.canonical(b).decode() in joined
    assert G2.canonical(a).decode() not in joined


def test_read_paths_multipage_and_submit_without_confirm_rerun():
    report = CLI._run_read_paths()
    assert report["ok"], report
    assert len(report["pages"]) >= 2
    assert report["reassembled_ok"]
    assert report["context_occurrences"] == 1
    assert report["confirm_rerun"] is False


def test_offline_guard_blocks_sockets():
    import socket
    with CLI._OfflineGuard():
        with pytest.raises(CLI.G2CliError) as exc:
            socket.socket().connect(("127.0.0.1", 9))
        assert exc.value.code == "PAID_GATE"


def test_scripted_sender_requires_offline_marker():
    G2.assert_offline_sender(G2.RecordingScriptedSender(["{}"]))
    with pytest.raises(G2.G2Error) as exc:
        G2.assert_offline_sender(object())
    assert exc.value.code == "PAID_GATE"
