"""RSI-R1 真实模型传输：``T.Request`` → 一次 ``ModelGateway.call`` → ``L.Reply``。

这条通道是 R1 里**唯一**会发网的地方。它只被 ``loop.build_production_sender``
构造出来的 ``LockedSender`` 调用（准入/LOCK/预算槽/请求 hash 在调用前已经全部
复核过），因此本模块自己不重复做门——但有三件事是构造期硬前提，缺一即拒：

1. ``max_attempts=1``：网关内部的 HTTP 退避重试会让「一次逻辑调用」变成多次
   POST，直接破坏 §8.1 的 lane 记账口径与 ``Reply.attempts_http<=1`` 断言；
2. ``billing_context``：网关挂了账本（``ledger=QuotaLedger.rsi_r1()``）时没有
   ``{task, batch_id, thread_id}`` 的调用会被 ``BillingGate`` fail-closed，本模块
   在构造时就把它挡在第一次 POST 之前（R0.2 T4 首跑踩过这个坑）；
3. 付费授权：``SIMPRAXIS_ALLOW_PAID=1`` 环境闸 + ``SpendContract(mode=paid,
   consent=...)``；没有 ``R1_SCOPE_RISK_WAIVER`` 授权就零 POST。

``response_id`` 由 lane + 进程内序号 + 请求 sha 前缀组成。它必须**在本轮
campaign 内唯一**：``state.record_reply`` 按 response_id 落 ``state/replies/``，
而 ``assembly.CampaignDispatcher._binding`` 比对「上一条 action 事件的
response_id」。同一 (lane, 序号, 请求 sha) 三元组再次出现意味着请求字节完全相同
（即可见状态没变），而这只有在该回复没有被记入历史时才会发生——循环每轮都会
记回复，故该三元组在实践中不会重复（T5 排演暴露过用固定 label 导致的撞号）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import loop as L

#: 研究动作统一走 ``coding_debug`` 阶段（与 R0/R0.2 的协议探针对齐）。
DEFAULT_STAGE = "coding_debug"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT_S = 120


class TransportError(RuntimeError):
    """配置/前提错误（不是模型结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def require_paid_env(env: "Mapping[str, str] | None" = None) -> None:
    """付费运行的环境闸：``SIMPRAXIS_ALLOW_PAID=1`` 才放行（R0 先例）。"""
    values = os.environ if env is None else env
    if str(values.get("SIMPRAXIS_ALLOW_PAID", "")).strip() != "1":
        raise TransportError(
            "PAID_NOT_AUTHORIZED",
            "实时付费运行需 SIMPRAXIS_ALLOW_PAID=1（与 consent 双闸）")


def _usage_of(capture: Mapping[str, Any],
              tokens: Any) -> "dict[str, Any]":
    """把网关的 usage 归一化成 ``{input_tokens, output_tokens, ...}``。

    缺 usage 一律 ``None``（§8.3：缺 usage 不当 0）。``call`` 的返回值在新旧
    版本间有二/四元组两种形态，这里两路都认。
    """
    usage: "dict[str, Any]" = {}
    if isinstance(tokens, (tuple, list)) and len(tokens) >= 2:
        usage["input_tokens"] = tokens[0]
        usage["output_tokens"] = tokens[1]
        if len(tokens) >= 3:
            usage["cache_read_tokens"] = tokens[2]
        if len(tokens) >= 4:
            usage["cache_create_tokens"] = tokens[3]
    elif isinstance(tokens, Mapping):
        usage.update(dict(tokens))
    captured = capture.get("usage")
    if isinstance(captured, Mapping):
        for src, dst in (("in", "input_tokens"), ("out", "output_tokens"),
                         ("cache_read", "cache_read_tokens"),
                         ("cache_create", "cache_create_tokens")):
            if captured.get(src) is not None:
                usage[dst] = captured[src]
    return usage


