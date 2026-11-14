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
import json
import math
import re
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

#: 判定否定语境的标记词（评审 W3-08）。只在**同一分句、且位于禁语之前**的窗口里找：
#: 「不能仅扩大分布就宣称鲁棒」→ 命中禁语「仅扩大分布」但同分句前缀含「不能」⇒ 不算
#: 肯定式违规。「本方案不声称零样本成功」同理。这是**收紧误杀**，不是放松判据：
#: 真肯定句（无否定前缀）照样命中。
_NEGATION_MARKERS = ("不", "未", "禁止", "无法", "没有", "不得", "避免", "杜绝", "并非")
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]")
_CLAUSE_SPLIT = re.compile(r"[，,；;：:]")

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
    """取 ``criteria.thresholds[name].value``；缺失即报错（不兜底编数值）。

    评审 W1-12：``unit == "step"`` 的阈值是**步数**，必须是整数。早先
    ``float(value)`` 把 534.5 也照单全收，随后被 ``int()`` 悄悄截断——判据
    被静默改写。这里 fail-closed，整数以外的写法（含 534.0 浮点字面量）一律报错。
    """
    table = criteria.get("thresholds") or {}
    entry = table.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ScoringError(f"criteria 缺阈值 {name!r}（不得在代码里兜底编数值）")
    value = entry["value"]
    if str(entry.get("unit") or "").strip() == "step":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ScoringError(
                f"阈值 {name!r} 单位为 step，value 必须是整数：{value!r}")
        return float(value)
    return float(value)


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


def _step_index_ok(telemetry) -> bool:
    """``step`` 必须是整数、严格递增、末首差 = 行数−1（基序无关，评审 W3-11）。"""
    steps = []
    for index, record in enumerate(telemetry):
        value = record.get("step") if isinstance(record, Mapping) else None
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        steps.append(value)
    if len(steps) < 1:
        return False
    if any(b <= a for a, b in zip(steps, steps[1:])):
        return False
    return steps[-1] - steps[0] == len(steps) - 1


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


def affirmative_claims(text: str,
                       phrases: "tuple[str, ...]" = R1_FORBIDDEN_CLAIMS
                       ) -> "list[str]":
    """找出**肯定式**出现的禁语（评审 W3-08）。

    早先用 ``phrase in text`` 的整串子串匹配，一份明确写着「本方案不声称零样本
    成功」「不能仅扩大分布就宣称鲁棒」的合法计划会被 HG-R1-7 误杀。这里按句/分句
    切开，只在「同一分句内、禁语之前没有否定标记」时才算命中。
    """
    hits: "set[str]" = set()
    for sentence in _SENTENCE_SPLIT.split(str(text)):
        for clause in _CLAUSE_SPLIT.split(sentence):
            for phrase in phrases:
                index = clause.find(phrase)
                if index < 0:
                    continue
                window = clause[:index]
                if any(marker in window for marker in _NEGATION_MARKERS):
                    continue
                hits.add(phrase)
    return sorted(hits)


