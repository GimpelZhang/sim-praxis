"""Phase 1 Task 1 真实飞书探针（B/C/D，HITL；0 LLM 付费，飞书 OpenAPI 不计方舟预算）。

每个探针可独立运行：
  python -m orchestrator.feishu_gateway.probes.probe_b_p2p
  python -m orchestrator.feishu_gateway.probes.probe_c_form --round empty|filled|approve
  python -m orchestrator.feishu_gateway.probes.probe_d_video --build-container   # 只生成容器脚本
  python -m orchestrator.feishu_gateway.probes.probe_d_video                     # 宿主机上传/发送
退出码语义统一：0=通过，2=未通过（证据 JSON 同步落盘）。探针只写不自动运行。
"""
from __future__ import annotations
