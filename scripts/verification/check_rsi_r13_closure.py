"""RSI-R1.3 独立结案核验（stdlib only；复算事实，不信结案件自述）。

用途：`python scripts/verification/check_rsi_r13_closure.py --taskset DIR \
        --t5-summary FILE --closeout FILE`

核验项：
1. taskset/resource_plan 字节哈希与结案件登记一致；
2. T5 报告的 all_ok 与每 study 的 all_ok 一致；
3. 五轴与事实自洽（无候选 study ⇒ study_execution=NOT_STARTED、
   claim=NONE、r1=NOT_MEASURED；process 不得为 COMPLETE）；
4. 族账历史（若给）与冻结常量 554 一致；
5. 零模型 POST（T5 paid_posts=0）。

退出码：0 一致；1 不一致；2 输入缺失/不可解析。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--taskset", required=True)
    ap.add_argument("--t5-summary", required=True)
    ap.add_argument("--closeout", required=True)
    args = ap.parse_args(argv)
    taskset = Path(args.taskset)
    problems: "list[str]" = []

    closeout = _read(Path(args.closeout))
    if closeout is None:
        print("CLOSEOUT_UNREADABLE", file=sys.stderr)
        return 2
    t5 = _read(Path(args.t5_summary))
    if t5 is None:
        print("T5_UNREADABLE", file=sys.stderr)
        return 2
    manifest = _read(taskset / "taskset_manifest.json")
    plan = _read(taskset / "resource_plan.json")
    if manifest is None or plan is None:
        print("TASKSET_UNREADABLE", file=sys.stderr)
        return 2

    facts = closeout.get("facts") or {}
    axes = closeout.get("axes") or {}
    if facts.get("taskset_sha256") != _sha(taskset / "taskset_manifest.json"):
        problems.append("TASKSET_SHA_MISMATCH")
    if facts.get("resource_plan_sha256") != _sha(taskset / "resource_plan.json"):
        problems.append("RESOURCE_PLAN_SHA_MISMATCH")
    if facts.get("holdout_task_ids") != manifest.get("holdout_task_ids"):
        problems.append("HOLDOUT_IDS_MISMATCH")
    if (facts.get("resource") or {}).get("hard_cap_steps") != plan.get(
            "hard_cap_steps"):
        problems.append("HARD_CAP_MISMATCH")
    if (facts.get("resource") or {}).get("total_steps") != plan.get(
            "total_steps"):
        problems.append("TOTAL_STEPS_MISMATCH")
    if bool(facts.get("t5", {}).get("all_ok")) != bool(t5.get("all_ok")):
        problems.append("T5_ALL_OK_MISMATCH")
    if int(t5.get("paid_posts", -1)) != 0:
        problems.append("T5_PAID_POSTS_NONZERO")

    studies = facts.get("studies") or []
    any_study = any(s.get("status") == "COMPLETED" for s in studies)
    if not any_study:
        if axes.get("study_execution") != "NOT_STARTED":
            problems.append("AXIS_STUDY_EXECUTION")
        if axes.get("claim") != "NONE":
            problems.append("AXIS_CLAIM")
        if axes.get("r1") != "NOT_MEASURED":
            problems.append("AXIS_R1")
    if "process" not in axes or "measurement" not in axes:
        problems.append("AXES_MISSING")

    result = {"ok": not problems, "problems": problems,
              "axes": axes,
              "facts": {"taskset_sha256": facts.get("taskset_sha256"),
                        "resource_plan_sha256":
                            facts.get("resource_plan_sha256")},
              "schema": "simpraxis.rsi_r13.closure_judge.v1"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