def _repro_facts(files: Mapping[str, Any]) -> dict:
    """从**两次运行各自的 telemetry/metrics** 重算复现结论（评审 W3-10）。

    早先 ``same_seed_identical`` / ``cross_seed_max_delta_m`` 直接取自候选
    提交的 ``reproducibility.json``——写两个常量就能同时过 HG-EVAL-4 与 20%
    复现维度。参考 harness 现在把同种子重跑与换种子运行的 ``metrics.json`` /
    ``telemetry.jsonl`` 一并放进产物（``repro/same_seed|repro/cross_seed``），
    本函数据此重算；缺任何一份即 ``independent=False``（不许退回自报）。
    """
    primary_t = files.get("telemetry.jsonl")
    same_t = files.get("repro/same_seed/telemetry.jsonl")
    cross_t = files.get("repro/cross_seed/telemetry.jsonl")
    primary_m = files.get("metrics.json")
    same_m = files.get("repro/same_seed/metrics.json")
    cross_m = files.get("repro/cross_seed/metrics.json")
    notes: "list[str]" = []
    have_primary = isinstance(primary_t, list) and isinstance(primary_m, Mapping)
    have_same = isinstance(same_t, list) and isinstance(same_m, Mapping)
    have_cross = isinstance(cross_t, list) and isinstance(cross_m, Mapping)
    independent = have_primary and have_same and have_cross
    if not independent:
        missing = [name for name, ok in (
            ("telemetry.jsonl", have_primary),
            ("repro/same_seed/telemetry.jsonl", have_same),
            ("repro/cross_seed/telemetry.jsonl", have_cross),
        ) if not ok]
        notes.append(f"缺 {missing}：无法独立重算复现（不得以自报代替）")
        return {"independent": False, "same_seed_identical": None,
                "cross_seed_max_delta_m": None, "notes": notes}

    telemetry_same = primary_t == same_t
    keys = (set(primary_m) | set(same_m)) - {"wall_time_seconds"}
    metrics_same = all(primary_m.get(key) == same_m.get(key) for key in keys)
    if not telemetry_same:
        notes.append("同种子 telemetry 不逐字段相等")
    if not metrics_same:
        notes.append("同种子 metrics 不逐字段相等（排除 wall_time_seconds）")
    delta = None
    try:
        delta = max(abs(float(primary_m["endpoint_distance_m"])
                        - float(cross_m["endpoint_distance_m"])),
                    abs(float(primary_m["max_penetration_m"])
                        - float(cross_m["max_penetration_m"])))
        if not math.isfinite(delta):
            notes.append("跨种子差值非有限")
            delta = None
    except (KeyError, TypeError, ValueError) as exc:
        notes.append(f"跨种子 metrics 缺数值字段：{exc}")
    return {"independent": True,
            "same_seed_identical": telemetry_same and metrics_same,
            "cross_seed_max_delta_m": delta,
            "repro_telemetry_identical": telemetry_same,
            "repro_metrics_identical": metrics_same,
            "notes": notes}


def mujoco_facts(files: Mapping[str, Any], criteria: Mapping) -> dict:
    """从 telemetry 重算全部物理量；``metrics.json`` 只用来对账。

    ``criteria`` 必需：自报与重算的对账容差 ``metrics_abs_tol`` 是**判据数据**，
    不得在代码里写浮点字面量（评审 W3-14 / W1-04：早先的 ``10 ** -6`` 是
    ``BinOp``，逃过了 AC-T4-7 的 ``Constant(float)`` AST 扫描）。
    """
    abs_tol = threshold(criteria, "metrics_abs_tol")
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
    #: 评审 W3-11：早先强制 0 基连续（``step == index``），而判据从未声明基序——
    #: 一份从 1 起的合法 telemetry 会被判失败。改为**基序无关**的等价检查：
    #: 整数、严格递增、且末值与首值之差恰为「行数 − 1」（不跳步、不重复）。
    step_ok = _step_index_ok(telemetry)

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
        #: 评审 CR-32：候选契约（``task_prompt._acceptance_block``）要求
        #: ``metrics.json`` **至少含** ``MUJOCO_METRIC_KEYS`` 全部键。早先
        #: 「缺键不判伪造，只对已有键对账」，于是 ``metrics.json = {}``（或只写
        #: ``{"steps": N}``）时 ``checked == 0`` ⇒ ``metrics_consistent=None``
        #: ⇒ HG-M1-7 不适用、v2 的 CRIT_metrics_consistency 也空过，整个
        #: 「自报 vs 重算」门从未执行。缺键本身即对账失败。
        for key in MUJOCO_METRIC_KEYS:
            if key not in reported:
                diffs.append(f"{key}: 自报缺失（契约要求 metrics.json 至少含全部指标键）")
                continue
            want = float(recomputed[key])
            got = reported[key]
            if not _finite(got) or abs(float(got) - want) > abs_tol:
                diffs.append(f"{key}: 自报 {got!r} ≠ 重算 {want!r}")
        consistent = not diffs
    return {
        "recomputed": recomputed,
        "metrics_abs_tol": abs_tol,
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
        "repro": _repro_facts(files),
    }


