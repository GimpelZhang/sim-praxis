"""RSI-R0 独立评测器：只从**宿主记录的真实事件**复算，不采信候选自报数字。

评测的输入永远是三件：(1) 公开任务书，(2) 宿主 :class:`ExperimentSession` 的
调用记录（每个 run 的 params / scene_sha256 / steps_run / events_sha256），
(3) 一个**重放函数**（同镜像同场景再跑一遍，给出原始序列）。

设计要点（计划 §4.1 / §4.4 / AC19）：

* 候选交卷只能引用 ``run_id``；它自报的 ``distance``/``effect``/``hypothesis``
  一律是**待核对的声明**，真值由维护侧从原始序列复算。
* **只有重放没有原事件必须拒**：``run_id`` 必须真的在调用记录里，且重放的
  ``events_sha256`` 必须与记录的逐位相同。否则 L1 判 FAIL（伪造/篡改）。
* 层与层之间不许"跳级"：上层 PASS 不掩盖下层 FAIL，:func:`first_failure`
  给出的首个失败层就是诊断对象。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.rsi_r0.contracts import (
    Family,
    Layer,
    LayerResult,
    LayerStatus,
    RESEARCH_CRITERIA,
    ScientificVerdict,
    TrialResult,
    SlotState,
    ExecutionVerdict,
    answer_schema,
    instance_family,
)
from orchestrator.rsi_r0.reference_analysis import (
    classify_fault,
    fit_slope,
    research_verdict,
    settling_time,
)

#: 评测器版本。改判据语义必须同时改这里，并重算 criteria_hash（AC20）。
EVALUATOR_VERSION = "rsi_r0.evaluator.v1"

#: 声明值与复算值的允许偏差（浮点同一实现本应逐位相同，留一点组合顺序余量）。
CLAIM_TOL = 1e-6

#: 研究族一次 run 的合法步数区间（C2 与 ``steps_match`` 共用这一份）。
RESEARCH_STEPS_RANGE = (50, 2000)

#: 研究族可以宣称的结论类型（与 :func:`reference_analysis.research_verdict` 同集合）。
#: 交卷写了别的字符串 = 协议违规（L0），不是"罕见结论"。
RESEARCH_VERDICTS = ("SUPPORTED", "REFUTED", "INCONCLUSIVE",
                     "INCONCLUSIVE_EQUIVALENT", "CENSORED")


class AnswerError(ValueError):
    """交卷格式不合法（这一层就判 L0 FAIL，不进后面各层）。"""


# ---------------------------------------------------------------------------
# 复算（纯函数，单测直接命中）
# ---------------------------------------------------------------------------
def series_metrics(series: Sequence[Sequence[float]],
                   target: Sequence[float]) -> dict[str, float]:
    """从 ``[step, x, y, vx, vy]`` 序列取末点距离与速度。"""
    if not series:
        raise AnswerError("序列为空，无法复算")
    _step, x, y, vx, vy = series[-1]
    return {"distance": math.hypot(x - float(target[0]), y - float(target[1])),
            "final_speed": math.hypot(vx, vy)}


def diagnose_from_runs(runs: Sequence[Mapping[str, Any]], *,
                       nominal: Mapping[str, float],
                       rules: Mapping[str, float],
                       dt: float) -> dict[str, Any]:
    """用候选自己的探测 run 复算 ``a(F)=A·F−B`` 并分类。

    每个 run 必须自带 ``params``（含 ``max_force`` 与 ``target_x`` 方向）与
    ``series``。两档力 × 两个方向是**任务要求**，缺了就抛 :class:`AnswerError`
    （不是评测器猜一个）。
    """
    fits: dict[int, dict[float, float]] = {+1: {}, -1: {}}
    for run in runs:
        p = run["params"]
        direction = 1 if float(p["target_x"]) > 0 else -1
        force = abs(float(p["max_force"]))
        fits[direction][force] = fit_slope(run["series"], dt)[0]
    for direction, got in fits.items():
        if len(got) < 2:
            raise AnswerError(
                f"方向 {direction:+d} 只给了 {len(got)} 档力，诊断需要 ≥2 档")
        (f1, a1), (f2, a2) = sorted(got.items())
        slope = (a2 - a1) / (f2 - f1)
        fits[direction]["A"] = slope            # type: ignore[assignment]
        fits[direction]["B"] = slope * f1 - a1  # type: ignore[assignment]
    out = classify_fault(fits[+1]["A"], fits[+1]["B"],   # type: ignore[index]
                         fits[-1]["A"], fits[-1]["B"],   # type: ignore[index]
                         nominal, rules)
    out["probe_count"] = len(runs)
    return out


def research_from_runs(runs: Sequence[Mapping[str, Any]], *,
                       dt: float, priv: Mapping[str, Any]) -> dict[str, Any]:
    """用候选自己的 run 复算两水平两初态的沉降效应与判决。"""
    cells: dict[tuple[float, float], dict[str, Any]] = {}
    for run in runs:
        p = run["params"]
        level = float(p["damping"])
        angle = float(p["initial_angle"])
        cells[(level, angle)] = settling_time(run, dt)
    levels = sorted({lv for (lv, _a) in cells})
    angles = sorted({a for (_lv, a) in cells})
    if len(levels) < 2 or len(angles) < 2:
        raise AnswerError(
            f"需要 ≥2 阻尼水平 × ≥2 初态，实际 {len(levels)}×{len(angles)}")
    effects: dict[str, float | None] = {}
    for angle in angles:
        lo, hi = cells[(levels[0], angle)], cells[(levels[1], angle)]
        effects[str(angle)] = (
            None if (lo["censored"] or hi["censored"])
            else (lo["settle_s"] - hi["settle_s"]) / lo["settle_s"])
    verdict = research_verdict(effects, priv)
    valid = [v for v in effects.values() if v is not None]
    return {"levels": levels, "angles": angles, "effects": effects,
            "mean_effect": sum(valid) / len(valid) if valid else None,
            "verdict": verdict, "cells": {f"{lv}@{a}": v for (lv, a), v in cells.items()}}


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvaluationInput:
    """一次 slot 的评测输入（全部由维护侧持有，候选看不到）。"""

    task: Mapping[str, Any]
    answer: Mapping[str, Any]
    calls: Sequence[Mapping[str, Any]]
    replay: Callable[[Mapping[str, Any]], Mapping[str, Any]]  # 入参是整个调用记录
    truth: Mapping[str, Any]          # 私有真值（fault/nominal/classify/expected/…）
    priv_rules: Mapping[str, Any]     # equivalence_bound / support_threshold / tolerance
    prereg_sha256: str | None = None  # 首跑前登记的计划哈希（C1 用）
    #: 本 slot 的宿主场景哈希（D 族是**注入故障后**的场景；缺省用任务书的公开哈希）
    scene_sha256: str | None = None
    #: 逐调用场景哈希（R 族每个阻尼一个场景，D 族故障场景与公开面不同）——
    #: 给了就以它为准，否则退化为上面的常量比较
    scene_for: Callable[[Mapping[str, Any]], str] | None = None
    slot_id: str = "slot"
    requests_used: int = 0


def replay_matches(call: Mapping[str, Any], replayed: Mapping[str, Any]) -> bool:
    """重放事件必须与记录的原始事件逐位相同（AC19：只有重放没有原事件即拒）。

    **两侧都必须真有哈希**：``None == None`` 不算"相同"——服务端漏回
    ``events_sha256`` 时，这一层会静默变成空转仍报 PASS，等于把"真实产物"
    判成通过（fail-open）。
    """
    recorded, again = call.get("events_sha256"), replayed.get("events_sha256")
    if not recorded or not again:
        return False
    return str(again) == str(recorded)


def scene_matches(call: Mapping[str, Any], want_scene: str) -> bool:
    """每次调用用的场景必须是本题场景（宿主构造，候选无法替换）。"""
    return str(call.get("scene_sha256")) == str(want_scene)


def steps_match(call: Mapping[str, Any], cap: int,
                limits: Sequence[float] | None = None) -> bool:
    """真步进：服务回报的步数 == 请求步数，且请求步数落在任务声明区间内。

    任务书在 ``interface.limits.steps`` 里声明了区间（研究族 50–2000）时以它为准，
    否则用 ``[1, steps_max]``——**不许**因为没写就放宽到 1 步。
    """
    steps = int(call["params"]["steps"])
    if int(call.get("steps_run", -1)) != steps:
        return False
    lo, hi = (int(limits[0]), int(limits[1])) if limits else (1, cap)
    return lo <= steps <= hi


def matches_expected(verdict: str, expected: str) -> bool:
    """预注册真值的接受规则：严格相等，或"上位结论"接受其诚实子类。

    R-v2 预注册的是上位结论 ``INCONCLUSIVE``，复算给出子类
    ``INCONCLUSIVE_EQUIVALENT``——子类成立即为预注册所指的阴结果。
    但 ``CENSORED`` 不是 INCONCLUSIVE 的子类（它是没测出来），仍判不一致。
    """
    if verdict == expected:
        return True
    return expected == "INCONCLUSIVE" and verdict.startswith("INCONCLUSIVE_")


def missing_criteria(answer: Mapping[str, Any]) -> list[str]:
    """研究族交卷必须逐项给出 C1–C8 的证据。"""
    crit = answer.get("criteria")
    if not isinstance(crit, Mapping):
        return list(RESEARCH_CRITERIA)
    return [c for c in RESEARCH_CRITERIA if c not in crit]


#: 候选可以主动提交的参数键（接口键 + 控制键）。宿主字段不在其中。
PARAM_KEY_ALLOWANCE: tuple[str, ...] = ("steps", "target_x", "target_y", "damping")
#: 交卷允许的额外键（研究族的事前诊断覆盖；其余族无）。
ANSWER_EXTRA_ALLOWANCE: tuple[str, ...] = ("diagnostics",)


def extra_answer_keys(task: Mapping[str, Any],
                      answer: Mapping[str, Any]) -> list[str]:
    """交卷夹带未声明字段（例如 scene_xml / force_scale）即越权。"""
    allowed = set(answer_schema(Family(task["family"]))["required"]) | set(
        ANSWER_EXTRA_ALLOWANCE)
    return sorted(set(answer) - allowed)


def undeclared_param_keys(task: Mapping[str, Any],
                          params: Mapping[str, Any]) -> list[str]:
    allowed = set(task["interface"]["keys"]) | set(PARAM_KEY_ALLOWANCE)
    return sorted(set(params) - allowed)


def _by_run_id(calls: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {c["run_id"]: c for c in calls if c.get("run_id")}


def _result(layer: Layer, status: LayerStatus, code: str,
            evidence: Iterable[str] = ()) -> LayerResult:
    return LayerResult(layer=layer, status=status, reason_code=code,
                       evidence=tuple(evidence))


def _check_shape(task: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
    family = Family(task["family"])
    required = answer_schema(family)["required"]
    missing = [k for k in required if k not in answer]
    if missing:
        raise AnswerError(f"交卷缺字段：{missing}")
    extra = extra_answer_keys(task, answer)
    if extra:
        raise AnswerError(f"额外字段：{extra}")
    if str(answer.get("family")) != family.value:
        raise AnswerError(f"family 不符：交卷 {answer.get('family')} vs 任务 {family.value}")
    if family is Family.R:
        if str(answer.get("verdict")) not in RESEARCH_VERDICTS:
            raise AnswerError(f"未声明的结论类型：{answer.get('verdict')!r}"
                              f"（可选 {list(RESEARCH_VERDICTS)}）")
        absent = missing_criteria(answer)
        if absent:
            raise AnswerError(f"研究判据缺项：{absent}")
    if not isinstance(answer.get("scope", ""), str):
        raise AnswerError("scope 必须是字符串")


def _referenced(answer: Mapping[str, Any]) -> list[str]:
    ids = answer.get("run_ids")
    if ids is None:
        ids = [answer.get("run_id")]
    if not isinstance(ids, (list, tuple)) or not ids:
        raise AnswerError("交卷未给出任何 run_id")
    return [str(i) for i in ids]


def evaluate(inp: EvaluationInput) -> TrialResult:
    """跑满 L0–L5（不适用层显式 NOT_APPLICABLE），返回一条 :class:`TrialResult`。"""
    task = inp.task
    family = Family(task["family"])
    dt = float(task["scene_params"]["timestep"])
    layers: dict[Layer, LayerResult] = {}

    # ---- L0 有效响应/协议 ----
    try:
        _check_shape(task, inp.answer)
        refs = _referenced(inp.answer)
        known = _by_run_id(inp.calls)
        unknown = [r for r in refs if r not in known]
        if unknown:
            raise AnswerError(f"引用了宿主不存在的 run_id：{unknown}")
        undeclared = sorted({k for c in inp.calls
                             for k in undeclared_param_keys(task, c["params"])})
        if undeclared:
            raise AnswerError(f"额外字段：请求参数夹带未声明键 {undeclared}")
        layers[Layer.L0] = _result(Layer.L0, LayerStatus.PASS, "OK",
                                   evidence=tuple(refs))
    except AnswerError as exc:
        layers[Layer.L0] = _result(Layer.L0, LayerStatus.FAIL, str(exc))
        for layer in (Layer.L1, Layer.L2, Layer.L3, Layer.L4):
            layers[layer] = _result(layer, LayerStatus.NOT_REACHED, "L0_FAILED")
        layers[Layer.L5] = _result(
            Layer.L5, LayerStatus.NOT_APPLICABLE if family is not Family.R
            else LayerStatus.NOT_REACHED,
            "NOT_APPLICABLE" if family is not Family.R else "L0_FAILED")
        return _assemble(inp, family, layers)

    known = _by_run_id(inp.calls)
    referenced = [known[r] for r in refs]

    # ---- L1 真实产物（重放必须与记录的原始事件逐位一致）----
    replay_runs: list[dict[str, Any]] = []
    mismatched: list[str] = []
    for call in referenced:
        got = inp.replay(call)
        if not replay_matches(call, got):
            mismatched.append(str(call["run_id"]))
            continue
        replay_runs.append({**dict(call), "series": got.get("series"),
                            "angle_series": got.get("angle_series"),
                            "energy_initial": got.get("energy_initial"),
                            "energy_final": got.get("energy_final")})
    if mismatched:
        layers[Layer.L1] = _result(Layer.L1, LayerStatus.FAIL, "REPLAY_MISMATCH",
                                   evidence=tuple(mismatched))
    else:
        layers[Layer.L1] = _result(Layer.L1, LayerStatus.PASS, "OK",
                                   evidence=tuple(refs))

    # ---- L2 真实加载（场景身份）----
    # 场景由宿主构造：D 族是注入故障后的场景、R 族每个阻尼一个场景，所以期望值
    # 既可以是本 slot 的常量，也可以是逐调用的宿主重建哈希。候选无论走哪条路都
    # 交不出场景，篡改只可能发生在记录侧，比对的就是记录侧。
    want_const = str(inp.scene_sha256 or task["scene_xml_sha256"])
    bad_scene, seen_scenes = [], set()
    for call in inp.calls:
        want = str(inp.scene_for(call)) if inp.scene_for else want_const
        seen_scenes.add(want)
        if not scene_matches(call, want):
            bad_scene.append(str(call["run_id"]))
    layers[Layer.L2] = (
        _result(Layer.L2, LayerStatus.PASS, "OK", evidence=tuple(sorted(seen_scenes)))
        if not bad_scene else
        _result(Layer.L2, LayerStatus.FAIL, "SCENE_MISMATCH", evidence=tuple(bad_scene)))

    # ---- L3 真实仿真步进 ----
    cap = int(task["steps_max"])
    step_limits = task.get("interface", {}).get("limits", {}).get("steps")
    bad_steps = [str(c["run_id"]) for c in inp.calls
                 if not steps_match(c, cap, step_limits)]
    layers[Layer.L3] = (
        _result(Layer.L3, LayerStatus.PASS, "OK", evidence=(str(cap),))
        if not bad_steps else
        _result(Layer.L3, LayerStatus.FAIL, "STEPS_MISMATCH", evidence=tuple(bad_steps)))

    # ---- L4 功能目标 ----
    layers[Layer.L4] = _functional(
        family, task, inp, replay_runs, dt,
        {L: layers[L].status for L in (Layer.L1, Layer.L2, Layer.L3)})

    # ---- L5 研究结论有效（只有 R 适用）----
    if family is Family.R:
        layers[Layer.L5] = _research_layer(inp, replay_runs, dt)
    else:
        layers[Layer.L5] = _result(Layer.L5, LayerStatus.NOT_APPLICABLE,
                                   "NOT_APPLICABLE")
    return _assemble(inp, family, layers)


def _functional(family: Family, task: Mapping[str, Any], inp: EvaluationInput,
                runs: Sequence[Mapping[str, Any]], dt: float,
                lower: Mapping[Layer, LayerStatus]) -> LayerResult:
    if any(lower.get(L) is not LayerStatus.PASS for L in (Layer.L1, Layer.L2, Layer.L3)):
        return _result(Layer.L4, LayerStatus.NOT_REACHED, "LOWER_LAYER_FAILED")
    try:
        if family is Family.E:
            tol = inp.priv_rules["tolerance"]
            worst_dist, worst_speed = 0.0, 0.0
            for run in runs:
                m = series_metrics(run["series"], task["goal"]["target"])
                worst_dist = max(worst_dist, m["distance"])
                worst_speed = max(worst_speed, m["final_speed"])
            claim = inp.answer["claim"]
            claimed = (float(claim["distance"]), float(claim["final_speed"]))
            if (abs(claimed[0] - worst_dist) > CLAIM_TOL
                    or abs(claimed[1] - worst_speed) > CLAIM_TOL):
                return _result(Layer.L4, LayerStatus.FAIL, "CLAIM_MISMATCH",
                               evidence=(f"recomputed={worst_dist},{worst_speed}",
                                         f"claimed={claimed[0]},{claimed[1]}"))
            ok = worst_dist <= tol["distance"] and worst_speed <= tol["final_speed"]
            return _result(Layer.L4, LayerStatus.PASS if ok else LayerStatus.FAIL,
                           "TARGET_MET" if ok else "TARGET_NOT_MET",
                           evidence=(f"distance={worst_dist:.6f}",
                                     f"final_speed={worst_speed:.6f}"))
        if family is Family.D:
            got = diagnose_from_runs(runs, nominal=inp.truth["nominal"],
                                     rules=inp.truth["classify"], dt=dt)
            truth = inp.truth["fault"]
            hyp = inp.answer["hypothesis"]
            rel = abs(got["scale"] - truth["scale"]) / abs(truth["scale"])
            kind_ok = got["kind"] == truth["kind"]
            claim_ok = str(hyp["kind"]) == got["kind"]
            ok = kind_ok and claim_ok and rel <= inp.priv_rules["tolerance"]["scale_rel"]
            code = ("DIAGNOSIS_CORRECT" if ok else
                    "WRONG_KIND" if not (kind_ok and claim_ok) else
                    "SCALE_OUT_OF_TOLERANCE")
            return _result(Layer.L4, LayerStatus.PASS if ok else LayerStatus.FAIL,
                           code, evidence=(f"recomputed={got['kind']}:{got['scale']:.4f}",
                                           f"truth={truth['kind']}:{truth['scale']}",
                                           f"claimed={hyp['kind']}:{hyp['scale']}"))
        # R：L4 判"实验真的做出了该做的对照"，L5 判研究结论有效性
        got = research_from_runs(runs, dt=dt, priv=inp.priv_rules)
        return _result(Layer.L4, LayerStatus.PASS, "CONTRAST_COMPLETE",
                       evidence=(f"levels={got['levels']}", f"angles={got['angles']}"))
    except (AnswerError, KeyError, ZeroDivisionError) as exc:
        return _result(Layer.L4, LayerStatus.FAIL, f"UNEVALUABLE:{exc.__class__.__name__}",
                       evidence=(str(exc),))


def _research_layer(inp: EvaluationInput, runs: Sequence[Mapping[str, Any]],
                    dt: float) -> LayerResult:
    problems: list[str] = []
    # C1 事前登记
    if not inp.prereg_sha256:
        problems.append("C1_preregistered_plan:missing_prereg")
    # C2 两水平两初态 + 步数区间 50–2000
    levels = {float(r["params"]["damping"]) for r in runs}
    angles = {float(r["params"]["initial_angle"]) for r in runs}
    if len(levels) < 2 or len(angles) < 2:
        problems.append(f"C2_two_levels_two_configs:{len(levels)}x{len(angles)}")
    if any(not RESEARCH_STEPS_RANGE[0] <= int(r["params"]["steps"])
           <= RESEARCH_STEPS_RANGE[1] for r in runs):
        problems.append("C2_two_levels_two_configs:steps_out_of_range")
    # C4 只动允许的键
    allowed = set(inp.task["interface"]["keys"]) | {"steps", "damping"}
    for run in runs:
        extra = set(run["params"]) - (allowed | {"target_x", "target_y"})
        if extra:
            problems.append(f"C4_units_and_controls:illegal_keys={sorted(extra)}")
            break
    try:
        got = research_from_runs(runs, dt=dt, priv=inp.priv_rules)
    except (AnswerError, KeyError) as exc:
        return _result(Layer.L5, LayerStatus.FAIL, f"UNEVALUABLE:{exc.__class__.__name__}",
                       evidence=(str(exc),))
    # C5 独立复算一致
    if got["verdict"] != str(inp.answer["verdict"]):
        problems.append(f"C5_independent_analysis:verdict {got['verdict']} != "
                        f"claimed {inp.answer['verdict']}")
    claimed_effect = inp.answer["effect"]
    if got["mean_effect"] is None:
        problems.append("C3_trusted_series:censored")
    elif abs(float(claimed_effect) - got["mean_effect"]) > 1e-3:
        problems.append(f"C5_independent_analysis:effect {got['mean_effect']:.6f} != "
                        f"claimed {claimed_effect}")
    # C6 结论与预注册边界一致
    expected = str(inp.truth["expected"])
    if not matches_expected(str(got["verdict"]), expected):
        problems.append(f"C6_conclusion_boundary:expected {expected}")
    # C7 适用范围声明
    if len(str(inp.answer.get("scope", "")).strip()) < 8:
        problems.append("C7_scope_statement:too_short")
    # C8 INCONCLUSIVE 需要事前诊断覆盖
    if got["verdict"].startswith("INCONCLUSIVE") and not inp.answer.get("diagnostics"):
        problems.append("C8_inconclusive_discipline:no_diagnostics")
    if problems:
        return _result(Layer.L5, LayerStatus.FAIL, problems[0], evidence=tuple(problems))
    return _result(Layer.L5, LayerStatus.PASS, "OK",
                   evidence=tuple(f"{c}:declared" for c in RESEARCH_CRITERIA))


def scientific_axis(verdict: str) -> ScientificVerdict:
    """交卷判决 → 科学轴四值：等效/删失这类**子类**如实归入 INCONCLUSIVE。

    科学轴只有"支持/反驳/不确定/不适用"四个值（计划 §4.2 两轴正交），
    "有效但等效"与"删失"都是不确定的诚实子类，不能抬成 SUPPORTED，也不能
    被抹成 FAIL——它们的执行轴是 COMPLETE。
    """
    if verdict in (ScientificVerdict.SUPPORTED.value, ScientificVerdict.REFUTED.value):
        return ScientificVerdict(verdict)
    return ScientificVerdict.INCONCLUSIVE


def _assemble(inp: EvaluationInput, family: Family,
              layers: dict[Layer, LayerResult]) -> TrialResult:
    blocked = any(r.status is LayerStatus.BLOCKED_INFRA for r in layers.values())
    complete = all(r.status in (LayerStatus.PASS, LayerStatus.NOT_APPLICABLE)
                   for r in layers.values())
    if blocked:
        execution, state = ExecutionVerdict.BLOCKED, SlotState.BLOCKED_INFRA
    elif complete:
        execution, state = ExecutionVerdict.COMPLETE, SlotState.COMPLETED
    else:
        execution, state = ExecutionVerdict.FAILED, SlotState.COMPLETED
    if family is Family.R:
        # 研究族没有"不适用"：结论层 PASS 才敢报结论，其余（FAIL / NOT_REACHED /
        # BLOCKED）一律进不确定——协议失败的 R 试次仍要占科学分母，不能算"不适用"。
        scientific = (scientific_axis(str(inp.answer["verdict"]))
                      if layers[Layer.L5].status is LayerStatus.PASS
                      else ScientificVerdict.INCONCLUSIVE)
    else:
        scientific = ScientificVerdict.NOT_APPLICABLE
    return TrialResult(
        slot_id=inp.slot_id, instance_id=str(inp.task["instance_id"]),
        family=instance_family(str(inp.task["instance_id"])), role=str(inp.task["role"]),
        state=state, layers=dict(layers), execution=execution, scientific=scientific,
        requests_used=inp.requests_used)
