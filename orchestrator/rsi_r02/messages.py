"""RSI-R0.2 上下文构造与本地输入门（计划 §4.1，AC12）。

三条纪律：

1. **本地计数是上下文控制，不是供应端 token**。计数对象是**完整序列化请求**
   （system + 全部 messages + max_tokens 包装），结果只用于本地 20000/128KiB 闸。
2. **确定性**：同一历史必得同一请求字节；更早轮次压缩为固定字段事件摘要，不由
   第二模型代写；数据文件不展开（只给路径/hash/状态）。
3. **fail-closed**：分词器缺件或哈希不符即拒绝计数与放行，不用字节估算冒充 token。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import protocol as PR
from orchestrator.rsi_r01 import schemas as S

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


# ---------------------------------------------------------------------------
# 本地计数器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalCounter:
    """钉 sha256 的分词器（复用 R0.1 ``protocol.Tokenizer``，不复制实现）。"""

    tokenizer: PR.Tokenizer

    @classmethod
    def load(cls, path: str | Any, sha256: str) -> "LocalCounter":
        try:
            return cls(tokenizer=PR.Tokenizer.load(path, sha256))
        except PR.TokenizerUnavailable as exc:
            raise CounterUnavailable(str(exc)) from exc

    @property
    def sha256(self) -> str:
        return self.tokenizer.sha256

    def count(self, text: str) -> int:
        return int(self.tokenizer.count(text))

    def describe(self) -> dict[str, str]:
        return {"kind": "hf_tokenizers", "path": str(self.tokenizer.path),
                "sha256": self.tokenizer.sha256}


def require_counter(counter: LocalCounter | Callable[[str], int] | None
                    ) -> Callable[[str], int]:
    """接受 ``LocalCounter`` 或可调用；None 一律拒绝（fail-closed）。"""
    if counter is None:
        raise CounterUnavailable("没有钉哈希的本地分词器，禁止构造/计数请求")
    if isinstance(counter, LocalCounter):
        return counter.count
    if callable(counter):
        return counter
    raise TypeError(f"不支持的计数器：{type(counter)!r}")


# ---------------------------------------------------------------------------
# system 文本（任务/API/边界/动作协议；schema 作为文本，不用原生 tools）
# ---------------------------------------------------------------------------


def action_schema_text(surface: AC.ActionSurface) -> str:
    bounds = {k: [v[0], v[1]] for k, v in sorted(surface.param_bounds.items())}
    fixed = {k: v for k, v in sorted(surface.fixed_params.items())}
    return "\n".join([
        "### 动作协议（每次回复只允许一条 JSON 对象，不要解释文字）",
        f'每条动作必须带 "schema": "{S.SCHEMA_ACTION}"。',
        "可用动作与必填字段：",
        '1) {"schema":..., "action":"preregister", "plan":{...}} —— 必须在第一个实验之前，'
        "成功后不得重复；plan 的必填字段逐字见任务说明 public.prereg.required_fields。",
        '2) {"schema":..., "action":"run_experiment", "params":{参数名:数值}}',
        '3) {"schema":..., "action":"analyze", "source":"<python源码>", "run_ids":["<run_id>",...]}',
        '4) {"schema":..., "action":"submit", "claim":"SUPPORTED|REFUTED|INCONCLUSIVE", '
        '"run_ids":[...], "analysis_ids":[...], "nonce":"<逐字>", "stat":数值} —— '
        "必填字段逐字见任务说明 public.submit.required_fields。",
        "",
        f"参数合法区间：{json.dumps(bounds, ensure_ascii=False, sort_keys=True)}",
        f"固定参数：{json.dumps(fixed, ensure_ascii=False, sort_keys=True)}",
        f"单次实验步数上限：{surface.max_steps}",
        "",
        "### 边界",
        "* 你没有网络、仓库、知识库、私有真值或其他试次的访问权；只能使用本对话给出的公开信息。",
        "* 每次回复恰好一个动作；动作失败会以结构化错误返回，你可以在剩余预算内自行修正。",
        "* 任务说明里的 required_fields 是字段要求的**唯一权威**：缺字段/类型不符都会被拒，"
        "请照抄字段名，不要自创字段。",
        "* analyze 的源码在受限容器内运行（无网络、只读根、CPU 限制），只能写 analysis.json；"
        "数值必须来自你本次实验的真实观测。",
        "* submit 的数字必须由你之前的 analyze 结果给出，并逐字引用 run_id / analysis_id。",
    ])


def build_system(public_doc: Mapping[str, Any], surface: AC.ActionSurface) -> str:
    pub = public_doc.get("public") or {}
    head = [
        "你是自主研究/工程候选。你只能通过给定的文本 JSON 动作与任务交互。",
        "",
        "### 任务",
        f"实例：{public_doc.get('instance_id')}（族：{public_doc.get('family')}）",
        f"目标：{pub.get('goal')}",
    ]
    research = pub.get("research")
    if research:
        head.append(f"公开研究假设 h={research.get('h')}："
                    f"阻尼区间 {research.get('band')}，窗口 {research.get('window_s')} s")
    iface = pub.get("interface") or {}
    head.append(f"公开接口：{json.dumps(iface, ensure_ascii=False, sort_keys=True)}")
    # 公开文档里的字段表必须**真的渲染进 system**：候选唯一的输入通道就是这份
    # 文本（``candidate.CandidateLoop.system``），只把 required_fields 放进文档
    # dict 而渲染时丢掉，等于 action_schema_text 指向一个候选看不见的章节——
    # 这正是 T4 首跑判 BLOCKED_PROTOCOL 的那类自相矛盾（评审 CRITICAL-1）。
    for key, title in (("prereg", "preregister 的 plan 必填字段"),
                       ("submit", "submit 必填字段")):
        spec = pub.get(key)
        if not isinstance(spec, Mapping) or not spec:
            continue
        head.append("")
        head.append(f"### {title}（逐字照抄字段名，不得增删或改名）")
        note = str(spec.get("note") or "").strip()
        if note:
            head.append(note)
        fields = spec.get("required_fields") or {}
        if isinstance(fields, Mapping):
            for name, desc in fields.items():
                head.append(f"* {name}：{desc}")
        if spec.get("delta_fixed") is not None:
            head.append(f"* delta 必须逐字等于 {spec['delta_fixed']}")
        if spec.get("cells_rule"):
            head.append(f"* cells 规则：{spec['cells_rule']}")
        values = spec.get("values")
        if values:
            head.append("* 本实例的参数取值：" + json.dumps(
                values, ensure_ascii=False, sort_keys=True))
    return "\n".join(head) + "\n\n" + action_schema_text(surface)


# ---------------------------------------------------------------------------
# 历史压缩与请求构造
# ---------------------------------------------------------------------------


def summarize_event(evt: Mapping[str, Any]) -> str:
    """更早轮次的固定字段摘要（不由模型代写）。"""
    keep = ("action", "ok", "run_id", "analysis_id", "path", "sha256",
            "status", "code", "n_rows")
    parts = [f"{k}={evt[k]}" for k in keep if evt.get(k) is not None]
    return "- " + (" ".join(parts) if parts else json.dumps(evt, sort_keys=True)[:200])


def _clip(text: str, limit: int) -> str:
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
        return Turn(model_text=_clip(self.model_text, TOOL_REPLY_MAX_BYTES),
                    result=self._clip_result(self.result))

    @staticmethod
    def _clip_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
        ok = bool(result.get("ok"))
        limit = TOOL_REPLY_MAX_BYTES if ok else ERROR_REPLY_MAX_BYTES
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(raw.encode("utf-8")) <= limit:
            return dict(result)
        return {"ok": ok, "code": result.get("code") or "RESULT_TRUNCATED",
                "message": _clip(str(result.get("message") or ""), limit // 2),
                "truncated": True}


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


def count_available(counter: LocalCounter | Callable[[str], int] | None) -> bool:
    try:
        require_counter(counter)
    except CounterUnavailable:
        return False
    return True
