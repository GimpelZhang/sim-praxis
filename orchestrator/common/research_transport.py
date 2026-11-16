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

#: RSI-G2（Plan §2.4）：交付策略是**冻结运行配置**，不由模型在 action 里选择。
#: 只有显式 ``g2_atomic_v1`` 才启用新 envelope 分支；``legacy`` 逐字旧行为。
POLICY_LEGACY = "legacy"
POLICY_G2_ATOMIC = "g2_atomic_v1"
DELIVERY_POLICIES = (POLICY_LEGACY, POLICY_G2_ATOMIC)
G2_ENVELOPE_KEY = "result_envelope"
G2_ENVELOPE_MAX_BYTES = 16 * 1024
#: 只读分页/产物上限（RSI-G2.1：一源常量；``g2_contract`` 直接别名到这里）。
G2_PAGE_MAX_BYTES = 8 * 1024
G2_ARTIFACT_MAX_BYTES = 8 * 1024 ** 2
G2_CONTEXT_HEADER = "### 活动结果视图（result_context；g2_atomic_v1，不可裁剪）\n"
G2_ENVELOPE_HEADER = "### 结果视图（g2_atomic_v1 最小交付单元，不可裁剪）\n"
#: 原子单元必需键（与 ``rsi_r1.g2_contract.ENVELOPE_REQUIRED`` 同名单；
#: 一致性由 tests/test_rsi_g2_contract.py 机器核对）。
G2_ENVELOPE_REQUIRED = (
    "schema", "schema_version", "study_id", "phase", "policy_id",
    "plan_sha256", "analysis_receipt_sha256", "analysis_output_sha256",
    "scope_contract_sha256", "scope", "required_contrast_ids", "contrasts",
    "primary", "diagnostics", "artifacts", "evidence", "summary_sha256",
    "envelope_sha256", "truncation",
)


class ResultViewIncomplete(RuntimeError):
    """最小交付单元无法完整构造/送达（**发网前**阻断；0 网络）。"""

    code = "RESULT_VIEW_INCOMPLETE"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(f"[RESULT_VIEW_INCOMPLETE] {detail}" if detail else
                         "RESULT_VIEW_INCOMPLETE")


def g2_envelope_bytes(envelope: Any) -> bytes:
    """原子校验 + 规范字节：缺任一必需键/超 16 KiB/NaN 一律阻断。"""
    if not isinstance(envelope, Mapping):
        raise ResultViewIncomplete("envelope 必须是对象")
    missing = [key for key in G2_ENVELOPE_REQUIRED if key not in envelope]
    if missing:
        raise ResultViewIncomplete(f"缺必需字段 {missing}")
    if envelope.get("truncation") is not False:
        raise ResultViewIncomplete("truncation 必须为 false")
    primary = envelope.get("primary")
    if not isinstance(primary, Mapping) or not {
            "estimand_id", "contrast_id", "metric_id", "estimate",
            "interval"} <= set(primary):
        raise ResultViewIncomplete("primary 不完整")
    for key in ("scope", "artifacts", "evidence", "required_contrast_ids",
                "contrasts"):
        if not envelope.get(key):
            raise ResultViewIncomplete(f"{key} 为空")
    try:
        data = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResultViewIncomplete(f"非规范 JSON: {exc}") from None
    if len(data) > G2_ENVELOPE_MAX_BYTES:
        raise ResultViewIncomplete(
            f"envelope {len(data)}B > {G2_ENVELOPE_MAX_BYTES}B（不偷偷只取 primary）")
    return data



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

#: ``read_result_artifact`` 分页回包的判别键（全部必须同时在场）。
READ_PAGE_KEYS = frozenset({"artifact_id", "artifact_sha256", "offset",
                            "n_bytes", "total_bytes", "page_sha256",
                            "encoding", "data", "next_cursor"})


