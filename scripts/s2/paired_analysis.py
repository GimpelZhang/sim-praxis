"""S2 配对统计（S2-5 初判）：精确 paired-permutation + PhysicalRSI 上游选择器。

**预注册主统计**（计划 §3 S2-5 原文）：以「实例对」为配对单位（6 对），
对 review-pass 率配对差做**精确符号翻转置换检验**（2^6=64 个符号模式；
单侧最小 p=1/64=0.015625，双侧最小 p=2/64=0.03125）。

**上游交叉核**：把同一配对结果适配成 PhysicalRSI 的
``comparison/cohort/results`` 结构，调用
``PhysicalRSI_core.self_harness.selection.select_survivor``（``inherit_child`` /
``retain_parent``）。上游要求完整配对与可核证据；适配失败如实记录
``UPSTREAM_ADAPTER_BLOCKED``，**不伪造**上游结论（计划 §7 风险表）。

非劣约束（预注册）：工程完成率、复跑 n_diffs=0、G-CLAIM 违规 0、会话成本比
≤ 1.50——任一不满足即 ``NON_INFERIORITY_VIOLATED``。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["SCHEMA", "AnalysisError", "exact_sign_flip_p", "permutation_table",
           "non_inferiority", "adapter_to_physicalrsi", "analyze"]

SCHEMA = "simpraxis.rsi_s2.paired_analysis.v1"


class AnalysisError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(doc: Any) -> str:
    return hashlib.sha256(_canonical(doc)).hexdigest()


def exact_sign_flip_p(diffs: Sequence[float]) -> "dict[str, Any]":
    """精确符号翻转置换检验（单侧/双侧；全 2^n 符号模式）。"""
    values = [float(d) for d in diffs]
    if not values:
        raise AnalysisError("EMPTY", "没有配对差")
    n = len(values)
    observed = math.fsum(values)
    total = 0
    at_least = 0
    opposite = 0
    for signs in itertools.product((1.0, -1.0), repeat=n):
        stat = math.fsum(s * v for s, v in zip(signs, values))
        total += 1
        if stat >= observed - 1e-12:
            at_least += 1
        if -stat >= observed - 1e-12:
            opposite += 1
    one_sided = at_least / total
    two_sided = min(1.0, 2.0 * min(one_sided, opposite / total))
    return {"n_pairs": n, "n_sign_patterns": total,
            "observed_sum": observed, "mean_diff": observed / n,
            "p_one_sided": one_sided, "p_two_sided": two_sided,
            "min_p_one_sided": 1.0 / total,
            "min_p_two_sided": 2.0 / total}


def permutation_table(instance_ids: Sequence[str],
                      diffs: Sequence[float]) -> "dict[str, Any]":
    if len(instance_ids) != len(diffs):
        raise AnalysisError("LENGTH_MISMATCH", "实例数与配对差不等")
    result = exact_sign_flip_p(diffs)
    return {"schema": f"{SCHEMA}.permutation",
            "paired_unit": "instance",
            "instances": list(instance_ids),
            "diffs": [float(d) for d in diffs],
            **result,
            "alpha": 0.05,
            "decision": ("inherit_child" if result["p_two_sided"] < 0.05
                         and result["mean_diff"] > 0 else "retain_parent")}


def non_inferiority(*, v0: Mapping[str, Any], v1: Mapping[str, Any],
                    bounds: Mapping[str, Any]) -> "dict[str, Any]":
    """预注册非劣约束逐项（工程/复跑/成本）。"""
    checks = {
        "engineering_completion": (
            int(v1.get("completed", 0)) >= int(v0.get("completed", 0))),
        "rerun_n_diffs_zero": (
            int(v1.get("rerun_n_diffs", 0)) == 0
            and int(v0.get("rerun_n_diffs", 0)) == 0),
        "g_claim_violations_zero": (
            int(v1.get("g_claim_violations", 0)) == 0
            and int(v0.get("g_claim_violations", 0)) == 0),
        "session_cost_ratio": (
            float(v1.get("session_tokens", 0) or 0)
            <= float(bounds.get("session_cost_ratio_max", 1.5))
            * max(1.0, float(v0.get("session_tokens", 0) or 0))),
    }
    return {"checks": checks, "passed": all(checks.values()),
            "bounds": dict(bounds)}


def adapter_to_physicalrsi(*, instances: Sequence[str],
                           scores: Mapping[str, Sequence[float]],
                           successes: Mapping[str, Sequence[bool]],
                           evidence_root: Path,
                           evidence_files: Mapping[str, Sequence[Path]],
                           minimum_gain: float = 0.5) -> "dict[str, Any]":
    """把配对结果适配成 PhysicalRSI 的 comparison/cohort/results 并调用上游选择器。

    ``scores[arm][i]`` / ``successes[arm][i]`` 对应 ``instances[i]``；
    ``evidence_files[arm][i]`` 是该实例的证据文件（逐件 sha 入 result）。
    适配失败（上游契约不满足）返回 ``BLOCKED`` + 原因，**不伪造**结论。
    """
    from orchestrator.rsi_bridge import selection as SEL

    # 触发 physicalrsi 0.3.0 的导入垫片（_physicalrsi）后取上游身份函数
    from PhysicalRSI_core.self_harness import selection as _upstream

    root = Path(evidence_root).resolve()
    try:
        layouts = {name: [_sha({"instance": name})] for name in instances}
        comparison = {
            "schema_version": 1,
            "round_id": "s2-first-controlled-improvement",
            "frozen_at": "2026-11-19T00:00:00+00:00",
            "parent_id": "v0",
            "candidates": {"v0": _sha({"arm": "v0"}),
                           "v1": _sha({"arm": "v1"})},
            "profile": {
                "infrastructure_retries": 0,
                "minimum_gain": float(minimum_gain),
                "tie_tolerance": 0.0,
                "tasks": {name: {"weight": 1, "episodes": 1,
                                 "score_range": [0.0, 1.0],
                                 "maximum_regression": 0.5}
                          for name in instances},
                "statistics": {"method": "paired-permutation", "alpha": 0.05,
                               "max_comparisons": 1, "resamples": 2000},
            },
        }
        comparison_sha = _upstream.comparison_identity(comparison)
        cohort = {"comparison_sha256": comparison_sha,
                  "generated_at": "2026-11-19T00:00:01+00:00",
                  "layouts": layouts}
        cohort_sha = _upstream.digest(cohort)
        results = []
        for arm in ("v0", "v1"):
            episodes = []
            for index, name in enumerate(instances):
                files = list(evidence_files.get(arm, [])[index]
                             if evidence_files else [])
                evidence = {str(Path(path).relative_to(root)): _sha_file(path)
                            for path in files if Path(path).is_file()}
                if not evidence:
                    # 无真实证据文件即 BLOCKED——绝不拿合成哈希喂上游选择器
                    # （评审 MEDIUM：那会产出无盘上依据的 inherit_child）。
                    return {"status": "UPSTREAM_ADAPTER_BLOCKED",
                            "detail": f"({arm}, {name}) 无证据文件可核",
                            "selection_identity": SEL.selection_identity()}
                episodes.append({
                    "task": name, "layout_sha256": layouts[name][0],
                    "state": "completed",
                    "success": bool(successes[arm][index]),
                    "score": float(scores[arm][index]),
                    "evidence_sha256": evidence,
                })
            results.append({
                "candidate_id": arm,
                "kind": "policy_evaluation",
                "native_exit_code": 0,
                "comparison_sha256": comparison_sha,
                "cohort_sha256": cohort_sha,
                "freeze_sha256": comparison["candidates"][arm],
                "evaluator_revision": "s2-review-v2",
                "episodes": episodes,
            })
        verdict = SEL.select_survivor(comparison, cohort, results,
                                      evidence_root=root)
    except Exception as exc:  # noqa: BLE001 - 适配失败如实记录，不伪造
        return {"status": "UPSTREAM_ADAPTER_BLOCKED",
                "detail": f"{type(exc).__name__}: {exc}",
                "selection_identity": SEL.selection_identity()}
    return {"status": "OK", "verdict": verdict,
            "selection_identity": SEL.selection_identity()}


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def analyze(*, instance_ids: Sequence[str],
            v0_scores: Sequence[float], v1_scores: Sequence[float],
            v0_success: Sequence[bool], v1_success: Sequence[bool],
            non_inferiority_report: Mapping[str, Any],
            upstream: Mapping[str, Any] | None = None,
            prediction_signature: Mapping[str, str] | None = None,
            dimension_deltas: Mapping[str, float] | None = None
            ) -> "dict[str, Any]":
    """双轴初判：主统计 + 非劣 + 预测签名兑现核对（矛盾即结论）。"""
    diffs = [float(b) - float(a) for a, b in zip(v0_scores, v1_scores)]
    table = permutation_table(instance_ids, diffs)
    checks: dict[str, Any] = {"non_inferiority": bool(non_inferiority_report.get("passed"))}
    if prediction_signature:
        deltas = dict(dimension_deltas or {})
        fulfilled = True
        details = {}
        for dim, sign in prediction_signature.items():
            delta = float(deltas.get(dim, 0.0))
            ok = ((sign == "+" and delta > 0) or (sign == "-" and delta < 0)
                  or (sign == "0" and abs(delta) < 1e-9))
            details[dim] = {"predicted": sign, "observed_delta": delta, "ok": ok}
            fulfilled = fulfilled and ok
        checks["prediction_fulfilled"] = fulfilled
        checks["prediction_detail"] = details
    else:
        checks["prediction_fulfilled"] = None
    statistical = table["decision"] == "inherit_child"
    verdict = ("FIRST_CONTROLLED_IMPROVEMENT_ESTABLISHED"
               if statistical and checks["non_inferiority"]
               and checks["prediction_fulfilled"] is not False
               else ("INCONCLUSIVE" if not statistical else "RETAIN_PARENT"))
    if checks["prediction_fulfilled"] is False:
        # 矛盾即结论：预测签名不兑现 → R2 不成立（即使主指标涨）
        verdict = "NOT_ESTABLISHED"
    return {"schema": f"{SCHEMA}.analysis", "statistics": table,
            "non_inferiority": dict(non_inferiority_report),
            "checks": checks, "upstream": dict(upstream or {}),
            "verdict": verdict,
            "note": ("统计初判 inherit_child 仅为初判；Gate2 accept 才晋升"
                     if statistical else "统计不过门 = retain_parent（初判）")}
