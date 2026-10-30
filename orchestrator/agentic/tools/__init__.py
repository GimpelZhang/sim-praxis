"""六组工具 + 两个控制原语（AC-T4-3..7）。

每组成两层：

- 本模块（tools/*.py）提供**工具层实现**（plain 函数/类，可不经
  smolagents 直接单测）：内部固定走 jail → schema → policy 强制链；
- loop.py 把实现包成 smolagents ``Tool`` 并在执行器注入时再包一层
  trace/预算 forward 闸（第二重）。

工具层返回 ``ToolResult``，不向环外抛恶意载荷异常：policy/jail/schema
拒绝一律是 deny 观察（exit 2 语义），环继续、副作用零发生。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..policy import PolicyDecision, deny_observation


@dataclass
class ToolResult:
    output: str                       # 模型可见观察（必要时已截断）
    full: str                         # 全量观察文本（落 trace 全量存储）
    decision: PolicyDecision
    denied: bool = False

    @staticmethod
    def denied_result(decision: PolicyDecision) -> "ToolResult":
        text = deny_observation(decision)
        return ToolResult(output=text, full=text, decision=decision,
                          denied=True)

    @staticmethod
    def allow_result(output: str, *, full: "str | None" = None
                     ) -> "ToolResult":
        decision = PolicyDecision.allow()
        return ToolResult(output=output, full=full if full is not None
                          else output, decision=decision)
