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
from pathlib import Path
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

#: S2 任务登记表（task_id 与 sha256 双键）：战役在开跑前登记 6 个实例，
#: K3 的漂移核对据此取**登记任务**（而不是写死 S1 的 A_N/B_N 构建器）。
_TASK_REGISTRY: "dict[str, Any]" = {}


def register_task(task) -> Any:
    """登记任务（幂等；键 = task_id 与 sha256 双份）。"""
    _TASK_REGISTRY[str(task.task_id)] = task
    _TASK_REGISTRY[str(task.sha256)] = task
    return task


def resolve_registered_task(*, task_id: str = "", plan_sha256: str = ""):
    for key in (plan_sha256, task_id):
        if key and key in _TASK_REGISTRY:
            return _TASK_REGISTRY[key]
    return None


def r1n_task_builder(name: str, **kwargs) -> Any:
    """R1-N/S2b 任务构建器薄包装（A_N / B_N / C_N / D_N）。"""
    from orchestrator.rsi_r1 import task_specs

    builders = {"a": task_specs.build_r1n_a, "b": task_specs.build_r1n_b,
                "c": task_specs.build_s2b_c, "d": task_specs.build_s2b_d}
    try:
        builder = builders[name]
    except KeyError:
        raise SkillError(f"未知任务族 {name!r}（只有 a/b/c/d）") from None
    return builder(**kwargs)


