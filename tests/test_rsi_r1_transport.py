"""RSI-R1 传输层提取回归（AC11/AC12 的结构半边）。

`orchestrator/common/research_transport.py` 是从 `rsi_r02/messages.py`
**原样提取**的纯请求层；本文件用重构前实现逐字节冻结的 golden
（`tests/fixtures/rsi_r1/research_transport_golden.json`）证明提取无行为漂移：

* golden 覆盖空历史、两轮全文、四轮摘要、三类截断（模型正文/保结构结果/错误回包/
  紧凑兜底）、unicode、自定义 max_tokens；
* 任何字段顺序、截断标记、摘要格式的变化都会打破字节相等；
* `rsi_r02.messages` 必须确实**重导出**同一实现（不是第二份拷贝）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r02 import messages as M

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "rsi_r1" / "research_transport_golden.json"


def _load_golden() -> "list[dict]":
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert doc["schema"] == "simpraxis.rsi_r1.transport_golden.v1"
    assert doc["cases"], "golden 不得为空"
    return doc["cases"]


GOLDEN_CASES = _load_golden()


def _build(case: "dict") -> T.Request:
    inp = case["input"]
    turns = [T.Turn(model_text=t["model_text"], result=t["result"])
             for t in inp["turns"]]
    return T.build_request(system=inp["system"], task_brief=inp["task_brief"],
                           turns=turns, counter=len,
                           max_tokens=inp["max_tokens"])


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
def test_transport_matches_frozen_golden_bytes(case: dict) -> None:
    req = _build(case)
    want = case["request"]
    assert req.serialized == want["serialized"]
    assert req.sha256 == want["sha256"]
    assert req.n_bytes == want["n_bytes"]
    assert req.local_tokens == want["local_tokens"]
    assert req.max_tokens == want["max_tokens"]
    assert [dict(m) for m in req.messages] == want["messages"]


def test_golden_covers_required_shapes() -> None:
    names = {c["name"] for c in GOLDEN_CASES}
    assert {"empty_history", "two_turns", "four_turns_summarized",
            "clip_model_text", "clip_result_structural", "clip_error_result",
            "compact_fallback", "unicode", "custom_max_tokens"} <= names


def test_rsi_r02_messages_reexports_same_implementation() -> None:
    # 单一实现：旧模块必须重导出，而不是自己再定义一份。
    assert M.build_request is T.build_request
    assert M.Turn is T.Turn
    assert M.Request is T.Request
    assert M.check_request is T.check_request
    assert M.summarize_event is T.summarize_event
    assert M.require_counter is T.require_counter
    assert M.LOCAL_TOKEN_CAP == T.LOCAL_TOKEN_CAP == 20000
    assert M.SERIALIZED_BYTE_CAP == T.SERIALIZED_BYTE_CAP == 128 * 1024
    assert M.MAX_OUTPUT_TOKENS == T.MAX_OUTPUT_TOKENS == 8192
    assert M.TOOL_REPLY_MAX_BYTES == T.TOOL_REPLY_MAX_BYTES == 4096


# ---------------------------------------------------------------------------
# AC12：输入完整计数 / 压缩 / 字节帽（20000-20001 与 128KiB 边界）
# ---------------------------------------------------------------------------

def _req_with(**kw) -> T.Request:
    base = dict(system="S", task_brief="T", turns=(), counter=len)
    base.update(kw)
    return T.build_request(**base)


def test_token_cap_boundary_is_inclusive() -> None:
    n = _req_with().local_tokens
    assert T.check_request(_req_with(), token_cap=n).verdict == T.VERDICT_OK
    acc = T.check_request(_req_with(), token_cap=n - 1)
    assert acc.verdict == T.VERDICT_TOKEN_OVER_CAP
    assert not acc.ok


def test_byte_cap_checked_before_token_cap() -> None:
    n_bytes = _req_with().n_bytes
    acc = T.check_request(_req_with(), token_cap=1, byte_cap=n_bytes - 1)
    assert acc.verdict == T.VERDICT_BYTE_OVER_CAP
    assert T.check_request(_req_with(), token_cap=1, byte_cap=n_bytes).verdict \
        == T.VERDICT_TOKEN_OVER_CAP
    # 字节恰好等于帽时放行
    assert T.check_request(_req_with(), token_cap=10**9,
                           byte_cap=n_bytes).verdict == T.VERDICT_OK


def test_counter_unavailable_fails_closed() -> None:
    with pytest.raises(T.CounterUnavailable):
        T.require_counter(None)
    # str 有 .count 但它不是计数器
    with pytest.raises(T.CounterUnavailable):
        T.require_counter("not-a-counter")
    assert not T.count_available(None)

    class Duck:
        def count(self, text: str) -> int:
            return 7

    fn = T.require_counter(Duck())
    assert fn("xx") == 7
    assert T.require_counter(len)("abc") == 3
    assert T.count_available(Duck())


def test_accounting_flags_local_counter_not_supplier_tokens() -> None:
    acc = T.check_request(_req_with())
    assert acc.ok
    assert "不是供应端 input_tokens" in acc.as_dict()["note"]


# ---------------------------------------------------------------------------
# AC11：动态反馈进入 nonce——状态一变 nonce 必变，同状态稳定
# ---------------------------------------------------------------------------

def test_stable_nonce_is_deterministic_and_feedback_sensitive() -> None:
    a = T.stable_nonce("study_A", {"last_run": "r1", "n": 1})
    b = T.stable_nonce("study_A", {"last_run": "r1", "n": 1})
    assert a == b and len(a) == 16

    c = T.stable_nonce("study_A", {"last_run": "r2", "n": 2})
    assert c != a
    # 键序不影响值（canonical）
    assert T.stable_nonce({"x": 1, "y": 2}) == T.stable_nonce({"y": 2, "x": 1})
    # 段边界不会被拼接吞掉
    assert T.stable_nonce("ab", "c") != T.stable_nonce("a", "bc")


# ---------------------------------------------------------------------------
# 2026-11-11 契约面审计：超帽回包必须保住候选作决定所需的字段
# ---------------------------------------------------------------------------

def _big_confirm_result(n_refs: int = 8) -> "dict":
    refs = [{"hypothesis_id": f"h{i}", "contrast_id": f"c{i}",
             "proposition": "位" * 400, "metric_id": "displacement_x_m",
             "delta": -0.01, "estimate": 0.02, "lower": 0.01, "upper": 0.03,
             "status": "OK", "n_blocks": 4} for i in range(n_refs)]
    return {"ok": True, "action": "run_confirm", "confirm_token": "t" * 24,
            "run_ids_head": [f"run-{i:03d}" for i in range(8)],
            "n_runs": 12, "result_sha256": "f" * 64,
            "references": refs, "message": "确认批完成 " + "m" * 3000}


def test_clip_keeps_references_and_run_summary() -> None:
    """保结构截断：references 是模型唯一可用的确认批数值来源，绝不能丢。"""
    turn = T.Turn(model_text="{}", result=_big_confirm_result()).clipped()
    body = turn.result
    assert body["ok"] is True and body.get("truncated") is True
    assert isinstance(body.get("references"), list) and body["references"]
    assert body["n_runs"] == 12 and len(body["run_ids_head"]) == 8
    # 每条参考都保住结构键（字符串可被截短，但不是整包丢弃）
    first = body["references"][0]
    assert {"hypothesis_id", "metric_id", "delta", "estimate", "lower",
            "upper"} <= set(first)
    assert len(json.dumps(body, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")) <= T.TOOL_REPLY_MAX_BYTES


def test_compact_fallback_keeps_analysis_and_references() -> None:
    """结构截断仍超帽时退到白名单，白名单必须包含 analyze/confirm 的关键键。"""
    result = _big_confirm_result()
    result["analysis"] = {"n_runs": 12, "summary": "s" * 200}
    result["analysis_relpath"] = "analysis/A-n1.json"
    for i in range(40):  # 大量无关键把总字节推过帽，逼出 compact 分支
        result[f"noise_{i:02d}"] = "n" * 200
    body = T.Turn(model_text="{}", result=result).clipped().result
    assert body.get("truncated") is True
    assert body.get("code") == "RESULT_TRUNCATED"
    assert "references" in body and "analysis" in body
    assert body.get("n_runs") == 12
    assert len(json.dumps(body, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")) <= T.TOOL_REPLY_MAX_BYTES