def is_read_page_result(result: Any) -> bool:
    """是否为 G2 只读分页回包（结构判别；不依赖 action 名）。"""
    if not isinstance(result, Mapping):
        return False
    return READ_PAGE_KEYS <= set(result) and bool(result.get("ok"))


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

    def clipped(self, *, policy: str = POLICY_LEGACY) -> "Turn":
        if policy == POLICY_LEGACY:
            return Turn(model_text=clip_text(self.model_text, TOOL_REPLY_MAX_BYTES),
                        result=self._clip_result(self.result))
        if policy != POLICY_G2_ATOMIC:
            raise ValueError(f"未知 delivery_policy={policy!r}")
        return self._g2_clipped()

    def _g2_clipped(self) -> "Turn":
        """G2：envelope 走原子校验，**不进** 通用裁剪白名单（Plan §2.4）。"""
        text = clip_text(self.model_text, TOOL_REPLY_MAX_BYTES)
        if G2_ENVELOPE_KEY in self.result:
            envelope = self.result[G2_ENVELOPE_KEY]
            g2_envelope_bytes(envelope)          # 缺失/超帽 ⇒ ResultViewIncomplete
            return Turn(model_text=text,
                        result={"ok": bool(self.result.get("ok")),
                                "action": self.result.get("action"),
                                G2_ENVELOPE_KEY: dict(envelope),
                                "truncated": False})
        if is_read_page_result(self.result):
            # RSI-G2.1（AC G21-07）：只读分页回包同样是**原子交付单元**。
            # 旧通用裁剪按 4096 帽把 8 KiB 页的 data 截成前缀，候选拿到的
            # 页既无 next_cursor 也无完整 sha 校验面——读取会被仪器静默
            # 破坏。这里只校验结构/页帽后逐字保留；超帽仍在发网前阻断。
            page = dict(self.result)
            data = page.get("data")
            if not isinstance(data, str) or page.get("encoding") not in (
                    "utf-8", "base64"):
                raise ResultViewIncomplete("read page 结构不符")
            n_bytes = page.get("n_bytes")
            if (not isinstance(n_bytes, int) or isinstance(n_bytes, bool)
                    or not 0 <= n_bytes <= G2_PAGE_MAX_BYTES):
                raise ResultViewIncomplete(f"read page n_bytes={n_bytes!r}")
            page["truncated"] = False
            return Turn(model_text=text, result=page)
        # 非确认类结果（write_artifact/propose_plan 等）沿用旧裁剪：G2 只改
        # 确认结果的最小交付单元，不扩大其它回包的可见面。
        return Turn(model_text=text, result=self._clip_result(self.result))


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
                "n_runs", "confirm_token", "references",
                # 2026-11-12 T5 离线演练：R1.2 的 run_confirm 把候选自己的
                # 确认批分析结果放在 ``candidate_output`` 里回给候选（提交
                # claims 只能从这里读），白名单漏了它 ⇒ 确认批 96 跑的
                # run_ids 一超帽，输出被整包丢掉，submit_claim 必然无值可引。
                # 属于与 11-11 同类的仪器耦合，不是候选能力。
                "candidate_output", "analysis_receipt")
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
                  max_tokens: int = MAX_OUTPUT_TOKENS,
                  delivery_policy: str = POLICY_LEGACY,
                  result_context: "Mapping[str, Any] | None" = None) -> Request:
    """确定性构造完整请求：任务 + 最近两轮全文 + 更早固定字段摘要。

    ``delivery_policy="legacy"``（默认）逐字旧行为；``g2_atomic_v1`` 走
    最小交付分支：envelope 以规范字节**逐字**进入 messages（终检在场），
    活动 result_context 固定携带、跨轮不重复。超界/缺失抛
    ``ResultViewIncomplete``（发网前阻断，0 网络）。
    """
    if delivery_policy not in DELIVERY_POLICIES:
        raise ValueError(f"未知 delivery_policy={delivery_policy!r}")
    if delivery_policy == POLICY_LEGACY:
        if result_context is not None:
            raise ValueError("legacy 不支持 result_context（不得暗改旧请求）")
        return _build_request_legacy(system=system, task_brief=task_brief,
                                     turns=turns, counter=counter,
                                     max_tokens=max_tokens)
    return _build_request_g2(system=system, task_brief=task_brief,
                             turns=turns, counter=counter,
                             max_tokens=max_tokens,
                             result_context=result_context)


