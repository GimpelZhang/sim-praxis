"""Phase 2.8.0 Task 4：三任务的维度评分与事实重算（全部从真实产物重算）。

纪律（D4-4 / D4-5 / AC-T4-7）：

- **权重与阈值一律来自** ``criteria.json``（``weights`` / ``thresholds``），
  本模块**不含任何浮点权/阈值字面量**——机判用 AST 扫本文件的浮点常量
  （``float_literals()``）；
- **物理量从 telemetry 重算**，不把候选自报的 ``metrics.json`` 当真值；
  自报与重算不一致本身就是 Hard Gate（M1 的 HG-M1-7）；
- 每个维度分数落在 ``[0,1]``，``detail`` 逐项写出判据与实测值，
  保证「为什么是这个分」可复算、可人读；
- L1/R1 无数值阈值（Task 1 已冻结 ``thresholds == {}``），其判据是**精确相等**
  的结构检查，不存在 ± 容差。
"""
from __future__ import annotations

import ast
import math
from typing import Any, Mapping

from orchestrator.benchmark.result_schema import Dimension

#: 浮点常量的替代写法（本文件不得出现浮点字面量，AC-T4-7 的 AST 机判）
_ZERO = float(0)
_ONE = float(1)


class ScoringError(RuntimeError):
    """评分输入结构不合法（缺文件/字段类型错）；候选问题应落 Hard Gate 而非抛本异常。"""


#: M1 telemetry 每行的必需字段（Task 4 定死；Task 6 的参考实现按此产出）
MUJOCO_TELEMETRY_FIELDS = ("step", "t", "cube_pos", "target_pos",
                           "max_penetration", "contact_force_norm")

#: M1 候选 metrics.json 与重算对账的键（缺键不判伪造，只对已有键对账）
MUJOCO_METRIC_KEYS = ("endpoint_distance_m", "max_penetration_m",
                      "contact_force_peak_n", "steps")

#: R1 experiment_plan.yaml 的 13 项必填段（路线图 §3.3；键名为 Task 4 定死的机读名）
R1_REQUIRED_SECTIONS = (
    "parameter_definitions",      # 1 参数定义/单位/取值
    "probability_distributions",  # 2 概率分布
    "parameter_correlations",     # 3 相关性假设
    "randomization_method",       # 4 随机化方法
    "sampling_strategy",          # 5 采样策略
    "curriculum",                 # 6 课程/训练调度
    "dataset_splits",             # 7 Train/Val/Holdout
    "random_seeds",               # 8 种子
    "metrics",                    # 9 指标与计算方式
    "ablation_study",             # 10 消融
    "failure_taxonomy",           # 11 失败分类
    "sim_to_real_risks",          # 12 Sim-to-Real 风险
    "conclusion_criteria",        # 13 结论判定标准
)

#: R1 三类必覆盖参数（rubric「Randomization design」）
R1_PARAMETER_CLASSES = ("friction", "contact_stiffness", "joint_damping")

#: 冻结条款：任何产物不得声称已翻案/已取得真机零样本成功（HG-R1-7）
R1_FORBIDDEN_CLAIMS = (
    "零样本成功", "zero-shot 成功", "zero-shot success", "已翻案",
    "冰面实验已成功", "contradiction_control_not_robust 已被推翻",
    # Task 5 负例 r1_07：把「只扩大参数分布」当作 Sim-to-Real 有效性的充分
    # 论证（只扩大分布无法覆盖分布外真实差异，须有 Holdout/风险分析支撑）
    "仅扩大分布",
)


def _clamp(value: float) -> float:
    return max(_ZERO, min(_ONE, value))


def _fraction(passed: int, total: int) -> float:
    if total <= 0:
        return _ZERO
    return _clamp(float(passed) / float(total))


def threshold(criteria: Mapping, name: str) -> float:
    """取 ``criteria.thresholds[name].value``；缺失即报错（不兜底编数值）。"""
    table = criteria.get("thresholds") or {}
    entry = table.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ScoringError(f"criteria 缺阈值 {name!r}（不得在代码里兜底编数值）")
    return float(entry["value"])


_threshold = threshold


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


# --------------------------------------------------------------------------
# M1 — mujoco_contact_push_v1
# --------------------------------------------------------------------------

def _vec3(record: Mapping, key: str) -> "tuple[float, float, float] | None":
    value = record.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        out = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return out  # type: ignore[return-value]


