"""R1.3 正式研究的公开 brief 与代码模板（只演示形状，不含答案）。

与 R1.2 的差别：

* 候选不再自选条件：两留出任务的干预条件在 ``TaskSpecV13`` 中已冻结，候选
  必须逐字复用（含 A 的 sham 与 2×2 角点）；
* 主估计量 = 任务登记的线性对比（2×2 交互），候选分析程序必须直接算它；
* 必须随 ``propose_plan``/``freeze_confirm`` 提交 ``design_intent``；
* 必须随 ``submit_claim`` 提交 ``research_report``（结构见 §4）；
* 分析输出在既有 ``analysis_output.v1`` 之外增加顶层 ``diagnostics`` 块。

本模块只生成文本；不读盘、不触网、不含任何私有 block/seed/旧轮结果。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from orchestrator.rsi_r1 import task_specs as TS


def _plan_template(task: TS.TaskSpecV13, *, phase: str = "explore") -> "dict[str, Any]":
    component = None
    # plan.primary 必须是任务内的单因子组件（宿主用它做合同锚点；任务级
    # 交互由判据从任务登记件重算）。
    weights = task.primary.weights
    ids = list(task.primary.condition_ids)
    for a in ids:
        for b in ids:
            if a >= b:
                continue
            ca = next(c for c in task.conditions if c.id == a)
            cb = next(c for c in task.conditions if c.id == b)
            diff = [k for k in ca.params if ca.params.get(k) != cb.params.get(k)]
            if len(diff) == 1:
                component = {"a": a, "b": b, "param": diff[0]}
                break
        if component:
            break
    a, b = component["a"], component["b"]
    primary = {
        "id": "component_single_factor",
        # R12 ResearchPlan 的 ContrastSpec.kind 只接受 primary/negative/exploratory；
        # R1.3 任务级交互类型在 TaskSpec.primary（ContrastV13）里独立登记。
        "kind": "primary",
        "metric": task.primary.metric,
        "proposition": task.primary.proposition,
        "delta": str(task.primary.delta),
        "condition_ids": [a, b],
    }
    nc = None
    if task.negative_control_spec is not None:
        nc_doc = task.negative_control_spec["contrast"]
        nc = {
            "id": nc_doc["id"], "kind": "negative",
            "metric": nc_doc["metric"], "proposition": "equivalent",
            "delta": str(task.primary.delta)
            if False else nc_doc["delta"],
            "condition_ids": [t["condition_id"] for t in nc_doc["terms"]],
        }
    varying = {k: list(v["support"]) for k, v in task.nuisance_schema.items()
               if v["mode"] == "varying"}
    hypotheses = [
        {"id": "h_interaction", "mechanism": "干预效应随工况改变（交互）",
         "predicted": task.primary.proposition,
         "contrast_id": primary["id"],
         "falsification": "95% 主区间不跨过 δ 与 0 的预登记判据"},
        {"id": "h_additive", "mechanism": "效应可加、不随工况改变",
         "predicted": "equivalent", "contrast_id": primary["id"],
         "falsification": "交互对比的 95% 区间完全落在 ±δ 之外"},
    ]
    return {
        "schema": "simpraxis.rsi_r1.plan.v1",
        "schema_version": 1,
        "study_id": task.mechanism_id.split("_")[0],
        "revision": 1 if phase == "explore" else 2,
        "parent_sha": None if phase == "explore" else "host",
        "hypotheses": hypotheses,
        "conditions": [c.to_dict() for c in task.conditions],
        "nuisance_distribution": {"generator": "host_uniform_v1",
                                  "block_count": 32, "varying": varying},
        "primary": primary,
        "negative_control": nc,
        "analysis_source_sha": None if phase == "explore" else "<sha>",
        "design_source_sha": None if phase == "explore" else "<sha>",
        "phase": phase,
    }


def _intent_template(task: TS.TaskSpecV13, *, role: str) -> "dict[str, Any]":
    nuisance = {k: {"mode": v["mode"], "support": list(v["support"]),
                    "assignment_owner": v["assignment_owner"]}
                for k, v in task.nuisance_schema.items()}
    return {
        "schema": "simpraxis.rsi_r13.design_intent.v1",
        "schema_version": 1,
        "phase_role": role,
        "estimand": {
            "metric_id": task.estimand["metric_id"],
            "contrast_ids": [task.primary.id],
            "target_population": task.estimand["target_population"],
            "aggregation": task.estimand["aggregation"],
            "weighting": task.estimand["weighting"],
        },
        "nuisance": nuisance,
        "replication": {"unit_id": "block_id", "independent_unit": "block_id",
                        "deterministic_repeat_purpose": "repeatability"},
        "phase_comparability": {
            "relation": "different_target" if role == "fixed_pilot"
            else "different_target",
            "rationale": ("探索覆盖设计空间；确认是宿主 32 block 抽样，"
                          "两阶段估计对象已在 estimand 中登记"),
        },
    }


DESIGN_TEMPLATE_R13 = '''\
import hashlib, json, sys
from pathlib import Path

def plan_sha(doc):
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

def main():
    view, out = Path(sys.argv[1]), Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    task = json.loads((view / "task.json").read_text(encoding="utf-8"))
    if len(plans) != 1:
        print("expected exactly one plan", file=sys.stderr)
        return 2
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    nuisance = task["nuisance_schema"]
    n = 8
    cells = []
    for cond in plan["conditions"]:
        for i in range(n):
            params = dict(cond["params"])
            for name, spec in nuisance.items():
                if spec["mode"] != "varying":
                    continue
                lo, hi = (float(x) for x in spec["support"])
                params[name] = format(lo + (hi - lo) * i / (n - 1), ".6f")
            cells.append({"condition_id": cond["id"], "block_id": "x%02d" % (i + 1),
                          "params": params})
    doc = {"schema": "simpraxis.rsi_r1.matrix.v1", "schema_version": 1,
           "study_id": plan["study_id"], "plan_sha": plan_sha(plan),
           "cells": cells}
    (out / "matrix.json").write_text(json.dumps(doc, sort_keys=True) + "\\n",
                                     encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

ANALYSIS_TEMPLATE_R13 = '''\
import hashlib, json, math, sys
from pathlib import Path

import numpy as np

SEED = 20261111
RESAMPLES = 10000

def first(v):
    return float(v[0]) if isinstance(v, (list, tuple)) else float(v)

def metric_value(record, metric_id, initial_x=None, amplitude=None):
    qpos = record["qpos"]
    if metric_id == "normalized_tracking_rmse":
        if record.get("instability_events"):
            return 10.0
        times = record["time_s"]; target = record["target_q"]
        idx = [i for i, t in enumerate(times) if 0.5 <= float(t) <= 4.0]
        sq = math.fsum((first(qpos[i]) - first(target[i])) ** 2 for i in idx)
        return math.sqrt(sq / len(idx)) / float(amplitude)
    anchor = float(initial_x) if initial_x is not None else first(qpos[0])
    return first(qpos[-1]) - anchor

def interval(values, level):
    arr = np.asarray(values, dtype=np.float64)
    rng = np.random.default_rng(np.random.PCG64(SEED))
    idx = rng.integers(0, arr.shape[0], size=(RESAMPLES, arr.shape[0]))
    means = arr[idx].mean(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha], method="linear")
    return float(lo), float(hi)

def verdict(proposition, delta, lower, upper):
    if proposition == "decrease_gt":
        if upper < -delta: return "SUPPORTED"
        if lower >= -delta: return "REFUTED"
    elif proposition == "increase_gt":
        if lower > delta: return "SUPPORTED"
        if upper <= delta: return "REFUTED"
    else:
        if lower > -delta and upper < delta: return "SUPPORTED"
        if upper <= -delta or lower >= delta: return "REFUTED"
    return "INCONCLUSIVE"

def main():
    view, out = Path(sys.argv[1]), Path(sys.argv[2])
    plans = sorted(view.glob("plan-*.json"))
    task = json.loads((view / "task.json").read_text(encoding="utf-8"))
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    plan_sha = hashlib.sha256(json.dumps(
        plan, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    records = {}
    for path in sorted(view.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line); records[rec["run_id"]] = rec
    condition = {}; initial_x = {}; amplitude = {}
    for cond in plan["conditions"]:
        p = cond.get("params") or {}
        if "target_amplitude_rad" in p:
            amplitude[cond["id"]] = float(p["target_amplitude_rad"])
    for meta in sorted(view.glob("*.meta.json")):
        doc = json.loads(meta.read_text(encoding="utf-8"))
        rid = meta.name[: -len(".meta.json")]
        condition[rid] = doc["condition_id"]
        if "initial_x_m" in doc:
            initial_x[rid] = doc["initial_x_m"]
    metric_id = task["primary"]["metric"]
    weights = {t["condition_id"]: float(t["weight"])
               for t in task["primary"]["terms"]}
    by_block = {}
    for run_id, rec in records.items():
        cond = condition[run_id]
        by_block.setdefault(rec["block_id"], {})[cond] = metric_value(
            rec, metric_id, initial_x.get(run_id), amplitude.get(cond))
    effects = {b: sum(w * vals[c] for c, w in weights.items())
               for b, vals in by_block.items()}
    # 与宿主同一顺序：按 block_id 排序后再做 bootstrap（同一 seed 下数组顺序
    # 改变会改变重抽样结果——2026-11-15 T5 实测 CI 差 1e-2）。
    values = [effects[b] for b in sorted(effects)]
    effect = float(np.mean(values))
    lower, upper = interval(values, 0.95)
    delta = float(task["primary"]["delta"])
    status = verdict(task["primary"]["proposition"], delta, lower, upper)
    diag = {
        "schema": "simpraxis.rsi_r13.analysis_output.v1", "schema_version": 1,
        "phase": "confirm", "study_id": task["study_id"],
        "task_id": task["task_id"],
        "input_sha256": plan_sha,
        "source_sha256": str(plan.get("analysis_source_sha") or ""),
        "contrasts": [{"contrast_id": task["primary"]["id"], "effect": effect,
                       "lower": lower, "upper": upper,
                       "n_blocks": len(values),
                       "block_effects": [{"block_id": b, "effect": effects[b]}
                                         for b in sorted(effects)]}],
        "coverage": {"blocks": sorted(by_block), "run_count": len(records),
                     "unique_design_count": len({
                         (condition[rid],
                          json.dumps(records[rid].get("params_applied") or {},
                                     sort_keys=True))
                         for rid in records}),
                     "independent_unit_count": len(by_block)},
        "summaries": [],
        "threshold_counts": [{"contrast_id": task["primary"]["id"],
                              "above_delta": sum(1 for v in values if v > delta),
                              "below_minus_delta": sum(1 for v in values if v < -delta),
                              "within": sum(1 for v in values if -delta <= v <= delta)}],
        "phase_comparability": {
            "explore_target": "explore grid", "confirm_target": "32 host blocks",
            "relation": "different_target",
            "rationale": "explore 覆盖设计空间；confirm 是宿主 block 抽样"},
    }
    doc = {"schema": "simpraxis.rsi_r1.analysis_output.v1", "schema_version": 1,
           "run_ids": sorted(records),
           "contrasts": [{"contrast_id": task["primary"]["id"],
                          "metric_id": metric_id,
                          "proposition": task["primary"]["proposition"],
                          "estimate": effect, "lower": lower, "upper": upper,
                          "status": status, "n_blocks": len(values)}],
           "diagnostics": diag}
    (out / "analysis.json").write_text(json.dumps(doc, sort_keys=True) + "\\n",
                                       encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def task_block(task: TS.TaskSpecV13) -> str:
    lines = [f"### 任务规格（冻结）：{task.task_id}",
             f"问题：{task.public_question}",
             "",
             "| condition_id | 参数 |",
             "|---|---|"]
    for c in task.conditions:
        lines.append(f"| {c.id} | {json.dumps(c.params, ensure_ascii=False, sort_keys=True)} |")
    terms = ", ".join(f"{cid}:{w}" for cid, w in task.primary.terms)
    lines += [
        "",
        f"主估计量（宿主判据逐位复算）：`{task.primary.kind}`，权重 {terms}，"
        f"metric={task.primary.metric}，proposition={task.primary.proposition}，"
        f"δ={task.primary.delta}",
        "nuisance："
        + json.dumps({k: dict(v) for k, v in task.nuisance_schema.items()},
                     ensure_ascii=False, sort_keys=True),
        "阴性控制："
        + (json.dumps(task.negative_control_spec, ensure_ascii=False,
                      sort_keys=True) if task.negative_control_spec else "无（开放研究）"),
        "",
        "**条件逐字复用**：propose_plan/freeze_confirm 的 conditions 必须与上表完全一致；"
        "run_batch 的 cell params 也必须带上该条件的全部参数（可覆盖 nuisance）。",
    ]
    return "\n".join(lines)


def render_study_brief_r13(study: str, task: TS.TaskSpecV13) -> str:
    explore_plan = _plan_template(task, phase="explore")
    explore_primary = explore_plan["primary"]
    design = DESIGN_TEMPLATE_R13
    analysis = ANALYSIS_TEMPLATE_R13
    parts = [
        f"### 本轮任务：正式研究 study {study}（R1.3 留出任务，48 呼预算）",
        task_block(task),
        "",
        "### 动作与必填字段（每个回复只给一个 JSON 对象）",
        "1. write_artifact(kind=design, source=<python>)  → 返回 source_sha256",
        "2. propose_plan(plan=<ResearchPlan>, design_intent=<DesignIntentV13>)\n"
        "   **design_intent 必须与 plan 同级（动作顶层字段）**，不要放进 plan 文档内。",
        "   design_intent 的 phase_role=fixed_pilot|distributional_exploration；"
        "若声明 distributional_exploration，run_batch 的矩阵必须真的让 nuisance 变化"
        "（全用占位值会被 DESIGN_DECLARATION_INVALID 拒绝）。",
        "3. run_batch(plan_sha256, design_sha256, phase=\"explore\")",
        "4. write_artifact(kind=analysis, source=<python>)",
        "5. analyze(source_sha256, run_ids=[...])",
        "6. freeze_confirm(plan_sha256=\"host\", design_sha256, analysis_sha256,"
        " plan=<confirm 计划>, design_intent=<confirmation 意图>)",
        "7. run_confirm(confirm_token)  → 返回 candidate_output/analysis_receipt/"
        "evidence_index（不含宿主参考值）",
        "8. submit_claim(claims=[...], report_text=<正文>, research_report=<结构见下>)",
        "",
        "### 计划模板（explore；confirm 把 phase 改 confirm、revision=2、parent_sha=\"host\"）",
        "```json\n" + json.dumps(explore_plan, ensure_ascii=False, indent=1,
                                  sort_keys=True) + "\n```",
        "### 探索意图模板（design_intent）",
        "```json\n" + json.dumps(_intent_template(
            task, role="distributional_exploration"), ensure_ascii=False,
            indent=1, sort_keys=True) + "\n```",
        "### 确认意图模板（freeze_confirm.design_intent）",
        "```json\n" + json.dumps(_intent_template(task, role="confirmation"),
                                  ensure_ascii=False, indent=1,
                                  sort_keys=True) + "\n```",
        "### 分析输出要求",
        "analysis.json 的顶层 schema 仍为 `simpraxis.rsi_r1.analysis_output.v1`；"
        "`contrasts` 只需且必须包含任务主对比 id（宿主不接受别的 id）；"
        "另加顶层 `diagnostics`（`simpraxis.rsi_r13.analysis_output.v1`），"
        "逐 block 效应/coverage/δ 两侧计数/阶段可比性见模板。",
        "### design 模板",
        "```python\n" + design + "```",
        "### analysis 模板",
        "```python\n" + analysis + "```",
        "### research_report 结构（submit_claim 必填）",
        json.dumps({
            "schema": "simpraxis.rsi_r13.research_report.v1",
            "schema_version": 1, "task_id": task.task_id,
            "question_and_hypotheses": "<文本>",
            "methods_and_assignment_responsibility": "<文本；宿主分配 nuisance>",
            "primary_results": [{"contrast_id": task.primary.id,
                                 "analysis_receipt_sha": "<run_confirm 返回的 analysis_receipt.sha256>",
                                 "result_locator": {"kind": "json_pointer",
                                                    "file": "analysis/output.json",
                                                    "sha256": "<evidence_index['analysis/output.json']>",
                                                    "pointer": "/diagnostics/contrasts/0/effect"}}],
            "heterogeneity_and_scope": [{"claim": "<文本>",
                                         "evidence_refs": [{"kind": "json_pointer",
                                                            "file": "confirm/blocks.json",
                                                            "sha256": "<evidence_index['confirm/blocks.json']>",
                                                            "pointer": "/blocks/0"}],
                                         "applicability": "<文本>"}],
            "explore_confirm_comparison": {"estimands": "<文本>", "comparable": True,
                                           "explanation": "<文本>",
                                           "evidence_refs": [{"kind": "json_pointer",
                                                              "file": "analysis/output.json",
                                                              "sha256": "<...>",
                                                              "pointer": "/diagnostics"}]},
            "alternative_explanations": ["<至少一条>"],
            "limitations": [{"statement": "<文本>", "evidence_refs": [{"...": "..."}],
                             "consequence": "<文本>"}],
            "next_steps": [{"unresolved_question": "<文本>",
                            "discriminating_experiment": "<文本>",
                            "possible_outcomes": ["<至少两种>"]}],
        }, ensure_ascii=False, indent=1),
        "报告里的每个 locator 必须用 run_confirm 返回的 evidence_index 中的 "
        "file/sha256；路径只能取 analysis/output.json、analysis/receipt.json、"
        "confirm/blocks.json。空局限、无引用、路径穿越都会被判 REPORT_* 拒绝。",
        "",
        "### claims 字段",
        "`{\"hypothesis_id\",\"proposition\",\"metric_id\",\"delta\",\"estimate\","
        "\"lower\",\"upper\"}`；数值逐字复制第 7 步 `candidate_output.contrasts` 里 "
        f"`{task.primary.id}` 的同名字段；delta 必须等于公开量表值 "
        f"{task.primary.delta}。",
        "",
        "### 硬状态规则（轮 N）",
        "第 7 步 run_confirm 成功后 study 立即进入 RESULTS_RELEASED：**唯一**"
        "合法动作是第 8 步 submit_claim。此后任何 write_artifact / analyze / "
        "propose_plan / run_batch / run_confirm 都会被 CLAIM_REQUIRED 拒绝且"
        "照计 1 呼——不要在发布后再改分析或再登记源码；直接按第 7 步返回的"
        "candidate_output / analysis_receipt / evidence_index 写主张与报告。",
    ]
    return "\n".join(parts)
