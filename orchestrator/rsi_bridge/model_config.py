"""模型接缝：PhysicalRSI 的语言提案器全部回 `ModelGateway.call` 计数。

硬规则 1 在此的结构性表达：
- stage 映射钉死——诊断/设计→`coding_debug`，规划→`macro_ideation`，视觉唯一入口
  `call_visual`（本模块不提供视觉路径）；
- 模块内**没有**任何模型 ID 字面量，模型选择只发生在 `stage_models.yaml`；
- image block 在触网前拒绝（继承网关守卫，不重复实现）；
- 一次逻辑调用恰好一次 `gateway.call`（网关内部退避重试不重复记账，账行 ≤1/调用）。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = ["GatewayLanguageProposer", "STAGE_BY_PURPOSE", "ModelConfigError"]

#: purpose → 网关 stage（唯一映射点；未知 purpose 拒绝，不猜阶段）
STAGE_BY_PURPOSE = {
    "diagnose": "coding_debug",
    "design": "coding_debug",
    "implement": "coding_debug",
    "plan": "macro_ideation",
    "ideate": "macro_ideation",
}


class ModelConfigError(RuntimeError):
    """越权/非法请求（stage 未知、含 image block 等），在触网前拒绝。"""


def _assert_text_only(messages: list[dict[str, Any]]) -> None:
    if not isinstance(messages, list) or not messages:
        raise ModelConfigError("messages 必须是非空消息列表")
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "image":
                    raise ModelConfigError(
                        f"messages[{index}] 含 image block：macro/coding 阶段视觉未测，"
                        "视觉唯一入口是 ModelGateway.call_visual（硬规则 1）")


class GatewayLanguageProposer:
    """PhysicalRSI `ModelConfig` 的语言面替身：只经网关，绝不直连供应商。"""

    def __init__(self, gateway: Any, *, max_tokens: int = 512,
                 temperature: float = 0.2, timeout: int = 120):
        if not hasattr(gateway, "call"):
            raise ModelConfigError("gateway 必须提供 call(stage, messages, ...)")
        self.gateway = gateway
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    def stage_for(self, purpose: str) -> str:
        try:
            return STAGE_BY_PURPOSE[purpose]
        except KeyError:
            raise ModelConfigError(
                f"未知 purpose {purpose!r}；允许 {sorted(STAGE_BY_PURPOSE)}") from None

    def propose(self, purpose: str, messages: list[dict[str, Any]],
                *, capture: dict | None = None) -> dict:
        stage = self.stage_for(purpose)
        _assert_text_only(messages)
        text, tokens = self.gateway.call(
            stage, messages, max_tokens=self.max_tokens,
            temperature=self.temperature, timeout=self.timeout, capture=capture)
        return {"stage": stage, "text": text,
                "tokens": {"in": tokens[0], "out": tokens[1]}}

    # PhysicalRSI 侧语言提案器的调用形状薄适配（保持我方单一入口）
    def __call__(self, prompt: str, *, purpose: str = "design") -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ModelConfigError("prompt 必须是非空字符串")
        return self.propose(purpose, [{"role": "user", "content": prompt}])