def mujoco_trace(telemetry) -> "list[dict]":
    """逐帧重算中间量（D4-5；落 ``recompute_trace.jsonl``）。"""
    trace = []
    last_index = len(telemetry) - 1
    for index, record in enumerate(telemetry):
        if not isinstance(record, Mapping):
            raise ScoringError(f"telemetry 第 {index} 行不是对象")
        missing = [name for name in MUJOCO_TELEMETRY_FIELDS if name not in record]
        if missing:
            raise ScoringError(f"telemetry 第 {index} 行缺字段 {missing}")
        cube = _vec3(record, "cube_pos")
        target = _vec3(record, "target_pos")
        if cube is None or target is None:
            raise ScoringError(f"telemetry 第 {index} 行 pos 不是三元组")
        trace.append({
            "step": index,
            "endpoint_distance_m": round(
                math.hypot(cube[0] - target[0], cube[1] - target[1]), 9),
            "max_penetration_m": round(float(record["max_penetration"]), 9),
            "contact_force_norm_n": round(float(record["contact_force_norm"]), 9),
            "cube_pos": [round(component, 9) for component in cube],
            "is_last": index == last_index,
        })
    return trace


def mujoco_facts(files: Mapping[str, Any]) -> dict:
    """从 telemetry 重算全部物理量；``metrics.json`` 只用来对账。"""
    telemetry = files.get("telemetry.jsonl")
    if not isinstance(telemetry, list) or not telemetry:
        raise ScoringError("telemetry.jsonl 缺失或为空（不得以 metrics 自报代替）")
    trace = mujoco_trace(telemetry)
    finite_ok = True
    for record in telemetry:
        for value in (*record["cube_pos"], *record["target_pos"],
                      record["max_penetration"], record["contact_force_norm"]):
            if not _finite(value):
                finite_ok = False
    penetrations = [row["max_penetration_m"] for row in trace]
    forces = [row["contact_force_norm_n"] for row in trace]
    first = tuple(float(v) for v in telemetry[0]["cube_pos"])
    last = tuple(float(v) for v in telemetry[-1]["cube_pos"])
    step_ok = all(str(record.get("step")) == str(index)
                  for index, record in enumerate(telemetry))

    recomputed = {
        "steps": len(telemetry),
        "endpoint_distance_m": round(trace[-1]["endpoint_distance_m"], 9),
        "max_penetration_m": round(max(penetrations), 9),
        "contact_force_peak_n": round(max(forces), 9),
    }
    reported = files.get("metrics.json")
    consistent: "bool | None" = None
    diffs: "list[str]" = []
    if isinstance(reported, Mapping):
        checked = 0
        for key in MUJOCO_METRIC_KEYS:
            if key not in reported:
                continue
            checked += 1
            want = float(recomputed[key])
            got = reported[key]
            if not _finite(got) or abs(float(got) - want) > 10 ** -6:
                diffs.append(f"{key}: 自报 {got!r} ≠ 重算 {want!r}")
        if checked:
            consistent = not diffs
    return {
        "recomputed": recomputed,
        "trace": trace,
        "max_penetration_m": recomputed["max_penetration_m"],
        "contact_force_peak_n": recomputed["contact_force_peak_n"],
        "endpoint_distance_m": recomputed["endpoint_distance_m"],
        "steps": recomputed["steps"],
        "all_finite": finite_ok,
        "step_index_monotonic": step_ok,
        "cube_moved": first != last,
        "contact_seen": recomputed["contact_force_peak_n"] > _ZERO,
        "metrics_consistent": consistent,
        "metrics_mismatches": diffs,
    }


