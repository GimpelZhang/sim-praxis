"""R1.3 T5 离线生产验证（真实物理、零模型 POST）。

沿生产装配面（``CampaignDispatcher`` + 真容器 PhysicsBackend + 真 analysis
receipt + judge_v13）跑两留出任务的**维护参考链**：

* 32 block 确认（A 5 条件 160 run / B 4 条件 128 run）；
* 冻结分析的候选模板真执行（sandbox 视图 + 真 python）；
* 诊断与研究报告合同；
* 独立物理 replay：同一冻结计划/分配再执行一遍，逐 run 比对指标与数组哈希。

本脚本不含模型调用；它证明的是「公开模板→执行→判据」这条路径可用，不是
候选能力，也不是科学结论。产物落 ``<out>/``，含每一步的 pass/fail 与哈希。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import judge_v13 as JV
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol_r13 as PR
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS
from tests.rsi_r1_helpers import make_ctx, make_dirs


@dataclass
class Ctx:
    root: Path
    ctx: Any
    state: Any
    dispatcher: Any
    study: str
    task: TS.TaskSpecV13
    backend: Any = None


def _dispatch(c, action: L.Action):
    rid = f"t5-{_dispatch.n}"
    _dispatch.n += 1
    c.state.journal.append("reply", {"response_id": rid, "text": "{}",
                                     "study": c.study})
    c.state.record_action(response_id=rid, action=action.to_doc(),
                          study=c.study)
    return c.dispatcher.execute_once(action, None)


_dispatch.n = 0


def _act(name: str, doc: Mapping[str, Any]) -> L.Action:
    return L.Action(name=name, doc={"action": name, **dict(doc)})


def _setup(task: TS.TaskSpecV13, root: Path, envelope: str) -> Ctx:
    ctx = make_ctx(root, envelope)
    make_dirs(root)
    for extra in ("public", "analysis", "private", "claim", "judge",
                  "design_audits", "reports", "replay"):
        (root / extra).mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = C.default_budget_v2(envelope)
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/envelope.json", {
        "schema": P.ENVELOPE_SCHEMA_V13, "schema_version": 1,
        "profile_id": P.PROFILE_R13, "envelope_id": envelope,
        "mode": "zero_paid", "budget_sha256": budget.sha256})
    ctx.write_json(A.PUBLIC_CONTRACT_RELPATH, {
        "schema": P.PUBLIC_CONTRACT_SCHEMA_V13, "schema_version": 1})
    state = S.ResearchState(ctx)
    for target in (S.ROOT_AUTHORIZED, S.ROOT_CALIBRATED, S.ROOT_FROZEN,
                   S.ROOT_PROBED, S.ROOT_PRACTICED):
        state.advance_root(target, reason="R1.3 T5 离线验证")
    spend = A.SpendContract(mode=A.MODE_ZERO_PAID)
    admission = A.CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                    lock_sha256="a" * 64, spend=spend,
                                    lane=f"study_{task.mechanism_id[0]}")

    def factory(batch_id: str):
        return BK.PhysicsBackend(
            observations_root=ctx.subdir("observations"),
            evidence_root=ctx.subdir("evidence_pack"),
            work_root=ctx.subdir("trials") / f"work-{batch_id}",
            runner=SB.DockerRunner(), image=BASE_IMAGE, batch_id=batch_id)

    dispatcher = A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study=task.mechanism_id.split("_")[0], runner=SB.DockerRunner(),
        image=BASE_IMAGE, backend_factory=factory,
        profile=P.PROFILE_R13_DEF, task=task, batch_workers=8)
    return Ctx(root=root, ctx=ctx, state=state, dispatcher=dispatcher,
               study=task.mechanism_id.split("_")[0], task=task)


def _report(c, out: Path, name: str, doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path = out / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode()).hexdigest()


def run_chain(task: TS.TaskSpecV13, root: Path, out: Path,
              envelope: str) -> "dict[str, Any]":
    t0 = time.time()
    c = _setup(task, root, envelope)
    checks: "list[dict[str, Any]]" = []

    def check(cid, ok, detail=""):
        checks.append({"id": cid, "ok": bool(ok), "detail": str(detail)[:400]})

    design = PR.DESIGN_TEMPLATE_R13
    analysis = PR.ANALYSIS_TEMPLATE_R13
    explore = PR._plan_template(task, phase="explore")
    i_explore = PR._intent_template(task, role="distributional_exploration")
    i_confirm = PR._intent_template(task, role="confirmation")

    out_doc = _dispatch(c, _act("write_artifact",
                                {"kind": "design", "source": design}))
    check("write-design", out_doc.get("ok"), out_doc.get("code"))
    design_sha = out_doc.get("source_sha256")
    out_doc = _dispatch(c, _act("propose_plan",
                                {"plan": explore, "design_intent": i_explore}))
    check("propose-plan", out_doc.get("ok"), out_doc.get("code"))
    plan_sha = out_doc.get("plan_sha256")
    out_doc = _dispatch(c, _act("run_batch", {
        "plan_sha256": plan_sha, "design_sha256": design_sha,
        "phase": "explore"}))
    check("run-batch", out_doc.get("ok"), out_doc.get("code"))
    run_ids = out_doc.get("run_ids") or []
    out_doc = _dispatch(c, _act("write_artifact",
                                {"kind": "analysis", "source": analysis}))
    check("write-analysis", out_doc.get("ok"), out_doc.get("code"))
    analysis_sha = out_doc.get("source_sha256")
    out_doc = _dispatch(c, _act("analyze", {
        "source_sha256": analysis_sha, "run_ids": run_ids}))
    check("analyze-explore", out_doc.get("ok"), out_doc.get("code"))
    confirm = PR._plan_template(task, phase="confirm")
    confirm["parent_sha"] = "host"
    confirm["design_source_sha"] = design_sha
    confirm["analysis_source_sha"] = analysis_sha
    out_doc = _dispatch(c, _act("freeze_confirm", {
        "plan_sha256": "host", "design_sha256": design_sha,
        "analysis_sha256": analysis_sha, "plan": confirm,
        "design_intent": i_confirm}))
    check("freeze-confirm", out_doc.get("ok"), out_doc.get("code"))
    token = out_doc.get("confirm_token")
    out_doc = _dispatch(c, _act("run_confirm", {"confirm_token": token}))
    check("run-confirm-32block", out_doc.get("ok"), out_doc.get("code"))
    if not out_doc.get("ok"):
        return _report(c, out, f"t5_{c.study}.json",
                       {"study": c.study, "checks": checks, "blocked": True})
    cand = out_doc["candidate_output"]["contrasts"][0]
    index = out_doc["evidence_index"]
    receipt_sha = out_doc["analysis_receipt"]["sha256"]

    def loc(file, pointer):
        return {"kind": "json_pointer", "file": file,
                "sha256": index[file], "pointer": pointer}

    report = {
        "schema": "simpraxis.rsi_r13.research_report.v1", "schema_version": 1,
        "task_id": task.task_id,
        "question_and_hypotheses": "维护参考：任务级交互是否可估",
        "methods_and_assignment_responsibility": "宿主逐 block 采样 nuisance",
        "primary_results": [{"contrast_id": task.primary.id,
                             "analysis_receipt_sha": receipt_sha,
                             "result_locator": loc("analysis/output.json",
                                                   "/diagnostics/contrasts/0/effect")}],
        "heterogeneity_and_scope": [{
            "claim": "仅覆盖登记条件",
            "evidence_refs": [loc("confirm/blocks.json", "/blocks/0")],
            "applicability": "本任务登记条件"}],
        "explore_confirm_comparison": {
            "estimands": task.primary.id, "comparable": False,
            "explanation": "探索覆盖设计空间；确认是宿主抽样",
            "evidence_refs": [loc("analysis/output.json", "/diagnostics")]},
        "alternative_explanations": ["数值积分差异"],
        "limitations": [{
            "statement": "仅本任务条件",
            "evidence_refs": [loc("confirm/blocks.json", "/blocks/0")],
            "consequence": "不外推"}],
        "next_steps": [{"unresolved_question": "端点是否饱和",
                        "discriminating_experiment": "增加端点条件",
                        "possible_outcomes": ["继续增大", "饱和"]}],
    }
    claims = [{"hypothesis_id": "h_interaction",
               "proposition": task.primary.proposition,
               "metric_id": task.primary.metric,
               "delta": float(task.primary.delta),
               "estimate": cand["estimate"], "lower": cand["lower"],
               "upper": cand["upper"], "n_blocks": cand["n_blocks"]}]
    out_doc = _dispatch(c, _act("submit_claim", {
        "claims": claims, "report_text": "R1.3 T5 维护参考链",
        "research_report": report}))
    check("submit-claim", out_doc.get("ok"), out_doc.get("code"))
    check("judge-ok", out_doc.get("judge_verdict") == "JUDGE_OK",
          out_doc.get("judge_verdict"))

    # ---- 独立物理 replay（同一冻结计划/分配，另一次执行） ----
    plan = c.dispatcher._current_plan(phase="confirm")  # noqa: SLF001
    manifest = c.dispatcher.confirm_manifest
    specs = J.build_specs(plan, manifest)
    ordered = [specs[rid] for rid in sorted(specs)]
    backend = BK.PhysicsBackend(
        observations_root=c.ctx.subdir("observations"),
        evidence_root=c.ctx.subdir("evidence_pack"),
        work_root=c.ctx.subdir("trials") / "work-replay",
        runner=SB.DockerRunner(), image=BASE_IMAGE,
        batch_id=f"replay-{c.study}")
    book = E.ResourceBook(max_runs=len(ordered),
                          max_steps=sum(s.nsteps for s in ordered))
    ev = E.execute_batch(ordered, backend, book, workers=8)
    check("replay-executed", not ev.blocked, ev.reason)
    comparison = {"study_id": c.study, "task_id": task.task_id,
                  "plan_sha256": plan.sha256,
                  "manifest_sha256": manifest.sha256,
                  "runs": len(ordered), "identical": 0, "mismatched": []}
    if not ev.blocked:
        orig = c.dispatcher.confirm_artifacts
        for outcome in ev.outcomes:
            o = orig.get(outcome.run_id) or {}
            rec_o = o.get("record") or {}
            rec_n = outcome.record or {}
            same = (rec_o.get("qpos") == rec_n.get("qpos")
                    and rec_o.get("qvel") == rec_n.get("qvel"))
            if same:
                comparison["identical"] += 1
            else:
                comparison["mismatched"].append(outcome.run_id)
        comparison["manifest_sha256_replay"] = (
            (backend.write_manifest() or {}).get("manifest_sha256")
            if hasattr(backend, "write_manifest") else None)
    check("replay-identical", comparison["identical"] == len(ordered),
          f"{comparison['identical']}/{len(ordered)}")
    _report(c, out, f"replay_comparison_{c.study}.json", comparison)

    doc = {
        "schema": "simpraxis.rsi_r13.t5_study.v1",
        "schema_version": 1,
        "study": c.study, "task_id": task.task_id,
        "checks": checks,
        "all_ok": all(c_["ok"] for c_ in checks),
        "primary": {"contrast_id": task.primary.id,
                    "estimate": cand["estimate"], "lower": cand["lower"],
                    "upper": cand["upper"], "status": cand["status"]},
        "containers_left": len([x for x in SB.DockerRunner().run(
            ["docker", "ps", "-aq"], timeout_s=20,
            container="t5-check").stdout_tail.split()]),
        "wall_seconds": round(time.time() - t0, 1),
        "paid_posts": 0,
    }
    sha = _report(c, out, f"t5_{c.study}.json", doc)
    doc["report_sha256"] = sha
    return doc


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--taskset", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--study", choices=["A", "B", "both"], default="both")
    args = ap.parse_args(argv)
    taskset = Path(args.taskset)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else (
        Path("/mnt/artifacts/simpraxis/rsi_r13/t5_validation") / stamp)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_a = TS.TaskSpecV13.from_dict(
        json.loads((taskset / "task_a.json").read_text(encoding="utf-8")))
    task_b = TS.TaskSpecV13.from_dict(
        json.loads((taskset / "task_b.json").read_text(encoding="utf-8")))
    results = []
    for task in ((task_a, task_b) if args.study == "both"
                 else (task_a,) if args.study == "A" else (task_b,)):
        env = f"r13-t5-{task.mechanism_id[:1].lower()}-{stamp.lower()}"
        root = out / "roots" / env
        results.append(run_chain(task, root, out, env))
    summary = {"schema": "simpraxis.rsi_r13.t5_summary.v1",
               "schema_version": 1, "captured_at_utc": stamp,
               "studies": results,
               "all_ok": all(r.get("all_ok") for r in results),
               "paid_posts": 0}
    path = out / "t5_summary.json"
    path.write_text(json.dumps(summary, indent=1, sort_keys=True,
                               ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps({"summary": str(path),
                      "all_ok": summary["all_ok"],
                      "results": [{"study": r.get("study"),
                                   "all_ok": r.get("all_ok"),
                                   "wall": r.get("wall_seconds")}
                                  for r in results]}, ensure_ascii=False))
    return 0 if summary["all_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
