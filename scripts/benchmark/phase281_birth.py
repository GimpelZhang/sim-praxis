#!/usr/bin/env python
"""Phase 2.8.1 出生检查 CLI：``--trial-dir`` / ``--level`` / ``--runtime-only``。

对应计划 §6.6 的步骤 2–3：

```bash
# 2) 生成一份真实 birth.json
python -I scripts/benchmark/phase281_birth.py \\
   --trial-dir <dir> --task mujoco_contact_push_v1 --level ALL --out <json>
# 3) 只做运行时证明（不跑出生）
python -I scripts/benchmark/phase281_birth.py --trial-dir <dir> --runtime-only
```

入口纪律（计划 §6.5 第 3 条）：**建任何目录之前**先调
``assert_no_benchmark_root_env()``——``SIMPRAXIS_BENCHMARK_ROOT`` 一旦误指到
2.8.0 的数据根，后续所有写入都会落错地方，而且指纹件是 ``O_EXCL`` 写的、
一旦写错位置只能人工清目录。

顺序纪律（AC-P281-2-13）：**运行时证明先采集，候选后启动**。本 CLI 在
落 ``birth.json`` 之前先落 ``runtime_attestation.json``，所以「采集时点早于
事件流首事件」在时序上成立，不是靠事后补一张纸。

退出码：``0`` = 出生 ``BORN`` 且运行时 ``PASS``；``3`` = 运行时
``BLOCKED_INFRA``（**不是**能力失败）；``4`` = ``NOT_BORN``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:                       # pragma: no cover - CLI 引导
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281 import candidate_birth as cb   # noqa: E402
from orchestrator.benchmark.phase281 import candidate_runtime as cr  # noqa: E402
from orchestrator.benchmark.phase281.paths import (                  # noqa: E402
    assert_no_benchmark_root_env,
)

EXIT_OK = 0
EXIT_BLOCKED = 3
EXIT_NOT_BORN = 4


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_birth",
        description="Phase 2.8.1 出生隔离 v2 + 每 Trial 运行时证明")
    ap.add_argument("--trial-dir", required=True, help="Trial 目录（birth.json 落这里）")
    ap.add_argument("--task", default=None,
                    help="任务登记名（如 mujoco_contact_push_v1）；运行时证明必需")
    ap.add_argument("--workdir", default=None,
                    help="候选工作目录（缺省 <trial-dir>/delivered）")
    ap.add_argument("--level", default="ALL",
                    choices=("B0", "B1", "B2", "B3", "B4", "ALL"))
    ap.add_argument("--wall-s", type=float, default=cb.LIMITS_DEFAULT.wall_s)
    ap.add_argument("--out", default=None, help="把本次判决件另存一份（0600）")
    ap.add_argument("--runtime-only", action="store_true",
                    help="只做运行时证明，不跑出生")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    return ap


def _write_out(path: "str | None", doc: dict) -> None:
    if not path:
        return
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

    target = Path(path)
    if target.is_symlink():
        raise cb.BirthError(f"{target} 是符号链接：拒绝写入判决件")
    ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", 0o600)


def _runtime(task: "str | None", trial_dir: Path, image: "str | None") -> dict:
    if not task:
        return {"verdict": "SKIPPED", "problems": [],
                "note": "未给 --task：本次不做运行时证明（不得当作已证明）"}
    spec = cb.load_task(task)
    return cr.RuntimeAttestation.gate(spec, trial_dir, image=image)


def main(argv: "list[str] | None" = None) -> int:
    assert_no_benchmark_root_env()
    args = _parser().parse_args(argv)
    trial_dir = Path(args.trial_dir)
    outcome: dict = {"trial_dir": str(trial_dir), "task_id": args.task,
                     "level": args.level}

    outcome["runtime"] = _runtime(args.task, trial_dir, None)
    if args.runtime_only:
        outcome["verdict"] = outcome["runtime"]["verdict"]
        _write_out(args.out, outcome)
        verdict = outcome["runtime"]["verdict"]
        rc = (EXIT_OK if verdict in ("PASS", "SKIPPED") else EXIT_BLOCKED)
    else:
        if not args.task:
            raise cb.BirthError("非 --runtime-only 时必须给 --task（判据来自冻结定义）")
        workdir = Path(args.workdir) if args.workdir else trial_dir / "delivered"
        spec = cb.spec_from_task(args.task, level=args.level,
                                 limits=cb.LimitsV2(wall_s=args.wall_s),
                                 workdir=workdir)
        validator = cb.CandidateBirthValidator(trial_dir=trial_dir,
                                              trial_id=trial_dir.name)
        result = validator.run(spec)
        outcome["birth"] = validator.write(result)
        outcome["birth_doc"] = result.to_json()
        outcome["status"] = result.status
        if outcome["runtime"]["verdict"] == cr.VERDICT_BLOCKED:
            outcome["verdict"] = cr.VERDICT_BLOCKED
            rc = EXIT_BLOCKED
        elif result.status == "BORN":
            outcome["verdict"] = "PASS"
            rc = EXIT_OK
        else:
            outcome["verdict"] = "FAIL"
            rc = EXIT_NOT_BORN
        _write_out(args.out, outcome)

    if args.json:
        printable = {k: v for k, v in outcome.items() if k != "birth_doc"}
        print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        rt = outcome["runtime"]
        print(f"[phase281-birth] runtime={rt['verdict']} problems={len(rt['problems'])}")
        if "status" in outcome:
            print(f"[phase281-birth] birth={outcome['status']} "
                  f"verdict={outcome['verdict']} "
                  f"path={outcome['birth']['birth_path']}")
        else:
            print(f"[phase281-birth] verdict={outcome['verdict']}")
        for problem in outcome.get("problems_all", []) or []:
            print(f"  - {problem}")
    return rc


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
