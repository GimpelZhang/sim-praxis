"""R1.3 审查 worker：**一次**非 agentic 模型调用（由父进程以新进程启动）。

隔离事实由调度器（父进程）创建：全新临时目录、输入白名单只有 ``packet.json``、
env 仅保留凭据/路径所需变量；worker 只读 packet、只发一次 POST、只写一个
结果文件。父进程据本文件写 ``ReviewRunReceipt``（请求/响应逐字节哈希）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from orchestrator.common import runtime_paths as RP
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context
from orchestrator.gateway.router import ModelGateway
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import review_runner as RN

STAGE = "coding_debug"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--system-out", required=True)
    ap.add_argument("--user-out", required=True)
    ap.add_argument("--request-out", required=True)
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--thread-id", required=True)
    ap.add_argument("--timeout", type=int, default=180)
    # 2026-11-17 R1-N：同一审查仪器，独立账本（2907）；缺省 2905 旧行为不变。
    ap.add_argument("--ledger-phase", type=int, default=2905)
    args = ap.parse_args(argv)
    packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    system, user = RN.build_request_text(packet=packet)
    Path(args.system_out).write_text(system, encoding="utf-8")
    Path(args.user_out).write_text(user, encoding="utf-8")
    request = json.dumps({"schema": RN.REQUEST_SCHEMA, "system": system,
                          "user": user, "review_id": packet.get("review_id"),
                          "reviewer_id": packet.get("reviewer_id")},
                         sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    Path(args.request_out).write_bytes(request)
    if int(args.ledger_phase) == 2907:
        ledger = QuotaLedger.rsi_r1n()
        pause_path = RP.rsi_r1n_gateway_state_path()
        # RSI-R1-N 传输加固（2026-11-17）：与候选链同款策略——每槽至多 3 次
        # HTTP 尝试、只重试传输类错误、轮级重试预算 30、单槽墙钟 ≥300s。
        gateway = ModelGateway(
            max_attempts=3, ledger=ledger, pause_state_path=pause_path,
            retry_log_path=RP.rsi_r1n_campaign_root() / "quota" / "retries.jsonl",
            retry_budget=30, retry_on_empty=False)
        timeout = max(int(args.timeout), 300)
    else:
        ledger = QuotaLedger.rsi_r13()
        pause_path = RP.rsi_r13_gateway_state_path()
        gateway = ModelGateway(
            max_attempts=1, ledger=ledger, pause_state_path=pause_path)
        timeout = int(args.timeout)
    capture: "dict[str, object]" = {}
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    with billing_context(task=args.task, batch_id=args.batch_id,
                         thread_id=args.thread_id):
        text, tokens = gateway.call(
            STAGE, messages, max_tokens=int(P.RSI_R13_OUTPUT_TOKENS),
            temperature=0.2, timeout=timeout, capture=capture)
    ended = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = {
        "schema": "simpraxis.rsi_r13.review_call.v1", "schema_version": 1,
        "text": str(text or ""), "usage": capture.get("usage"),
        "attempts_http": int(capture.get("attempts_http", 1) or 1),
        "stop_reason": capture.get("stop_reason"),
        "process_id": os.getpid(), "started_at_utc": started,
        "ended_at_utc": ended,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "packet_sha256": hashlib.sha256(
            json.dumps(packet, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()).hexdigest(),
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
