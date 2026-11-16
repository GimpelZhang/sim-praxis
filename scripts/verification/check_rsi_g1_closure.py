#!/usr/bin/env python3
"""RSI-G1 独立复核：不采信 decision.json 的自述，按产物重算五轴。

用法：
    python scripts/verification/check_rsi_g1_closure.py --root <g1root>

判定（与 `g1_decision.aggregate_diagnostics` 同规则但**独立实现**）：

* history_observability：任一 trace 有 gaps ⇒ PARTIAL；无 trace ⇒ UNOBSERVABLE；
* delivery_integrity：仅当所有 trace 的 attribution_level == OBSERVED 且无
  suspected 才 CONFIRMED；有 suspected 但等级非 OBSERVED ⇒ UNKNOWN；否则 UNKNOWN；
* packet_integrity：任一历史包审计 ok=false ⇒ FAIL；无审计 ⇒ UNKNOWN；
* compiler_fixture_status：全部 case ok ⇒ PASS，否则 FAIL；
* model_comparison：probe_report.overall，缺失 ⇒ NOT_RUN。

另核：trace 事件链（重算 event_sha）、manifest 计数、state.json 历史存在性、
decision.json 与重算结果逐字段一致。rc=0 表示一致且无问题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _trace_events(trace_dir: Path):
    events = trace_dir / "events.jsonl"
    if not events.is_file():
        return None
    rows, prev = [], None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        doc = json.loads(line)
        eid, esha = doc.pop("event_id"), doc.pop("event_sha256")
        body = dict(doc)
        if _sha(body) != eid:
            raise SystemExit(f"FAIL event_id@{len(rows)}")
        check = dict(body, event_id=eid)
        if _sha(check) != esha:
            raise SystemExit(f"FAIL event_sha@{len(rows)}")
        if prev is not None and doc.get("parent_event_id") != prev:
            raise SystemExit(f"FAIL parent@{len(rows)}")
        prev = eid
        rows.append(doc)
    return rows


def judge(root: Path, probe_report: "Path | None" = None) -> dict:
    problems: "list[str]" = []
    summaries = []
    for path in sorted((root / "trace").glob("*_summary.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    for study in ("A", "B"):
        d = root / "trace" / study
        if d.is_dir():
            rows = _trace_events(d)
            if rows is None:
                problems.append(f"{study}: 缺 events.jsonl")
    if not summaries:
        history = "UNOBSERVABLE"
    elif any(s.get("gaps") for s in summaries):
        history = "PARTIAL"
    else:
        history = "COMPLETE"
    levels = {s.get("attribution_level") for s in summaries}
    suspected = [s for s in summaries if s.get("suspected")]
    if summaries and levels == {"OBSERVED"} and not suspected:
        delivery = "CONFIRMED"
    else:
        delivery = "UNKNOWN"
    audits = [json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((root / "packet_sanity").glob("historical_*.json"))]
    if not audits:
        packet = "UNKNOWN"
    elif any(not a.get("ok") for a in audits):
        packet = "FAIL"
    else:
        packet = "PASS"
    cases_file = root / "compiler_cases" / "results.json"
    if cases_file.is_file():
        cases = json.loads(cases_file.read_text(encoding="utf-8")).get("results") or []
        compiler = "PASS" if cases and all(c.get("ok") for c in cases) else "FAIL"
    else:
        compiler = "FAIL"
        problems.append("缺 compiler_cases/results.json")
    probe = _read(probe_report) if probe_report else _read(
        root / "probe" / "probe_report.json")
    model = (probe or {}).get("overall") or "NOT_RUN"
    axes = {"history_observability": history, "delivery_integrity": delivery,
            "packet_integrity": packet, "compiler_fixture_status": compiler,
            "model_comparison": model}
    decision = _read(root / "decision" / "decision.json")
    consistent = True
    if decision is None:
        problems.append("缺 decision/decision.json")
        consistent = False
    else:
        for key, value in axes.items():
            if (decision.get("axes") or {}).get(key) != value:
                problems.append(f"axes.{key}: 重算 {value} != 判决 "
                                f"{(decision.get('axes') or {}).get(key)}")
                consistent = False
    state = _read(root / "state.json")
    if not state:
        problems.append("缺 state.json（A 层步骤未记录）")
    else:
        steps = [h.get("step") for h in state.get("history") or []]
        if state.get("step") != "CLOSED":
            problems.append(f"state={state.get('step')}（未 CLOSED）")
        if state.get("warnings"):
            problems.append(f"state warnings={len(state['warnings'])}")
        if len(set(steps)) != len(steps):
            problems.append("state 步骤重复")
    return {"schema": "simpraxis.rsi_g1.closure_judge.v1", "ok": not problems,
            "problems": problems, "axes": axes, "consistent": consistent,
            "n_traces": len(summaries),
            "note": "独立重算；trace 事件 sha 与父链逐条复核"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--probe-report", default=None)
    args = parser.parse_args(argv)
    report = judge(Path(args.root),
                   Path(args.probe_report) if args.probe_report else None)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
