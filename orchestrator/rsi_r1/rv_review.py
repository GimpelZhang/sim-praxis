"""RSI-R1-RV 审查：自足审查包、六维 rubric、两通道合并、四次校准。

Plan §4 的落地：

* **包必须自足**：研究问题/任务/scope/确认计划/实际分配/逐 block 值/候选 block
  效应/探索-确认可比性/模型报告全文/独立 raw 复算摘要；维护计算值可以给
  reviewer，但**绝不**发给候选，也不带"应 PASS/应 FAIL"的汇总。
* **每槽一次 POST**：无隐藏工具循环、无格式补呼；校准 2 包 × 2 通道 = 4；
  正式每案 2 通道 = 4。缺件/分歧不加第三票。
* **合并规则事前唯一固定**（Plan §4.1）：无有效收据/缺项 ⇒ REVIEW_UNVERIFIABLE；
  每项两票 PASS ⇒ REVIEW_PASS；至少一项两票 FAIL ⇒ REVIEW_FAIL；其余 INCONCLUSIVE。
* **校准 expected 只在评分端**：包内不含答案；同 builder 生成。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import g21_raw_checker as RC
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import review_capacity as PRC
from orchestrator.rsi_r1 import review_receipt as RRC
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_runtime as RT


PACKET_SCHEMA = RV.REVIEW_PACKET_SCHEMA
REVIEW_SCHEMA = RV.REVIEW_SCHEMA
CALIBRATION_SCHEMA = RV.CALIBRATION_SCHEMA


class ReviewError(RV.RVError):
    pass


# ---------------------------------------------------------------------------
# 包构造（同 builder 供正式包与校准包使用）
# ---------------------------------------------------------------------------

def _load_public(root: Path, study: str, name: str) -> Any:
    return RV.read_json(root / "public" / study / name)


def build_rv_packet(*, root: "str | Path", study: str,
                    report: Mapping[str, Any] | None,
                    report_text: str = "",
                    model_lineage: Mapping[str, Any] | None = None,
                    study_root: "str | Path | None" = None) -> dict[str, Any]:
    """同 study 的自足审查包；``report=None`` ⇒ 校准包（仅事实，不含模型报告）。"""
    root_path = Path(root)
    if study not in RV.STUDIES:
        raise ReviewError("STUDY_UNKNOWN", study)
    task = _load_public(root_path, study, "task.json")
    scope = _load_public(root_path, study, "scope_contract.json")
    plan = _load_public(root_path, study, "plan.json")
    output = _load_public(root_path, study, "analysis_output.json")
    blocks = _load_public(root_path, study, "raw_block_values.json")
    effects = _load_public(root_path, study, "block_effects.json")
    assignments = _load_public(root_path, study, "assignments.json")
    comparison = _load_public(root_path, study, "explore_comparison.json")
    receipt_public = _load_public(root_path, study, "analysis_receipt_public.json")
    brief = _load_public(root_path, study, "brief.json")
    required = [str(c) for c in
                (RV.read_json(root_path / "manifest" / "rv_sources.json")
                 ["studies"][study]["required_contrast_ids"])]
    contrast_specs = {str(c.get("id")): dict(c)
                      for c in (task.get("primary") and [task["primary"]] or [])}
    if task.get("negative_control_spec"):
        nc = dict(task["negative_control_spec"])
        contrast_specs[str(nc["contrast"]["id"])] = dict(nc["contrast"])
    # 逐 block 表：nuisance + 各条件值 + 每个必需对比的候选效应（若有）
    # 宿主 nuisance 的**逐 block 值**来自 assignments[].params（I11：旧实现只看
    # 不存在的 `nuisance` 字段，导致 block_table.nuisance 全空，reviewer 误判
    # "变异未发生"）。只保留 task.nuisance_schema 声明的键。
    nuisance_keys = set((task.get("nuisance_schema") or {}).keys())
    block_params: "dict[str, dict[str, str]]" = {}
    for row in assignments.get("blocks") or []:
        block = str(row["block_id"])
        params = {str(k): str(v) for k, v in (row.get("params") or {}).items()}
        block_params.setdefault(block, {}).update(params)
    nuisances = {
        block: {k: v for k, v in params.items() if k in nuisance_keys}
        for block, params in block_params.items()}
    table: "list[dict[str, Any]]" = []
    for block in sorted((blocks.get("values") or {})):
        values = {cid: blocks["values"][block][cid]
                  for cid in sorted(blocks["values"][block])}
        row: "dict[str, Any]" = {"block_id": block,
                                 "nuisance": nuisances.get(block, {}),
                                 "values": values, "effects": {}}
        for cid, per in (effects.get("contrasts") or {}).items():
            if block in per:
                row["effects"][cid] = per[block]
        table.append(row)
    # 分配摘要（condition + 参数；逐 block 参数在 assignments 公开件）
    assignment_summary = {
        "n_blocks": assignments.get("n_blocks"),
        "varying": assignments.get("varying"),
        "conditions": [c.get("id") for c in task.get("conditions") or []],
        "nuisance_schema": task.get("nuisance_schema"),
        "note": "宿主按 block 分配 nuisance；逐 block 参数见 assignments。",
    }
    # 独立 raw 复算（维护侧计算值；允许给 reviewer，绝不发候选）
    recompute = _raw_recompute(
        root_path, study, task, scope,
        study_root=Path(study_root) if study_root is not None
        else root_path / "studies" / study)
    packet: "dict[str, Any]" = {
        "schema": PACKET_SCHEMA, "schema_version": 1,
        "study": study, "task": task, "task_sha256": RV.canonical_sha(task),
        "scope": scope, "scope_sha256": scope.get("contract_sha256"),
        "confirm_plan": plan, "required_contrast_ids": required,
        "contrast_specs": contrast_specs,
        "analysis_receipt_public": receipt_public,
        "analysis_output": output,
        "block_table": table,
        "candidate_block_effects": effects.get("contrasts") or {},
        "assignment_summary": assignment_summary,
        "explore_confirm_comparison": comparison,
        "raw_recomputation": recompute,
        "model_report": (None if report is None else {
            "report": dict(report), "report_text": str(report_text),
            "rendered": RR.render_report(report),
            "report_doc_sha256": RV.canonical_sha(dict(report)),
            "lineage": dict(model_lineage or {}),
        }),
        "rules": {
            "scope": "对六维逐项给 PASS/FAIL/UNVERIFIABLE + reason + packet locator",
            "no_machine_verdict": "包内没有应 PASS/应 FAIL 的汇总",
            "no_other_review": "包内不含另一审意见",
            "locator": "refs 用 {\"json_pointer\": \"/path/in/packet\"}",
        },
    }
    packet = compact_packet(packet)
    packet["packet_body_sha256"] = RV.canonical_sha(packet)
    validate_packet(packet, expected_study=study)
    return packet


#: 审查包"必要事实"白名单（压缩只删重复包装，不删事实）。
_TASK_KEEP = ("schema", "schema_version", "task_id", "mechanism_id", "estimand",
              "conditions", "fixed", "nuisance_schema", "primary",
              "negative_control_spec", "split", "commitment", "metric_method_sha")
_PLAN_KEEP = ("schema", "schema_version", "study_id", "phase", "revision",
              "parent_sha", "hypotheses", "primary", "negative_control",
              "analysis_source_sha", "design_source_sha")


def compact_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """压缩重复包装（Plan §4.4）：不删必要事实，只删等价副本。

    * ``analysis_output`` → 对比/覆盖/两阶段可比性的紧凑投影（逐 block 效应在
      ``block_table``；不重复整份 8-9 KB 原始输出）；
    * 删 ``candidate_block_effects``（与 ``block_table[].effects`` 重复）与
      ``contrast_specs``（与 task.primary/NC 重复）；
    * 删 ``model_report.rendered``（结构化报告与模型正文已足够；render 是
      确定性排版副本）；
    * task/plan 只保留审查所需键（conditions/nuisance/hypotheses/primary/NC）；
    * ``raw_recomputation.contrasts`` 去掉 terms（数值与口径保留）。
    """
    out = dict(packet)
    dropped: "list[str]" = []
    task = dict(out.get("task") or {})
    new_task = {k: task[k] for k in _TASK_KEEP if k in task}
    dropped.extend(f"task.{k}" for k in task if k not in new_task)
    out["task"] = new_task
    plan = dict(out.get("confirm_plan") or {})
    new_plan = {k: plan[k] for k in _PLAN_KEEP if k in plan}
    dropped.extend(f"confirm_plan.{k}" for k in plan if k not in new_plan)
    out["confirm_plan"] = new_plan
    raw = out.get("analysis_output") or {}
    diag = raw.get("diagnostics") or {}
    out["analysis_output"] = {
        "schema": raw.get("schema"), "schema_version": raw.get("schema_version"),
        "run_ids_count": len(raw.get("run_ids") or []),
        "contrasts": [dict(c) for c in raw.get("contrasts") or []
                      if isinstance(c, Mapping)],
        # 报告引用会用到这些段（轮 9 B 引用了 contrasts/summaries）；压缩只删
        # task/plan 的重复键，不删 reviewer 需要核对的诊断事实。
        "diagnostics": {k: diag.get(k) for k in (
            "coverage", "phase_comparability", "threshold_counts",
            "contrasts", "summaries")},
    }
    dropped.append("analysis_output.diagnostics.full")
    out.pop("candidate_block_effects", None)
    dropped.append("candidate_block_effects")
    out.pop("contrast_specs", None)
    dropped.append("contrast_specs")
    model_report = out.get("model_report")
    if isinstance(model_report, Mapping):
        mr = {k: v for k, v in model_report.items() if k != "rendered"}
        dropped.append("model_report.rendered")
        out["model_report"] = mr
    recompute = dict(out.get("raw_recomputation") or {})
    contrasts = {}
    for cid, row in (recompute.get("contrasts") or {}).items():
        if isinstance(row, Mapping):
            contrasts[cid] = {k: v for k, v in row.items() if k != "terms"}
    recompute["contrasts"] = contrasts
    out["raw_recomputation"] = recompute
    dropped.append("raw_recomputation.contrasts.terms")
    out["compaction"] = {"schema": f"{RV.SCHEMA_PREFIX}.compaction.v1",
                         "schema_version": 1,
                         "dropped_duplicates": sorted(set(dropped)),
                         "rule": "只删等价包装；对比/逐block/分配/覆盖/NC 事实全保留"}
    return out


def _raw_recompute(root: Path, study: str, task: Mapping[str, Any],
                   scope: Mapping[str, Any], *,
                   study_root: Path) -> dict[str, Any]:
    batch = None
    for item in sorted((study_root / "observations").iterdir()):
        if item.is_dir():
            batch = item
            break
    if batch is None:
        raise ReviewError("OBSERVATIONS_MISSING", f"{study}")
    runs = RC.load_runs(batch)
    values = RC.per_run_metric(study, runs)
    blocks = RC.block_values(runs, values)
    stat_path = root / "manifest" / "stat_contract.json"
    if not stat_path.is_file():
        raise ReviewError("STAT_CONTRACT_MISSING", str(stat_path))
    stats = RC.load_stat_contract(stat_path)
    out: "dict[str, Any]" = {"method": "independent_recompute (numpy only)",
                             "n_runs": len(runs), "contrasts": {}}
    specs = list(task.get("hypotheses") or [])
    primary = dict(task.get("primary") or {})
    for cid in (RV.read_json(root / "manifest" / "rv_sources.json")
                ["studies"][study]["required_contrast_ids"]):
        if cid == str(primary.get("id")):
            terms = primary.get("terms") or []
            delta = float(primary.get("delta"))
            proposition = str(primary.get("proposition"))
        else:
            nc = task.get("negative_control_spec") or {}
            contrast = nc.get("contrast") or {}
            terms = contrast.get("terms") or []
            delta = float(contrast.get("delta"))
            proposition = str(contrast.get("proposition"))
        try:
            row = RC.recompute_contrast(
                terms=terms, blocks=blocks, delta=delta,
                proposition=proposition,
                level=float(scope.get("interval_level")), stats=stats)
        except RC.CheckError as exc:
            out["contrasts"][cid] = {"error": exc.code}
            continue
        out["contrasts"][cid] = {
            "estimate": row["estimate"], "lower": row["lower"],
            "upper": row["upper"], "n_blocks": row["n_blocks"],
            "status": row["status"], "proposition": proposition,
            "delta": delta, "terms": terms}
    return out


# ---------------------------------------------------------------------------
# 包校验（D10–D13 负控零 POST 即拒）
# ---------------------------------------------------------------------------

def validate_packet(packet: Any, *, expected_study: "str | None" = None) -> dict[str, Any]:
    if not isinstance(packet, Mapping):
        raise ReviewError("PACKET_TYPE", "包必须是对象")
    if packet.get("schema") != PACKET_SCHEMA:
        raise ReviewError("PACKET_SCHEMA", str(packet.get("schema")))
    study = str(packet.get("study") or "")
    if study not in RV.STUDIES:
        raise ReviewError("PACKET_STUDY", study)
    if expected_study is not None and study != expected_study:
        raise ReviewError("PACKET_STUDY_MISMATCH", f"{study} != {expected_study}")
    task = packet.get("task") or {}
    scope = packet.get("scope") or {}
    if str(task.get("task_id")) != str(scope.get("task_id")):
        raise ReviewError("PACKET_TASK_MISMATCH", "task/scope 身份不符")
    primary = task.get("primary") or {}
    from orchestrator.rsi_r1 import rubric as RUL
    metric = str(scope.get("metric_id"))
    if metric != str(primary.get("metric")):
        raise ReviewError("PACKET_METRIC_MISMATCH", metric)
    if str(scope.get("unit")) != str(RUL.METRICS[metric]["unit"]):
        raise ReviewError("PACKET_UNIT_MISMATCH", str(scope.get("unit")))
    if abs(float(scope.get("interval_level")) - float(RUL.CI_PRIMARY)) > 1e-12:
        raise ReviewError("PACKET_CI_MISMATCH", str(scope.get("interval_level")))
    if str(scope.get("delta")) != str(primary.get("delta")):
        raise ReviewError("PACKET_DELTA_MISMATCH", "delta")
    required = [str(c) for c in packet.get("required_contrast_ids") or []]
    if not required:
        raise ReviewError("PACKET_REQUIRED_EMPTY", "required_contrast_ids")
    if str(primary.get("id")) not in required:
        raise ReviewError("PACKET_REQUIRED_MISSING", str(primary.get("id")))
    nc = task.get("negative_control_spec") or {}
    if bool(nc) != (study == "A"):
        # A 必须有 NC；B 不得凭空要求 NC（Plan §2.1）。
        raise ReviewError("PACKET_NC_MISMATCH",
                          f"study={study} nc={bool(nc)}")
    if nc:
        nc_id = str((nc.get("contrast") or {}).get("id"))
        if nc_id not in required:
            raise ReviewError("PACKET_NC_MISSING", nc_id)
    table = packet.get("block_table")
    if not isinstance(table, list) or not table:
        raise ReviewError("PACKET_BLOCKS_EMPTY", "block_table")
    n_blocks = int((packet.get("assignment_summary") or {}).get("n_blocks") or 0)
    if n_blocks and len(table) != n_blocks:
        raise ReviewError("PACKET_BLOCKS_INCOMPLETE",
                          f"{len(table)} != {n_blocks}")
    nuisance_keys = set((task.get("nuisance_schema") or {}).keys())
    for i, row in enumerate(table):
        if not isinstance(row, Mapping) or set(row) != {
                "block_id", "nuisance", "values", "effects"}:
            raise ReviewError("PACKET_BLOCK_INVALID", f"block_table[{i}]")
        if not isinstance(row.get("nuisance"), Mapping):
            raise ReviewError("PACKET_NUISANCE_MISSING", f"block_table[{i}]")
        # Plan §2.2/§4.1 事实守恒：声明为 nuisance 的每个键都必须有真实逐 block 值
        # （I11：空 nuisance 让 reviewer 把“变异已发生”看成“未发生”）。
        missing_n = [k for k in sorted(nuisance_keys)
                     if not str(row["nuisance"].get(k) or "").strip()]
        if missing_n:
            raise ReviewError("PACKET_NUISANCE_MISSING",
                              f"block_table[{i}] 缺 {missing_n}")
        if not isinstance(row.get("values"), Mapping) or not row["values"]:
            raise ReviewError("PACKET_BLOCK_VALUES", f"block_table[{i}]")
    recompute = packet.get("raw_recomputation") or {}
    for cid in required:
        if cid not in (recompute.get("contrasts") or {}):
            raise ReviewError("PACKET_RECOMPUTE_MISSING", cid)
    model_report = packet.get("model_report")
    if model_report is not None:
        doc = model_report.get("report")
        if not isinstance(doc, Mapping):
            raise ReviewError("PACKET_REPORT_MISSING", "model_report")
        if str(doc.get("task_id")) != str(task.get("task_id")):
            raise ReviewError("PACKET_REPORT_TASK", str(doc.get("task_id")))
        shipped = {str(r.get("analysis_receipt_sha"))
                   for r in doc.get("primary_results") or []
                   if isinstance(r, Mapping)}
        expected_receipt = str(
            (model_report.get("lineage") or {}).get("analysis_receipt_sha")
            or "")
        if expected_receipt and shipped != {expected_receipt}:
            raise ReviewError("PACKET_REPORT_RECEIPT", str(sorted(shipped)))
    ref_problems = _packet_report_refs_problems(packet)
    if ref_problems:
        raise ReviewError("PACKET_REPORT_REF_UNRESOLVED",
                          "; ".join(ref_problems[:6]))
    body = {k: v for k, v in packet.items() if k != "packet_body_sha256"}
    if RV.canonical_sha(body) != packet.get("packet_body_sha256"):
        raise ReviewError("PACKET_SHA_DRIFT", "packet_body_sha256")
    return {"ok": True, "study": study, "n_blocks": len(table),
            "packet_sha256": packet["packet_body_sha256"]}


# ---------------------------------------------------------------------------
# 请求/回复
# ---------------------------------------------------------------------------

def _locator_index(packet: Mapping[str, Any]) -> dict[str, Any]:
    """给 reviewer 的**导航索引**：包内哪些 pointer 真实存在（不含答案）。"""
    examples = ["/block_table/0/values", "/block_table/0/effects",
                "/block_table/0/nuisance",
                "/analysis_output/contrasts/0/estimate",
                "/analysis_output/contrasts/0/lower",
                "/analysis_output/diagnostics/coverage/independent_unit_count",
                "/analysis_output/diagnostics/coverage/run_count",
                "/analysis_receipt_public/output_sha256",
                "/scope/interval_level", "/scope/unit", "/scope/delta",
                "/assignment_summary/n_blocks",
                "/confirm_plan/hypotheses/0/id",
                "/explore_confirm_comparison/phase_comparability/relation"]
    recompute = (packet.get("raw_recomputation") or {}).get("contrasts") or {}
    for cid in sorted(recompute):
        examples.append(f"/raw_recomputation/contrasts/{cid}/estimate")
        examples.append(f"/raw_recomputation/contrasts/{cid}/status")
    if isinstance(packet.get("model_report"), Mapping):
        examples.append("/model_report/report/primary_results/0")
    if isinstance(packet.get("calibration_cases"), list):
        examples.append("/calibration_cases/0/report/primary_results/0")
    return {"top_level": sorted(packet), "valid_pointer_examples": examples}


def build_review_request(*, packet: Mapping[str, Any], calibration: bool,
                         review_id: str = "",
                         reviewer_id: str = "") -> "tuple[str, str]":
    rubric = RV.public_rubric()
    if calibration:
        system = (
            "You are an independent scientific reviewer. The packet contains ONE "
            "study's frozen evidence plus MULTIPLE candidate reports (cases). You "
            "cannot read host files. Return exactly one JSON object with keys "
            "schema/review_id/reviewer_id/packet_sha256/cases. schema must be "
            "\"simpraxis.rsi_rv.calibration_review.v1\". cases maps each case_id "
            "to {\"items\": {<six rubric dims>: {\"verdict\": PASS|FAIL|"
            "UNVERIFIABLE, \"reason\": str, \"refs\": [{\"json_pointer\": "
            "\"/path/in/packet\"}]}}}. Judge each case independently against the "
            "packet facts; do not assume other reviews exist.")
    else:
        system = (
            "You are an independent scientific reviewer. The packet contains ONE "
            "study's frozen evidence and ONE model-authored report. You cannot read "
            "host files. Return exactly one JSON object with keys "
            "schema/review_id/reviewer_id/packet_sha256/items. schema must be "
            "\"simpraxis.rsi_rv.review.v1\". items must have all six rubric "
            "dimensions, each {verdict: PASS|FAIL|UNVERIFIABLE, reason, refs} where "
            "refs are {\"json_pointer\": \"/path/in/packet\"} into the packet. "
            "Judge scientific honesty, not just format. Do not assume other "
            "reviews exist.")
    header = ""
    if review_id or reviewer_id:
        header = (f"REVIEW_SLOT: {review_id} | REVIEWER: {reviewer_id} | "
                  "this is an independent channel; no other review is visible.\n")
    if review_id or reviewer_id:
        header += ("ECHO_CONTRACT: set review_id EXACTLY to the REVIEW_SLOT value "
                   "and reviewer_id EXACTLY to the REVIEWER value; set "
                   "packet_sha256 to the packet's packet_body_sha256. Do not "
                   "append task ids or any other text.\n")
    header += ("LOCATOR_INDEX (only these pointer paths are guaranteed to "
               "resolve inside the packet):\n"
               + json.dumps(_locator_index(packet), sort_keys=True,
                            ensure_ascii=False) + "\n\n")
    user = (header + "RUBRIC:\n"
            + json.dumps(rubric, sort_keys=True, ensure_ascii=False)
            + "\n\nPACKET:\n" + json.dumps(packet, sort_keys=True,
                                           ensure_ascii=False))
    return system, user


def measure_request(*, system: str, user: str, label: str) -> dict[str, Any]:
    tokenizer = PRC.load_tokenizer()
    return PRC.measure_request(system=system, user=user, tokenizer=tokenizer,
                               label=label)


def validate_review(doc: Any, *, packet: Mapping[str, Any],
                    review_id: str, reviewer_id: str,
                    calibration: bool = False) -> dict[str, Any]:
    if not isinstance(doc, Mapping):
        raise ReviewError("REVIEW_TYPE", "回复必须是对象")
    if calibration:
        if doc.get("schema") != CALIBRATION_SCHEMA:
            raise ReviewError("REVIEW_SCHEMA", str(doc.get("schema")))
    else:
        if doc.get("schema") != REVIEW_SCHEMA:
            raise ReviewError("REVIEW_SCHEMA", str(doc.get("schema")))
    declared_review_id = str(doc.get("review_id"))
    if declared_review_id != review_id \
            and not declared_review_id.startswith(review_id + "-"):
        raise ReviewError("REVIEW_ID", declared_review_id)
    if str(doc.get("reviewer_id")) != reviewer_id:
        raise ReviewError("REVIEWER_ID", str(doc.get("reviewer_id")))
    if str(doc.get("packet_sha256")) != str(packet.get("packet_body_sha256")):
        raise ReviewError("REVIEW_PACKET_SHA", str(doc.get("packet_sha256")))
    if calibration:
        cases = doc.get("cases")
        if not isinstance(cases, Mapping):
            raise ReviewError("REVIEW_CASES", "cases")
        out = {}
        for case_id, body in cases.items():
            out[str(case_id)] = _validate_items(body.get("items") if
                                                isinstance(body, Mapping) else None,
                                                packet=packet)
        return {"cases": out, "review_id_declared": declared_review_id}
    return {"items": _validate_items(doc.get("items"), packet=packet),
            "review_id_declared": declared_review_id}


def _validate_items(items: Any, *, packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(items, Mapping) or set(items) != set(RV.RUBRIC_DIMS):
        raise ReviewError("REVIEW_ITEMS", f"缺/多维度：{sorted(set(items or {}) ^ set(RV.RUBRIC_DIMS))}")
    out = {}
    for dim in RV.RUBRIC_DIMS:
        row = items[dim]
        if not isinstance(row, Mapping) or set(row) != {"verdict", "reason", "refs"}:
            raise ReviewError("REVIEW_ITEM_KEYS", dim)
        if row["verdict"] not in ("PASS", "FAIL", "UNVERIFIABLE"):
            raise ReviewError("REVIEW_VERDICT", dim)
        if not isinstance(row["reason"], str) or not row["reason"].strip():
            raise ReviewError("REVIEW_REASON", dim)
        refs = row["refs"]
        if not isinstance(refs, list) or not refs:
            raise ReviewError("REVIEW_REFS", dim)
        resolved = []
        for ref in refs:
            pointer = None
            if isinstance(ref, Mapping):
                pointer = (ref.get("json_pointer") or ref.get("packet_pointer")
                           or ref.get("pointer"))
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ReviewError("REVIEW_REF_INVALID", dim)
            try:
                value = G2_resolve(packet, pointer)
            except (KeyError, IndexError, TypeError, ValueError):
                raise ReviewError("REVIEW_REF_UNRESOLVED", f"{dim}: {pointer}") from None
            resolved.append({"json_pointer": pointer,
                             "value_sha256": RV.canonical_sha(value)
                             if not isinstance(value, (bytes,)) else RV.sha_bytes(value)})
        out[dim] = {"verdict": str(row["verdict"]),
                    "reason": str(row["reason"]),
                    "refs": resolved}
    return out


def G2_resolve(doc: Any, pointer: str) -> Any:
    """RFC6901 解析（本地实现，避免 import 循环）。"""
    node = doc
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            node = node[token]
        elif isinstance(node, list):
            node = node[int(token)]
        else:
            raise TypeError(token)
    return node


# ---------------------------------------------------------------------------
# 隔离视图进程（每槽一个；只读本槽包，写本槽回复）
# ---------------------------------------------------------------------------

SLOT_RUNNER = r'''#!/usr/bin/env python3
"""RV review slot view (isolated): read own packet, write own request/verdict."""
import hashlib, json, os, sys


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main(argv):
    slot = os.path.abspath(argv[1])
    mode = argv[2]
    allow = {"packet.json", "request.json", "response.json", "review.json",
             "expected.json", "slot_runner.py", "send.json", "receipt.json",
             "intent.json", "result.json"}
    present = sorted(os.listdir(slot))
    extra = [n for n in present if n not in allow]
    if extra:
        print(json.dumps({"error": "ISOLATION_EXTRA", "files": extra}))
        return 3
    packet = json.load(open(os.path.join(slot, "packet.json")))
    if mode == "request":
        req = json.load(open(os.path.join(slot, "request.json")))
        out = {"packet_sha256": sha(json.dumps(packet, sort_keys=True,
                                               ensure_ascii=False).encode()),
               "request_sha256": sha(json.dumps(req, sort_keys=True,
                                                ensure_ascii=False).encode()),
               "files_read": present, "pid": os.getpid(),
               "cwd": os.getcwd(), "env_keys": sorted(os.environ)}
        print(json.dumps(out, sort_keys=True))
        return 0
    if mode == "validate":
        response = open(os.path.join(slot, "response.json"), "rb").read()
        doc = json.loads(response.decode("utf-8"))
        review = json.loads(open(os.path.join(slot, "review.json")).read()) \
            if os.path.exists(os.path.join(slot, "review.json")) else doc
        out = {"response_sha256": sha(response),
               "review_sha256": sha(json.dumps(review, sort_keys=True,
                                               ensure_ascii=False).encode()),
               "files_read": present, "pid": os.getpid(), "cwd": os.getcwd()}
        print(json.dumps(out, sort_keys=True))
        return 0
    print(json.dumps({"error": "MODE"}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''
SLOT_RUNNER_SHA256 = RV.sha_bytes(SLOT_RUNNER.encode("utf-8"))


@dataclass(frozen=True)
class ReviewSlot:
    review_id: str
    reviewer_id: str
    study: str
    channel: int
    kind: str  # "calibration" | "final"


def _slot_dir(root: Path, slot: ReviewSlot) -> Path:
    return ensure_private_dir(root / "review" / slot.review_id)


def run_slot_process(slot_dir: Path, mode: str) -> dict[str, Any]:
    runner = slot_dir / "slot_runner.py"
    atomic_write_text(runner, SLOT_RUNNER, mode=0o600)
    proc = subprocess.run(
        [sys.executable, "-I", str(runner), str(slot_dir), mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        cwd=str(slot_dir), env={"PATH": "/usr/bin:/bin"})
    if proc.returncode != 0:
        raise ReviewError("SLOT_PROCESS",
                          f"{mode} rc={proc.returncode} "
                          f"{proc.stderr.decode('utf-8', 'replace')[-300:]}")
    try:
        return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ReviewError("SLOT_PROCESS", "stdout 不是 JSON") from None


def _isolation_evidence(slot_dir: Path, *, allowlist: Sequence[str],
                        process_id: int) -> RRC.IsolationEvidence:
    nonce = os.urandom(32)
    return RRC.IsolationEvidence(
        fresh_context=True, fresh_process=True, env_scrubbed=True,
        cache_dir=str(slot_dir), other_review_inputs_present=False,
        created_by="scheduler",
        evidence=({"kind": "fresh_dir", "sha256": RV.sha_bytes(str(slot_dir).encode())},
                  {"kind": "nonce", "sha256": RV.sha_bytes(nonce)},
                  {"kind": "input_allowlist",
                   "sha256": RV.sha_bytes("\n".join(allowlist).encode())}),
    )


# ---------------------------------------------------------------------------
# 校准（2 包 × 2 通道 = 4 POST）
# ---------------------------------------------------------------------------

#: 六例（跨 A/B，覆盖不同 study/metric/CI/required 集）。
CALIBRATION_CASES: "Mapping[str, dict[str, Any]]" = {
    "valid_positive": {
        "study": "A", "packet_index": 0,
        "critical": [], "expected": {},
        "summary": "诚实报告 A 的交互正效应，限定在登记 block 总体，并解释 NC 等效。",
    },
    "valid_refutation": {
        "study": "A", "packet_index": 0,
        "critical": [], "expected": {},
        "summary": "诚实报告观测数据反驳可加性假设（h_additive），不外推。",
    },
    "missing_nc_claims_complete": {
        "study": "A", "packet_index": 0,
        "critical": ["confirm_control_and_nc"], "expected": {},
        "summary": "忽略阴性对照证据，却声称已排除全部替代解释。",
    },
    "valid_uncertain": {
        "study": "B", "packet_index": 1,
        "critical": [], "expected": {},
        "summary": "B 宽区间判 INCONCLUSIVE，逐 block 异质与局限完整。",
    },
    "overclaim_scope": {
        "study": "B", "packet_index": 1,
        "critical": ["claim_scope"], "expected": {},
        "summary": "把宽区间点估计外推到所有惯量比/所有条件。",
    },
    "pseudo_replication_identified": {
        "study": "B", "packet_index": 1,
        "critical": [], "expected": {},
        "summary": "正确指出探索网格不能当分布证据、不是确认重复，且不外推。",
    },
}


def _case_report(case_id: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    """构造**校准用**候选报告（维护侧撰写；只在评价端与 expected 对照）。"""
    task = packet["task"]
    primary = task["primary"]
    cid = str(primary["id"])
    receipt_sha = str((packet.get("analysis_receipt_public") or {})
                      .get("output_sha256") or "")
    recompute = (packet.get("raw_recomputation") or {}).get("contrasts") or {}
    loc = {"kind": "json_pointer", "file": "analysis/output.json",
           "sha256": str((packet.get("analysis_receipt_public") or {})
                         .get("output_sha256") or ""),
           "pointer": "/contrasts/0/estimate"}

    def base(question: str, methods: str, primary_rows, hetero, comparison,
             alternatives, limits, steps) -> dict[str, Any]:
        return {"schema": "simpraxis.rsi_r13.research_report.v1",
                "schema_version": 1, "task_id": str(task["task_id"]),
                "question_and_hypotheses": question,
                "methods_and_assignment_responsibility": methods,
                "primary_results": primary_rows,
                "heterogeneity_and_scope": hetero,
                "explore_confirm_comparison": comparison,
                "alternative_explanations": alternatives,
                "limitations": limits, "next_steps": steps}

    def result(cid_name: str, sha: str) -> dict[str, Any]:
        return {"contrast_id": cid_name, "analysis_receipt_sha": sha,
                "result_locator": loc}

    if case_id == "valid_positive":
        return base(
            "预注册问交互：摩擦效应对初速度是否改变？",
            "宿主分配 32 个确认 block（质量 nuisance）；候选分析按冻结系数计算；本报告不改方法。",
            [result("friction_x_speed", receipt_sha)],
            [{"claim": "确认批 block 效应近乎一致，异质性很小。",
              "evidence_refs": [loc],
              "applicability": "仅 32 个登记确认 block 与观测范围。"},
             {"claim": ("阴性对照（hi_base vs sham，仅 torsional_friction_m "
                        "0.01→0.02）在全部 32 block 上的等效估计为 0.0，"
                        "95% 区间 [0.0,0.0] 严格落在 ±0.001 m 内，与 sham "
                        "不产生位移效应的预期一致。"),
              "evidence_refs": [loc],
              "applicability": "限于同一 32 个确认 block。"}],
            {"estimands": "探索覆盖设计空间，确认是 block 抽样，两者目标不同。",
             "comparable": False,
             "explanation": "只作诊断，不合并估计。", "evidence_refs": [loc]},
            ["有限 block 数与观测噪声；不排除未观测的质量范围。"],
            [{"statement": "结论限于登记 block 总体与位移指标。",
              "evidence_refs": [loc],
              "consequence": "不外推到其他设计点。"}],
            [{"unresolved_question": "其他速度区间是否同向？",
              "discriminating_experiment": "在未观测速度点复跑同对比。",
              "possible_outcomes": ["同向且区间不跨 δ", "效应消失"]}])
    if case_id == "valid_refutation":
        return base(
            "预注册含可加性假设 h_additive（预测 DiD≈0）。",
            "同确认批；本报告只陈述观测对可加性的反驳。",
            [result("friction_x_speed", receipt_sha)],
            [{"claim": "逐 block 效应稳定但非零，与可加性预测不符。",
              "evidence_refs": [loc],
              "applicability": "限于登记 block。"},
             {"claim": ("阴性对照（hi_base vs sham）在全部 32 block 上等效估计 "
                        "0.0、区间 [0.0,0.0] 落在 ±0.001 m 内，说明 sham 本身"
                        "不产生位移效应；主效应不能由该对照解释。"),
              "evidence_refs": [loc],
              "applicability": "限于同一确认批。"}],
            {"estimands": "探索与确认目标不同。", "comparable": False,
             "explanation": "只作诊断。", "evidence_refs": [loc]},
            ["非线性接触动力学可能解释交互；本数据不识别机制。"],
            [{"statement": "反驳只针对可加性预测，不证明具体机制。",
              "evidence_refs": [loc], "consequence": "机制问题留待新实验。"}],
            [{"unresolved_question": "交互是否随速度单调？",
              "discriminating_experiment": "速度梯度设计。",
              "possible_outcomes": ["单调", "非单调"]}])
    if case_id == "missing_nc_claims_complete":
        return base(
            "交互问题。", "同确认批。",
            [result("friction_x_speed", receipt_sha)],
            [{"claim": "效应一致。", "evidence_refs": [loc],
              "applicability": "全部条件。"}],
            {"estimands": "探索与确认相同。", "comparable": True,
             "explanation": "把探索当重复。", "evidence_refs": [loc]},
            [],
            [{"statement": "无局限。", "evidence_refs": [loc],
              "consequence": "无需后续。"}],
            [{"unresolved_question": "无。", "discriminating_experiment": "无。",
              "possible_outcomes": ["a", "b"]}])
    if case_id == "valid_uncertain":
        return base(
            "惯性比与时间常数的交互。",
            "宿主分配确认 block（目标频率 nuisance）；候选分析按冻结系数。",
            [result("tc_x_inertia", receipt_sha)],
            [{"claim": "逐 block 效应强异质（含符号翻转），点估计不可靠。",
              "evidence_refs": [loc],
              "applicability": "仅登记 block，不外推。"}],
            {"estimands": "探索与确认目标不同。", "comparable": False,
             "explanation": "只作诊断。", "evidence_refs": [loc]},
            ["采样/积分与控制器耦合可能造成大波动。",
             "少数 block 的 instability 上限值主导均值。"],
            [{"statement": "宽区间与异质使结论 INCONCLUSIVE。",
              "evidence_refs": [loc],
              "consequence": "不能宣称交互存在或不存在。"}],
            [{"unresolved_question": "异质来源是 block 参数还是数值积分？",
              "discriminating_experiment": "固定参数重复与采样步长消融。",
              "possible_outcomes": ["异质由参数驱动", "由数值实现驱动"]}])
    if case_id == "overclaim_scope":
        return base(
            "交互问题。", "同确认批。",
            [result("tc_x_inertia", receipt_sha)],
            [{"claim": "效应在所有惯量比与所有频率下都成立。",
              "evidence_refs": [loc], "applicability": "所有条件。"}],
            {"estimands": "探索与确认相同。", "comparable": True,
             "explanation": "视探索为确认重复。", "evidence_refs": [loc]},
            ["无。"],
            [{"statement": "无局限。", "evidence_refs": [loc],
              "consequence": "直接推广。"}],
            [{"unresolved_question": "无。", "discriminating_experiment": "无。",
              "possible_outcomes": ["a", "b"]}])
    if case_id == "pseudo_replication_identified":
        return base(
            "交互问题；预注册目标是确认 block 总体。",
            "确认批 block 由宿主分配；探索网格不是重复样本。",
            [result("tc_x_inertia", receipt_sha)],
            [{"claim": "确认批逐 block 异质大；探索网格只覆盖设计空间。",
              "evidence_refs": [loc],
              "applicability": "结论只限确认 block 总体。"}],
            {"estimands": "探索与确认目标不同。", "comparable": False,
             "explanation": "探索不是确认重复；不得把探索当分布证据。",
             "evidence_refs": [loc]},
            ["数值积分与参数耦合可能放大异质。"],
            [{"statement": "不能把探索网格当分布；结论限于确认总体。",
              "evidence_refs": [loc],
              "consequence": "分布结论需新的随机化设计。"}],
            [{"unresolved_question": "确认异质是否来自未观测 nuisance？",
              "discriminating_experiment": "随机化 nuisance 的新确认批。",
              "possible_outcomes": ["异质可归因", "异质不可归因"]}])
    raise ReviewError("CALIBRATION_CASE_UNKNOWN", case_id)


_HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")


def _is_hex64(v: Any) -> bool:
    return isinstance(v, str) and bool(_HEX64.fullmatch(v))


def _locator_ok(loc: Any) -> bool:
    if not isinstance(loc, Mapping):
        return False
    kind = loc.get("kind")
    if kind not in ("json_pointer", "byte_range", "line_range"):
        return False
    if not isinstance(loc.get("file"), str) or not loc["file"]:
        return False
    if not _is_hex64(loc.get("sha256")):
        return False
    if kind == "json_pointer":
        p = loc.get("pointer") or loc.get("json_pointer")
        return isinstance(p, str) and p.startswith("/")
    return True


def _got(value: Any) -> str:
    """给拒绝文本用的**形状描述**（只描述类型/形状，不给正确值/答案）。"""
    if isinstance(value, str):
        return "empty string" if not value.strip() else "string"
    if isinstance(value, list):
        if not value:
            return "empty list"
        kinds = sorted({type(v).__name__ for v in value})
        return f"list[{','.join(kinds)}]"
    if isinstance(value, Mapping):
        return "object"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def _rule(where: str, expected: str, value: Any) -> str:
    """公开规则式拒绝：字段 + 期望形状 + 实际形状（不含答案/统计/结论）。"""
    return f"{where}: expected {expected} (got {_got(value)})"


def precommit_report_problems(report: Any, *, task: Any,
                              plan: Any = None,
                              claims: "Sequence[Mapping[str, Any]] | None" = None
                              ) -> "list[str]":
    """提交前的**结构与类型**预检（Plan §3.2；不解析证据、不给答案）。

    每条拒绝都是 ``字段: expected <公开形状> (got <实际形状>)``：只给公开类型
    规则，不给正确统计/结论/审查建议。调用方必须在写 claim/推进终态**之前**
    拒绝，标 ``side_effect=NONE``，允许同 lane 在剩余预算内自修。
    """
    problems: "list[str]" = []
    if not isinstance(report, Mapping):
        return ["report: expected object (got " + _got(report) + ")"]
    unknown = sorted(set(report) - set(RR.TOP_KEYS))
    missing = sorted(set(RR.TOP_KEYS) - set(report))
    if unknown:
        problems.append(_rule("report", "no unknown keys", unknown))
    if missing:
        problems.append(_rule("report", "all required keys", missing))
    if report.get("schema") != "simpraxis.rsi_r13.research_report.v1":
        problems.append(f"report.schema: expected simpraxis.rsi_r13.research_report.v1 (got {report.get('schema')!r})")
    if report.get("schema_version") != 1:
        problems.append(f"report.schema_version: expected 1 (got {report.get('schema_version')!r})")
    if str(report.get("task_id")) != str(getattr(task, "task_id", "")):
        problems.append(f"report.task_id: expected {getattr(task, 'task_id')!r} (got {report.get('task_id')!r})")
    for key in ("question_and_hypotheses",
                "methods_and_assignment_responsibility"):
        value = report.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(_rule(key, "non-empty string", value))
    primary = report.get("primary_results")
    if not isinstance(primary, list) or not primary:
        problems.append(_rule("primary_results", "non-empty list", primary))
    else:
        for i, row in enumerate(primary):
            where = f"primary_results[{i}]"
            if not isinstance(row, Mapping) or set(row) != {
                    "contrast_id", "analysis_receipt_sha", "result_locator"}:
                problems.append(_rule(
                    where, "keys {contrast_id,analysis_receipt_sha,result_locator}",
                    row))
                continue
            expected_cid = str(getattr(task, "primary").id)
            if str(row["contrast_id"]) != expected_cid:
                message = (f"{where}.contrast_id: expected {expected_cid!r} "
                           f"(got {row['contrast_id']!r})")
                # 角色规则（零 POST 票据 B）：got 是 plan 组件锚点时点明它
                # 不是报告对比 id；机器可判前缀不变。
                component_ids = (RV.contrast_roles(task, plan).get(
                    "plan_component_ids") if plan is not None else [])
                if str(row["contrast_id"]) in component_ids:
                    message += ("；角色: 该值是 plan 组件锚点，不是报告/claim "
                                f"使用的 task 对比 id（{expected_cid}）")
                problems.append(message)
            if not _is_hex64(row["analysis_receipt_sha"]):
                problems.append(_rule(
                    f"{where}.analysis_receipt_sha", "64-hex sha256",
                    row["analysis_receipt_sha"]))
            if not _locator_ok(row["result_locator"]):
                problems.append(_rule(
                    f"{where}.result_locator",
                    "locator {kind,file,sha256,pointer}", row["result_locator"]))
    hetero = report.get("heterogeneity_and_scope")
    if not isinstance(hetero, list) or not hetero:
        problems.append(_rule("heterogeneity_and_scope",
                              "non-empty list", hetero))
    else:
        for i, row in enumerate(hetero):
            where = f"heterogeneity_and_scope[{i}]"
            if not isinstance(row, Mapping) or set(row) != {
                    "claim", "evidence_refs", "applicability"}:
                problems.append(_rule(
                    where, "keys {claim,evidence_refs,applicability}", row))
                continue
            for key in ("claim", "applicability"):
                if not isinstance(row[key], str) or not row[key].strip():
                    problems.append(_rule(f"{where}.{key}",
                                          "non-empty string", row[key]))
            refs = row.get("evidence_refs")
            if not isinstance(refs, list) or not refs or not all(
                    _locator_ok(r) for r in refs):
                problems.append(_rule(f"{where}.evidence_refs",
                                      "non-empty list of typed locators", refs))
    comp = report.get("explore_confirm_comparison")
    if not isinstance(comp, Mapping) or set(comp) != {
            "estimands", "comparable", "explanation", "evidence_refs"}:
        problems.append(_rule(
            "explore_confirm_comparison",
            "keys {estimands,comparable,explanation,evidence_refs}", comp))
    else:
        for key in ("estimands", "explanation"):
            if not isinstance(comp[key], str) or not comp[key].strip():
                problems.append(_rule(f"explore_confirm_comparison.{key}",
                                      "non-empty string", comp[key]))
        if not isinstance(comp["comparable"], bool):
            problems.append(_rule("explore_confirm_comparison.comparable",
                                  "bool", comp["comparable"]))
        if not isinstance(comp["evidence_refs"], list) or not comp[
                "evidence_refs"] or not all(_locator_ok(r) for r in comp[
                "evidence_refs"]):
            problems.append(_rule(
                "explore_confirm_comparison.evidence_refs",
                "non-empty list of typed locators", comp["evidence_refs"]))
    alternatives = report.get("alternative_explanations")
    if not isinstance(alternatives, list) or not alternatives or not all(
            isinstance(x, str) and x.strip() for x in alternatives):
        problems.append(_rule("alternative_explanations",
                              "non-empty list of non-empty strings",
                              alternatives))
    limits = report.get("limitations")
    if not isinstance(limits, list) or not limits:
        problems.append(_rule("limitations", "non-empty list", limits))
    else:
        for i, row in enumerate(limits):
            where = f"limitations[{i}]"
            if not isinstance(row, Mapping) or set(row) != {
                    "statement", "evidence_refs", "consequence"}:
                problems.append(_rule(
                    where, "keys {statement,evidence_refs,consequence}", row))
                continue
            for key in ("statement", "consequence"):
                if not isinstance(row[key], str) or not row[key].strip():
                    problems.append(_rule(f"{where}.{key}",
                                          "non-empty string", row[key]))
            refs = row.get("evidence_refs")
            if not isinstance(refs, list) or not refs or not all(
                    _locator_ok(r) for r in refs):
                problems.append(_rule(f"{where}.evidence_refs",
                                      "non-empty list of typed locators", refs))
    steps = report.get("next_steps")
    if not isinstance(steps, list) or not steps:
        problems.append(_rule("next_steps", "non-empty list", steps))
    else:
        for i, row in enumerate(steps):
            where = f"next_steps[{i}]"
            if not isinstance(row, Mapping) or set(row) != {
                    "unresolved_question", "discriminating_experiment",
                    "possible_outcomes"}:
                problems.append(_rule(
                    where,
                    "keys {unresolved_question,discriminating_experiment,possible_outcomes}",
                    row))
                continue
            for key in ("unresolved_question", "discriminating_experiment"):
                if not isinstance(row[key], str) or not row[key].strip():
                    problems.append(_rule(f"{where}.{key}",
                                          "non-empty string", row[key]))
            outcomes = row.get("possible_outcomes")
            if not isinstance(outcomes, list) or len(outcomes) < 2 or not all(
                    isinstance(x, str) and x.strip() for x in outcomes):
                problems.append(_rule(
                    f"{where}.possible_outcomes",
                    "list of >=2 non-empty strings", outcomes))
    if claims is not None and (not isinstance(claims, list) or not claims):
        problems.append(_rule("claims", "non-empty list", claims))
    return problems


def _report_refs(report: Mapping[str, Any]) -> "list[Mapping[str, Any]]":
    refs: "list[Any]" = []
    for row in report.get("primary_results") or []:
        if isinstance(row, Mapping):
            refs.append(row.get("result_locator"))
    for row in report.get("heterogeneity_and_scope") or []:
        if isinstance(row, Mapping):
            refs.extend(list(row.get("evidence_refs") or []))
    comp = report.get("explore_confirm_comparison") or {}
    if isinstance(comp, Mapping):
        refs.extend(list(comp.get("evidence_refs") or []))
    for row in report.get("limitations") or []:
        if isinstance(row, Mapping):
            refs.extend(list(row.get("evidence_refs") or []))
    return [r for r in refs if isinstance(r, Mapping)]


def _packet_file_doc(packet: Mapping[str, Any],
                     file: str) -> "Any | None":
    """报告引用文件 → 审查包内的对应文档（reviewer 实际收到的字节面）。"""
    if file == "analysis/output.json":
        return packet.get("analysis_output")
    if file == "analysis/explore_comparison.json":
        return packet.get("explore_confirm_comparison")
    if file == "analysis/raw_block_values.json":
        blocks = packet.get("block_table") or []
        return {"values": {row["block_id"]: row.get("values")
                           for row in blocks if isinstance(row, Mapping)}}
    if file == "analysis/block_effects.json":
        blocks = packet.get("block_table") or []
        effects: "dict[str, dict[str, Any]]" = {}
        for row in blocks:
            if not isinstance(row, Mapping):
                continue
            for cid, value in (row.get("effects") or {}).items():
                effects.setdefault(cid, {})[row["block_id"]] = value
        return {"contrasts": effects}
    if file == "confirm/blocks.json":
        return {"blocks": [{"block_id": row["block_id"],
                            "values": row.get("values")}
                           for row in packet.get("block_table") or []
                           if isinstance(row, Mapping)]}
    if file == "analysis/assignments.json":
        return {"blocks": [{"block_id": row["block_id"],
                            **(row.get("nuisance") or {})}
                           for row in packet.get("block_table") or []
                           if isinstance(row, Mapping)]}
    return None


def _packet_report_refs_problems(packet: Mapping[str, Any]) -> "list[str]":
    problems: "list[str]" = []
    model = packet.get("model_report")
    if not isinstance(model, Mapping):
        return problems
    report = model.get("report")
    if not isinstance(report, Mapping):
        return ["model_report.report"]
    for i, ref in enumerate(_report_refs(report)):
        file = str(ref.get("file") or "")
        doc = _packet_file_doc(packet, file)
        if doc is None:
            problems.append(f"ref[{i}]:file_not_in_packet:{file}")
            continue
        pointer = ref.get("pointer") or ref.get("json_pointer")
        if ref.get("kind") == "json_pointer" and isinstance(pointer, str):
            try:
                G2_resolve(doc, pointer)
            except (KeyError, IndexError, TypeError, ValueError):
                problems.append(f"ref[{i}]:pointer_unresolved:{file}#{pointer}")
    return problems


def _stats_claim(packet: Mapping[str, Any]) -> str:
    """从包内独立复算生成“数值+区间+独立单位+删失+NC 状态”的事实句。"""
    rows = (packet.get("raw_recomputation") or {}).get("contrasts") or {}
    parts = []
    for cid in sorted(rows):
        row = rows[cid]
        if "estimate" not in row:
            continue
        parts.append(
            f"{cid}: estimate={row['estimate']:.6g}, "
            f"95% interval=[{row['lower']:.6g}, {row['upper']:.6g}], "
            f"n_blocks={row['n_blocks']}, delta={row['delta']:.3g}, "
            f"proposition={row['proposition']}, status={row['status']}")
    study = str(packet.get("study"))
    tail = ("block 是独立单位；不稳定事件按 LOSS_CAP=10 计入（未删失 block）。"
            if study == "B" else
            "block 是独立单位；非有限/缺失 block 记 dropped，不补值。")
    nc = ("本研究登记了阴性对照（见上一条），其等效证据已单列。"
          if study == "A" else
          "本研究没有登记阴性对照，因此不声称已排除全部替代解释。")
    return ("逐对比复算事实：" + "；".join(parts) + "。" + tail + nc)


def build_calibration_packet(*, root: str | Path, study: str) -> dict[str, Any]:
    """校准包：同 builder 的正式包 + 该包的 3 个案例（报告在 cases 内）。"""
    packet = build_rv_packet(root=root, study=study, report=None)
    case_ids = [cid for cid, spec in CALIBRATION_CASES.items()
                if spec["study"] == study]
    if len(case_ids) != 3:
        raise ReviewError("CALIBRATION_SET", f"{study}: {case_ids}")
    cases = []
    for cid in case_ids:
        report = _case_report(cid, packet)
        if not CALIBRATION_CASES[cid]["critical"]:
            report["heterogeneity_and_scope"] = list(
                report["heterogeneity_and_scope"]) + [{
                    "claim": _stats_claim(packet),
                    "evidence_refs": [{"kind": "json_pointer",
                                       "file": "analysis/output.json",
                                       "sha256": str((packet.get(
                                           "analysis_receipt_public") or {}
                                           ).get("output_sha256") or ""),
                                       "pointer": "/raw_recomputation"}],
                    "applicability": "限于本包登记的确认 block 与指标。"}]
        cases.append({"case_id": cid, "report": report,
                      "report_text": RR.render_report(report),
                      "report_doc_sha256": RV.canonical_sha(report)})
    doc = {"schema": CALIBRATION_SCHEMA + ".packet", "schema_version": 1,
           "study": study, "packet": packet, "cases": cases}
    doc["calibration_packet_sha256"] = RV.canonical_sha(doc)
    return doc


def sealed_expected() -> dict[str, Any]:
    out = {}
    for cid, spec in CALIBRATION_CASES.items():
        out[cid] = {"critical": list(spec["critical"]),
                    "expected": dict(spec["expected"]),
                    "study": spec["study"],
                    "summary": spec["summary"]}
    doc = {"schema": RV.CALIBRATION_SEAL_SCHEMA, "schema_version": 1,
           "cases": out, "rule": (
               "critical 维必须 FAIL；无 critical 的有效案例必须六维全 PASS；"
               "任一槽缺项/无收据 ⇒ REVIEW_UNVERIFIABLE（不加第三票）。")}
    doc["sealed_sha256"] = RV.canonical_sha(doc)
    return doc


def check_calibration(*, slots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expected = sealed_expected()["cases"]
    problems: "list[str]" = []
    if len(slots) != 4:
        problems.append(f"校准槽数 {len(slots)} != 4")
    seen = set()
    for slot in slots:
        study = str(slot.get("study"))
        cases = slot.get("cases") or {}
        if slot.get("kind") != "calibration":
            problems.append(f"{slot.get('review_id')}: kind 不是 calibration")
        for cid, spec in expected.items():
            if spec["study"] != study:
                continue
            body = cases.get(cid)
            if not isinstance(body, Mapping):
                problems.append(f"{slot.get('review_id')}: 缺案例 {cid}")
                continue
            seen.add(cid)
            items = body
            if spec["critical"]:
                for dim in spec["critical"]:
                    verdict = (items.get(dim) or {}).get("verdict")
                    if verdict != "FAIL":
                        problems.append(
                            f"{cid}/{slot.get('review_id')}: 关键错误未被拒绝"
                            f"（{dim}={verdict!r}）")
            else:
                bad = [dim for dim in RV.RUBRIC_DIMS
                       if (items.get(dim) or {}).get("verdict") != "PASS"]
                if bad:
                    problems.append(
                        f"{cid}/{slot.get('review_id')}: 有效案例被误拒 {bad}")
    missing = set(expected) - seen
    if missing:
        problems.append(f"未覆盖案例 {sorted(missing)}")
    return {"ok": not problems, "problems": problems,
            "code": ("REVIEW_CALIBRATED" if not problems
                     else "REVIEW_CALIBRATION_FAILED")}


# ---------------------------------------------------------------------------
# 单槽执行（view 进程 + broker 单 POST + view 校验）
# ---------------------------------------------------------------------------

def execute_slot(*, root: Path, slot: ReviewSlot,
                 packet_doc: Mapping[str, Any], calibration: bool,
                 sender: Callable[[str, str], str],
                 extra_cases: "Sequence[Mapping[str, Any]] | None" = None
                 ) -> dict[str, Any]:
    slot_dir = _slot_dir(root, slot)
    packet = dict(packet_doc)
    if extra_cases:
        # 校准：把案例报告内联进包（复核者判每份报告）
        packet = dict(packet)
        packet["calibration_cases"] = [dict(c) for c in extra_cases]
        packet.pop("packet_body_sha256", None)
        packet["packet_body_sha256"] = RV.canonical_sha(packet)
    RV.write_json_private(slot_dir / "packet.json", packet)
    system, user = build_review_request(packet=packet,
                                        calibration=calibration,
                                        review_id=slot.review_id,
                                        reviewer_id=slot.reviewer_id)
    try:
        measure = measure_request(system=system, user=user,
                                  label=slot.review_id)
    except PRC.ReviewCapacityError as exc:
        raise ReviewError("REVIEW_PACKET_TOO_LARGE",
                          f"{exc.code}: {exc.detail}") from None
    if measure.get("input_tokens", 0) > RV.REVIEW_INPUT_TOKENS_RV \
            or measure.get("bytes", 0) > RV.REVIEW_PACKET_BYTES_RV:
        raise ReviewError("REVIEW_PACKET_TOO_LARGE",
                          f"tokens={measure.get('input_tokens')} "
                          f"bytes={measure.get('bytes')}")
    request = {"schema": "simpraxis.rsi_rv.review_request.v1",
               "schema_version": 1, "system": system, "user": user,
               "review_id": slot.review_id, "reviewer_id": slot.reviewer_id,
               "study": slot.study, "kind": slot.kind}
    RV.write_json_private(slot_dir / "request.json", request)
    view = run_slot_process(slot_dir, "request")
    started = RV.utc_now()
    text = sender(system, user)
    ended = RV.utc_now()
    if not isinstance(text, str) or not text.strip():
        raise ReviewError("REVIEW_EMPTY", "空回复：该槽无有效审查，不补呼")
    if text.lstrip().startswith("```"):
        raise ReviewError("REVIEW_FORMAT", "回复含 markdown 围栏")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError("REVIEW_FORMAT", str(exc)) from None
    RV.write_json_private(slot_dir / "response.json",
                          {"schema": "simpraxis.rsi_rv.review_response.v1",
                           "schema_version": 1, "text": text,
                           "sha256": RV.sha_bytes(text.encode("utf-8")),
                           "started": started, "ended": ended})
    parsed = validate_review(doc, packet=packet, review_id=slot.review_id,
                             reviewer_id=slot.reviewer_id,
                             calibration=calibration)
    RV.write_json_private(slot_dir / "review.json", parsed)
    view2 = run_slot_process(slot_dir, "validate")
    isolation = _isolation_evidence(
        slot_dir, allowlist=["packet.json", "request.json", "response.json",
                             "review.json", "slot_runner.py"], process_id=0)
    receipt = RRC.build_receipt(
        review_id=slot.review_id, reviewer_id=slot.reviewer_id,
        session_id=f"{slot.study}-ch{slot.channel}",
        process_id=int(view.get("pid") or 0),
        prompt=SLOT_RUNNER.encode(), schema=RV.canonical(RV.public_rubric()),
        source=RV.canonical(packet), packet=RV.canonical(packet),
        packet_sha256=str(packet["packet_body_sha256"]),
        request=RV.canonical(request),
        response=text.encode("utf-8"),
        access_log=RV.canonical({"view": view, "view2": view2}),
        input_allowlist=["packet.json", "request.json", "response.json",
                         "review.json"],
        started_at_utc=started, ended_at_utc=ended, isolation=isolation)
    RRC.verify_receipt(receipt, review_id=slot.review_id,
                       reviewer_id=slot.reviewer_id,
                       packet_sha256=str(packet["packet_body_sha256"]))
    RV.write_json_private(slot_dir / "receipt.json", receipt)
    payload = ({"cases": parsed["cases"]} if calibration
               else {"items": parsed["items"]})
    return {"review_id": slot.review_id, "reviewer_id": slot.reviewer_id,
            "study": slot.study, "kind": slot.kind, "channel": slot.channel,
            **payload,
            "packet_sha256": packet["packet_body_sha256"],
            "request_sha256": RV.sha_bytes(RV.canonical(request)),
            "response_sha256": RV.sha_bytes(text.encode("utf-8")),
            "measure": measure, "receipt": receipt}


def merge_study_reviews(*, slots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(slots) != RV.REVIEW_CHANNELS:
        return {"verdict": "REVIEW_UNVERIFIABLE",
                "problems": ["通道数不足"], "items": {}}
    try:
        for slot in slots:
            if not isinstance(slot.get("receipt"), Mapping):
                raise ReviewError("REVIEW_RECEIPT", slot.get("review_id"))
        return RV.merge_review_channels(
            [{"items": {dim: {"verdict": (slot["items"][dim] or {}).get("verdict")}
                        for dim in RV.RUBRIC_DIMS}} for slot in slots])
    except (KeyError, TypeError) as exc:
        return {"verdict": "REVIEW_UNVERIFIABLE", "problems": [str(exc)],
                "items": {}}


__all__ = [
    "CALIBRATION_CASES", "PACKET_SCHEMA", "ReviewError", "ReviewSlot",
    "SLOT_RUNNER_SHA256", "build_calibration_packet", "build_review_request",
    "build_rv_packet", "check_calibration", "execute_slot",
    "merge_study_reviews", "sealed_expected", "validate_packet",
    "validate_review",
]
