"""Phase 2.6 Task3：四项工程修复的开关载体（默认全关 = Phase 2.5 逐字节旧行为）。

``ALL_OFF`` 是唯一回滚面：四个布尔开关全 False 时，prompt / 缓存指纹 /
trace 行 / 账行与 Phase 2.5 代码路径逐字节相同（AC-T3-5）。四个开关任一
开启即整体进入 ``agentic-v3`` 缓存纪元（见 forensics.cache_epoch），不存在
「半开组合共享 v2 键」。

本对象只在 ``loop.py`` 装配期读一次，不进入任何 prompt 渲染。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgenticConfig:
    """Phase 2.6 Task3 四项修复的开关。全 False ＝ Phase 2.5 逐字节旧行为。

    - verify_repair_tail: F1 验证-修复尾（入口脚本出生后追授轮帽）
    - verify_repair_tail_rounds: 追授轮数；提纲要求 >= 3，上限 8 防预算无界
    - run_expr: F2 内联探针（受限小程序，AST 白名单）
    - tolerant_patch: F3 容错补丁器（仅 unified_diff 通道的模糊回退）
    - scratchpad: F4 跨轮记忆文件（磁盘 + payload/指纹双注入）
    """

    verify_repair_tail: bool = False
    verify_repair_tail_rounds: int = 3      # 提纲要求 >= 3
    run_expr: bool = False
    tolerant_patch: bool = False
    scratchpad: bool = False

    def __post_init__(self) -> None:
        for name in ("verify_repair_tail", "run_expr",
                     "tolerant_patch", "scratchpad"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 必须是 bool")
        rounds = self.verify_repair_tail_rounds
        if type(rounds) is not int or isinstance(rounds, bool):
            raise ValueError("verify_repair_tail_rounds 必须是整数")
        if self.verify_repair_tail and rounds < 3:
            raise ValueError("verify_repair_tail_rounds 必须 >= 3")
        if rounds > 8:
            raise ValueError("verify_repair_tail_rounds 上限 8（防预算无界）")

    @property
    def any_enabled(self) -> bool:
        """任一开关开启 ⇒ agentic-v3 纪元（I3-3，不得半开共享 v2 键）。"""
        return (self.verify_repair_tail or self.run_expr
                or self.tolerant_patch or self.scratchpad)


ALL_OFF = AgenticConfig()
