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
    """Phase 2.6 Task3 四项修复 + Phase 2.7 Task3 弹性内环的开关载体。

    全 False ＝ Phase 2.5 逐字节旧行为。

    Phase 2.6（旧四项，任一开启 ⇒ ``agentic-v3`` 纪元）：

    - verify_repair_tail: F1 验证-修复尾（入口脚本出生后追授轮帽）
    - verify_repair_tail_rounds: 追授轮数；提纲要求 >= 3，上限 8 防预算无界
    - run_expr: F2 内联探针（受限小程序，AST 白名单）
    - tolerant_patch: F3 容错补丁器（仅 unified_diff 通道的模糊回退）
    - scratchpad: F4 跨轮记忆文件（磁盘 + payload/指纹双注入）

    Phase 2.7（新五项，``env_spec``/``render_preview`` 任一开启 ⇒
    ``agentic-v4`` 纪元，见 ``forensics.cache_epoch_for``）：

    - env_spec: D3-2 环境卡（静态 + 探针 + 真值段）注入 system prompt
    - render_preview: D3-5/D3-8 预览探针工具（默认只回文本）
    - elastic_round_cap: D3-4 票内弹性轮帽；0 = 沿用 RunSpec，>0 时受票额钳制
    - privileged_state_inject: BR-3 第 5 项门控——真值位姿/空间关系**仅**
      模式 N 且预注册写明时才渲染；默认 False（模式 R 一律不给）
    - visual_critic: BR-1 视觉诊断——经既有 ``call_visual`` 通道（唯一视觉
      入口），回传**文本**，图不进内环（硬规则 1）
    """

    verify_repair_tail: bool = False
    verify_repair_tail_rounds: int = 3      # 提纲要求 >= 3
    run_expr: bool = False
    tolerant_patch: bool = False
    scratchpad: bool = False

    # --- Phase 2.7 Task 3 ---
    env_spec: bool = False
    render_preview: bool = False
    elastic_round_cap: int = 0
    privileged_state_inject: bool = False
    visual_critic: bool = False
    run_mode: str = "R"        # "R"（冻结墙配置）/ "N"；真值段只在 N 下可注入

    def __post_init__(self) -> None:
        for name in ("verify_repair_tail", "run_expr", "tolerant_patch",
                     "scratchpad", "env_spec", "render_preview",
                     "privileged_state_inject", "visual_critic"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 必须是 bool")
        rounds = self.verify_repair_tail_rounds
        if type(rounds) is not int or isinstance(rounds, bool):
            raise ValueError("verify_repair_tail_rounds 必须是整数")
        if self.verify_repair_tail and rounds < 3:
            raise ValueError("verify_repair_tail_rounds 必须 >= 3")
        if rounds > 8:
            raise ValueError("verify_repair_tail_rounds 上限 8（防预算无界）")
        elastic = self.elastic_round_cap
        if type(elastic) is not int or isinstance(elastic, bool):
            raise ValueError("elastic_round_cap 必须是整数")
        if elastic < 0:
            raise ValueError("elastic_round_cap 不得为负（0 = 沿用 RunSpec）")
        if self.privileged_state_inject and not self.env_spec:
            # 真值段住在 ENV_SPEC 里：没有环境卡就没有承载物，静默忽略等于
            # 让「已开启」的开关不生效（配置说谎），故装配期 fail-closed。
            raise ValueError(
                "privileged_state_inject=True 需要同时开启 env_spec（真值段在环境卡内）")
        if self.run_mode not in ("R", "N"):
            raise ValueError(f"run_mode 必须是 'R' 或 'N'：{self.run_mode!r}")
        if self.visual_critic and not self.render_preview:
            # 视觉诊断的输入是 render_quick_preview 落盘的 PNG，缺它无图可看。
            raise ValueError(
                "visual_critic=True 需要同时开启 render_preview（否则无图可诊断）")

    @property
    def any_enabled(self) -> bool:
        """旧四项任一开启 ⇒ agentic-v3 纪元（I3-3，不得半开共享 v2 键）。

        语义保持不变：Phase 2.7 的新开关不参与本属性，v4 判定走
        :func:`orchestrator.agentic.forensics.cache_epoch_for`。
        """
        return (self.verify_repair_tail or self.run_expr
                or self.tolerant_patch or self.scratchpad)

    @property
    def v4_enabled(self) -> bool:
        """新纪元开关：任一新开关开启 ⇒ agentic-v4（D3-7）。"""
        return self.env_spec or self.render_preview


ALL_OFF = AgenticConfig()
