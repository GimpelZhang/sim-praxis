#!/usr/bin/env python
"""独立评测入口：``python -I scripts/benchmark/evaluate_run.py --task X --run-dir D``。

D4-2 独立进程纪律：评测真正跑在**隔离解释器**里。本入口在非隔离模式下启动时，
先校验启动钩子（``sitecustomize``/``usercustomize``）没有从不受信路径加载，再用
``os.execv`` 带 ``-I`` 自重入（``-I`` 忽略 ``PYTHONPATH``/用户 site 且不把 cwd
放进 ``sys.path``），这样候选目录里的 ``sitecustomize.py`` 之类无法劫持评测器
（AC-T4-8）。

边界：若调用方已经在受污染的解释器里启动了本脚本，启动钩子代码在**本文件被读入
之前**就已执行，此处无法撤销它。因此这种情况一律 fail-closed（rc=3，不产出任何
评测结果），绝不静默给出可能被劫持的判决。

逻辑在 ``orchestrator/benchmark/evaluator.py``；本文件只做参数解析与打印
（Plan T4.4：薄封装）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXIT_OK = 0
EXIT_STOP = 3


def _untrusted_startup_hook() -> "str | None":
    """返回受污染启动钩子的说明；干净环境返回 None。"""
    for name in ("sitecustomize", "usercustomize"):
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            return f"{name} 已加载但没有来源文件"
        resolved = Path(origin).resolve()
        for root in {Path(sys.prefix), Path(sys.base_prefix)}:
            root = root.resolve()
            if resolved == root or root in resolved.parents:
                break
        else:
            return f"{name} 由不受信路径加载：{resolved}"
    return None


def _main(argv: "list[str] | None" = None) -> int:
    import argparse

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from orchestrator.benchmark import evaluator as ev  # noqa: E402
    from orchestrator.benchmark import paths, registry  # noqa: E402

    parser = argparse.ArgumentParser(
        prog="scripts/benchmark/evaluate_run.py",
        description="Task 4：独立评测一次运行（快照式输入 + 硬门 + 七态，0 付费）")
    parser.add_argument("--task", required=True,
                        help="task_id（如 mujoco_contact_push_v1）")
    parser.add_argument("--run-dir", required=True,
                        help="候选运行产物目录（绝对路径）")
    parser.add_argument("--out", default=None,
                        help="输出目录（缺省 <bench_root>/evaluator_results/<run_id>）")
    parser.add_argument("--source-root", default=None,
                        help="评测器源码根（默认仓库根；仅用于自校验测试的副本）")
    args = parser.parse_args(argv)

    try:
        spec = registry.BenchmarkRegistry().get(args.task)
    except Exception as exc:               # noqa: BLE001 - 未登记任务即停
        print(f"[evaluate] 取任务失败：{exc}")
        return EXIT_STOP

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out) if args.out else (
        paths.evaluator_results_dir() / run_dir.name)
    evaluator = ev.Evaluator(
        spec=spec,
        source_root=Path(args.source_root) if args.source_root else None)
    try:
        written = evaluator.evaluate(run_dir, out_dir)
    except (ev.EvaluatorError, OSError) as exc:
        print(f"[evaluate] {spec.task_id}: {exc}")
        return EXIT_STOP

    print(f"[evaluate] task={spec.task_id} run={written['run_id']}")
    print(f"[evaluate] status={written['status']} "
          f"weighted_score={written['weighted_score']:.6f}")
    for name, dim in written["dimensions"].items():
        print(f"[evaluate] dim {name:<28} weight={dim['weight']:.3f} "
              f"score={dim['score']:.6f}")
    print(f"[evaluate] hard_gates={written['hard_gate_ids'] or '[]'}")
    print(f"[evaluate] gates_checked={written['gates_checked']}")
    print(f"[evaluate] not_applicable_gates="
          f"{written['not_applicable_gates'] or '无'}")
    print(f"[evaluate] wrote {written['result_path']}")
    return EXIT_OK


if __name__ == "__main__":
    if not sys.flags.isolated:
        contaminated = _untrusted_startup_hook()
        if contaminated:
            print(f"拒绝在受污染的启动环境里评测：{contaminated}；"
                  "请改用 python -I 调用本入口。", file=sys.stderr)
            raise SystemExit(EXIT_STOP)
        os.execv(sys.executable,
                 [sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
    raise SystemExit(_main())
