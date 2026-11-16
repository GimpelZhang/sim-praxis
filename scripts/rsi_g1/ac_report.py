#!/usr/bin/env python3
"""RSI-G1 AC 记录表（Plan §9：每 AC 记 nodeid / rc / 报告 hash / 实现 SHA）。

用法：
    python scripts/rsi_g1/ac_report.py --out <g1root>/decision/ac_report.json

只跑 G1 的离线单测 + 静态门；**零网络/零模型**。无独立 nodeid 的条目显式记
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
    "G1-01": {"nodeids": ["tests/test_rsi_g1_trace.py::test_inventory_rejects_symlink_and_caps"],
              "note": "write_audit=UNKNOWN；窗口内由 verify-inputs 复核"},
    "G1-02": {"nodeids": ["tests/test_rsi_g1_trace.py::test_inventory_rejects_symlink_and_caps",
                          "tests/test_rsi_g1_trace.py::test_trace_missing_analysis_is_missing_not_empty_hash"]},
    "G1-03": {"nodeids": ["tests/test_rsi_g1_trace.py::test_trace_chain_and_verify",
                          "tests/test_rsi_g1_capture.py::test_capture_demo_records_four_boundaries"]},
    "G1-04": {"nodeids": ["tests/test_rsi_g1_trace.py::test_tampered_event_is_detected"]},
    "G1-05": {"nodeids": ["tests/test_rsi_g1_claim_compiler.py::test_all_nine_cases_match_expectations",
                          "tests/test_rsi_g1_claim_compiler.py::test_rounded_values_are_rejected_not_silently_overwritten"]},
    "G1-06": {"nodeids": ["tests/test_rsi_g1_claim_compiler.py::test_compiler_module_never_imports_judge_or_oracle",
                          "tests/test_rsi_g1_claim_compiler.py::test_compiler_copies_wrong_candidate_value_verbatim",
                          "tests/test_rsi_g1_claim_compiler.py::test_store_refuses_forbidden_and_traversal_paths",
                          "tests/test_rsi_g1_capture.py::test_maintenance_store_is_rejected_by_compiler"]},
    "G1-07": {"nodeids": ["tests/test_rsi_g1_capture.py::test_capture_shows_candidate_output_lost_before_next_request",
                          "tests/test_rsi_g1_trace.py::test_small_result_keeps_candidate_output_and_flags_view_branch"]},
    "G1-08": {"nodeids": ["tests/test_rsi_g1_packet_audit.py"]},
    "G1-09": {"nodeids": [], "evidence": "cmd_packet_audit 分写 historical_/repaired_；只读旧根"},
    "G1-10": {"nodeids": ["tests/test_rsi_g1_closeout.py::test_sidecar_ands_studies_and_keeps_three_fields",
                          "tests/test_rsi_g1_closeout.py::test_empty_study_set_is_not_vacuously_true"]},
    "G1-11": {"nodeids": ["tests/test_rsi_g1_closeout.py::test_refuted_and_inconclusive_are_not_relabelled"]},
    "G1-12": {"nodeids": ["tests/test_rsi_g1_claim_compiler.py::test_refuted_and_inconclusive_labels_are_not_structural_errors"]},
    "G1-13": {"nodeids": ["tests/test_rsi_g1_cli.py::test_delivery_test_cli_writes_results"]},
    "G1-14": {"nodeids": ["tests/test_rsi_g1_cli.py::test_cli_has_no_study_or_review_subcommands",
                          "tests/test_rsi_g1_cli.py::test_probe_refuses_without_approval_or_paid_env",
                          "tests/test_rsi_g1_capture.py::test_a_layer_guard_blocks_sender"]},
    "G1-15": {"nodeids": [], "evidence": "跨轮只读登记（a3 140 件）；无跨轮写入 ⇒ 导入链未构造"},
    "G1-16": {"nodeids": ["tests/test_rsi_g1_closeout.py::test_three_axis_matrix_reports_evidence_not_single_label"]},
    "G1-17": {"nodeids": ["tests/test_rsi_g1_oss_notes.py"]},
    "G1-18": {"nodeids": [], "evidence": "gate_static.sh rc；G1 专项套全绿"},
    "G1-19": {"nodeids": ["tests/test_rsi_g1_closeout.py::test_sidecar_ands_studies_and_keeps_three_fields"]},
    "G1-20": {"nodeids": [], "evidence": "无新 R1 入口；probe 只服务 B 诊断"},
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True, check=False).stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    records = {}
    for ac, spec in MAPPING.items():
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
    gate = subprocess.run(["bash", "scripts/verification/gate_static.sh"],
                          cwd=REPO, capture_output=True, text=True)
    doc = {
        "schema": "simpraxis.rsi_g1.ac_report.v1", "schema_version": 1,
        "impl_sha": _git("rev-parse", "HEAD"),
        "clean_tree": _git("status", "--porcelain") == "",
        "module_shas": {name: _sha(REPO / "orchestrator/rsi_r1" / f"{name}.py")
                        for name in ("g1_trace", "g1_delivery", "g1_packet_audit",
                                     "g1_decision", "g1_cli", "g1_probe",
                                     "g1_capture")},
        "static_gate_rc": gate.returncode,
        "acs": records,
        "failed": [ac for ac, r in records.items() if r["rc"] not in (None, 0)],
    }
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)
    doc["report_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    print(json.dumps({"failed": doc["failed"], "static_gate_rc": gate.returncode,
                      "impl_sha": doc["impl_sha"],
                      "report_sha256": doc["report_sha256"]}, ensure_ascii=False))
    return 0 if not doc["failed"] and gate.returncode == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
