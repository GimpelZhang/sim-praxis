"""RSI-R0.2 输入门测试（AC12）：确定性请求、两轮全文+更早摘要、20000/128KiB 边界。

计数器全部用桩（不读真实分词器、不触网）；真正例另测 ``LocalCounter`` 的
缺件/哈希不符两条 fail-closed 路径（哈希检查先于解析，故不需要真分词器文件）。
"""
from __future__ import annotations

import json

import pytest

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r02 import messages as M


def _surface(**kw) -> AC.ActionSurface:
    base = dict(kind="pendulum_run", param_bounds={"force": (-2.0, 2.0)},
                fixed_params={"theta0": 0.1}, max_steps=600)
    base.update(kw)
    return AC.ActionSurface(**base)


def _fixed_counter(n: int):
    return lambda text: n


def _req(*, turns=(), brief="任务", system="SYS", counter=None, max_tokens=M.MAX_OUTPUT_TOKENS):
    return M.build_request(system=system, task_brief=brief, turns=turns,
                           counter=counter or _fixed_counter(10), max_tokens=max_tokens)


def _turn(text='{"action":"run_experiment"}', **result):
    base = {"ok": True, "action": "run_experiment"}
    base.update(result)
    return M.Turn(model_text=text, result=base)


# ---- 确定性 ----


def test_request_is_deterministic():
    turns = [_turn(run_id="r1"), _turn(run_id="r2")]
    a = _req(turns=turns)
    b = _req(turns=list(turns))
    assert a.serialized == b.serialized
    assert a.sha256 == b.sha256
    assert a.n_bytes == len(a.serialized.encode("utf-8"))
    assert a.local_tokens == 10
    assert json.loads(a.serialized)["messages"] == list(a.messages)


def test_two_round_full_history_and_earlier_summarized():
    turns = [_turn(f'{{"action":"run_experiment","n":{i}}}', run_id=f"r{i}")
             for i in range(4)]
    req = _req(turns=turns)
    msgs = list(req.messages)
    assert msgs[0] == {"role": "user", "content": "任务"}
    assert "更早轮次（固定字段摘要）" in msgs[1]["content"]
    older = msgs[1]["content"]
    # 更早两轮只留固定字段：run_id 在场，模型原文（带 n=0/n=1）不在场
    assert "run_id=r0" in older and "run_id=r1" in older
    assert '"n"' not in older
    # 最近两轮保留全文
    tail = msgs[2:]
    assert [m["role"] for m in tail] == ["assistant", "user", "assistant", "user"]
    assert tail[0]["content"] == '{"action":"run_experiment","n":2}'
    assert json.loads(tail[1]["content"])["run_id"] == "r2"
    assert tail[2]["content"] == '{"action":"run_experiment","n":3}'
    assert len(msgs) == 6


def test_summarize_event_ignores_unknown_fields():
    line = M.summarize_event({"action": "analyze", "ok": True,
                              "analysis_id": "a1", "the_answer": "42"})
    assert "action=analyze" in line and "analysis_id=a1" in line
    assert "42" not in line


# ---- 截断上限 ----


def test_tool_reply_clipped_at_4kib():
    big = "x" * (M.TOOL_REPLY_MAX_BYTES + 100)
    t = M.Turn(model_text=big, result={"ok": True, "message": big}).clipped()
    assert t.model_text.endswith("[TRUNCATED_BY_HOST]")
    assert len(t.model_text.encode()) <= M.TOOL_REPLY_MAX_BYTES + 64
    assert t.result.get("truncated") is True


def test_error_reply_clipped_at_2kib():
    big = "y" * (M.ERROR_REPLY_MAX_BYTES + 100)
    t = M.Turn(model_text="bad", result={"ok": False, "code": "X", "message": big}).clipped()
    assert t.result["truncated"] is True
    assert t.result["code"] == "X"
    assert len(json.dumps(t.result, ensure_ascii=False).encode()) <= M.ERROR_REPLY_MAX_BYTES + 128


def test_small_results_pass_through_unchanged():
    r = {"ok": True, "run_id": "r1", "path": "runs/r1.json"}
    assert M.Turn(model_text="ok", result=r).clipped().result == r


# ---- 字节与 token 边界 ----


def test_byte_cap_boundary():
    req = _req(brief="甲" * 5000)
    at = M.check_request(req, token_cap=10 ** 9, byte_cap=req.n_bytes)
    assert at.verdict == M.VERDICT_OK and at.ok
    over = M.check_request(req, token_cap=10 ** 9, byte_cap=req.n_bytes - 1)
    assert over.verdict == M.VERDICT_BYTE_OVER_CAP and not over.ok


def test_token_cap_boundary():
    req = _req(counter=_fixed_counter(20000))
    assert M.check_request(req).verdict == M.VERDICT_OK
    over = _req(counter=_fixed_counter(20001))
    assert M.check_request(over).verdict == M.VERDICT_TOKEN_OVER_CAP


def test_byte_checked_before_token():
    req = _req(brief="乙" * 20000, counter=_fixed_counter(99999))
    assert M.check_request(req, byte_cap=10).verdict == M.VERDICT_BYTE_OVER_CAP


def test_accounting_declares_local_semantics():
    acc = M.check_request(_req()).as_dict()
    assert acc["token_cap"] == M.LOCAL_TOKEN_CAP == 20000
    assert acc["byte_cap"] == M.SERIALIZED_BYTE_CAP == 131072
    assert "不是供应端" in acc["note"]


# ---- 计数器 fail-closed ----


def test_require_counter_rejects_none():
    with pytest.raises(M.CounterUnavailable):
        M.require_counter(None)
    assert M.count_available(None) is False
    assert M.count_available(_fixed_counter(1)) is True
    with pytest.raises(TypeError):
        M.require_counter(123)


def test_local_counter_missing_file(tmp_path):
    with pytest.raises(M.CounterUnavailable):
        M.LocalCounter.load(tmp_path / "nope.json", "0" * 64)


def test_local_counter_sha_mismatch(tmp_path):
    f = tmp_path / "tok.json"
    f.write_text('{"junk": true}', encoding="utf-8")
    with pytest.raises(M.CounterUnavailable):
        M.LocalCounter.load(f, "f" * 64)


# ---- system 文本 ----


def test_action_schema_text_has_four_actions_and_bounds():
    text = M.action_schema_text(_surface())
    for action in ("preregister", "run_experiment", "analyze", "submit"):
        assert f'"action":"{action}"' in text
    assert '"force": [-2.0, 2.0]' in text
    assert '"theta0": 0.1' in text
    assert "步数上限：600" in text
    assert "恰好一个动作" in text


def test_build_system_uses_only_public_sections():
    public_doc = {
        "instance_id": "D3",
        "family": "D",
        "public": {"goal": "修复推杆", "interface": {"kind": "push_control",
                                                     "keys": ["force"]},
                   "research": {"h": "h1", "band": [0.1, 0.2], "window_s": 3.0}},
        "private": {"reference_gains": [9.9], "fault": "SECRET_FAULT"},
    }
    text = M.build_system(public_doc, _surface(kind="push_control"))
    assert "实例：D3（族：D）" in text
    assert "修复推杆" in text
    assert "h1" in text
    assert "SECRET_FAULT" not in text and "9.9" not in text
