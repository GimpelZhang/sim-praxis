"""GatewayModel：smolagents Model → ModelGateway.call("coding_debug") 唯一缝（AC-T4-1/2）。

契约（与 Stage0 P5 探针同结论，探针禁止 import 本件）：

- generate 签名与 smolagents 1.26.0 抽象方法逐字一致；
- 内部唯一出网调用是 ``ModelGateway.call("coding_debug", ...)``，业务码
  零供应商标识（无 URL/model_id/import openai|litellm，AST 白名单测试
  锁 agentic/ 全包）；
- CodeAgent 路径**永不**向网关透传 tools/tool_choice/response_format：
  tools_to_call_from/response_format 非 None 即编程错误 fail-fast；
- 锁内适配（Phase 2.5，shim 依赖②；
  probes/stage0/p5_offline_analysis.json shim_dependencies 第 2 条）：
  CodeAgent 每轮无条件传 stop_sequences（smolagents 1.26.0
  agents.py:1650-1654：恒含 ["Observation:","Calling tools:"]；closing_tag
  仅当不被 opening_tag 包含时才追加——markdown 形态开栏 ```python 已含闭栏
  ```，故实际序列就是这两个），而 ModelGateway.call 无此参数——
  产品形为「**不转发网关 + 本地实现等价 stop 语义**」：拿到原文后按任一
  序列最早出现位置 text[:idx] 截断（哨兵不回显，同供应侧 stop），raw 与
  raw_response_ref 保留原文、ChatMessage.content 为截断文。不能只丢参：
  模型若在哨兵后续写伪造 Observation/工具块，不截断会被 parse_code_blobs
  当真解析（安全面，非便利面）。探针的「非 None 即拒」是探针最小形，
  产品不可照搬，否则环无法运转；
- 零图像：text-only flatten，任何 image block 立即拒绝（视觉唯一入口
  是 ModelGateway.call_visual，本包 grep 守卫锁死，AC-T4-2）；
- tool-call/tool-response 角色是 CodeAgent 文本协议的正常形态，按
  smolagents tool_role_conversions 映射为 assistant/user（不是原生
  tool_use 往返；原生角色名 tool/function 才拒绝）；
- 供应商错（凭据/暂停/null stage/4xx/硬超时/重试耗尽）抛
  SupplierModelError，由 loop 优雅终局；空回答（EmptyResponseError）
  是模型可自修错：返回空 content，让 CodeAgent 解析失败反馈进下一轮
  （仍占该轮 seq/账行，gateway 侧已记 ok=false，不掩盖）。

接触面事实（smolagents 1.26.0 源码核实，教程不可全信）：
- 与框架的全部接触收敛在五个文档化点：``Model.generate``（本件）/
  ``CodeAgent.run`` / ``CodeAgent.step``（_step_stream 调用本 generate）/
  ``@tool``（loop.py 的 Tool 子类）/ ``LocalPythonExecutor``
  （executor.py 硬化子类）；版本精确 pin 1.26.0，不追升级。
- ``parse_code_blobs`` 分支序：①``<code>`` 标签 → ②```python/```py
  markdown 围栏 → ③**ast.parse 裸 blob**——无围栏的合法 Python 文本乃至
  裸 JSON 都会被当代码步骤解析。故产品必须 code_block_tags="markdown"
  显式约束输出形态，且本地 stop 截断必须先于解析生效（否则哨兵后的伪造
  Observation/围栏会被当真，这也是上面截断语义属安全面的原因）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from smolagents.models import ChatMessage, MessageRole, Model, TokenUsage

from orchestrator.gateway.router import (
    EmptyResponseError,
    GatewayError,
    GatewayPausedError,
    HardDeadlineTimeout,
    MissingGatewayCredentialError,
    NonRetryableError,
    PauseStateCorruptError,
    StageUnresolvedError,
)

from .caps import CapExceeded, BudgetTracker
from .forensics import (
    CACHE_VERSION,
    agentic_cache_fingerprint,
    canonical_json,
    to_disk_key,
)

STAGE = "coding_debug"
# 与 agent_runner/runner.py 旧路径同值（不 import 旧引擎，AC-T4-11 旧路径
# 零变化；两处各自常量，数值锁在 golden 回归与本件测试里）。
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.6
DEFAULT_THINKING = {"type": "disabled"}
DEFAULT_TIMEOUT_S = 120


class AgenticModelError(RuntimeError):
    """agentic 模型缝异常基类。"""


class SupplierModelError(AgenticModelError):
    """供应侧/配置侧错误，模型无法自修：loop 优雅终局出 FAILURE 件。"""


def is_supplier_error(exc: BaseException) -> bool:
    return isinstance(exc, (
        NonRetryableError,
        StageUnresolvedError,
        GatewayPausedError,
        PauseStateCorruptError,
        MissingGatewayCredentialError,
        HardDeadlineTimeout,
        GatewayError,          # 5xx/429 重试耗尽等基类兜底
    ))


def flatten_text_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """smolagents ChatMessage/dict → 网关 content-block 消息（纯文本）。"""
    role_map = {
        MessageRole.USER: "user",
        MessageRole.ASSISTANT: "assistant",
        MessageRole.SYSTEM: "system",
        MessageRole.TOOL_CALL: "assistant",   # "Calling tools: ..."
        MessageRole.TOOL_RESPONSE: "user",    # "Observation: ..."
        "user": "user", "assistant": "assistant", "system": "system",
    }
    out: list[dict[str, Any]] = []
    for m in messages:
        role_obj = getattr(m, "role", None)
        content = getattr(m, "content", None)
        if role_obj is None and isinstance(m, dict):
            role_obj, content = m.get("role"), m.get("content")
        role_value = role_obj.value if hasattr(role_obj, "value") else role_obj
        if role_value in ("tool", "function", "tool_calls"):
            raise ValueError(
                f"CodeAgent 文本路径不应出现原生工具往返角色 {role_value!r}"
                "（那是 ToolCallingAgent 形态，本缝永不透传 tools）")
        if role_value not in role_map:
            raise ValueError(f"不支持的消息角色：{role_value!r}")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    # MINOR 4（2026-10-30 评审）：str() 静默会把畸形供应侧
                    # 响应（如 list[str]/str block）粉饰成文本喂给
                    # parse_code_blobs（裸 blob 即代码分支）；fail-closed。
                    raise ValueError(
                        f"content block 必须是 dict，收到 {type(block).__name__}")
                btype = block.get("type")
                if btype == "image":
                    # AC-T4-2 硬规则 1：coding 阶段零 image block。
                    raise ValueError("硬规则 1：agentic 内环禁止 image block；"
                                     "视觉唯一入口 call_visual")
                if btype in ("text", None):
                    parts.append(str(block.get("text", "")))
                else:
                    raise ValueError(f"不支持的 content block 类型：{btype}")
            text = "\n".join(p for p in parts if p)
        elif content is None:
            if getattr(m, "tool_calls", None) or (
                    isinstance(m, dict) and m.get("tool_calls")):
                raise ValueError("CodeAgent 文本路径不应携带原生 tool_calls")
            text = ""
        else:
            text = str(content)
        out.append({"role": role_map[role_value],
                    "content": [{"type": "text", "text": text}]})
    return out


# Recorder 回调形状：(round_idx, raw_text, usage, cache_hit, latency_ms,
# disk_key|None, stopped_by_local, stop_hit|None, seq:int|None) ->
# evidence_ref 字符串（loop 接 TraceStore；测试可注入）。raw_text 永远是
# 网关/缓存原文。M5：实呼轮传该轮真实 ledger seq（含 ok=false），缓存命中轮 None。
Recorder = Callable[
    [int, str, "tuple[int,int] | None", bool, int, "str | None", bool,
     "str | None", "int | None"], "str | None"]


def local_stop_truncate(text: str, stop_sequences: Any
                        ) -> tuple[str, "str | None"]:
    """本地等价 stop 语义：任一哨兵最早出现处截断，哨兵不回显。

    smolagents CodeAgent 每轮恒传 ["Observation:","Calling tools:",
    closing_tag]；网关无 stop 参数（不下发，见模块 docstring），故在此对
    返回原文做截断：取所有序列在文中的最早下标 ``text[:idx]``。模型若在
    哨兵后续写伪造 Observation/工具块，未截断会被 parse_code_blobs 当真。
    无哨兵/无命中时原文返回（hit=None）；空/非字符串序列项忽略。
    """
    if not text or not stop_sequences:
        return text, None
    best_idx: "int | None" = None
    best_seq: "str | None" = None
    for seq in stop_sequences:
        if not isinstance(seq, str) or not seq:
            continue
        idx = text.find(seq)
        if idx >= 0 and (best_idx is None or idx < best_idx):
            best_idx, best_seq = idx, seq
    if best_idx is None:
        return text, None
    return text[:best_idx], best_seq


def build_gateway_model(gateway: Any, *, stage: str = STAGE,
                        max_tokens: int = DEFAULT_MAX_TOKENS,
                        temperature: float = DEFAULT_TEMPERATURE,
                        thinking: dict | None = DEFAULT_THINKING,
                        timeout_s: int = DEFAULT_TIMEOUT_S,
                        budget: "BudgetTracker | None" = None,
                        call_cache: Any = None,
                        recorder: "Recorder | None" = None,
                        seq_provider: "Callable[[], int | None] | None" = None
                        ) -> "GatewayModel":
    """构造 coding_debug stage 的文本模型缝（**工厂**，C2 评审返修）。

    C2（2026-10-30 CRITICAL）：网关句柄、预算、缓存、recorder、seq 读取器、
    系统提示/任务/前缀、采样参数等**敏感活对象全部只存在于本工厂的局部
    细胞**里，返回的 :class:`Model` 实例上无同名属性、无槽位、也无下划线
    或名字改写版本——即使模型代码经别的面拿到模型对象，``model.gateway``
    /``model.budget``/``model._system_prompt`` 等读取全部 AttributeError，
    无从直接 ``gateway.call("macro_ideation", ...)`` 逃付费（H）、置空预算
    （I）、读提示（J）或改写 stage 路由（G）。实例唯一对外的面是 smolagents
    合同的 generate、引擎用的 configure/note_step/restore_round、只读
    round_idx/stage，以及只读计数器（calls/cache_hits/empty_responses/
    supplier_errors，生产不读它做判定）。

    generate/note_step/M5 seq 记账语义与旧类逐字一致（缓存命中轮 seq=null
    且不调 seq_provider；实呼轮在网关返回后只读一次 seq，空回答同路径；
    bool 不是 int）。
    """
    # --- 敏感配置：仅工厂局部细胞，方法闭包捕获，不落实例 ---
    system_prompt = ""
    task_text = ""
    prefix = "0" * 64
    round_no = 0
    calls: list[dict[str, Any]] = []
    counters = {"cache_hits": 0, "supplier_errors": 0, "empty_responses": 0}

    def fingerprint() -> tuple[str, str]:
        model_id = gateway.model_id(stage)
        fp = agentic_cache_fingerprint(
            stage=stage, model=model_id,
            system_prompt=system_prompt, task=task_text,
            round_idx=round_no, prefix_hex=prefix,
            max_tokens=max_tokens, temperature=temperature,
            thinking=thinking)
        return to_disk_key(fp), fp

    def read_round_seq() -> "int | None":
        """M5：实呼后只读一次本轮 ledger seq；无 provider/无 billing → None。"""
        if seq_provider is None:
            return None
        seq = seq_provider()
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
            raise ValueError(
                f"seq_provider 必须返回 int|None，收到 {type(seq).__name__}")
        return seq

    def call_gateway(payload: list[dict[str, Any]]
                     ) -> tuple[str, "tuple[int,int] | None", bool]:
        """返回 (text, usage, empty)；empty=True 是可自修空回答。"""
        try:
            text, usage = gateway.call(
                stage, payload,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout_s,
                thinking=thinking)
        except EmptyResponseError:
            # 模型可自修错：gateway 已记 ok=false 账行；回空 content 触发
            # CodeAgent 解析失败反馈，下一轮在轮帽内重试，不破坏环。
            counters["empty_responses"] += 1
            return "", None, True
        except CapExceeded:
            raise
        except Exception as exc:
            if is_supplier_error(exc):
                counters["supplier_errors"] += 1
                raise SupplierModelError(
                    f"供应侧错误（{type(exc).__name__}）：{exc}") from exc
            raise
        return text, usage, False

    # 状态写操作集中在工厂级函数里（方法参数名 system_prompt 与工厂局部
    # 同名，在方法内直接 nonlocal 会与局部参数冲突，故事先在此绑定细胞）。
    def configure_state(prompt: str, task_value: str) -> None:
        nonlocal system_prompt, task_text, prefix, round_no
        system_prompt = prompt
        task_text = task_value
        prefix = "0" * 64
        round_no = 0

    def restore_round_no(value: int) -> None:
        nonlocal round_no
        if type(value) is not int or value < 0:
            raise ValueError("round_idx 必须是非负整数")
        round_no = value

    def advance_round() -> int:
        nonlocal round_no
        round_no += 1
        return round_no

    class GatewayModel(Model):
        """smolagents Model 文本缝；敏感依赖全在工厂闭包，实例零敏感属性。"""

        def __init__(self) -> None:
            super().__init__(flatten_messages_as_text=True, model_id=None)

        @property
        def stage(self) -> str:
            # 只读：工厂局部 stage 的视图，实例无 back-reference 可改写。
            return stage

        @property
        def round_idx(self) -> int:
            return round_no

        @property
        def calls(self) -> list[dict[str, Any]]:
            return calls

        @property
        def cache_hits(self) -> int:
            return counters["cache_hits"]

        @property
        def empty_responses(self) -> int:
            return counters["empty_responses"]

        @property
        def supplier_errors(self) -> int:
            return counters["supplier_errors"]

        # ---- 轨迹/指纹配置（AC-T4-9 版本化指纹） ----------------------

        def configure(self, *, system_prompt: str, task: str) -> None:
            configure_state(system_prompt, task)

        def restore_round(self, round_idx: int) -> None:
            """kill-9 续写：前缀链由 note_step 重放恢复后对齐轮计数。

            下一次 generate 自增为 round_idx+1，与原进程下一轮一致
            （AC-T4-13）。
            """
            restore_round_no(round_idx)

        def note_step(self, *, step: int, tool: str, args: dict[str, Any],
                      normalized_obs_sha256: str) -> None:
            """工具步结束后由 loop 喂入：推进轨迹前缀链（入指纹）。"""
            from .forensics import advance_prefix
            nonlocal prefix
            prefix = advance_prefix(
                prefix, step=step, tool=tool,
                args_canonical=canonical_json(args),
                norm_obs_sha256=normalized_obs_sha256)

        # ---- smolagents 唯一抽象方法 -----------------------------------

        def generate(self, messages, stop_sequences=None,
                     response_format=None, tools_to_call_from=None,
                     **kwargs) -> ChatMessage:
            # shim 缺口项：非 None 即调用方走错形态（ToolCallingAgent/结构化）。
            if tools_to_call_from is not None:
                raise ValueError(
                    "CodeAgent 路径永不向网关透传 tools（AC-T4-1）；"
                    "tools_to_call_from 非 None 是 ToolCallingAgent 形态")
            if response_format is not None:
                raise ValueError(
                    "shim 缺口：ModelGateway.call 不支持 response_format/JSON mode")
            if kwargs.get("tools") is not None or \
                    kwargs.get("tool_choice") is not None:
                raise ValueError(
                    "CodeAgent 路径不得透传 tools/tool_choice kwarg")
            # 锁内适配（Phase 2.5，shim 依赖②）：stop_sequences 不转发网关；
            # 本地等价截断在拿到原文后执行（见 local_stop_truncate）。
            # 未知 kwarg fail-fast（smolagents 1.26.0 CodeAgent 只传
            # stop_sequences）。
            if kwargs:
                raise ValueError(
                    f"GatewayModel 收到未知 kwarg：{sorted(kwargs)}")

            payload = flatten_text_messages(messages)
            if budget is not None:
                budget.add_round()
            round_idx = advance_round()

            disk_key, fp = fingerprint()
            cached = None
            if call_cache is not None:
                cached = call_cache.get(disk_key, fp)

            t0 = time.monotonic()
            if cached is not None:
                text, usage = cached["raw"], tuple(cached["usage"])
                cache_hit = True
                counters["cache_hits"] += 1
                empty = False
                # M5：缓存命中轮不占账，模型轮行 seq 显式 null（工具行仍挂
                # 最近实呼 seq，由 recorder 侧 RoundState 保持）。
                round_seq = None
            else:
                text, usage, empty = call_gateway(payload)
                cache_hit = False
                # M5：实呼返回后只读一次 seq——成功与 EmptyResponseError 两路
                # 都到达这里（空回答网关侧已记 ok=false，seq 同样被账套占用）。
                round_seq = read_round_seq()
                if not empty and call_cache is not None:
                    # 成功才写缓存（与 CallReplay 契约一致）；指纹含规范化
                    # 轨迹，重放同输入同历史必命中、时间戳噪声已在 obs 规范
                    # 化剥除。缓存刻意存**截断前原文**——截断是
                    # (raw, stop_sequences) 的纯函数，而 CodeAgent 每轮哨兵集
                    # 恒为那两个固定序列（smolagents 1.26.0 合同），二次进程/
                    # kill-9 续写读出后对同一原文跑同一 local_stop_truncate，
                    # 喂 parse_code_blobs 的 content 逐字节一致；存截断文会丢
                    # 哨兵命中证据且与哨兵集版本耦合。
                    model_id = gateway.model_id(stage)
                    call_cache.put(
                        disk_key, kind=CACHE_VERSION, model=model_id, raw=text,
                        usage=(usage[0] or 0, usage[1] or 0), fingerprint=fp)
            latency_ms = int((time.monotonic() - t0) * 1000)

            tin, tout = (usage or (None, None))
            if not cache_hit and budget is not None and not empty:
                budget.add_usage(tin or 0, tout or 0)

            # 本地 stop 截断（实呼与缓存命中汇合后唯一执行一次，空回答不截）：
            # ChatMessage.content 是截断文、raw/raw_response_ref 留原文。
            content, stop_hit = local_stop_truncate(text, stop_sequences)
            stopped_local = stop_hit is not None

            ref = None
            if recorder is not None:
                ref = recorder(round_idx, text, usage, cache_hit, latency_ms,
                               disk_key, stopped_local, stop_hit, round_seq)
            calls.append({
                "round": round_idx, "stage": stage,
                "seq": round_seq,
                "payload": payload, "cache_hit": cache_hit,
                "usage": list(usage) if usage else None,
                "empty": empty, "disk_key": disk_key if cache_hit else None,
                "evidence_ref": ref,
                "stopped_by_local": stopped_local,
                "stop_sequence_hit": stop_hit,
            })
            token_usage = None
            if isinstance(tin, int) and isinstance(tout, int):
                token_usage = TokenUsage(input_tokens=tin, output_tokens=tout)
            return ChatMessage(
                role=MessageRole.ASSISTANT, content=content,
                tool_calls=None,   # 文本协议：永不产出原生 tool_calls。
                raw={"stage": stage, "evidence_ref": ref,
                     "cache_hit": cache_hit, "empty_response": empty,
                     "stopped_by_local": stopped_local,
                     "stop_sequence_hit": stop_hit,
                     "fingerprint": fp},
                token_usage=token_usage)

    return GatewayModel()
