#!/usr/bin/env python3
"""Task 2 direction_setter 命令行入口。

实呼（生产）：
  python -m orchestrator.direction.cli --thread t1_maniskill_20261014 \
      --track track1 --index 1 --batch-id T2-B1
前置条件：批次凭据 quota/approvals/<batch-id>.json 必须已由 quota_ledger approve
写入（用户书面批准后），否则首次 preflight 即拒绝；不存在 --approve 旁路。

--no-gate 仅限本地/测试（monkeypatch 网关或人工指定），且必须显式置
SIMPRAXIS_ALLOW_NO_GATE=1；产物会被打上 gate_bypassed_locally=true 红印，
禁止作为 Gate1 通过证据使用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from memory_flywheel.vector_engine import VectorEngine
from orchestrator.direction.inputs import get_input
from orchestrator.direction.setter import (
    DirectionError, DirectionSchemaError, DirectionSetter, _evidence_dir,
)
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context
from orchestrator.gateway.router import ModelGateway


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase1 Task2 direction_setter")
    ap.add_argument("--thread", required=True, help="全局唯一 thread_id（建议 t<track><n>_<date>）")
    ap.add_argument("--track", required=True, choices=("track1", "track2"))
    ap.add_argument("--index", required=True, type=int)
    ap.add_argument("--batch-id", required=True, dest="batch_id")
    ap.add_argument("--task-title", default=None)
    ap.add_argument("--no-gate", action="store_true",
                    help="本地调试：不发 Gate1（需 SIMPRAXIS_ALLOW_NO_GATE=1）")
    args = ap.parse_args(argv)

    payload = get_input(args.track, args.index)
    # Task3（§6.3）：账本挂在网关上；task/batch 归属经 billing_context 透传，
    # 缺上下文的付费调用在 BillingGate fail-closed（禁止裸跑烧预算）。
    gateway = ModelGateway(ledger=QuotaLedger())
    ve = VectorEngine()
    setter = DirectionSetter(gateway, ve)

    if args.no_gate:
        if os.environ.get("SIMPRAXIS_ALLOW_NO_GATE") != "1":
            print("ERROR: --no-gate 需显式 SIMPRAXIS_ALLOW_NO_GATE=1（Gate1 不可默认旁路）",
                  file=sys.stderr)
            return 2
        with billing_context(task=2, batch_id=args.batch_id, thread_id=args.thread):
            result = setter.run_direction(
                track=args.track, payload=payload, thread_id=args.thread,
                batch_id=args.batch_id, bypass_stamp=True)
        out = _evidence_dir() / f"proposals_{args.thread}_r0.json"
        print(f"[LOCAL-ONLY] gate_bypassed_locally=true；{out} 带红印，不得作为 Gate1 证据")
        return 0

    # 生产：真实飞书长连接 Gate1，阻塞到真人决策
    from orchestrator.feishu_gateway.client import gateway_from_env
    feishu = gateway_from_env()
    try:
        with billing_context(task=2, batch_id=args.batch_id, thread_id=args.thread):
            outcome = setter.run_gate1_loop(
                feishu, track=args.track, payload=payload,
                thread_id=args.thread, batch_id=args.batch_id,
                task_title=args.task_title)
    finally:
        feishu.close()

    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = _evidence_dir() / f"gate1_outcome_{args.thread}_{stamp}.json"
    out.write_text(json.dumps(outcome, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.chmod(out, 0o600)
    print(json.dumps({"status": outcome["status"],
                      "gate1_rejects": outcome["gate1_rejects"],
                      "evidence": str(out)}, ensure_ascii=False, indent=2))
    if outcome["status"] == "human_failure":
        print("HUMAN_FAILURE：Gate1 连续 2 次打回，按 D10 结案转人工", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DirectionSchemaError as exc:
        print(f"SCHEMA_FAILURE: {exc}", file=sys.stderr)
        raise SystemExit(4)
    except DirectionError as exc:
        print(f"DIRECTION_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(5)
