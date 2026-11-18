"""RSI-R1-N 结案：五轴判定（Plan §6），不把过程 PASS 等同科学成功。

五轴独立记录：

* ``process``：``COMPLETE``｜``CLOSEOUT_INCOMPLETE``；
* ``measurement``：``VALID``｜``INVALID_INSTRUMENT``｜``INCOMPLETE``；
* ``study_execution``：``NOT_STARTED``｜``COMPLETED``｜``VALID_FAIL``｜
  ``BLOCKED``｜``ASSISTED``（**救援必须落 ASSISTED，不得冒 BLOCKED**）；
* ``claim``：``NONE``｜``SUPPORTED``｜``REFUTED``｜``INCONCLUSIVE``｜
  ``CLAIM_INVALID``（这是**主张-证据一致性**轴，不等于科学支持）；
* ``r1``：``NOT_MEASURED``｜``INCOMPLETE_COVERAGE``｜``NOT_ESTABLISHED``｜
  ``MINIMAL_RESEARCH_LOOP_ESTABLISHED``。

r1 成立的五条 AND（路线图 L86/L118 的编码）：

1. 两题各自完整闭环（八动作完成、claim 与判据一致、报告双审通过）；
2. 有效阴性成立：A_N 的 sham 等效主张 SUPPORTED；
3. 独立复跑成立：两题确认集＋冻结分析逐位/容差一致；
4. 无实质人工救援（有救援整案降 assisted）；
5. 工程轴与科学轴分开：``REFUTED/INCONCLUSIVE`` 是合法科学结论。

旧 ``closeout_r13.build_closeout`` 与本模块并存；旧调用点与旧判件一字不动。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

CLOSEOUT_SCHEMA = "simpraxis.rsi_r1n.closeout.v1"

PROCESS_COMPLETE = "COMPLETE"
PROCESS_INCOMPLETE = "CLOSEOUT_INCOMPLETE"
MEASUREMENT_VALID = "VALID"
MEASUREMENT_INVALID = "INVALID_INSTRUMENT"
MEASUREMENT_INCOMPLETE = "INCOMPLETE"
STUDY_NOT_STARTED = "NOT_STARTED"
STUDY_COMPLETED = "COMPLETED"
STUDY_VALID_FAIL = "VALID_FAIL"
STUDY_BLOCKED = "BLOCKED"
STUDY_ASSISTED = "ASSISTED"
CLAIM_NONE = "NONE"
CLAIM_SUPPORTED = "SUPPORTED"
CLAIM_REFUTED = "REFUTED"
CLAIM_INCONCLUSIVE = "INCONCLUSIVE"
CLAIM_INVALID = "CLAIM_INVALID"
R1_NOT_MEASURED = "NOT_MEASURED"
R1_INCOMPLETE = "INCOMPLETE_COVERAGE"
R1_NOT_ESTABLISHED = "NOT_ESTABLISHED"
R1_ESTABLISHED = "MINIMAL_RESEARCH_LOOP_ESTABLISHED"

CLAIM_SCOPE = "two_case_feasibility"
NO_EXTRAPOLATION = (
    "成功只声明：两个新机制案例上的最小研究可行性。"
    "不外推成功率、迁移能力或 RSI 成立；不启动 R2、不解冻 agentic。")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: "str | Path | None") -> "dict[str, Any] | None":
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _contrast_status(analysis: Mapping[str, Any] | None, cid: str) -> "str | None":
    if not isinstance(analysis, Mapping):
        return None
    for row in analysis.get("contrasts") or []:
        if isinstance(row, Mapping) and str(row.get("contrast_id")) == cid:
            return str(row.get("status") or "") or None
    return None


def build_closeout_r1n(*, campaign: str, taskset_dir: "str | Path",
                       t5_summary: "str | Path | None" = None,
                       family_doc: "str | Path | None" = None,
                       calibration_report: "str | Path | None" = None,
                       study_reports: "Sequence[str | Path]" = (),
                       review_reports: "Sequence[str | Path]" = (),
                       replay_reports: "Sequence[str | Path]" = (),
                       analyses: "Sequence[str | Path]" = (),
                       human_events: "str | Path | None" = None,
                       primary_contrast_ids: "Mapping[str, str] | None" = None,
                       negative_contrast_id: "str | None" = None,
                       notes: Sequence[str] = ()) -> "dict[str, Any]":
    taskset = Path(taskset_dir)
    manifest = _read(taskset / "taskset_manifest.json") or {}
    plan = _read(taskset / "resource_plan.json") or {}
    t5 = _read(t5_summary)
    family = _read(family_doc)
    calibration = _read(calibration_report)
    # 2026-11-18：保留 study 报告路径 —— judge 块缺 primary_status 时回退读
    # 同 campaign 的 judge/report_<study>.json（第二轮 science_direction 为空
    # 的根因；study 报告只是摘要，判据全件才有 primary_status/negative_control）。
    study_docs = [(Path(p), d) for p in study_reports
                  for d in (_read(p),) if d]
    studies = [d for _p, d in study_docs]
    reviews = [r for r in (_read(p) for p in review_reports) if r]
    replays = [r for r in (_read(p) for p in replay_reports) if r]
    analysis_docs = [a for a in (_read(p) for p in analyses) if a]
    human = _read(human_events)
    primary_ids = dict(primary_contrast_ids or {})

    t5_ok = bool(t5 and t5.get("all_ok"))
    calibration_ok = bool(calibration and calibration.get("ok"))

    # ---- 每 study 的工程/科学事实 ---------------------------------------
    per_study: "list[dict[str, Any]]" = []
    for path, doc in study_docs:
        study = str(doc.get("study") or "")
        judge = doc.get("judge") or {}
        if not judge.get("primary_status"):
            full = _read(path.parent.parent / "judge" / f"report_{study}.json")
            if full:
                judge = {**full, **judge}
        task_id = str(doc.get("task_id") or "")
        cid = primary_ids.get(study) or (primary_ids.get(task_id) or "")
        analysis = None
        for cand in analysis_docs:
            if str(cand.get("task_id") or "") == task_id and task_id:
                analysis = cand
                break
        judge_nc = judge.get("negative_control") or {}
        nc_status = None
        nc_source = None
        if study == "A":
            if judge_nc.get("status"):
                nc_status = str(judge_nc["status"])
                nc_source = str(judge_nc.get("source") or "judge")
            elif negative_contrast_id:
                nc_status = _contrast_status(analysis, negative_contrast_id)
                nc_source = ("analysis_output_fallback"
                             if nc_status else None)
        per_study.append({
            "study": study,
            "task_id": task_id,
            "status": doc.get("status"),
            "study_state": doc.get("study_state"),
            "judge": {
                "verdict": judge.get("verdict"),
                "claim_status": judge.get("claim_status"),
                "evidence_valid": judge.get("evidence_valid"),
            },
            "primary_contrast_id": cid or None,
            # 科学方向优先取宿主重算结果（判据件 primary_status），
            # 其次候选分析；两者都不改 r1 判定。
            "science_direction": (str(judge.get("primary_status"))
                                  if judge.get("primary_status") else
                                  (_contrast_status(analysis, cid) if cid else None)),
            "negative_contrast_id": (negative_contrast_id
                                     if study == "A" else None),
            "negative_status": nc_status,
            "negative_source": nc_source,
        })

    # ---- study_execution（ASSISTED 优先，不冒 BLOCKED）-------------------
    assisted_flag = any(str(p.get("study_state")) == STUDY_ASSISTED
                        for p in per_study)
    rescue_orders = list((human or {}).get("rescue_orders") or [])
    if rescue_orders:
        assisted_flag = True
    if not per_study:
        study_execution = STUDY_NOT_STARTED
    elif assisted_flag:
        study_execution = STUDY_ASSISTED
    elif all(str(p.get("study_state")) == "VERIFIED" for p in per_study):
        study_execution = STUDY_COMPLETED
    elif any(str(p.get("study_state")) == "VALID_FAIL" for p in per_study):
        study_execution = STUDY_VALID_FAIL
    else:
        study_execution = STUDY_BLOCKED

    # ---- claim（主张-证据一致性轴；不是科学支持）------------------------
    claim = CLAIM_NONE
    for p in per_study:
        status = str(p["judge"].get("claim_status") or "").upper()
        if status == "MATCH":
            claim = (CLAIM_SUPPORTED if p["judge"].get("verdict") == "JUDGE_OK"
                     else CLAIM_INCONCLUSIVE)
        elif status:
            claim = CLAIM_INVALID

    # ---- measurement ------------------------------------------------------
    if calibration is not None and not calibration_ok:
        measurement = MEASUREMENT_INVALID
    elif any(p["judge"].get("evidence_valid") is False for p in per_study):
        measurement = MEASUREMENT_INVALID
    elif t5_ok or calibration_ok:
        measurement = MEASUREMENT_VALID
    else:
        measurement = MEASUREMENT_INCOMPLETE

    # ---- 有效阴性 / 复跑 / 双审 ------------------------------------------
    nc_status = None
    nc_source = None
    for p in per_study:
        if p.get("negative_status"):
            nc_status = p["negative_status"]
            nc_source = p.get("negative_source")
    # 有效阴性必须来自**宿主独立重算**（judge 件），不接受候选分析自报。
    effective_negative = (str(nc_status or "").upper() == "SUPPORTED"
                          and nc_source == "host_recomputed_from_raw")

    replay_ok = bool(replays) and all(
        bool(r.get("ok")) and bool(r.get("identical", True))
        and bool(r.get("analysis_identical", True)) for r in replays)
    replay_complete = len(replays) >= len(per_study) and bool(per_study)

    reviews_ok = bool(reviews) and all(
        r.get("verdict") == "REVIEW_PASS" for r in reviews)
    reviews_complete = len(reviews) >= 2 * len(per_study) and bool(per_study)

    # ---- r1：五条 AND ------------------------------------------------------
    conditions = {
        "both_studies_closed": (len(per_study) == 2
                                and study_execution == STUDY_COMPLETED
                                and claim == CLAIM_SUPPORTED
                                and reviews_ok),
        "effective_negative": effective_negative,
        "independent_replay": replay_ok,
        "no_substantive_rescue": not assisted_flag,
        "measurement_valid": measurement == MEASUREMENT_VALID,
    }
    if not per_study:
        r1 = R1_NOT_MEASURED
    elif not (reviews_complete and replay_complete):
        r1 = R1_INCOMPLETE
    else:
        r1 = R1_ESTABLISHED if all(conditions.values()) else R1_NOT_ESTABLISHED

    process = PROCESS_COMPLETE if (
        manifest and plan and calibration_ok and per_study and reviews_complete
        and replay_complete) else PROCESS_INCOMPLETE

    axes = {
        "process": process,
        "measurement": measurement,
        "study_execution": study_execution,
        "claim": claim,
        "r1": r1,
    }
    facts: "dict[str, Any]" = {
        "campaign": campaign,
        "claim_scope": CLAIM_SCOPE,
        "taskset_sha256": _sha(taskset / "taskset_manifest.json")
        if (taskset / "taskset_manifest.json").is_file() else None,
        "resource_plan_sha256": _sha(taskset / "resource_plan.json")
        if (taskset / "resource_plan.json").is_file() else None,
        "holdout_task_ids": manifest.get("holdout_task_ids") or [],
        "resource": {
            "total_steps": plan.get("total_steps"),
            "hard_cap_steps": plan.get("hard_cap_steps"),
            "subenvelopes": plan.get("subenvelopes"),
        },
        "t5": None if t5 is None else {
            "all_ok": bool(t5.get("all_ok")),
            "studies": [{"study": s.get("study"), "all_ok": s.get("all_ok")}
                        for s in t5.get("studies") or []],
        },
        "family": None if family is None else {
            "family_cap": family.get("family_cap"),
            "members": family.get("members"),
        },
        "calibration": None if calibration is None else {
            "code": calibration.get("code"),
            "ok": calibration_ok,
        },
        "studies": per_study,
        "reviews": [{"verdict": r.get("verdict")} for r in reviews],
        "replays": [{"study": r.get("study"), "ok": r.get("ok"),
                     "identical": r.get("identical"),
                     "analysis_identical": r.get("analysis_identical")}
                    for r in replays],
        "human": None if human is None else {
            "complete": human.get("complete"),
            "rescue_orders": len(rescue_orders),
            "rescue_success_count": human.get("rescue_success_count"),
            "n_events": human.get("n_events"),
            "known_seconds_total": human.get("known_seconds_total"),
        },
        "r1_conditions": conditions,
    }
    doc = {
        "schema": CLOSEOUT_SCHEMA,
        "schema_version": 1,
        "axes": axes,
        "facts": facts,
        "notes": list(notes) + [NO_EXTRAPOLATION],
        "rule": ("过程 PASS ≠ 科学成功；claim MATCH ≠ 科学 SUPPORTED；"
                 "REFUTED/INCONCLUSIVE 是合法科学结论；有救援整案降 assisted；"
                 "任何一条 AND 不成立即 r1=NOT_ESTABLISHED 并如实结案。"),
    }
    return doc


def write_closeout(out: "str | Path", doc: Mapping[str, Any]) -> str:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode()).hexdigest()
