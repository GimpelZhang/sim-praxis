#!/usr/bin/env python3
"""Phase 2 Task6 D-8：thinking 配置块接受性一次性诊断探针（图外独立工具）。

**一次性探针，不是生产路径**：LangGraph 图/runner 永不调用本脚本或
``ModelGateway.diag_thinking``（生产调用点由 tests/milestone2/
test_diag_thinking.py 源码红线扫描锁定，仅本文件 1 处）。生产两条传输路径
（router._chat_loop 的 litellm completion kwargs、_post_messages 默认
payload）刻意一个 thinking 字符都不传——Ark 浮动别名曾吃光输出预算
（test_04 事故，D-8）。本探针只是付费前回答一个问题：**方舟 Anthropic-
Messages shim 接不接受正形 ``thinking={"type":"enabled","budget_tokens":N}``
配置块**。

锁内适配（plan L559 骨架原写 direction.cli 首轮，现实：direction/cli.py
Task6 入场闸早于付费件落地、ascent 唯一入口是图进程，而生产 router 刻意
不传 thinking）：首轮"req 可识别"无法经生产路径完成，也不应为探针改生产
路径，故探针在图外独立单发。macro/coding 各跑一次、各吃自己那张票的
1 个 seq（T6-B1-macro / T6-B1-coding）；无论结果如何，本批次 router 不
开启 thinking：被拒（exit 10）由 lead 在 docs/ADAPTATIONS.md 追加债务行，
max_tokens 维持 16384/8192 不调（硬规则 7 不放水）。

安全纪律：
  - 显式付费守卫：必须 ``SIMPRAXIS_ALLOW_PAID=1``，裸跑零触网 exit 2
    （与 paid 测试同一守卫名）；
  - 硬规则 1：只允许 macro_ideation/coding_debug 两个**文本** stage
    （macro/coding 视觉未测，探针绝不携带 image block）；模型 ID 只读
    stage_models.yaml，本脚本不接受任何模型覆盖参数；
  - 经 BillingGate/billing_context 正常记账（preflight + 恰好一行整数
    seq）；缺凭据写 seq=null blocked；连接类异常记 ok=false 后抛出；
  - 证据 JSON 以 0600 原子写落 phase2 runs/task6_ascent/，含网关 redact
    后的原始响应体，不含 URL/认证头/请求文本。

退出码：0=shim 接受（200/type=message）；2=未设付费守卫或参数错误；
10=收到明确 HTTP 响应但被拒（accepted=False，证据已落盘）；
1=连接/配置等异常（ok=false 已记账，证据不落）。

用法：
  SIMPRAXIS_ALLOW_PAID=1 python scripts/phase2/diag_thinking.py \\
      --stage macro_ideation --batch-id T6-B1-macro \\
      [--thread t6-genesis-ascent-20261016a] [--out PATH] \\
      [--budget-tokens 1024] [--max-tokens 2048]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text,
    ensure_private_dir,
)
from orchestrator.common.runtime_paths import phase2_runs_root  # noqa: E402
from orchestrator.common.validators import validate_batch_id, validate_thread_id  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger,
    billing_context,
)
from orchestrator.gateway.router import (  # noqa: E402
    DIAG_THINKING_STAGES,
    ModelGateway,
)

DEFAULT_THREAD = "t6-genesis-ascent-20261016a"
TASK = 6

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1


def default_out(stage: str) -> Path:
    return phase2_runs_root() / "task6_ascent" / f"diag_thinking_{stage}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diag_thinking.py",
        description="D-8 thinking 配置块接受性一次性诊断探针（显式付费守卫）")
    parser.add_argument("--stage", required=True, choices=DIAG_THINKING_STAGES,
                        help="被诊断的文本 stage（硬规则1：禁止 visual）")
    parser.add_argument("--batch-id", required=True,
                        help="本 stage 自己的配额票号（如 T6-B1-macro）")
    parser.add_argument("--thread", default=DEFAULT_THREAD,
                        help=f"记账线程（默认 {DEFAULT_THREAD}）")
    parser.add_argument("--out", default=None,
                        help="证据 JSON 输出路径（默认 phase2 runs/"
                             "task6_ascent/diag_thinking_<stage>.json）")
    parser.add_argument("--budget-tokens", type=int, default=1024,
                        help="thinking.budget_tokens（默认 1024）")
    parser.add_argument("--max-tokens", type=int, default=2048,
                        help="请求 max_tokens，必须严格大于 budget（默认 2048）")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    # 首行付费守卫：裸跑零触网（与 paid pytest 同一环境变量）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("diag_thinking: 拒绝运行——本探针发起真实付费调用，必须显式设置 "
              "SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）", file=sys.stderr)
        return EXIT_GUARD

    args = build_parser().parse_args(argv)
    try:
        validate_thread_id(args.thread)
        validate_batch_id(args.batch_id)
    except ValueError as exc:
        print(f"diag_thinking: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out = Path(args.out) if args.out else default_out(args.stage)

    # 账外构造 fail-closed（G-13）：必须挂真实 QuotaLedger。
    gateway = ModelGateway(ledger=QuotaLedger())
    try:
        with billing_context(task=TASK, batch_id=args.batch_id,
                             thread_id=args.thread):
            record = gateway.diag_thinking(
                args.stage, max_tokens=args.max_tokens,
                budget_tokens=args.budget_tokens)
    except Exception as exc:  # noqa: BLE001 —— 连接/配置类：ok=false 已在网关记账
        print(f"diag_thinking: 探针异常（无 HTTP 证据可落）："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # 账位三元组随证据落盘（stage 已在 record 内）。
    record["batch_id"] = args.batch_id
    record["thread_id"] = args.thread
    ensure_private_dir(out.parent)
    atomic_write_text(
        out, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", mode=0o600)
    print(f"diag_thinking: stage={args.stage} accepted={record['accepted']} "
          f"http_status={record['http_status']} type={record['type']!r} "
          f"blocks={record['content_block_types']} 证据→{out}")
    if record["accepted"]:
        return 0
    print("diag_thinking: shim 明确拒绝 thinking 配置块（exit 10）——"
          "lead 需在 docs/ADAPTATIONS.md 追加债务行；本批次不开启 thinking、"
          "不调 max_tokens", file=sys.stderr)
    return EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
