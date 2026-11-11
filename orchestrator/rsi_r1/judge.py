"""RSI-R1 T4：judge —— 从可信 raw 独立重算科学证据（Plan §5.6）。

judge **只**读：冻结的 ``ResearchPlan``、host 分配的 ``ConfirmManifest``、宿主写的
raw（record+meta）与候选提交件。它**不**采信候选的 summary/analysis 作为原始事实，
也**不** import ``statistics.py``（重抽样与主张分类各自独立实现；两者逐位一致由
``tests/test_rsi_r1_judge.py`` 互证）。候选提交的效应/区间/分类与 judge 重算按
``1e-8 + 1e-6×|ref|`` 比；不一致即 ``CLAIM_INVALID``，**不得**用 judge 自己的
正确结论替候选过关。

三层判决（Plan §5.6，``ok=true`` 不再充当三层同义词）：

1. ``schema_accepted``：提交件/计划/manifest 结构合法；
2. ``evidence_valid``：分母完整、block 配对、单因子识别、raw hash 与 spec 绑定；
3. ``scientific``：每条登记主张的 SUPPORTED/REFUTED/INCONCLUSIVE（由重算区间决定）。

识别规则（锁内适配，记录于 Complete D-R1-05）：合同未登记"许可干预集"字段，
故 judge 采用**单因子识别**军规——对比两端条件必须恰差 1 个参数（阴性控制还必须
恰为量表登记的 ``NEGATIVE_CONTROL_ALLOWED_DIFF``），≥2 个即判 ``CONFOUNDED``。
该规则只影响 judge 的证据层，不改量表数值、不改 rubric sha。
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import rubric as R

JUDGE_SCHEMA = "simpraxis.rsi_r1.judge_report.v1"
SUBMISSION_SCHEMA = "simpraxis.rsi_r1.claim_submission.v1"
SCHEMA_VERSION = 1

#: judge 自己的统计 seed（必与 statistics.STATISTICS_SEED 相同；测试互证）。
JUDGE_STATISTICS_SEED = 20261111

#: 登记指标 → raw 重算值键。阴性控制指标复用位移量本身（量表的 δ 不同、量相同）。
_METRIC_VALUE_KEY = {
    "displacement_x_m": "displacement_x_m",
    "displacement_x_m_negative_equivalence": "displacement_x_m",
    "stop_time_s": "stop_time_s",
    "normalized_tracking_rmse": "normalized_tracking_rmse",
}


class JudgeError(RuntimeError):
    """judge 输入/证据不合规；带机器码。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 独立统计实现（不 import statistics.py）
# ---------------------------------------------------------------------------

