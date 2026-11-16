#!/usr/bin/env python3
"""RSI-G2 独立复核：不采信 closeout 自述，按产物重算工程轴。

用法：
    python scripts/verification/check_rsi_g2_closure.py --root <g2root> \
        [--compat-report <ac_report.json>]

判定（与 ``g2_cli.cmd_closeout`` 同规则但**独立实现**，只用标准库）：

* content_integrity：成功形状必须 envelope 逐字在场；阻断形状必须
  ``RESULT_VIEW_INCOMPLETE``；legacy 负控必须丢主结果；
* visibility_observability：至少一个形状在最终 request 中带 envelope；
* scope_contract：逐 case 按**轴**重算分类，且合同快照自洽；
* binding_numeric：全部 OK case 的 binding/numeric 均为真；
* legacy_compat：compat 报告对应 AC 记录 rc=0（缺报告 ⇒ UNKNOWN）；
* isolation：``ISOLATION_UNVERIFIED`` 记 gap，不宣称无泄漏。

rc=0 表示 problems 为空（gaps 单独列出）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(obj) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _visibility_axes(report) -> "tuple[str, str, list[str]]":
    problems: "list[str]" = []
    if not report:
        return "UNKNOWN", "UNKNOWN", ["缺 visibility/report.json"]
    observed = False
    for row in report.get("results") or []:
        expected = row.get("expected_outcome")
        if expected == "SUCCESS":
            if row.get("outcome") != "SUCCESS" or not row.get("envelope_in_request"):
                problems.append(f"{row.get('case_id')}: 成功形状未带 envelope")
            else:
                observed = True
        elif expected == "BLOCK_BEFORE_SEND":
            if row.get("outcome") != "BLOCK_BEFORE_SEND" or \
                    row.get("code") != "RESULT_VIEW_INCOMPLETE":
                problems.append(f"{row.get('case_id')}: 阻断形状未阻断")
        elif expected == "INPUT_LIMIT_BEFORE_SEND":
            if row.get("outcome") != "INPUT_LIMIT_BEFORE_SEND":
                problems.append(f"{row.get('case_id')}: 未在发网前 INPUT_LIMIT")
        elif expected == "EXPECTED_LEGACY_FAILURE":
            if row.get("primary_marker_present") is not False:
                problems.append(f"{row.get('case_id')}: legacy 负控未复现丢字段")
    reads = report.get("read_paths") or {}
    if not reads.get("reassembled_ok"):
        problems.append("read_paths: 多页重组失败")
    if reads.get("context_occurrences") != 1:
        problems.append("read_paths: result_context 未恰好一次")
    if reads.get("confirm_rerun"):
        problems.append("read_paths: 读后提交触发了 confirm 重跑")
    content = "PASS" if not problems else "FAIL"
    history = "PASS" if observed else "UNKNOWN"
    return content, history, problems


def _scope_axes(report, contract_doc) -> "tuple[str, str, list[str]]":
    problems: "list[str]" = []
    if not report:
        return "UNKNOWN", "UNKNOWN", ["缺 scope/report.json"]
    for row in report.get("results") or []:
        cls = row.get("classification")
        if cls in ("CASE_INVALID", "CLAIM_MISSING"):
            continue
        ok = (row.get("parse_ok") and row.get("binding_valid")
              and row.get("numeric_valid") and row.get("tuple_match")
              and row.get("scope_valid") and row.get("semantic_label_valid")
              and row.get("semantic_match")
              and row.get("evidence_coverage_valid"))
        want = "OK" if ok else cls
        if cls != want:
            problems.append(f"{row.get('case_id')}: 分类 {cls} != 重算 {want}")
        if cls == "SCOPE_CONTRACT_MISMATCH" and not (
                row.get("semantic_match") and not row.get("scope_valid")):
            problems.append(f"{row.get('case_id')}: scope 错与语义错未分离")
    snapshot = contract_doc
    if not snapshot:
        problems.append("缺 contract/g2_contract.json")
    else:
        want = _sha({k: v for k, v in snapshot.items()
                     if k != "contract_sha256"})
        if snapshot.get("contract_sha256") != want:
            problems.append("contract_sha256 不符")
        if (snapshot.get("limits") or {}).get("envelope_max_bytes") != 16384:
            problems.append("envelope 上限漂移")
    scope = "PASS" if not problems else "FAIL"
    binding = scope
    return scope, binding, problems


def _compat_axis(ac) -> "tuple[str, list[str]]":
    if not ac:
        return "UNKNOWN", []
    records = ac.get("records") or {}
    if "G2-13" not in records:
        return "UNKNOWN", []
    if records["G2-13"].get("rc") == 0:
        return "PASS", []
    return "FAIL", ["G2-13 兼容回归未过"]


def judge(root: Path, *, compat_report: "Path | None" = None) -> dict:
    problems: "list[str]" = []
    gaps: "list[str]" = []
    visibility = _read(root / "visibility" / "report.json")
    scope = _read(root / "scope" / "report.json")
    isolation = _read(root / "isolation" / "report.json")
    contract = _read(root / "contract" / "g2_contract.json")
    decision = _read(root / "closeout" / "g2_closeout.json")
    ac = _read(compat_report) if compat_report else \
        _read(root / "reports" / "ac_report.json")
    content, history, p1 = _visibility_axes(visibility)
    problems.extend(p1)
    scope_axis, binding, p2 = _scope_axes(scope, contract)
    problems.extend(p2)
    legacy, p3 = _compat_axis(ac)
    problems.extend(p3)
    iso = (isolation or {}).get("status", "UNKNOWN")
    if iso == "ISOLATION_UNVERIFIED":
        gaps.append("ISOLATION_UNVERIFIED")
    elif iso != "ISOLATION_OK":
        gaps.append(f"ISOLATION:{iso}")
    axes = {"content_integrity": content,
            "visibility_observability": history,
            "scope_contract": scope_axis,
            "binding_numeric": binding,
            "legacy_compat": legacy,
            "isolation": iso}
    consistent = True
    if decision is None:
        problems.append("缺 closeout/g2_closeout.json")
        consistent = False
    else:
        for key, value in axes.items():
            got = (decision.get("axes") or {}).get(key)
            if got != value:
                problems.append(f"axes.{key}: 重算 {value} != 判决 {got}")
                consistent = False
        expected_gaps = ([g for g in gaps if g == "ISOLATION_UNVERIFIED"]
                         + [f"UNKNOWN:{k}" for k, v in axes.items()
                            if v not in ("PASS", "ISOLATION_UNVERIFIED",
                                         "ISOLATION_OK")])
        if sorted(decision.get("gaps") or []) != sorted(expected_gaps):
            problems.append("gaps 与重算不一致")
    return {"schema": "simpraxis.rsi_g2.closure_judge.v1",
            "ok": not problems, "problems": problems, "gaps": gaps,
            "axes": axes, "consistent": consistent,
            "read_paths_ok": bool((visibility or {}).get("read_paths", {})
                                  .get("ok")),
            "note": "独立重算；不采信 closeout 自述"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--compat-report", default=None)
    args = parser.parse_args(argv)
    report = judge(Path(args.root),
                   compat_report=Path(args.compat_report)
                   if args.compat_report else None)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
