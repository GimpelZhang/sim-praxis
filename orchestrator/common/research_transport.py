"""跨阶段共享的**纯请求/传输**：上下文装配、本地输入门与确定性 nonce。

来源与边界（Plan §2.1、§5.2）：

* 本模块从 ``orchestrator/rsi_r02/messages.py`` **原样提取**请求构造与本地输入门，
  供 RSI-R1 复用；R0.2 的旧实现改为从这里导入，避免第二份实现。
  旧协议字节由 ``tests/fixtures/rsi_r1/research_transport_golden.json``
  （重构前实现逐字节冻结）回归，任何行为变化都会打破 golden。
* 纯函数、零网络、零模型、零文件系统：计数由调用方注入的 blocker 完成后回传；
  这里只做**结构**（消息序列、截断、字节/token 账）。
* 本地计数是**上下文控制**，不是供应端 token；越界即阻断（不缩帽、不截断后重发）。

新增（R1 需要、R0.2 没有）：``stable_nonce``——把「真实动态反馈」压成短串，
用于 AC11 的「不同 run nonce/hash」；同一可见状态必得同一 nonce，状态一变即变。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

LOCAL_TOKEN_CAP = 20000
SERIALIZED_BYTE_CAP = 128 * 1024
MAX_OUTPUT_TOKENS = 8192
TOOL_REPLY_MAX_BYTES = 4096
ERROR_REPLY_MAX_BYTES = 2048
FULL_HISTORY_ROUNDS = 2

VERDICT_OK = "OK"
VERDICT_TOKEN_OVER_CAP = "TOKEN_OVER_CAP"
VERDICT_BYTE_OVER_CAP = "BYTE_OVER_CAP"
VERDICT_COUNTER_UNAVAILABLE = "COUNTER_UNAVAILABLE"


class CounterUnavailable(RuntimeError):
    """分词器不可用：禁止用未钉哈希的计数器放行。"""


def require_counter(counter: Any) -> Callable[[str], int]:
    """接受含 ``count`` 的对象（如 ``messages.LocalCounter``）或可调用；None 拒绝。

    ``str``/``bytes`` 也有 ``.count``，但它们不是计数器，必须显式排除。
    """
    if counter is None:
        raise CounterUnavailable("没有钉哈希的本地分词器，禁止构造/计数请求")
    if isinstance(counter, (str, bytes, bytearray)):
        raise CounterUnavailable(f"不支持的计数器类型：{type(counter).__name__}")
    count = getattr(counter, "count", None)
    if callable(count):
        return count
    if callable(counter):
        return counter
    raise TypeError(f"不支持的计数器：{type(counter)!r}")


def count_available(counter: Any) -> bool:
    try:
        require_counter(counter)
    except CounterUnavailable:
        return False
    return True


# ---------------------------------------------------------------------------
# 截断（保结构，不静默丢键）
# ---------------------------------------------------------------------------

def clip_text(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    # 按字节截断且不截断半个字符；标记截断，不静默丢内容
    cut = raw[:limit].decode("utf-8", errors="ignore")
    return cut + "\n[TRUNCATED_BY_HOST]"


@dataclass(frozen=True)
class Turn:
    """一次真实往返：模型原始回复 + 宿主结构化结果。"""

    model_text: str
    result: Mapping[str, Any]

    def clipped(self) -> "Turn":
        return Turn(model_text=clip_text(self.model_text, TOOL_REPLY_MAX_BYTES),
                    result=self._clip_result(self.result))

    @staticmethod
    def _clip_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
        ok = bool(result.get("ok"))
        limit = TOOL_REPLY_MAX_BYTES if ok else ERROR_REPLY_MAX_BYTES
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(raw.encode("utf-8")) <= limit:
            return dict(result)
        # 2026-11-10 R0.2 root G 文本审计 FALSE #31：超帽时原先整包替换成
        # ``{ok, code, message}`` 存根，把 ``analysis_id`` 与 ``stdout_preview``
        # 一并丢掉——而公开文本要求候选逐字引用 analyze 的 analysis_id、并用
        # 它打印的 stat 提交。于是一次「回包太大」就能把本该成立的链判死，
        # 属于仪器耦合，不是候选能力。改为**保结构**截断：先按字段把超长
        # 字符串各自截断（含嵌套），保住全部结构键；仍超帽才退化为存根。
        budget = max(256, limit // 4)

        def clip_tree(node: Any, depth: int = 0) -> Any:
            if depth > 6:
                return node
            if isinstance(node, str):
                return clip_text(node, budget)
            if isinstance(node, Mapping):
                return {k: clip_tree(v, depth + 1) for k, v in node.items()}
            if isinstance(node, (list, tuple)):
                return [clip_tree(v, depth + 1) for v in node]
            return node

        clipped = clip_tree(dict(result))
        clipped["truncated"] = True
        if len(json.dumps(clipped, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")) <= limit:
            return clipped
        keep = ("ok", "action", "run_id", "analysis_id", "analysis_ids", "path",
                "sha256", "status", "claim", "cell", "code", "message",
                # 2026-11-11 审计：成功回包超 4096 字节时旧白名单会把
                # analyze 的 analysis/run_ids 与 run_confirm 的 references
                # 整包丢掉，模型看不到自己观测的数值，claim 分支必死。
                "analysis", "analysis_relpath", "run_ids", "run_ids_head",
                "n_runs", "confirm_token", "references")
        compact = {k: clipped[k] for k in keep if k in clipped}
        compact["message"] = clip_text(str(result.get("message") or ""), budget)
        compact["code"] = result.get("code") or "RESULT_TRUNCATED"
        compact["truncated"] = True

        def _size(node: Any) -> int:
            return len(json.dumps(node, ensure_ascii=False,
                                  sort_keys=True).encode("utf-8"))

        def tighten(node: Any, budget_: int, depth: int = 0) -> Any:
            if depth > 6:
                return node
            if isinstance(node, str):
                return clip_text(node, budget_)
            if isinstance(node, Mapping):
                return {k: tighten(v, budget_, depth + 1)
                        for k, v in node.items()}
            if isinstance(node, (list, tuple)):
                return [tighten(v, budget_, depth + 1) for v in node]
            return node

        # 白名单本身也可能超帽（如 8 条 references 各带 1 KiB 正文）。逐档收紧
        # 字符串预算直到塞进上限；数值键不受影响，结构键全部保留。
        for divisor in (8, 16, 32, 64):
            trial = tighten(compact, max(16, limit // divisor))
            trial["truncated"] = True
            if _size(trial) <= limit:
                return trial
        # 最后兜底：只留身份/状态键，message 收窄——绝不超帽。
        return {"ok": ok, "code": compact["code"],
                "message": clip_text(compact["message"], 128),
                "truncated": True}


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    system: str
    messages: tuple[dict[str, str], ...]
    max_tokens: int
    serialized: str = field(compare=False)
    sha256: str
    n_bytes: int
    local_tokens: int

    def as_messages(self) -> list[dict[str, str]]:
        return [dict(m) for m in self.messages]


def summarize_event(evt: Mapping[str, Any]) -> str:
    """更早轮次的固定字段摘要（不由模型代写）。"""
    keep = ("action", "ok", "run_id", "analysis_id", "path", "sha256",
            "status", "code", "n_rows")
    parts = [f"{k}={evt[k]}" for k in keep if evt.get(k) is not None]
    return "- " + (" ".join(parts) if parts else json.dumps(evt, sort_keys=True)[:200])


def _action_of(turn: Turn) -> str:
    parsed = json.loads(turn.model_text) if _is_json(turn.model_text) else {}
    return str(parsed.get("action") or turn.result.get("action") or "?") \
        if isinstance(parsed, dict) else "?"


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _result_fields(turn: Turn) -> dict[str, Any]:
    r = turn.result
    out: dict[str, Any] = {"ok": bool(r.get("ok"))}
    for key in ("run_id", "analysis_id", "path", "sha256", "status", "code"):
        if r.get(key) is not None:
            out[key] = r[key]
    return out


def build_request(*, system: str, task_brief: str, turns: Sequence[Turn],
                  counter: Callable[[str], int],
                  max_tokens: int = MAX_OUTPUT_TOKENS) -> Request:
    """确定性构造完整请求：任务 + 最近两轮全文 + 更早固定字段摘要。"""
    messages: list[dict[str, str]] = [{"role": "user", "content": task_brief}]
    clipped = [t.clipped() for t in turns]
    split = max(0, len(clipped) - FULL_HISTORY_ROUNDS)
    older = [summarize_event({"action": _action_of(t), **_result_fields(t)})
             for t in clipped[:split]]
    if older:
        messages.append({"role": "user",
                         "content": "### 更早轮次（固定字段摘要）\n" + "\n".join(older)})
    for t in clipped[split:]:
        messages.append({"role": "assistant", "content": t.model_text})
        messages.append({"role": "user",
                         "content": json.dumps(t.result, ensure_ascii=False,
                                               sort_keys=True)})
    payload = {"system": system, "messages": messages, "max_tokens": int(max_tokens)}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    n_bytes = len(serialized.encode("utf-8"))
    return Request(system=system, messages=tuple(messages), max_tokens=int(max_tokens),
                   serialized=serialized,
                   sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                   n_bytes=n_bytes, local_tokens=int(counter(serialized)))


@dataclass(frozen=True)
class Accounting:
    verdict: str
    n_bytes: int
    local_tokens: int
    token_cap: int
    byte_cap: int

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_OK

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "n_bytes": self.n_bytes,
                "local_tokens": self.local_tokens, "token_cap": self.token_cap,
                "byte_cap": self.byte_cap,
                "note": "本地计数=上下文控制，不是供应端 input_tokens"}


def check_request(req: Request, *, token_cap: int = LOCAL_TOKEN_CAP,
                  byte_cap: int = SERIALIZED_BYTE_CAP) -> Accounting:
    """先判字节后判 token；越界即阻断（不缩帽、不截断后重发）。"""
    if req.n_bytes > byte_cap:
        verdict = VERDICT_BYTE_OVER_CAP
    elif req.local_tokens > token_cap:
        verdict = VERDICT_TOKEN_OVER_CAP
    else:
        verdict = VERDICT_OK
    return Accounting(verdict=verdict, n_bytes=req.n_bytes,
                      local_tokens=req.local_tokens,
                      token_cap=token_cap, byte_cap=byte_cap)


# ---------------------------------------------------------------------------
# 确定性 nonce（AC11：真实动态反馈必须进入下一次请求）
# ---------------------------------------------------------------------------

def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def stable_nonce(*parts: Any) -> str:
    """把无语义关联的状态段压成 16 位 hex：同状态同值，状态一变即变。

    只用于请求内「可见状态指纹」，不承担加密职责；不含隐藏结果。
    """
    h = hashlib.sha256()
    h.update(b"simpraxis.rsi_r1.nonce.v1\n")
    for part in parts:
        h.update(canonical_bytes(part))
        h.update(b"\n")
    return h.hexdigest()[:16]
