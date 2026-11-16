#!/usr/bin/env python3
"""RSI-G2.1 AC 记录表（Plan §4：每 AC 记 nodeid / rc / 实现 SHA / 报告 hash）。

用法::

    python scripts/rsi_g21/ac_report.py --out <g21root>/reports/ac_report.json \
        [--evidence-root <g21root>] [--skip-static]

只跑 G2.1 专项离线单测 + legacy/G2 定向 golden + 静态门；**零网络/零模型**。
带 ``--evidence-root`` 时把该根里已跑出的 isolation/closeout 报告 sha 记进证据
（隔离容器探针按 Plan §4 显式单独执行，不在本脚本内跑）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

G21 = "tests/test_rsi_g21_"
MAPPING: "dict[str, dict]" = {
    "G21-01": {"nodeids": [f"{G21}bridge.py::test_materialize_copies_are_byte_identical",
                           f"{G21}bridge.py::test_context_refuses_path_traversal",
                           f"{G21}bridge.py::test_import_rejects_unbound_artifact_reply"],
               "evidence": "G2.1 根与旧族根隔离由 rsi_g21_roots_isolated 守卫"},
    "G21-02": {"nodeids": [f"{G21}submit_e2e.py::test_synthetic_complete_study_reaches_verified"],
               "evidence": "trace/<study>/events.jsonl 落原件；gateway body 缺失记 MISSING"},
    "G21-03": {"nodeids": [f"{G21}bridge.py::test_source_readiness_detects_missing_required_contrast",
                           f"{G21}submit_e2e.py::test_synthetic_complete_study_reaches_verified"],
               "evidence": "真实封存 A 缺 negative_torsional ⇒ SOURCE_EVIDENCE_INCOMPLETE（blocked）；合成完整 NC 仅验代码路径"},
    "G21-04": {"nodeids": [f"{G21}compat.py::test_legacy_clipping_is_byte_identical",
                           f"{G21}compat.py::test_dispatcher_task_contrast_flag_defaults_off"]},
    "G21-05": {"nodeids": [f"{G21}submit_e2e.py::test_synthetic_complete_study_reaches_verified",
                           f"{G21}submit_e2e.py::test_five_faces_detect_tampered_reply_bytes",
                           f"{G21}submit_e2e.py::test_bridge_records_consumer_label_and_resources"]},
    "G21-06": {"nodeids": [f"{G21}bridge.py::test_placeholder_source_sha_is_refused",
                           f"{G21}bridge.py::test_source_readiness_detects_missing_required_contrast"]},
    "G21-07": {"nodeids": [f"{G21}compat.py::test_read_action_field_validation",
                           f"{G21}compat.py::test_read_page_is_atomic_under_g2_policy",
                           "tests/test_rsi_g2_contract.py::test_read_page_multipage_and_errors",
                           "tests/test_rsi_g2_contract.py::test_read_page_refuses_cursor_inside_multibyte_char",
                           "tests/test_rsi_g2_visibility.py::test_read_paths_multipage_and_submit_without_confirm_rerun"]},
    "G21-08": {"nodeids": [f"{G21}raw_checker.py::test_recompute_matches_candidate_and_axes",
                           f"{G21}raw_checker.py::test_registered_mutations_match_expectation",
                           f"{G21}raw_checker.py::test_tampered_raw_is_rejected"]},
    "G21-09": {"nodeids": [f"{G21}raw_checker.py::test_recompute_matches_candidate_and_axes",
                           f"{G21}raw_checker.py::test_scope_unit_drift_is_detected"]},
    "G21-10": {"nodeids": [f"{G21}bridge.py::test_source_readiness_detects_missing_required_contrast",
                           f"{G21}compat.py::test_dispatcher_task_contrast_flag_defaults_off"]},
    "G21-11": {"nodeids": [f"{G21}capacity.py::test_tokenizer_contract_is_the_approved_artifact"]},
    "G21-12": {"nodeids": [f"{G21}capacity.py::test_capacity_matrix_all_fixtures",
                           f"{G21}capacity.py::test_capacity_matrix_entries_carry_frozen_limits"]},
    "G21-13": {"nodeids": [f"{G21}capacity.py::test_capacity_matrix_all_fixtures"]},
    "G21-14": {"nodeids": [f"{G21}isolation.py::test_probe_semantics_pass_and_fail",
                           f"{G21}isolation.py::test_docker_argv_is_hardened_and_cpu_only"],
               "evidence": "容器探针显式单独执行（bridge-isolation）；报告 sha 记入本次 evidence"},
    "G21-15": {"nodeids": [f"{G21}bridge.py::test_signature_freshness_detects_implementation_drift"]},
    "G21-16": {"nodeids": ["tests/test_rsi_r1_transport.py",
                           "tests/test_rsi_r01_transport.py",
                           "tests/test_rsi_r1_loop.py",
                           "tests/test_rsi_g2_contract.py",
                           "tests/test_rsi_g2_visibility.py",
                           "tests/test_rsi_g2_scope.py",
                           "tests/test_rsi_g2_compat.py",
                           "tests/test_rsi_g2_cli.py"]},
    "G21-17": {"nodeids": [f"{G21}isolation.py::test_closeout_never_claims_ready_without_evidence"]},
    "G21-18": {"nodeids": [], "evidence": "静态门 rc + 专项套全绿；无 skip/xfail/.only/TODO"},
    "G21-19": {"nodeids": [f"{G21}isolation.py::test_closeout_never_claims_ready_without_evidence",
                           f"{G21}submit_e2e.py::test_bridge_records_consumer_label_and_resources"]},
    "G21-20": {"nodeids": [f"{G21}submit_e2e.py::test_synthetic_complete_study_reaches_verified"]},
    "G21-21": {"nodeids": [f"{G21}bridge.py::test_envelope_pointers_survive_contrast_permutation"]},
    "G21-22": {"nodeids": [f"{G21}bridge.py::test_source_readiness_detects_missing_required_contrast",
                           f"{G21}submit_e2e.py::test_synthetic_complete_study_reaches_verified"]},
    "G21-23": {"nodeids": [f"{G21}isolation.py::test_probe_semantics_pass_and_fail"]},
    "G21-24": {"nodeids": [f"{G21}raw_checker.py::test_tampered_raw_is_rejected",
                           f"{G21}submit_e2e.py::test_closure_checker_rejects_self_asserted_ready"]},
}

MODULES = ("orchestrator/rsi_r1/g21_bridge.py",
           "orchestrator/rsi_r1/g21_raw_checker.py",
           "orchestrator/rsi_r1/g2_contract.py",
           "orchestrator/rsi_r1/g2_cli.py",
           "orchestrator/rsi_r1/assembly.py",
           "orchestrator/rsi_r1/loop.py",
           "orchestrator/rsi_r1/state.py",
           "orchestrator/common/research_transport.py",
           "orchestrator/common/runtime_paths.py",
           "tests/fixtures/g21_synthetic.py",
           "tests/fixtures/g21_consumer/consumer.py",
           "scripts/verification/check_rsi_g21_closure.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True)
    return proc.stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-static", action="store_true")
    parser.add_argument("--evidence-root")
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
        records["G21-18"]["rc"] = static_rc
        records["G21-18"]["tail"] = proc.stdout.strip().splitlines()[-1] \
            if proc.stdout.strip() else ""
    evidence = {}
    if args.evidence_root:
        root = Path(args.evidence_root)
        for rel in ("isolation/report.json", "decision/g21_closeout.json",
                    "raw_check/report.json", "capacity/report.json"):
            path = root / rel
            evidence[rel] = {"exists": path.is_file(),
                             "sha256": _sha(path) if path.is_file() else ""}
    doc = {"schema": "simpraxis.rsi_g21.ac_report.v1", "schema_version": 1,
           "git_head": _git("rev-parse", "HEAD"),
           "clean_tree": not _git("status", "--porcelain"),
           "module_shas": {rel: _sha(REPO / rel) for rel in MODULES},
           "static_gate_rc": static_rc,
           "records": records,
           "evidence": evidence,
           "n_ok": sum(1 for rec in records.values() if rec["rc"] == 0),
           "n_total": len(records)}
    doc["ok"] = doc["n_ok"] == doc["n_total"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)
    print(json.dumps({"ok": doc["ok"], "n_ok": doc["n_ok"],
                      "n_total": doc["n_total"],
                      "report_sha256": hashlib.sha256(
                          text.encode("utf-8")).hexdigest(),
                      "git_head": doc["git_head"],
                      "clean_tree": doc["clean_tree"]},
                     ensure_ascii=False, sort_keys=True))
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
