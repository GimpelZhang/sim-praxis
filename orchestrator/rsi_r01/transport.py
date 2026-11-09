"""RSI-R0.1 传输层：完整输入计数、一逻辑请求一 POST、UNKNOWN_SENT 语义。

计划 §6.2 的落地。三条硬合同：

1. **计数对象是即将发送的完整序列化请求**（system/messages/tool schema/历史/
   包装/供应商额外开销），计数后字节 hash 必须与发网件一致；
2. **全链一次逻辑请求最多 1 POST**——gateway、SDK、HTTP adapter 都没有自动重试，
   本模块用 ``sender`` 恰好调用一次，并用 fake transport 精确计数验证；
3. 网络错误在**发送状态不确定**时记 ``UNKNOWN_SENT``：占槽、按最坏费用预留、
   **不自动重发**；发网前拒绝是独立事件（``PREFLIGHT_BLOCKED``），不伪装 POST。

计数证明（写进证据包）：

* 上界 U = 序列化请求的 UTF-8 字节数。理由：任何字节级 BPE 词表里每个 token
  都解码为**至少一个字节**，故 token 数不可能超过字节数。此界可证但对长链过松。
* 下界 L 需要「单 token 最多编码 k 字节」这一供应商事实。没有锁定该常量时
  L=None，长链区间 [15000,18000] 无法被证明落入 ⇒ 按计划记
  ``BLOCKED_CONTEXT_MEASUREMENT``，**不把短输入冒充长链**，也不用事后 usage
  当硬门（plan 明确禁止）。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from orchestrator.rsi_r01 import schemas as S


class SendState:
    PREFLIGHT_BLOCKED = "preflight_blocked"
    SENT_OK = "sent_ok"
    SENT_FAIL = "sent_fail"
    UNKNOWN_SENT = "unknown_sent"


class AccountingVerdict:
    OK = "OK"
    TOKEN_OVER_CAP = "TOKEN_OVER_CAP"
    BLOCKED_TOKEN_ACCOUNTING = "BLOCKED_TOKEN_ACCOUNTING"
    BLOCKED_CONTEXT_MEASUREMENT = "BLOCKED_CONTEXT_MEASUREMENT"


@dataclass(frozen=True)
class SendPlan:
    """即将发网的完整请求（序列化即计数对象，也即发网件）。"""

    request: dict[str, Any]
    max_output_tokens: int = S.MAX_OUTPUT_TOKENS

    @property
    def serialized(self) -> bytes:
        return json.dumps(self.request, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.serialized).hexdigest()

    @property
    def n_bytes(self) -> int:
        return len(self.serialized)


@dataclass(frozen=True)
class Accounting:
    verdict: str
    n_bytes: int
    upper_tokens: int
    lower_tokens: int | None
    interval: tuple[int, int] | None
    interval_ok: bool
    request_sha256: str
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"schema": S.SCHEMA_INPUT_ACCOUNTING,
                "verdict": self.verdict, "bytes": self.n_bytes,
                "upper_tokens": self.upper_tokens,
                "lower_tokens": self.lower_tokens,
                "interval": list(self.interval) if self.interval else None,
                "interval_ok": self.interval_ok,
                "request_sha256": self.request_sha256,
                "upper_bound_proof": "tokens ≤ utf8_bytes（每个 token 至少编码 1 字节）",
                "lower_bound_proof": ("tokens ≥ ceil(bytes/max_token_bytes)；"
                                      "max_token_bytes 必须由锁定的供应商事实给出"),
                "reasons": list(self.reasons)}


def count_input(plan: SendPlan, *, cap: int = S.MAX_INPUT_TOKENS,
                interval: tuple[int, int] | None = None,
                max_token_bytes: int | None = None) -> Accounting:
    """对**完整序列化请求**计数并判定是否放行。

    ``interval`` 给出时必须能证明整个 [L,U] 落入区间才算通过（长链场景）。
    """
    n = plan.n_bytes
    upper = n                                   # 可证上界
    lower = None
    reasons: list[str] = []
    if max_token_bytes is not None:
        if max_token_bytes <= 0:
            raise ValueError("max_token_bytes 必须为正整数")
        lower = math.ceil(n / int(max_token_bytes))
        if lower > upper:
            raise ValueError("下界大于上界：max_token_bytes 常量不可能 ≥1 才合法")
    if upper > cap:
        return Accounting(AccountingVerdict.TOKEN_OVER_CAP, n, upper, lower,
                          interval, False, plan.sha256,
                          (S.ReasonCode.TOKEN_OVER_CAP,))
    interval_ok = True
    verdict = AccountingVerdict.OK
    if interval is not None:
        lo, hi = int(interval[0]), int(interval[1])
        if lower is None:
            # 只有宽松上界：不能证明长度下限，不许把短输入冒充长链
            interval_ok = False
            verdict = AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
            reasons.append("LOWER_BOUND_UNPROVABLE")
        elif lower < lo or upper > hi:
            interval_ok = False
            verdict = AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
            reasons.append("INTERVAL_NOT_PROVEN")
    return Accounting(verdict, n, upper, lower, interval, interval_ok,
                      plan.sha256, tuple(reasons))


# ---------------------------------------------------------------------------
# 单 POST 传输
# ---------------------------------------------------------------------------

#: sender 的返回约定：``(meta, text)``；meta 至少含 http_status / request_id /
#: stop_reason / usage。抛异常一律视为**发送状态不确定**。
Sender = Callable[[SendPlan], "tuple[Mapping[str, Any], str]"]


@dataclass
class Attempt:
    """一次逻辑请求的完整状态（不重发、不合并）。"""

    seq: int
    slot_id: str
    state: str = SendState.PREFLIGHT_BLOCKED
    request_sha256: str | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    text_sha256: str | None = None
    text_len: int = 0
    failure: str | None = None
    reason_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": S.SCHEMA_ATTEMPT, "seq": self.seq, "slot_id": self.slot_id,
                "state": self.state, "request_sha256": self.request_sha256,
                "http_status": self.http_status,
                "provider_request_id": self.provider_request_id,
                "stop_reason": self.stop_reason, "usage": self.usage,
                "text_sha256": self.text_sha256, "text_len": self.text_len,
                "failure": self.failure,
                "reason_codes": list(self.reason_codes)}


class PreflightRefused(RuntimeError):
    """计数/预算/冻结任一不过 ⇒ 零 POST（独立事件，不伪装成发送失败）。"""


def classify_reply(meta: Mapping[str, Any], text: str) -> tuple[str, list[str]]:
    """把一次 HTTP 200 回包分类；返回 (state, reason_codes)。

    * 空 content ⇒ ``SENT_FAIL/EMPTY_CONTENT``（是一次**已消费**的 POST 失败）；
    * 只有非文本 block ⇒ ``SENT_FAIL/NON_TEXT_BLOCK``；
    * ``stop_reason == "max_tokens"`` 或显式 truncated ⇒ ``SENT_FAIL/TRUNCATED``；
    * 正常文本 ⇒ ``SENT_OK``。
    """
    codes: list[str] = []
    blocks = meta.get("content_blocks")
    if isinstance(blocks, list) and blocks:
        text_blocks = [b for b in blocks
                       if isinstance(b, Mapping) and b.get("type") == "text"
                       and str(b.get("text") or "").strip()]
        if not text_blocks:
            return SendState.SENT_FAIL, [S.ReasonCode.NON_TEXT_BLOCK]
    if not isinstance(text, str) or not text.strip():
        return SendState.SENT_FAIL, [S.ReasonCode.EMPTY_CONTENT]
    if meta.get("truncated") is True or str(meta.get("stop_reason") or "") == "max_tokens":
        return SendState.SENT_FAIL, [S.ReasonCode.TRUNCATED]
    return SendState.SENT_OK, codes


class Transport:
    """一逻辑请求一 POST 的唯一通道；自带序列计数与终态记账。"""

    def __init__(self):
        self.attempts: list[Attempt] = []
        self._seq = 0

    @property
    def posts(self) -> int:
        """真正发网的 POST 数（preflight_blocked 不计）。"""
        return sum(1 for a in self.attempts if a.state != SendState.PREFLIGHT_BLOCKED)

    def send(self, plan: SendPlan, accounting: Accounting, *, slot_id: str,
             sender: Sender) -> Attempt:
        """发一次（且仅一次）。计数不过则零 POST 并抛 :class:`PreflightRefused`。"""
        self._seq += 1
        att = Attempt(seq=self._seq, slot_id=slot_id,
                      request_sha256=plan.sha256)
        self.attempts.append(att)
        if accounting.verdict != AccountingVerdict.OK:
            att.reason_codes = list(accounting.reasons) or [accounting.verdict]
            raise PreflightRefused(f"{accounting.verdict}: {att.reason_codes}")
        if accounting.request_sha256 != plan.sha256:
            att.reason_codes = ["ACCOUNTING_HASH_MISMATCH"]
            raise PreflightRefused("计数的字节 hash 与发网件不一致")
        try:
            meta, text = sender(plan)
        except Exception as exc:  # noqa: BLE001 —— 发送状态不确定，绝不重发
            att.state = SendState.UNKNOWN_SENT
            att.failure = f"{type(exc).__name__}: {exc}"[:400]
            att.reason_codes = [S.ReasonCode.UNKNOWN_SENT]
            raise
        att.state, att.reason_codes = classify_reply(meta, text)
        att.http_status = meta.get("http_status")
        att.provider_request_id = meta.get("request_id")
        att.stop_reason = meta.get("stop_reason")
        att.usage = dict(meta.get("usage") or {}) or None
        att.text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        att.text_len = len(text or "")
        return att

    def summary(self) -> dict[str, Any]:
        counts = {SendState.PREFLIGHT_BLOCKED: 0, SendState.SENT_OK: 0,
                  SendState.SENT_FAIL: 0, SendState.UNKNOWN_SENT: 0}
        for a in self.attempts:
            counts[a.state] = counts.get(a.state, 0) + 1
        return {"attempts": len(self.attempts), "posts": self.posts, **counts}


def plan_from_messages(*, system: str | None, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None,
                       max_output_tokens: int = S.MAX_OUTPUT_TOKENS,
                       model: str | None = None) -> SendPlan:
    """构造发网件：所有会被计费/计数的部分都在同一 dict 里（不藏在适配器里）。"""
    req: dict[str, Any] = {"messages": messages,
                           "max_tokens": int(max_output_tokens)}
    if system:
        req["system"] = system
    if tools:
        req["tools"] = tools
    if model:
        req["model"] = model
    return SendPlan(request=req, max_output_tokens=int(max_output_tokens))
