#!/usr/bin/env python3
"""Phase 2 Task6：coding_debug 输出上限接受性一次性诊断探针（图外独立工具）。

**一次性探针，不是生产路径**：LangGraph 图/runner 永不导入或调用本脚本
（调用点由 tests/milestone2/test_diag_max_tokens.py 源码红线扫描锁定，
仅本文件 1 处；本脚本也永不 import orchestrator.graph）。背景：拆块出生
3 轮（seq50/54/55）全部在 max_tokens=16384 处到顶、JSON 信封未闭合，
成功的 seq53 仅余 7 token——16384 对 4 块切分零余量。用户 2026-10-21
裁决（AskUserQuestion 选项 A）：探针先验供应侧是否接受 32768，再改
引擎常量。本探针只回答两个事实：

  1. **接受性**：方舟 Anthropic-Messages shim 对 coding_debug 携带
     ``max_tokens=32768`` 的请求是否返回 200/type=message（明确 4xx 即
     被拒，exit 10）；
  2. **真实余量**：同一 seq 内让模型做确定性长输出（纯文本数数），若
     ``usage.output_tokens > 16384``，则 16384 不是硬墙、32768 提供真实
     token 余量（headroom_proven=true）。HTTP 200 但产出没超 16384 也算
     接受（accepted=true、headroom_proven=false），余量留给生产首验。
     **headroom_proven 只证明 token 账上越过 16384**：output_tokens 可能
     含思考链（该模型曾吃光全部 output 只产 thinking），故另给机器可读
     的文本旁证——count_lines/last_number/count_completed/output_at_cap。
     改引擎常量的放行条件是 accepted=true 且（count_completed=true 或
     raw_tail 旁证有序长文本），不得只凭 headroom 布尔值翻案。若在
     32768 处被截断（output_at_cap=true、count_completed=false），同样
     证明线顶 ≥32768，但 32768 是否够生产用留给出生首验。

安全纪律（镜像 scripts/phase2/diag_thinking.py）：
  - 显式付费守卫：必须 ``SIMPRAXIS_ALLOW_PAID=1``，裸跑零触网 exit 2；
  - 硬规则 1：只允许 coding_debug 一个**文本** stage，绝不携带 image
    block；模型 ID 只读 stage_models.yaml，本脚本不接受任何模型覆盖参数；
  - **单发**：ModelGateway(max_attempts=1)——所有已分类出口（空 200/
    429/5xx/超时/连接/404/4xx）一次运行至多一次上游 HTTP，禁用 G-12
    空回答退避（该模型确有 thinking-only 200 史，默认 5 连退会白烧 5
    个长输出 seq）。路由器未分类异常兜底分支硬编码补发一次（极端情况
    至多 2 个上游请求，仍只记 1 seq）。代价：首个瞬时 429 即写长熔断
    暂停件（一次性工具，照 §6.3 手工清）；thinking-only 200 立即
    EmptyResponseError→exit 1（恰好 1 seq，不丢证据文件以外的信息）；
  - 运行前**必须先落本探针自己的书面票**（preflight 拦截，无票不触网）：
    python -m orchestrator.gateway.quota_ledger approve \\
        --batch-id T6-B2-probe32k --task 6 --stage coding_debug \\
        --approved 1 --note "32k max_tokens 接受性单发探针（用户选项A）"
  - 经 billing_context/QuotaLedger 正常记账（恰好一行整数 seq，失败也
    占 seq；缺凭据/缺票/触顶 seq=null blocked 或 GatewayPausedError）；
  - 证据 JSON 0600 原子写落 phase2 runs/task6_ascent/：只存请求 max_tokens、
    usage、数数核验字段、raw 长度与脱敏 head/tail 200 字符，不存认证头/
    完整原文；err 与 head/tail 经密钥正则 + URL 剥离双重脱敏。
    --out 的父目录会被 ensure_private_dir 置 0700（已存在也 chmod，
    勿指 /tmp 等共享目录；生产用默认 phase2 runs 路径）。

退出码：0=shim 接受（200 且有文本）；2=未设付费守卫或参数错误；
10=收到明确拒绝（NonRetryableError，证据已落盘）；1=其他停止出口——
连接异常、EmptyResponseError（thinking-only/空 200）、GatewayPausedError
（缺票/任务或全局硬顶/长熔断）、StageUnresolvedError、配置/凭据错误；
exit 1 不落 HTTP 证据，原因看 stderr 与台账该行 err。

用法：
  python -m orchestrator.gateway.quota_ledger approve --batch-id \\
      T6-B2-probe32k --task 6 --stage coding_debug --approved 1 --note "..."
  SIMPRAXIS_ALLOW_PAID=1 python scripts/phase2/diag_max_tokens.py \\
      --batch-id T6-B2-probe32k [--max-tokens 32768] [--count-to 12000] \\
      [--thread t6-genesis-ascent-20261016a] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text,
    ensure_private_dir,
)
from orchestrator.common.runtime_paths import phase2_runs_root  # noqa: E402
from orchestrator.common.secrets import redact_text  # noqa: E402
from orchestrator.common.validators import validate_batch_id, validate_thread_id  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger,
    billing_context,
)
from orchestrator.gateway.router import (  # noqa: E402
    NonRetryableError,
    ModelGateway,
)

STAGE = "coding_debug"          # 硬规则 1：探针只诊断文本 coding stage
DEFAULT_THREAD = "t6-genesis-ascent-20261016a"
TASK = 6
HEAD_TAIL_CHARS = 200
MIN_PROBE_MAX_TOKENS = 16385    # 探针存在意义：必须高于现网 16384
MAX_PROBE_MAX_TOKENS = 65536
TIMEOUT_S = 900
SINGLE_ATTEMPT = 1             # 单发：禁用 G-12 退避，一次运行至多一次付费 HTTP

_URL_RE = re.compile(r"https?://[^\s\"'）)]+")


def _scrub(text: str, limit: int | None = None) -> str:
    """证据脱敏：密钥正则单一源 + URL 结构化剥离（异常串可能带 api_base）。"""
    out = _URL_RE.sub("[url]", redact_text(text))
    return out if limit is None else out[:limit]


def _count_check(raw_text: str, count_to: int) -> dict:
    """数数核验（机器可读文本旁证）：解析每行整数，核对 1..N 完整有序。"""
    nums: list[int] = []
    nonempty_lines = 0
    for line in raw_text.splitlines():
        s = line.strip()
        if not s:
            continue
        nonempty_lines += 1
        if len(s) <= 9 and s.lstrip("+").isdigit():  # 最多 9 位，拒符号串
            nums.append(int(s.lstrip("+")))  # 守卫与解析同一口径（防 ++5 失配）
    expect = list(range(1, count_to + 1))
    completed = nums == expect
    return {
        "count_lines": nonempty_lines,
        "numbers_parsed": len(nums),
        "last_number": nums[-1] if nums else None,
        "count_completed": completed,
        # 有序但未数完 = 被长度截断的直接旁证（配合 output_at_cap）。
        "ordered_prefix": nums == expect[:len(nums)] if nums else False,
    }

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1


def default_out(max_tokens: int) -> Path:
    return (phase2_runs_root() / "task6_ascent"
            / f"diag_max_tokens_{max_tokens}.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diag_max_tokens.py",
        description="coding_debug max_tokens 上限接受性一次性诊断探针"
                    "（显式付费守卫）")
    parser.add_argument("--batch-id", required=True,
                        help="本探针自己的配额票号（如 T6-B2-probe32k）")
    parser.add_argument("--max-tokens", type=int, default=32768,
                        help="请求 max_tokens（默认 32768，须 >16384）")
    parser.add_argument("--count-to", type=int, default=12000,
                        help="确定性长输出目标：从 1 逐行数到 N（默认 12000）")
    parser.add_argument("--thread", default=DEFAULT_THREAD,
                        help=f"记账线程（默认 {DEFAULT_THREAD}）")
    parser.add_argument("--out", default=None,
                        help="证据 JSON 输出路径（默认 runs/task6_ascent/"
                             "diag_max_tokens_<max>.json）；警告：父目录会被"
                             "强制 chmod 0700，勿指共享目录")
    return parser


def _probe_prompt(count_to: int) -> str:
    return (
        "这是一个输出容量测试，不写代码。严格按要求输出：从 1 开始每行"
        f"一个整数，一直数到 {count_to}，只输出这些整数行，不要标题、"
        "不要解释、不要 markdown 围栏、不要空行、不要省略。")


def main(argv: "list[str] | None" = None) -> int:
    # 首行付费守卫：裸跑零触网（与 paid pytest 同一环境变量）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("diag_max_tokens: 拒绝运行——本探针发起真实付费长输出调用，"
              "必须显式设置 SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）",
              file=sys.stderr)
        return EXIT_GUARD

    args = build_parser().parse_args(argv)
    try:
        validate_thread_id(args.thread)
        validate_batch_id(args.batch_id)
        if not (MIN_PROBE_MAX_TOKENS <= args.max_tokens <= MAX_PROBE_MAX_TOKENS)\
                or not isinstance(args.max_tokens, int):
            raise ValueError(
                f"max_tokens 须在 [{MIN_PROBE_MAX_TOKENS},{MAX_PROBE_MAX_TOKENS}]"
                f" 整数区间：{args.max_tokens!r}")
        if not (100 <= args.count_to <= 100000):
            raise ValueError(f"count_to 超出 [100,100000]：{args.count_to!r}")
    except ValueError as exc:
        print(f"diag_max_tokens: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out = Path(args.out) if args.out else default_out(args.max_tokens)

    # G-13 账外构造 fail-closed；max_attempts=1：已分类出口绝不内部退避
    # （thinking-only 200 史曾见，5 连退=白烧 5 个长输出 seq），仅路由器
    # 未分类异常兜底可能补发一次（仍只记 1 seq）；代价是首个瞬时 429
    # 即写长熔断暂停件（一次性工具，手工清）。
    gateway = ModelGateway(ledger=QuotaLedger(),
                           max_attempts=SINGLE_ATTEMPT)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _probe_prompt(args.count_to)}]}]
    try:
        with billing_context(task=TASK, batch_id=args.batch_id,
                             thread_id=args.thread):
            raw, usage = gateway.call(
                STAGE, messages,
                max_tokens=args.max_tokens, temperature=0, timeout=TIMEOUT_S)
    except NonRetryableError as exc:
        record = {
            "probe": "diag_max_tokens",
            "accepted": False,
            "captured_at_iso": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stage": STAGE,
            "batch_id": args.batch_id,
            "thread_id": args.thread,
            "max_tokens": args.max_tokens,
            "count_to": args.count_to,
            "err": _scrub(str(exc), 300),
        }
        try:
            ensure_private_dir(out.parent)
            atomic_write_text(
                out, json.dumps(record, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n", mode=0o600)
        except OSError as write_exc:  # 证据写失败不得变 traceback（seq 已记）
            print(f"diag_max_tokens: 供应侧已拒绝但证据写失败："
                  f"{type(write_exc).__name__}: {write_exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"diag_max_tokens: 供应侧明确拒绝 max_tokens={args.max_tokens}"
              f"（exit 10）证据→{out}", file=sys.stderr)
        return EXIT_REJECTED
    except Exception as exc:  # noqa: BLE001 —— 连接/熔断/缺票/空回答/配置类：账已记
        print(f"diag_max_tokens: 探针停止（无 HTTP 接受证据，原因看台账）："
              f"{type(exc).__name__}: {_scrub(str(exc), 300)}", file=sys.stderr)
        return EXIT_ERROR

    tokens_in, tokens_out = usage
    raw_text = raw if isinstance(raw, str) else str(raw)
    check = _count_check(raw_text, args.count_to)
    # m1：网关契约允许 usage 为 None；None 不可与 int 比较，按"余量未证"处理。
    headroom = tokens_out is not None and tokens_out > 16384
    record = {
        "probe": "diag_max_tokens",
        "accepted": True,
        "captured_at_iso": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage": STAGE,
        "batch_id": args.batch_id,
        "thread_id": args.thread,
        "max_tokens": args.max_tokens,
        "count_to": args.count_to,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        "headroom_proven": bool(headroom),
        # output_tokens 贴线（max-1 容差 litellm 口径）= 可能仍被长度截断。
        "output_at_cap": (tokens_out is not None
                          and tokens_out >= args.max_tokens - 1),
        **check,
        "raw_chars": len(raw_text),
        "raw_head": _scrub(raw_text[:HEAD_TAIL_CHARS]),
        "raw_tail": _scrub(raw_text[-HEAD_TAIL_CHARS:]),
    }
    try:
        ensure_private_dir(out.parent)
        atomic_write_text(
            out, json.dumps(record, ensure_ascii=False, indent=2,
                            sort_keys=True) + "\n", mode=0o600)
    except OSError as write_exc:  # 接受路径证据写失败也不得 traceback（seq 已记）
        print(f"diag_max_tokens: 供应侧已接受但证据写失败："
              f"{type(write_exc).__name__}: {write_exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"diag_max_tokens: accepted=True max_tokens={args.max_tokens} "
          f"out_tokens={tokens_out} headroom_proven={record['headroom_proven']} "
          f"count_completed={record['count_completed']} "
          f"output_at_cap={record['output_at_cap']} "
          f"last_number={record['last_number']} 证据→{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
