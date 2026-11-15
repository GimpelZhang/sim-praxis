"""RSI-R1.3 端到端（零付费/零模型/合成物理）：设计意图→确认→诊断→报告→判据。

用生产装配面（``CampaignDispatcher`` + 真 sandbox 视图 + 真 analysis receipt）
跑完整八动作，只把 physics backend 换成合成记录。断言：

* 探索矩阵必须真的让 nuisance 变化（占位值被拒）；
* 确认条件必须逐字等于任务登记；
* 主估计量（2×2 交互）由宿主从 raw 重算并与候选输出逐位一致；
* 诊断/报告合同不全即判据拒绝；
* 通过时 verdict=JUDGE_OK 且 state 推 VERIFIED。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest

from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol_r13 as PR
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS
from tests import rsi_r1_helpers as H
from tests.rsi_r1_rehearsal import LocalRunner, _a_record, _b_record

ENVELOPE = "r13test"
IMAGE = "simpraxis/rsi-r13-rehearsal@sha256:" + "e" * 64


@dataclass
class R13Backend:
    observations_root: Path
    batch_id: str
    values: Mapping[str, float]
    entries: "list[Any]" = field(default_factory=list)
    metas: "dict[str, dict[str, Any]]" = field(default_factory=dict)
    records: "dict[str, dict[str, Any]]" = field(default_factory=dict)

    def run(self, spec: E.ExperimentSpec) -> dict:
        if spec.study_id == "A":
            record = _a_record(spec, displacement=self.values[spec.condition_id])
        else:
            record = _b_record(spec, rmse=self.values[spec.condition_id])
        record = O.validate_record(record, scene_id=spec.scene_id)
        base = Path(self.observations_root) / self.batch_id
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        jsonl = base / f"{spec.run_id}.jsonl"
        O.write_record_jsonl(jsonl, record)
        meta = {
            "schema": "simpraxis.rsi_r1.run_meta.v1", "schema_version": 1,
            "batch_id": self.batch_id, "run_id": spec.run_id,
            "spec_hash": spec.spec_hash, "study_id": spec.study_id,
            "scene_id": spec.scene_id, "condition_id": spec.condition_id,
            "block_id": spec.block_id, "status": "ok",
            "nsteps_requested": spec.nsteps,
            "nsteps_recorded": len(record["time_s"]),
            "execution_id": f"exec-{spec.run_id}",
            "container_id": f"ctr-{spec.run_id}",
            "started_at_utc": "2026-11-15T01:00:00Z",
            "raw_sha256": hashlib.sha256(spec.run_id.encode()).hexdigest(),
            "root": str(self.observations_root), "cache_hit": False,
        }
        if spec.study_id == "A":
            meta["initial_x_m"] = 0.0
        target = base / f"{spec.run_id}.meta.json"
        target.write_text(json.dumps(meta, sort_keys=True) + "\n",
                          encoding="utf-8")
        os.chmod(target, 0o600)
        self.metas[spec.run_id] = meta
        self.records[spec.run_id] = record
        self.entries.append(O.manifest_entry(
            spec.run_id, jsonl.name, jsonl, nsteps=len(record["time_s"])))
        return record

    def write_manifest(self):
        studies = {m["study_id"] for m in self.metas.values()}
        manifest = O.build_manifest(self.entries, study=studies.pop(),
                                    batch_id=self.batch_id)
        target = Path(self.observations_root) / self.batch_id / "manifest.json"
        target.write_text(json.dumps(manifest, sort_keys=True) + "\n",
                          encoding="utf-8")
        os.chmod(target, 0o600)
        O.verify_manifest(manifest, target.parent)
        return manifest


def _setup(tmp_path: Path, *, task: TS.TaskSpecV13, values: Mapping[str, float]):
    root = tmp_path / "camp"
    ctx = H.make_ctx(root, ENVELOPE)
    H.make_dirs(root)
    for extra in ("public", "analysis", "private", "claim", "judge",
                  "design_audits", "reports"):
        (root / extra).mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = C.default_budget_v2(ENVELOPE)
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/envelope.json", {
        "schema": P.ENVELOPE_SCHEMA_V13, "schema_version": 1,
        "profile_id": P.PROFILE_R13, "envelope_id": ENVELOPE,
        "mode": "zero_paid", "budget_sha256": budget.sha256})
    ctx.write_json(A.PUBLIC_CONTRACT_RELPATH, {
        "schema": P.PUBLIC_CONTRACT_SCHEMA_V13, "schema_version": 1})
    state = S.ResearchState(ctx)
    for target in (S.ROOT_AUTHORIZED, S.ROOT_CALIBRATED, S.ROOT_FROZEN,
                   S.ROOT_PROBED, S.ROOT_PRACTICED):
        state.advance_root(target, reason="R1.3 零付费排演")
    spend = A.SpendContract(mode=A.MODE_ZERO_PAID)
    admission = A.CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                    lock_sha256="a" * 64, spend=spend,
                                    lane="study_A")

    def factory(batch_id: str):
        return R13Backend(observations_root=ctx.subdir("observations"),
                          batch_id=batch_id, values=values)

    dispatcher = A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study=task.mechanism_id.split("_")[0], runner=LocalRunner(),
        image=IMAGE, backend_factory=factory, profile=P.PROFILE_R13_DEF,
        task=task)
    return ctx, state, dispatcher


def _act(name: str, doc: Mapping[str, Any]) -> L.Action:
    return L.Action(name=name, doc={"action": name, **dict(doc)})


_COUNTER = {"n": 0}


def _dispatch(dispatcher, state, action: L.Action, store=None):
    del store
    """直接分派：先登记 reply/action 事实（生产里由 ResearchLoop 完成）。"""
    _COUNTER["n"] += 1
    rid = f"rehearsal-{_COUNTER['n']}"
    state.journal.append("reply", {"response_id": rid, "text": "{}",
                                   "study": dispatcher.study})
    state.record_action(response_id=rid, action=action.to_doc(),
                        study=dispatcher.study)
    return dispatcher.execute_once(action, None)


def _run_to_confirm(ctx, state, dispatcher, task: TS.TaskSpecV13):
    """跑到第 7 位 run_confirm 成功（RESULTS_RELEASED），返回确认面输出。"""
    design = PR.DESIGN_TEMPLATE_R13
    analysis = PR.ANALYSIS_TEMPLATE_R13
    explore = PR._plan_template(task, phase="explore")
    intent_explore = PR._intent_template(task, role="distributional_exploration")
    intent_confirm = PR._intent_template(task, role="confirmation")

    out = _dispatch(dispatcher, state, _act("write_artifact",
                                       {"kind": "design", "source": design}), None)
    assert out["ok"], out
    design_sha = out["source_sha256"]

    out = _dispatch(dispatcher, state, _act("propose_plan", {
        "plan": explore, "design_intent": intent_explore}), None)
    assert out["ok"], out
    plan_sha = out["plan_sha256"]

    out = _dispatch(dispatcher, state, _act("run_batch", {
        "plan_sha256": plan_sha, "design_sha256": design_sha,
        "phase": "explore"}), None)
    assert out["ok"], out
    run_ids = out["run_ids"]

    out = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "analysis", "source": analysis}), None)
    assert out["ok"], out
    analysis_sha = out["source_sha256"]

    out = _dispatch(dispatcher, state, _act("analyze", {
        "source_sha256": analysis_sha, "run_ids": run_ids}), None)
    assert out["ok"], out

    confirm = PR._plan_template(task, phase="confirm")
    confirm["parent_sha"] = "host"
    confirm["design_source_sha"] = design_sha
    confirm["analysis_source_sha"] = analysis_sha
    out = _dispatch(dispatcher, state, _act("freeze_confirm", {
        "plan_sha256": "host", "design_sha256": design_sha,
        "analysis_sha256": analysis_sha, "plan": confirm,
        "design_intent": intent_confirm}), None)
    assert out["ok"], out
    token = out["confirm_token"]

    out = _dispatch(dispatcher, state, _act("run_confirm", {"confirm_token": token}),
                                  None)
    assert out["ok"], out
    assert "references" not in out
    return out


def _run_chain(ctx, state, dispatcher, task: TS.TaskSpecV13, *,
               report_kwargs: "dict[str, Any] | None" = None):
    out = _run_to_confirm(ctx, state, dispatcher, task)
    cand = out["candidate_output"]["contrasts"][0]
    report = _report_doc(task, cand, out["analysis_receipt"]["sha256"],
                         out["evidence_index"])
    if report_kwargs:
        report.update(report_kwargs)
    claims = [{"hypothesis_id": "h_interaction",
               "proposition": task.primary.proposition,
               "metric_id": task.primary.metric,
               "delta": float(task.primary.delta),
               "estimate": cand["estimate"], "lower": cand["lower"],
               "upper": cand["upper"], "n_blocks": cand["n_blocks"]}]
    out = _dispatch(dispatcher, state, _act("submit_claim", {
        "claims": claims, "report_text": PR.render_study_brief_r13(
            task.mechanism_id.split("_")[0], task)[:200],
        "research_report": report}), None)
    return out, state, ctx


def _report_doc(task, cand, receipt_sha, index):
    """研究报告最小合法文档（locator 一律用 run_confirm 的 evidence_index）。"""
    del cand
    def loc(file, pointer):
        return {"kind": "json_pointer", "file": file,
                "sha256": index[file], "pointer": pointer}

    return {
        "schema": "simpraxis.rsi_r13.research_report.v1", "schema_version": 1,
        "task_id": task.task_id,
        "question_and_hypotheses": "交互效应是否不为零",
        "methods_and_assignment_responsibility": "nuisance 由宿主逐 block 分配",
        "primary_results": [{"contrast_id": task.primary.id,
                             "analysis_receipt_sha": receipt_sha,
                             "result_locator": loc("analysis/output.json",
                                                   "/diagnostics/contrasts/0/effect")}],
        "heterogeneity_and_scope": [{
            "claim": "效应仅覆盖已登记的四个条件",
            "evidence_refs": [loc("confirm/blocks.json", "/blocks/0")],
            "applicability": "box/单关节场景"}],
        "explore_confirm_comparison": {
            "estimands": task.primary.id, "comparable": False,
            "explanation": "探索覆盖设计空间，确认是宿主 block 抽样",
            "evidence_refs": [loc("analysis/output.json", "/diagnostics")]},
        "alternative_explanations": ["接触/积分数值差异而非物理效应"],
        "limitations": [{
            "statement": "仅四个干预条件与两个 nuisance",
            "evidence_refs": [loc("confirm/blocks.json", "/blocks/0")],
            "consequence": "不外推到其他工况"}],
        "next_steps": [{"unresolved_question": "更高水平是否饱和",
                        "discriminating_experiment": "增加端点条件",
                        "possible_outcomes": ["继续增大", "饱和"]}],
    }


def _values_a():
    return {"hi_base": 1.0, "hi_low_mu": 1.30, "lo_base": 0.4,
            "lo_low_mu": 0.45, "sham": 1.0}


def _values_b():
    return {"s_low": 1.0, "s_high": 1.0, "l_low": 1.0, "l_high": 1.6}


def test_r13_study_a_end_to_end_judge_ok(tmp_path):
    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    out, state, ctx = _run_chain(ctx, state, dispatcher, task)
    assert out["ok"], out
    assert out["judge_verdict"] == "JUDGE_OK", out
    assert out["verdict"] == S.ST_VERIFIED
    assert out["claim_status"] == "MATCH"
    assert out["diagnostics_valid"] and out["report_valid"]
    assert (ctx.root / "judge" / "report_A.json").is_file()
    assert (ctx.root / "design_audits").is_dir()


def test_r13_study_b_end_to_end_judge_ok(tmp_path):
    task = TS.build_new_b()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_b())
    out, state, ctx = _run_chain(ctx, state, dispatcher, task)
    assert out["ok"], out
    assert out["judge_verdict"] == "JUDGE_OK", out
    assert out["verdict"] == S.ST_VERIFIED


def test_r13_placeholder_exploration_is_rejected(tmp_path):
    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    design = PR.DESIGN_TEMPLATE_R13.replace(
        'params[name] = format(lo + (hi - lo) * i / (n - 1), ".6f")',
        'params[name] = "0.5"')
    out = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "design", "source": design}), None)
    assert out["ok"]
    design_sha = out["source_sha256"]
    out = _dispatch(dispatcher, state, _act("propose_plan", {
        "plan": PR._plan_template(task, phase="explore"),
        "design_intent": PR._intent_template(
            task, role="distributional_exploration")}), None)
    assert out["ok"], out
    out = _dispatch(dispatcher, state, _act("run_batch", {
        "plan_sha256": out["plan_sha256"], "design_sha256": design_sha,
        "phase": "explore"}), None)
    assert not out["ok"]
    assert out["code"] == "DESIGN_DECLARATION_INVALID"


def test_r13_confirm_conditions_must_match_task(tmp_path):
    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    design_sha = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "design", "source": PR.DESIGN_TEMPLATE_R13}), None)["source_sha256"]
    _dispatch(dispatcher, state, _act("propose_plan", {
        "plan": PR._plan_template(task, phase="explore"),
        "design_intent": PR._intent_template(
            task, role="distributional_exploration")}), None)
    analysis_sha = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "analysis", "source": PR.ANALYSIS_TEMPLATE_R13}),
        None)["source_sha256"]
    confirm = PR._plan_template(task, phase="confirm")
    confirm["conditions"] = confirm["conditions"][:3]
    confirm["design_source_sha"] = design_sha
    confirm["analysis_source_sha"] = analysis_sha
    out = _dispatch(dispatcher, state, _act("freeze_confirm", {
        "plan_sha256": "host", "design_sha256": design_sha,
        "analysis_sha256": analysis_sha, "plan": confirm,
        "design_intent": PR._intent_template(task, role="confirmation")}), None)
    assert not out["ok"]
    assert out["code"] == "TASK_CONDITIONS_MISMATCH"


def test_r13_missing_report_rejected(tmp_path):
    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    out, state, ctx = _run_chain(
        ctx, state, dispatcher, task, report_kwargs={"limitations": []})
    assert out["ok"], out
    assert out["judge_verdict"] != "JUDGE_OK"
    assert "LIMITATIONS_MISSING" in out["reason_codes"]
    assert out["verdict"] == S.ST_VALID_FAIL


# ---------------------------------------------------------------------------
# 轮 M（R13-D7）：候选分析文档里的非有限数必须是**可恢复**拒绝，不是宿主崩溃
# ---------------------------------------------------------------------------

def _nan_analysis_source() -> str:
    """在模板的自由字段里塞 NaN（对比字段本身合法）。"""
    src = PR.ANALYSIS_TEMPLATE_R13
    needle = '"independent_unit_count": len(by_block)'
    assert needle in src
    return src.replace(needle,
                       '"independent_unit_count": len(by_block),\n'
                       '                     "nan_probe": float("nan")')


def test_r13_analysis_nonfinite_rejected_recoverably(tmp_path):
    from orchestrator.rsi_r1 import recovery as RC

    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    design = PR.DESIGN_TEMPLATE_R13
    explore = PR._plan_template(task, phase="explore")
    intent = PR._intent_template(task, role="distributional_exploration")

    out = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "design", "source": design}), None)
    assert out["ok"], out
    design_sha = out["source_sha256"]
    out = _dispatch(dispatcher, state, _act("propose_plan", {
        "plan": explore, "design_intent": intent}), None)
    assert out["ok"], out
    out = _dispatch(dispatcher, state, _act("run_batch", {
        "plan_sha256": out["plan_sha256"], "design_sha256": design_sha,
        "phase": "explore"}), None)
    assert out["ok"], out
    run_ids = out["run_ids"]
    out = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "analysis", "source": _nan_analysis_source()}), None)
    assert out["ok"], out
    analysis_sha = out["source_sha256"]

    out = _dispatch(dispatcher, state, _act("analyze", {
        "source_sha256": analysis_sha, "run_ids": run_ids}), None)
    assert out["ok"] is False, out
    assert out["code"] == "NAN_OR_INF", out
    assert out["side_effect_status"] == "KNOWN_COMPLETE"
    decision = RC.classify_failure(RC.FailureEvidence(
        phase="analyze", frozen=False, open_intent=False,
        effect_status=out["side_effect_status"], code=out["code"]))
    assert decision.recoverable, decision
    # 无效分析一律不落盘（不留「带 NaN 的归档件」）。
    assert not list((ctx.root / "analysis").glob("explore-*.json"))


def test_r13_parse_action_rejects_nonfinite():
    with pytest.raises(L.LoopError) as info:
        L.parse_action('{"action": "analyze", "source_sha256": "%s",'
                       ' "run_ids": [], "sneaky": NaN}' % ("a" * 64))
    assert info.value.code == "NAN_OR_INF"
    assert info.value.terminal is False


def test_r13_results_released_only_allows_submit_claim(tmp_path):
    """轮 N（R13-D8）：RESULTS_RELEASED 之后只在入口放行 submit_claim。

    轮 M study A 实证：发布后 write_artifact/analyze 被放行，模型在「再修一次
    分析」里烧掉 39 呼、零主张。此处的拒绝必须是非终止、副作用 NONE，
    且不写任何事件（下一呼仍可正常提交主张）。
    """
    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    out = _run_to_confirm(ctx, state, dispatcher, task)
    assert out["ok"], out
    assert state.study_state("A") == S.ST_RESULTS_RELEASED

    results_before = sum(1 for e in state.journal.events()
                         if e.event == "action_result")
    blocked = _dispatch(dispatcher, state, _act("write_artifact", {
        "kind": "analysis", "source": PR.ANALYSIS_TEMPLATE_R13}), None)
    assert blocked["ok"] is False, blocked
    assert blocked["code"] == "CLAIM_REQUIRED"
    assert blocked["side_effect_status"] == "NONE"
    blocked = _dispatch(dispatcher, state, _act("freeze_confirm", {
        "plan_sha256": "host"}), None)
    assert blocked["ok"] is False and blocked["code"] == "CLAIM_REQUIRED"
    # 只允许测试驱动的 reply/action 登记；被拒动作不得留下结果/转移事件。
    assert sum(1 for e in state.journal.events()
               if e.event == "action_result") == results_before
    # 合法出口不受影响：主张照样能提交，study 进入成功终态。
    cand = out["candidate_output"]["contrasts"][0]
    receipt_sha = out["analysis_receipt"]["sha256"]
    claims = [{"hypothesis_id": "h_interaction",
               "proposition": task.primary.proposition,
               "metric_id": task.primary.metric,
               "delta": float(task.primary.delta),
               "estimate": cand["estimate"], "lower": cand["lower"],
               "upper": cand["upper"], "n_blocks": cand["n_blocks"]}]
    done = _dispatch(dispatcher, state, _act("submit_claim", {
        "claims": claims, "report_text": "结论见报告",
        "research_report": _report_doc(task, cand, receipt_sha,
                                       out["evidence_index"])}), None)
    assert done["ok"], done
    assert done.get("judge_verdict") == "JUDGE_OK", done


def test_r13_formal_packet_reads_reports_under_campaign_root(tmp_path, monkeypatch):
    """轮 O（R13-D9）：审查包必须从 campaign 根内的 ``reports/`` 取报告。

    轮 N review A 实证：``ctx.subdir("reports")`` 走的是 R1 通用子目录表，
    直接抛 CampaignError；``reports`` 只属 R1.3 子目录集，读取侧一律按
    campaign 根内相对路径。
    """
    from orchestrator.rsi_r1 import r13_review_run as RR

    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    (ctx.root / "reports").mkdir(mode=0o700, exist_ok=True)
    (ctx.root / "reports" / "A-abc123.json").write_text(
        json.dumps({"report": {"schema": "x", "task_id": task.task_id}}),
        encoding="utf-8")
    class _Manifest:
        assignments: "list[Any]" = []

    class _Plan:
        sha256 = "deadbeef" * 8

    (ctx.root / "design_audits" / "intent_confirm_deadbeefdead.json"
     ).write_text(json.dumps({"estimand": {"contrast_ids": [task.primary.id]}}),
                  encoding="utf-8")
    monkeypatch.setattr(
        RR, "_load_confirm",
        lambda ctx, study, **kw: (_Plan(), _Manifest(),
                            {"b0": {c.id: 1.0 for c in task.conditions},
                             "b1": {c.id: 1.2 for c in task.conditions}}, {},
                            task.primary.metric))
    packet, report_doc, values = RR.build_formal_packet(ctx, "A", task)
    assert report_doc["task_id"] == task.task_id
    assert packet["candidate_report"]["task_id"] == task.task_id
    # 缺报告时是合同拒绝，而不是宿主异常。
    (ctx.root / "reports" / "A-abc123.json").unlink()
    with pytest.raises(RR.ReviewRunError) as exc:
        RR.build_formal_packet(ctx, "A", task)
    assert exc.value.code == "REPORT_MISSING"


def test_r13_review_reply_parsing_tolerates_fences():
    """轮 O（R13-D9 同族）：审查回复的格式噪声不许变成宿主裸异常。"""
    from orchestrator.rsi_r1 import r13_review_run as RR

    assert RR._parse_review_doc('{"a": 1}') == {"a": 1}
    assert RR._parse_review_doc('```json\n{"a": 1}\n```') == {"a": 1}
    assert RR._parse_review_doc('说明如下：\n{"a": 1}\n以上。') == {"a": 1}
    for bad in ("", "没有对象", "[1, 2]", "{坏 json"):
        with pytest.raises(RR.ReviewRunError) as exc:
            RR._parse_review_doc(bad)
        assert exc.value.code == "REVIEW_REPLY_INVALID"


def test_r13_formal_packet_intent_is_study_scoped(tmp_path):
    """轮 P（R13-D10）：审查包里的 design_intent 必须是**本 study** 的。

    轮 O review B 实证：全局 glob 取到的是 study A 的意图，两名审查者据此在
    problem_and_hypothesis / exploration_declaration / confirmation_controls
    上判 FAIL——审查证据被宿主缺陷污染。此处的复现：同一根里先有 A 的意图，
    再建 B 的包，必须取到 B 自己的 estimand；取不到就 INTENT_MISSING。
    """
    from orchestrator.rsi_r1 import r13_review_run as RR
    from orchestrator.rsi_r1 import contracts as C

    task_b = TS.build_new_b()
    ctx, state, dispatcher = _setup(tmp_path, task=task_b, values=_values_b())
    out, state, ctx = _run_chain(ctx, state, dispatcher, task_b)
    assert out["ok"], out
    plan_b = C.ResearchPlan.from_dict(
        ctx.read_json("confirm/plan_B.json"))
    intent_b = json.loads(ctx.relative(
        f"design_audits/intent_confirm_{plan_b.sha256[:12]}.json")
        .read_text(encoding="utf-8"))
    # 伪造一份"排序更大"的他 study 意图，模拟旧实现的污染路径。
    (ctx.root / "design_audits" / "intent_confirm_zzzzzzzzzzzz.json").write_text(
        json.dumps({**intent_b,
                    "estimand": {**intent_b["estimand"],
                                 "contrast_ids": ["friction_x_speed"],
                                 "metric_id": "displacement_x_m"}}),
        encoding="utf-8")
    packet, _, _ = RR.build_formal_packet(ctx, "B", task_b)
    assert packet["design_intent"]["estimand"]["contrast_ids"] == [
        task_b.primary.id]
    # fail-closed：本 study 的意图缺件时不许拿别人的顶。
    ctx.relative(f"design_audits/intent_confirm_{plan_b.sha256[:12]}.json").unlink()
    with pytest.raises(RR.ReviewRunError) as exc:
        RR.build_formal_packet(ctx, "B", task_b)
    assert exc.value.code == "INTENT_MISSING"


def test_r13_formal_packet_uses_task_primary_metric(tmp_path):
    """轮 P（R13-D11）：块表取任务登记的主指标，不被「最后写入的键」覆盖。

    study A 的 run 同时含 displacement_x_m 与 stop_time_s；旧实现让块表变成
    停止时间，审查者据此审了与报告不一致的数（轮 O review A）。
    """
    from orchestrator.rsi_r1 import r13_review_run as RR

    task = TS.build_new_a()
    ctx, state, dispatcher = _setup(tmp_path, task=task, values=_values_a())
    out, state, ctx = _run_chain(ctx, state, dispatcher, task)
    assert out["ok"], out
    packet, _, values = RR.build_formal_packet(ctx, "A", task)
    claim = ctx.read_json("claim/submission_A.json")["claims"][0]
    effect = packet["primary_recomputation"]["effect"]
    assert abs(float(effect) - float(claim["estimate"])) < 1e-12
    assert len(values) == 32
    assert packet["primary_contrast"]["metric"] == task.primary.metric
