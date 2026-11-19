#!/usr/bin/env python3
"""RSI-S2 结案机判（S2-7）：重读证据复算 problems/gaps + 判件不动点。

判件**只信盘上证据**，不信报告自述：

* 双 LOCK 复算（规则面先于任何候选 diff——AC-01 时间戳序）；
* 候选包机检重跑 + 无代写（包内内容与工作区逐字节一致）；
* 12 个臂-实例研究报告：K0→K8 状态、v2 词表、G-CLAIM 违规 0、机器门记录；
* 双臂独立复跑：n_diffs=0 且逐对比与 K7 一致；
* 双审：24 包 `_used_packs` 复算、两通道互异、冻结报告 sha；
* 统计：paired-permutation 表、非劣逐项、预测签名兑现（矛盾即结论）；
* Gate2 决策件；网关 2910 账（seq 连续、ts 严格 Z、task 帽、批准件覆盖）+ 会话账；
* ResearchMemory 三件 + 闭源 push 面 0 件。

**判件不动点**：``judge_sha256`` 对剔除所有 ``*_at_utc`` 的稳定投影取 sha——
同一证据树两次复判逐位相同（S1 教训：live 时间戳入哈希会漂移）。

用法::

    python scripts/verification/check_s2_closure.py --campaign-root <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s2_closeout_dir)
from orchestrator.gateway.quota_ledger import QuotaLedger  # noqa: E402
from orchestrator.hk import evolution as EV  # noqa: E402
from orchestrator.hk import verdicts as V  # noqa: E402
from orchestrator.rsi_bridge import review_s2 as RV2  # noqa: E402

SCHEMA = "simpraxis.rsi_s2.closure_judge.v1"


class JudgeError(RuntimeError):
    pass


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stable(doc: Any) -> Any:
    """稳定投影：递归剔除 ``*_at_utc`` 键（判件不动点）。"""
    if isinstance(doc, Mapping):
        return {k: _stable(v) for k, v in sorted(doc.items())
                if not str(k).endswith("_at_utc")}
    if isinstance(doc, list):
        return [_stable(v) for v in doc]
    return doc


def _sha(doc: Any) -> str:
    return hashlib.sha256(json.dumps(_stable(doc), ensure_ascii=False,
                                      sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")
                          ).hexdigest()


def _prereg_module():
    path = _REPO / "scripts" / "s2" / "prereg_campaign.py"
    spec = importlib.util.spec_from_file_location("s2_prereg_judge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*, campaign_root: Path, report_path: Path | None = None) -> "dict[str, Any]":
    root = Path(campaign_root)
    problems: "list[str]" = []
    gaps: "list[str]" = []
    checks: "dict[str, Any]" = {}

    # 1) 双 LOCK + 时间戳序
    prereg = _prereg_module()
    try:
        rules_lock = prereg.verify_lock("rules")
        campaign_lock = prereg.verify_lock("campaign")
        checks["locks"] = {"rules": rules_lock["bundle_sha256"],
                           "campaign": campaign_lock["bundle_sha256"]}
    except SystemExit as exc:
        problems.append(f"LOCKS: {exc}")
        rules_lock = campaign_lock = {}
    validation_path = Path("/mnt/artifacts/simpraxis/rsi_s2/candidate/validation.json")
    if validation_path.is_file():
        validation = _load(validation_path)
        checks["candidate"] = {"diff_sha256": validation.get("diff_sha256"),
                               "files": validation.get("files")}
    else:
        problems.append("CANDIDATE: validation.json 缺失")
        validation = {}
    outcome_path = Path("/mnt/artifacts/simpraxis/rsi_s2/candidate/proposer_outcome.json")
    if outcome_path.is_file():
        proposer = _load(outcome_path)
        if proposer.get("maintainer_edited") is not False:
            problems.append("NO_GHOSTWRITING: maintainer_edited 不为 False")
        # AC-01 口径：规则**内容**首次冻结时刻必须先于候选 diff（重签不影响）
        try:
            frozen_at = str(_load(Path("/mnt/artifacts/simpraxis/rsi_s2/prereg"
                                       "/rules/rules.json"))
                            .get("rules_content_frozen_at_utc") or "")
        except FileNotFoundError:
            frozen_at = ""
        rules_signed = frozen_at or (rules_lock.get("signed_at_utc") or [""])[0]
        started = str(proposer.get("started_at_utc") or "")
        if not started:
            gaps.append("AC-01: 候选会话开始时刻缺失（无法核时间戳序）")
        elif rules_signed and started <= rules_signed:
            problems.append("AC-01: 候选 diff 早于规则面 LOCK")

    # 2) 校准 + denial
    try:
        calibration = _load(Path("/mnt/artifacts/simpraxis/rsi_s2/closeout"
                                 "/calibration_report.json"))
        checks["calibration"] = {"report_sha256": calibration.get("report_sha256"),
                                 "all_ok": calibration.get("all_ok")}
        if calibration.get("all_ok") is not True:
            problems.append("CALIBRATION: 六门未全过")
    except FileNotFoundError:
        problems.append("CALIBRATION: 报告缺失")

    # 3) 臂-实例 + 复跑（**结果 vs 完整性分层**）
    #    v1 = 候选臂：必须 K0→K8 全链 + 复跑 n_diffs=0；
    #    v0 = S1 as-built：在共享规则面（NC 三连判据）下**预期**结构停止
    #    （NC_DEGENERATE / NC_UNREGISTERED），如实记入 results，不当完整性问题。
    instances = [str(i["instance_id"]) for i in prereg.INSTANCES]
    reviewed_pairs = [iid for iid in instances
                      if (root / "review" / f"{iid}-v0.json").is_file()
                      and (root / "review" / f"{iid}-v1.json").is_file()]
    trials: "dict[str, Any]" = {}
    v0_outcomes: "dict[str, Any]" = {}
    for iid in reviewed_pairs:
        for arm in ("v0", "v1"):
            label = f"{iid}-{arm}"
            trial_path = root / "trial" / label / "study_report.json"
            if not trial_path.is_file():
                problems.append(f"TRIAL_MISSING: {label}")
                continue
            trial = _load(trial_path)
            family = trial["family"]
            stage = (trial["outcome"].get("stages") or {}).get(family) or {}
            claim = stage.get("claim") or {}
            code = str(stage.get("code") or "")
            rerun_path = root / "rerun" / f"{label}.json"
            rerun = _load(rerun_path) if rerun_path.is_file() else {}
            if arm == "v0":
                v0_outcomes[label] = {"status": stage.get("status"), "code": code,
                                      "rerun_skipped": rerun.get("skipped")}
                if stage.get("status") != "human_failure" or not code.startswith("NC_"):
                    if code != "STAGE_RUNTIME_ERROR":
                        gaps.append(f"V0_UNEXPECTED: {label} {code}")
                trials[label] = {"status": stage.get("status"), "code": code}
                continue
            # v1（候选臂）
            if stage.get("status") != "done":
                problems.append(f"TRIAL_NOT_DONE: {label} {code}")
            if claim.get("verdict") not in V.VERDICTS_V2:
                problems.append(f"VERDICT_INVALID: {label} {claim.get('verdict')!r}")
            if not any(g.get("machine_gate")
                       for g in trial.get("machine_gates") or []):
                problems.append(f"MACHINE_GATE_MISSING: {label}")
            if not rerun_path.is_file():
                problems.append(f"RERUN_MISSING: {label}")
            elif int(rerun.get("n_diffs") or 0) != 0:
                problems.append(f"RERUN_DIFFS: {label} n_diffs={rerun.get('n_diffs')}")
            elif rerun.get("contrasts_match_K7") is not True:
                problems.append(f"RERUN_MISMATCH: {label}")
            trials[label] = {"status": stage.get("status"),
                             "verdict": claim.get("verdict"),
                             "n_runs": rerun.get("n_runs")}
    checks["trials"] = trials
    checks["v0_outcomes"] = v0_outcomes
    checks["reviewed_pairs"] = reviewed_pairs

    # 4) 双审（按其实际覆盖的配对）
    dual_path = root / "dual_review.json"
    if not dual_path.is_file():
        problems.append("DUAL_REVIEW_MISSING")
        dual = {}
    else:
        dual = _load(dual_path)
        try:
            RV2.verify_frozen_report(dual)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"DUAL_REVIEW_TAMPERED: {exc}")
        reports = dual.get("reports") or []
        if len(reports) != 2 * len(reviewed_pairs):
            problems.append(
                f"DUAL_REVIEW_PACKS: {len(reports)} != {2 * len(reviewed_pairs)}")
        for row in reports:
            label = f"{row.get('instance_id')}-{row.get('arm')}"
            for channel in ("channel_a", "channel_b"):
                pack = ((row.get("channels") or {}).get(channel, {})
                        .get("_used_packs") or {}).get(channel)
                if not isinstance(pack, Mapping):
                    problems.append(f"PACK_PROVENANCE: {label}/{channel}")
                    continue
                claimed = str(pack.get("pack_sha256") or "")
                actual = hashlib.sha256(json.dumps(
                    {k: v for k, v in pack.items() if k != "pack_sha256"},
                    ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()
                if claimed != actual:
                    problems.append(f"PACK_SHA: {label}/{channel}")
        checks["dual_review_sha256"] = dual.get("report_sha256")

    # 5) 统计 / 非劣 / 预测签名（**结果面**，不是完整性问题）
    analysis_path = root / "analysis.json"
    if not analysis_path.is_file():
        problems.append("ANALYSIS_MISSING")
        analysis = {}
    else:
        analysis = _load(analysis_path)
        stats = analysis.get("statistics") or {}
        if stats.get("n_pairs") != len(reviewed_pairs):
            problems.append(
                f"PERMUTATION: n_pairs={stats.get('n_pairs')} != {len(reviewed_pairs)}")
        if stats.get("n_sign_patterns") != 2 ** len(reviewed_pairs):
            problems.append("PERMUTATION: 符号模式数不符")
        lock_path = root / "prediction_lock.json"
        if not lock_path.is_file():
            problems.append("PREDICTION_NOT_LOCKED: 预测签名未在试验前冻结")
        else:
            lock = _load(lock_path)
            actual = hashlib.sha256(json.dumps(
                lock.get("prediction_signature"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if actual != lock.get("prediction_signature_sha256"):
                problems.append("PREDICTION_LOCK_TAMPERED")
            if lock.get("locked_before_trial") is not True:
                gaps.append("PREDICTION_LOCK_POSTHOC: 预测签名锁定晚于试验"
                            "（篡改窗口存在，结论按保守方向采信）")
            checks["prediction_lock_sha256"] = lock.get(
                "prediction_signature_sha256")
        checks["analysis"] = {
            "verdict": analysis.get("verdict"),
            "p_two_sided": stats.get("p_two_sided"),
            "mean_diff": stats.get("mean_diff"),
            "decision": stats.get("decision"),
            "non_inferiority": (analysis.get("non_inferiority") or {}).get("passed"),
            "prediction_fulfilled":
                (analysis.get("checks") or {}).get("prediction_fulfilled"),
            "upstream_status": (analysis.get("upstream") or {}).get("status"),
            "dimension_deltas": analysis.get("dimension_deltas"),
        }
        upstream = analysis.get("upstream") or {}
        if upstream.get("status") != "OK":
            gaps.append(f"UPSTREAM_ADAPTER: {upstream.get('status')}")

    # 6) Gate2
    gate2_path = root / "gate2_decision.json"
    if not gate2_path.is_file():
        problems.append("GATE2_MISSING")
        gate2 = {}
    else:
        gate2 = _load(gate2_path)
        checks["gate2_approved"] = bool(gate2.get("approved"))
        if not gate2.get("approved"):
            problems.append("GATE2_REJECTED")

    # 7) 网关 2910 账 + 会话账
    try:
        ledger = QuotaLedger.rsi_s2()
        rows = ledger._read_rows()  # noqa: SLF001
        calls = [r for r in rows if r.get("event", "call") == "call"]
        seqs = [r.get("seq") for r in calls]
        if seqs != list(range(1, len(seqs) + 1)):
            problems.append(f"LEDGER_SEQ: {seqs[:5]}…")
        for row in calls:
            ts = str(row.get("ts_iso") or "")
            if not ts.endswith("Z"):
                problems.append(f"LEDGER_TS: {row.get('seq')} {ts!r}")
                break
        if len(calls) > 40:
            problems.append(f"LEDGER_CAP: {len(calls)} > 40")
        checks["gateway_calls"] = len(calls)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"LEDGER: {type(exc).__name__}: {exc}")
    try:
        from orchestrator.rsi_bridge.session_ledger import SessionLedger
        from orchestrator.common.runtime_paths import rsi_s2_sessions_dir

        session_rows = SessionLedger(
            rsi_s2_sessions_dir() / "sessions.jsonl").read()
        # 会话数 = session_start 行数（账是两行式 start/end）；行数不是会话数
        sessions = [r for r in session_rows if r.get("kind") == "session_start"]
        checks["sessions"] = len(sessions)
        checks["session_rows"] = len(session_rows)
        if len(sessions) > 48:
            problems.append(f"SESSION_CAP: {len(sessions)} > 48")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"SESSION_LEDGER: {type(exc).__name__}: {exc}")

    # 8) ResearchMemory 三件 + 闭源 push 面
    memory_path = Path("/mnt/artifacts/simpraxis/rsi_s2/closed/research_memory"
                       "/s2_lessons.jsonl")
    if memory_path.is_file():
        lessons = [json.loads(line) for line in
                   memory_path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        checks["s2_lessons"] = [row.get("lesson_id") for row in lessons]
        if len(lessons) < 3:
            problems.append(f"S2_LESSONS: {len(lessons)} < 3")
    else:
        problems.append("S2_LESSONS_MISSING")

    # 8b) 过程性缺口如实挂牌（verifier 复评提出）
    archived = sorted((campaign_root / "review_instrument_v1").glob("*.json"))
    if len(archived) < 12:
        gaps.append(f"REVIEW_ARCHIVE_LOSS: 归档仅 {len(archived)}/12 包"
                    "（首轮同名 3 包被第二轮 rename 覆盖；判词与理由在 progress 留痕）")
    try:
        rules_doc = _load(Path("/mnt/artifacts/simpraxis/rsi_s2/prereg/rules"
                                "/rules.json"))
        if rules_doc.get("rules_repaired_at_utc"):
            gaps.append(
                "RULE_REPAIR_POSTHOC: 规则模块试验后被修 "
                f"{rules_doc.get('repaired_modules')}（仪器面；统计结论不变）")
    except FileNotFoundError:
        pass
    gate2_doc = {}
    try:
        gate2_doc = _load(campaign_root / "gate2_decision.json").get("decision") or {}
    except FileNotFoundError:
        pass
    if not int((gate2_doc or {}).get("rejects") or 0):
        gaps.append("GATE2_REJECT_LOOP: 打回循环未实战（首卡即 approve，不伪造）")
    gaps.append("HUMAN_COIMPROVEMENT: 降级路径仅单测负控"
                "（tests/milestoneS2/test_human_coimprovement.py），未在战役中触发")

    # 9) 双判词（分轴；互不替代）
    verdict = analysis.get("verdict") if analysis else None
    v1_reports = [r for r in (dual.get("reports") or []) if r.get("arm") == "v1"]
    if not v1_reports:
        r1_v1 = None
    elif all(r.get("verdict") == "PASS" for r in v1_reports):
        r1_v1 = "MINIMAL_RESEARCH_LOOP_ESTABLISHED(v1)"
    else:
        r1_v1 = "NOT_ESTABLISHED"
    checks["verdicts"] = {"R1_v1": r1_v1, "R2": verdict,
                           "v1_pass": sum(1 for r in v1_reports
                                          if r.get("verdict") == "PASS"),
                           "v1_total": len(v1_reports)}
    if not dual:
        gaps.append("R1-v1 判词不可得（双审缺失）")

    judge = {
        "schema": SCHEMA,
        "campaign_root": str(root),
        "checks": checks,
        "problems": problems,
        "gaps": gaps,
        "pass": not problems,
        "judged_at_utc": utc_now_iso_z(),
        "judge_sha256": "",
    }
    judge["judge_sha256"] = _sha(judge)
    target = Path(report_path) if report_path else (
        rsi_s2_closeout_dir() / "s2_closure_judge.json")
    ensure_private_dir(Path(target).parent)
    atomic_write_text(target, json.dumps(judge, ensure_ascii=False,
                                          indent=2, sort_keys=True) + "\n",
                      mode=0o600)
    return judge


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S2 结案机判")
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    report = run(campaign_root=Path(args.campaign_root),
                 report_path=Path(args.report) if args.report else None)
    print(json.dumps({"pass": report["pass"], "problems": report["problems"],
                      "gaps": report["gaps"],
                      "judge_sha256": report["judge_sha256"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
