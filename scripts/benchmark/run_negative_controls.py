#!/usr/bin/env python
"""Task 5 控制运行入口：``python scripts/benchmark/run_negative_controls.py --all``。

逐条跑 golden / 负例 / 十类变异，收集 ``rejected`` 并落三份结果 JSON
（``<bench_root>/negative_controls/``，T5.7）。逻辑在
``orchestrator/benchmark/controls.py``；本文件只做参数解析与打印（薄封装）。

退出码：全部按预期判过 → 0；任一负例/变异被放过或 golden 被判负 → 1；
**部分执行（``--task`` 不跑变异，判据缺失）→ 2**（评审 W3-13：未执行不得
与「通过」同码）；夹具/环境不合法（基建问题）→ 3。
"""
from __future__ import annotations

import sys
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
    from orchestrator.benchmark import controls  # noqa: E402

    parser = argparse.ArgumentParser(
        prog="scripts/benchmark/run_negative_controls.py",
        description="Task 5：golden smoke + 20 负例 + 10 变异（0 付费）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="跑全部控制")
    group.add_argument("--task", action="append", default=None,
                       help="只跑某任务的 golden/负例（可重复；不跑变异）")
    parser.add_argument("--out-root", default=None,
                        help="结果目录（缺省 <bench_root>/negative_controls）")
    args = parser.parse_args(argv)

    try:
        report = controls.run_controls(out_root=args.out_root, tasks=args.task)
    except (controls.ControlError, OSError) as exc:
        print(f"[controls] 无法运行：{exc}")
        return EXIT_STOP

    summary = report.summary()
    print(f"[controls] golden_passed={summary['golden_passed']} "
          f"negative_rejected={summary['negative_rejected']} "
          f"mutation_rejected={summary['mutation_rejected']}")
    for row in report.negatives["rows"]:
        mark = "REJECT" if row["rejected"] else "NOT-REJECTED"
        print(f"[controls] {mark} {row['case_id']:<28} "
              f"{row['actual_status']:<18} {row['reason'][:96]}")
    for row in report.mutations["rows"]:
        mark = "REJECT" if row["rejected"] else "NOT-REJECTED"
        print(f"[controls] {mark} {row['mutation']:<28} "
              f"{row['actual_status']:<18} {row['reason'][:96]}")
    print(f"[controls] wrote {report.out_root}")
    if report.mutations.get("partial_skip"):
        print("[controls] 部分执行：变异未跑（判据缺失），不计为通过")
        return EXIT_PARTIAL
    return EXIT_OK if summary["all_passed"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(_main())