def default_batch_runner(batch_plan: Mapping[str, Any], context=None):
    """K3 真实跑批（S1 接线）：任务构建器校验 → 受信物理服务执行。

    锁内适配（计划 S1-2）：计划原文「K3=default_batch_runner 调 r1n_task_builder(name)
    + physics_service」。条件矩阵由 K1/K2 提交，但**必须与登记任务逐值一致**
    （id 与参数集；不一致即 ``CONDITIONS_DRIFT``——actor 不能自造条件）；私有 block
    分配由 host 的 ``build_block_assignment`` 生成，actor 不可见种子。

    需要 context 提供观测/证据根（``observations_root``/``evidence_root``）与
    ``batch_id``；缺根时诚实抛 ``SkillEngineNotWired``（禁止假产物）。
    """
    from orchestrator.rsi_bridge.physics_service import (PhysicsService,
                                                         build_block_assignment)

    plan = _require_mapping(batch_plan, "batch_plan")
    ctx = context if isinstance(context, Mapping) else {}
    obs_root = ctx.get("observations_root")
    ev_root = ctx.get("evidence_root")
    if not obs_root or not ev_root:
        raise SkillEngineNotWired(
            "批量执行器需要受信服务根（context.observations_root/evidence_root）；"
            "禁止返回假 run 产物")
    study = str(plan.get("study") or "")
    name = {"A_N": "a", "B_N": "b", "C_N": "c", "D_N": "d"}.get(study)
    if name is None:
        raise SkillEngineNotWired(f"未注册研究 {study!r}（只有 A_N/B_N/C_N/D_N）")
    # S2：登记实例优先（task_id/plan_sha 双键）；S1 回退到 R1-N 构建器。
    task = resolve_registered_task(
        task_id=str(plan.get("task_id") or ""),
        plan_sha256=str(plan.get("plan_sha256") or "")) or r1n_task_builder(name)
    registered = {c.id: dict(c.params) for c in task.conditions}
    given_docs = plan.get("conditions")
    if not isinstance(given_docs, list) or not given_docs:
        raise SkillError("batch_plan.conditions 必须是非空列表")
    given = {str(c["id"]): dict(c.get("params") or {}) for c in given_docs}
    if given != registered:
        raise SkillError(
            f"CONDITIONS_DRIFT: 提交条件与登记任务不一致（{sorted(given)} vs "
            f"{sorted(registered)}）；actor 不得自造/篡改条件")
    n_block = plan.get("n_block")
    if isinstance(n_block, bool) or not isinstance(n_block, int) or n_block <= 0:
        raise SkillError(f"n_block 必须是正整数，收到 {n_block!r}")
    plan_sha = str(plan.get("plan_sha256") or "")
    # 评审 MEDIUM：plan_sha256 必须与登记 spec 一致（不能由 actor 换值移动 block 分配）
    registered_plan_sha = ctx.get("registered_plan_sha256")
    if registered_plan_sha and plan_sha != str(registered_plan_sha):
        raise SkillError(
            f"PLAN_SHA_DRIFT: 计划 plan_sha256={plan_sha[:12]}… 与登记 "
            f"{str(registered_plan_sha)[:12]}… 不符")
    nuisance_docs = []
    for param, spec in (task.nuisance_schema or {}).items():
        if not isinstance(spec, Mapping):
            continue
        support = spec.get("support") or ["0", "0"]
        nuisance_docs.append({"param": param, "low": str(support[0]),
                              "high": str(support[-1]),
                              "precision": "0.001" if study == "A_N" else "0.01"})
    blocks = build_block_assignment(plan_sha, study=study, n_block=n_block,
                                    nuisance=nuisance_docs,
                                    salt=ctx.get("assignment_salt"))
    # S2-1（计划 §2.1 第 3 行/§2.2）：NC 由 K1 的 NC 设计技能在任务槽内**提名**
    # base/sham，host 侧构造对照（actor 不写权重/δ）；无提名时退回槽的默认对照
    # （v0 臂复现 S1 退化 sham）。槽本身来自登记任务，不来自 actor。
    task_doc = task.to_dict()
    nc_slot = task_doc.get("negative_control_spec")
    nc_contrast: dict[str, Any] | None = None
    nc_slot_v2 = (nc_slot if isinstance(nc_slot, Mapping)
                  and nc_slot.get("schema") == "simpraxis.rsi_s2.nc_slot.v1"
                  else None)
    if nc_slot_v2 is not None:
        from orchestrator.hk import nc_registration as N

        planned = plan.get("negative_control")
        if not isinstance(planned, Mapping):
            planned = nc_slot_v2.get("default")
        if isinstance(planned, Mapping):
            try:
                nc_contrast = N.build_nc_contrast(
                    nc_slot_v2, base=str(planned.get("base")),
                    sham=str(planned.get("sham")),
                    conditions=task_doc["conditions"])
            except N.NCRegistrationError as exc:
                raise SkillError(f"{exc.code}: {exc.detail}") from None
        task_doc["negative_control_spec"] = (
            None if nc_contrast is None else {"contrast": nc_contrast})
    service = PhysicsService(observations_root=Path(str(obs_root)),
                             evidence_root=Path(str(ev_root)))
    manifest = service.run_batch(
        study=study, task=task_doc, plan_sha256=plan_sha, blocks=blocks,
        batch_id=str(ctx.get("batch_id") or "s1"))
    # S1 尾债 LOW（AC 计划 §1）：分类器按 contrasts[0] 取主对比，位置依赖
    # 必须显式断言（run_batch 恒把登记 primary 放首位）。
    if (manifest.get("contrasts") or [{}])[0].get("id") != task.primary.id:
        raise SkillError(
            f"PRIMARY_ORDER_DRIFT: contrasts[0]={ (manifest.get('contrasts') or [{}])[0].get('id')!r}"
            f" != 登记 primary {task.primary.id!r}")
    manifest["confirm_plan"] = {
        "study": study, "plan_sha256": plan_sha,
        "conditions": [dict(c) for c in given_docs],
        "blocks": blocks,
        "contrasts": manifest["contrasts"],
        "runs": manifest["runs"],
        "primary_id": task.primary.id,
        "nc_slot": nc_slot_v2,
        "nc_contrast": nc_contrast,
        "nc_sensitivity": ctx.get("nc_sensitivity"),
    }
    return manifest


def _linear_contrast_values(rows, contrast):
    """逐 block 线性对比值（DiD 四项 ±1 与 pairwise 两项 ±1 同式）。"""
    from orchestrator.rsi_bridge.host_recompute import HostRecomputeError
    from orchestrator.rsi_bridge.host_recompute import (
        _linear_contrast_values as impl)

    try:
        return impl(rows, contrast)
    except HostRecomputeError as exc:
        raise SkillError(f"{exc.code}: {exc.detail}") from None