def score_mujoco(files: Mapping[str, Any], criteria: Mapping) -> "dict[str, Dimension]":
    facts = mujoco_facts(files, criteria)
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
    repro = facts.get("repro") or {}
    if repro.get("independent"):
        same_seed = repro.get("same_seed_identical") is True
        delta = repro.get("cross_seed_max_delta_m")
        delta_ok = _finite(delta) and float(delta) <= repro_tol
        repro_checks = [
            (same_seed, f"[重算] 同种子两份 telemetry/metrics 逐字段相等="
                         f"{same_seed}"),
            (delta_ok, f"[重算] 跨种子最大端态差 {delta!r} m ≤ {repro_tol} m"),
        ]
    else:
        repro_checks = [(False, "缺同种子/换种子两份 telemetry，无法独立重算复现"
                                "（不得以自报代替）")]

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


def libero_facts(files: Mapping[str, Any], criteria: Mapping | None = None) -> dict:
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
    #: 评审 W3-02：早先 ``contract_match`` 初值即 ``True``，指纹里**没有**契约段时
    #: 硬门 HG-L1-5 永不触发、0.2 权重的契约维度照样满分——删掉 ``simulator``
    #: 段就能豁免。现在改为「至少实际对过一段且逐段相符才算 True」，没有可对契约
    #: 一律 False（判据原文是「与 task.yaml 一致」，无契约即无法证明一致）。
    #: 评审 W3-03：候选可控字段一律先做类型守卫再比较，畸形值抛 ``ScoringError``
    #: （评测器落 INVALID_ARTIFACT），而不是让 ``int("abc")`` 抛未捕获的 ValueError。
    contract_match = False
    compared = 0
    mismatches: "list[str]" = []
    contract_notes = []
    if isinstance(fp_action, Mapping) and fp_action.get("action_dim") is not None:
        want = fp_action.get("action_dim")
        if not isinstance(want, int) or isinstance(want, bool):
            raise ScoringError(f"指纹 action_contract.dim 不是整数：{want!r}")
        got_dim = action_dim if action_ok else -1
        compared += 1
        if want != got_dim:
            mismatches.append(f"action_dim 指纹 {want!r} ≠ 探针 {got_dim!r}")
        contract_notes.append(f"action_dim 探针 {action_dim!r} vs 指纹 {want!r}")
    if isinstance(fp_obs, Mapping) and fp_obs.get("keys") is not None:
        want_keys = fp_obs.get("keys")
        if not isinstance(want_keys, (list, tuple)):
            raise ScoringError(
                f"指纹 observation_contract.keys 不是列表：{want_keys!r}")
        want = tuple(str(item) for item in want_keys)
        got = tuple(str(item) for item in keys) if obs_ok else ()
        compared += 1
        if want != got:
            mismatches.append(f"obs keys 指纹 {list(want)} ≠ 探针 {list(got)}")
        contract_notes.append(f"obs keys 探针 {list(got)} vs 指纹 {list(want)}")
    if compared:
        contract_match = not mismatches
    else:
        contract_notes.append("指纹无契约段可对（无法证明与 task.yaml 一致）")

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
    #: AC-13 的机判口径是「每个任务都有非空的 ``recompute_trace.jsonl``」。
    #: L1 没有逐行数值 telemetry 可重算，能重算的是**从候选快照里抽出来的
    #: 结构事实**（一个输入件一行）——与 M1 的逐行重算同性质：都由评测器自己
    #: 读件得出，不采信候选自报的布尔。
    trace = [
        {"artifact": "birth/functional_birth.json", "levels": dict(status)},
        {"artifact": "env_probe.json", "action_dim": action_dim,
         "observation_keys": list(keys) if obs_ok else []},
        {"artifact": "runtime_fingerprint.json", "backend": backend,
         "contracts_compared": compared, "contract_match": contract_match},
        {"artifact": "one_step_result.json", "advanced": advanced,
         "state_finite": finite_ok},
    ]
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
        "trace": trace,
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


