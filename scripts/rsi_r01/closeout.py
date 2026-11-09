#!/usr/bin/env python3
"""RSI-R0.1 离线结案器（T4 / AC25）：事实驱动，不复制 R0 的硬编码终判。

三条纪律：

1. **独立离线入口**：只要求一个可确认的 campaign 输出根与调用者写权限；不要求
   env 成功、consent、LOCK、预算件或完整阶段状态存在。缺件写成 ``missing`` 并附
   原因，报告照样产出，也不据缺件补造授权。
2. **事实驱动**：主判决由 :func:`decide` 按计划 §9.2 优先级从**归一化事实**推出；
   所有触发的判决与 reason_codes 全保留（高优先级覆盖显示，低优先级不丢）。
3. **fixture 先行**：``--write-fixtures`` 为 §9.3 每个事件写一份合成事实并走同一
   ``decide``/写报告路径；付费前必须全绿（AC25）。合成 POST 数 ≠ ledger 行数。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.rsi_r01 import schemas as S

SCHEMA_CLOSEOUT = "simpraxis.rsi_r01.closeout.v1"

DEFAULT_ROOT = Path("/mnt/artifacts/simpraxis/rsi_r01")

#: §9.3 事件 → 期望的主判决（fixture 断言表；trial 级事件写 campaign 侧后果）。
EVENT_EXPECTATIONS: dict[str, str] = {
    "T0_missing_env": S.MeasurementVerdict.BLOCKED_ENV,
    "T0_missing_approval": S.MeasurementVerdict.BLOCKED_APPROVAL,
    "T0_price_unknown": S.MeasurementVerdict.BLOCKED_PRICE,
    "T0_token_accounting_missing": S.MeasurementVerdict.BLOCKED_TOKEN_ACCOUNTING,
    "STOP_TASK_DESIGN": S.MeasurementVerdict.DESIGN_NOT_READY,
    "long_chain_lower_bound": S.MeasurementVerdict.BLOCKED_CONTEXT_MEASUREMENT,
    "input_over_cap": S.MeasurementVerdict.BLOCKED_INPUT_LIMIT,
    "policy_violation_trial": S.MeasurementVerdict.ABORTED_INCOMPLETE,
    "safety_breach": S.MeasurementVerdict.SAFETY_INVALID,
    "empty_answer": S.MeasurementVerdict.BLOCKED_TRANSPORT,
    "unknown_sent_unresolved": S.MeasurementVerdict.BLOCKED_TRANSPORT,
    "unknown_sent_not_sent": S.MeasurementVerdict.ABORTED_INCOMPLETE,
    "unknown_sent_resolved": S.MeasurementVerdict.ABORTED_INCOMPLETE,
    "cap_exhausted_dev": S.MeasurementVerdict.DEVELOPMENT_NOT_READY,
    "resource_cap": S.MeasurementVerdict.BLOCKED_BUDGET,
    "instrument_judge_crash": S.MeasurementVerdict.INVALID_INSTRUMENT,
    "manual_abort": S.MeasurementVerdict.ABORTED_INCOMPLETE,
    "closeout_missing_evidence": S.MeasurementVerdict.CLOSEOUT_INCOMPLETE,
    "freeze_drift": S.MeasurementVerdict.FREEZE_INVALID,
    "protocol_probe_fail": S.MeasurementVerdict.BLOCKED_PROTOCOL,
    "validation_floor_research": S.MeasurementVerdict.RESEARCH_FLOOR,
    "validation_ceiling": S.MeasurementVerdict.CEILING,
    "validation_pilot": S.MeasurementVerdict.PILOT_CALIBRATED,
}

#: 过程轴事件 → 期望 process_verdict。
PROCESS_EXPECTATIONS: dict[str, str] = {
    "unknown_sent_unresolved": S.ProcessVerdict.ABORTED_INCOMPLETE,
    "manual_abort": S.ProcessVerdict.ABORTED_INCOMPLETE,
    "closeout_missing_evidence": S.ProcessVerdict.CLOSEOUT_INCOMPLETE,
    "validation_ceiling": S.ProcessVerdict.COMPLETE,
}


def _new_facts() -> dict[str, Any]:
    """归一化事实模板（全部显式初始化，避免缺键被静默当成「没问题」）。"""
    return {
        "campaign_id": None,
        "consent_present": True,
        "lock_present": True,
        "env_ok": True,
        "model_config_known": True,
        "price_known": True,
        "token_accounting_ok": True,
        "safety_incidents": [],
        "freeze_incidents": [],
        "instrument_incidents": [],
        "protocol": {"ran": True, "ok": True},
        "transport": {"ok": True, "empty_answers": 0},
        "unknown_sent": {"count": 0, "unresolved": 0, "resolved_not_sent": 0,
                         "resolved_with_response": 0},
        "budget": {"blocked": False, "reason": None},
        "context_measurement": {"blocked": False, "reason": None},
        "input_limit": {"blocked_trials": []},
        "aborted": False,
        "closeout": {"required": [], "missing": []},
        "design_not_ready": False,
        "dev": {"families": {}, "failure_trials": []},
        "validation": {"slots": [], "ran": False},
        "ledger": {"rows": 0, "call_rows": 0, "event_rows": 0,
                   "post_rows": 0, "unknown_rows": 0},
        "slots": {"global_used": 0, "posts": 0},
    }


def _triggered(facts: Mapping[str, Any]) -> list[tuple[str, str]]:
    """按 §9.2 收集 (主判决, 细因)。**不排序不裁减**——全部保留后由优先级裁决。"""
    out: list[tuple[str, str]] = []
    mv = S.MeasurementVerdict
    if facts["safety_incidents"]:
        out.append((mv.SAFETY_INVALID, "SAFETY_INCIDENT"))
    if facts["freeze_incidents"]:
        out.append((mv.FREEZE_INVALID, "FREEZE_DRIFT"))
    if facts["instrument_incidents"]:
        out.append((mv.INVALID_INSTRUMENT, "INSTRUMENT_INCIDENT"))
    if not facts["consent_present"]:
        out.append((mv.BLOCKED_APPROVAL, S.ReasonCode.SCHEMA_INVALID))
    if not facts["env_ok"]:
        out.append((mv.BLOCKED_ENV, "ENV_NOT_READY"))
    if not facts["model_config_known"]:
        out.append((mv.BLOCKED_MODEL_CONFIG, "MODEL_CONFIG_UNKNOWN"))
    if not facts["price_known"]:
        out.append((mv.BLOCKED_PRICE, "PRICE_UNKNOWN"))
    if not facts["token_accounting_ok"]:
        out.append((mv.BLOCKED_TOKEN_ACCOUNTING, "TOKEN_ACCOUNTING_UNPROVEN"))
    if facts["context_measurement"].get("blocked"):
        out.append((mv.BLOCKED_CONTEXT_MEASUREMENT, "LOWER_BOUND_UNPROVABLE"))
    if facts["protocol"].get("ran") and not facts["protocol"].get("ok", True):
        out.append((mv.BLOCKED_PROTOCOL, "PROTOCOL_PROBE_FAILED"))
    if not facts["transport"].get("ok", True):
        out.append((mv.BLOCKED_TRANSPORT, "TRANSPORT_BLOCKED"))
    if int(facts["unknown_sent"].get("unresolved") or 0) > 0:
        out.append((mv.BLOCKED_TRANSPORT, S.ReasonCode.UNRESOLVED_SEND))
    if facts["budget"].get("blocked"):
        out.append((mv.BLOCKED_BUDGET, S.ReasonCode.BUDGET_EXCEEDED))
    if facts["input_limit"].get("blocked_trials"):
        out.append((mv.BLOCKED_INPUT_LIMIT, S.ReasonCode.TOKEN_OVER_CAP))
    if facts["aborted"]:
        out.append((mv.ABORTED_INCOMPLETE, "ABORTED"))
    if facts["closeout"].get("missing"):
        out.append((mv.CLOSEOUT_INCOMPLETE, "CLOSEOUT_EVIDENCE_MISSING"))
    if facts["design_not_ready"]:
        out.append((mv.DESIGN_NOT_READY, "STOP_TASK_DESIGN"))
    for fac, verdict in (facts["dev"].get("families") or {}).items():
        if verdict == "FAIL":
            out.append((mv.DEVELOPMENT_NOT_READY, f"DEV_FAIL:{fac}"))
    slots = list(facts["validation"].get("slots") or [])
    if facts["validation"].get("ran") and slots:
        floor = _floor(facts)
        if floor is not None:
            out.append((floor, "VALIDATION_FLOOR"))
        for slot in slots:
            if slot.get("state") == S.TrialState.INVALID:
                out.append((mv.INVALID_INSTRUMENT, f"VALID_SLOT_INVALID:{slot.get('slot')}"))
            if slot.get("state") in (S.TrialState.CANCELLED, S.TrialState.ABORTED):
                out.append((mv.ABORTED_INCOMPLETE, f"VALID_SLOT_CANCELLED:{slot.get('slot')}"))
    return out


def _floor(facts: Mapping[str, Any]) -> str | None:
    """§9.2 第 8 条：六验证 slot 全为有效能力结果时才给地板/天花板。"""
    slots = list(facts["validation"].get("slots") or [])
    if len(slots) != 6:
        return None
    if any(s.get("state") not in S.TrialState.VALID_CAPABILITY for s in slots):
        return None
    passed = {f: 0 for f in S.Family.ALL}
    for s in slots:
        if s.get("state") == S.TrialState.VALID_PASS:
            passed[s["family"]] += 1
    if passed["R"] == 0:
        return S.MeasurementVerdict.RESEARCH_FLOOR
    if passed["E"] == 0 or passed["D"] == 0:
        return S.MeasurementVerdict.ENGINEERING_FLOOR
    if sum(passed.values()) == 6:
        return S.MeasurementVerdict.CEILING
    return S.MeasurementVerdict.PILOT_CALIBRATED


def decide(facts: Mapping[str, Any]) -> dict[str, Any]:
    """主判决（最高优先级）+ 全量 reason_codes + 两轴账。"""
    triggered = _triggered(facts)
    verdicts = [v for v, _ in triggered]
    primary = None
    if verdicts:
        primary = min(verdicts, key=lambda v: S.MeasurementVerdict.PRIORITY[v])
    if primary is None:
        primary = S.MeasurementVerdict.CLOSEOUT_INCOMPLETE
        triggered.append((primary, "NO_TERMINAL_PATH"))
    # 过程轴：aborted/未解决发送 ⇒ ABORTED_INCOMPLETE；缺件 ⇒ CLOSEOUT_INCOMPLETE
    process = S.ProcessVerdict.COMPLETE
    if int(facts["unknown_sent"].get("unresolved") or 0) > 0 or facts["aborted"]:
        process = S.ProcessVerdict.ABORTED_INCOMPLETE
    elif facts["closeout"].get("missing"):
        process = S.ProcessVerdict.CLOSEOUT_INCOMPLETE
    slots = list(facts["validation"].get("slots") or [])
    by_family = {}
    for fam in S.Family.ALL:
        assigned = sum(1 for s in slots if s.get("family") == fam)
        success = sum(1 for s in slots
                      if s.get("family") == fam
                      and s.get("state") == S.TrialState.VALID_PASS)
        effective = sum(1 for s in slots
                        if s.get("family") == fam
                        and s.get("state") in S.TrialState.VALID_CAPABILITY)
        by_family[fam] = {"assigned": assigned, "success": success,
                          "effective": effective,
                          "invalid": [s.get("slot") for s in slots
                                      if s.get("family") == fam
                                      and s.get("state") not in S.TrialState.VALID_CAPABILITY]}
    return {
        "process_verdict": process,
        "measurement_verdict": primary,
        "reason_codes": sorted({code for _, code in triggered}),
        "triggered_verdicts": sorted({v for v, _ in triggered},
                                     key=lambda v: S.MeasurementVerdict.PRIORITY[v]),
        "families": by_family,
        "paid_calls": int(facts["ledger"].get("post_rows") or 0),
        "ledger": dict(facts["ledger"]),
        "slots": dict(facts["slots"]),
        "unknown_sent": dict(facts["unknown_sent"]),
    }


# ---------------------------------------------------------------------------
# 收集（只读；缺件如实登记，不因缺件拒出报告）
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def gather(root: Path) -> dict[str, Any]:
    root = Path(root)
    facts = _new_facts()
    facts["campaign_id"] = root.name
    contract = root / "contract"
    facts["consent_present"] = (contract / "consent.json").is_file()
    env = _read_json(contract / "environment_check.json")
    facts["env_ok"] = bool(env and str(env.get("verdict", "")).upper() == "OK")
    baseline = _read_json(contract / "baseline.json")
    facts["model_config_known"] = bool(baseline and baseline.get("stage_models"))
    facts["price_known"] = bool(_read_json(root / "protocol" / "t5_lock.json")
                                or _read_json(contract / "price_table.json"))
    accounting = _read_json(root / "protocol" / "token_accounting.json")
    facts["token_accounting_ok"] = bool(accounting and accounting.get("proof_ok"))
    protocol = _read_json(root / "protocol" / "t6_protocol.json")
    if protocol is not None:
        facts["protocol"]["ran"] = True
        facts["protocol"]["ok"] = bool(protocol.get("passed"))
    ledger_rows = _rows(root / "quota" / "ledger.jsonl")
    facts["ledger"]["rows"] = len(ledger_rows)
    facts["ledger"]["event_rows"] = sum(1 for r in ledger_rows
                                        if r.get("event") not in (None, "call"))
    facts["ledger"]["call_rows"] = sum(1 for r in ledger_rows
                                       if r.get("event") in (None, "call"))
    facts["ledger"]["post_rows"] = facts["ledger"]["call_rows"]
    facts["ledger"]["unknown_rows"] = sum(
        1 for r in ledger_rows if r.get("status") == "UNKNOWN_SENT"
        or str(r.get("state") or "") == "UNKNOWN_SENT")
    attempts = _rows(root / "quota" / "attempts.jsonl")
    unknown = [a for a in attempts if a.get("state") == "UNKNOWN_SENT"]
    facts["unknown_sent"]["count"] = len(unknown)
    facts["unknown_sent"]["unresolved"] = sum(
        1 for a in unknown if not a.get("resolution"))
    facts["unknown_sent"]["resolved_not_sent"] = sum(
        1 for a in unknown if a.get("resolution") == "not_sent")
    facts["unknown_sent"]["resolved_with_response"] = sum(
        1 for a in unknown if a.get("resolution") == "response")
    slots = _read_json(root / "quota" / "slots.json") or {}
    facts["slots"] = {"global_used": len(slots), "posts": sum(
        int(v.get("posts") or 0) for v in slots.values() if isinstance(v, Mapping))}
    for name in ("t2_design_gate.json",):
        if not (root / "closeout" / name).is_file():
            facts["closeout"]["missing"].append(name)
    trials = root / "trials"
    if trials.is_dir():
        for state_path in sorted(trials.glob("*/state.json")):
            state = _read_json(state_path) or {}
            trial_id = state_path.parent.name
            facts["validation"]["slots"].append({
                "slot": trial_id, "family": state.get("family"),
                "state": state.get("state", S.TrialState.NOT_STARTED)})
            facts["dev"]["families"].setdefault(
                str(state.get("family")), "NOT_RUN")
    return facts


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def build_report(facts: Mapping[str, Any]) -> dict[str, Any]:
    decision = decide(facts)
    return {
        "schema": SCHEMA_CLOSEOUT,
        "campaign_id": facts.get("campaign_id"),
        "created_utc": S.utc_now(),
        "facts": dict(facts),
        "decision": decision,
        "notes": ("离线结案：缺件只登记为 missing，不补造授权；"
                  "paid_calls 只数 ledger 的 call 行，preflight/selfcheck 单列。"),
    }


def write_report(root: Path, facts: Mapping[str, Any] | None = None,
                 *, out: Path | None = None) -> dict[str, Any]:
    root = Path(root)
    report = build_report(facts if facts is not None else gather(root))
    target = Path(out) if out is not None else root / "closeout" / "closeout_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    sha = S.write_canonical_json(target, report)
    return {"report_path": str(target), "sha256": sha,
            "process_verdict": report["decision"]["process_verdict"],
            "measurement_verdict": report["decision"]["measurement_verdict"]}


# ---------------------------------------------------------------------------
# §9.3 fixtures（付费前全部为合成事实，零触网）
# ---------------------------------------------------------------------------


def fixture(event: str) -> dict[str, Any]:
    facts = _new_facts()
    facts["campaign_id"] = f"fixture_{event}"
    mv = S.MeasurementVerdict
    if event == "T0_missing_env":
        facts["env_ok"] = False
    elif event == "T0_missing_approval":
        facts["consent_present"] = False
    elif event == "T0_price_unknown":
        facts["price_known"] = False
    elif event == "T0_token_accounting_missing":
        facts["token_accounting_ok"] = False
    elif event == "STOP_TASK_DESIGN":
        facts["design_not_ready"] = True
    elif event == "long_chain_lower_bound":
        facts["context_measurement"] = {"blocked": True,
                                        "reason": "LOWER_BOUND_UNPROVABLE"}
    elif event == "input_over_cap":
        facts["input_limit"]["blocked_trials"] = ["dev-E"]
    elif event == "policy_violation_trial":
        facts["aborted"] = True
        facts["dev"]["failure_trials"] = ["dev-E"]
    elif event == "safety_breach":
        facts["safety_incidents"] = ["private_truth_mounted"]
    elif event == "empty_answer":
        facts["transport"] = {"ok": False, "empty_answers": 1}
    elif event == "unknown_sent_unresolved":
        facts["unknown_sent"].update({"count": 1, "unresolved": 1})
        facts["transport"]["ok"] = False
    elif event == "unknown_sent_not_sent":
        facts["unknown_sent"].update({"count": 1, "unresolved": 0,
                                      "resolved_not_sent": 1})
        facts["aborted"] = True
    elif event == "unknown_sent_resolved":
        facts["unknown_sent"].update({"count": 1, "unresolved": 0,
                                      "resolved_with_response": 1})
        facts["aborted"] = True
    elif event == "cap_exhausted_dev":
        facts["dev"]["families"] = {"E": "FAIL"}
    elif event == "resource_cap":
        facts["budget"] = {"blocked": True, "reason": "trial_wall_s"}
    elif event == "instrument_judge_crash":
        facts["instrument_incidents"] = ["judge_crash"]
    elif event == "manual_abort":
        facts["aborted"] = True
    elif event == "freeze_drift":
        facts["freeze_incidents"] = ["model_drift_mid_run"]
    elif event == "protocol_probe_fail":
        facts["protocol"] = {"ran": True, "ok": False}
    elif event == "validation_floor_research":
        facts["validation"] = {"ran": True, "slots": [
            {"slot": f"{fam}-v{i}", "family": fam, "state": state}
            for fam, states in (
                ("E", (S.TrialState.VALID_PASS, S.TrialState.VALID_FAIL_CAP)),
                ("D", (S.TrialState.VALID_PASS, S.TrialState.VALID_PASS)),
                ("R", (S.TrialState.VALID_FAIL_CAP, S.TrialState.VALID_FAIL_CAP)),
            ) for i, state in enumerate(states, 1)]}
    elif event == "validation_ceiling":
        facts["validation"] = {"ran": True, "slots": [
            {"slot": f"{fam}-v{i}", "family": fam, "state": S.TrialState.VALID_PASS}
            for fam in S.Family.ALL for i in (1, 2)]}
    elif event == "validation_pilot":
        facts["validation"] = {"ran": True, "slots": [
            {"slot": f"{fam}-v{i}", "family": fam, "state": state}
            for fam, states in (
                ("E", (S.TrialState.VALID_PASS, S.TrialState.VALID_FAIL_CAP)),
                ("D", (S.TrialState.VALID_PASS, S.TrialState.VALID_FAIL_CAP)),
                ("R", (S.TrialState.VALID_PASS, S.TrialState.VALID_FAIL_CAP)),
            ) for i, state in enumerate(states, 1)]}
    elif event == "closeout_missing_evidence":
        facts["closeout"]["missing"] = ["t2_design_gate.json"]
    else:
        raise KeyError(f"未知 fixture 事件 {event!r}")
    return facts


def write_fixtures(out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    problems: list[str] = []
    for event, expected in EVENT_EXPECTATIONS.items():
        facts = fixture(event)
        report = build_report(facts)
        target = out_dir / f"{event}.json"
        S.write_canonical_json(target, report)
        got = report["decision"]["measurement_verdict"]
        ok = got == expected
        if not ok:
            problems.append(f"{event}: 期望 {expected} 实际 {got}")
        proc_expected = PROCESS_EXPECTATIONS.get(event)
        proc_ok = proc_expected is None or report["decision"]["process_verdict"] == proc_expected
        if not proc_ok:
            problems.append(f"{event}: process 期望 {proc_expected} "
                            f"实际 {report['decision']['process_verdict']}")
        results[event] = {"expected": expected, "got": got, "ok": ok,
                          "process_ok": proc_ok,
                          "triggered": report["decision"]["triggered_verdicts"]}
    summary = {"schema": "simpraxis.rsi_r01.closeout_fixtures.v1",
               "events": len(results), "results": results,
               "problems": problems, "all_ok": not problems}
    S.write_canonical_json(out_dir / "summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_root(args: argparse.Namespace) -> Path:
    if args.campaign_root:
        root = Path(args.campaign_root)
    else:
        root = DEFAULT_ROOT / str(args.campaign_id)
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 离线结案器（不触网）")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--campaign-root", default=None)
    ap.add_argument("--out", default=None, help="报告写到别处（默认 closeout/）")
    ap.add_argument("--write-fixtures", default=None,
                    help="生成 §9.3 全部 fixture 报告目录并自判")
    args = ap.parse_args(argv)
    if args.write_fixtures:
        summary = write_fixtures(Path(args.write_fixtures))
        print(json.dumps({"events": summary["events"], "all_ok": summary["all_ok"],
                          "problems": summary["problems"]}, ensure_ascii=False, indent=1))
        return 0 if summary["all_ok"] else 3
    if not args.campaign_id and not args.campaign_root:
        ap.error("需要 --campaign-id 或 --campaign-root（或用 --write-fixtures）")
    root = resolve_root(args)
    out = write_report(root, out=Path(args.out) if args.out else None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
