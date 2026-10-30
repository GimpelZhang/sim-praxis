"""finish/fail 控制原语（AC-T4-6）。

- 两者都**自终局**：在硬化执行器内抛 ``TerminateSignal``（BaseException，
  模型代码的 except Exception 接不住），HardenedLocalPythonExecutor 把它
  映射成 is_final_answer 的 CodeOutput，CodeAgent 据此收尾；
- **finish ≠ pass**：finish 只是内层 agent 声称做完，evaluate/Gate2 永远
  不从 finish 推断成功（图节点照常跑原始 evaluate；有测试钉死这点）；
- fail 是 agent 自检不可恢复（例如三轮修不好同一个锚点）时的诚实终局，
  出 FAILURE 件（kind=fail），不包装成异常穿环。
"""
from __future__ import annotations

from ..executor import TerminateSignal
from ..caps import KIND_FAIL, KIND_FINISH
from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext
from ..schema import MAX_ARG_CHARS, SchemaError, str_field, validate
from . import ToolResult

_FINISH_FIELDS = (
    str_field("answer", max_chars=MAX_ARG_CHARS),
    str_field("reason", required=False, default="", max_chars=4_000),
)
_FAIL_FIELDS = (
    str_field("reason", max_chars=4_000),
)


def _deny(exc: Exception) -> ToolResult:
    # 控制原语的 schema 错不终局：回观察让模型补全参数后重试。
    return ToolResult.denied_result(PolicyDecision.deny(
        f"schema 拒绝：{exc}"))


def finish(ctx: ToolContext, **kwargs) -> ToolResult:
    """声称完成；永不等于通过（evaluate/Gate2 独立判定）。"""
    try:
        args = validate(_FINISH_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(exc)
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("finish", args)
    if decision.decision == "deny":
        # 控制原语恒 allow（control_primitive 源）；此分支只在装配损坏时到。
        return ToolResult.denied_result(decision)
    raise TerminateSignal(KIND_FINISH, args["reason"], value=args["answer"])


def fail(ctx: ToolContext, **kwargs) -> ToolResult:
    """诚实终局：agent 自检不可恢复，出 FAILURE 件，不穿裸异常。"""
    try:
        args = validate(_FAIL_FIELDS, kwargs)
    except SchemaError as exc:
        return _deny(exc)
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("fail", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    raise TerminateSignal(KIND_FAIL, args["reason"])
