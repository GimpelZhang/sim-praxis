"""Phase 2.7 Task 3（D3-1 / D3-9）：system prompt 的**唯一**组装入口与字节预算。

为什么要有单一入口：`loop.py` 原先直接 ``system_contract(extra=…) + _tool_docs(cfg)``
就地拼接，若别处再拼一次，机判只会测到其中一条路径（「测 A 跑 B」），
「≤12 KB 且含 ENV_SPEC」就变成不可证伪的说法。本模块把三件事收在一处：

1. **组装**：``system_contract``（静态契约）+ ``_tool_docs``（开关相关工具说明）
   + 工作记忆铁律 + ``[ENV_SPEC]`` 环境卡 + ``[SKILLS]`` 技能卡 + 附加约束，
   顺序与字节**确定性**（不含时间戳、不含随机量、不含任何历史轮内容）；
2. **不随历史增长**（D3-9，AIDE² 的真实机理）：输入只有 ``cfg`` 与三块
   轮次无关的文本，历史只进对话消息。故同一 cfg 第 1 轮与第 N 轮逐字节相同；
3. **预算自检**：超过软上限（且没有 ENV_SPEC 这一正当增量）或硬上限时
   **抛错，不截断**——截断会让「模型看到的环境卡」与「证据里的环境卡」
   不是同一份。

三个阈值都是本仓自定，**不是**文献数字（见 Plan §T3.3 锁内适配 1）：
``PROMPT_BASELINE_BYTES`` 是 2026-11-03 实测基线（system_contract 2,709 B
+ 全开 _tool_docs 348 B），``PROMPT_SOFT_BYTES`` 是「基线 + ENV_SPEC +
技能卡」的增量预算，``PROMPT_BUDGET_BYTES`` 是硬失败线。
"""
from __future__ import annotations

from .config import AgenticConfig

# 硬上限：超过即 ValueError（硬失败，不截断）。
PROMPT_BUDGET_BYTES = 12_000
# 2026-11-03 实测：system_contract() = 2,709 B，旧开关全开 _tool_docs() = 348 B。
PROMPT_BASELINE_BYTES = 3_057
# 含 ENV_SPEC + 技能块后的软上限（超了先删长列表，不得调大本常量）。
PROMPT_SOFT_BYTES = 6_000

# F4 主动面（Plan §T3.4-G / AC-T3-12①）：逐字一句，字节数实测后钉死；
# 断言写「= PROMPT_BASELINE_BYTES + SCRATCHPAD_RULE_BYTES」而不是抄一个数。
# 锁内适配（Phase 2.7 Task 3）：本行只在 ``cfg.scratchpad`` 为真（即
# ``update_scratchpad`` 工具真实注册）时才注入——开关关闭时该工具不存在，
# 写「必须调用 update_scratchpad」是悬空指令，且会破坏 ALL_OFF 逐字节回滚面。
SCRATCHPAD_RULE = (
    "\n【工作记忆铁律】当你发现关键 API 路径、类名或验证通过的配置时，"
    "必须立即调用 `update_scratchpad` 将其记录在案。"
    "未写入工作记忆的信息将在上下文截断时永久丢失。\n"
)
SCRATCHPAD_RULE_BYTES = len(SCRATCHPAD_RULE.encode("utf-8"))

# 段落标记：机判（AC-T3-1/AC-T3-9）靠它们定位「环境卡是否在场」。
ENV_SPEC_MARKER = "[ENV_SPEC]"
SKILLS_MARKER = "[SKILLS]"


def _block(marker: str, body: str) -> str:
    """把一段卡文本包成确定性段落（去首尾空行、段尾恰好一个换行）。"""
    text = body.strip("\n")
    if not text:
        return ""
    return f"\n{marker}\n{text}\n"


def build_system_prompt(*, cfg: AgenticConfig, env_spec_block: str = "",
                        skills_block: str = "", extra: str = "") -> str:
    """组装 system prompt 并做字节预算自检（唯一入口）。

    ``env_spec_block`` / ``skills_block`` 由调用方传入**已渲染**的确定性
    文本（``EnvSpec.render_block()`` / ``skills.registry.env_spec_skills_block()``）；
    本函数不认识它们的内容，只负责拼装与预算。

    失败语义（Plan §T3.9）：

    - ``> PROMPT_BUDGET_BYTES`` → ``ValueError``（不截断，调用方须先精简）；
    - ``> PROMPT_SOFT_BYTES`` **且无 env_spec_block** → ``ValueError``
      （防「无声膨胀」：软上限只在 ENV_SPEC 这一正当增量在场时放宽）。
    """
    if not isinstance(cfg, AgenticConfig):
        raise TypeError(f"cfg 必须是 AgenticConfig，收到 {type(cfg).__name__}")
    # 延迟导入：loop.py 在模块顶层 import 本模块，此处反向 import 会成环。
    from .loop import _tool_docs, system_contract

    parts = [system_contract(extra=extra), _tool_docs(cfg)]
    if cfg.scratchpad:
        parts.append(SCRATCHPAD_RULE)
    parts.append(_block(ENV_SPEC_MARKER, env_spec_block))
    parts.append(_block(SKILLS_MARKER, skills_block))
    prompt = "".join(parts)

    size = len(prompt.encode("utf-8"))
    if size > PROMPT_BUDGET_BYTES:
        raise ValueError(
            f"system prompt {size} B 超过硬上限 {PROMPT_BUDGET_BYTES} B："
            "先精简 ENV_SPEC 长列表（资产/技能只给前 N 个 + 计数），"
            "不得调大 PROMPT_BUDGET_BYTES")
    if size > PROMPT_SOFT_BYTES and not env_spec_block:
        raise ValueError(
            f"system prompt {size} B 超过软上限 {PROMPT_SOFT_BYTES} B 且无 "
            "ENV_SPEC 增量：疑为契约/工具说明无声膨胀，先定位来源")
    return prompt


def prompt_bytes(cfg: AgenticConfig, env_spec_block: str = "",
                 skills_block: str = "", extra: str = "") -> int:
    """便携口径：返回与 :func:`build_system_prompt` 相同的字节数（不吞异常）。"""
    return len(build_system_prompt(
        cfg=cfg, env_spec_block=env_spec_block,
        skills_block=skills_block, extra=extra).encode("utf-8"))
