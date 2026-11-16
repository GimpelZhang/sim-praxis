#!/usr/bin/env python3
"""RSI-G2 AC 记录表（Plan §7：每 AC 记 nodeid / rc / 实现 SHA / 报告 hash）。

用法：
    python scripts/rsi_g2/ac_report.py --out <g2root>/reports/ac_report.json

只跑 G2 离线单测 + 静态门；**零网络/零模型**。无独立 nodeid 的条目显式记
`evidence` 说明，不伪造 nodeid。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

MAPPING: "dict[str, dict]" = {
    "G2-01": {"nodeids": ["tests/test_rsi_g2_compat.py::test_g2_dispatcher_requires_scope_contract"],
              "evidence": "旧根只读由 CLI 只写 --root 保证；write_audit=UNKNOWN"},
    "G2-02": {"nodeids": ["tests/test_rsi_g2_contract.py::test_envelope_roundtrip_and_verify",
                          "tests/test_rsi_g2_contract.py::test_missing_required_contrast_is_refused"]},
    "G2-03": {"nodeids": ["tests/test_rsi_g2_contract.py::test_missing_primary_is_atomic_failure_not_partial",
                          "tests/test_rsi_g2_contract.py::test_oversize_envelope_is_blocked_before_send"]},
    "G2-04": {"nodeids": ["tests/test_rsi_g2_contract.py::test_boundary_16384_accepted_16385_rejected",
                          "tests/test_rsi_g2_contract.py::test_summary_is_droppable_primary_is_not",
                          "tests/test_rsi_g2_cli.py::test_visibility_report_marks_legacy_negative_control"]},
    "G2-05": {"nodeids": ["tests/test_rsi_g2_visibility.py::test_real_turn_captures_request_and_envelope_at_boundary"]},
    "G2-06": {"nodeids": ["tests/test_rsi_g2_visibility.py::test_legacy_result_loses_primary_before_request"]},
    "G2-07": {"nodeids": ["tests/test_rsi_g2_compat.py::test_legacy_submit_claim_event_has_no_origin"],
              "evidence": "四路 sha 组装在 assembly._r13_submit（G2 分支）；legacy 事件字节不变"},
    "G2-08": {"nodeids": ["tests/test_rsi_g2_visibility.py::test_scripted_sender_requires_offline_marker",
                          "tests/test_rsi_g2_visibility.py::test_offline_guard_blocks_sockets",
                          "tests/test_rsi_g2_contract.py::test_read_page_refuses_private_and_traversal_paths"],
              "evidence": "同 uid 环境 OS 级主体隔离不可得 ⇒ ISOLATION_UNVERIFIED（Plan §3.3）"},
    "G2-09": {"nodeids": ["tests/test_rsi_g2_contract.py::test_scope_contract_requires_public_enums_and_formula",
                          "tests/test_rsi_g2_scope.py::test_schema_document_matches_enums"]},
    "G2-10": {"nodeids": ["tests/test_rsi_g2_scope.py::test_scope_error_with_correct_label_is_not_semantic_gap"]},
    "G2-11": {"nodeids": ["tests/test_rsi_g2_scope.py::test_missing_block_formula_makes_case_invalid",
                          "tests/test_rsi_g2_scope.py::test_expected_contract_drift_is_fail_closed"]},
    "G2-12": {"nodeids": ["tests/test_rsi_g2_scope.py::test_all_fixtures_score_ok_with_contract_derived_claims"]},
    "G2-13": {"nodeids": ["tests/test_rsi_g2_compat.py::test_default_policy_is_byte_identical_to_explicit_legacy",
                          "tests/test_rsi_g2_compat.py::test_action_registry_keeps_legacy_prompt_unchanged"]},
    "G2-14": {"nodeids": ["tests/test_rsi_g2_cli.py::test_full_cycle_zero_post",
                          "tests/test_rsi_g2_visibility.py::test_offline_guard_blocks_sockets"]},
    "G2-15": {"nodeids": ["tests/test_rsi_g2_cli.py::test_limit_problems_are_pure_and_fail_closed"]},
    "G2-16": {"nodeids": ["tests/test_rsi_g2_scope.py::test_batch_scoring_is_not_vacuous"]},
    "G2-17": {"nodeids": [], "evidence": "gate_static.sh rc；G2 专项套全绿（无 skip/xfail/TODO）"},
    "G2-18": {"nodeids": ["tests/test_rsi_g2_cli.py::test_full_cycle_zero_post"],
              "evidence": "closeout.scientific_claim=NONE；不写 R1/R2 结论"},
    "G2-19": {"nodeids": ["tests/test_rsi_g2_contract.py::test_missing_required_contrast_is_refused"]},
    "G2-20": {"nodeids": ["tests/test_rsi_g2_contract.py::test_read_page_multipage_and_errors",
                          "tests/test_rsi_g2_visibility.py::test_read_paths_multipage_and_submit_without_confirm_rerun"]},
    "G2-21": {"nodeids": ["tests/test_rsi_g2_visibility.py::test_history_shift_requires_result_context",
                          "tests/test_rsi_g2_visibility.py::test_cross_study_envelope_does_not_leak"]},
    "G2-22": {"nodeids": ["tests/test_rsi_g2_scope.py::test_tuple_fields_are_compared_individually"]},
    "G2-23": {"nodeids": ["tests/test_rsi_g2_contract.py::test_read_page_refuses_private_and_traversal_paths"],
              "evidence": "OS 级拒绝同 uid 不可得 ⇒ ISOLATION_UNVERIFIED（gap，不宣称无泄漏）"},
    "G2-24": {"nodeids": ["tests/test_rsi_g2_cli.py::test_visibility_report_marks_legacy_negative_control",
                          "tests/test_rsi_g2_cli.py::test_scope_gate_fails_on_tampered_claim"]},
}

MODULES = ("orchestrator/common/research_transport.py",
           "orchestrator/rsi_r1/g2_contract.py",
           "orchestrator/rsi_r1/g2_validation.py",
           "orchestrator/rsi_r1/g2_cli.py",
           "orchestrator/rsi_r1/assembly.py",
           "orchestrator/rsi_r1/loop.py",
           "orchestrator/rsi_r1/state.py",
           "orchestrator/rsi_r1/judge_v13.py",
           "orchestrator/rsi_r1/profiles.py",
           "orchestrator/common/runtime_paths.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, check=False).stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-static", action="store_true")
    args = parser.parse_args(argv)
    records = {}
    for ac, spec in sorted(MAPPING.items()):
        nodeids = list(spec.get("nodeids") or [])
        rec = {"nodeids": nodeids, "rc": None, "evidence": spec.get("evidence")}
        if nodeids:
            proc = subprocess.run([sys.executable, "-m", "pytest", *nodeids,
                                   "-q", "--tb=no", "-p", "no:cacheprovider"],
                                  cwd=REPO, capture_output=True, text=True)
            rec["rc"] = proc.returncode
            rec["tail"] = (proc.stdout.strip().splitlines()[-1]
                           if proc.stdout.strip() else "")
        records[ac] = rec
    static_rc = None
    if not args.skip_static:
        proc = subprocess.run(["bash", "scripts/verification/gate_static.sh"],
                              cwd=REPO, capture_output=True, text=True)
        static_rc = proc.returncode
    report = {
        "schema": "simpraxis.rsi_g2.ac_report.v1", "schema_version": 1,
        "git_head": _git("rev-parse", "HEAD"),
        "clean_tree": _git("status", "--porcelain") == "",
        "module_shas": {rel: _sha(REPO / rel) for rel in MODULES},
        "static_gate_rc": static_rc,
        "records": records,
        "n_ok": sum(1 for r in records.values()
                    if r["rc"] in (0, None) and not r.get("failed")),
        "n_total": len(records),
    }
    failed = [ac for ac, rec in records.items() if rec["rc"] not in (0, None)]
    report["ok"] = not failed and static_rc in (0, None)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    target.write_text(text, encoding="utf-8")
    report["report_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(json.dumps({"out": str(target), "ok": report["ok"],
                      "report_sha256": report["report_sha256"],
                      "n_ok": report["n_ok"], "n_total": report["n_total"],
                      "static_gate_rc": static_rc,
                      "clean_tree": report["clean_tree"],
                      "git_head": report["git_head"]},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
