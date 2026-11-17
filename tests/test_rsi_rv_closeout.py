"""RV-19..RV-24：结案件聚合、NOT_RUN 保留、24 槽分母与不越权晋升。"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.rsi_r1 import rv_cli as CLI
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_runtime as RT
from tests.fixtures import rv_synthetic as RS


def _root(tmp_path: Path) -> Path:
    return RS.build_initialized_root(tmp_path)


def _write_study(root: Path, study: str, *, state: str,
                 review: str = "REVIEW_PASS", gates=None) -> None:
    base = {"source_valid": True, "actual_worker_isolated": True,
            "request_fidelity": True, "candidate_origin": True,
            "binding": True, "report_evidence": True,
            "review_verdict": review == "REVIEW_PASS",
            "numeric_fidelity": True}
    if gates:
        base.update(gates)
    RV.write_json_private(root / "studies" / study / "result.json",
                          {"schema": f"{RV.SCHEMA_PREFIX}.study_result.v1",
                           "schema_version": 1, "study": study,
                           "state": state, "gates": base,
                           "problems": [] if state == "MODEL_REPORT_VALID"
                           else ["fixture_failure"]})
    RV.write_json_private(root / "review" / f"final_{study}.json",
                          {"schema": f"{RV.SCHEMA_PREFIX}.final_review.v1",
                           "schema_version": 1, "study": study,
                           "verdict": review, "items": {}, "problems": []})


def test_closeout_feasible_keeps_all_slots(tmp_path):
    root = _root(tmp_path)
    _write_study(root, "A", state="MODEL_REPORT_VALID")
    _write_study(root, "B", state="MODEL_REPORT_VALID")
    RV.write_json_private(root / "offline" / "report.json",
                          {"isolation": {"ok": True}})
    assert CLI.main(["closeout", "--root", str(root)]) == 0
    doc = RV.read_json(root / "closeout" / "rv_closeout.json")
    assert doc["overall"] == "MODEL_SUFFIX_FEASIBLE_ON_GIVEN_EVIDENCE"
    assert doc["slots"] == {lane: {"cap": RV.RV_LANE_POST_CAPS[lane], "used": 0}
                            for lane in RV.RV_LANE_ORDER}
    assert sum(row["cap"] for row in doc["slots"].values()) == 24
    assert doc["ledger"]["n_calls"] == 0
    assert doc["notes"]


def test_closeout_not_established_preserves_per_study_failure(tmp_path):
    root = _root(tmp_path)
    _write_study(root, "A", state="MODEL_FAILED",
                 gates={"binding": False, "review_verdict": False})
    _write_study(root, "B", state="MODEL_REPORT_VALID")
    RV.write_json_private(root / "offline" / "report.json",
                          {"isolation": {"ok": True}})
    assert CLI.main(["closeout", "--root", str(root)]) == 0
    doc = RV.read_json(root / "closeout" / "rv_closeout.json")
    assert doc["overall"] == "MODEL_SUFFIX_NOT_ESTABLISHED"
    assert doc["studies"]["A"]["state"] == "MODEL_FAILED"
    assert doc["studies"]["B"]["state"] == "MODEL_REPORT_VALID"


def test_closeout_missing_results_is_not_measured_and_keeps_not_run(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["closeout", "--root", str(root)]) == 0
    doc = RV.read_json(root / "closeout" / "rv_closeout.json")
    # 两份都未启动 ⇒ 优先级 3：NOT_MEASURED（不是靠空集合 all() 晋升）
    assert doc["overall"] == "NOT_MEASURED"
    assert doc["studies"]["A"]["state"] == "NOT_RUN"
    assert doc["studies"]["A"]["review_verdict"] == "REVIEW_NOT_RUN"


def test_closeout_review_failure_blocks_feasible(tmp_path):
    root = _root(tmp_path)
    _write_study(root, "A", state="MODEL_REPORT_VALID", review="REVIEW_FAIL")
    _write_study(root, "B", state="MODEL_REPORT_VALID")
    RV.write_json_private(root / "offline" / "report.json",
                          {"isolation": {"ok": True}})
    assert CLI.main(["closeout", "--root", str(root)]) == 0
    doc = RV.read_json(root / "closeout" / "rv_closeout.json")
    assert doc["overall"] == "MODEL_SUFFIX_NOT_ESTABLISHED"


def test_closeout_requires_two_studies(tmp_path):
    root = _root(tmp_path)
    _write_study(root, "A", state="MODEL_REPORT_VALID")
    RV.write_json_private(root / "offline" / "report.json",
                          {"isolation": {"ok": True}})
    assert CLI.main(["closeout", "--root", str(root)]) == 0
    doc = RV.read_json(root / "closeout" / "rv_closeout.json")
    # 一份有效报告 + 一份 NOT_RUN ⇒ 优先级 5（缺一个合格案例），不是 NOT_MEASURED
    assert doc["overall"] == "MODEL_SUFFIX_NOT_ESTABLISHED"
    assert doc["studies"]["B"]["state"] == "NOT_RUN"


def test_gates_require_claim_match_and_real_container_reads():
    from types import SimpleNamespace
    from orchestrator.rsi_r1 import rv_cli as CLI

    class Ev:
        def __init__(self, event, payload):
            self.event = event
            self.payload = payload

    class Journal:
        def events(self):
            return [Ev("action", {"response_id": "r1", "name": "submit_claim"})]

    class Reader:
        records = [{"container_id": "cid", "host_pid": 123}]

    state = SimpleNamespace(journal=Journal())
    parts = SimpleNamespace(reader=SimpleNamespace(reader=Reader()))
    claim = SimpleNamespace(payload={"response_id": "r1",
                                     "producer": RT.RV_PRODUCER})
    lineage = {"intents": [{"request_sha256": "a" * 64}],
               "results": [{"resolved": True, "response_id": "r1",
                            "attempts_http": 1,
                            "max_tokens": RV.RV_OUTPUT_TOKENS}]}
    layers = {"receipt_valid": True, "numerics_valid": True,
              "evidence_valid": True, "diagnostics_valid": True,
              "claim_status": "MATCH", "report_valid": True}
    judge = {"verdict": "JUDGE_OK", "layers": layers}
    gates = CLI._gates(study="A", outcome={"stopped": "SUBMITTED"},
                       faces={}, judge=judge, lineage=lineage, claim=claim,
                       state=state, parts=parts, source_valid=True)
    assert all(v for k, v in gates.items() if k != "review_verdict")
    # claim 不匹配 ⇒ binding 失败（RV-16）
    bad = dict(layers, claim_status="CLAIM_INVALID")
    gates = CLI._gates(study="A", outcome={}, faces={},
                       judge={"verdict": "JUDGE_OK", "layers": bad},
                       lineage=lineage, claim=claim, state=state,
                       parts=parts, source_valid=True)
    assert gates["binding"] is False
    # 没有真实容器实例 ⇒ 隔离门失败
    Reader.records = []
    gates = CLI._gates(study="A", outcome={}, faces={}, judge=judge,
                       lineage=lineage, claim=claim, state=state,
                       parts=parts, source_valid=True)
    assert gates["actual_worker_isolated"] is False
    # 未解析/漏 intent ⇒ 请求保真失败
    Reader.records = [{"container_id": "cid", "host_pid": 123}]
    gates = CLI._gates(study="A", outcome={}, faces={}, judge=judge,
                       lineage={"intents": [], "results": lineage["results"]},
                       claim=claim, state=state, parts=parts,
                       source_valid=True)
    assert gates["request_fidelity"] is False


def test_packet_error_makes_review_instrument_blocked(tmp_path, monkeypatch):
    from tests.fixtures import rv_synthetic as RS
    from orchestrator.rsi_r1 import rv_review as RVW
    from orchestrator.rsi_r1 import rv_runtime as RT
    root = RS.build_initialized_root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    prereg = RV.read_json(root / "contract" / "prereg.json")
    RT.materialize_rv_study(root, manifest, "A")
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = RVW._case_report("valid_positive", base)
    receipt_sha = str((base.get("analysis_receipt_public") or {}).get(
        "output_sha256"))
    report["limitations"][0]["evidence_refs"][0]["pointer"] = "/nope"
    RV.write_json_private(root / "studies" / "A" / "result.json", {
        "schema": f"{RV.SCHEMA_PREFIX}.study_result.v1", "schema_version": 1,
        "study": "A", "state": "MODEL_REPORT_VALID", "gates": {}, "problems": []})
    RV.write_json_private(root / "submit" / "A" / "report.json", {
        "schema": f"{RV.SCHEMA_PREFIX}.sealed_report.v1", "schema_version": 1,
        "study": "A", "report": report, "report_text": "正文",
        "lineage": {"producer": RT.RV_PRODUCER,
                    "analysis_receipt_sha": receipt_sha}})
    permit = RV.build_permit(
        round_id=root.name, kind="suffix_and_review", prereg=prereg,
        closure_sha256="a" * 64, worker_policy_sha="b" * 64,
        prompt_sha="c" * 64, schema_sha="d" * 64, tokenizer_sha="e" * 64,
        rubric_sha="f" * 64, config_sha="1" * 64,
        calibration_report_sha="2" * 64)
    RV.write_json_private(root / "contract" / "permit_suffix.json", permit)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert CLI.main(["review", "--root", str(root), "--study", "A"]) == 1
    doc = RV.read_json(root / "review" / "final_A.json")
    assert doc["verdict"] == "INSTRUMENT_BLOCKED"
    assert not (root / "quota" / "ledger.jsonl").exists()
    assert CLI.main(["closeout", "--root", str(root)]) == 1
    close = RV.read_json(root / "closeout" / "rv_closeout.json")
    assert close["overall"] == "INSTRUMENT_BLOCKED"
