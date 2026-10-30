"""Task 5b：协议反馈包装子类（最小生成器包装缝；冻结规格 §3.3）。

为何是包装而不是复制方法体：smolagents 1.26.0 ``CodeAgent._step_stream``
在 parse 前已把入解析文本写入 ``memory_step.model_output``
（agents.py:1697，auto-close 补闭栏之后），parse 块唯一异常类型是
``AgentParsingError``（utils.py:104）。故子类只需 ``yield from super()``
并在该异常上：取原文 → 形态归因 → 以同一 ``(message, logger)`` 构造形状
重抛**新** AgentParsingError。成功路径（yield tool_call / ActionOutput /
final answer）逐字透传；parse 失败发生在首次 yield 之前，包装层不产生
任何重复工具副作用。

Golden 双锚在 tests/milestone25/test_task5b_protocol_hardening.py：
①安装环境 ``CodeAgent._step_stream`` 源 sha256 前缀 e027f0eaa32a8d94
（1.26.0；版本漂移硬规则 8 已 pin）；②行为锚：库存 CodeAgent 喂畸形
输出时抛 AgentParsingError 且 model_output 已就位（本包装缝依赖的两个
上游事实任一被改坏即红）。
"""
from __future__ import annotations

from typing import Any, Generator

from smolagents.agents import AgentParsingError, CodeAgent
from smolagents.memory import ActionStep

from .protocol import build_parse_feedback


class ProtocolHardenedCodeAgent(CodeAgent):
    """只精确化 parse 失败观察；记账、执行、终局语义与库存 CodeAgent 相同。"""

    def _step_stream(
        self, memory_step: ActionStep
    ) -> Generator[Any, None, None]:
        try:
            yield from super()._step_stream(memory_step)
        except AgentParsingError as exc:
            # model_output 在 parse 前已赋值（含 auto-close 补的闭栏），
            # 与 parse_code_blobs 的实际输入逐字一致。
            output_text = getattr(memory_step, "model_output", "") or ""
            message = build_parse_feedback(
                output_text, str(exc), self._protocol_error_streak())
            # 同构造形状 (message, logger)，from exc 保留原因链供 forensics。
            raise AgentParsingError(message, self.logger) from exc

    def _protocol_error_streak(self) -> int:
        """含本轮的连续 parse 失败计数（当前步尚未 append，从历史尾部数）。"""
        streak = 1
        for step in reversed(self.memory.steps):
            if isinstance(step, ActionStep) and isinstance(
                    step.error, AgentParsingError):
                streak += 1
            else:
                break
        return streak