@dataclass
class GatewayTransport:
    """一次逻辑调用 = 一次 ``gateway.call``；绝不重发（``max_attempts=1``）。"""

    gateway: Any
    lane: str
    batch_id: str
    thread_id: str = ""
    stage: str = DEFAULT_STAGE
    temperature: float = DEFAULT_TEMPERATURE
    timeout: int = DEFAULT_TIMEOUT_S
    _n: int = field(default=0, init=False)
    calls: "list[dict[str, Any]]" = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.lane not in LANES.LANE_TASK_ID:
            raise TransportError("LANE_UNKNOWN", repr(self.lane))
        attempts = getattr(self.gateway, "max_attempts", None)
        if int(attempts or 0) != 1:
            raise TransportError(
                "HIDDEN_RETRY",
                f"ModelGateway 必须以 max_attempts=1 构造，实为 {attempts!r}")
        if not str(self.batch_id).strip():
            raise TransportError("BATCH_MISSING", "batch_id 不得为空")
        billing = getattr(self.gateway, "_billing", None)
        if billing is None:
            # 账外调用只允许出现在显式 unguarded 模式（--validate-models/单测）。
            if not getattr(self.gateway, "unguarded", False):
                raise TransportError(
                    "COST_UNKNOWN", "网关未挂账本：实呼必须逐次记账")
        if not hasattr(self.gateway, "call"):
            raise TransportError("GATEWAY_INVALID", "gateway 缺 call()")

    # -- 记账归属 --------------------------------------------------------
    @property
    def task_id(self) -> int:
        return int(LANES.LANE_TASK_ID[self.lane])

    def _billing(self) -> Any:
        from orchestrator.gateway.quota_ledger import billing_context

        return billing_context(task=self.task_id, batch_id=self.batch_id,
                               thread_id=self.thread_id)

    # -- 传输 ------------------------------------------------------------
    def __call__(self, request: T.Request) -> L.Reply:
        if not isinstance(request, T.Request):
            raise TransportError("REQUEST_INVALID",
                                 f"期望 T.Request，实为 {type(request).__name__}")
        messages = [{"role": "system", "content": request.system},
                    *[dict(m) for m in request.messages]]
        capture: "dict[str, Any]" = {}
        with self._billing():
            text, tokens = self.gateway.call(
                self.stage, messages, max_tokens=int(request.max_tokens),
                temperature=float(self.temperature), timeout=int(self.timeout),
                capture=capture)
        self._n += 1
        attempts = int(capture.get("attempts_http", 1) or 1)
        reply = L.Reply(
            text=str(text or ""),
            response_id=(f"r1-{self.lane}-{self._n:03d}-"
                         f"{request.sha256[:12]}"),
            usage=_usage_of(capture, tokens),
            attempts_http=attempts)
        self.calls.append({"n": self._n, "request_sha256": request.sha256,
                           "response_id": reply.response_id,
                           "attempts_http": attempts,
                           "stop_reason": capture.get("stop_reason"),
                           "content_bytes": capture.get("content_bytes")})
        return reply


def build_gateway(*, ledger: Any, pause_state_path: Any = None,
                  stage_models_path: Any = None, stage: str = DEFAULT_STAGE
                  ) -> Any:
    """生产网关：``max_attempts=1`` + R1 账本 + 本命名空间熔断件。"""
    from orchestrator.gateway.router import ModelGateway

    # ``ledger=`` 必须以**具名关键字**出现：G-13 的 AST 红线扫描
    # （``tests/milestone1/test_quota_ledger_hardening.py``）只认具名 kwargs，
    # 打包成 ``**kwargs`` 会被判「未挂账本的构造点」。可选件单独并入。
    optional: "dict[str, Any]" = {}
    if pause_state_path is not None:
        optional["pause_state_path"] = pause_state_path
    if stage_models_path is not None:
        optional["stage_models_path"] = stage_models_path
    gateway = ModelGateway(max_attempts=1, ledger=ledger, **optional)
    if gateway.model_id(stage) is None:
        raise TransportError("STAGE_UNRESOLVED",
                             f"stage={stage} 的 model_id 为 null（未实证）")
    return gateway


def build_transport(*, gateway: Any, lane: str, batch_id: str,
                    thread_id: str = "", env: "Mapping[str, str] | None" = None,
                    stage: str = DEFAULT_STAGE) -> GatewayTransport:
    """付费传输的构造点：先过环境闸，再装配。"""
    require_paid_env(env)
    return GatewayTransport(gateway=gateway, lane=lane, batch_id=batch_id,
                            thread_id=thread_id, stage=stage)


def null_transport(request: T.Request) -> L.Reply:  # pragma: no cover - 防御性
    raise TransportError("TRANSPORT_NULL", "null transport 不得被调用")


__all__ = ["DEFAULT_STAGE", "GatewayTransport", "TransportError",
           "build_gateway", "build_transport", "require_paid_env",
           "null_transport"]
