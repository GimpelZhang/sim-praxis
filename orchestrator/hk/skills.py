"""K0–K8 研究技能骨架：八动作协议编译为类型化技能序列（设计族 §1）。

- 每技能 = `CallableSkill(SkillDescriptor(id=..., revision="s0", input=..., output=...,
  effects=(...)), fn)`；Contract 一律引自 `contracts.py`（单一真值，本文件无第二处
  字面量）。
- K1/K4/K8 是 System 2 触点（actor 注入；S0 用假 actor 测试，S1 接 fresh 会话）。
- K3/K5/K7 委托既有引擎（锁内适配：R1-N 构建器 `task_specs.build_r1n_a/b`；
  分析重算 `statistics.iter_block_pairs/estimate/classify`），本文件只做薄包装，
  **不复制引擎代码**。批量执行器（MuJoCo 容器跑批）S1 才接线，未接线时
  `default_batch_runner` 抛 `SkillEngineNotWired`（诚实失败，不造假产物）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from ._physicalrsi import CallableSkill, SkillDescriptor, compose_sequence
from . import contracts as C

__all__ = ["SkillError", "SkillEngineNotWired", "build_skills", "build_research_loop",
           "default_analyzer", "default_confirmer", "r1n_task_builder"]


class SkillError(RuntimeError):
    """技能输入/输出不符合研究协议。"""


class SkillEngineNotWired(SkillError):
    """执行引擎未接线（S0 允许；S1 必须接真实跑批/重算，禁止造假产物）。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SkillError(f"{what} 必须是 dict，收到 {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# 默认引擎适配（薄包装，不复制实现）
# ---------------------------------------------------------------------------

def r1n_task_builder(name: str, **kwargs) -> Any:
    """R1-N 任务构建器薄包装（A_N / B_N 忠实复用）。"""
    from orchestrator.rsi_r1 import task_specs

    builders = {"a": task_specs.build_r1n_a, "b": task_specs.build_r1n_b}
    try:
        builder = builders[name]
    except KeyError:
        raise SkillError(f"未知任务族 {name!r}（只有 a/b）") from None
    return builder(**kwargs)


def default_batch_runner(batch_plan: Mapping[str, Any], context=None):
    raise SkillEngineNotWired(
        "批量执行器未接线：S0 只落技能骨架；真实 MuJoCo 跑批在 S1 由容器适配器接入"
        "（禁止返回假 run 产物）")


def default_analyzer(run_manifest: Mapping[str, Any], context=None) -> dict:
    """冻结分析：逐对比配对差 + percentile 区间（委托 rsi_r1.statistics）。"""
    from orchestrator.rsi_r1 import statistics

    document = _require_mapping(run_manifest, "run_manifest")
    rows = document.get("runs")
    contrasts = document.get("contrasts")
    if not isinstance(rows, list) or not rows:
        raise SkillError("run_manifest.runs 必须是非空列表")
    if not isinstance(contrasts, list) or not contrasts:
        raise SkillError("run_manifest.contrasts 必须是非空列表")
    # K6 需要冻结的确认分配（私有 block 配对）；S0 未接线确认批生成时诚实失败，
    # 不产出注定在 K6 崩掉的半成品（评审 MEDIUM）。
    confirm_plan = document.get("confirm_plan")
    if not isinstance(confirm_plan, Mapping) or not confirm_plan.get("pairs"):
        raise SkillEngineNotWired(
            "confirm_plan 未接线：K5 默认适配器只做开发批分析；确认批私有配对在 "
            "S1 由冻结分配器生成（禁止伪造 pairs）")
    results = []
    for contrast in contrasts:
        c = _require_mapping(contrast, "contrast")
        try:
            pairs = statistics.iter_block_pairs(
                rows,
                treatment_condition=c["treatment_condition"],
                control_condition=c["control_condition"],
                metric_id=c["metric_id"],
            )
            estimated = statistics.estimate(statistics.paired_diffs(pairs))
        except Exception as exc:  # 引擎异常原样升级，不失真
            raise SkillError(f"分析 {c.get('id')!r} 失败：{exc}") from exc
        results.append({
            "id": c.get("id"),
            "metric_id": c["metric_id"],
            "proposition": c.get("proposition"),
            "delta": c.get("delta"),
            "estimate": estimated["effect"],
            "ci_low": estimated["lower"],
            "ci_high": estimated["upper"],
            "n_blocks": estimated["n_blocks"],
            "claim": statistics.classify(
                c["proposition"], c["delta"], estimated["lower"], estimated["upper"]),
        })
    return {"contrasts": results, "confirm_plan": dict(confirm_plan)}


def default_confirmer(confirm_plan: Mapping[str, Any], context=None) -> dict:
    """host 独立复跑：用冻结方法在全新配对数据上重算（委托同一统计器）。"""
    from orchestrator.rsi_r1 import statistics

    plan = _require_mapping(confirm_plan, "confirm_plan")
    if plan.get("frozen") is not True:
        raise SkillEngineNotWired("confirm_plan 未冻结：拒绝在未冻结方法上复跑")
    pairs_by_contrast = plan.get("pairs")
    if not isinstance(pairs_by_contrast, Mapping) or not pairs_by_contrast:
        raise SkillError("confirm_plan.pairs 必须是非空 mapping")
    results = []
    for contrast_id, pairs in sorted(pairs_by_contrast.items()):
        estimated = statistics.estimate(pairs)
        results.append({
            "id": contrast_id,
            "estimate": estimated["effect"],
            "ci_low": estimated["lower"],
            "ci_high": estimated["upper"],
            "n_blocks": estimated["n_blocks"],
        })
    return {"contrasts": results}


# ---------------------------------------------------------------------------
# 技能工厂
# ---------------------------------------------------------------------------

def _skill(sid: str, input_contract, output_contract, effects, fn,
           guards: tuple[str, ...]) -> CallableSkill:
    return CallableSkill(
        SkillDescriptor(id=sid, revision="s0", input=input_contract,
                        output=output_contract, effects=effects,
                        metadata={"guards": list(guards)}),
        fn)


def build_skills(*, actor: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None,
                 batch_runner: Callable | None = None,
                 analyzer: Callable | None = None,
                 confirmer: Callable | None = None) -> dict[str, CallableSkill]:
    """返回 K0–K8 技能表；未注入的引擎用默认适配（批次执行器默认未接线）。"""
    batch_runner = batch_runner or default_batch_runner
    analyzer = analyzer or default_analyzer
    confirmer = confirmer or default_confirmer

    def touchpoint(name: str):
        if actor is None:
            raise SkillEngineNotWired(f"System 2 触点 {name} 未接线（需注入 actor）")
        return actor

    def k0_register_spec(value, context=None):
        spec = _require_mapping(value, "experiment_spec")
        return {"spec": dict(spec), "registered": True, "sha256": _sha(spec)}

    def k1_propose_plan(value, context=None):
        plan = touchpoint("K1")(  # System 2 触点①：假设/设计
            "propose_plan", C.EXPERIMENT_SPEC.name, _require_mapping(value, "spec"))
        plan = dict(_require_mapping(plan, "study_plan"))
        if not plan.get("conditions"):
            raise SkillError("study_plan.conditions 为空")
        return plan

    def k2_prepare_batch(value, context=None):
        plan = _require_mapping(value, "study_plan")
        conditions = plan.get("conditions")
        n_block = plan.get("n_block")
        if not isinstance(conditions, list) or not conditions:
            raise SkillError("batch_plan 需要非空 conditions")
        if isinstance(n_block, bool) or not isinstance(n_block, int) or n_block <= 0:
            raise SkillError(f"n_block 必须是正整数，收到 {n_block!r}")
        allocations = [[i, b] for i in range(len(conditions)) for b in range(n_block)]
        return {"conditions": list(conditions), "n_block": n_block,
                "n_cond": len(conditions), "runs": len(allocations),
                "allocations": allocations}

    def k3_run_batch(value, context=None):
        plan = _require_mapping(value, "batch_plan")
        manifest = batch_runner(plan, context)
        manifest = dict(_require_mapping(manifest, "run_manifest"))
        if not manifest.get("runs"):
            raise SkillError("run_manifest.runs 为空：拒绝空批")
        manifest.setdefault("batch_sha256", _sha(plan))
        return manifest

    def k4_write_analysis(value, context=None):
        program = touchpoint("K4")(  # System 2 触点②：诊断/写分析
            "write_analysis", C.RUN_MANIFEST.name, _require_mapping(value, "run_manifest"))
        program = dict(_require_mapping(program, "analysis_program"))
        source = program.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SkillError("analysis_program.source 为空")
        program["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return program

    def k5_analyze(value, context=None):
        result = analyzer(_require_mapping(value, "analysis_program"), context)
        result = dict(_require_mapping(result, "analysis_result"))
        if not result.get("contrasts"):
            raise SkillError("analysis_result.contrasts 为空")
        return result

    def k6_freeze_confirm(value, context=None):
        result = _require_mapping(value, "analysis_result")
        plan = result.get("confirm_plan")
        if not isinstance(plan, Mapping):
            raise SkillError("analysis_result 缺少 confirm_plan（冻结分配表）")
        frozen = dict(plan)
        frozen.update(frozen=True, analysis_sha256=_sha(result))
        if not frozen.get("pairs"):
            raise SkillError("confirm_plan.pairs 为空：冻结后无确认批可跑")
        return frozen

    def k7_run_confirm(value, context=None):
        plan = _require_mapping(value, "confirm_plan")
        if plan.get("frozen") is not True:
            raise SkillError("confirm_plan 未冻结：禁止复跑")
        result = confirmer(plan, context)
        result = dict(_require_mapping(result, "confirm_result"))
        result.setdefault("plan_sha256", _sha(plan))
        return result

    def k8_submit_claim(value, context=None):
        result = _require_mapping(value, "confirm_result")
        claim = touchpoint("K8")(  # System 2 触点③：解释
            "submit_claim", C.CONFIRM_RESULT.name, result)
        claim = dict(_require_mapping(claim, "claim"))
        verdict = claim.get("verdict")
        if verdict not in ("SUPPORTED", "REFUTED", "INCONCLUSIVE"):
            raise SkillError(
                f"claim.verdict 必须是 SUPPORTED/REFUTED/INCONCLUSIVE，收到 {verdict!r}")
        return claim

    return {
        "K0": _skill("k0_register_spec", C.EXPERIMENT_SPEC, C.EXPERIMENT_SPEC, (),
                     k0_register_spec, ("G-PREREG",)),
        "K1": _skill("k1_propose_plan", C.EXPERIMENT_SPEC, C.STUDY_PLAN, ("llm",),
                     k1_propose_plan, ("G-PREREG",)),
        "K2": _skill("k2_prepare_batch", C.STUDY_PLAN, C.BATCH_PLAN, (),
                     k2_prepare_batch, ("G-BUDGET",)),
        "K3": _skill("k3_run_batch", C.BATCH_PLAN, C.RUN_MANIFEST,
                     ("physics", "fs_write"), k3_run_batch, ("G-BUDGET", "G-SEND-ONCE")),
        "K4": _skill("k4_write_analysis", C.RUN_MANIFEST, C.ANALYSIS_PROGRAM, ("llm",),
                     k4_write_analysis, ("G-BYTES",)),
        "K5": _skill("k5_analyze", C.ANALYSIS_PROGRAM, C.ANALYSIS_RESULT, ("fs_write",),
                     k5_analyze, ("G-NOPROG",)),
        "K6": _skill("k6_freeze_confirm", C.ANALYSIS_RESULT, C.CONFIRM_PLAN, ("fs_write",),
                     k6_freeze_confirm, ("G-FREEZE",)),
        "K7": _skill("k7_run_confirm", C.CONFIRM_PLAN, C.CONFIRM_RESULT,
                     ("physics", "fs_write"), k7_run_confirm,
                     ("G-FREEZE", "G-SEND-ONCE")),
        "K8": _skill("k8_submit_claim", C.CONFIRM_RESULT, C.CLAIM, ("llm",),
                     k8_submit_claim, ("G-CLAIM",)),
    }


def build_research_loop(*, actor, batch_runner=None, analyzer=None, confirmer=None,
                        name: str = "research_loop_v1") -> CallableSkill:
    """K0→K8 一次 compose（静态类型表即兼容；不插显式 adapt，见设计族 §1）。"""
    skills = build_skills(actor=actor, batch_runner=batch_runner,
                          analyzer=analyzer, confirmer=confirmer)
    ordered = [skills[f"K{i}"] for i in range(9)]
    return compose_sequence(name, *ordered)
