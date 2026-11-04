"""三门预算（AC-T4-10）：工具步数帽 / token 帽 / coding 轮帽。

到帽语义统一为「优雅终局出 FAILURE 件」：不向环外抛裸异常，而是由
executor 把 ``CapExceeded``（BaseException，模型代码的 except Exception
接不住）映射成终局 CodeOutput，loop engine 读到 kind=cap_exceeded 后写
FAILURE artifact。批次票与 task cap 仍走 Task 0 的 phase25 命名空间，
本件只做运行时计数与判定，不新建配额文件格式。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 终局 kind 常量（engine 据此分类；finish 不等于通过，见 terminate.py）。
KIND_FINISH = "finish"
KIND_FAIL = "fail"
KIND_STEP_CAP = "step_cap"
KIND_TOKEN_CAP = "token_cap"
KIND_ROUND_CAP = "round_cap"
# C4（2026-10-30 第五轮返修）：blob 抛出的非 Exception 基类异常在硬化执行器
# 边界 fail-closed 映射的引擎错误 kind（KIND_* 同族，常量单一源在本件）。
KIND_ENGINE_ERROR = "engine_error"
# Phase 2.6 Task3（F1）：追授轮帽耗尽（尾轮内仍未通过验证）与探索帽耗尽
# 仍无出生（入口脚本从未达标）。KIND_BIRTH_ABSENT **不是**新停机条件——
# 探索帽耗尽时的行为与旧版完全一致，只是 kind 更可诊断（Task 4 判决按
# kind 分类统计「信息墙 vs 合同墙」，§13.1.4 R4 解耦的机器基础）。
KIND_ROUND_TAIL = "round_tail"
KIND_BIRTH_ABSENT = "birth_absent"
FAILURE_KINDS = frozenset(
    {KIND_FAIL, KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP,
     KIND_ROUND_TAIL, KIND_BIRTH_ABSENT})


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
    """单 run 预算计数；0 表示沿用 RunSpec/默认帽，负数非法。

    ⚠️ 术语（Phase 2.7 D3-3）：本类字段名里的 ``round`` 指**付费网关轮**
    （一次模型调用 = 一次实呼/一次缓存命中），``step`` 指 smolagents 的
    ActionStep（在 smolagents 里同为一次模型调用，由 loop 的 ``max_steps``
    用法体现）。``tool_step_cap`` 是**工具执行次数**，不额外计价。
    """

    step_cap: int = 0          # 每 run 工具步数（0 = 不显式限，由轮帽兜底）
    token_cap: int = 0         # 每 run 累计 token（in+out；0 = 不显式限）
    round_cap: int = 5         # coding 模型轮帽（RunSpec.max_coding_rounds 同源）
    steps_used: int = 0
    tokens_used: int = 0
    rounds_used: int = 0
    # Phase 2.7 Task3（D3-3）：工具执行次数帽（0 = 不限；不额外计价）。
    tool_step_cap: int = 0
    # Phase 2.6 Task3（F1）：追授轮数（0 = 旧行为，不追授）；tail_granted
    # 只在出生校验通过后置真；born_at_round 为出生轮号（幂等标记用）。
    verify_repair_tail_rounds: int = 0
    tail_granted: bool = False
    born_at_round: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("step_cap", "token_cap", "round_cap",
                     "verify_repair_tail_rounds", "tool_step_cap"):
            value = getattr(self, name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.round_cap == 0:
            raise ValueError("round_cap 必须 >=1（至少一轮模型调用）")
        if self.verify_repair_tail_rounds > 8:
            raise ValueError("verify_repair_tail_rounds 上限 8（防预算无界）")
        if type(self.tail_granted) is not bool:
            raise ValueError("tail_granted 必须是 bool")
        if self.born_at_round is not None and (
                type(self.born_at_round) is not int
                or isinstance(self.born_at_round, bool)
                or self.born_at_round < 0):
            raise ValueError("born_at_round 必须是非负整数或 None")

    @property
    def effective_round_cap(self) -> int:
        """当前生效轮帽（追授后 = round_cap + verify_repair_tail_rounds）。"""
        extra = self.verify_repair_tail_rounds if self.tail_granted else 0
        return self.round_cap + extra

    def clamp_to_ticket(self, remaining: int) -> int:
        """D3-4：把轮帽钳到**票额剩余**（弹性 ≠ 无限）。

        返回 ``min(effective_round_cap, remaining)``；``remaining < 0`` 视为
        0（票已用尽 ⇒ 一轮都不许跑）。**只回答**「本 run 最多几轮」，
        不改 ``add_round`` 的终局逻辑——到帽仍由 ``add_round`` 抛
        ``CapExceeded``，票额耗尽同样在那一处显形（kind=round_cap）。
        钳制结果是**调用方装配期**的决定（写进 budget.round_cap），
        本方法本身幂等、无副作用。
        """
        if type(remaining) is not int or isinstance(remaining, bool):
            raise ValueError("remaining 必须是整数")
        if remaining < 0:
            return 0
        return min(self.effective_round_cap, remaining)

    def mark_born(self, round_idx: int) -> bool:
        """记录出生轮号；幂等——首次返 True，已出生返 False。"""
        if type(round_idx) is not int or isinstance(round_idx, bool) \
                or round_idx < 0:
            raise ValueError("round_idx 必须是非负整数")
        if self.born_at_round is not None:
            return False
        self.born_at_round = round_idx
        return True

    def grant_verify_tail(self) -> bool:
        """追授验证-修复尾轮帽；幂等——首次返 True，已追授/未配置返 False。"""
        if self.verify_repair_tail_rounds <= 0 or self.tail_granted:
            return False
        self.tail_granted = True
        return True

    def check_tool_step(self) -> None:
        """工具调用前计数：下一次调用会超步帽即终局。"""
        if self.step_cap and self.steps_used >= self.step_cap:
            raise CapExceeded(
                KIND_STEP_CAP,
                f"工具步数帽耗尽：{self.steps_used}/{self.step_cap}")

    def note_tool_step(self) -> None:
        self.steps_used += 1

    def add_round(self) -> None:
        """每轮模型调用前：超轮帽即终局（CodeAgent max_steps 的同源帽）。

        Phase 2.6 Task3（F1）：未配置追授（``verify_repair_tail_rounds == 0``）
        时行为与旧版逐字一致（KIND_ROUND_CAP）；配置后把 kind 细分为
        ``birth_absent``（探索帽耗尽仍无出生）与 ``round_tail``（追授尾轮
        内仍未通过验证），**停机条件本身不变**（仍然是到帽即停）。
        """
        cap = self.effective_round_cap
        if self.rounds_used >= cap:
            if self.verify_repair_tail_rounds > 0:
                kind = KIND_ROUND_TAIL if self.tail_granted else KIND_BIRTH_ABSENT
            else:
                kind = KIND_ROUND_CAP
            raise CapExceeded(kind, f"coding 轮帽耗尽：{self.rounds_used}/{cap}")
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