def _split_id_list(entry: Any) -> "list[str]":
    """把一个 split 条目里声明的 id 按原序收集（**保留重复**，供 CR-60 计数）。"""
    if not isinstance(entry, Mapping):
        return []
    out: "list[str]" = []
    for key in ("ids", "episodes", "tasks", "scenarios"):
        values = entry.get(key)
        if isinstance(values, (list, tuple)):
            out.extend(str(item) for item in values)
    return out


def _number(value: Any) -> "float | None":
    """只认真正的数（``bool`` 是 ``int`` 的子类，必须排除）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bounds(entry: Any) -> "tuple[float, float] | None":
    """取 ``range`` 或 ``support`` 的 [lo, hi]，且必须 lo < hi（评审 W3-06）。"""
    if not isinstance(entry, Mapping):
        return None
    bounds = entry.get("range")
    if bounds is None:
        bounds = entry.get("support")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        return None
    low, high = _number(bounds[0]), _number(bounds[1])
    if low is None or high is None or not low < high:
        return None
    return (low, high)


def _json_pointer(target: Any, pointer: str) -> bool:
    if pointer in ("", "/"):
        return target not in (None, "", [], {})
    parts = [part.replace("~1", "/").replace("~0", "~")
             for part in pointer.lstrip("/").split("/") if part != ""]
    node = target
    for part in parts:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.isdigit() \
                and int(part) < len(node):
            node = node[int(part)]
        else:
            return False
    return node not in (None, "", [], {})


def _resolve_ref(ref: Any, plan: Mapping, files: Mapping) -> bool:
    """``evidence_refs`` 每一条都要能指回**真实条目**（评审 W3-06）。

    支持三种形态：``file.json#/json/pointer``（在候选产物里逐段解析）、
    ``file.md#散文锚点``（锚点文本必须真的出现在该文件正文里）、
    以及裸段名 / 指标名 / 消融名 / 参数名。
    """
    text = str(ref or "").strip()
    if not text:
        return False
    if "#" in text:
        name, _, pointer = text.partition("#")
        name = name.strip()
        if name not in files:
            return False
        target = files[name]
        if pointer and isinstance(target, str):
            return pointer.strip() in target
        return _json_pointer(target, pointer)
    if _section(plan, text) not in (None, "", [], {}):
        return True
    for key in ("metrics", "ablation_study", "parameter_definitions"):
        items = _section(plan, key)
        if isinstance(items, list) and any(
                isinstance(item, Mapping) and str(item.get("name")) == text
                for item in items):
            return True
    return False


def research_facts(files: Mapping[str, Any], criteria: Mapping) -> dict:
    ablation_min = int(threshold(criteria, "ablation_min_groups"))
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

    #: 评审 W3-06：「Randomization design」既要「范围」也要「物理依据」。
    #: 早先从不读取 range 值，空壳段可拿满分。现在每一定义项必须给出合法
    #: ``range``/``support``（lo < hi）与一条非空 ``rationale``，且三类必覆盖
    #: 参数的分布段同样要带 rationale。
    definitions = plan.get("parameter_definitions")
    definitions = definitions if isinstance(definitions, list) else []
    ranges_ok = bool(definitions) and all(
        isinstance(entry, Mapping) and _bounds(entry) is not None
        and bool(str(entry.get("rationale") or "").strip())
        for entry in definitions)
    distributions = _section(plan, "probability_distributions")
    distributions = distributions if isinstance(distributions, Mapping) else {}
    distribution_basis_ok = all(
        isinstance(distributions.get(name), Mapping)
        and _bounds(distributions[name]) is not None
        and bool(str(distributions[name].get("rationale") or "").strip())
        for name in R1_PARAMETER_CLASSES)

    #: 评审 W3-06：「问题陈述与原文一致；不改 RL 算法约束被显式承认」。
    problem = _section(plan, "research_problem")
    problem_structured = isinstance(problem, Mapping) and bool(
        str(problem.get("question") or "").strip())
    rl_flag = problem.get("rl_constraints_unchanged") \
        if isinstance(problem, Mapping) else None
    if rl_flag is None:
        rl_flag = plan.get("rl_constraints_unchanged")
    rl_constraints_ok = rl_flag is True

    correlations = _section(plan, "parameter_correlations")
    corr_ok = isinstance(correlations, list) and any(
        isinstance(item, Mapping) and str(item.get("rationale") or "").strip()
        for item in correlations)

    ablation = _section(plan, "ablation_study")
    ablation_ok = isinstance(ablation, list) and len(ablation) >= ablation_min
    splits = _section(plan, "dataset_splits")
    splits_ok = isinstance(splits, Mapping) and set(
        ("train", "validation", "holdout")) <= set(splits)
    holdout = splits.get("holdout") if isinstance(splits, Mapping) else None
    split_names = ("train", "validation", "holdout")
    split_lists = ({name: _split_id_list(splits.get(name))
                    for name in split_names}
                   if isinstance(splits, Mapping) else {})
    split_ids = {name: set(values) for name, values in split_lists.items()}
    ids_nonempty = all(bool(split_ids.get(name)) for name in split_names)
    #: 评审 CR-60：早先 id 直接并进 ``set``，split 内重复（``["e1", "e1"]``）
    #: 塌成一个，只检测跨 split 重叠。于是「声明 2 个 holdout episode、实际
    #: 只有 1 个唯一 id」也能过 ``holdout_ok``/``leak_ok``。列表长度与集合
    #: 长度不等即设计失败（同一 id 在同一 split 里出现两次）。
    split_duplicates: "dict[str, list[str]]" = {}
    for name in split_names:
        values = split_lists.get(name) or []
        duplicated = sorted({item for item in values if values.count(item) > 1})
        if duplicated:
            split_duplicates[name] = duplicated
    #: Holdout 非空**且**其 id 列表无重复——「2 个 episode 塌成 1 个唯一 id」
    #: 不是独立留出，HG-R1-4 与 HG-R1-3 同时判负。
    holdout_ok = (holdout not in (None, "", [], {})
                  and not split_duplicates.get("holdout"))
    overlap: "list[str]" = []
    pairs = ((0, 1), (0, 2), (1, 2))
    for left, right in pairs:
        shared = split_ids.get(split_names[left], set()) & \
            split_ids.get(split_names[right], set())
        overlap.extend(sorted(shared))
    overlap = sorted(set(overlap))
    disjoint_declared = bool(isinstance(splits, Mapping)
                             and splits.get("disjoint") is True)
    #: 评审 W3-07：判据是**实际互斥**，不是「声明了 disjoint 布尔」。
    #: 早先 ``leak_ok = disjoint_declared and not overlap``：三段都给空 id 时
    #: 交集为空即可空转通过，而真实无重叠但没写 ``disjoint: true`` 的合法计划
    #: 反被 HG-R1-3 误杀。现在要求三段 id 全部非空、段内无重复且两两无交集；
    #: ``disjoint`` 只作为附加声明保留在 facts 里。
    leak_ok = ids_nonempty and not split_duplicates and not overlap
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
    #: 评审 W3-08：禁语只在**肯定句**里算违规。整串子串匹配会把
    #: 「本方案不声称零样本成功」「不能仅扩大分布就宣称鲁棒」这类合法否认句
    #: 判成 HG-R1-7 违规。``json.dumps`` 让计划的结构化字段以逗号分隔，
    #: 与 ``affirmative_claims`` 的分句口径对齐。
    claims = report + "\n" + json.dumps(plan, ensure_ascii=False,
                                        sort_keys=True, default=str)
    forbidden = affirmative_claims(claims)

    conclusion = _section(plan, "conclusion_criteria")
    refs = (conclusion or {}).get("evidence_refs") if isinstance(
        conclusion, Mapping) else None
    refs_listed = isinstance(refs, list) and len(refs) > 0
    unresolved = [str(ref) for ref in (refs or [])
                  if refs_listed and not _resolve_ref(ref, plan, files)]
    trace_ok = refs_listed and not unresolved
    outcome = metrics_doc.get("conclusion")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    criterion_met = outcome.get("criterion_met")
    claimed_success = outcome.get("claimed_success")
    contradiction = claimed_success is True and criterion_met is False
    #: AC-13 同口径（见 ``libero_facts``）：R1 也没有数值 telemetry，可重算的是
    #: 从候选快照里重新抽取的结构事实，一个输入件一行。
    trace = [
        {"artifact": "experiment_plan.yaml",
         "sections_present": len(present_sections),
         "sections_missing": list(missing_sections),
         "ablation_groups": len(ablation) if isinstance(ablation, list) else 0},
        {"artifact": "dataset_splits",
         "split_ids": {name: sorted(split_ids.get(name) or [])
                       for name in split_names},
         "split_duplicates": dict(split_duplicates),
         "overlap_ids": list(overlap), "leak_ok": leak_ok},
        {"artifact": "evaluation_metrics.json",
         "metrics_listed": len(metrics_list) if isinstance(metrics_list, list) else 0,
         "maps_to": list(mapped), "has_stat_test_or_ci": stats_ok,
         "claimed_success": claimed_success, "criterion_met": criterion_met},
        {"artifact": "research_report.md", "chars": len(report),
         "forbidden_claims": list(forbidden)},
    ]

    return {
        "present_sections": present_sections,
        "missing_sections": missing_sections,
        "uncovered_classes": uncovered,
        "units_ok": units_ok,
        "ranges_ok": ranges_ok,
        "distribution_basis_ok": distribution_basis_ok,
        "ablation_min_groups": ablation_min,
        "corr_ok": corr_ok,
        "ablation_ok": ablation_ok,
        "ablation_count": len(ablation) if isinstance(ablation, list) else 0,
        "splits_ok": splits_ok,
        "holdout_ok": holdout_ok,
        "disjoint_declared": disjoint_declared,
        "split_ids_nonempty": ids_nonempty,
        "split_duplicates": split_duplicates,
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
        "evidence_refs": list(refs or []),
        "unresolved_refs": unresolved,
        "conclusion_structured": isinstance(criterion_met, bool)
        and isinstance(claimed_success, bool),
        "contradiction": contradiction,
        "research_problem_ok": problem_structured,
        "rl_constraints_ok": rl_constraints_ok,
        "report_ok": bool(report.strip()),
        "trace": trace,
    }


def score_research(files: Mapping[str, Any], criteria: Mapping) -> "dict[str, Dimension]":
    facts = research_facts(files, criteria)
    weights = criteria["weights"]

    checks = {
        "research_problem_formulation": [
            (facts["research_problem_ok"], "research_problem 含非空问题陈述"),
            (facts["rl_constraints_ok"], "显式承认不改 RL 算法约束"),
            (facts["report_ok"], "research_report.md 非空"),
        ],
        "randomization_design": [
            (not facts["missing_sections"] and bool(facts["present_sections"]),
             f"13 项必填段齐 {len(facts['present_sections'])}/13"),
            (not facts["uncovered_classes"],
             f"三类参数覆盖（缺 {facts['uncovered_classes'] or '无'}）"),
            (facts["units_ok"], "参数全部带物理单位"),
            (facts["ranges_ok"], "参数定义带合法 range 与物理依据 rationale"),
            (facts["distribution_basis_ok"],
             "三类参数分布带上下界与 rationale"),
        ],
        "parameter_relationships": [
            (facts["corr_ok"], "≥1 组相关性且给理由"),
        ],
        "experimental_design": [
            (facts["splits_ok"], "Train/Validation/Holdout 三段齐全"),
            (facts["holdout_ok"], "Holdout 非空（独立留出）"),
            (facts["leak_ok"],
             f"三段 id 非空且两两无交集（重叠 {facts['overlap_ids'] or '无'}）"
             + (f"；段内重复 {facts['split_duplicates']}"
                if facts["split_duplicates"] else "")),
            (facts["ablation_ok"],
             f"Ablation {facts['ablation_count']} 组 ≥ "
             f"{facts['ablation_min_groups']}"),
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
            (facts["trace_ok"],
             f"evidence_refs 全部可解析（未解析 {facts['unresolved_refs'] or '无'}）"),
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

#: 各任务评分器实际产出的维度集（评审 W1-13：``criteria.weights`` 的键集必须
#: 与它逐字相等——多一个键意味着有一段权重被静默忽略，少一个键意味着某个维度
#: 拿不到权重。早先只在 ``task_spec`` 校验 Σw≈1，从不交叉核对键集）。
TASK_DIMENSIONS = {
    "mujoco_contact_push_v1": ("functional_correctness", "physics_validity",
                               "reproducibility", "evidence_completeness"),
    "libero_env_adapter_v1": ("import_and_construction", "reset_and_step",
                              "observation_action_contract", "runtime_identity",
                              "evidence_completeness"),
    "icy_ground_dr_v1": ("research_problem_formulation", "randomization_design",
                         "parameter_relationships", "experimental_design",
                         "metrics_and_statistics", "sim_to_real_validity",
                         "evidence_traceability"),
}


def _audit_weights(task_id: str, criteria: Mapping) -> None:
    """``criteria.weights`` 键集必须与评分维度集逐字相同（评审 W1-13）。"""
    weights = criteria.get("weights")
    if not isinstance(weights, Mapping):
        raise ScoringError(f"{task_id}: criteria 缺 weights 段")
    expected = set(TASK_DIMENSIONS[task_id])
    actual = {str(key) for key in weights}
    if actual != expected:
        raise ScoringError(
            f"{task_id}: weights 键集与评分维度不符 "
            f"（多 {sorted(actual - expected)} / 少 {sorted(expected - actual)}）")


def score_task(task_id: str, files: Mapping[str, Any],
               criteria: Mapping) -> "dict[str, Dimension]":
    try:
        scorer = SCORERS[task_id]
    except KeyError:
        raise ScoringError(f"任务 {task_id!r} 没有评分器") from None
    _audit_weights(task_id, criteria)
    return scorer(files, criteria)


def facts_task(task_id: str, files: Mapping[str, Any],
               criteria: Mapping) -> dict:
    try:
        extractor = FACT_EXTRACTORS[task_id]
    except KeyError:
        raise ScoringError(f"任务 {task_id!r} 没有事实抽取器") from None
    return extractor(files, criteria)


_OPS = {
    ast.Pow: lambda a, b: a ** b,
    ast.Div: lambda a, b: a / b,
    ast.Mult: lambda a, b: a * b,
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
}


class _Unknown:
    """静态求值失败标记（与 ``None`` 可区分）。"""


_UNKNOWN = _Unknown()


def _static_number(node: ast.AST) -> object:
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool) \
            and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _static_number(node.operand)
        if isinstance(inner, _Unknown):
            return _UNKNOWN
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _static_number(node.left), _static_number(node.right)
        if isinstance(left, _Unknown) or isinstance(right, _Unknown):
            return _UNKNOWN
        try:
            return _OPS[type(node.op)](left, right)
        except (ArithmeticError, TypeError):
            return _UNKNOWN
    return _UNKNOWN


def float_literals(source: str) -> "list[float]":
    """AST 收集源码里的浮点常量（AC-T4-7 机判：本模块必须为空）。

    评审 W3-14：早先只收 ``ast.Constant(float)``，``10 ** -6`` 这类**算术表达式**
    （``BinOp``）藏得住一个浮点容差。现在对「操作数全为字面量」的加减乘除/幂
    做静态求值，结果只要落在浮点域就一并收上来——同样是「代码里不许出现浮点
    权/阈值字面量」的机判，只是把盲区补上。
    """
    tree = ast.parse(source)
    found: "list[float]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append(node.value)
        elif isinstance(node, ast.BinOp):
            value = _static_number(node)
            if not isinstance(value, _Unknown) and isinstance(value, float):
                found.append(value)
    return found
