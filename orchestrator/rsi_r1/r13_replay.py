"""R1.3 T8 独立复跑：真实物理 + 冻结分析重执行（零模型 POST）。

对每个 study：读冻结的 confirm 计划/分配 → 重新构造 specs → 新批次真容器
执行 → 与确认批逐 run 比较 qpos/qvel；再用**同一冻结分析源码**在复跑视图上
重算，比较 contrasts/diagnostics。写 ``replay/comparison_<study>.json``。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS


class Replay13Error(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_replay(ctx: Any, study: str, task: TS.TaskSpecV13) -> "dict[str, Any]":
    plan = C.ResearchPlan.from_dict(ctx.read_json(f"confirm/plan_{study}.json"))
    manifest = E.manifest_from_doc(
        ctx.read_json(f"confirm/manifest_{study}.json"))
    specs = J.build_specs(plan, manifest)
    ordered = [specs[rid] for rid in sorted(specs)]
    backend = BK.PhysicsBackend(
        observations_root=ctx.subdir("observations"),
        evidence_root=ctx.subdir("evidence_pack"),
        work_root=ctx.subdir("trials") / "work-replay",
        runner=SB.DockerRunner(), image=BASE_IMAGE,
        batch_id=f"replay-{study}")
    book = E.ResourceBook(len(ordered), sum(s.nsteps for s in ordered))
    ev = E.execute_batch(ordered, backend, book, workers=8)
    comparison: "dict[str, Any]" = {
        "schema": "simpraxis.rsi_r13.replay_comparison.v1",
        "schema_version": 1, "envelope_id": ctx.envelope_id,
        "study": study, "task_id": task.task_id,
        "plan_sha256": plan.sha256, "manifest_sha256": manifest.sha256,
        "runs": len(ordered), "identical": 0, "mismatched": [],
        "analysis_identical": False,
    }
    if ev.blocked:
        comparison["blocked"] = ev.reason
        comparison["ok"] = False
        ctx.write_json(f"replay/comparison_{study}.json", comparison)
        return comparison
    # 确认批原始记录
    base = ctx.subdir("observations")
    original = {}
    for run_id in specs:
        hits = list(base.glob(f"*/{run_id}.jsonl"))
        if not hits:
            raise Replay13Error("RAW_MISSING", run_id)
        original[run_id] = json.loads(hits[0].read_text(encoding="utf-8"))
    for outcome in ev.outcomes:
        rec_o = original.get(outcome.run_id) or {}
        rec_n = outcome.record or {}
        if (rec_o.get("qpos") == rec_n.get("qpos")
                and rec_o.get("qvel") == rec_n.get("qvel")):
            comparison["identical"] += 1
        else:
            comparison["mismatched"].append(outcome.run_id)
    backend.write_manifest()
    # 冻结分析复跑
    lock = S.ResearchState(ctx).confirm_lock(study)
    source_sha = str(lock.analysis_source_sha256)
    source = ctx.relative(f"designs/{source_sha}.py").read_text(encoding="utf-8")
    entries: "list[SB.ViewEntry]" = []
    rdir = base / f"replay-{study}"
    for run_id in sorted(specs):
        entries.append(SB.ViewEntry(f"replay-{study}/{run_id}.jsonl",
                                    _sha_file(rdir / f"{run_id}.jsonl")))
        entries.append(SB.ViewEntry(f"replay-{study}/{run_id}.meta.json",
                                    _sha_file(rdir / f"{run_id}.meta.json")))
    extra = A.analysis_host_inputs(ctx, plan)
    task_view = {"task_id": task.task_id,
                 "study_id": task.mechanism_id.split("_")[0],
                 "conditions": [c.to_dict() for c in task.conditions],
                 "primary": task.primary.to_dict(),
                 "nuisance_schema": dict(task.nuisance_schema),
                 "negative_control_spec": task.negative_control_spec}
    extra = extra + (("task.json", (json.dumps(
        task_view, sort_keys=True, ensure_ascii=False) + "\n").encode()),)
    ev2 = SB.run_analysis(
        source, entries, evidence_root=ctx.subdir("observations"),
        work_root=ctx.subdir("trials") / "work-replay-analysis",
        runner=SB.DockerRunner(), image=BASE_IMAGE, retain_bytes=True,
        extra_files=extra)
    try:
        replay_out = json.loads(ev2.blobs["analysis.json"].decode("utf-8"))
        original_out = json.loads(ctx.relative(
            f"private/analysis/{study}-confirm.raw.json").read_text(
                encoding="utf-8"))
        same = (replay_out.get("contrasts") == original_out.get("contrasts")
                and replay_out.get("diagnostics")
                == original_out.get("diagnostics"))
        comparison["analysis_identical"] = same
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        comparison["analysis_detail"] = f"{type(exc).__name__}: {exc}"
    comparison["ok"] = (comparison["identical"] == len(ordered)
                        and comparison["analysis_identical"])
    ctx.write_json(f"replay/comparison_{study}.json", comparison)
    return comparison
