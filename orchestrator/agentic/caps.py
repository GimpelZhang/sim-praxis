"""三门预算（AC-T4-10）：工具步数帽 / token 帽 / coding 轮帽。

到帽语义统一为「优雅终局出 FAILURE 件」：不向环外抛裸异常，而是由
executor 把 ``CapExceeded``（BaseException，模型代码的 except Exception
接不住）映射成终局 CodeOutput，loop engine 读到 kind=cap_exceeded 后写
FAILURE artifact。批次票与 task cap 仍走 Task 0 的 phase25 命名空间，
本件只做运行时计数与判定，不新建配额文件格式。
"""
from __future__ import annotations

from dataclasses import dataclass

# 终局 kind 常量（engine 据此分类；finish 不等于通过，见 terminate.py）。
KIND_FINISH = "finish"
KIND_FAIL = "fail"
KIND_STEP_CAP = "step_cap"
KIND_TOKEN_CAP = "token_cap"
KIND_ROUND_CAP = "round_cap"
# C4（2026-10-30 第五轮返修）：blob 抛出的非 Exception 基类异常在硬化执行器
# 边界 fail-closed 映射的引擎错误 kind（KIND_* 同族，常量单一源在本件）。
KIND_ENGINE_ERROR = "engine_error"
FAILURE_KINDS = frozenset(
    {KIND_FAIL, KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP})


class CapExceeded(BaseException):
    """工具/模型层到帽信号（继承 BaseException：同 smolagents FinalAnswerException，

    防止模型生成代码里的 ``except Exception`` 把终局信号吞掉）。
    """

    def __init__(self, kind: str, reason: str):
        self.kind = kind
        self.reason = reason
        super().__init__(f"{kind}: {reason}")


@dataclass
class BudgetTracker:
    """单 run 预算计数；0 表示沿用 RunSpec/默认帽，负数非法。"""

    step_cap: int = 0          # 每 run 工具步数（0 = 不显式限，由轮帽兜底）
    token_cap: int = 0         # 每 run 累计 token（in+out；0 = 不显式限）
    round_cap: int = 5         # coding 模型轮帽（RunSpec.max_coding_rounds 同源）
    steps_used: int = 0
    tokens_used: int = 0
    rounds_used: int = 0

    def __post_init__(self) -> None:
        for name in ("step_cap", "token_cap", "round_cap"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.round_cap == 0:
            raise ValueError("round_cap 必须 >=1（至少一轮模型调用）")

    def check_tool_step(self) -> None:
        """工具调用前计数：下一次调用会超步帽即终局。"""
        if self.step_cap and self.steps_used >= self.step_cap:
            raise CapExceeded(
                KIND_STEP_CAP,
                f"工具步数帽耗尽：{self.steps_used}/{self.step_cap}")

    def note_tool_step(self) -> None:
        self.steps_used += 1

    def add_round(self) -> None:
        """每轮模型调用前：超轮帽即终局（CodeAgent max_steps 的同源帽）。"""
        if self.rounds_used >= self.round_cap:
            raise CapExceeded(
                KIND_ROUND_CAP,
                f"coding 轮帽耗尽：{self.rounds_used}/{self.round_cap}")
        self.rounds_used += 1

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        if type(input_tokens) is not int or type(output_tokens) is not int:
            # None 由调用方先过滤；布尔同理拒绝（与 callcache 同严口径）。
            raise ValueError("usage token 必须是 int")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("usage token 不允许负数")
        if self.token_cap:
            projected = self.tokens_used + input_tokens + output_tokens
            if projected > self.token_cap:
                raise CapExceeded(
                    KIND_TOKEN_CAP,
                    f"token 帽将被突破：{projected}/{self.token_cap}")
        self.tokens_used += input_tokens + output_tokens
