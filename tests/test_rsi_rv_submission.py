"""RV-06/RV-08/RV-10/RV-11：零 POST 脚本正控与负控、producer 归因。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import rv_cli as CLI
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_runtime as RT
from tests.fixtures import rv_synthetic as RS


def _root(tmp_path: Path) -> Path:
    return RS.build_initialized_root(tmp_path)


def test_offline_positive_control_reaches_verified(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["validate-offline", "--root", str(root)]) == 0
    report = RV.read_json(root / "offline" / "report.json")
    assert report["ok"] is True, report["problems"]
    assert report["isolation"]["ok"] is True
    assert report["negative_controls"] == {
        "wrong_sha": "ARTIFACT_SHA_MISMATCH",
        "wrong_study": "PACKET_STUDY_MISMATCH",
        "missing_nc": "PACKET_NC_MISMATCH"}
    for study in ("A", "B"):
        judge = RV.read_json(root / "offline_studies" / study /
                             "judge" / f"report_{study}.json")
        assert judge["verdict"] == "JUDGE_OK"
        assert judge["layers"]["claim_status"] == "MATCH"
        assert report["studies"][study]["five_faces_ok"] is True


def test_scripted_claim_is_not_counted_as_candidate(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["validate-offline", "--root", str(root)]) == 0
    for study in ("A", "B"):
        events = [json.loads(line) for line in
                  (root / "offline_studies" / study / "state" / "journal.jsonl")
                  .read_text().splitlines() if line.strip()]
        claims = [e for e in events if e["event"] == "claim"]
        assert claims, study
        assert claims[-1]["payload"]["producer"] == RT.SCRIPTED_PRODUCER
        assert claims[-1]["payload"]["producer"] != RT.RV_PRODUCER


def test_wrong_receipt_in_report_is_rejected_by_structure(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["validate-offline", "--root", str(root)]) == 0
    # 取脚本正控产生的报告，篡改 receipt sha 后重跑结构校验（不重跑链路）。
    from orchestrator.rsi_r1 import task_specs as TS
    study = "A"
    material_root = root / "offline_studies" / study
    action = None
    for line in (material_root / "state" / "journal.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["event"] == "action" and event["payload"].get("name") == "submit_claim":
            action = event["payload"]["payload"]
    assert action is not None
    report = dict(action["research_report"])
    report["primary_results"] = [dict(report["primary_results"][0],
                                      analysis_receipt_sha="0" * 64)]
    task = TS.TaskSpecV13.from_dict(RV.read_json(root / "public" / study / "task.json"))
    evidence = {"analysis/output.json":
                (root / "public" / study / "analysis_output.json").read_bytes()}
    with pytest.raises(RR.ReportError) as exc:
        RR.validate_report(
            report, task=task,
            receipts={}, evidence=evidence,
            candidate_origin={"producer": "candidate",
                              "reply_sha256": "a" * 64, "assisted": False},
            expected_reply_sha256="a" * 64)
    assert exc.value.code == "RECEIPT_MISSING"


def test_quiet_reads_go_through_container_worker(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["validate-offline", "--root", str(root)]) == 0
    logs = sorted((root / "worker_offline" / "A").glob("read-*.json"))
    assert logs
    for path in logs:
        record = RV.read_json(path)
        assert record["container_id"]
        assert record["host_pid"] > 0
        assert record["reader_sha256"] == RT.W.READER_SHA256
        assert record["request"]["page_bytes"] <= RV.PAGE_BYTES_RV


def test_g2_read_page_still_the_reference_for_same_bytes(tmp_path):
    root = _root(tmp_path)
    data = (root / "public" / "A" / "analysis_output.json").read_bytes()
    sha = RV.sha_bytes(data)
    registry = G2.ArtifactRegistry([{
        "artifact_id": "analysis/output.json",
        "relative_path": "analysis/A/output.json", "sha256": sha,
        "bytes": len(data), "access_mode": "read_result_artifact"}])
    page = G2.read_page(registry=registry, read_bytes=lambda rel: data,
                        artifact_id="analysis/output.json",
                        output_sha256=sha, cursor=0, page_bytes=8192)
    assert page["total_bytes"] == len(data)
    assert page["next_cursor"] is None or page["next_cursor"] <= len(data)


def test_submit_without_mandatory_read_is_rejected(tmp_path):
    root = _root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    prereg = RV.read_json(root / "contract" / "prereg.json")
    material = RT.materialize_rv_study(root, manifest, "A")
    entry = manifest["studies"]["A"]
    primary = material["task_doc"]["primary"]
    consumer = RT.ScriptedRVConsumer(
        study="A", receipt_sha=entry["receipt_sha"],
        task_id=str(material["task_doc"]["task_id"]),
        primary_id=str(primary["id"]),
        required=entry["required_contrast_ids"],
        hypotheses=[str(h["id"]) for h in material["plan_doc"]["hypotheses"]],
        metric_id=str(primary["metric"]), delta=float(primary["delta"]),
        proposition=str(primary["proposition"]), mandatory=())
    budget = RT.RVBudget(envelope_id=root.name,
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget, prefix="offline")
    parts = RT.build_rv_parts(
        root=root, study="A", manifest=manifest, material=material,
        prereg=prereg, budget=budget, lanes=lanes, ledger=type(
            "L", (), {"_read_rows": lambda self: []})(),
        permits={}, closure_reader=lambda: {},
        public_verifier=lambda: {}, sender=consumer,
        lane="suffix_A", policy=RT.W.WorkerPolicy(), scripted=True)
    outcome = RT.run_turns(parts, max_turns=2)
    assert outcome["turns"][0]["code"] == "MANDATORY_READ_MISSING"
    assert outcome["study_state"] == "RESULTS_RELEASED"


def test_non_rv_action_is_terminal(tmp_path):
    root = _root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    prereg = RV.read_json(root / "contract" / "prereg.json")
    material = RT.materialize_rv_study(root, manifest, "A")

    class ForbiddenConsumer:
        OFFLINE = True

        def send_once(self, request, reservation):
            return L.Reply(text=json.dumps(
                {"action": "write_artifact", "kind": "design",
                 "source": "print('x')"}), response_id="forbidden-1")

    from orchestrator.rsi_r1 import loop as L
    budget = RT.RVBudget(envelope_id=root.name,
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget, prefix="offline")
    parts = RT.build_rv_parts(
        root=root, study="A", manifest=manifest, material=material,
        prereg=prereg, budget=budget, lanes=lanes, ledger=type(
            "L", (), {"_read_rows": lambda self: []})(),
        permits={}, closure_reader=lambda: {}, public_verifier=lambda: {},
        sender=ForbiddenConsumer(), lane="suffix_A",
        policy=RT.W.WorkerPolicy(), scripted=True)
    outcome = RT.run_turns(parts, max_turns=3)
    # v3 动作层：白名单在解析层执行，越界动作是 ACTION_FORBIDDEN（终态）
    assert outcome["turns"][0]["code"] == "ACTION_FORBIDDEN"
    assert outcome["turns"][0]["terminal"] is True
    assert outcome["study_state"] == "RESULTS_RELEASED"


def test_offline_validation_does_not_touch_real_study_roots(tmp_path):
    root = _root(tmp_path)
    assert CLI.main(["validate-offline", "--root", str(root)]) == 0
    # 离线预演必须只写 offline_studies/，真实 studies/ 保持缺席（未材料化）。
    assert not (root / "studies" / "A" / "state" / "journal.jsonl").exists()
    assert (root / "offline_studies" / "A" / "state" / "journal.jsonl").is_file()
    assert (root / "worker_offline" / "A").is_dir()


def test_envelope_registers_full_public_artifact_set(tmp_path):
    root = _root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    material = RT.materialize_rv_study(root, manifest, "A")
    from orchestrator.rsi_r1 import g2_contract as G2
    from orchestrator.rsi_r1 import task_specs as TS
    scope = G2.load_scope_contract(root / "public" / "A" / "scope_contract.json")
    envelope = RT.build_rv_envelope(
        study="A", plan_sha256=material["plan"].sha256,
        output_bytes=material["output_bytes"],
        receipt_bytes=material["receipt_bytes"],
        output_rel=material["candidate_output_rel"],
        receipt_rel=material["candidate_receipt_rel"], scope=scope,
        task=material["task"], public_root=root / "public")
    ids = {a["artifact_id"] for a in envelope["artifacts"]}
    for name in RT.RVE_EXTRA_ARTIFACTS:
        assert f"analysis/{name}" in ids
        entry = next(a for a in envelope["artifacts"]
                     if a["artifact_id"] == f"analysis/{name}")
        data = (root / "public" / "A" / name).read_bytes()
        assert entry["sha256"] == RV.sha_bytes(data)
        assert entry["bytes"] == len(data)
    G2.verify_envelope(envelope, scope_contract=scope)


def test_judge_evidence_map_includes_published_artifacts(tmp_path):
    root = _root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    prereg = RV.read_json(root / "contract" / "prereg.json")
    material = RT.materialize_rv_study(root, manifest, "A")
    budget = RT.RVBudget(envelope_id=root.name,
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget, prefix="offline")
    parts = RT.build_rv_parts(
        root=root, study="A", manifest=manifest, material=material,
        prereg=prereg, budget=budget, lanes=lanes, ledger=type(
            "L", (), {"_read_rows": lambda self: []})(),
        permits={}, closure_reader=lambda: {}, public_verifier=lambda: {},
        sender=object(), lane="suffix_A", policy=RT.W.WorkerPolicy(),
        scripted=True)
    evidence = parts.dispatcher._r13_evidence(
        material["plan"], parts.dispatcher.confirm_manifest or
        parts.dispatcher._frozen_manifest_raw(), material["output_bytes"],
        material["receipt_bytes"])
    for name in RT.RVE_EXTRA_ARTIFACTS:
        assert f"analysis/{name}" in evidence
        data = (root / "public" / "A" / name).read_bytes()
        assert evidence[f"analysis/{name}"] == data


def test_precommit_rejects_real_b_reply_without_sealing(tmp_path):
    import json
    from pathlib import Path
    from orchestrator.rsi_r1 import loop as L
    root = _root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    prereg = RV.read_json(root / "contract" / "prereg.json")
    material = RT.materialize_rv_study(root, manifest, "B")
    budget = RT.RVBudget(envelope_id=root.name,
                         posts_by_lane=dict(RV.RV_LANE_POST_CAPS))
    lanes = RT.RVSlotBook(RT.RVContext(root), budget, prefix="offline")
    parts = RT.build_rv_parts(
        root=root, study="B", manifest=manifest, material=material,
        prereg=prereg, budget=budget, lanes=lanes, ledger=type(
            "L", (), {"_read_rows": lambda self: []})(),
        permits={}, closure_reader=lambda: {}, public_verifier=lambda: {},
        sender=object(), lane="suffix_B", policy=RT.W.WorkerPolicy(),
        scripted=True)
    fixture = json.loads((Path("tests/fixtures") /
                          "rv_b_real_report_20261116.json").read_text())
    doc = {"action": "submit_claim", "claims": fixture["claims"],
           "report_text": fixture["report_text"],
           "research_report": fixture["research_report"]}
    parts.store.record_reply(response_id="rv-real-b", text="x",
                             phase="explore", nonce="1234567890abcdef")
    parts.store.record_action(response_id="rv-real-b", action=doc)
    action = L.parse_action(json.dumps(doc))
    result = parts.dispatcher.execute_once(action, parts.store)
    assert result["code"] == "REPORT_PRECOMMIT_INVALID"
    assert result["side_effect_status"] == "NONE"
    claims = [e for e in parts.state.journal.events() if e.event == "claim"]
    assert claims == []
    assert parts.state.study_state("B") == "RESULTS_RELEASED"
