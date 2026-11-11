"""RSI-R1 T2：动作解析、渲染 nonce、LOCK 化 sender 与循环往返（AC10–AC14）。

全部离线：假 sender/transporter 只在内存里计数，不触网、不调模型。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import rubric as R
from tests.rsi_r1_helpers import plan_doc

DESIGN_SRC = "def build(spec):\n    return spec\n"
ANALYSIS_SRC = "def analyse(obs):\n    return {'x': 0.0}\n"
HEX = "a" * 64


# ---------------------------------------------------------------------------
# AC10：七动作解析
# ---------------------------------------------------------------------------

def _all_actions() -> "dict[str, dict[str, Any]]":
    claim = {"hypothesis_id": "h1", "proposition": "decrease_gt",
             "metric_id": "displacement_x_m",
             "delta": float(R.metric_delta("displacement_x_m")),
             "estimate": 0.02, "lower": 0.01, "upper": 0.03}
    return {
        "write_artifact": {"action": "write_artifact", "kind": "design",
                           "source": DESIGN_SRC},
        "propose_plan": {"action": "propose_plan", "plan": plan_doc("A", phase="explore")},
        "run_batch": {"action": "run_batch", "plan_sha256": HEX,
                      "design_sha256": HEX, "phase": "explore"},
        "analyze": {"action": "analyze", "source_sha256": HEX,
                    "run_ids": ["run-1", "run-2"]},
        "freeze_confirm": {"action": "freeze_confirm", "plan_sha256": HEX,
                           "design_sha256": HEX, "analysis_sha256": HEX,
                           "plan": plan_doc("A")},
        "run_confirm": {"action": "run_confirm", "confirm_token": "t" * 32},
        "submit_claim": {"action": "submit_claim", "claims": [claim],
                         "report_text": "报告"},
    }


def test_parse_accepts_every_action() -> None:
    docs = _all_actions()
    assert set(docs) == set(L.ACTION_NAMES)
    for name, doc in docs.items():
        action = L.parse_action(json.dumps(doc, ensure_ascii=False))
        assert action.name == name
        assert action.to_doc() == doc


def test_freeze_confirm_accepts_host_plan_sha_sentinel_only() -> None:
    """锁内适配（T6）：确认计划的 sha 由宿主计算，候选只能写字面 sentinel。"""
    doc = dict(_all_actions()["freeze_confirm"])
    doc["plan_sha256"] = L.HOST_PLAN_SHA
    action = L.parse_action(json.dumps(doc, ensure_ascii=False))
    assert action.doc["plan_sha256"] == "host"
    # 大小写/近似值一律不放宽：仍按 64 hex 拒绝
    for bad in ("HOST", "host ", "", "h" * 64):
        broken = dict(doc)
        broken["plan_sha256"] = bad
        with pytest.raises(L.LoopError) as exc:
            L.parse_action(json.dumps(broken, ensure_ascii=False))
        assert exc.value.code == "SOURCE_SHA_INVALID"


def test_parse_rejects_non_single_json() -> None:
    for text in ("", "   ", "not json", "[1,2]", '"str"',
                 json.dumps(_all_actions()["run_confirm"]) + "\n"
                 + json.dumps(_all_actions()["run_confirm"])):
        with pytest.raises(L.LoopError) as exc:
            L.parse_action(text)
        assert exc.value.code in ("NOT_SINGLE_ACTION", "MALFORMED_JSON")
        assert not exc.value.terminal, "格式错误必须可自修，不是终止"


def test_parse_rejects_unknown_and_extra_keys() -> None:
    doc = _all_actions()["run_confirm"]
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**doc, "action": "rm_rf"}))
    assert exc.value.code == "UNKNOWN_ACTION"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**doc, "extra": 1}))
    assert exc.value.code == "UNKNOWN_KEY"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({"action": "run_batch", "plan_sha256": HEX}))
    assert exc.value.code == "MISSING_KEY"

    # 大小写/空白的 hex 不得放行
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**_all_actions()["run_batch"],
                                   "design_sha256": "A" * 64}))
    assert exc.value.code == "SOURCE_SHA_INVALID"


def test_freeze_confirm_accepts_host_parent_sha_sentinel() -> None:
    """L-R1-07：confirm 计划的 ``parent_sha`` 同为不可计算项，接受字面 sentinel。

    round 1 的 probe 死在第二步：brief 一边说「模型无法自己算 sha256」，一边要求
    ``parent_sha`` 是 64 hex。修复后它与 ``plan_sha256`` 一样可写 ``"host"``——
    解析层只做结构校验，值校验（是否等于当前探索计划）由宿主替换后完成。
    """
    doc = dict(_all_actions()["freeze_confirm"])
    doc["plan"] = {**doc["plan"], "parent_sha": L.HOST_PLAN_SHA}
    action = L.parse_action(json.dumps(doc, ensure_ascii=False))
    assert action.doc["plan"]["parent_sha"] == "host"
    # 近似值一律不放宽：仍按 64 hex 拒
    for bad in ("HOST", "host ", "h" * 63):
        broken = {**doc, "plan": {**doc["plan"], "parent_sha": bad}}
        with pytest.raises(L.LoopError) as exc:
            L.parse_action(json.dumps(broken, ensure_ascii=False))
        assert exc.value.code == "SOURCE_SHA_INVALID"
    # 合法 64 hex 在解析层放行（值对不对由宿主判 PARENT_SHA_MISMATCH）
    ok = {**doc, "plan": {**doc["plan"], "parent_sha": "b" * 64}}
    assert L.parse_action(json.dumps(ok, ensure_ascii=False)).name == "freeze_confirm"


def test_parse_rejects_confirm_phase_in_run_batch() -> None:
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**_all_actions()["run_batch"],
                                   "phase": "confirm"}))
    assert exc.value.code == "PLAN_PHASE_INVALID"
    assert "run_confirm" in exc.value.detail


def test_parse_rejects_bad_claims() -> None:
    base = _all_actions()["submit_claim"]
    good = dict(base["claims"][0])

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base, "claims": []}))
    assert exc.value.code == "WRONG_TYPE"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base,
                                   "claims": [{**good, "delta": 0.5}]}))
    assert exc.value.code == "DELTA_DRIFT"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base,
                                   "claims": [{**good, "lower": 0.9}]}))
    assert exc.value.code == "CI_INVALID"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base,
                                   "claims": [{**good, "proposition": "nope"}]}))
    assert exc.value.code == "PROPOSITION_INVALID"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base,
                                   "claims": [{**good, "metric_id": "x"}]}))
    assert exc.value.code == "METRIC_UNREGISTERED"

    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base, "report_text": "x" * (24 * 1024 + 1)}))
    assert exc.value.code == "FIELD_TOO_LARGE"


def test_report_text_is_required_and_bounded() -> None:
    base = _all_actions()["submit_claim"]
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base, "report_text": "   "}))
    assert exc.value.code == "REPORT_MISSING"
    ok = L.parse_action(json.dumps({**base, "report_text": "x" * (24 * 1024)}))
    assert ok.name == "submit_claim"


# ---------------------------------------------------------------------------
# AC11：可见状态 nonce（真实动态反馈进入下一次请求）
# ---------------------------------------------------------------------------

def _visible(**kw) -> L.VisibleState:
    base = dict(study="A", study_state="EXPLORING",
                artifacts={"design": [], "analysis": []}, last_batch=None,
                budget_slots_remaining=40, confirm_token=None)
    base.update(kw)
    return L.make_visible_state(**base)


def test_nonce_is_deterministic_and_feedback_sensitive() -> None:
    a, b = _visible(), _visible()
    assert a.nonce == b.nonce and len(a.nonce) == 16

    c = _visible(last_batch={"run_ids": ["r1"], "ok": True, "displacement_x_m": 0.123})
    assert c.nonce != a.nonce
    d = _visible(budget_slots_remaining=39)
    assert d.nonce != a.nonce
    e = _visible(artifacts={"design": [HEX], "analysis": []})
    assert e.nonce != a.nonce
    # 键序不影响 nonce
    assert _visible(artifacts={"analysis": [], "design": []}).nonce == a.nonce


def test_rendered_brief_carries_only_public_state_and_nonce() -> None:
    state = _visible(last_batch={"run_ids": ["r1"], "ok": True})
    brief = L.render_task_brief(state)
    assert state.nonce in brief
    assert '"nonce"' in brief
    # 隐藏面（私有 block / seed / 确认集）不得出现在渲染里
    body = json.loads(brief.split("\n", 1)[1].rsplit("\n\n请给出", 1)[0])
    assert set(body) == {"study", "state", "artifacts", "last_batch",
                         "budget_slots_remaining", "confirm_token", "nonce"}
    assert "seed" not in brief.lower() and "private" not in brief.lower()


def test_system_text_states_limits_and_boundaries() -> None:
    text = L.render_system(extra_rules="study A 提示")
    assert str(T.LOCAL_TOKEN_CAP) in text and str(T.SERIALIZED_BYTE_CAP) in text
    assert str(L.REPORT_MAX_BYTES) in text
    for name in L.ACTION_NAMES:
        assert name in text
    assert "越权" in text and "study A 提示" in text


# ---------------------------------------------------------------------------
# LOCK 化 sender（AC13：绕开 CLI 直接装配也不得裸发网）
# ---------------------------------------------------------------------------

def _lock_doc(**kw) -> "dict[str, Any]":
    doc = {"schema": L.LOCK_SCHEMA, "envelope_id": "env1",
           "budget_sha256": "b" * 64, "stage": "coding_debug", "phase": "explore",
           "dependencies": {"quota/caps.json": "c" * 64}}
    doc.update(kw)
    return doc


class _Admission:
    def __init__(self, *, envelope: str = "env1", budget: str = "b" * 64):
        self.envelope = envelope
        self.budget = budget
        self.calls = 0

    def check_all(self, *, envelope, root_lock, phase, spend_contract) -> L.Permit:
        self.calls += 1
        return L.Permit(envelope_id=self.envelope, lock_sha256=root_lock.sha256,
                        budget_sha256=self.budget, phase=phase)


class _Budget:
    def __init__(self):
        self.reserved: "list[L.Reservation]" = []
        self.resolved: "list[L.Reply]" = []

    def reserve_atomic(self, *, call_id, permit) -> L.Reservation:
        res = L.Reservation(call_id=call_id, permit=permit, slot_id="slot-1")
        self.reserved.append(res)
        return res

    def resolve_or_pause(self, reply) -> None:
        self.resolved.append(reply)


def _request(store=None) -> T.Request:
    turns = [] if store is None else store.history()
    return T.build_request(system=L.render_system(),
                           task_brief=L.render_task_brief(_visible()),
                           turns=turns, counter=len)


def test_load_lock_fails_closed_when_missing() -> None:
    def missing():
        raise FileNotFoundError("no such file")

    with pytest.raises(L.LoopError) as exc:
        L.load_lock(missing)
    assert exc.value.code == "LOCK_MISSING" and exc.value.terminal
    with pytest.raises(L.LoopError):
        L.load_lock(lambda: {"schema": "wrong"})


def test_build_production_sender_never_constructs_transport_without_lock() -> None:
    built: "list[int]" = []

    def factory():
        built.append(1)
        return lambda request: L.Reply(text="x", response_id="r",
                                       attempts_http=1)

    def missing():
        raise FileNotFoundError("absent")

    with pytest.raises(L.LoopError) as exc:
        L.build_production_sender(lock_reader=missing, transport_factory=factory,
                                  admission=_Admission(), budget=_Budget(),
                                  envelope=object(), spend_contract=object())
    assert exc.value.code == "LOCK_MISSING"
    assert built == [], "LOCK 缺失时不得构造 transport（零 POST）"


def test_build_production_sender_refuses_dependency_drift() -> None:
    built: "list[int]" = []

    def factory():
        built.append(1)
        return lambda request: L.Reply(text="x", response_id="r",
                                       attempts_http=1)

    with pytest.raises(L.LoopError) as exc:
        L.build_production_sender(lock_reader=_lock_doc, transport_factory=factory,
                                  admission=_Admission(), budget=_Budget(),
                                  envelope=object(), spend_contract=object(),
                                  sha_of=lambda path: "d" * 64)
    assert exc.value.code == "LOCK_DRIFT" and exc.value.terminal
    assert built == []

    sender = L.build_production_sender(
        lock_reader=_lock_doc, transport_factory=factory, admission=_Admission(),
        budget=_Budget(), envelope=object(), spend_contract=object(),
        sha_of=lambda path: "c" * 64)
    assert isinstance(sender, L.LockedSender)


def test_locked_sender_rechecks_permit_and_calls_transport_once() -> None:
    lock = L.LockDocument.from_doc(_lock_doc())
    sent: "list[T.Request]" = []

    def transport(request):
        sent.append(request)
        return L.Reply(text="ok", response_id="r1", attempts_http=1)

    sender = L.LockedSender(transport, lock=lock, admission=_Admission(),
                            budget=_Budget(), envelope=object(),
                            spend_contract=object())
    request = _request()
    good = L.Permit(envelope_id="env1", lock_sha256=lock.sha256,
                    budget_sha256="b" * 64, phase="explore")

    for bad in (L.Permit(envelope_id="env2", lock_sha256=lock.sha256,
                         budget_sha256="b" * 64, phase="explore"),
                L.Permit(envelope_id="env1", lock_sha256="0" * 64,
                         budget_sha256="b" * 64, phase="explore"),
                L.Permit(envelope_id="env1", lock_sha256=lock.sha256,
                         budget_sha256="9" * 64, phase="explore")):
        with pytest.raises(L.LoopError) as exc:
            sender.send_once(request, L.Reservation(call_id="x", permit=bad,
                                                    slot_id="s"))
        assert exc.value.code == "LOCK_MISMATCH" and exc.value.terminal
    assert sent == [], "准入不一致时一次都不得发"

    good_call = f"env1:{request.sha256[:16]}"
    with pytest.raises(L.LoopError) as exc:
        sender.send_once(request, L.Reservation(call_id="wrong", permit=good,
                                                slot_id="s"))
    assert exc.value.code == "LOCK_MISMATCH"
    assert sent == []

    reply = sender.send_once(request, L.Reservation(call_id=good_call,
                                                    permit=good, slot_id="s"))
    assert reply.text == "ok" and len(sent) == 1


def test_reply_with_hidden_retry_is_rejected() -> None:
    with pytest.raises(L.LoopError) as exc:
        L.Reply(text="x", response_id="r", attempts_http=2)
    assert exc.value.code == "HIDDEN_RETRY"


# ---------------------------------------------------------------------------
# 循环（AC14 的「完整流程 + 生产异常路径」半边）
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self) -> None:
        self.replies: "list[dict[str, Any]]" = []
        self.actions: "list[dict[str, Any]]" = []
        self.results: "list[dict[str, Any]]" = []
        self.stops: "list[dict[str, Any]]" = []
        self._text: "dict[str, str]" = {}

    def history(self) -> "list[T.Turn]":
        return [T.Turn(model_text=self._text[rid], result=res)
                for rid, res in self.results]

    def record_reply(self, *, response_id, text, phase, nonce):
        self._text[response_id] = text
        self.replies.append({"response_id": response_id, "text": text,
                             "phase": phase, "nonce": nonce})
        return {}

    def record_action(self, *, response_id, action):
        self.actions.append({"response_id": response_id, "action": dict(action)})
        return {}

    def record_result(self, *, response_id, result):
        self.results.append((response_id, dict(result)))
        return {}

    def stop_or_block(self, *, code, detail):
        self.stops.append({"code": code, "detail": detail})
        return {}


class _Dispatcher:
    def __init__(self, result: "Mapping[str, Any] | None" = None):
        self.result = dict(result or {"ok": True, "action": "?", "run_id": "run-1"})
        self.seen: "list[L.Action]" = []

    def execute_once(self, action, store):
        self.seen.append(action)
        return {**self.result, "action": action.name}


def _loop(*, reply_text: str, store=None, dispatcher=None, brief=None,
          transport=None):
    lock = L.LockDocument.from_doc(_lock_doc())
    admission = _Admission()
    budget = _Budget()
    calls: "list[T.Request]" = []

    def default_transport(request):
        calls.append(request)
        return L.Reply(text=reply_text, response_id="resp-1", attempts_http=1)

    sender = L.LockedSender(transport or default_transport, lock=lock,
                            admission=admission, budget=budget,
                            envelope=object(), spend_contract=object())
    store = store or _Store()
    dispatcher = dispatcher or _Dispatcher()

    def messages() -> T.Request:
        return T.build_request(
            system=L.render_system(),
            task_brief=brief or L.render_task_brief(_visible()),
            turns=store.history(), counter=len)

    loop = L.ResearchLoop(sender=sender, parser=L.parse_action,
                          dispatcher=dispatcher, store=store,
                          messages=messages, counter=len, admission=admission,
                          budget=budget, envelope=object(), root_lock=lock,
                          spend_contract=object(), phase="explore")
    return loop, store, dispatcher, budget, calls


def test_loop_happy_path_runs_one_paid_round() -> None:
    text = json.dumps(_all_actions()["run_batch"])
    loop, store, dispatcher, budget, calls = _loop(reply_text=text)
    out = loop.turn()

    assert out.ok and out.action == "run_batch" and not out.terminal
    assert len(calls) == 1, "一次 turn 恰好一次逻辑调用"
    assert len(budget.reserved) == 1 and len(budget.resolved) == 1
    assert store.replies[0]["response_id"] == "resp-1"
    assert store.replies[0]["nonce"] == _visible().nonce
    assert store.actions[0]["action"]["action"] == "run_batch"
    assert store.results[0][1]["run_id"] == "run-1"
    assert dispatcher.seen[0].name == "run_batch"
    assert store.stops == []


def test_loop_parse_failure_is_feedback_and_next_turn_sees_it() -> None:
    loop, store, dispatcher, budget, calls = _loop(reply_text="not json at all")
    out = loop.turn()
    assert not out.ok and out.code == "MALFORMED_JSON" and not out.terminal
    assert dispatcher.seen == [] and store.stops == []
    assert store.results[0][1]["code"] == "MALFORMED_JSON"
    assert len(budget.resolved) == 1

    loop.turn()
    # 第二次请求必须包含第一次的失败回包（真实反馈进入下一 request）
    assert "MALFORMED_JSON" in calls[1].messages[-1]["content"]
    assert calls[0].sha256 != calls[1].sha256


def test_loop_input_over_cap_stops_before_any_post() -> None:
    loop, store, dispatcher, budget, calls = _loop(
        reply_text=json.dumps(_all_actions()["run_confirm"]),
        brief="x" * (T.SERIALIZED_BYTE_CAP + 10))
    out = loop.turn()
    assert out.code == "INPUT_LIMIT" and out.terminal
    assert calls == [] and budget.reserved == [] and dispatcher.seen == []
    assert store.stops[0]["code"] == "INPUT_LIMIT"
    assert "BYTE_OVER_CAP" in store.stops[0]["detail"]


def test_loop_sender_terminal_error_stops_without_dispatch() -> None:
    def broken(request):
        raise L.LoopError("COST_UNKNOWN", "供应商超时且费用不明", terminal=True)

    loop, store, dispatcher, budget, calls = _loop(
        reply_text=json.dumps(_all_actions()["run_confirm"]),
        transport=broken)
    out = loop.turn()
    assert out.code == "COST_UNKNOWN" and out.terminal
    assert dispatcher.seen == []
    assert store.stops[0]["code"] == "COST_UNKNOWN"
    assert len(budget.resolved) == 1


def test_loop_dispatcher_failure_is_recorded_not_retried() -> None:
    text = json.dumps(_all_actions()["analyze"])
    loop, store, dispatcher, budget, calls = _loop(
        reply_text=text,
        dispatcher=_Dispatcher({"ok": False, "code": "RUN_ID_UNKNOWN"}))
    out = loop.turn()
    assert not out.ok and out.code == "RUN_ID_UNKNOWN" and not out.terminal
    assert len(calls) == 1, "不得隐式重发同一动作"
    assert store.results[0][1]["code"] == "RUN_ID_UNKNOWN"


def test_loop_terminal_parse_error_stops() -> None:
    # 越权不是 can 自修：这里用带 terminating 语义的解析器模拟宿主判定的越权文本
    def parser(text: str) -> L.Action:
        raise L.LoopError("AUTHORITY", "试图读取确认集私有 block", terminal=True)

    loop, store, dispatcher, budget, calls = _loop(
        reply_text=json.dumps(_all_actions()["run_confirm"]))
    loop.parser = parser
    out = loop.turn()
    assert out.terminal and out.code == "AUTHORITY"
    assert dispatcher.seen == []
    assert store.stops[0]["code"] == "AUTHORITY"


def test_event_store_folds_journal_into_turns(tmp_path) -> None:
    from orchestrator.rsi_r1 import state as ST
    from tests.rsi_r1_helpers import make_ctx, make_dirs

    make_dirs(tmp_path)
    st = ST.ResearchState(make_ctx(tmp_path))
    store = L.EventStore(st)
    store.record_reply(response_id="r1", text='{"action":"run_batch"}',
                       phase="explore", nonce="abc")
    store.record_result(response_id="r1",
                        result={"ok": True, "action": "run_batch", "run_id": "x"})
    turns = store.history()
    assert len(turns) == 1
    assert turns[0].model_text == '{"action":"run_batch"}'
    assert turns[0].result["run_id"] == "x"
    store.stop_or_block(code="INPUT_LIMIT", detail="over")
    assert any(ev.event == "loop_stop" for ev in st._events())
