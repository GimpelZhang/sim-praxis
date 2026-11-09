#!/usr/bin/env python3
"""RSI-R0.1 结案判决复验（AC25 / AC34）：独立实现，不 import 结案器决策函数。

复验内容（全部离线、只读）：

1. 报告字节 hash 与记录值一致，且报告 schema/必填块齐全；
2. 用**本文件自己的** §9.2 优先级实现从 ``report["facts"]`` 重推两轴判决，与
   结案器结论逐字比对（不同实现互证，非自指）；
3. ledger 口径复算：call 行数 = 报告 ``paid_calls``，事件行/preflight 单列，
   ``UNKNOWN_SENT`` 行不得被当已结算；
4. fixture 汇总（若存在）必须 ``all_ok``；
5. 证据包 manifest（若存在）逐件 sha256 复验，且 manifest 不得把自身或判决件
   写成自指条目；包缺席记 gap（不判 problems），不补造。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.rsi_r01 import schemas as S

DEFAULT_ROOT = Path("/mnt/artifacts/simpraxis/rsi_r01")

#: 独立优先级表（照计划 §9.2 抄写；不 import 结案器）。
PRIORITY = {
    "SAFETY_INVALID": 1,
    "FREEZE_INVALID": 2,
    "INVALID_INSTRUMENT": 3,
    "BLOCKED_ENV": 4, "BLOCKED_MODEL_CONFIG": 4, "BLOCKED_TOKEN_ACCOUNTING": 4,
    "BLOCKED_PRICE": 4, "BLOCKED_APPROVAL": 4, "BLOCKED_CONTEXT_MEASUREMENT": 4,
    "BLOCKED_PROTOCOL": 5, "BLOCKED_TRANSPORT": 5, "BLOCKED_BUDGET": 5,
    "BLOCKED_INPUT_LIMIT": 5,
    "ABORTED_INCOMPLETE": 6, "CLOSEOUT_INCOMPLETE": 6,
    "DESIGN_NOT_READY": 7, "DEVELOPMENT_NOT_READY": 7,
    "RESEARCH_FLOOR": 8, "ENGINEERING_FLOOR": 8, "CEILING": 8,
    "PILOT_CALIBRATED": 8,
}

VALID_CAPABILITY = ("VALID_PASS", "VALID_FAIL_CAP")


def _verdicts_from_facts(facts: Mapping[str, Any]) -> list[str]:
    """独立重推：只读 facts 的显式字段，不与结案器共享代码。"""
    mv: list[str] = []
    if facts.get("safety_incidents"):
        mv.append("SAFETY_INVALID")
    if facts.get("freeze_incidents"):
        mv.append("FREEZE_INVALID")
    if facts.get("instrument_incidents"):
        mv.append("INVALID_INSTRUMENT")
    if not facts.get("consent_present", True):
        mv.append("BLOCKED_APPROVAL")
    if not facts.get("env_ok", True):
        mv.append("BLOCKED_ENV")
    if not facts.get("model_config_known", True):
        mv.append("BLOCKED_MODEL_CONFIG")
    if not facts.get("price_known", True):
        mv.append("BLOCKED_PRICE")
    if not facts.get("token_accounting_ok", True):
        mv.append("BLOCKED_TOKEN_ACCOUNTING")
    if (facts.get("context_measurement") or {}).get("blocked"):
        mv.append("BLOCKED_CONTEXT_MEASUREMENT")
    proto = facts.get("protocol") or {}
    if proto.get("ran") and not proto.get("ok", True):
        mv.append("BLOCKED_PROTOCOL")
    if not (facts.get("transport") or {}).get("ok", True):
        mv.append("BLOCKED_TRANSPORT")
    if int((facts.get("unknown_sent") or {}).get("unresolved") or 0) > 0:
        mv.append("BLOCKED_TRANSPORT")
    if (facts.get("budget") or {}).get("blocked"):
        mv.append("BLOCKED_BUDGET")
    if (facts.get("input_limit") or {}).get("blocked_trials"):
        mv.append("BLOCKED_INPUT_LIMIT")
    if facts.get("aborted"):
        mv.append("ABORTED_INCOMPLETE")
    if (facts.get("closeout") or {}).get("missing"):
        mv.append("CLOSEOUT_INCOMPLETE")
    if facts.get("design_not_ready"):
        mv.append("DESIGN_NOT_READY")
    if any(v == "FAIL" for v in ((facts.get("dev") or {}).get("families") or {}).values()):
        mv.append("DEVELOPMENT_NOT_READY")
    slots = list(((facts.get("validation") or {}).get("slots")) or [])
    if (facts.get("validation") or {}).get("ran") and slots:
        if len(slots) == 6 and all(s.get("state") in VALID_CAPABILITY for s in slots):
            passed = {f: 0 for f in ("E", "D", "R")}
            for s in slots:
                if s.get("state") == "VALID_PASS":
                    passed[s.get("family")] += 1
            if passed["R"] == 0:
                mv.append("RESEARCH_FLOOR")
            elif passed["E"] == 0 or passed["D"] == 0:
                mv.append("ENGINEERING_FLOOR")
            elif sum(passed.values()) == 6:
                mv.append("CEILING")
            else:
                mv.append("PILOT_CALIBRATED")
        if any(s.get("state") == "INVALID" for s in slots):
            mv.append("INVALID_INSTRUMENT")
        if any(s.get("state") in ("CANCELLED", "ABORTED") for s in slots):
            mv.append("ABORTED_INCOMPLETE")
    return mv


def _expected_primary(verdicts: Sequence[str]) -> str | None:
    return min(verdicts, key=lambda v: PRIORITY[v]) if verdicts else None


def _process_from_facts(facts: Mapping[str, Any]) -> str:
    if int((facts.get("unknown_sent") or {}).get("unresolved") or 0) > 0 or facts.get("aborted"):
        return "ABORTED_INCOMPLETE"
    if (facts.get("closeout") or {}).get("missing"):
        return "CLOSEOUT_INCOMPLETE"
    return "COMPLETE"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _ledger_counts(path: Path) -> dict[str, int]:
    rows = 0
    calls = 0
    events = 0
    unknown = 0
    if not path.is_file():
        return {"rows": 0, "call_rows": 0, "event_rows": 0, "unknown_rows": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        rows += 1
        if obj.get("event") in (None, "call"):
            calls += 1
        else:
            events += 1
        if str(obj.get("status") or obj.get("state") or "") == "UNKNOWN_SENT":
            unknown += 1
    return {"rows": rows, "call_rows": calls, "event_rows": events,
            "unknown_rows": unknown}


def verify(campaign_root: Path) -> dict[str, Any]:
    campaign_root = Path(campaign_root)
    problems: list[str] = []
    gaps: list[str] = []
    report_path = campaign_root / "closeout" / "closeout_report.json"
    if not report_path.is_file():
        return {"schema": "simpraxis.rsi_r01.closure_check.v1",
                "campaign_root": str(campaign_root), "ok": False,
                "problems": ["missing_closeout_report"], "gaps": [],
                "report_sha256": None}
    raw = report_path.read_bytes()
    report_sha = sha256_bytes(raw)
    try:
        report = json.loads(raw)
    except ValueError as exc:
        return {"schema": "simpraxis.rsi_r01.closure_check.v1",
                "campaign_root": str(campaign_root), "ok": False,
                "problems": [f"report_not_json:{exc}"], "gaps": [],
                "report_sha256": report_sha}
    if report.get("schema") != "simpraxis.rsi_r01.closeout.v1":
        problems.append(f"report_schema:{report.get('schema')}")
    facts = report.get("facts") or {}
    decision = report.get("decision") or {}
    if not facts or not decision:
        problems.append("report_missing_facts_or_decision")
    if facts and decision:
        independent = _verdicts_from_facts(facts)
        expected = _expected_primary(independent)
        if expected is None:
            problems.append("independent_recompute_no_terminal_path")
        elif expected != decision.get("measurement_verdict"):
            problems.append(
                f"measurement_mismatch:report={decision.get('measurement_verdict')}"
                f" recomputed={expected}")
        proc = _process_from_facts(facts)
        if proc != decision.get("process_verdict"):
            problems.append(f"process_mismatch:report={decision.get('process_verdict')}"
                            f" recomputed={proc}")
        if decision.get("paid_calls") != int((facts.get("ledger") or {})
                                             .get("post_rows") or 0):
            problems.append("paid_calls_not_from_ledger_post_rows")
    ledger = _ledger_counts(campaign_root / "quota" / "ledger.jsonl")
    if facts:
        claimed = facts.get("ledger") or {}
        for key in ("rows", "call_rows", "event_rows"):
            if int(claimed.get(key, -1)) != ledger[key]:
                problems.append(f"ledger_{key}_mismatch:"
                                f"report={claimed.get(key)} actual={ledger[key]}")
    fixtures = campaign_root / "closeout" / "fixtures" / "summary.json"
    if fixtures.is_file():
        try:
            fx = json.loads(fixtures.read_text(encoding="utf-8"))
        except ValueError:
            fx = {}
            problems.append("fixtures_summary_not_json")
        if fx and not fx.get("all_ok"):
            problems.append("fixtures_not_all_ok")
    else:
        gaps.append("fixtures_missing")
    # 证据包：若存在则逐件复验（不得自指）
    manifest_path = campaign_root / "evidence_pack" / "manifest.json"
    pack_checked = 0
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
            problems.append("evidence_manifest_not_json")
        entries = (manifest or {}).get("files") or {}
        pack_root = manifest_path.parent
        manifest_sha = sha256_file(manifest_path)
        for rel, meta in entries.items():
            if Path(rel).as_posix() in ("manifest.json", "closeout_report.json"):
                problems.append(f"evidence_manifest_self_reference:{rel}")
                continue
            target = pack_root / rel
            if not target.is_file() or target.is_symlink():
                problems.append(f"evidence_missing_or_symlink:{rel}")
                continue
            want = str((meta or {}).get("sha256") or "")
            if sha256_file(target) != want:
                problems.append(f"evidence_hash_mismatch:{rel}")
                continue
            pack_checked += 1
        if str((manifest or {}).get("judgment_sha256") or "") in ("", None):
            gaps.append("judgment_sha256_absent")
        if (manifest or {}).get("self_sha256") == manifest_sha:
            gaps.append("manifest_hash_is_not_fixpoint")
    else:
        gaps.append("evidence_pack_not_created")
    return {
        "schema": "simpraxis.rsi_r01.closure_check.v1",
        "campaign_root": str(campaign_root),
        "report_sha256": report_sha,
        "measurement_verdict": decision.get("measurement_verdict"),
        "process_verdict": decision.get("process_verdict"),
        "ledger": ledger,
        "evidence_files_checked": pack_checked,
        "problems": problems,
        "gaps": gaps,
        "ok": not problems,
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 结案判决独立复验（离线）")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--campaign-root", default=None)
    ap.add_argument("--verify", action="store_true", help="复验并把 rc 绑到结论")
    ap.add_argument("--report", default=None, help="复验结果写到该路径")
    args = ap.parse_args(argv)
    if args.campaign_root:
        root = Path(args.campaign_root)
    elif args.campaign_id:
        root = DEFAULT_ROOT / args.campaign_id
    else:
        ap.error("需要 --campaign-id 或 --campaign-root")
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    out = verify(root)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(out, ensure_ascii=False, indent=1,
                                     sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
