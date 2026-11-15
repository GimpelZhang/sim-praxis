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


def _run_chain(ctx, state, dispatcher, task: TS.TaskSpecV13, *,
               report_kwargs: "dict[str, Any] | None" = None):
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
    cand = out["candidate_output"]["contrasts"][0]
    index = out["evidence_index"]
    receipt_sha = out["analysis_receipt"]["sha256"]

    def loc(file, pointer):
        return {"kind": "json_pointer", "file": file,
                "sha256": index[file], "pointer": pointer}

    report = {
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
