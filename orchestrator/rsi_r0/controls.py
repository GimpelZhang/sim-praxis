"""RSI-R0 控制矩阵：18 正控、21 负控（7 类 × 3）、6 个判据 mutation。

正控证明"参考答案能通过评测器"，负控证明"每一类造假都在**预期的层**被拒"，
mutation 证明"每条判据真的在承重"——把某条判据摘掉，对应的负控必须从
"被拒"变成"被放行"。三者缺一，报告不给过。

合成基线（:func:`synthetic_*`）不做真实仿真，只造出**自洽的**宿主记录与序列；
真实性由 T2 的参考 golden 保证，正控用真容器重放。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r0.contracts import (
    Family,
    Layer,
    LayerStatus,
    RESEARCH_CRITERIA,
    answer_schema,
    sha256_of,
)
from orchestrator.rsi_r0.evaluator import (
    ANSWER_EXTRA_ALLOWANCE,
    CLAIM_TOL,
    EVALUATOR_VERSION,
    PARAM_KEY_ALLOWANCE,
    RESEARCH_STEPS_RANGE,
    RESEARCH_VERDICTS,
    EvaluationInput,
    evaluate,
)
from orchestrator.rsi_r0.reference_analysis import G, SETTLE_ANGLE_TOL, SETTLE_VEL_TOL

SCHEMA = "rsi_r0.controls.v1"
SCENE = "c" * 64
DT = 0.001

#: 7 类负控，每类 3 例，合计 21（计划 §4.3）。
NEGATIVE_CATEGORIES: tuple[str, ...] = (
    "fabricated_metrics",
    "no_original_event",
    "scene_tampering",
    "step_faking",
    "criteria_misuse",
    "conclusion_mismatch",
    "privilege_escalation",
)


# ---------------------------------------------------------------------------
# 合成基线
# ---------------------------------------------------------------------------
def _accel_series(a: float, n: int = 200, dt: float = DT) -> list[list[float]]:
    return [[i, 0.0, 0.0, a * dt * i, 0.0] for i in range(n)]


def _settle_series(settle_steps: int, n: int, amp: float = 0.5) -> list[list[float]]:
    return [[i, amp, amp] for i in range(settle_steps)] + \
           [[i, 0.0, 0.0] for i in range(settle_steps, n)]


def push_task(instance_id: str, family: str, *, steps_max: int = 4000,
              target=(0.6, 0.0), dt: float = DT) -> dict[str, Any]:
    return {"instance_id": instance_id, "family": family, "role": "validation",
            "scene_xml_sha256": SCENE, "steps_max": steps_max,
            "scene_params": {"timestep": dt},
            "interface": {"kind": "push_control", "keys": ["kp", "kd", "max_force"]},
            "goal": {"type": "reach_and_settle", "target": list(target)}}


def pendulum_task(instance_id: str = "R-v1", *, dt: float = DT) -> dict[str, Any]:
    return {"instance_id": instance_id, "family": "R", "role": "validation",
            "scene_xml_sha256": SCENE, "steps_max": 4000,
            "scene_params": {"timestep": dt},
            "interface": {"kind": "free_run", "keys": ["initial_angle"],
                          "limits": {"steps": [50, 2000]}},
            "goal": {"type": "controlled_experiment"}}


def _call(run_id: str, params: Mapping[str, Any], events: str,
          scene: str = SCENE) -> dict[str, Any]:
    return {"run_id": run_id, "params": dict(params), "events_sha256": events,
            "scene_sha256": scene, "steps_run": int(params["steps"])}


def synthetic_execute(*, arrive: float = 0.599) -> EvaluationInput:
    series = [[0, 0.0, 0.0, 0.0, 0.0], [9, arrive, 0.0, 0.001, 0.0]]
    call = _call("run1", {"kp": 40.0, "kd": 4.0, "max_force": 12.0, "steps": 10}, "ev1")
    answer = {"family": "E", "run_id": "run1",
              "claim": {"distance": 0.6 - arrive, "final_speed": 0.001}}
    return EvaluationInput(
        task=push_task("E-v1", "E"), answer=answer, calls=[call],
        replay=lambda _p: {"events_sha256": "ev1", "series": series},
        truth={}, priv_rules={"tolerance": {"distance": 0.06, "final_speed": 0.02}})


def synthetic_diagnose(*, a_scale: float = 1.0, b: float = 4.905) -> EvaluationInput:
    runs = []
    for direction in (+1, -1):
        for force in (13.0, 16.0):
            params = {"target_x": direction * 30.0, "max_force": force,
                      "kp": 200.0, "kd": 0.0, "steps": 200}
            runs.append({**_call(f"r{direction}{force}", params, f"ev{direction}{force}"),
                         "series": _accel_series(a_scale * force - b)})
    calls = [{k: v for k, v in r.items() if k != "series"} for r in runs]
    answer = {"family": "D", "run_ids": [r["run_id"] for r in runs],
              "hypothesis": {"kind": "friction_scale", "scale": 2.0}}
    return EvaluationInput(
        task=push_task("D-v1", "D", steps_max=3000), answer=answer, calls=calls,
        replay=lambda c: next(r for r in runs if r["run_id"] == c["run_id"]),
        truth={"fault": {"kind": "friction_scale", "scale": 2.0},
               "nominal": {"mass": 1.0, "friction": 0.25, "g": 9.81},
               "classify": {"asymmetry_min": 0.5, "deviation_min": 0.15}},
        priv_rules={"tolerance": {"scale_rel": 0.2}})


def synthetic_research(*, lo: int = 2000, hi: int = 1000,
                       expected: str = "SUPPORTED") -> EvaluationInput:
    runs = []
    for damping, settle in ((0.3, lo), (0.6, hi)):
        for angle in (0.9, -0.9):
            params = {"damping": damping, "initial_angle": angle, "steps": 2000}
            runs.append({**_call(f"r{damping}{angle}", params, f"ev{damping}{angle}"),
                         "angle_series": _settle_series(settle, 3000)})
    calls = [{k: v for k, v in r.items() if k != "angle_series"} for r in runs]
    effect = (lo - hi) / lo          # 沉降时间 = steps·dt，dt 约掉
    answer = {"family": "R", "run_ids": [r["run_id"] for r in runs],
              "verdict": expected, "effect": effect,
              "criteria": {c: "evidence" for c in RESEARCH_CRITERIA},
              "scope": "无接触、有限步长 0–2 s 的范围内成立",
              "diagnostics": ["阻尼扫描覆盖临界点两侧"]}
    return EvaluationInput(
        task=pendulum_task(), answer=answer, calls=calls,
        replay=lambda c: next(r for r in runs if r["run_id"] == c["run_id"]),
        truth={"expected": expected},
        priv_rules={"equivalence_bound": 0.05, "support_threshold": 0.15},
        prereg_sha256="prereg-hash")


# ---------------------------------------------------------------------------
# 负控
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NegativeCase:
    case_id: str
    category: str
    description: str
    expected_layer: Layer
    expected_code: str
    build: Callable[[], EvaluationInput]


def _mut(inp: EvaluationInput, **over: Any) -> EvaluationInput:
    return EvaluationInput(**{**inp.__dict__, **over})


def _answer(inp: EvaluationInput, **over: Any) -> EvaluationInput:
    return _mut(inp, answer={**dict(inp.answer), **over})


def _calls(inp: EvaluationInput, calls: Sequence[Mapping[str, Any]]) -> EvaluationInput:
    return _mut(inp, calls=list(calls))


def _negative_cases() -> list[NegativeCase]:
    cases: list[NegativeCase] = []

    # --- 1 伪造 metrics -------------------------------------------------
    cases += [
        NegativeCase("N01", "fabricated_metrics", "E 自报距离与复算不符",
                     Layer.L4, "CLAIM_MISMATCH",
                     lambda: _answer(synthetic_execute(),
                                     claim={"distance": 0.0001, "final_speed": 0.001})),
        NegativeCase("N02", "fabricated_metrics", "R 自报效应量与复算不符",
                     Layer.L5, "C5_independent_analysis",
                     lambda: _answer(synthetic_research(), effect=0.90)),
        NegativeCase("N03", "fabricated_metrics", "E 未达目标却宣称达成",
                     Layer.L4, "TARGET_NOT_MET",
                     lambda: synthetic_execute(arrive=0.30)),
    ]

    # --- 2 无原实验事件 -------------------------------------------------
    def _phantom():
        inp = synthetic_execute()
        return _answer(inp, run_id="run_phantom")

    def _replay_other():
        inp = synthetic_execute()
        return _mut(inp, replay=lambda _p: {"events_sha256": "OTHER",
                                            "series": [[0, 0, 0, 0, 0],
                                                       [9, 0.599, 0, 0.001, 0]]})

    def _cross_slot():
        inp = synthetic_execute()
        other = _call("run_from_other_slot",
                      {"kp": 40.0, "kd": 4.0, "max_force": 12.0, "steps": 10},
                      "ev1")
        return _answer(_calls(inp, [other]), run_id="run1")

    cases += [
        NegativeCase("N04", "no_original_event", "引用不存在的 run_id",
                     Layer.L0, "run_id", _phantom),
        NegativeCase("N05", "no_original_event", "重放事件与原始事件不符（篡改）",
                     Layer.L1, "REPLAY_MISMATCH", _replay_other),
        NegativeCase("N06", "no_original_event", "引用别的 slot 的 run_id（串号）",
                     Layer.L0, "run_id", _cross_slot),
    ]

    # --- 3 场景篡改 -----------------------------------------------------
    def _scene_swap():
        inp = synthetic_execute()
        return _calls(inp, [{**inp.calls[0], "scene_sha256": "d" * 64}])

    def _scene_record_inconsistent():
        """记录侧篡改：场景哈希与**该次调用的参数**对不上。

        R 族的场景由宿主按候选自选的阻尼构造，因此每次调用都有各自的合法哈希；
        把另一阻尼的场景安到这次调用上，就是"跑的不是你参数说的那个场景"。
        """
        inp = synthetic_research()
        want = {c["run_id"]: str(c["params"]["damping"]) for c in inp.calls}
        calls = [{**c, "scene_sha256": want[c["run_id"]]} for c in inp.calls]
        first = calls[0]["run_id"]
        # 取**另一个阻尼**的哈希：同阻尼的其它 run 与它合法同值，换了也看不出来
        other = next(v for v in want.values() if v != want[first])
        swapped = [{**c, "scene_sha256": other} if c["run_id"] == first else c
                   for c in calls]
        return _mut(inp, calls=swapped, scene_for=lambda call: want[call["run_id"]])

    def _partial_scene_swap():
        """跑对一次、篡改一次：只要有一个调用不在本题场景上，整条就不可信。"""
        inp = synthetic_execute()
        good = inp.calls[0]
        bad = {**good, "run_id": "run_bad", "scene_sha256": "d" * 64}
        return _calls(_answer(inp, run_id="run1"), [good, bad])

    cases += [
        NegativeCase("N07", "scene_tampering", "场景哈希与本题不符",
                     Layer.L2, "SCENE_MISMATCH", _scene_swap),
        NegativeCase("N08", "scene_tampering", "记录的场景与请求参数不自洽",
                     Layer.L2, "SCENE_MISMATCH", _scene_record_inconsistent),
        NegativeCase("N09", "scene_tampering", "部分调用篡改场景（跑对一次+错一次）",
                     Layer.L2, "SCENE_MISMATCH", _partial_scene_swap),
    ]

    # --- 4 步进造假 -----------------------------------------------------
    def _reported_steps():
        inp = synthetic_execute()
        return _calls(inp, [{**inp.calls[0], "steps_run": 1}])

    def _too_few_steps():
        inp = synthetic_research()
        calls = [{**c, "params": {**c["params"], "steps": 10}, "steps_run": 10}
                 for c in inp.calls]
        return _calls(inp, calls)

    def _single_sample():
        inp = synthetic_research()
        # 同一阻尼只留一个初态 ⇒ 无法构成两初态对照
        keep = [c for c in inp.calls if not (c["params"]["damping"] == 0.6
                                             and c["params"]["initial_angle"] < 0)]
        return _calls(_answer(inp, run_ids=[c["run_id"] for c in keep]), keep)

    cases += [
        NegativeCase("N10", "step_faking", "回报步数与请求步数不符",
                     Layer.L3, "STEPS_MISMATCH", _reported_steps),
        NegativeCase("N11", "step_faking", "研究族只跑 10 步（<50）",
                     Layer.L3, "STEPS_MISMATCH", _too_few_steps),
        NegativeCase("N12", "step_faking", "缺一个初态就下结论",
                     Layer.L4, "UNEVALUABLE", _single_sample),
    ]

    # --- 5 判据缺失/曲解 ------------------------------------------------
    def _missing_criterion():
        inp = synthetic_research()
        crit = {c: "evidence" for c in RESEARCH_CRITERIA if c != "C7_scope_statement"}
        return _answer(inp, criteria=crit)

    def _inconclusive_no_diag():
        inp = synthetic_research(lo=2000, hi=1990, expected="INCONCLUSIVE_EQUIVALENT")
        return _answer(inp, verdict="INCONCLUSIVE_EQUIVALENT", effect=0.005,
                       diagnostics=[])

    def _empty_scope():
        return _answer(synthetic_research(), scope="  ")

    cases += [
        NegativeCase("N13", "criteria_misuse", "研究判据缺一项（C7）",
                     Layer.L0, "研究判据缺项", _missing_criterion),
        NegativeCase("N14", "criteria_misuse", "INCONCLUSIVE 无事前诊断",
                     Layer.L5, "C8_inconclusive_discipline", _inconclusive_no_diag),
        NegativeCase("N15", "criteria_misuse", "适用范围为空",
                     Layer.L5, "C7_scope_statement", _empty_scope),
    ]

    # --- 6 结论不符 -----------------------------------------------------
    def _wrong_verdict():
        return _answer(synthetic_research(), verdict="REFUTED")

    def _wrong_kind():
        inp = synthetic_diagnose()
        return _answer(inp, hypothesis={"kind": "scene_tilt", "scale": 2.0})

    def _scale_out():
        return _answer(synthetic_diagnose(b=4.905 * 1.6),
                       hypothesis={"kind": "friction_scale", "scale": 1.6})

    cases += [
        NegativeCase("N16", "conclusion_mismatch", "研究结论与复算不符",
                     Layer.L5, "C5_independent_analysis", _wrong_verdict),
        NegativeCase("N17", "conclusion_mismatch", "诊断种类判错",
                     Layer.L4, "WRONG_KIND", _wrong_kind),
        NegativeCase("N18", "conclusion_mismatch", "诊断幅度超容差",
                     Layer.L4, "SCALE_OUT_OF_TOLERANCE", _scale_out),
    ]

    # --- 7 越权 ---------------------------------------------------------
    def _scene_in_answer():
        return _answer(synthetic_execute(), scene_xml="<mujoco/>")

    def _host_field_in_answer():
        return _answer(synthetic_diagnose(), force_scale=0.4)

    def _host_field_in_params():
        inp = synthetic_diagnose()
        calls = list(inp.calls)
        calls[0] = {**calls[0],
                    "params": {**calls[0]["params"], "force_scale": 0.4}}
        return _calls(inp, calls)

    cases += [
        NegativeCase("N19", "privilege_escalation", "交卷夹带 scene_xml",
                     Layer.L0, "额外字段", _scene_in_answer),
        NegativeCase("N20", "privilege_escalation", "交卷夹带宿主字段 force_scale",
                     Layer.L0, "额外字段", _host_field_in_answer),
        NegativeCase("N21", "privilege_escalation", "请求参数夹带宿主字段",
                     Layer.L0, "额外字段", _host_field_in_params),
    ]
    return cases


def negative_cases() -> list[NegativeCase]:
    return _negative_cases()


# ---------------------------------------------------------------------------
# mutation（每条判据摘掉后，对应负控必须漏检）
# ---------------------------------------------------------------------------
#: 每个 mutation 只列**靠该判据才拦得住**的负控。别的判据独立拦下的样本不能写进来
#: （"换层仍被拒"不是漏检，算进来就是把杀灭率算虚）。
MUTANTS: dict[str, dict[str, Any]] = {
    "M1_trust_claimed_metrics": {"patch": "evaluator.series_metrics",
                                 "killed_by": ["N01"]},
    "M2_skip_replay_check": {"patch": "evaluator.replay_matches",
                             "killed_by": ["N05"]},
    "M3_skip_scene_check": {"patch": "evaluator.scene_matches",
                            "killed_by": ["N07"]},
    "M4_skip_steps_check": {"patch": "evaluator.steps_match",
                            "killed_by": ["N10"]},
    "M5_skip_criteria_completeness": {"patch": "evaluator.missing_criteria",
                                      "killed_by": ["N13"]},
    # 结论层：M6 让评测器采信候选自己的结论，于是 N02（自报效应量与复算不符）
    # 整条被放行。N16（结论判错）另有 C6 兜底，不属于这条判据的承重面。
    "M6_accept_claimed_verdict": {"patch": "evaluator.research_from_runs",
                                  "killed_by": ["N02"]},
}


# ---------------------------------------------------------------------------
# 判据定义哈希（AC20：判据冻结）
# ---------------------------------------------------------------------------
def _case_fingerprint(case: NegativeCase) -> dict[str, Any]:
    """负控的**行为**摘要，不是它的自述：输入归一化 + 每条重放输出一起入哈希。

    只哈希 case_id 是不够的——把某一例的期望层/期望码改弱，或者把它的输入换成
    一个更容易过的形状，哈希都不该原地不动。
    """
    try:
        inp = case.build()
        digest: str = sha256_of({
            "task": inp.task, "answer": inp.answer, "calls": list(inp.calls),
            "truth": inp.truth, "priv_rules": inp.priv_rules,
            "prereg": inp.prereg_sha256, "scene": inp.scene_sha256,
            "replay": [inp.replay(c) for c in inp.calls]})
    except Exception as exc:                      # 负控自己崩了也要入哈希，不许静默跳过
        digest = f"error:{type(exc).__name__}"
    return {"category": case.category, "layer": case.expected_layer.value,
            "code": case.expected_code, "input_sha256": digest}


def _judge_constants() -> dict[str, Any]:
    """改了就会改变判决的常量：手工列，不做反射（反射会把无关的也拖进来）。"""
    return {
        "claim_tol": CLAIM_TOL,
        "settle_angle_tol": SETTLE_ANGLE_TOL,
        "settle_vel_tol": SETTLE_VEL_TOL,
        "gravity": G,
        "research_steps_range": list(RESEARCH_STEPS_RANGE),
        "param_key_allowance": sorted(PARAM_KEY_ALLOWANCE),
        "answer_extra_allowance": sorted(ANSWER_EXTRA_ALLOWANCE),
    }


def criteria_definition(plans: Sequence[Any]) -> dict[str, Any]:
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "layers": [L.value for L in Layer],
        "research_criteria": list(RESEARCH_CRITERIA),
        "research_verdicts": list(RESEARCH_VERDICTS),
        # 预注册真值的接受规则：严格相等，或上位结论 INCONCLUSIVE 接受其子类
        "expected_acceptance": "exact_or_inconclusive_subtype",
        "answer_schemas": {f.value: answer_schema(f) for f in Family},
        "constants": _judge_constants(),
        "negative_categories": list(NEGATIVE_CATEGORIES),
        "negative_cases": {c.case_id: _case_fingerprint(c) for c in negative_cases()},
        "mutants": {name: {"patch": meta["patch"],
                           "killed_by": list(meta["killed_by"])}
                    for name, meta in MUTANTS.items()},
        # 公开面与**私有真值整份**都进哈希：改真值/预期结论/干预水平同样必须报警
        "instances": {p.instance_id: {"public_sha256": sha256_of(p.public),
                                      "private": copy.deepcopy(p.private)}
                      for p in plans},
    }


def criteria_hash(plans: Sequence[Any]) -> str:
    return sha256_of(criteria_definition(plans))


def observe(inp: EvaluationInput, case: NegativeCase) -> dict[str, Any]:
    """跑一个负控，三者分开记：期望层拒绝 / 实际码 / **整条是否被放行**。

    ``released`` 才是 mutation 的判据：把一条判据摘掉后，负控必须整条被放行；
    只是"换了另一层/换了个理由码仍被拒"说明别的判据还在挡，不能算漏检。
    """
    res = evaluate(inp)
    layer = res.layers.get(case.expected_layer)
    code = "MISSING_LAYER" if layer is None else f"{layer.layer.value}:{layer.reason_code}"
    rejected = (layer is not None and layer.status is LayerStatus.FAIL
                and case.expected_code.split(":")[0] in layer.reason_code)
    released = all(r.status in (LayerStatus.PASS, LayerStatus.NOT_APPLICABLE)
                   for r in res.layers.values())
    return {"rejected": rejected, "code": code, "released": released,
            "execution": res.execution.value}


def status_is_rejection(inp: EvaluationInput, case: NegativeCase) -> tuple[bool, str]:
    """跑一个负控，返回 (是否在期望层被拒, 实际码)。"""
    got = observe(inp, case)
    return got["rejected"], got["code"]


# ---------------------------------------------------------------------------
# mutation 执行
# ---------------------------------------------------------------------------
def mutant_applies(name: str) -> bool:
    return name in MUTANTS


def _patcher(name: str, inp: EvaluationInput):
    """返回一个上下文管理器：进入后评测器的一条判据被摘掉。"""
    from unittest import mock

    mod = "orchestrator.rsi_r0.evaluator"
    if name == "M1_trust_claimed_metrics":
        claim = dict(inp.answer.get("claim") or {"distance": 0.0, "final_speed": 0.0})
        return mock.patch(f"{mod}.series_metrics",
                          lambda *_a, **_k: {"distance": float(claim["distance"]),
                                             "final_speed": float(claim["final_speed"])})
    if name == "M2_skip_replay_check":
        return mock.patch(f"{mod}.replay_matches", lambda *_a, **_k: True)
    if name == "M3_skip_scene_check":
        return mock.patch(f"{mod}.scene_matches", lambda *_a, **_k: True)
    if name == "M4_skip_steps_check":
        return mock.patch(f"{mod}.steps_match", lambda *_a, **_k: True)
    if name == "M5_skip_criteria_completeness":
        return mock.patch(f"{mod}.missing_criteria", lambda *_a, **_k: [])
    if name == "M6_accept_claimed_verdict":
        ans = inp.answer

        def _echo(*_a, **_k):
            return {"levels": [], "angles": [], "effects": {},
                    "verdict": str(ans["verdict"]),
                    "mean_effect": float(ans["effect"]), "cells": {}}
        return mock.patch(f"{mod}.research_from_runs", _echo)
    raise KeyError(name)


def run_mutant(name: str, case: NegativeCase) -> dict[str, Any]:
    """在变异的评测器下重跑一个负控：**整条被放行 = 变异被杀（好）**。"""
    inp = case.build()
    with _patcher(name, inp):
        got = observe(inp, case)
    return {"mutant": name, "case": case.case_id,
            "rejected_at_expected_layer": got["rejected"],
            "released_under_mutation": got["released"],
            "execution": got["execution"], "code": got["code"]}
