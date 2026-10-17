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
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.common.validators import assert_within, validate_thread_id
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

    # G-5：thread_id 是证据文件名路径段，先过字符集白名单
    # （^[A-Za-z0-9_-]{1,64}$），拒绝 ../、斜杠、空格等遍历/覆盖形态。
    try:
        validate_thread_id(args.thread)
    except (TypeError, ValueError) as exc:
        print(f"ERROR: 非法 --thread：{exc}", file=sys.stderr)
        return 2

    payload = get_input(args.track, args.index)

    # 锁内适配（Phase 2 Task6 入场）：计划 L553 骨架第 2 步写的是
    # legacy direction.cli，但 Task2 已把 classify 做成图节点且 ascent Gate1
    # 卡/Diff 请求器只在图缝接线；旧入口放行会用标准 Gate1 卡白烧 macro。
    # 故在任何 ModelGateway/VectorEngine/飞书构造之前加闸（退出码 6）：
    # 缺 V1 档案（V1IndexMissing，新克隆/CI 常态）→ 放行原 standard 流程；
    # 档案损坏/命中路径校验失败 → fail-closed；命中冻结 V1 档案的升阶题 →
    # 唯一图入口。--no-gate 红印路径同样不放行（保持升阶单入口）。
    from orchestrator.graph.ascent import (
        V1IndexCorrupt, V1IndexMissing, classify_run, load_v1_index)
    from orchestrator.judge.ascent_thresholds import AscentThresholdError
    try:
        v1_index = load_v1_index()
    except V1IndexMissing:
        v1_index = None
    except V1IndexCorrupt as exc:
        print(f"ERROR: V1 冻结档案索引损坏，禁止静默降级（exit 6）：{exc}",
              file=sys.stderr)
        return 6
    if v1_index is not None:
        try:
            verdict = classify_run(payload, v1_index)
        except (V1IndexCorrupt, AscentThresholdError) as exc:
            # 命中题面的 sha 对质失败或 dr_envelope 与登记基线漂移：
            # 档案不可信，与图节点 node_classify 同语义 fail-closed。
            print(f"ERROR: V1 冻结档案校验失败，禁止静默降级（exit 6）：{exc}",
                  file=sys.stderr)
            return 6
        if verdict["run_mode"] == "ascent":
            print(
                "ERROR: 本题命中冻结 V1 档案（RSI 升阶题 / ascent），legacy "
                "direction CLI 只支持 standard Gate1；升阶 Gate1 假设卡与 "
                "Gate2 双媒体 Diff 仅接在 LangGraph 图入口，在此放行会用标准 "
                "Gate1 卡白烧 macro（exit 6）。请改走图入口：\n"
                "  python -m orchestrator.graph.cli run --task 6 \\\n"
                f"    --thread {args.thread} --track {args.track} "
                f"--index {args.index} \\\n"
                f"    --batch-id {args.batch_id} "
                "--coding-batch-id <T6 coding 批次> "
                "--visual-batch-id <T6 visual 批次>\n"
                "图内顺序：classify→ascent Gate1 假设卡→真人批准→"
                "agent_run→evaluate→双媒体 Diff Gate2。",
                file=sys.stderr)
            return 6

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
    # G-7/G-5：0600 原子写 + 写后 containment 断言（thread 虽已过白名单，双保险）。
    atomic_write_text(out, json.dumps(outcome, ensure_ascii=False, indent=2))
    assert_within(out, _evidence_dir())
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