def default_analyzer(run_manifest: Mapping[str, Any], context=None) -> dict:
    """冻结分析：逐对比配对差 + percentile 区间（委托 rsi_r1.statistics）。

    S1 真实路径（manifest 由 ``default_batch_runner`` 产出，含 runs+contrasts+confirm_plan）：
    逐 block 计算**线性对比值**（DiD 四项 ±1 / pairwise 两项 ±1），再走
    ``statistics.estimate`` 的 block 重抽样区间；equivalence 类主张用 90% 区间
    （rubric 口径），其余 95%。
    """
    from orchestrator.rsi_r1 import rubric as R
    from orchestrator.rsi_r1 import statistics

    document = _require_mapping(run_manifest, "run_manifest")
    # K4→K5 的 Contract 是 analysis_program；run_manifest 作为**句柄内嵌**随程序走
    # （blob 引用同构：记录本体在盘上，程序只带引用与摘要），K5 在这里解引用。
    if isinstance(document.get("run_manifest"), Mapping):
        document = _require_mapping(document["run_manifest"], "run_manifest.ref")
    rows = document.get("runs")
    contrasts = document.get("contrasts")
    if not isinstance(rows, list) or not rows:
        raise SkillError("run_manifest.runs 必须是非空列表")
    if not isinstance(contrasts, list) or not contrasts:
        raise SkillError("run_manifest.contrasts 必须是非空列表")
    confirm_plan = document.get("confirm_plan")
    if isinstance(confirm_plan, Mapping) and confirm_plan.get("blocks") \
            and confirm_plan.get("runs"):
        from orchestrator.hk import verdicts as V

        # S1 尾债 LOW：verdict 绑定依赖 contrasts[0] 位置，显式断言登记主对比。
        primary_id = confirm_plan.get("primary_id")
        if primary_id is not None and contrasts and \
                str(contrasts[0].get("id")) != str(primary_id):
            raise SkillError(
                f"PRIMARY_ORDER_DRIFT: contrasts[0]="
                f"{contrasts[0].get('id')!r} != 登记 primary {primary_id!r}")
        results = []
        for contrast in contrasts:
            c = _require_mapping(contrast, "contrast")
            level = (str(R.CI_NEGATIVE) if c.get("proposition") == "equivalent"
                     else str(R.CI_PRIMARY))
            values = _linear_contrast_values(rows, c)
            estimated = statistics.estimate(values, level=level)
            results.append({
                "id": c.get("id"),
                "metric_id": c.get("metric_id"),
                "proposition": c.get("proposition"),
                "delta": c.get("delta"),
                "level": level,
                "estimate": estimated["effect"],
                "ci_low": estimated["lower"],
                "ci_high": estimated["upper"],
                "n_blocks": estimated["n_blocks"],
                # S2 词表 v2（五值；跨零=NOT_DETECTED、带内=EQUIVALENT）
                "claim": V.classify_v2(c.get("proposition"), c.get("delta"),
                                       estimated["lower"], estimated["upper"],
                                       n_blocks=estimated["n_blocks"]),
            })
        # S2-1（计划 §2.1 第 3 行）：每研究强制注册 NC + 三连判据结构拒绝。
        nc_evidence = None
        nc_slot = confirm_plan.get("nc_slot")
        if isinstance(nc_slot, Mapping):
            from orchestrator.hk import nc_registration as N

            nc_contrast = confirm_plan.get("nc_contrast")
            if not isinstance(nc_contrast, Mapping):
                # 与 NC_DEGENERATE 同构：协议级停止码走 GuardViolation，
                # 否则被驱动器的通用异常分支吞成 STAGE_RUNTIME_ERROR（评审 MEDIUM）。
                from orchestrator.hk.guards import GuardViolation

                raise GuardViolation(
                    "g_nc", "NC_UNREGISTERED",
                    "任务槽要求阴性对照，但计划未提名 base/sham")
            nc_values = _linear_contrast_values(rows, nc_contrast)
            nc_est = statistics.estimate(
                nc_values, level=str(nc_slot.get("ci_level") or "0.90"))
            try:
                nc_evidence = N.validate_nc(
                    contrast=nc_contrast,
                    block_values=list(nc_values.values()),
                    estimate=nc_est["effect"], ci_low=nc_est["lower"],
                    ci_high=nc_est["upper"],
                    sensitivity=confirm_plan.get("nc_sensitivity"))
            except N.NCRegistrationError as exc:
                # 结构拒绝 = 协议级停止码（driver 按 GuardViolation 原码上报）
                from orchestrator.hk.guards import GuardViolation

                raise GuardViolation("g_nc", exc.code, exc.detail) from None
        return {"contrasts": results, "confirm_plan": dict(confirm_plan),
                "negative_control": nc_evidence}
    # 旧骨架路径（S0 假件/合成夹具）：confirm_plan.pairs 直接喂 estimate。
    # K6 需要冻结的确认分配（私有 block 配对）；未接线时诚实失败，
    # 不产出注定在 K6 崩掉的半成品（评审 MEDIUM）。
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
    """host 独立复跑。

    * S1 真实路径（plan 带 ``runs``+``conditions``+``blocks``）：调
      ``host_recompute.recompute`` 在全新根逐点重仿真并比对——不一致即停止码
      （不重跑求解，计划 S1-1/停止规则）。
    * 旧骨架路径（plan 带 ``pairs``）：同一统计器在冻结配对值上重算。
    """
    from orchestrator.rsi_r1 import statistics

    plan = _require_mapping(confirm_plan, "confirm_plan")
    if plan.get("frozen") is not True:
        raise SkillEngineNotWired("confirm_plan 未冻结：拒绝在未冻结方法上复跑")
    if plan.get("runs") and plan.get("conditions") and plan.get("blocks"):
        from orchestrator.rsi_bridge import host_recompute

        ctx = context if isinstance(context, Mapping) else {}
        fresh_root = ctx.get("fresh_root")
        if not fresh_root:
            raise SkillEngineNotWired(
                "S1 确认复跑需要 context.fresh_root（全新证据根；禁止复用原批根）")
        try:
            handle = host_recompute.recompute(plan, fresh_root=fresh_root,
                                              batch_id=str(ctx.get("batch_id") or "confirm"))
        except host_recompute.HostRecomputeError as exc:
            raise SkillError(f"{exc.code}: {exc.detail}") from None
        result = dict(handle.confirm_result)
        result["recompute_report"] = {
            "n_diffs": len(handle.diffs),
            "fresh_root": str(handle.fresh_root),
        }
        if handle.diffs:
            raise SkillError(
                "RERUN_POINT_MISMATCH: "
                + json.dumps([d.to_dict() for d in handle.diffs[:5]],
                             ensure_ascii=False))
        # S2b F1.1：confirm_result 必须逐字含 negative_control（NC 注册行 +
        # 三连判据结果 + estimate/CI）。host_recompute 的 replay_rows 有
        # replay_value，NC 对比值从冻结计划的 nc_contrast/nc_slot 计算。
        # 无 NC 槽时恒设 None（键在场，K8 结构检查通过；报告段如实写"未注册"）。
        nc_slot = plan.get("nc_slot")
        nc_contrast = plan.get("nc_contrast")
        result["negative_control"] = None
        if isinstance(nc_slot, Mapping) and isinstance(nc_contrast, Mapping):
            from orchestrator.hk import nc_registration as N

            replay_stat_rows = [{**row, "value": row.get("replay_value")}
                                for row in handle.runs]
            nc_values = _linear_contrast_values(replay_stat_rows, nc_contrast)
            nc_est = statistics.estimate(
                nc_values, level=str(nc_slot.get("ci_level") or "0.90"))
            try:
                nc_evidence = N.validate_nc(
                    contrast=nc_contrast,
                    block_values=list(nc_values.values()),
                    estimate=nc_est["effect"], ci_low=nc_est["lower"],
                    ci_high=nc_est["upper"],
                    sensitivity=plan.get("nc_sensitivity"))
            except N.NCRegistrationError as exc:
                from orchestrator.hk.guards import GuardViolation

                raise GuardViolation("g_nc", exc.code, exc.detail) from None
            result["negative_control"] = nc_evidence
        return result
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
        # S1：study/plan_sha256 等计划元数据必须透传到 K3（跑批需要登记身份），
        # 只重写形状字段；S0 骨架的额外键也原样保留。
        return {**{k: v for k, v in plan.items()
                   if k not in ("conditions", "n_block", "n_cond", "runs",
                                "allocations")},
                "conditions": list(conditions), "n_block": n_block,
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
        manifest = _require_mapping(value, "run_manifest")
        # S2b F3：K4 触点请求瘦身——run 摘要代替全量 manifest（S2 冒烟 41 轮
        # 事故教训：223KB 全量 manifest 使 K4 会话耗尽 max_turns）。
        # 摘要含记录路径（分析程序需要读数据）+ 对比定义 + NC 槽 + 条件/块表；
        # 本体仍在盘上，程序只带引用与摘要（blob 引用同构）。
        runs = manifest.get("runs") or []
        run_summary = {
            "n_runs": len(runs),
            "run_records": [
                {"condition_id": r.get("condition_id"),
                 "block_id": r.get("block_id"),
                 "record_path": r.get("record_path")}
                for r in runs
            ],
            "contrasts": manifest.get("contrasts"),
            "confirm_plan": manifest.get("confirm_plan"),
            "nc_slot": (manifest.get("confirm_plan") or {}).get("nc_slot")
            if isinstance(manifest.get("confirm_plan"), Mapping) else None,
            "nc_contrast": (manifest.get("confirm_plan") or {}).get("nc_contrast")
            if isinstance(manifest.get("confirm_plan"), Mapping) else None,
            "conditions": manifest.get("conditions"),
            "blocks": manifest.get("blocks"),
        }
        program = touchpoint("K4")(  # System 2 触点②：诊断/写分析
            "write_analysis", C.RUN_MANIFEST.name,
            {"run_manifest_summary": run_summary,
             "run_manifest": manifest,
             **({k: v for k, v in value.items() if k == "_ctx"})})
        program = dict(_require_mapping(program, "analysis_program"))
        source = program.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SkillError("analysis_program.source 为空")
        program["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        # 数据句柄随程序走（S1）：K5 只读引用，不改 raw。
        program["run_manifest"] = dict(_require_mapping(value, "run_manifest"))
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
        if not frozen.get("pairs") and not frozen.get("runs"):
            raise SkillError("confirm_plan.pairs/runs 为空：冻结后无确认批可跑")
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
        # S2b F1.2：K8 触点请求强制装配 negative_control 证据与全字段 results。
        # 缺任一 → SkillError 结构拒绝，不发触点（根因修复：K8 缺 NC 证据 →
        # actor 编造"未注册/未验证" → 报告自相矛盾）。
        if "negative_control" not in result:
            raise SkillError(
                "K8_INFO_SURFACE_INCOMPLETE: confirm_result 缺 negative_control"
                "（NC 注册行 + 三连判据 + estimate/CI）；不发触点")
        contrasts = result.get("contrasts")
        if not isinstance(contrasts, list) or not contrasts:
            raise SkillError(
                "K8_INFO_SURFACE_INCOMPLETE: confirm_result 缺 contrasts 行")
        _REQUIRED_RESULT_FIELDS = ("id", "metric_id", "proposition", "delta",
                                   "estimate", "ci_low", "ci_high", "n_blocks",
                                   "claim")
        for i, row in enumerate(contrasts):
            if not isinstance(row, Mapping):
                raise SkillError(
                    f"K8_INFO_SURFACE_INCOMPLETE: contrasts[{i}] 不是对象")
            missing = [k for k in _REQUIRED_RESULT_FIELDS if k not in row]
            if missing:
                raise SkillError(
                    f"K8_INFO_SURFACE_INCOMPLETE: contrasts[{i}] 缺 {missing}")
        # 组装触点请求：results 全字段 + nc_evidence 显式在场
        request_value = dict(result)
        request_value["results"] = list(contrasts)  # 显式别名，触点指令引用
        request_value["nc_evidence"] = result["negative_control"]
        claim = touchpoint("K8")(  # System 2 触点③：解释
            "submit_claim", C.CONFIRM_RESULT.name, request_value)
        claim = dict(_require_mapping(claim, "claim"))
        verdict = claim.get("verdict")
        from orchestrator.hk import verdicts as V

        if verdict not in V.VERDICTS_V2:
            raise SkillError(
                f"claim.verdict 必须是 v2 五值 {V.VERDICTS_V2}，收到 {verdict!r}")
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
