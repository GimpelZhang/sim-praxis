"""R1.3 独立判据：任务级线性对比 + 诊断复算 + 报告合同。

与 R1.2 判据的差别（Plan §4/§8.1）：

* 主估计量是**任务登记**的线性对比（``task.primary``，可含 2×2 交互），
  不是候选 plan 里的 pairwise 子对比；候选数值仍需与宿主从 trusted raw 的
  独立复算逐位一致；
* 诊断（``diagnostics`` 顶层块）由宿主从 raw 复算后逐项核对；
* 研究报告（``research_report``）做结构/来源/引用校验；
* 判据仍**不把宿主数值回灌候选**；报告只含 pass/fail 层与机器码。

锁内适配：候选分析产物 schema 保持 ``analysis_output.v1``（避免改写 R1.2
已冻结凭据格式），R1.3 诊断作为同文档的**顶层 typed 块** ``diagnostics``
提交；缺失即 ``DIAGNOSTICS_MISSING``，不静默跳过。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import design_intent as DI
from orchestrator.rsi_r1 import diagnostics as DG
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import task_specs as TS

JUDGE_SCHEMA = "simpraxis.rsi_r13.judge_report.v1"


def _block_values(per_run: Mapping[str, Mapping[str, Any]],
                  specs: Mapping[str, Any], metric: str
                  ) -> "tuple[dict[str, dict[str, float]], list[str]]":
    values: "dict[str, dict[str, float]]" = {}
    missing: "list[str]" = []
    for run_id, spec in specs.items():
        row = per_run.get(run_id)
        if row is None:
            missing.append(run_id)
            continue
        got = (row.get("values") or {}).get(metric)
        if got is None:
            missing.append(run_id)
            continue
        values.setdefault(spec.block_id, {})[spec.condition_id] = float(got)
    return values, missing


def judge_study_v13(*, task: TS.TaskSpecV13, plan: Any, manifest: Any,
                    artifacts: Mapping[str, Mapping[str, Any]],
                    specs: Mapping[str, Any],
                    submission: "Mapping[str, Any] | None",
                    report_doc: "Mapping[str, Any] | None",
                    receipt_ok: bool, receipt_reasons: Sequence[str],
                    output: "Mapping[str, Any] | None",
                    output_error: str = "",
                    candidate_origin: Mapping[str, Any] | None = None,
                    expected_reply_sha256: str = "",
                    evidence_files: "Mapping[str, bytes] | None" = None,
                    design_counts: "Mapping[str, int] | None" = None,
                    report_text: "str | None" = None
                    ) -> "dict[str, Any]":
    problems: "list[str]" = []
    checks: "list[dict[str, Any]]" = []

    def check(cid: str, ok: bool, detail: Any = "") -> None:
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    check("R13-RECEIPT", receipt_ok, list(receipt_reasons)[:8])
    if not receipt_ok:
        problems.append("INVALID_ANALYSIS")
    if output is None:
        problems.append(output_error or "INVALID_ANALYSIS")
        check("R13-OUTPUT", False, output_error)
        return _report(task, checks, problems, claim_status="MISSING")

    metric = task.primary.metric
    per_run = J.recompute_metrics(plan=plan, artifacts=artifacts, specs=specs)
    values, missing = _block_values(per_run, specs, metric)
    check("R13-RUN-SET", not missing, f"missing={missing[:5]}")
    if missing:
        problems.append("EVIDENCE_INVALID")
        return _report(task, checks, problems, claim_status="MISSING")

    reference = task.primary.estimate(values)
    reference["estimate"] = reference["effect"]
    numeric: "list[str]" = []
    cand = (output.get("contrasts") or {})
    got = cand.get(task.primary.id) if isinstance(cand, Mapping) else None
    if got is None:
        numeric.append("output.primary.missing")
    else:
        numeric.extend(AR.compare_contrast_identity(
            got, {"metric_id": metric, "proposition": task.primary.proposition,
                  "n_blocks": reference["n_blocks"]},
            where="output.primary"))
        numeric.extend(AR.compare_values(got, reference, where="output.primary"))
        if str(got.get("status")) != str(reference.get("claim")):
            numeric.append("output.primary.status")
    check("R13-NUMERICS", not numeric, numeric[:8])
    if numeric:
        problems.extend(numeric[:8])
        problems.append("INVALID_NUMERICS")

    # R1-N（有效阴性）：宿主用同一 raw 独立重算阴性对照，记录在判据件里；
    # 不参与 verdict（NC 是 r1 聚合条件，不是候选报告合格条件）。
    negative = None
    nc_spec = getattr(task, "negative_control_spec", None)
    if nc_spec:
        try:
            from orchestrator.rsi_r1 import contrasts as _X
            nc = _X.ContrastV13.from_dict(nc_spec["contrast"])
            nc_val = nc.estimate(values)
            negative = {"contrast_id": nc.id, "metric": nc.metric,
                        "proposition": nc.proposition, "delta": str(nc.delta),
                        "effect": nc_val.get("effect"),
                        "lower": nc_val.get("lower"),
                        "upper": nc_val.get("upper"),
                        "n_blocks": nc_val.get("n_blocks"),
                        "status": str(nc_val.get("claim")),
                        "source": "host_recomputed_from_raw"}
        except Exception as exc:  # noqa: BLE001 —— 记录失败原因，不冒充通过
            negative = {"contrast_id": str((nc_spec.get("contrast") or {}).get("id")),
                        "status": "UNVERIFIABLE",
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    diagnostics_ok = False
    diag = output.get("diagnostics")
    if diag is None:
        problems.append("DIAGNOSTICS_MISSING")
        check("R13-DIAGNOSTICS", False, "missing")
    else:
        try:
            DG.validate_diagnostics(
                diag, task=task, raw=values, expected_phase="confirm",
                expected_input_sha256=str(plan.sha256),
                expected_source_sha256=str((submission or {}).get(
                    "analysis_source_sha") or _analysis_sha(plan)),
                design_counts=design_counts)
            diagnostics_ok = True
            check("R13-DIAGNOSTICS", True, "recomputed")
        except DG.DiagnosticError as exc:
            problems.append(exc.code)
            check("R13-DIAGNOSTICS", False, f"{exc.code}: {exc.detail}")

    # 每个冻结假设都指向任务级主对比（R1.3 的 plan.primary 只是单因子组件；
    # 任务级交互由本判据从 raw 重算）。一条 claim 覆盖全部假设即可。
    hypotheses = {h.id: {"contrast_id": task.primary.id, "metric_id": metric,
                         "proposition": task.primary.proposition}
                  for h in getattr(plan, "hypotheses", ())} or {
        task.primary.id: {"contrast_id": task.primary.id,
                          "metric_id": metric,
                          "proposition": task.primary.proposition}}
    claim_problems = ([] if not submission else
                      AR.claim_consistency(submission.get("claims") or [],
                                           output, hypotheses=hypotheses))
    claim_status = "MATCH" if not claim_problems else "CLAIM_INVALID"
    check("R13-CLAIM", not claim_problems, claim_problems[:8])
    problems.extend(claim_problems[:8])

    report_ok = False
    if report_doc is None:
        problems.append("REPORT_MISSING")
        check("R13-REPORT", False, "missing")
    else:
        try:
            receipt = {"receipt_sha256": str((submission or {}).get(
                "analysis_receipt_sha") or "0" * 64)}
            RR.validate_report(
                report_doc, task=task, receipts={receipt["receipt_sha256"]:
                                                 receipt},
                evidence=evidence_files or {},
                candidate_origin=candidate_origin or {},
                expected_reply_sha256=expected_reply_sha256,
                report_text=report_text)
            report_ok = True
            check("R13-REPORT", True, "structure/source/refs ok")
        except RR.ReportError as exc:
            problems.append(exc.code)
            check("R13-REPORT", False, f"{exc.code}: {exc.detail}")

    verdict = "JUDGE_OK"
    if problems:
        verdict = ("EVIDENCE_INVALID" if "EVIDENCE_INVALID" in problems
                   else "INVALID_NUMERICS" if "INVALID_NUMERICS" in problems
                   else "CLAIM_INVALID" if claim_status != "MATCH"
                   else "INVALID_ANALYSIS")
    return _report(task, checks, problems, claim_status=claim_status,
                   verdict=verdict, diagnostics_ok=diagnostics_ok,
                   report_ok=report_ok,
                   extra={**({"negative_control": negative} if negative else {}),
                          "primary_status": str(reference.get("claim"))})


def _analysis_sha(plan: Any) -> str:
    return str(getattr(plan, "analysis_source_sha", "") or "")


def _report(task: TS.TaskSpecV13, checks: "list[dict[str, Any]]",
            problems: Sequence[str], *, claim_status: str,
            verdict: str = "JUDGE_REJECT", diagnostics_ok: bool = False,
            report_ok: bool = False,
            extra: "Mapping[str, Any] | None" = None) -> "dict[str, Any]":
    by_id = {c["id"]: bool(c["ok"]) for c in checks}
    layers = {
        "schema_accepted": by_id.get("R13-RECEIPT", False),
        "receipt_valid": by_id.get("R13-RECEIPT", False),
        "numerics_valid": by_id.get("R13-NUMERICS", False),
        "evidence_valid": by_id.get("R13-RUN-SET", False),
        "claim_status": claim_status,
        "diagnostics_valid": diagnostics_ok,
        "report_valid": report_ok,
    }
    doc = {
        "schema": JUDGE_SCHEMA,
        "schema_version": 1,
        "study_id": task.mechanism_id.split("_")[0],
        "task_id": task.task_id,
        "verdict": verdict,
        "layers": layers,
        "checks": checks,
        "reason_codes": list(dict.fromkeys(problems)),
    }
    if extra:
        # 2026-11-17 R1-N：阴性对照由宿主从 raw 独立重算，写进判据件
        # （不改 verdict/layers；旧轮判件不含此字段，逐字节不变）。
        doc.update(dict(extra))
    return doc
