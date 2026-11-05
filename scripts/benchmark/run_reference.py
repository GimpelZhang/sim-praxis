#!/usr/bin/env python
"""Task 6 薄封装：跑一次参考实现并交给独立评测器判决（0 付费）。

用法::

    python scripts/benchmark/run_reference.py --task mujoco_contact_push_v1 --seed 0

真实逻辑全在 ``orchestrator/benchmark/reference.py``（``ReferenceRunner``）；
本文件只解析参数、打印结局、可选写一份运行摘要。退出码：``PASS``→0，
其余判决→3（与 ``evaluate_run.py`` 的 ``EXIT_STOP`` 同口径）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXIT_OK = 0
EXIT_STOP = 3


def _main(argv: "list[str] | None" = None) -> int:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from orchestrator.benchmark import reference as ref  # noqa: E402

    parser = argparse.ArgumentParser(
        prog="scripts/benchmark/run_reference.py",
        description="Task 6：执行一次参考实现（私有根）+ 独立评测（0 付费）")
    parser.add_argument("--task", required=True, choices=sorted(ref.REFERENCE_SPECS))
    parser.add_argument("--seed", type=int, default=None,
                        help="缺省取 reference_recipe 登记的首个种子")
    parser.add_argument("--out", default=None,
                        help="把运行摘要写到该 JSON 路径（可选）")
    args = parser.parse_args(argv)

    try:
        outcome = ref.ReferenceRunner(args.task, seed=args.seed).run()
    except ref.ReferenceError as exc:
        print(f"[reference] 失败：{exc}", file=sys.stderr)
        return EXIT_STOP

    summary = {
        "task_id": outcome.task_id,
        "run_id": outcome.run_id,
        "seed": outcome.seed,
        "run_root": str(outcome.root),
        "status": outcome.status,
        "birth_status": outcome.birth_status,
        "fingerprint_sha256": outcome.fingerprint_sha256,
        "run_manifest": str(outcome.manifest_path),
        "evaluation_dir": str(outcome.evaluation_dir),
        "weighted_score": outcome.evaluation.get("weighted_score"),
        "hard_gate_ids": outcome.evaluation.get("hard_gate_ids") or [],
    }
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n", encoding="utf-8")

    print(f"[reference] task={outcome.task_id} run={outcome.run_id} "
          f"status={outcome.status} birth={outcome.birth_status}")
    print(f"[reference] weighted_score={summary['weighted_score']}")
    print(f"[reference] hard_gates={summary['hard_gate_ids'] or '[]'}")
    print(f"[reference] run_root={outcome.root}")
    return EXIT_OK if outcome.status == "PASS" else EXIT_STOP


if __name__ == "__main__":
    raise SystemExit(_main())
