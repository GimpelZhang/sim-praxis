"""R1.3 T1 维护侧参考可测性演练（真实物理，零模型 POST）。

Plan §2：参考解在**独立维护根**验证方法与可测性；§9.1 的完整维护参考链在 T5，
这里只做最小可测性检查：

* 每个留出任务取 2 个 pilot block × 全部条件，走正式 ``build_host_spec`` →
  ``PhysicsBackend`` → 真容器；
* 用宿主判据同一实现（``judge._metric_a/_metric_b``）计算指标；
* 逐 block 计算主对比（含 2×2 交互），确认数值有限、分母完整、可估计；
* 记镜像 digest、任务/任务集 sha、容器残留前后对照；不写任何候选可见根。

不改任何科学阈值/δ；不做"必须正向"的筛选——只验证**可测**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import task_specs as TS

PILOT_BLOCKS = ("pilot-b1", "pilot-b2")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_task(task: TS.TaskSpecV13, *, out: Path, stamp: str) -> dict:
    build = time.time()
    specs = []
    by_run = {}
    for block in PILOT_BLOCKS:
        for cond in task.conditions:
            spec = E.build_host_spec(
                plan_sha256=task.sha256, study_id=task.mechanism_id.split("_")[0],
                condition_id=cond.id, block_id=block,
                scene_id=cond.scene_id, params=dict(cond.params))
            specs.append(spec)
            by_run[spec.run_id] = cond
    book = E.ResourceBook(max_runs=len(specs),
                          max_steps=sum(s.nsteps for s in specs))
    be = BK.PhysicsBackend(
        observations_root=out / "observations",
        evidence_root=out / "evidence_pack",
        work_root=out / "work" / stamp,
        runner=SB.DockerRunner(), image=BASE_IMAGE,
        batch_id=f"T1-TESTABILITY-{task.mechanism_id}")
    ev = E.execute_batch(specs, be, book)
    metrics = {}
    if not ev.blocked:
        for outcome in ev.outcomes:
            if not outcome.ok or outcome.record is None:
                continue
            cond = by_run[outcome.run_id]
            if task.mechanism_id == "A_contact_friction":
                meta = be.metas[outcome.run_id]
                m = J._metric_a(outcome.record,
                                initial_x_m=meta["initial_x_m"])["values"]
                metrics[outcome.run_id] = m["displacement_x_m"]
            else:
                amp = cond.params["target_amplitude_rad"]
                m = J._metric_b(outcome.record,
                                amplitude_rad=amp)["values"]
                metrics[outcome.run_id] = m["normalized_tracking_rmse"]
    # 逐 block 线性对比
    values: "dict[str, dict[str, float]]" = {b: {} for b in PILOT_BLOCKS}
    spec_by_run = {s.run_id: s for s in specs}
    for run_id, value in metrics.items():
        spec = spec_by_run[run_id]
        values.setdefault(spec.block_id, {})[spec.condition_id] = value
    effects = {}
    try:
        effects = task.primary.block_effects(values)
    except Exception as exc:  # noqa: BLE001 - 记录失败而不是崩溃
        effects = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "task_id": task.task_id,
        "task_sha256": task.sha256,
        "primary": task.primary.to_dict(),
        "specs": len(specs),
        "runs_ok": ev.n_ok,
        "blocked": ev.blocked,
        "reason": ev.reason,
        "resources": book.to_dict(),
        "metrics": metrics,
        "block_effects": effects,
        "estimable": (not ev.blocked and ev.n_ok == len(specs)
                      and isinstance(effects, dict) and "error" not in effects
                      and all(v == v for v in
                              (x for x in metrics.values()))),
        "wall_seconds": round(time.time() - build, 2),
        "manifest": (out / "observations" /
                     f"T1-TESTABILITY-{task.mechanism_id}" /
                     "manifest.json").exists(),
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="R1.3 T1 参考可测性（真实物理）")
    parser.add_argument("--taskset", required=True,
                        help="T1 产物目录（task_a.json/task_b.json）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    taskset = Path(args.taskset)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else (
        RP.rsi_r13_root() / "maintenance" / "t1_testability" / stamp)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_a = TS.TaskSpecV13.from_dict(
        json.loads((taskset / "task_a.json").read_text(encoding="utf-8")))
    task_b = TS.TaskSpecV13.from_dict(
        json.loads((taskset / "task_b.json").read_text(encoding="utf-8")))
    before = SB.DockerRunner().run(
        ["docker", "ps", "-aq"], timeout_s=20, container="t1-probe")
    results = [_run_task(task, out=out / task.task_id, stamp=stamp)
               for task in (task_a, task_b)]
    after = SB.DockerRunner().run(
        ["docker", "ps", "-aq"], timeout_s=20, container="t1-probe")
    report = {
        "schema": "simpraxis.rsi_r13.t1_testability.v1",
        "schema_version": 1,
        "captured_at_utc": stamp,
        "image": BASE_IMAGE,
        "taskset_manifest_sha256": _sha(taskset / "taskset_manifest.json"),
        "resource_plan_sha256": _sha(taskset / "resource_plan.json"),
        "blocks": list(PILOT_BLOCKS),
        "containers_before": len((before.stdout_tail or "").split()),
        "containers_after": len((after.stdout_tail or "").split()),
        "results": results,
        "all_estimable": all(r["estimable"] for r in results),
        "paid_posts": 0,
        "note": ("维护侧可测性：不筛选方向、不调阈值；只证明方法与分母可用。"
                 "完整 32 block 参考链在 T5 执行。"),
    }
    text = json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    target = out / "testability_report.json"
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)
    print(json.dumps({
        "report": str(target),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "all_estimable": report["all_estimable"],
        "containers_before": report["containers_before"],
        "containers_after": report["containers_after"],
        "paid_posts": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["all_estimable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
