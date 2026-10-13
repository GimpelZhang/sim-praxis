#!/usr/bin/env python3
"""探针 B：P2P 文本通道真实往返（register_p2_im_message_receive_v1）。

流程：启动 WS（复用网关 start_ws，生产参数 auto_reconnect=True；探针为一次性
进程，结束即 gw.close() 关闭连接）→ 机器人向 owner P2P 发
「请回复任意一句话（Phase1 探针B）」→ 最长等 240s → 断言回调事件 content/chat_id/
sender 正确 → 终端人工确认本人操作 → 落 probe_b_p2p.json，exit 0/2。

运行（source scripts/00_env.sh 后）：
  /mnt/venvs/simpraxis/bin/python -m orchestrator.feishu_gateway.probes.probe_b_p2p
"""
from __future__ import annotations

import sys
import time

from ..client import FeishuAPIError, gateway_from_env
from ._evidence import confirm, now_iso, print_checklist, write_evidence

WAIT_SECONDS = 240
PROBE_TEXT = "请回复任意一句话（Phase1 探针B）"


def main() -> int:
    print_checklist("探针 B：P2P 文本双通道真实往返", [
        "确认本进程已打印 WebSocket 已连接",
        "在飞书里找到 sim-praxis 机器人的 P2P 会话",
        f"收到机器人消息「{PROBE_TEXT}」后，240 秒内回复任意一句话",
        "回到终端按提示确认是你本人操作",
    ])
    captured: list[dict] = []
    try:
        gw = gateway_from_env()
    except RuntimeError as exc:
        print(f"[probe-b] {exc}", file=sys.stderr)
        return 2
    # 探针观察回调：直接挂属性（构造后注入，避免闭包前向引用）
    gw._on_p2p_text = lambda info: captured.append(info)  # noqa: SLF001

    try:
        # 等长连接建立（与 Phase 0 探针同写法，生产参数 auto_reconnect 在网关内为 True）
        for _ in range(20):
            conn = getattr(gw._ws_client, "_conn", None)  # noqa: SLF001
            if conn is not None:
                break
            time.sleep(0.5)
        connected = getattr(gw._ws_client, "_conn", None) is not None  # noqa: SLF001
        print(f"[probe-b] WebSocket 已连接: {connected}", flush=True)

        send_error = None
        sent_message_id = None
        try:
            sent_message_id = gw.send_text(gw.owner_open_id, PROBE_TEXT)
        except FeishuAPIError as exc:
            send_error = str(exc)
        print(f"[probe-b] 引导文本已发送 message_id={sent_message_id}", flush=True)

        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and not captured:
            time.sleep(0.5)

        ev = captured[0] if captured else None
        checks: list[dict] = []
        if ev is None:
            checks.append({"item": "240s 内收到 p2p 回调", "expected": True,
                           "actual": False, "passed": False})
        else:
            text = ev.get("text", "")
            checks += [
                {"item": "chat_type=p2p", "expected": "p2p",
                 "actual": ev.get("chat_type"),
                 "passed": ev.get("chat_type") == "p2p"},
                {"item": "sender=owner open_id", "expected": gw.owner_open_id,
                 "actual": ev.get("sender_open_id"),
                 "passed": ev.get("sender_open_id") == gw.owner_open_id},
                {"item": "content 非空且可解析 text",
                 "expected": "非空字符串", "actual": text,
                 "passed": bool(text)},
                {"item": "open_chat_id 非空", "expected": "非空",
                 "actual": ev.get("chat_id"),
                 "passed": bool(ev.get("chat_id"))},
            ]
        manual_confirmed = bool(ev) and confirm("事件已收到。请确认是你本人在飞书发送？")
        checks.append({"item": "人工确认本人操作", "expected": True,
                       "actual": manual_confirmed, "passed": manual_confirmed})
        passed = connected and send_error is None and ev is not None \
            and all(c["passed"] for c in checks)

        payload = {
            "probe": "b_p2p_text",
            "started_at": now_iso(),
            "wait_seconds": WAIT_SECONDS,
            "ws_connected": connected,
            "sent_message_id": sent_message_id,
            "send_error": send_error,
            "event": ev,
            "checks": checks,
            "manual_confirmed": manual_confirmed,
            "passed": passed,
        }
        path = write_evidence("probe_b_p2p.json", payload)
        print(f"[probe-b] 证据已写 {path}；passed={passed}", flush=True)
        return 0 if passed else 2
    finally:
        gw.close()


if __name__ == "__main__":
    sys.exit(main())