def score_mujoco(files: Mapping[str, Any], criteria: Mapping) -> "dict[str, Dimension]":
    facts = mujoco_facts(files)
    weights = criteria["weights"]
    d_max = _threshold(criteria, "endpoint_distance_m")
    pen_max = _threshold(criteria, "max_penetration_m")
    force_min = _threshold(criteria, "contact_force_peak_n_min")
    force_max = _threshold(criteria, "contact_force_peak_n_max")
    steps_min = int(_threshold(criteria, "steps_min"))
    steps_max = int(_threshold(criteria, "steps_max"))
    repro_tol = _threshold(criteria, "repro_abs_tol")

    functional_checks = [
        (facts["endpoint_distance_m"] <= d_max,
         f"末位到目标中心 {facts['endpoint_distance_m']:.9f} m ≤ {d_max} m"),
        (steps_min <= facts["steps"] <= steps_max,
         f"步数 {facts['steps']} ∈ [{steps_min}, {steps_max}]"),
    ]
    physics_checks = [
        (facts["max_penetration_m"] <= pen_max,
         f"最大穿模 {facts['max_penetration_m']:.9f} m ≤ {pen_max} m"),
        (force_min <= facts["contact_force_peak_n"] <= force_max,
         f"接触力峰值 {facts['contact_force_peak_n']:.9f} N ∈ "
         f"[{force_min}, {force_max}]"),
        (facts["all_finite"], "telemetry 数值全部 finite"),
    ]
    repro = files.get("reproducibility.json")
    if isinstance(repro, Mapping):
        same_seed = bool(repro.get("same_seed_identical"))
        delta = repro.get("cross_seed_max_delta_m")
        delta_ok = _finite(delta) and float(delta) <= repro_tol
        repro_checks = [
            (same_seed, f"同种子逐位相等={same_seed}"),
            (delta_ok, f"跨种子最大端态差 {delta!r} m ≤ {repro_tol} m"),
        ]
    else:
        repro_checks = [(False, "缺 reproducibility.json（不可复现不得沉默通过）")]

    outputs = ("scene_spec.json", "metrics.json", "telemetry.jsonl")
    present = sum(1 for name in outputs if isinstance(files.get(name), (dict, list)))
    evidence_checks = [(present == len(outputs),
                        f"候选产物齐全 {present}/{len(outputs)}"),
                       (facts["step_index_monotonic"],
                        "telemetry step 序号连续")]

    def dim(key: str, checks) -> Dimension:
        passed = sum(1 for ok, _ in checks if ok)
        detail = "; ".join(("✔ " if ok else "✘ ") + text for ok, text in checks)
        return Dimension(weight=float(weights[key]), detail=detail,
                         score=_fraction(passed, len(checks)))

    return {
        "functional_correctness": dim("functional_correctness", functional_checks),
        "physics_validity": dim("physics_validity", physics_checks),
        "reproducibility": dim("reproducibility", repro_checks),
        "evidence_completeness": dim("evidence_completeness", evidence_checks),
    }


# --------------------------------------------------------------------------
# L1 — libero_env_adapter_v1（结构判据，无数值阈值）
# --------------------------------------------------------------------------

def _norm_python(entry: Any) -> str:
    return str(entry or "").strip()


