#!/usr/bin/env python3
"""Phase 2.8.1 T6 干跑 CLI（计划 §10.4 / §10.5 步骤 1；**0 付费、0 触网**）。

    /mnt/venvs/simpraxis/bin/python -I scripts/benchmark/phase281_dryrun.py --all

把三个任务的罐头候选送进**生产流水线**（``trial_harness.run_trial``）各跑一遍，
产物落 ``<root>/dryrun/<UTC 时间戳>/``，并把整棵树就地打上 ``simulated`` 标记。

干跑**不是**实验：它不发一个模型请求、不读也不写真实账本，产出的任何数字都
不得进 P281-9 的报告——那道理由 ``dryrun.load_real`` / ``dryrun.assert_real``
把守（读到模拟件即抛 ``AnalysisRefused``，不降级、不静默过滤）。

``-I``（隔离子进程模式）下 ``sys.path`` 不含脚本目录，故本文件自己把仓根插进去。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281 import constants, dryrun, paths  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 2.8.1 干跑：罐头候选走完整条 Trial 流水线（0 付费）")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--all", action="store_true",
                        help="三个任务 × 两条臂（缺省）")
    target.add_argument("--task", choices=list(constants.TASK_IDS),
                        help="只跑一个任务（可配 --arm）")
    parser.add_argument("--arm", choices=list(constants.ARMS),
                        help="只跑一条臂；缺省两条都跑")
    parser.add_argument("--rep", type=int, default=0,
                        help="重复轮次号（干跑只用一个，缺省 0）")
    parser.add_argument("--root", default=None,
                        help="干跑落点根；缺省 $SIMPRAXIS_PHASE281_ROOT 或数据根")
    parser.add_argument("--no-stamp", action="store_true",
                        help="不打 simulated 标记（仅供排障；产物随之不可分析）")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    # 本阶段任何进程里 2.8.0 的根 env 都必须未设置（P281-0 锁）
    paths.assert_no_benchmark_root_env()

    tasks = [args.task] if args.task else list(constants.TASK_IDS)
    arms = [args.arm] if args.arm else list(constants.ARMS)
    summary = dryrun.run_all(tasks=tasks, arms=arms, rep=int(args.rep),
                             root=(Path(args.root) if args.root else None),
                             stamp_tree_after=not args.no_stamp)
    printable = {
        "root": summary["root"],
        "rep": summary["rep"],
        "ledger_view": summary["ledger_view"],
        "ledger_rows": summary["ledger_rows"],
        "trials": [{"trial_id": t["trial_id"], "status": t["status"],
                    "used_calls": t["used_calls"],
                    "canned_calls": t["canned_calls"]}
                   for t in summary["trials"]],
    }
    if summary.get("stamp"):
        printable["stamp"] = summary["stamp"]
    print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))

    # 干跑的判据只有一条：**流水线真的走到了终态**（不是崩溃、不是启动即拒）。
    # 干跑不承诺候选能 PASS——罐头的目标是让七步跑完，不是拿分。``CAP_HIT`` 与
    # ``BLOCKED_INFRA`` 都是**合法终态**（前者说明帽子生效，后者说明环境门生效），
    # 只有 ``CRASHED`` 与词汇之外的取值才算流水线没走通。
    from orchestrator.benchmark.phase281 import trial_harness

    broken = [t["trial_id"] for t in summary["trials"]
              if t["status"] == "CRASHED"
              or t["status"] not in trial_harness.STATUSES]
    if broken:
        print(f"干跑未走完流水线：{broken}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
