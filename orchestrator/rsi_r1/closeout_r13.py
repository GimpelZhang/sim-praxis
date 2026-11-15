"""R1.3 T9 结案：五轴判定与事实清点（不把过程 PASS 等同科学成功）。

五轴（Plan §7/§9）：

* ``process``：CLOSEOUT_INCOMPLETE｜COMPLETE——所有计划内阶段是否都有终态件；
* ``measurement``：VALID｜INVALID_INSTRUMENT｜CONTAMINATED｜INCOMPLETE；
* ``study_execution``：NOT_STARTED｜COMPLETED｜VALID_FAIL｜BLOCKED｜ASSISTED；
* ``claim``：NONE｜SUPPORTED｜REFUTED｜INCONCLUSIVE｜CLAIM_INVALID；
* ``r1``：NOT_MEASURED｜INCOMPLETE_COVERAGE｜NOT_ESTABLISHED｜
  MINIMAL_RESEARCH_LOOP_ESTABLISHED。

结案件只记录事实与判据给出的分层结果；不重判、不改写 raw、不把「T5 参考链
通过」说成候选研究成立。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

CLOSEOUT_SCHEMA = "simpraxis.rsi_r13.closeout.v1"

PROCESS_COMPLETE = "COMPLETE"
PROCESS_INCOMPLETE = "CLOSEOUT_INCOMPLETE"
MEASUREMENT_VALID = "VALID"
MEASUREMENT_INVALID = "INVALID_INSTRUMENT"
MEASUREMENT_CONTAMINATED = "CONTAMINATED"
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: "Path | None") -> "dict[str, Any] | None":
    if path is None or not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_closeout(*, campaign: str, taskset_dir: "str | Path",
                   t5_summary: "str | Path | None",
                   family_doc: "str | Path | None" = None,
                   calibration_report: "str | Path | None" = None,
                   study_reports: "Sequence[str | Path]" = (),
                   review_reports: "Sequence[str | Path]" = (),
                   human_events: "str | Path | None" = None,
                   notes: Sequence[str] = ()) -> "dict[str, Any]":
    taskset = Path(taskset_dir)
    manifest = _read(taskset / "taskset_manifest.json") or {}
    plan = _read(taskset / "resource_plan.json") or {}
    t5 = _read(Path(t5_summary) if t5_summary else None)
    family = _read(Path(family_doc) if family_doc else None)
    calibration = _read(Path(calibration_report) if calibration_report else None)
    studies = [_read(Path(p)) for p in study_reports]
    reviews = [_read(Path(p)) for p in review_reports]

    facts: "dict[str, Any]" = {
        "campaign": campaign,
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
            "history_posts": family.get("audited_history_posts"),
            "members": family.get("members"),
        },
        "calibration": None if calibration is None else {
            "code": calibration.get("code"),
            "ok": bool(calibration.get("ok")),
        },
        "studies": [s for s in studies if s],
        "reviews": [r for r in reviews if r],
        "human_events": str(human_events) if human_events else None,
    }

    t5_ok = bool(facts["t5"] and facts["t5"]["all_ok"])
    any_study = any(s.get("status") == "COMPLETED" for s in facts["studies"])
    any_review = bool(facts["reviews"])

    measurement = (MEASUREMENT_VALID if t5_ok else
                   MEASUREMENT_INCOMPLETE)
    if any_study:
        # 候选 study 的判据层决定 measurement 是否被污染/无效
        layers = [s.get("judge") or {} for s in facts["studies"]]
        if any(not l.get("evidence_valid", True) for l in layers):
            measurement = MEASUREMENT_INVALID
    if not calibration and not any_study:
        study_execution = STUDY_NOT_STARTED
    elif any_study:
        verdicts = [s.get("study_state") for s in facts["studies"]]
        if any(v == "VERIFIED" for v in verdicts):
            study_execution = STUDY_COMPLETED
        elif any(v == "VALID_FAIL" for v in verdicts):
            study_execution = STUDY_VALID_FAIL
        else:
            study_execution = STUDY_BLOCKED
    else:
        study_execution = STUDY_BLOCKED

    claim = CLAIM_NONE
    for s in facts["studies"]:
        judge = s.get("judge") or {}
        status = (judge.get("claim_status") or "").upper()
        if status == "MATCH":
            claim = CLAIM_SUPPORTED if judge.get("verdict") == "JUDGE_OK" \
                else CLAIM_INCONCLUSIVE
        elif status:
            claim = CLAIM_INVALID

    if not any_study:
        r1 = R1_NOT_MEASURED
    elif not any_review:
        r1 = R1_INCOMPLETE
    else:
        passed = all((r.get("verdict") == "REVIEW_PASS") for r in facts["reviews"])
        r1 = R1_ESTABLISHED if (passed and claim == CLAIM_SUPPORTED
                                and measurement == MEASUREMENT_VALID) \
            else R1_NOT_ESTABLISHED

    process = (PROCESS_COMPLETE
               if (facts["taskset_sha256"] and t5_ok and any_study and any_review
                   and calibration and calibration.get("ok"))
               else PROCESS_INCOMPLETE)

    axes = {
        "process": process,
        "measurement": measurement,
        "study_execution": study_execution,
        "claim": claim,
        "r1": r1,
    }
    doc = {
        "schema": CLOSEOUT_SCHEMA,
        "schema_version": 1,
        "axes": axes,
        "facts": facts,
        "notes": list(notes),
        "rule": ("过程 PASS ≠ 科学成功；未执行的阶段如实标 NOT_STARTED/"
                 "NOT_MEASURED，不用参考链冒充候选研究"),
    }
    return doc


def write_closeout(out: "str | Path", doc: Mapping[str, Any]) -> str:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode()).hexdigest()