def libero_facts(files: Mapping[str, Any]) -> dict:
    birth = files.get("birth/functional_birth.json")
    levels = (birth or {}).get("levels") if isinstance(birth, Mapping) else None
    status = {name: (levels or {}).get(name, {}).get("status")
              for name in ("B0", "B1", "B2", "B3", "B4")}
    probe = files.get("env_probe.json")
    probe = probe if isinstance(probe, Mapping) else {}
    one_step = files.get("one_step_result.json")
    one_step = one_step if isinstance(one_step, Mapping) else {}
    fingerprint = files.get("runtime_fingerprint.json")
    fingerprint = fingerprint if isinstance(fingerprint, Mapping) else {}

    keys = probe.get("observation_keys")
    obs_ok = isinstance(keys, (list, tuple)) and len(keys) > 0
    action_dim = probe.get("action_dim")
    action_ok = isinstance(action_dim, int) and not isinstance(action_dim, bool) \
        and action_dim > 0
    fp_action = fingerprint.get("action_contract") or {}
    fp_obs = fingerprint.get("observation_contract") or {}
    # Task 5：schema 合法的指纹把契约放在 simulator 段（action_contract.dim /
    # observation_contract.keys）；顶层的旧形态（Task 4 合成样本）优先，两者
    # 都认，但不臆造：都没有就按「无可对契约」处理。
    simulator = fingerprint.get("simulator") or {}
    if isinstance(simulator, Mapping):
        if not isinstance(fp_action, Mapping) or \
                fp_action.get("action_dim") is None:
            sim_action = simulator.get("action_contract")
            if isinstance(sim_action, Mapping) and sim_action.get("dim") is not None:
                fp_action = {"action_dim": sim_action.get("dim")}
        if not isinstance(fp_obs, Mapping) or fp_obs.get("keys") is None:
            sim_obs = simulator.get("observation_contract")
            if isinstance(sim_obs, Mapping) and sim_obs.get("keys") is not None:
                fp_obs = {"keys": sim_obs.get("keys")}
    contract_match = True
    contract_notes = []
    if isinstance(fp_action, Mapping) and fp_action.get("action_dim") is not None:
        want = fp_action.get("action_dim")
        contract_match &= int(want) == (action_dim if action_ok else -1)
        contract_notes.append(f"action_dim 探针 {action_dim} vs 指纹 {want}")
    if isinstance(fp_obs, Mapping) and fp_obs.get("keys") is not None:
        want = tuple(fp_obs.get("keys"))
        got = tuple(keys) if obs_ok else ()
        contract_match &= want == got
        contract_notes.append(f"obs keys 探针 {list(got)} vs 指纹 {list(want)}")

    # HG-L1-2：探针必须与同一份运行身份自洽（假 env_spec 的面）
    fp_python = fingerprint.get("python") or {}
    fp_container = fingerprint.get("container") or {}
    probe_exe = _norm_python(probe.get("python_executable"))
    probe_ver = _norm_python(probe.get("python_version"))
    fp_exe = _norm_python(fp_python.get("executable"))
    fp_ver = _norm_python(fp_python.get("version"))
    captured_in = str(probe.get("captured_in") or "")
    backend = str(fp_container.get("backend") or "")
    expect_captured = {"docker": "container", "host": "host"}.get(backend, "")
    probe_consistent: "bool | None"
    if not fingerprint or not probe:
        probe_consistent = None
    else:
        probe_consistent = (
            probe_exe == fp_exe
            and (probe_ver == fp_ver
                 or (probe_ver and fp_ver.startswith(f"{probe_ver}.")))
            and (not expect_captured or captured_in == expect_captured)
        )

    state_values = list((one_step.get("state") or {}).values()) \
        if isinstance(one_step.get("state"), Mapping) else []
    advanced = one_step.get("advanced") is True
    finite_ok = bool(state_values) and all(_finite(v) for v in state_values)
    return {
        "birth_status": (birth or {}).get("birth_status") if isinstance(
            birth, Mapping) else None,
        "levels": status,
        "action_dim": action_dim,
        "observation_keys": list(keys) if obs_ok else [],
        "action_ok": action_ok,
        "obs_ok": obs_ok,
        "contract_match": contract_match,
        "contract_notes": contract_notes or ["无指纹契约段可对（不臆造）"],
        "probe_consistent": probe_consistent,
        "probe_python": (probe_exe, probe_ver),
        "fingerprint_python": (fp_exe, fp_ver),
        "captured_in": captured_in,
        "uploaded_backend": backend,
        "advanced": advanced,
        "state_finite": finite_ok,
        "fingerprint_present": bool(fingerprint),
    }


def score_libero(files: Mapping[str, Any], criteria: Mapping) -> "dict[str, Dimension]":
    facts = libero_facts(files)
    weights = criteria["weights"]

    import_checks = [
        (facts["levels"].get("B1") == "PASS", f"B1={facts['levels'].get('B1')}"),
        (facts["levels"].get("B2") == "PASS", f"B2={facts['levels'].get('B2')}"),
    ]
    step_checks = [
        (facts["levels"].get("B4") == "PASS", f"B4={facts['levels'].get('B4')}"),
        (facts["advanced"], f"one_step advanced={facts['advanced']}"),
        (facts["state_finite"], "one_step 状态全部 finite"),
    ]
    contract_checks = [
        (facts["action_ok"], f"action_dim={facts['action_dim']}"),
        (facts["obs_ok"], f"observation_keys={len(facts['observation_keys'])} 个"),
        (facts["contract_match"], "; ".join(facts["contract_notes"])),
    ]
    identity_checks = [
        (facts["fingerprint_present"], "runtime_fingerprint.json 在场"),
        (facts["probe_consistent"] is True,
         f"探针与指纹自洽（探针 python {facts['probe_python']} / 指纹 "
         f"{facts['fingerprint_python']} / captured_in={facts['captured_in']!r}）"),
    ]

    outputs = ("adapter.py", "env_probe.json", "one_step_result.json",
               "runtime_fingerprint.json")
    present = sum(1 for name in outputs if files.get(name) is not None)
    evidence_checks = [(present == len(outputs),
                        f"候选产物齐全 {present}/{len(outputs)}")]

    def dim(key: str, checks) -> Dimension:
        passed = sum(1 for ok, _ in checks if ok)
        detail = "; ".join(("✔ " if ok else "✘ ") + text for ok, text in checks)
        return Dimension(weight=float(weights[key]), detail=detail,
                         score=_fraction(passed, len(checks)))

    return {
        "import_and_construction": dim("import_and_construction", import_checks),
        "reset_and_step": dim("reset_and_step", step_checks),
        "observation_action_contract": dim("observation_action_contract",
                                           contract_checks),
        "runtime_identity": dim("runtime_identity", identity_checks),
        "evidence_completeness": dim("evidence_completeness", evidence_checks),
    }