def _finalize_request(*, system: str, messages: "list[dict[str, str]]",
                      counter: Callable[[str], int],
                      max_tokens: int) -> Request:
    payload = {"system": system, "messages": messages, "max_tokens": int(max_tokens)}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    n_bytes = len(serialized.encode("utf-8"))
    return Request(system=system, messages=tuple(messages), max_tokens=int(max_tokens),
                   serialized=serialized,
                   sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                   n_bytes=n_bytes, local_tokens=int(counter(serialized)))


def _build_request_legacy(*, system: str, task_brief: str,
                          turns: Sequence[Turn],
                          counter: Callable[[str], int],
                          max_tokens: int) -> Request:
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
    return _finalize_request(system=system, messages=messages,
                             counter=counter, max_tokens=max_tokens)

def _build_request_g2(*, system: str, task_brief: str, turns: Sequence[Turn],
                      counter: Callable[[str], int], max_tokens: int,
                      result_context: "Mapping[str, Any] | None") -> Request:
    messages: list[dict[str, str]] = [{"role": "user", "content": task_brief}]
    required_texts: "list[str]" = []
    active_sha: "str | None" = None
    if result_context is not None:
        ctx_bytes = g2_envelope_bytes(result_context)
        active_sha = str(result_context.get("envelope_sha256"))
        ctx_text = ctx_bytes.decode("utf-8")
        messages.append({"role": "user", "content": G2_CONTEXT_HEADER + ctx_text})
        required_texts.append(ctx_text)
    split = max(0, len(turns) - FULL_HISTORY_ROUNDS)
    # 更早轮次只留固定字段摘要；若其中有 envelope 而调用方又没给
    # result_context，则该 envelope 会被摘要丢弃——显式阻断，绝不静默丢证据。
    for t in turns[:split]:
        if G2_ENVELOPE_KEY in t.result and active_sha is None:
            raise ResultViewIncomplete(
                "早期 envelope 会落入摘要区；必须提供 result_context")
    older = [summarize_event({"action": _action_of(t), **_result_fields(t)})
             for t in turns[:split]]
    if older:
        messages.append({"role": "user",
                         "content": "### 更早轮次（固定字段摘要）\n" + "\n".join(older)})
    seen_env: "set[str]" = set()
    for t in turns[split:]:
        if G2_ENVELOPE_KEY not in t.result:
            view = t.clipped(policy=POLICY_G2_ATOMIC)
            messages.append({"role": "assistant", "content": view.model_text})
            messages.append({"role": "user",
                             "content": json.dumps(view.result,
                                                   ensure_ascii=False,
                                                   sort_keys=True)})
            continue
        envelope = t.result[G2_ENVELOPE_KEY]
        env_bytes = g2_envelope_bytes(envelope)
        env_sha = str(envelope.get("envelope_sha256"))
        rest = {k: v for k, v in t.result.items() if k != G2_ENVELOPE_KEY}
        messages.append({"role": "assistant",
                         "content": clip_text(t.model_text, TOOL_REPLY_MAX_BYTES)})
        if env_sha == active_sha or env_sha in seen_env:
            # 同一 envelope 已在 result_context / 上一回合逐字出现：防重复叠加。
            messages.append({"role": "user",
                             "content": json.dumps(rest, ensure_ascii=False,
                                                   sort_keys=True)})
            continue
        seen_env.add(env_sha)
        env_text = env_bytes.decode("utf-8")
        messages.append({"role": "user",
                         "content": json.dumps(rest, ensure_ascii=False,
                                               sort_keys=True)
                         + "\n" + G2_ENVELOPE_HEADER + env_text})
        required_texts.append(env_text)
    req = _finalize_request(system=system, messages=messages,
                            counter=counter, max_tokens=max_tokens)
    # 终检（Plan §2.4）：完整 envelope 必须在**最终** messages 中逐字在场。
    joined = "\n".join(message["content"] for message in req.messages)
    for text in required_texts:
        if text not in joined:
            raise ResultViewIncomplete("最终 request 中 envelope 不在场（RETURN_VIEW）")
    return req



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
