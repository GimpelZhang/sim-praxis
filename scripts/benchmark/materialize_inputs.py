#!/usr/bin/env python
"""Task 6 输入 stage：把 KB 技能/方案逐件复制到私有根并写 sha256 清单。

用法::

    python scripts/benchmark/materialize_inputs.py --task mujoco_contact_push_v1
    python scripts/benchmark/materialize_inputs.py --all

**不含任何解题逻辑**（不影响物理/环境/模板，只做复制 + 逐件复算 sha256）：

- 技能：``load_skills_manifest()`` 的清单口径 → ``<bench_root>/private/skills_stage/<sha12>/``；
- 方案：``solutions.resolver.resolve_solution().stage()`` → ``<bench_root>/private/solutions_stage/<solution_id>/``。

两者都落 ``private/``（0700，永不入 git），并把逐件 sha256 汇总写进
``<bench_root>/private/input_manifest.json``。重复执行幂等：已存在且 sha 相符即跳过，
不符即 fail-closed（staged 副本被改写必须人看）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXIT_OK = 0
EXIT_STOP = 3

#: 每个任务需要哪些输入（task_id → 需要的 kind 列表）
INPUTS_BY_TASK = {
    "mujoco_contact_push_v1": ("skills",),
    "libero_env_adapter_v1": ("skills", "solution"),
    "icy_ground_dr_v1": (),
}


def _main(argv: "list[str] | None" = None) -> int:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from orchestrator.benchmark import reference as ref  # noqa: E402

    parser = argparse.ArgumentParser(
        prog="scripts/benchmark/materialize_inputs.py",
        description="Task 6：stage KB 技能/方案到私有根（只复制，不含解题逻辑）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", choices=sorted(ref.REFERENCE_SPECS))
    group.add_argument("--all", action="store_true")
    parser.add_argument("--solution-id", default=ref.DEFAULT_LIBERO_SOLUTION,
                        help="LIBERO 方案 id（缺省 %(default)s）")
    parser.add_argument("--json", action="store_true", help="打印完整 JSON 清单")
    args = parser.parse_args(argv)

    tasks = sorted(ref.REFERENCE_SPECS) if args.all else [args.task]
    staged: "dict[str, dict]" = {}
    summary: "dict[str, list[str]]" = {}
    try:
        for task_id in tasks:
            rows: "list[dict]" = []
            for kind in INPUTS_BY_TASK[task_id]:
                if kind not in staged:
                    staged[kind] = (ref.materialize_skills() if kind == "skills"
                                    else ref.materialize_solution(args.solution_id))
                rows.append(staged[kind])
            ref.record_input_manifest(task_id, rows)
            summary[task_id] = [
                f"{row['kind']}({len(row['files'])} 件)" for row in rows] or ["无外部输入"]
        doc = ref._read_json(ref.input_manifest_path())
    except ref.ReferenceError as exc:
        print(f"[materialize] 失败：{exc}", file=sys.stderr)
        return EXIT_STOP

    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for task_id, kinds in sorted(summary.items()):
            print(f"[materialize] {task_id}: {'、'.join(kinds)}")
        print(f"[materialize] 清单 {ref.input_manifest_path()}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(_main())
