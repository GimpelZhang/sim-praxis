"""Phase 1 Task 1：飞书真实审批网关（新包，刻意与 orchestrator/gateway 模型网关分离）。

双通道真实阻断（详细计划 §1.2 / §2 Task 1）：
  通道①卡片 form（P2CardActionTrigger，form_submit）；
  通道②P2P 文本（im.message.receive_v1，10 分钟会话窗追加 human_notes）。
关联键 thread_id + gate(1|2)，pending_gates.db 独立于 LangGraph checkpoint 持久化，
支持重启后按已有 message_id 重挂等待、绝不重发卡片。

本包不导入 orchestrator.gateway（模型网关），不读 stage_models.yaml，零 LLM 付费。
"""
from __future__ import annotations

from .cards import (
    build_gate1_card,
    build_gate1_card_v2,
    build_gate2_card,
    build_gate2_card_v2,
    parse_card_event,
)
from .client import FeishuAPIError, FeishuGateway, get_gateway
from .store import PendingStore
from .video import compress_command, select_ladder_step

__all__ = [
    "FeishuAPIError",
    "FeishuGateway",
    "PendingStore",
    "build_gate1_card",
    "build_gate1_card_v2",
    "build_gate2_card",
    "build_gate2_card_v2",
    "parse_card_event",
    "select_ladder_step",
    "compress_command",
    "get_gateway",
]
