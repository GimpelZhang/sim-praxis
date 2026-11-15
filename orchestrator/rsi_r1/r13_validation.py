"""R1.3 T5 生产验证驱动（真实物理/真实沙盒，零模型 POST）。

供 CLI ``validate-production`` 调用：在**已初始化**的 campaign 根上，沿生产
分派器跑两留出任务的维护参考链（32 block 确认 + 独立 replay + 冻结分析 +
judge_v13），并写 ``contract/t5_summary.json``。不构造任何模型 sender。

本模块不创建 envelope/budget；CLI 负责。root 状态为跑通 lane 门会推进到
PRACTICED（仅离线验证根；报告如实记录这是演练身份）。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from orchestrator.agentic.image_pin import BASE_IMAGE
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol_r13 as PR
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS

T5_SUMMARY_REL = "contract/t5_summary.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _act(name: str, doc: Mapping[str, Any]) -> L.Action:
    return L.Action(name=name, doc={"action": name, **dict(doc)})


class _Driver:
    def __init__(self, ctx: Any, study: str, task: TS.TaskSpecV13) -> None:
        self.ctx = ctx
        self.study = study
        self.task = task
        self.n = 0
        self.state = S.ResearchState(ctx)
        budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
        spend = A.SpendContract(mode=A.MODE_OFFLINE_VALIDATION)
        self.admission = A.CampaignAdmission(
            ctx=ctx, state=self.state, budget=budget,
            lock_sha256="0" * 64, spend=spend, lane=f"study_{study}")

        def factory(batch_id: str) -> BK.PhysicsBackend:
            return BK.PhysicsBackend(
                observations_root=ctx.subdir("observations"),
                evidence_root=ctx.subdir("evidence_pack"),
                work_root=ctx.subdir("trials") / f"work-{batch_id}",
                runner=SB.DockerRunner(), image=BASE_IMAGE, batch_id=batch_id)

        self.dispatcher = A.CampaignDispatcher(
            ctx=ctx, state=self.state, admission=self.admission, budget=budget,
            study=study, runner=SB.DockerRunner(), image=BASE_IMAGE,
            backend_factory=factory, profile=P.PROFILE_R13_DEF, task=task,
            batch_workers=8)

    def dispatch(self, action: L.Action) -> Mapping[str, Any]:
        self.n += 1
        rid = f"t5-validation-{self.study}-{self.n}"
        self.state.journal.append("reply", {"response_id": rid, "text": "{}",
                                            "study": self.study})
        self.state.record_action(response_id=rid, action=action.to_doc(),
                                 study=self.study)
        return self.dispatcher.execute_once(action, None)


def _advance_to_practiced(state: Any) -> None:
    for target in (S.ROOT_AUTHORIZED, S.ROOT_CALIBRATED, S.ROOT_FROZEN,
                   S.ROOT_PROBED, S.ROOT_PRACTICED):
        if S.ROOT_ORDER.index(target) > S.ROOT_ORDER.index(state.root_state()):
            state.advance_root(target, reason="R1.3 T5 生产验证（离线演练根）")


def run_study_chain(ctx: Any, state: Any, *, task: TS.TaskSpecV13,
                    out: Path) -> "dict[str, Any]":
    t0 = time.time()
    driver = _Driver(ctx, task.mechanism_id.split("_")[0], task)
    checks: "list[dict[str, Any]]" = []

    def check(cid: str, ok: bool, detail: Any = "") -> None:
        checks.append({"id": cid, "ok": bool(ok), "detail": str(detail)[:400]})

    design = PR.DESIGN_TEMPLATE_R13
    analysis = PR.ANALYSIS_TEMPLATE_R13
    explore = PR._plan_template(task, phase="explore")
    i_explore = PR._intent_template(task, role="distributional_exploration")
    i_confirm = PR._intent_template(task, role="confirmation")

    o = driver.dispatch(_act("write_artifact", {"kind": "design",
                                                "source": design}))
    check("write-design", o.get("ok"), o.get("code"))
    design_sha = o.get("source_sha256")
    o = driver.dispatch(_act("propose_plan", {"plan": explore,
                                              "design_intent": i_explore}))
    check("propose-plan", o.get("ok"), o.get("code"))
    plan_sha = o.get("plan_sha256")
    o = driver.dispatch(_act("run_batch", {
        "plan_sha256": plan_sha, "design_sha256": design_sha,
        "phase": "explore"}))
    check("run-batch", o.get("ok"), o.get("code"))
    run_ids = o.get("run_ids") or []
    o = driver.dispatch(_act("write_artifact", {"kind": "analysis",
                                                "source": analysis}))
    check("write-analysis", o.get("ok"), o.get("code"))
    analysis_sha = o.get("source_sha256")
    o = driver.dispatch(_act("analyze", {"source_sha256": analysis_sha,
                                         "run_ids": run_ids}))
    check("analyze-explore", o.get("ok"), o.get("code"))
    confirm = PR._plan_template(task, phase="confirm")
    confirm["parent_sha"] = "host"
    confirm["design_source_sha"] = design_sha
    confirm["analysis_source_sha"] = analysis_sha
    o = driver.dispatch(_act("freeze_confirm", {
        "plan_sha256": "host", "design_sha256": design_sha,
        "analysis_sha256": analysis_sha, "plan": confirm,
        "design_intent": i_confirm}))
    check("freeze-confirm", o.get("ok"), o.get("code"))
    token = o.get("confirm_token")
    o = driver.dispatch(_act("run_confirm", {"confirm_token": token}))
    check("run-confirm-32block", o.get("ok"), o.get("code"))
    if not o.get("ok"):
        return _finish(driver, checks, task, out, t0, primary=None,
                       replay=None)
    cand = o["candidate_output"]["contrasts"][0]
    index = o["evidence_index"]
    receipt_sha = o["analysis_receipt"]["sha256"]

    def loc(file: str, pointer: str) -> "dict[str, Any]":
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
        "limitations": [{"statement": "仅本任务条件",
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
    o = driver.dispatch(_act("submit_claim", {
        "claims": claims, "report_text": "R1.3 T5 维护参考链",
        "research_report": report}))
    check("submit-claim", o.get("ok"), o.get("code"))
    check("judge-ok", o.get("judge_verdict") == "JUDGE_OK",
          o.get("judge_verdict"))

    replay = _replay(driver, task, out)
    check("replay-identical", replay["identical"] == replay["runs"],
          f"{replay['identical']}/{replay['runs']}")
    check("replay-analysis-identical", replay["analysis_identical"],
          replay.get("analysis_detail"))
    return _finish(driver, checks, task, out, t0, primary=cand, replay=replay)


def _replay(driver: _Driver, task: TS.TaskSpecV13, out: Path
            ) -> "dict[str, Any]":
    plan = driver.dispatcher._current_plan(phase="confirm")  # noqa: SLF001
    manifest = driver.dispatcher.confirm_manifest
    specs = J.build_specs(plan, manifest)
    ordered = [specs[rid] for rid in sorted(specs)]
    backend = BK.PhysicsBackend(
        observations_root=driver.ctx.subdir("observations"),
        evidence_root=driver.ctx.subdir("evidence_pack"),
        work_root=driver.ctx.subdir("trials") / "work-replay",
        runner=SB.DockerRunner(), image=BASE_IMAGE,
        batch_id=f"replay-{driver.study}")
    ev = E.execute_batch(ordered, backend, E.ResourceBook(
        len(ordered), sum(s.nsteps for s in ordered)), workers=8)
    comparison = {"study_id": driver.study, "task_id": task.task_id,
                  "plan_sha256": plan.sha256,
                  "manifest_sha256": manifest.sha256,
                  "runs": len(ordered), "identical": 0, "mismatched": [],
                  "analysis_identical": False}
    if ev.blocked:
        comparison["blocked"] = ev.reason
        return comparison
    original = driver.dispatcher.confirm_artifacts
    for outcome in ev.outcomes:
        rec_o = (original.get(outcome.run_id) or {}).get("record") or {}
        rec_n = outcome.record or {}
        if (rec_o.get("qpos") == rec_n.get("qpos")
                and rec_o.get("qvel") == rec_n.get("qvel")):
            comparison["identical"] += 1
        else:
            comparison["mismatched"].append(outcome.run_id)
    backend.write_manifest()
    # 冻结分析复跑：同一源码、同一视图形态
    from orchestrator.rsi_r1 import assembly as A2
    lock = driver.state.confirm_lock(driver.study)
    source_sha = str(lock.analysis_source_sha256)
    source = (driver.ctx.relative(f"designs/{source_sha}.py")
              .read_text(encoding="utf-8"))
    entries: "list[SB.ViewEntry]" = []
    for run_id in sorted(specs):
        base = driver.ctx.subdir("observations") / f"replay-{driver.study}"
        batch = f"replay-{driver.study}"
        entries.append(SB.ViewEntry(f"{batch}/{run_id}.jsonl",
                                    A2._sha_file(base / f"{run_id}.jsonl")))
        entries.append(SB.ViewEntry(f"{batch}/{run_id}.meta.json",
                                    A2._sha_file(base / f"{run_id}.meta.json")))
    from orchestrator.rsi_r1.assembly import analysis_host_inputs
    extra = analysis_host_inputs(driver.ctx, plan)
    task_view = {
        "task_id": driver.task.task_id,
        "study_id": driver.task.mechanism_id.split("_")[0],
        "conditions": [c.to_dict() for c in driver.task.conditions],
        "primary": driver.task.primary.to_dict(),
        "nuisance_schema": dict(driver.task.nuisance_schema),
        "negative_control_spec": driver.task.negative_control_spec,
    }
    extra = extra + (("task.json", (json.dumps(
        task_view, sort_keys=True, ensure_ascii=False) + "\n").encode()),)
    ev2 = SB.run_analysis(
        source, entries,
        evidence_root=driver.ctx.subdir("observations"),
        work_root=driver.ctx.subdir("trials") / "work-replay-analysis",
        runner=SB.DockerRunner(), image=BASE_IMAGE, retain_bytes=True,
        extra_files=extra)
    try:
        replay_out = json.loads(ev2.blobs["analysis.json"].decode("utf-8"))
        original_out = json.loads(
            driver.ctx.relative(
                f"private/analysis/{driver.study}-confirm.raw.json"
            ).read_text(encoding="utf-8"))
        same = (replay_out.get("contrasts") == original_out.get("contrasts")
                and replay_out.get("diagnostics") == original_out.get(
                    "diagnostics"))
        comparison["analysis_identical"] = same
        comparison["analysis_detail"] = "identical" if same else "differs"
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        comparison["analysis_detail"] = f"{type(exc).__name__}: {exc}"
    return comparison


def _finish(driver: _Driver, checks: "list[dict[str, Any]]",
            task: TS.TaskSpecV13, out: Path, t0: float, *,
            primary: "Mapping[str, Any] | None",
            replay: "Mapping[str, Any] | None") -> "dict[str, Any]":
    doc = {
        "schema": "simpraxis.rsi_r13.t5_study.v1", "schema_version": 1,
        "study": driver.study, "task_id": task.task_id, "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
        "primary": None if primary is None else {
            "contrast_id": task.primary.id,
            "estimate": primary["estimate"], "lower": primary["lower"],
            "upper": primary["upper"], "status": primary["status"]},
        "replay": replay,
        "containers_left": len((SB.DockerRunner().run(
            ["docker", "ps", "-aq"], timeout_s=20,
            container="t5-check").stdout_tail or "").split()),
        "wall_seconds": round(time.time() - t0, 1),
        "paid_posts": 0,
        "identity": "offline_validation_root",
    }
    path = out / f"t5_{driver.study}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    doc["report_sha256"] = _sha(text.encode())
    if replay is not None:
        rp = out / f"replay_comparison_{driver.study}.json"
        rp.write_text(json.dumps(replay, indent=1, sort_keys=True) + "\n",
                      encoding="utf-8")
        rp.chmod(0o600)
    return doc


def _formal_packet_selftest(ctx: Any, task: Any) -> "dict[str, Any]":
    """零 POST：用 T5 参考链的产物构造正式审查包，校验自足性与块覆盖。"""
    from orchestrator.rsi_r1 import r13_review_run as RRUN
    from orchestrator.rsi_r1 import review_runner as RN
    study = str(task.mechanism_id.split("_")[0])
    problems: "list[str]" = []
    try:
        packet, report_doc, values = RRUN.build_formal_packet(ctx, study, task)
        payload = RN.packet_bytes(packet)
    except Exception as exc:  # noqa: BLE001 —— 任何宿主异常都是仪器缺陷
        return {"study": study, "ok": False,
                "problems": [f"{type(exc).__name__}: {exc}"]}
    if not payload:
        problems.append("packet 为空")
    table_blocks = [str(row.get("block_id"))
                    for row in (packet.get("block_table") or [])]
    if sorted(table_blocks) != sorted(values):
        problems.append("包内 block 集与确认批不一致")
    if not report_doc.get("task_id"):
        problems.append("报告缺 task_id")
    if int((packet.get("primary_recomputation") or {}).get("n_blocks")
           or -1) != len(values):
        problems.append("主对比重算的 block 数与包内 block 数不一致")
    # 轮 P（R13-D11）：包的块表效应必须与候选提交的主张（第 7 步 candidate_output
    # 的逐字复制）同值。参考链里候选主张=宿主重算，两者不等只可能是宿主取错
    # 指标/取错 study——这正是轮 O 审查包带错数据（A：stop_time_s）的情形。
    claim_rel = f"claim/submission_{study}.json"
    if not ctx.exists(claim_rel):
        problems.append(f"缺主张提交 {claim_rel}")
    else:
        claim = (ctx.read_json(claim_rel).get("claims") or [{}])[0]
        want = float(claim.get("estimate", "nan"))
        got = float((packet.get("primary_recomputation") or {})
                    .get("effect", "nan"))
        if not (abs(got - want) <= 1e-9):
            problems.append("块表主效应与参考链主张不一致（参考链主张=宿主重算，不等即宿主取错指标/study）")
    return {"study": study, "ok": not problems, "problems": problems,
            "blocks": len(values), "packet_bytes": len(payload),
            "packet_sha256": _sha(payload)}


def run_production_validation(ctx: Any, *, taskset_dir: Path,
                              out: Path) -> "dict[str, Any]":
    """CLI 入口：初始化后的根上跑两任务参考链，写 T5 汇总件。"""
    state = S.ResearchState(ctx)
    _advance_to_practiced(state)
    taskset = Path(taskset_dir)
    task_a = TS.TaskSpecV13.from_dict(json.loads(
        (taskset / "task_a.json").read_text(encoding="utf-8")))
    task_b = TS.TaskSpecV13.from_dict(json.loads(
        (taskset / "task_b.json").read_text(encoding="utf-8")))
    from orchestrator.rsi_r1 import r13_review_run as RRUN
    adapter = RRUN.review_adapter_selftest()
    results = [run_study_chain(ctx, state, task=task, out=out)
               for task in (task_a, task_b)]
    # 轮 O（R13-D9）：正式审查包的**构造面**必须在 T5 就跑到——轮 N 的
    # ``ctx.subdir("reports")`` 宿主异常直到第一个付费审查才暴露（review A 崩，
    # 0 呼）。T5 的参考链走完 8 位后 root 内已有 reports/、confirm/、observations/，
    # 正好可以在零 POST 下逐 study 构造正式包并校验其结构。
    formal = [_formal_packet_selftest(ctx, task) for task in (task_a, task_b)]
    summary = {
        "schema": "simpraxis.rsi_r13.t5_summary.v1", "schema_version": 1,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "studies": results,
        "review_adapter": adapter,
        "review_formal_packets": formal,
        "all_ok": (all(r.get("all_ok") for r in results)
                   and bool(adapter.get("ok"))
                   and all(f.get("ok") for f in formal)),
        "paid_posts": 0,
        "identity": "offline_validation_root",
    }
    path = ctx.relative(T5_SUMMARY_REL)
    text = json.dumps(summary, indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    summary["summary_sha256"] = _sha(text.encode())
    return summary