# --------------------------------------------------------------------------
# R1 — icy_ground_dr_v1（13 项结构检查，无阈值）
# --------------------------------------------------------------------------

def _section(plan: Mapping, name: str) -> Any:
    return plan.get(name) if isinstance(plan, Mapping) else None


def _split_ids(entry: Any) -> "set[str]":
    if not isinstance(entry, Mapping):
        return set()
    out: "set[str]" = set()
    for key in ("ids", "episodes", "tasks", "scenarios"):
        values = entry.get(key)
        if isinstance(values, (list, tuple)):
            out |= {str(item) for item in values}
    return out


def research_facts(files: Mapping[str, Any]) -> dict:
    plan = files.get("experiment_plan.yaml")
    plan = plan if isinstance(plan, Mapping) else {}
    report = files.get("research_report.md")
    report = report if isinstance(report, str) else ""
    space = files.get("parameter_space.json")
    space = space if isinstance(space, Mapping) else {}
    metrics_doc = files.get("evaluation_metrics.json")
    metrics_doc = metrics_doc if isinstance(metrics_doc, Mapping) else {}

    present_sections = [name for name in R1_REQUIRED_SECTIONS
                        if _section(plan, name) not in (None, "", [], {})]
    missing_sections = [name for name in R1_REQUIRED_SECTIONS
                        if name not in present_sections]

    params = space.get("parameters")
    params = params if isinstance(params, list) else []
    classes = {str(p.get("class")) for p in params if isinstance(p, Mapping)}
    uncovered = [name for name in R1_PARAMETER_CLASSES if name not in classes]
    units_ok = bool(params) and all(
        isinstance(p, Mapping) and str(p.get("unit") or "").strip()
        for p in params)

    correlations = _section(plan, "parameter_correlations")
    corr_ok = isinstance(correlations, list) and any(
        isinstance(item, Mapping) and str(item.get("rationale") or "").strip()
        for item in correlations)

    ablation = _section(plan, "ablation_study")
    ablation_ok = isinstance(ablation, list) and len(ablation) >= 3
    splits = _section(plan, "dataset_splits")
    splits_ok = isinstance(splits, Mapping) and set(
        ("train", "validation", "holdout")) <= set(splits)
    holdout = splits.get("holdout") if isinstance(splits, Mapping) else None
    holdout_ok = holdout not in (None, "", [], {})
    ids = {name: _split_ids(splits.get(name)) for name in
           ("train", "validation", "holdout")} if isinstance(splits, Mapping) else {}
    overlap = sorted((ids.get("train", set()) & ids.get("holdout", set())))
    disjoint_declared = bool(isinstance(splits, Mapping)
                             and splits.get("disjoint") is True)
    leak_ok = disjoint_declared and not overlap
    seeds = _section(plan, "random_seeds")
    seeds_ok = bool(seeds)

    metrics_list = _section(plan, "metrics")
    metrics_ok = isinstance(metrics_list, list) and bool(metrics_list)
    mapped = [str(item.get("maps_to")) for item in (metrics_list or [])
              if isinstance(item, Mapping) and item.get("maps_to")]
    mapped_ok = bool(mapped) and set(mapped) <= set(R1_REQUIRED_SECTIONS)
    stats_ok = isinstance(metrics_doc.get("metrics"), list) and any(
        isinstance(m, Mapping) and (m.get("statistical_test") or m.get("ci"))
        for m in metrics_doc.get("metrics") or [])
    taxonomy = _section(plan, "failure_taxonomy")
    taxonomy_ok = bool(taxonomy)

    risks = _section(plan, "sim_to_real_risks")
    risks_ok = bool(risks)
    claims = " ".join([report, str(plan)])
    forbidden = [phrase for phrase in R1_FORBIDDEN_CLAIMS if phrase in claims]

    conclusion = _section(plan, "conclusion_criteria")
    refs = (conclusion or {}).get("evidence_refs") if isinstance(
        conclusion, Mapping) else None
    trace_ok = isinstance(refs, list) and len(refs) > 0
    outcome = metrics_doc.get("conclusion")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    criterion_met = outcome.get("criterion_met")
    claimed_success = outcome.get("claimed_success")
    contradiction = claimed_success is True and criterion_met is False

    return {
        "present_sections": present_sections,
        "missing_sections": missing_sections,
        "uncovered_classes": uncovered,
        "units_ok": units_ok,
        "corr_ok": corr_ok,
        "ablation_ok": ablation_ok,
        "ablation_count": len(ablation) if isinstance(ablation, list) else 0,
        "splits_ok": splits_ok,
        "holdout_ok": holdout_ok,
        "disjoint_declared": disjoint_declared,
        "overlap_ids": overlap,
        "leak_ok": leak_ok,
        "seeds_ok": seeds_ok,
        "metrics_ok": metrics_ok,
        "mapped_ok": mapped_ok,
        "stats_ok": stats_ok,
        "taxonomy_ok": taxonomy_ok,
        "risks_ok": risks_ok,
        "forbidden_claims": forbidden,
        "trace_ok": trace_ok,
        "conclusion_structured": isinstance(criterion_met, bool)
        and isinstance(claimed_success, bool),
        "contradiction": contradiction,
        "research_problem_ok": bool(_section(plan, "research_problem")),
        "report_ok": bool(report.strip()),
    }