def _finite(value: Any, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgeError("WRONG_TYPE", f"{where}={value!r} 不是实数")
    out = float(value)
    if not math.isfinite(out):
        raise JudgeError("NONFINITE", f"{where}={value!r} 非有限")
    return out


def _numeric(value: Any, *, where: str) -> float:
    """参数值（spec 里是规范字符串）→ float；布尔与不可解析值仍拒绝。"""
    if isinstance(value, bool):
        raise JudgeError("WRONG_TYPE", f"{where}={value!r} 是布尔")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise JudgeError("WRONG_TYPE", f"{where}={value!r} 不是实数") from None
    if not math.isfinite(out):
        raise JudgeError("NONFINITE", f"{where}={value!r} 非有限")
    return out


def _quantile_linear(sorted_values: "np.ndarray", q: float) -> float:
    """linear 分位（与 numpy 默认 ``method="linear"`` 同一插值定义）。"""
    n = int(sorted_values.shape[0])
    if n == 0:
        raise JudgeError("EMPTY", "分位输入为空")
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def _bootstrap_ci(values: Iterable[float], *, level: str | Decimal) -> "tuple[float, float]":
    """percentile bootstrap：block 重抽样、10000 次、独立 RNG（PCG64 同 seed）。"""
    arr = np.asarray([_finite(v, where="diff") for v in values], dtype=np.float64)
    if arr.shape[0] < 2:
        raise JudgeError("INSUFFICIENT_N", f"block 数 {arr.shape[0]} < 2")
    lv = Decimal(str(level))
    if not (Decimal("0") < lv < Decimal("1")):
        raise JudgeError("LEVEL_INVALID", f"level={lv}")
    rng = np.random.Generator(np.random.PCG64(JUDGE_STATISTICS_SEED))
    n = int(arr.shape[0])
    draw = rng.integers(0, n, size=(int(R.BOOTSTRAP_RESAMPLES), n))
    means = np.sort(arr[draw].mean(axis=1))
    alpha = float((Decimal("1") - lv) / Decimal("2"))
    return _quantile_linear(means, alpha), _quantile_linear(means, 1.0 - alpha)


def _classify(proposition: str, delta: Any, lower: Any, upper: Any) -> str:
    """独立实现的主张分类（语义必须与公开量表逐条相同）。"""
    if proposition not in R.PROPOSITIONS:
        raise JudgeError("PROPOSITION_INVALID", repr(proposition))
    d = Decimal(str(delta))
    lo = Decimal(str(lower))
    hi = Decimal(str(upper))
    if lo > hi:
        raise JudgeError("CI_INVALID", f"L={lo} > U={hi}")
    if proposition == "decrease_gt":
        if hi < -d:
            return "SUPPORTED"
        if lo >= -d:
            return "REFUTED"
        return "INCONCLUSIVE"
    if proposition == "increase_gt":
        if lo > d:
            return "SUPPORTED"
        if hi <= d:
            return "REFUTED"
        return "INCONCLUSIVE"
    if lo > -d and hi < d:
        return "SUPPORTED"
    if hi <= -d or lo >= d:
        return "REFUTED"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# 独立指标重算（不 import metrics.py）
# ---------------------------------------------------------------------------

def _col(record: Mapping[str, Any], name: str, index: int = 0) -> "list[float]":
    series = record.get(name)
    if not isinstance(series, list) or not series:
        raise JudgeError("RAW_SHAPE", f"{name} 不是非空数组")
    out: "list[float]" = []
    for row in series:
        if isinstance(row, list):
            if index >= len(row):
                raise JudgeError("RAW_SHAPE", f"{name} 行宽不足")
            out.append(float(row[index]))
        else:
            if index != 0:
                raise JudgeError("RAW_SHAPE", f"{name} 是标量序列")
            out.append(float(row))
    return out


def _times(record: Mapping[str, Any]) -> "list[float]":
    times = [float(t) for t in record["time_s"]]
    if len(times) < 2:
        raise JudgeError("RAW_SHAPE", "time_s 太短")
    dt = times[1] - times[0]
    if dt <= 0 or not math.isfinite(dt):
        raise JudgeError("RAW_SHAPE", f"dt={dt!r}")
    for i in range(1, len(times)):
        if abs((times[i] - times[i - 1]) - dt) > 1e-9:
            raise JudgeError("DT_MISMATCH", f"[{i-1}]→[{i}] 间隔不均")
    return times


def _stop_time(times: "Sequence[float]", vx: "Sequence[float]") -> "float | None":
    eps = float(R.STOP_VX_EPS_MPS)
    hold = int(round(float(R.STOP_HOLD_S) / (times[1] - times[0])))
    n = len(times)
    for i in range(n):
        if abs(vx[i]) >= eps:
            continue
        if i + hold >= n:
            return None
        if all(abs(vx[j]) < eps for j in range(i, i + hold + 1)):
            return float(times[i])
    return None


def _metric_a(record: Mapping[str, Any], *, initial_x_m: Any) -> "dict[str, Any]":
    times = _times(record)
    x = _col(record, "qpos", 0)
    vx = _col(record, "qvel", 0)
    if len(x) != len(times) or len(vx) != len(times):
        raise JudgeError("RAW_SHAPE", "qpos/qvel 与 time_s 不等长")
    stop = _stop_time(times, vx)
    metrics = {"displacement_x_m": x[-1] - _numeric(initial_x_m, where="initial_x_m")}
    if stop is not None:
        metrics["stop_time_s"] = stop
    return {"values": metrics, "censored": {"stop_time_s": stop is None},
            "nsteps": len(times)}


def _metric_b(record: Mapping[str, Any], *, amplitude_rad: Any) -> "dict[str, Any]":
    times = _times(record)
    amp = _numeric(amplitude_rad, where="amplitude_rad")
    if amp <= 0:
        raise JudgeError("PARAM_INVALID", f"amplitude={amp}")
    events = record.get("instability_events") or []
    if events:
        return {"values": {"normalized_tracking_rmse": float(R.LOSS_CAP)},
                "censored": {"normalized_tracking_rmse": False},
                "nsteps": len(times), "instability": True}
    q = _col(record, "qpos", 0)
    target = _col(record, "target_q", 0)
    lo, hi = float(R.TRACKING_WINDOW_START_S), float(R.TRACKING_WINDOW_END_S)
    idx = [i for i, t in enumerate(times) if lo <= t <= hi]
    if len(idx) < 2:
        raise JudgeError("WINDOW_EMPTY", f"[{lo},{hi}] 内 {len(idx)} 个采样")
    if len(q) != len(times) or len(target) != len(times):
        raise JudgeError("RAW_SHAPE", "qpos/target_q 与 time_s 不等长")
    sq = math.fsum((q[i] - target[i]) ** 2 for i in idx)
    return {"values": {"normalized_tracking_rmse": math.sqrt(sq / len(idx)) / amp},
            "censored": {"normalized_tracking_rmse": False},
            "nsteps": len(times), "instability": False}


def recompute_metrics(*, plan: C.ResearchPlan, artifacts: Mapping[str, Mapping[str, Any]],
                      specs: Mapping[str, E.ExperimentSpec]) -> "dict[str, dict[str, Any]]":
    """逐 run 重算指标；``artifacts[run_id] = {"meta", "record"}``。"""
    out: "dict[str, dict[str, Any]]" = {}
    for run_id in sorted(artifacts):
        spec = specs.get(run_id)
        if spec is None:
            raise JudgeError("EXTRA_RUN", f"{run_id} 不在确认分配里")
        artifact = artifacts[run_id]
        record = artifact.get("record")
        meta = artifact.get("meta") or {}
        if not isinstance(record, Mapping):
            raise JudgeError("MISSING_RECORD", run_id)
        if meta.get("run_id") != run_id or record.get("run_id") != run_id:
            raise JudgeError("RUN_ID_MISMATCH", run_id)
        if record.get("block_id") != spec.block_id:
            raise JudgeError("BLOCK_MISMATCH", f"{run_id}: {record.get('block_id')}")
        if record.get("spec_hash") != spec.spec_hash or meta.get("spec_hash") != spec.spec_hash:
            raise JudgeError("SPEC_MISMATCH", run_id)
        if meta.get("condition_id") != spec.condition_id:
            raise JudgeError("CONDITION_SWAP", f"{run_id}: meta={meta.get('condition_id')}")
        if plan.study_id == "A":
            rest = _metric_a(record, initial_x_m=meta.get("initial_x_m"))
        else:
            rest = _metric_b(record, amplitude_rad=spec.physical_params["target_amplitude_rad"])
        out[run_id] = {**rest, "condition_id": spec.condition_id,
                       "block_id": spec.block_id,
                       "spec_hash": spec.spec_hash}
    return out


# ---------------------------------------------------------------------------
# 结构层：确认分配 → host 重算 spec
# ---------------------------------------------------------------------------

def build_specs(plan: C.ResearchPlan,
                manifest: E.ConfirmManifest) -> "dict[str, E.ExperimentSpec]":
    """按确认 manifest 由 host 重建可信 spec（与候选输入无关）。

    走单真值构造点 ``E.build_host_spec``（不做探索矩阵的 64-run 帽判定——
    确认批是 32×条件，本就多于探索帽）。
    """
    conditions = {c.id: c for c in plan.conditions}
    specs: "dict[str, E.ExperimentSpec]" = {}
    for a in manifest.assignments:
        condition = conditions.get(a.condition_id)
        if condition is None:
            raise JudgeError("CONDITION_UNKNOWN",
                             f"manifest 条件 {a.condition_id!r} 不在 plan")
        spec = E.build_host_spec(plan_sha256=plan.sha256,
                                 study_id=plan.study_id,
                                 condition_id=condition.id,
                                 block_id=a.block_id,
                                 scene_id=condition.scene_id,
                                 params=a.params)
        if spec.run_id in specs:
            raise JudgeError("DUPLICATE_RUN", spec.run_id)
        specs[spec.run_id] = spec
    return specs


def _check_manifest(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                    specs: Mapping[str, E.ExperimentSpec]) -> "list[dict[str, Any]]":
    checks: "list[dict[str, Any]]" = []

    def add(cid: str, ok: bool, detail: str = "") -> None:
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    add("J-MANIFEST-STUDY", manifest.study_id == plan.study_id,
        f"{manifest.study_id} vs {plan.study_id}")
    add("J-MANIFEST-PLAN", manifest.plan_sha256 == plan.sha256,
        f"{manifest.plan_sha256[:12]} vs {plan.sha256[:12]}")
    conds = {c.id for c in plan.conditions}
    expected = len(plan.conditions) * int(R.CONFIRM_BLOCKS)
    add("J-DENOMINATOR", len(manifest.assignments) == expected
        and len(specs) == expected,
        f"assignments={len(manifest.assignments)} specs={len(specs)} 期望={expected}")
    per_cond: "dict[str, set[str]]" = {}
    for a in manifest.assignments:
        per_cond.setdefault(a.condition_id, set()).add(a.block_id)
    add("J-CONDITIONS", set(per_cond) == conds, f"{sorted(per_cond)} vs {sorted(conds)}")
    want_blocks = {f"b{i:02d}" for i in range(1, int(R.CONFIRM_BLOCKS) + 1)}
    add("J-32-BLOCKS", all(blocks == want_blocks for blocks in per_cond.values()),
        "每个条件必须恰有 b01..b32")

    # block 配对：varying 参数必须逐 block 跨条件相同（common random numbers）。
    varying = sorted(manifest.varying)
    by_block: "dict[str, dict[str, Mapping[str, str]]]" = {}
    for a in manifest.assignments:
        by_block.setdefault(a.block_id, {})[a.condition_id] = a.params
    unpaired: "list[str]" = []
    for block_id, rows in sorted(by_block.items()):
        values = {tuple(rows[c].get(p) for p in varying) for c in rows}
        if len(values) != 1:
            unpaired.append(block_id)
    add("J-BLOCK-PAIRING", not unpaired, f"配对不一致 block={unpaired}")

    # 单因子识别（锁内适配 D-R1-05）：每个登记对比的两端恰差 1 个参数；
    # 阴性控制还必须恰为量表登记的 NEGATIVE_CONTROL_ALLOWED_DIFF。
    confounded: "list[str]" = []
    for contrast in _contrasts(plan):
        c1, c2 = contrast.condition_ids
        if c1 not in per_cond or c2 not in per_cond:
            confounded.append(f"{contrast.id}:missing-condition")
            continue
        # 用首个 block 的静态参数（varying 已保证逐 block 相等）
        p1 = _static_params(plan, manifest, c1)
        p2 = _static_params(plan, manifest, c2)
        diff = sorted(k for k in set(p1) | set(p2) if p1.get(k) != p2.get(k))
        if contrast.kind == "negative":
            if tuple(diff) != tuple(R.NEGATIVE_CONTROL_ALLOWED_DIFF):
                confounded.append(
                    f"{contrast.id}:negative-diff={diff} 期望 "
                    f"{list(R.NEGATIVE_CONTROL_ALLOWED_DIFF)}")
            continue
        if len(diff) != 1:
            confounded.append(f"{contrast.id}:{diff}")
    add("J-IDENTIFICATION", not confounded, f"非单因子对比={confounded}")
    return checks


def _static_params(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                   condition_id: str) -> "dict[str, str]":
    for a in manifest.assignments:
        if a.condition_id == condition_id:
            return {k: v for k, v in a.params.items() if k not in manifest.varying}
    raise JudgeError("CONDITION_UNKNOWN", condition_id)


def _contrasts(plan: C.ResearchPlan) -> "list[C.ContrastSpec]":
    out = [plan.primary]
    if plan.negative_control is not None:
        out.append(plan.negative_control)
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def judge_study(*, plan: C.ResearchPlan, manifest: E.ConfirmManifest,
                artifacts: Mapping[str, Mapping[str, Any]],
                submission: "Mapping[str, Any] | None" = None) -> "dict[str, Any]":
    """三层判决。``artifacts[run_id] = {"record": {...}, "meta": {...}}``。"""
    checks: "list[dict[str, Any]]" = []
    specs = build_specs(plan, manifest)
    checks.extend(_check_manifest(plan, manifest, specs))

    missing = sorted(set(specs) - set(artifacts))
    extra = sorted(set(artifacts) - set(specs))
    checks.append({"id": "J-RUN-SET", "ok": not missing and not extra,
                   "detail": f"missing={missing[:5]} extra={extra[:5]}"})
    if missing or extra:
        return _report(plan, manifest, checks, [], _schema_check(plan, submission,
                                                                 manifest_sha=manifest.sha256))

    per_run = recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    checks.append({"id": "J-METRICS-RECOMPUTED", "ok": True,
                   "detail": f"{len(per_run)} runs"})

    contrasts: "list[dict[str, Any]]" = []
    for contrast in _contrasts(plan):
        contrasts.append(_judge_contrast(plan, per_run, contrast, checks))

    schema_layer = _schema_check(plan, submission, manifest_sha=manifest.sha256)
    claim_layer = _claim_check(plan, contrasts, submission, checks)
    return _report(plan, manifest, checks, contrasts,
                   {**schema_layer, **claim_layer}, per_run=per_run)


def _judge_contrast(plan: C.ResearchPlan, per_run: Mapping[str, Mapping[str, Any]],
                    contrast: C.ContrastSpec, checks: "list[dict[str, Any]]") -> dict:
    t_cond, c_cond = contrast.condition_ids
    if contrast.metric not in R.METRICS:
        raise JudgeError("METRIC_UNREGISTERED", contrast.metric)
    if R.metric_delta(contrast.metric) != contrast.delta:
        raise JudgeError("DELTA_DRIFT", f"{contrast.id}: {contrast.delta}")
    level = str(R.CI_NEGATIVE if contrast.kind == "negative" else R.CI_PRIMARY)
    by_block: "dict[str, dict[str, float]]" = {}
    for run in per_run.values():
        if run["condition_id"] not in (t_cond, c_cond):
            continue
        block = run["block_id"]
        value_key = _METRIC_VALUE_KEY.get(contrast.metric)
        if value_key is None:
            raise JudgeError("METRIC_UNREGISTERED", contrast.metric)
        if value_key in run["values"]:
            by_block.setdefault(block, {})[run["condition_id"]] = float(
                run["values"][value_key])
    pairs: "dict[str, float]" = {}
    dropped: "list[str]" = []
    for block in sorted(by_block):
        slot = by_block[block]
        if t_cond not in slot or c_cond not in slot:
            dropped.append(block)
            continue
        pairs[block] = slot[t_cond] - slot[c_cond]
    checks.append({"id": f"J-PAIRED-{contrast.id}", "ok": not dropped and bool(pairs),
                   "detail": f"n={len(pairs)} dropped={dropped}"})
    if not pairs:
        return {"id": contrast.id, "metric_id": contrast.metric,
                "proposition": contrast.proposition, "delta": str(contrast.delta),
                "status": "INCONCLUSIVE", "n_blocks": 0, "effect": None,
                "lower": None, "upper": None, "error": "NO_PAIRED_BLOCKS"}
    lower, upper = _bootstrap_ci(pairs.values(), level=level)
    effect = math.fsum(pairs.values()) / len(pairs)
    status = _classify(contrast.proposition, contrast.delta, lower, upper)
    return {"id": contrast.id, "kind": contrast.kind, "metric_id": contrast.metric,
            "proposition": contrast.proposition, "delta": str(contrast.delta),
            "level": level, "n_blocks": len(pairs), "effect": effect,
            "lower": lower, "upper": upper, "status": status}


def _schema_check(plan: C.ResearchPlan, submission: "Mapping[str, Any] | None",
                  *, manifest_sha: str) -> "dict[str, Any]":
    if submission is None:
        return {"schema_accepted": False,
                "schema_detail": "没有候选提交件（claims 未提交）"}
    if not isinstance(submission, Mapping):
        return {"schema_accepted": False, "schema_detail": "提交件不是对象"}
    problems: "list[str]" = []
    if submission.get("schema") != SUBMISSION_SCHEMA:
        problems.append(f"schema={submission.get('schema')!r}")
    if submission.get("study_id") != plan.study_id:
        problems.append(f"study_id={submission.get('study_id')!r}")
    if submission.get("plan_sha256") != plan.sha256:
        problems.append("plan_sha256 漂移")
    if submission.get("confirm_manifest_sha256") not in (None, manifest_sha):
        problems.append("confirm_manifest_sha256 漂移")
    claims = submission.get("claims")
    if not isinstance(claims, list) or not claims:
        problems.append("claims 必须是非空数组")
        claims = []
    for i, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            problems.append(f"claims[{i}] 不是对象")
            continue
        need = {"hypothesis_id", "proposition", "metric_id", "delta",
                "estimate", "lower", "upper"}
        missing = sorted(need - set(claim))
        extra = sorted(set(claim) - need - {"n_blocks"})
        if missing:
            problems.append(f"claims[{i}] 缺 {missing}")
        if extra:
            problems.append(f"claims[{i}] 多余 {extra}")
        if claim.get("proposition") not in R.PROPOSITIONS:
            problems.append(f"claims[{i}] proposition 非法")
        if claim.get("metric_id") not in R.METRICS:
            problems.append(f"claims[{i}] metric 未注册")
        elif Decimal(str(claim.get("delta"))) != R.metric_delta(str(claim["metric_id"])):
            problems.append(f"claims[{i}] delta 漂移")
        try:
            lo = float(claim["lower"])
            hi = float(claim["upper"])
            if not (lo <= hi):
                problems.append(f"claims[{i}] lower > upper")
        except Exception:  # noqa: BLE001
            problems.append(f"claims[{i}] lower/upper 非数")
    primary = submission.get("primary_contrast_id")
    if primary is not None and primary != plan.primary.id:
        problems.append(f"primary_contrast_id={primary!r} != 登记 {plan.primary.id!r}")
    return {"schema_accepted": not problems, "schema_detail": problems}


def _claim_check(plan: C.ResearchPlan, contrasts: "list[dict[str, Any]]",
                 submission: "Mapping[str, Any] | None",
                 checks: "list[dict[str, Any]]") -> "dict[str, Any]":
    if not submission:
        return {"claim_status": "MISSING", "claim_mismatches": []}
    hyp = {h.id: h for h in plan.hypotheses}
    by_id = {c["id"]: c for c in contrasts}
    mismatches: "list[str]" = []
    seen: "set[str]" = set()
    for i, claim in enumerate(submission.get("claims") or []):
        hid = claim.get("hypothesis_id")
        h = hyp.get(str(hid))
        if h is None:
            mismatches.append(f"claims[{i}] hypothesis_id={hid!r} 未登记")
            continue
        seen.add(h.contrast_id)
        target = by_id.get(h.contrast_id)
        if target is None:
            mismatches.append(f"claims[{i}] 指向未判对比 {h.contrast_id!r}")
            continue
        if claim.get("metric_id") != target["metric_id"]:
            mismatches.append(f"claims[{i}] metric_id 与对比不符")
            continue
        for key, ref_key in (("estimate", "effect"), ("lower", "lower"),
                             ("upper", "upper")):
            ref = target.get(ref_key)
            if ref is None:
                mismatches.append(f"claims[{i}] 对比无 {ref_key}")
                continue
            got = _finite(claim.get(key), where=f"claims[{i}].{key}")
            if abs(got - float(ref)) > float(R.CLAIM_ABS_TOL) + \
                    float(R.CLAIM_REL_TOL) * abs(float(ref)):
                mismatches.append(
                    f"claims[{i}].{key}={got!r} != judge {ref!r}")
        if claim.get("proposition") != target["proposition"]:
            mismatches.append(f"claims[{i}] proposition 与登记对比不符")
        if claim.get("n_blocks") is not None:
            try:
                if int(claim["n_blocks"]) != int(target["n_blocks"]):
                    mismatches.append(
                        f"claims[{i}].n_blocks={claim['n_blocks']} != {target['n_blocks']}")
            except (TypeError, ValueError):
                mismatches.append(f"claims[{i}].n_blocks 非整数")
    # 每条登记主张都必须被候选逐条提交（不能用汇总替代）
    for contrast in contrasts:
        if contrast["id"] not in seen and any(
                h.contrast_id == contrast["id"] for h in hyp.values()):
            mismatches.append(f"对比 {contrast['id']!r} 没有对应 claim")
    checks.append({"id": "J-CLAIM-MATCH", "ok": not mismatches,
                   "detail": mismatches[:6]})
    return {"claim_status": "MATCH" if not mismatches else "CLAIM_INVALID",
            "claim_mismatches": mismatches}


def _report(plan: C.ResearchPlan, manifest: E.ConfirmManifest,
            checks: "list[dict[str, Any]]", contrasts: "list[dict[str, Any]]",
            layers: Mapping[str, Any], *,
            per_run: "Mapping[str, Mapping[str, Any]] | None" = None) -> dict:
    evidence_ok = all(c["ok"] for c in checks)
    claims_ok = layers.get("claim_status") in (None, "MATCH", "MISSING")
    schema_ok = bool(layers.get("schema_accepted"))
    scientific = [{k: c[k] for k in ("id", "kind", "metric_id", "proposition",
                                    "delta", "level", "n_blocks", "effect",
                                    "lower", "upper", "status") if k in c}
                  for c in contrasts]
    return {
        "schema": JUDGE_SCHEMA, "schema_version": SCHEMA_VERSION,
        "study_id": plan.study_id, "plan_sha256": plan.sha256,
        "confirm_manifest_sha256": manifest.sha256,
        "rubric_sha256": R.rubric_sha256(),
        "layers": {
            "schema_accepted": schema_ok,
            "schema_detail": layers.get("schema_detail"),
            "evidence_valid": evidence_ok,
            "claim_status": layers.get("claim_status"),
            "scientific": scientific,
        },
        "claim_mismatches": list(layers.get("claim_mismatches") or []),
        "checks": checks,
        "per_run": ({k: {"values": {m: str(v) for m, v in sorted(r["values"].items())},
                         "censored": dict(r["censored"]),
                         "condition_id": r["condition_id"],
                         "block_id": r["block_id"],
                         "spec_hash": r["spec_hash"]}
                     for k, r in sorted(per_run.items())}
                    if per_run is not None else None),
        "verdict": ("JUDGE_REJECT" if (not schema_ok or not evidence_ok or not claims_ok)
                    else "JUDGE_OK"),
    }


def write_report(path: Any, report: Mapping[str, Any]) -> None:
    """0600 原子写（与其余合同件同纪律）。"""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                              indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)
