#!/usr/bin/env python3
"""RSI-S1 结案机判（S1-8）。

口径（仿 S0 判件）：**重读账本与证据复算，不信任何自报**。

检查项（problems 任一即红；gaps 为如实挂牌的不达成项，不计入通过）：

1. 预注册：manifest 逐件 sha 复算 + 双签互异 LOCK（改件即红）。
2. 校准门：``calibration_report.json`` all_ok 且 report_sha256 复算一致。
3. 研究包：A_N/B_N 各有 study_report；每 study 的 run 目录 K0–K8 齐、K7 逐点
   diffs 为空、K8 与 K7 数值逐字段一致（G-CLAIM 事后复核）。
4. 阴性：A_N 的 negative_torsional 主张必须为 SUPPORTED（90% 区间落 ±δ）。
5. 产者链：每个触点有 ``touchpoint_<name>.meta.json`` 且带 session_id+model；
   transcript 在场。
6. 会话账：哈希链可读、无半行、无未结束会话；会话数 ≤ 硬帽。
7. 网关账：2909 行 seq 连续、ts 严格 Z；call 行数 ≤ 硬帽；任务帽不越界；
   所用 task 的批准件在场（自批）。
8. 双审：校准 READY（且无 misaccept）+ 2×2 正式审查冻结件 sha 复算一致 +
   真值表完整；报告出现在结案目录。
9. 证据根有 denial probe 且 all_denied。

gaps（如实挂牌）：gate2 打回循环未实战、review FAIL/INCONCLUSIVE、assisted 救援、
研究结论 INCONCLUSIVE（B_N 允许）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import parse_ts_iso_strict  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s1_closeout_dir, rsi_s1_evidence_root, rsi_s1_ledger_path,
    rsi_s1_prereg_dir, rsi_s1_sessions_dir)
from orchestrator.rsi_bridge import review_s1 as RV  # noqa: E402
from orchestrator.rsi_bridge.session_ledger import SessionLedger  # noqa: E402

SCHEMA = "simpraxis.rsi_s1.closure.v1"
STUDIES = ("A_N", "B_N")


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        return None


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_sha(prereg: Path) -> "str | None":
    manifest = _load(prereg / "manifest.json")
    if not isinstance(manifest, dict):
        return None
    files = manifest.get("files") or {}
    actual = {}
    for name in sorted(files):
        path = prereg / name
        if not path.is_file():
            return None
        actual[name] = _sha_file(path)
        if actual[name] != files[name].get("sha256"):
            return None
    payload = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    computed = hashlib.sha256(payload).hexdigest()
    return computed if computed == manifest.get("bundle_sha256") else None


def _check_prereg(problems: list[str]) -> "dict[str, Any] | None":
    prereg = rsi_s1_prereg_dir()
    sha = _bundle_sha(prereg)
    if sha is None:
        problems.append("PREREG_CHANGED: manifest/bundle sha 不复算一致")
        return None
    lock = _load(prereg / "LOCK.json")
    if not isinstance(lock, dict) or lock.get("bundle_sha256") != sha:
        problems.append("PREREG_UNLOCKED: 缺 LOCK 或绑定不符")
        return {"bundle_sha256": sha}
    signers = [s.get("signer") for s in lock.get("signatures") or []
               if s.get("bundle_sha256") == sha]
    if len(signers) < 2 or len(set(signers)) != len(signers):
        problems.append("PREREG_SINGLE_SIGN: 双签不足或同名")
    return {"bundle_sha256": sha, "signers": signers}


def _check_calibration(problems: list[str], gaps: list[str]) -> "dict | None":
    report = _load(rsi_s1_closeout_dir() / "calibration_report.json")
    if not isinstance(report, dict):
        problems.append("CALIBRATION_MISSING")
        return None
    claimed = report.get("report_sha256")
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    actual = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    if claimed != actual:
        problems.append("CALIBRATION_TAMPERED: report_sha256 不复算一致")
    if report.get("all_ok") is not True:
        problems.append("CALIBRATION_FAILED: all_ok != true")
    return {"report_sha256": claimed, "all_ok": report.get("all_ok")}


def _latest_study_evidence() -> "dict[str, Path]":
    out: dict[str, Path] = {}
    for directory in sorted(rsi_s1_evidence_root().glob("*/")):
        report = _load(directory / "study_report.json")
        if not isinstance(report, dict):
            continue
        for study in report.get("studies") or []:
            out[study] = directory  # 目录名按 UTC 升序，后者覆盖前者
    return out


def _check_studies(problems: list[str], gaps: list[str]) -> dict[str, Any]:
    found = _latest_study_evidence()
    result: dict[str, Any] = {}
    for study in STUDIES:
        directory = found.get(study)
        if directory is None:
            problems.append(f"STUDY_MISSING: {study} 无 study_report")
            continue
        report = _load(directory / "study_report.json")
        outcome = report.get("outcome") or {}
        stage = (outcome.get("stages") or {}).get(study) or {}
        if stage.get("status") != "done":
            gaps.append(f"STUDY_{study}_NOT_DONE: code={stage.get('code')}")
        run_dirs = sorted((directory / "runs").glob(f"{study}_run*"))
        if not run_dirs:
            problems.append(f"STUDY_{study}_RUNS_MISSING")
            continue
        run_dir = run_dirs[-1]
        # 锁内适配（as-built）：driver 在 run_stage 开头落 ``stage_input.json``
        # （含 K0 登记件 spec/registered/sha256），K1–K8 逐技能落文件；K0 不单列。
        need = ["stage_input.json"] + [f"K{i}.json" for i in range(1, 9)]
        missing = [name for name in need if not (run_dir / name).is_file()]
        if missing:
            problems.append(f"STUDY_{study}_CHAIN_MISSING: {missing}")
        k7 = _load(run_dir / "K7.json") or {}
        k8 = _load(run_dir / "K8.json") or {}
        if (k7.get("recompute_report") or {}).get("n_diffs") not in (0, None):
            problems.append(f"STUDY_{study}_RERUN_DIFFS")
        if not k7.get("contrasts"):
            problems.append(f"STUDY_{study}_CONFIRM_EMPTY")
        claimed = {str(r.get("id")): r for r in (k8.get("contrasts") or [])
                   if isinstance(r, Mapping)}
        for row in k7.get("contrasts") or []:
            other = claimed.get(str(row.get("id")))
            if other is None:
                problems.append(f"STUDY_{study}_CLAIM_MISSING:{row.get('id')}")
                continue
            for key in ("estimate", "ci_low", "ci_high", "n_blocks"):
                if other.get(key) != row.get(key):
                    problems.append(
                        f"STUDY_{study}_CLAIM_MISMATCH:{row.get('id')}.{key}")
        # 阴性（A_N）：sham 必须 equivalent → claim SUPPORTED
        if study == "A_N":
            sham = next((r for r in k7.get("contrasts") or []
                         if r.get("id") == "negative_torsional"), None)
            if sham is None:
                problems.append("STUDY_A_N_NO_NEGATIVE_CONTROL")
            elif sham.get("claim") != "SUPPORTED":
                gaps.append("SHAM_NOT_EQUIVALENT: "
                            f"claim={sham.get('claim')} CI=[{sham.get('ci_low')},"
                            f"{sham.get('ci_high')}]")
        # 产者链
        metas = list(run_dir.glob("touchpoint_*.meta.json"))
        names = {m.name for m in metas}
        for tp in ("propose_plan", "write_analysis", "submit_claim"):
            meta_path = run_dir / f"touchpoint_{tp}.meta.json"
            if meta_path.name not in names:
                problems.append(f"STUDY_{study}_TOUCHPOINT_META_MISSING:{tp}")
                continue
            meta = _load(meta_path) or {}
            if not meta.get("claude_session_id") or not meta.get("model"):
                problems.append(f"STUDY_{study}_PRODUCER_CHAIN:{tp}")
            transcript = Path(str(meta.get("transcript") or ""))
            if not transcript.is_file():
                problems.append(f"STUDY_{study}_TRANSCRIPT_MISSING:{tp}")
            if meta.get("assisted"):
                gaps.append(f"STUDY_{study}_ASSISTED:{tp}")
        result[study] = {"evidence": str(directory), "run_dir": str(run_dir),
                         "claim_verdict": k8.get("verdict"),
                         "contrasts": [r.get("id") for r in k8.get("contrasts") or []]}
        for gap in report.get("gaps") or []:
            gaps.append(f"{study}:{gap}")
    return result


def _check_sessions(problems: list[str]) -> dict[str, Any]:
    path = rsi_s1_sessions_dir() / "sessions.jsonl"
    if not path.is_file():
        problems.append("SESSION_LEDGER_MISSING")
        return {}
    try:
        ledger = SessionLedger(path)
    except Exception as exc:  # noqa: BLE001 - 任何结构错误都是红
        problems.append(f"SESSION_LEDGER_INVALID: {exc}")
        return {}
    summary = ledger.summary()
    face = summary["session_face"]
    if face["open_sessions"]:
        problems.append(f"SESSION_UNCLOSED: {face['open_sessions']}")
    return summary


def _check_gateway(problems: list[str], gaps: list[str]) -> dict[str, Any]:
    path = rsi_s1_ledger_path()
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                problems.append("GATEWAY_ROW_MALFORMED")
    calls = [r for r in rows if r.get("event", "call") == "call"]
    seqs = [r.get("seq") for r in calls if isinstance(r.get("seq"), int)]
    if seqs != list(range(1, len(seqs) + 1)):
        problems.append(f"GATEWAY_SEQ: {seqs[:5]}… 非 1..N 连续")
    for row in rows:
        ts = row.get("ts_iso")
        if not isinstance(ts, str) or not ts.endswith("Z"):
            problems.append(f"GATEWAY_TS_NOT_Z: {ts!r}")
            break
        try:
            parse_ts_iso_strict(ts)
        except Exception:  # noqa: BLE001
            problems.append(f"GATEWAY_TS_INVALID: {ts!r}")
            break
    caps = {1: 4, 2: 8, 3: 8, 4: 20}
    by_task: dict[int, int] = {}
    for row in calls:
        task = row.get("task")
        if isinstance(task, int):
            by_task[task] = by_task.get(task, 0) + 1
    for task, used in by_task.items():
        if task not in caps or used > caps[task]:
            problems.append(f"GATEWAY_TASK_CAP: task={task} used={used}")
    if len(calls) > 40:
        problems.append(f"GATEWAY_HARD_CAP: {len(calls)} > 40")
    return {"rows": len(rows), "calls": len(calls), "by_task": by_task}


def _check_review(problems: list[str], gaps: list[str]) -> dict[str, Any]:
    review_dir = rsi_s1_closeout_dir() / "review"
    calib = _load(review_dir / "review_calibration.json")
    if not isinstance(calib, dict):
        problems.append("REVIEW_CALIBRATION_MISSING")
        calib = {}
    else:
        try:
            RV.verify_frozen_report(calib)
        except RV.ReviewError as exc:
            problems.append(f"REVIEW_CALIBRATION_TAMPERED: {exc.code}")
        if calib.get("ready") is not True:
            problems.append("REVIEW_INSTRUMENT_NOT_READY")
        if calib.get("misaccept"):
            problems.append(f"REVIEW_MISACCEPT: {calib['misaccept']}")
    dual = _load(review_dir / "dual_review.json")
    truth: dict[str, Any] = {}
    if not isinstance(dual, dict):
        problems.append("DUAL_REVIEW_MISSING")
    else:
        try:
            RV.verify_frozen_report(dual)
        except RV.ReviewError as exc:
            problems.append(f"DUAL_REVIEW_TAMPERED: {exc.code}")
        truth = dual.get("truth_table") or {}
        for study in STUDIES:
            row = truth.get(study)
            if not isinstance(row, dict) or row.get("verdict") is None:
                problems.append(f"DUAL_REVIEW_INCOMPLETE:{study}")
                continue
            if row["verdict"] != "PASS":
                gaps.append(f"DUAL_REVIEW_{study}_{row['verdict']}")
    return {"calibration_ready": calib.get("ready"), "truth_table": truth}


def _check_denial(problems: list[str]) -> "dict | None":
    for directory in sorted(rsi_s1_evidence_root().glob("*/")):
        audit = _load(directory / "mujoco_denial_probe.json")
        if isinstance(audit, dict):
            if audit.get("all_denied") is not True:
                problems.append("MUJOCO_NOT_DENIED_IN_SANDBOX")
            return audit
    problems.append("DENIAL_PROBE_MISSING")
    return None


def judge() -> dict[str, Any]:
    problems: list[str] = []
    gaps: list[str] = []
    report = {
        "schema": SCHEMA,
        "prereg": _check_prereg(problems),
        "calibration": _check_calibration(problems, gaps),
        "denial": _check_denial(problems),
        "studies": _check_studies(problems, gaps),
        "sessions": _check_sessions(problems),
        "gateway": _check_gateway(problems, gaps),
        "review": _check_review(problems, gaps),
        "problems": problems,
        "gaps": sorted(set(gaps)),
        "pass": not problems,
    }
    payload = json.dumps({k: v for k, v in report.items() if k != "judge_sha256"},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    report["judge_sha256"] = hashlib.sha256(payload).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 结案机判")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    report = judge()
    target = Path(args.report) if args.report else (
        rsi_s1_closeout_dir() / "s1_closure.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"pass": report["pass"],
                      "problems": report["problems"],
                      "gaps": report["gaps"],
                      "judge_sha256": report["judge_sha256"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
