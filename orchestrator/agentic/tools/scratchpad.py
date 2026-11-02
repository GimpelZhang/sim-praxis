"""F4：跨轮 scratchpad（整体覆盖写 + 写后回读校验）。

落点是 workdir 内的 ``.agentic_scratchpad.md``：每一轮由 loop **读一次**，
同一份文本同时注入①出站 payload 的首个 system 消息（``model.generate``
展平之前）与②缓存指纹（``agentic_cache_fingerprint`` 的
``scratchpad_sha256``/``scratchpad_chars``）。两面不同源就会「指纹变了、
请求没变」或反之，前者打散键空间、后者静默返回上一轮答案（D3-7）。

本工具**只负责写**：jail 与 Gate1 写前缀闸与 ``write_file`` 同款，写后
立刻从磁盘回读并与入参逐字节比对，不等即 ``AgenticToolError``（不信
``atomic_write_text`` 的返回值自述）。超长（> 4000 字符）直接拒绝，
**不静默截断输入**——截断发生在读取侧（外部编辑文件时可观察
``scratchpad_truncated``），写入侧截断会让模型以为写进去了。
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

from typing import Any

from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext, resolve_or_deny
from ..schema import Field, SchemaError, validate
from . import AgenticToolError, ToolResult

SCRATCHPAD_REL = ".agentic_scratchpad.md"
SCRATCHPAD_MAX_CHARS = 4000


def _check_content(value: Any) -> None:
    """允许空串（清空记忆是合法动作），但必须是 str 且不超上限。"""
    if not isinstance(value, str):
        raise SchemaError(f"必须是字符串，收到 {type(value).__name__}")
    if len(value) > SCRATCHPAD_MAX_CHARS:
        raise SchemaError(f"超长：{len(value)} > {SCRATCHPAD_MAX_CHARS} 字符")


_FIELDS = (Field("content", True, None, _check_content),)


def update_scratchpad(ctx: ToolContext, **kwargs) -> ToolResult:
    """整体覆盖写（不是追加）；超长即拒，写后回读逐字节校验。"""
    try:
        args = validate(_FIELDS, kwargs)
    except SchemaError as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"schema 拒绝：{exc}"))
    content: str = args["content"]
    target, reason = resolve_or_deny(ctx.jail, SCRATCHPAD_REL)
    if target is None:
        return ToolResult.denied_result(PolicyDecision.deny(reason))
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check(
            "update_scratchpad", args, resolved_path=target)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    ensure_private_dir(Path(target).parent)
    atomic_write_text(target, content, mode=0o600)
    # 写后回读：字节相等才算成功（防原子写静默截断/编码漂移）。
    readback = Path(target).read_bytes()
    if readback != content.encode("utf-8"):
        raise AgenticToolError(
            "update_scratchpad 回读校验失败：磁盘内容与入参不一致"
            f"（{len(readback)} B vs {len(content.encode('utf-8'))} B）")
    return ToolResult.allow_result(
        f"[update_scratchpad] 已覆盖写入 {SCRATCHPAD_REL}"
        f"（{len(content)} 字符，0600；下一轮生效）")