def score_research(files: Mapping[str, Any], criteria: Mapping) -> "dict[str, Dimension]":
    facts = research_facts(files)
    weights = criteria["weights"]

    checks = {
        "research_problem_formulation": [
            (facts["research_problem_ok"], "research_problem 段非空"),
            (facts["report_ok"], "research_report.md 非空"),
        ],
        "randomization_design": [
            (not facts["missing_sections"] and bool(facts["present_sections"]),
             f"13 项必填段齐 {len(facts['present_sections'])}/13"),
            (not facts["uncovered_classes"],
             f"三类参数覆盖（缺 {facts['uncovered_classes'] or '无'}）"),
            (facts["units_ok"], "参数全部带物理单位"),
        ],
        "parameter_relationships": [
            (facts["corr_ok"], "≥1 组相关性且给理由"),
        ],
        "experimental_design": [
            (facts["splits_ok"], "Train/Validation/Holdout 三段齐全"),
            (facts["holdout_ok"], "Holdout 非空（独立留出）"),
            (facts["leak_ok"], "互斥声明且 id 无重叠"),
            (facts["ablation_ok"], f"Ablation {facts['ablation_count']} 组 ≥ 3"),
            (facts["seeds_ok"], "种子显式"),
            (facts["taxonomy_ok"], "失败分类可操作"),
        ],
        "metrics_and_statistics": [
            (facts["metrics_ok"], "指标清单非空"),
            (facts["stats_ok"], "含统计检验或置信区间"),
            (facts["mapped_ok"], "指标映射到 13 项必填段"),
        ],
        "sim_to_real_validity": [
            (facts["risks_ok"], "Sim-to-Real 风险与不可消除项非空"),
            (not facts["forbidden_claims"],
             f"未出现冻结条款禁止措辞（命中 {facts['forbidden_claims'] or '无'}）"),
        ],
        "evidence_traceability": [
            (facts["trace_ok"], "结论判定标准带 evidence_refs"),
            (facts["conclusion_structured"], "结论产出含 criterion_met/claimed_success"),
        ],
    }

    out: "dict[str, Dimension]" = {}
    for key, items in checks.items():
        passed = sum(1 for ok, _ in items if ok)
        detail = "; ".join(("✔ " if ok else "✘ ") + text for ok, text in items)
        out[key] = Dimension(weight=float(weights[key]), score=_fraction(
            passed, len(items)), detail=detail)
    return out


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------

SCORERS = {
    "mujoco_contact_push_v1": score_mujoco,
    "libero_env_adapter_v1": score_libero,
    "icy_ground_dr_v1": score_research,
}

FACT_EXTRACTORS = {
    "mujoco_contact_push_v1": mujoco_facts,
    "libero_env_adapter_v1": libero_facts,
    "icy_ground_dr_v1": research_facts,
}


def score_task(task_id: str, files: Mapping[str, Any],
               criteria: Mapping) -> "dict[str, Dimension]":
    try:
        scorer = SCORERS[task_id]
    except KeyError:
        raise ScoringError(f"任务 {task_id!r} 没有评分器") from None
    return scorer(files, criteria)


def facts_task(task_id: str, files: Mapping[str, Any]) -> dict:
    try:
        extractor = FACT_EXTRACTORS[task_id]
    except KeyError:
        raise ScoringError(f"任务 {task_id!r} 没有事实抽取器") from None
    return extractor(files)


def float_literals(source: str) -> "list[float]":
    """AST 收集源码里的浮点常量（AC-T4-7 机判：本模块必须为空）。"""
    tree = ast.parse(source)
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, float)]
