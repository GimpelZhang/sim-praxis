#!/usr/bin/env python
"""Phase 2.8.1：把 Phase 2.8.0 的钉死参考运行快照成 ``$ROOT/references/<task>/``。

计划 §5.7 步骤 4 要求用评测器重评三个参考运行，重评的输入是
``$ROOT/references/<task>`` —— 即 **Trial 形态**（``delivered/`` + 出生件 +
运行器遥测 + 清单）。Phase 2.8.0 落盘的是运行形态，因此这一步做搬运与包装：
实现只有一份，在 :mod:`orchestrator.benchmark.phase281.references`。

钉死身份由 Phase 2.8.0 的 ``closeout/run_identity.json`` 解析
（``orchestrator.benchmark.paths.pinned_run``：多候选且无登记即抛错，禁止
「字典序最大者悄悄顶替被判对象」）。三个任务的 ``run_manifest.status`` 必须
是 ``PASS``——参考运行若本身没过，包装它做 parity 等于拿坏答案当标准答案。

用法::

    $PY -I scripts/benchmark/phase281_references.py --root $ROOT
    $PY -I scripts/benchmark/phase281_references.py --root $ROOT --task icy_ground_dr_v1

**0 付费**：只读本地文件，不触网、不调模型。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark import paths as p0_paths          # noqa: E402
from orchestrator.benchmark.phase281 import paths            # noqa: E402
from orchestrator.benchmark.phase281 import references       # noqa: E402

TASKS = ("mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_references",
        description="把 Phase 2.8.0 参考运行包成 $ROOT/references/<task>（0 付费）")
    ap.add_argument("--root", default=None, help="Phase 2.8.1 数据根（缺省按 env 解析）")
    ap.add_argument("--phase0-root", default=None,
                    help="Phase 2.8.0 数据根（缺省 roadmap_phase0）")
    ap.add_argument("--task", action="append", choices=TASKS, default=None,
                    help="只处理该任务（可重复；缺省三个全做）")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印摘要")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root) if args.root else paths.phase281_root()
    phase0 = (Path(args.phase0_root) if args.phase0_root
              else p0_paths.bench_root(read_only=True))
    tasks = args.task or list(TASKS)

    rows = []
    for task_id in tasks:
        run = p0_paths.pinned_run(phase0, task_id)
        summary = references.build_reference(root, task_id, run)
        if summary["run_status"] != "PASS":
            print(f"[phase281-references] {task_id}: 参考运行 status="
                  f"{summary['run_status']!r} ≠ PASS，拒绝包装", file=sys.stderr)
            return 1
        rows.append(summary)

    receipt = paths.analysis_dir() / "references_build.json"
    references.write_json(receipt, {
        "schema": "simpraxis.phase281.references_build.v1",
        "phase0_root": str(phase0),
        "root": str(root),
        "references": rows,
    })
    if args.json:
        print(json.dumps({"references": rows, "receipt": str(receipt)},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"[phase281-references] {row['task_id']} ← {row['run_id']} "
                  f"payload={row['payload_files']} 件 birth={row['birth_status']} "
                  f"→ {row['target']}")
        print(f"[phase281-references] 回执：{receipt}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
