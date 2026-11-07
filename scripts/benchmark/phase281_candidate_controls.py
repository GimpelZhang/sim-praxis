#!/usr/bin/env python
"""P281-6 候选对照面入口：``python -I scripts/benchmark/phase281_candidate_controls.py --all``。

逻辑在 ``orchestrator.benchmark.phase281.candidate_controls``；本文件只做参数解析、
逐条打印与落一份结果 JSON（薄封装）。**0 付费、0 网络、0 docker**：账本是临时目录里
自建的文件，网关是空实现，出生验证器只起本地隔离子进程。

退出码（与 ``run_negative_controls.py`` 同一套语义）：

* ``0``——全部 18 条都执行了，且**没有一条被放过**（``matched`` 或已登记为缺口）；
* ``1``——出现「既不相符、也没登记缺口」的对照（STOP-14 的形态），或观测与冻结表漂移；
* ``2``——部分执行（``--control`` 只跑其中几条，判据不完整）；
* ``3``——基建问题（夹具缺失、判决面结构变了、钱包/环境异常）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PARTIAL = 2
EXIT_STOP = 3


def _main(argv: "list[str] | None" = None) -> int:
    import argparse

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from orchestrator.benchmark.phase281 import candidate_controls as cc  # noqa: E402

    parser = argparse.ArgumentParser(
        prog="scripts/benchmark/phase281_candidate_controls.py",
        description="P281-6：18 条候选级负例对照（0 付费）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="跑全部 18 条")
    group.add_argument("--control", action="append", default=None,
                       metavar="ID", help="只跑指定对照（可重复，如 --control C07）")
    parser.add_argument("--out", default=None,
                        help="结果 JSON 落盘路径（缺省不落）")
    parser.add_argument("--workspace", default=None,
                        help="临时工作区（缺省建在系统临时目录，跑完删除）")
    parser.add_argument("--keep", action="store_true",
                        help="保留临时工作区（排障用）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace) if args.workspace else Path(
        tempfile.mkdtemp(prefix="p281-controls-"))
    owned = args.workspace is None
    only = None if args.all else list(args.control or ())
    try:
        try:
            results = cc.run_controls(workspace=workspace, only=only)
        except (cc.ControlError, OSError, KeyError) as exc:
            print(f"[controls] INFRA {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_STOP

        for row in results:
            mark = "ok " if row.matched else ("GAP" if row.gap else "BAD")
            print(f"[{row.control_id}] {mark} channel={row.channel:<9} "
                  f"expected={row.expected:<16} observed={row.observed:<16} "
                  f"| {row.title}")
            if row.observed_detail:
                print(f"        observed_detail: {row.observed_detail}")
            if not row.matched and row.gap:
                print(f"        GAP（无判决通路，已登记）: {row.gap}")
            elif not row.matched:
                print("        未登记缺口 ⇒ 负例被放过（计划 §10.7 STOP-14）")

        summary = cc.summarize(results)
        print(f"[controls] total={summary['total']} matched={summary['matched']} "
              f"gaps={len(summary['gaps'])} let_through="
              f"{summary['let_through']}")
        if summary["gaps_without_registration"]:
            print(f"[controls] 未登记缺口: "
                  f"{summary['gaps_without_registration']}", file=sys.stderr)
        frozen = [row.control_id for row in results
                  if cc.FROZEN_OBSERVED.get(row.control_id) != row.observed]
        if frozen:
            print(f"[controls] 观测与冻结表漂移: {frozen}", file=sys.stderr)

        if args.out:
            target = cc.write_report(args.out, results)
            print(f"[controls] report={target}")

        if not summary["ok"] or frozen:
            return EXIT_FAIL
        if len(results) != len(cc.CONTROLS):
            return EXIT_PARTIAL
        return EXIT_OK
    finally:
        if owned and not args.keep:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(_main())
