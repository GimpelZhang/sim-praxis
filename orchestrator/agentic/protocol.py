"""Task 5b：纯文本协议形态归因 + parse 失败反馈精确化（0 付费产品件）。

冻结规格：``scripts/phase25/walls/task5b_spec.md``（攢批115 ``af2df49``
预注册，§3.1/§3.3 实现缝）。Task 5 证据：smolagents code_agent.yaml 规则 1
强制 Thought+围栏成对，旧契约反向禁止围栏外文本，模型在冲突下连续输出
Thought-only 散文（36% 协议空轮，各占付费轮/轮帽），框架英文反馈
（``Make sure to provide correct code blobs.`` + ``Thoughts: Your
thoughts`` 示例）收敛慢。

本件只做两件**形状层面**的事，不含任何任务/墙/文件名信息：

- :func:`classify_format_violation`：以框架 ``parse_code_blobs`` 为唯一
  有效性裁决器（规格 §3.1 注），解析失败后仅对原文做形态归因；
- :func:`build_parse_feedback`：保留框架英文反馈原文（证据连续），追加
  中文精确块（违例标签 + 合法形状 + 连续空转计数 + 记账事实）。

记账语义零变化：parse 失败轮已在 model.generate 内完成实呼、账行与
budget.add_round（model.py:367-369），本反馈不产生任何免费重试。
"""
from __future__ import annotations

import re
from typing import Any

from smolagents.utils import parse_code_blobs

# 产品 CodeAgent 配置逐字（loop.py build_agent: code_block_tags="markdown"
# → smolagents agents.py:1561 元组）。分类器必须与运行件同参数，禁双源。
PRODUCT_TAGS: tuple[str, str] = ("```python", "```")

LABEL_EMPTY = "empty"                  # 截断后空串（含 EmptyResponseError）
LABEL_NO_FENCE = "no_code_fence"       # 无 python/py 开栏（散文/裸```/补闭栏）
LABEL_UNCLOSED = "fence_unclosed"      # 有 python 开栏、闭栏缺失（纯函数直调）
LABEL_EMPTY_BLOCK = "empty_code_block"  # 围栏成对但块内只有空白（纯函数直调）
LABEL_UNKNOWN = "unknown"              # 解析失败但形态归因不进上述类

# 实现期发现（规格 §3.3 补记）：smolagents 在解析前对不以 ``` 结尾的输出
# 自动补一个结尾 ```（agents.py:1693-1695），于是「Thought 散文」与「残缺
# 裸围栏」在 parse 失败后的文本上不可区分（都变成末尾单 ```、无开栏），
# 且二者的纠正动作完全相同（给出完整闭合围栏）——合并为单一 no_code_fence
# 标签，文案点破「自动补的闭栏不能替代开栏」。
_LABEL_CN: dict[str, str] = {
    LABEL_EMPTY: "空回答（0 字符，没有任何围栏或代码）",
    LABEL_NO_FENCE: (
        "没有可执行的 ```python 代码围栏（只有 Thought 散文，或仅有残缺的 "
        "``` 标记；框架自动补的结尾 ``` 不能替代开栏）"),
    LABEL_UNCLOSED: "代码围栏未闭合（有 ```python 开栏、缺结尾 ```）",
    LABEL_EMPTY_BLOCK: "围栏内没有任何代码",
    LABEL_UNKNOWN: "输出未能解析为可执行代码且形态未归类",
}

_OPEN_FENCE_RE = re.compile(r"```(?:python|py)\b[^\n]*\n?", re.IGNORECASE)
_ANY_FENCE = "```"

