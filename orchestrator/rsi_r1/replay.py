"""RSI-R1 T4：独立物理复跑证据（Plan §5.6，AC22/AC23 的机判件）。

三条不能混同的事实，分别取证：

1. **物理重放**：在**新的独立根**里、**新容器**中、按已冻结 spec 重跑同一批确认
   run，产出新 raw（新执行身份、新时间戳）。复跑 raw 与原件不得是同一份字节，
   也不得命中调用缓存——copy 原件或 cache 命中都只是"复现了自己"，不是物理重放。
2. **分析重算**：从原件 raw 重算指标只是再算一遍数字，不是重放；本模块的
   :func:`compare_run` 必须拿到**两份 raw** 才肯比。
3. **判决稳定**：把复跑 raw 重新交给判据，逐对比的主张状态必须与原件相同；
   区间靠边导致的翻判就是 ``REPLAY_DISAGREEMENT``——**不放宽容差、不改阈值**。

容差只在 ``rubric.REPLAY_TOLERANCES`` 登记（首校准前冻结，abs + rel×|原值|）。
本模块不 import ``judge``（判据自有独立实现）；判据件以纯报告 dict 进入
:func:`judge_disagreement`。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import rubric as R

REPLAY_SCHEMA = "simpraxis.rsi_r1.replay_comparison.v1"
SCHEMA_VERSION = 1

#: 复跑 artifact 必须具备的执行身份键（缺任一即 fail-closed）。
EXECUTION_KEYS = ("execution_id", "container_id", "started_at_utc",
                  "raw_sha256", "root")


class ReplayError(RuntimeError):
    """复跑证据不合规（copy 原件/命中缓存/同一执行身份/根不独立）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _canonical_sha(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def roundtrip_hash(record: Mapping[str, Any]) -> str:
    """raw 记录的内容哈希（用于识别"复跑"其实是同一份 raw）。"""
    return _canonical_sha(record)


# ---------------------------------------------------------------------------
# 独立性：必须是新根 / 新容器 / 新执行时间 / 不命中缓存 / 不复用原件字节
# ---------------------------------------------------------------------------

def assert_roots_independent(original_root: str, replay_root: str) -> None:
    """复跑根必须与原件根互不相等、互不嵌套（嵌套会把原件当复跑证据）。"""
    a = str(original_root).rstrip("/") or "/"
    b = str(replay_root).rstrip("/") or "/"
    if a == b:
        raise ReplayError("ROOT_NOT_INDEPENDENT", f"复跑根与原件根相同：{a}")
    if b.startswith(a + "/") or a.startswith(b + "/"):
        raise ReplayError("ROOT_NOT_INDEPENDENT", f"复跑根与原件根嵌套：{a} ⊂/⊃ {b}")


def verify_independence(*, original: Mapping[str, Any],
                        replay: Mapping[str, Any]) -> "dict[str, Any]":
    """逐项证明这不是"copy 原件"或"命中缓存"。

    ``original``/``replay`` 形如 ``{"record": ..., "meta": {...}}``；meta 必须
    携带 :data:`EXECUTION_KEYS`。返回证据 dict，任何一项不成立即
    :class:`ReplayError`（fail-closed，不做部分放行）。
    """
    ometa = original.get("meta") or {}
    rmeta = replay.get("meta") or {}
    for side, meta in (("original", ometa), ("replay", rmeta)):
        missing = [k for k in EXECUTION_KEYS if not meta.get(k)]
        if missing:
            raise ReplayError("EXECUTION_IDENTITY_MISSING",
                              f"{side} 缺 {missing}")
    if rmeta.get("cache_hit"):
        raise ReplayError("REPLAY_CACHE_HIT",
                          f"复跑命中缓存：{rmeta.get('cache_hit')!r}")
    if rmeta["execution_id"] == ometa["execution_id"]:
        raise ReplayError("SAME_EXECUTION", rmeta["execution_id"])
    if rmeta["container_id"] == ometa["container_id"]:
        raise ReplayError("SAME_CONTAINER", str(rmeta["container_id"]))
    if rmeta["raw_sha256"] == ometa["raw_sha256"]:
        raise ReplayError("COPIED_RAW", "复跑 raw_sha256 与原件相同")
    if rmeta["started_at_utc"] == ometa["started_at_utc"]:
        raise ReplayError("SAME_START_TIME", str(rmeta["started_at_utc"]))
    assert_roots_independent(str(ometa["root"]), str(rmeta["root"]))
    record_sha = roundtrip_hash(replay["record"])
    # 内容哈希相同不是"抄原件"的充分证据——确定性物理在相同输入下本就应当
    # 逐位复现；"抄"的判据是文件身份（raw_sha256）与执行身份（上面三项）。
    # 这里只记录事实，供 reviewer 识别"太完美"的复跑。
    record_identical = record_sha == roundtrip_hash(original["record"])
    return {"execution_id": rmeta["execution_id"],
            "container_id": rmeta["container_id"],
            "started_at_utc": rmeta["started_at_utc"],
            "raw_sha256": rmeta["raw_sha256"],
            "root": rmeta["root"],
            "record_sha256": record_sha,
            "record_identical": record_identical}


# ---------------------------------------------------------------------------
# 暴露/失稳类别与逐 block 数值比对
# ---------------------------------------------------------------------------

def exposure_class(*, study_id: str, record: Mapping[str, Any],
                   metrics: M.StudyMetrics) -> "dict[str, Any]":
    """暴露/失稳类别：复跑必须与原件的类别**完全相同**（不是"差不多"）。"""
    nsteps = len(record.get("time_s") or [])
    if study_id == "A":
        dims = record.get("contact_dim_per_step")
        if not isinstance(dims, list) or not dims:
            raise ReplayError("RAW_SHAPE", "A 缺 contact_dim_per_step")
        return {"censored": {k: bool(v) for k, v in sorted(metrics.censored.items())},
                "contact_dim": sorted({int(d) for d in dims}), "nsteps": nsteps}
    events = record.get("instability_events")
    if not isinstance(events, list):
        raise ReplayError("RAW_SHAPE", "B 缺 instability_events")
    return {"instability": bool(events),
            "reasons": sorted({str(e.get("reason")) for e in events if isinstance(e, dict)}),
            "nsteps": nsteps}


def _params(study_id: str, spec: E.ExperimentSpec,
            meta: Mapping[str, Any]) -> "dict[str, Any]":
    if study_id == "A":
        if meta.get("initial_x_m") is None:
            raise ReplayError("PARAM_MISSING", "A 缺 initial_x_m")
        return {"initial_x_m": meta["initial_x_m"]}
    return {"amplitude_rad": spec.physical_params["target_amplitude_rad"]}


def compare_run(*, study_id: str, spec: E.ExperimentSpec,
                original: Mapping[str, Any],
                replay: Mapping[str, Any]) -> "dict[str, Any]":
    """一条 run 的逐指标 abs+rel 比对 + 暴露类别比对。

    容差取自量表；右删失不一致与类别不一致都是硬失败（不是"数值近似"）。
    """
    ometa, rmeta = original.get("meta") or {}, replay.get("meta") or {}
    params = _params(study_id, spec, ometa)
    o_metrics = M.metrics_for(original["record"], study_id=study_id, **params)
    r_params = _params(study_id, spec, rmeta)
    r_metrics = M.metrics_for(replay["record"], study_id=study_id, **r_params)
    problems: "list[str]" = []
    metrics_out: "dict[str, Any]" = {}
    for metric_id in sorted(set(o_metrics.values) | set(r_metrics.values)
                            | set(o_metrics.censored)):
        if metric_id not in R.METRICS:
            problems.append(f"UNREGISTERED_METRIC:{metric_id}")
            continue
        o_censored = bool(o_metrics.censored.get(metric_id, False))
        r_censored = bool(r_metrics.censored.get(metric_id, False))
        if o_censored or r_censored:
            ok = o_censored and r_censored
            if not ok:
                problems.append(f"CENSORING_MISMATCH:{metric_id}")
            metrics_out[metric_id] = {"original": None, "replay": None,
                                      "censored": {"original": o_censored,
                                                   "replay": r_censored},
                                      "allowed": None, "delta": None, "ok": ok}
            continue
        o_val = float(o_metrics.values[metric_id])
        r_val = float(r_metrics.values[metric_id])
        tol_abs, tol_rel = R.replay_tolerance(metric_id)
        allowed = float(Decimal(str(tol_abs)) + Decimal(str(tol_rel)) * abs(Decimal(str(o_val))))
        delta = abs(o_val - r_val)
        ok = delta <= allowed
        if not ok:
            problems.append(f"NUMERIC_DRIFT:{metric_id}")
        metrics_out[metric_id] = {"original": o_val, "replay": r_val,
                                  "delta": delta, "allowed": allowed, "ok": ok}
    o_exp = exposure_class(study_id=study_id, record=original["record"],
                           metrics=o_metrics)
    r_exp = exposure_class(study_id=study_id, record=replay["record"],
                           metrics=r_metrics)
    if o_exp != r_exp:
        problems.append("EXPOSURE_CLASS_MISMATCH")
    return {"run_id": spec.run_id, "condition_id": spec.condition_id,
            "block_id": spec.block_id, "metrics": metrics_out,
            "exposure": {"original": o_exp, "replay": r_exp,
                         "ok": o_exp == r_exp},
            "problems": problems, "ok": not problems}


# ---------------------------------------------------------------------------
# 整 study：run 集合相同才算复跑；判决必须稳定
# ---------------------------------------------------------------------------

def compare_study(*, plan: Any, specs: Mapping[str, E.ExperimentSpec],
                  originals: Mapping[str, Mapping[str, Any]],
                  replays: Mapping[str, Mapping[str, Any]]) -> "dict[str, Any]":
    """``originals``/``replays``：``run_id → {"record", "meta"}``。"""
    expected = set(specs)
    missing = sorted(expected - set(replays))
    extra = sorted(set(replays) - expected)
    missing_orig = sorted(expected - set(originals))
    runs: "list[dict[str, Any]]" = []
    problems: "list[str]" = []
    if missing_orig:
        problems.append(f"ORIGINAL_MISSING:{missing_orig[:5]}")
    if missing:
        problems.append(f"NOT_REPLAYED:{missing[:5]}")
    if extra:
        problems.append(f"EXTRA_REPLAY:{extra[:5]}")
    for run_id in sorted(expected & set(originals) & set(replays)):
        spec = specs[run_id]
        independence = verify_independence(original=originals[run_id],
                                           replay=replays[run_id])
        cmp = compare_run(study_id=plan.study_id, spec=spec,
                          original=originals[run_id], replay=replays[run_id])
        cmp["independence"] = independence
        if not cmp["ok"]:
            problems.extend(f"{run_id}:{p}" for p in cmp["problems"])
        runs.append(cmp)
    return {"schema": REPLAY_SCHEMA, "schema_version": SCHEMA_VERSION,
            "study_id": plan.study_id, "plan_sha256": plan.sha256,
            "n_expected": len(expected), "n_replayed": len(replays),
            "runs": runs, "problems": problems, "ok": not problems}


def judge_disagreement(original_report: Mapping[str, Any],
                       replay_report: Mapping[str, Any]) -> "dict[str, Any]":
    """逐对比比主张状态与分母；**翻判就是 REPLAY_DISAGREEMENT**。

    只看两层报告的科学层（``layers.scientific``），不读候选提交件——候选在
    复跑里没有发言权。
    """
    def _by_id(report: Mapping[str, Any]) -> "dict[str, dict]":
        layers = report.get("layers") or {}
        return {str(c.get("id")): dict(c) for c in layers.get("scientific") or []}

    a, b = _by_id(original_report), _by_id(replay_report)
    problems: "list[str]" = []
    if set(a) != set(b):
        problems.append(f"CONTRAST_SET:{sorted(set(a) ^ set(b))}")
    rows: "list[dict[str, Any]]" = []
    for cid in sorted(set(a) & set(b)):
        left, right = a[cid], b[cid]
        status_same = left.get("status") == right.get("status")
        n_same = int(left.get("n_blocks") or 0) == int(right.get("n_blocks") or 0)
        if not status_same:
            problems.append(f"REPLAY_DISAGREEMENT:{cid}:"
                            f"{left.get('status')}→{right.get('status')}")
        if not n_same:
            problems.append(f"DENOMINATOR_CHANGED:{cid}")
        rows.append({"contrast_id": cid, "original": left.get("status"),
                     "replay": right.get("status"),
                     "n_original": left.get("n_blocks"),
                     "n_replay": right.get("n_blocks"),
                     "ok": status_same and n_same})
    return {"schema": f"{REPLAY_SCHEMA}.judgement", "schema_version": SCHEMA_VERSION,
            "study_id": original_report.get("study_id"),
            "expectation_sha256": _canonical_sha(original_report),
            "replay_sha256": _canonical_sha(replay_report),
            "contrasts": rows, "problems": problems, "ok": not problems}


def summarize(comparisons: Sequence[Mapping[str, Any]]) -> "dict[str, int]":
    """台账用计数（不放行任何失败项）。"""
    return {"runs": len(comparisons),
            "ok": sum(1 for c in comparisons if c.get("ok")),
            "failed": sum(1 for c in comparisons if not c.get("ok"))}