_FEEDBACK_TEMPLATE = """
[SimPraxis 协议精确提示]
上一轮输出形态：{label_cn}——本轮未执行任何工具。
记账事实：该轮仍然计 1 次付费调用并占用 1 轮轮帽；没有任何格式重试是免费的。
每轮合法输出恰好两部分，顺序固定：
Thought: <一句话说明本轮动作>
```python
<调用工具的 Python 代码>
```
这是连续第 {streak} 次协议空转：不要只写 Thought 散文或残缺围栏，
Thought 一行之后必须立即给出**闭合的** python 围栏；终局轮在围栏内
调用 finish(...) 或 fail(...)，保持同一形状。""".strip()


def classify_format_violation(text: str) -> dict[str, Any] | None:
    """对单轮模型输出做形态归因。

    合法（框架 ``parse_code_blobs`` 成功，含无围栏但本身是合法 Python 的
    ast 裸 blob 分支）返回 ``None``；解析失败返回 ``{"label",
    "label_cn", "has_fence"}``。空串归 :data:`LABEL_EMPTY`。

    注意：smolagents 在解析前会对不以闭栏结尾的输出**自动补闭栏**
    （agents.py:1693-1695），补后能解析的输出不会走到本归因；调用方
    （agent_code.ProtocolHardenedCodeAgent）传入的是
    ``memory_step.model_output``——即自动补栏后、框架实际入解析的文本，
    与解析器输入严格一致。
    """
    if not text or not text.strip():
        return {"label": LABEL_EMPTY,
                "label_cn": _LABEL_CN[LABEL_EMPTY], "has_fence": False}
    try:
        parse_code_blobs(text, PRODUCT_TAGS)
        return None
    except Exception:
        # 唯一裁决器判失败；以下只做形态归因，不二次裁决可执行性。
        pass

    stripped = text.strip()
    m = _OPEN_FENCE_RE.search(stripped)
    if m is not None:
        remainder = stripped[m.end():]
        if _ANY_FENCE not in remainder:
            return {"label": LABEL_UNCLOSED,
                    "label_cn": _LABEL_CN[LABEL_UNCLOSED], "has_fence": True}
        close_idx = remainder.find(_ANY_FENCE)
        body = remainder[:close_idx]
        if not body.strip():
            return {"label": LABEL_EMPTY_BLOCK,
                    "label_cn": _LABEL_CN[LABEL_EMPTY_BLOCK],
                    "has_fence": True}
        # 成对且非空却仍解析失败：不归因为协议空转（可能是多栏等边界
        # 形态），交 unknown——反馈照样要求单一闭合围栏，不猜原因。
        return {"label": LABEL_UNKNOWN,
                "label_cn": _LABEL_CN[LABEL_UNKNOWN], "has_fence": True}
    # 无 python/py 开栏：散文、裸 ```、框架补的结尾 ``` 合并归因
    # （见模块 docstring/标签注释的不可区分性论证）。
    return {"label": LABEL_NO_FENCE,
            "label_cn": _LABEL_CN[LABEL_NO_FENCE],
            "has_fence": _ANY_FENCE in stripped}


def build_parse_feedback(text: str, framework_message: str,
                         streak: int) -> str:
    """组装替换给 AgentParsingError 的精确反馈（纯函数，无任务信息）。

    - ``framework_message``：框架原异常 ``str(exc)``，逐字保留在最前
      （含其 ValueError 对原文的回显，证据连续不断代）；
    - ``streak``：含本轮在内的连续 parse 失败次数（调用方从
      memory.steps 尾部计数）；非正整数 fail-fast。
    输出只依赖这三个入参：不同任务/墙下同输入逐字节相同（契约测试锁定，
    防任务线索从反馈面泄漏）。
    """
    if not isinstance(streak, int) or isinstance(streak, bool) or streak < 1:
        raise ValueError(f"streak 必须是 ≥1 的整数，收到 {streak!r}")
    v = classify_format_violation(text)
    label_cn = v["label_cn"] if v is not None else _LABEL_CN[LABEL_UNKNOWN]
    head = (framework_message or "").rstrip()
    precise = _FEEDBACK_TEMPLATE.format(label_cn=label_cn, streak=streak)
    return f"{head}\n\n{precise}"
