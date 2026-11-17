"""RV-13/RV-14/RV-15：审查包自足性、D10–D13 负控与两通道合并规则。"""
from __future__ import annotations

import copy
import json

import pytest

from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_review as RVW
from orchestrator.rsi_r1 import rv_runtime as RT
from tests.fixtures import rv_synthetic as RS


def _prepared(tmp_path):
    root = RS.build_initialized_root(tmp_path)
    manifest = RV.read_json(root / "manifest" / "rv_sources.json")
    for study in ("A", "B"):
        RT.materialize_rv_study(root, manifest, study)
    return root


def test_packet_is_self_contained(tmp_path):
    root = _prepared(tmp_path)
    for study in ("A", "B"):
        packet = RVW.build_rv_packet(root=root, study=study, report=None)
        assert packet["study"] == study
        assert len(packet["block_table"]) == 32
        assert all(row["nuisance"] is not None for row in packet["block_table"])
        required = packet["required_contrast_ids"]
        assert set(required) <= set(packet["raw_recomputation"]["contrasts"])
        assert packet["analysis_receipt_public"]["validity"] is True
        output = RV.read_json(root / "public" / study / "analysis_output.json")
        primary = output["contrasts"][0]
        recompute = packet["raw_recomputation"]["contrasts"][primary["contrast_id"]]
        assert abs(recompute["estimate"] - primary["estimate"]) < 1e-9
        assert packet["packet_body_sha256"]
    a = RVW.build_rv_packet(root=root, study="A", report=None)
    assert "negative_torsional" in a["required_contrast_ids"]
    b = RVW.build_rv_packet(root=root, study="B", report=None)
    assert b["required_contrast_ids"] == ["tc_x_inertia"]


def test_d10_wrong_study_packet_is_rejected(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    broken = dict(packet, study="B")
    broken.pop("packet_body_sha256", None)
    broken["packet_body_sha256"] = RV.canonical_sha(broken)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_packet(broken, expected_study="A")
    assert exc.value.code == "PACKET_STUDY_MISMATCH"


def test_d11_metric_drift_is_rejected(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    broken = copy.deepcopy(packet)
    broken["scope"]["metric_id"] = "stop_time_s"
    broken.pop("packet_body_sha256", None)
    broken["packet_body_sha256"] = RV.canonical_sha(broken)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_packet(broken, expected_study="A")
    assert exc.value.code == "PACKET_METRIC_MISMATCH"


def test_d12_missing_nuisance_is_rejected(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    broken = copy.deepcopy(packet)
    broken["block_table"][0]["nuisance"] = None
    broken.pop("packet_body_sha256", None)
    broken["packet_body_sha256"] = RV.canonical_sha(broken)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_packet(broken, expected_study="A")
    assert exc.value.code == "PACKET_NUISANCE_MISSING"


def test_d13_missing_required_contrast_is_rejected(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    broken = copy.deepcopy(packet)
    broken["required_contrast_ids"] = ["friction_x_speed"]
    broken.pop("packet_body_sha256", None)
    broken["packet_body_sha256"] = RV.canonical_sha(broken)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_packet(broken, expected_study="A")
    assert exc.value.code in ("PACKET_NC_MISMATCH", "PACKET_NC_MISSING")


def test_d13_ci_level_drift_is_rejected(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    broken = copy.deepcopy(packet)
    broken["scope"]["interval_level"] = 0.9
    broken.pop("packet_body_sha256", None)
    broken["packet_body_sha256"] = RV.canonical_sha(broken)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_packet(broken, expected_study="A")
    assert exc.value.code == "PACKET_CI_MISMATCH"


def _items(verdicts: dict) -> dict:
    return {dim: {"verdict": verdicts.get(dim, "PASS"),
                  "reason": "fixture", "refs": []}
            for dim in RV.RUBRIC_DIMS}


def test_merge_rules_are_fixed_and_total():
    all_pass = RV.merge_review_channels(
        [{"items": _items({})}, {"items": _items({})}])
    assert all_pass["verdict"] == "REVIEW_PASS"
    both_fail = RV.merge_review_channels(
        [{"items": _items({"claim_scope": "FAIL"})},
         {"items": _items({"claim_scope": "FAIL"})}])
    assert both_fail["verdict"] == "REVIEW_FAIL"
    split = RV.merge_review_channels(
        [{"items": _items({"claim_scope": "FAIL"})},
         {"items": _items({"claim_scope": "PASS"})}])
    assert split["verdict"] == "REVIEW_INCONCLUSIVE"
    unver = RV.merge_review_channels(
        [{"items": _items({"claim_scope": "UNVERIFIABLE"})},
         {"items": _items({})}])
    assert unver["verdict"] == "REVIEW_UNVERIFIABLE"
    missing = RV.merge_review_channels([{"items": {}}, {"items": _items({})}])
    assert missing["verdict"] == "REVIEW_UNVERIFIABLE"


def test_validate_review_requires_all_dims_and_resolvable_refs(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    good = {"schema": RV.REVIEW_SCHEMA, "schema_version": 1,
            "review_id": "r1", "reviewer_id": "e1",
            "packet_sha256": packet["packet_body_sha256"],
            "items": {dim: {"verdict": "PASS", "reason": "ok",
                            "refs": [{"json_pointer": "/block_table/0/values"}]}
                      for dim in RV.RUBRIC_DIMS}}
    parsed = RVW.validate_review(good, packet=packet, review_id="r1",
                                 reviewer_id="e1")
    assert set(parsed["items"]) == set(RV.RUBRIC_DIMS)
    bad = copy.deepcopy(good)
    del bad["items"][RV.RUBRIC_DIMS[0]]
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_review(bad, packet=packet, review_id="r1",
                            reviewer_id="e1")
    assert exc.value.code == "REVIEW_ITEMS"
    bad = copy.deepcopy(good)
    bad["items"][RV.RUBRIC_DIMS[0]]["refs"] = [{"json_pointer": "/nope"}]
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.validate_review(bad, packet=packet, review_id="r1",
                            reviewer_id="e1")
    assert exc.value.code == "REVIEW_REF_UNRESOLVED"


def test_calibration_expected_is_sealed_and_checks_critical_failures():
    sealed = RVW.sealed_expected()
    assert sealed["sealed_sha256"]
    assert set(sealed["cases"]) == set(RVW.CALIBRATION_CASES)
    # 正确槽：有效案例全 PASS；关键案例 critical 维 FAIL。
    slots = []
    for study in ("A", "B"):
        for channel in (1, 2):
            cases = {}
            for cid, spec in RVW.CALIBRATION_CASES.items():
                if spec["study"] != study:
                    continue
                verdicts = {dim: {"verdict": "PASS", "reason": "ok", "refs": []}
                            for dim in RV.RUBRIC_DIMS}
                for dim in spec["critical"]:
                    verdicts[dim]["verdict"] = "FAIL"
                cases[cid] = verdicts
            slots.append({"review_id": f"cal-{study}-{channel}",
                          "study": study, "channel": channel,
                          "kind": "calibration", "cases": cases})
    result = RVW.check_calibration(slots=slots)
    assert result["ok"] is True, result["problems"]
    # 关键错误未被拒绝 ⇒ 失败（不能用准确率平均掩盖）
    broken = copy.deepcopy(slots)
    broken[0]["cases"]["missing_nc_claims_complete"]["confirm_control_and_nc"][
        "verdict"] = "PASS"
    result = RVW.check_calibration(slots=broken)
    assert result["ok"] is False
    assert any("关键错误未被拒绝" in p for p in result["problems"])
    # 有效案例被误拒 ⇒ 失败
    broken = copy.deepcopy(slots)
    broken[0]["cases"]["valid_positive"]["claim_scope"]["verdict"] = "FAIL"
    result = RVW.check_calibration(slots=broken)
    assert result["ok"] is False
    assert any("有效案例被误拒" in p for p in result["problems"])


def _fake_review_text(case_ids, packet_sha, review_id, reviewer_id,
                      critical=None):
    critical = critical or {}
    cases = {}
    for cid in case_ids:
        verdicts = {dim: {"verdict": "PASS", "reason": "fixture ok",
                          "refs": [{"json_pointer": "/block_table/0/values"}]}
                    for dim in RV.RUBRIC_DIMS}
        for dim in critical.get(cid, []):
            verdicts[dim]["verdict"] = "FAIL"
        cases[cid] = {"items": verdicts}
    return json.dumps({"schema": RV.CALIBRATION_SCHEMA, "schema_version": 1,
                       "review_id": review_id, "reviewer_id": reviewer_id,
                       "packet_sha256": packet_sha, "cases": cases},
                      ensure_ascii=False)


def test_calibration_slot_runs_in_fresh_view_and_receipt_verifies(tmp_path):
    root = _prepared(tmp_path)
    cal = RVW.build_calibration_packet(root=root, study="A")
    packet = cal["packet"]
    cases = cal["cases"]
    slot = RVW.ReviewSlot(review_id="cal-A-ch1", reviewer_id="rv-cal-A-1",
                          study="A", channel=1, kind="calibration")
    case_ids = [c["case_id"] for c in cases]
    critical = {cid: list(RVW.CALIBRATION_CASES[cid]["critical"])
                for cid in case_ids}

    def sender(system: str, user: str) -> str:
        # 从请求里取 packet sha（真实模型也只能看到请求）
        packet_seen = json.loads(user.split("PACKET:\n", 1)[1])
        return _fake_review_text(case_ids, packet_seen["packet_body_sha256"],
                                 "cal-A-ch1", "rv-cal-A-1", critical)

    row = RVW.execute_slot(root=root, slot=slot, packet_doc=packet,
                           calibration=True, sender=sender,
                           extra_cases=cases)
    assert set(row["cases"]) == set(case_ids)
    assert row["receipt"]["review_id"] == "cal-A-ch1"
    assert row["receipt"]["isolation"]["fresh_process"] is True
    assert (root / "review" / "cal-A-ch1" / "slot_runner.py").is_file()
    assert (root / "review" / "cal-A-ch1" / "receipt.json").is_file()


def test_final_slot_rejects_unresolved_refs_from_model(tmp_path):
    root = _prepared(tmp_path)
    packet = RVW.build_rv_packet(root=root, study="A", report=None)
    slot = RVW.ReviewSlot(review_id="final-A-ch1", reviewer_id="rv-final-A-1",
                          study="A", channel=1, kind="final")

    def sender(system: str, user: str) -> str:
        packet_seen = json.loads(user.split("PACKET:\n", 1)[1])
        value = packet_seen["packet_body_sha256"]
        items = {dim: {"verdict": "PASS", "reason": "ok",
                       "refs": [{"json_pointer": "/does/not/exist"}]}
                 for dim in RV.RUBRIC_DIMS}
        return json.dumps({"schema": RV.REVIEW_SCHEMA, "schema_version": 1,
                           "review_id": "final-A-ch1",
                           "reviewer_id": "rv-final-A-1",
                           "packet_sha256": value, "items": items})

    with pytest.raises(RVW.ReviewError) as exc:
        RVW.execute_slot(root=root, slot=slot, packet_doc=packet,
                         calibration=False, sender=sender)
    assert exc.value.code == "REVIEW_REF_UNRESOLVED"


def test_packet_rejects_report_bound_to_wrong_receipt(tmp_path):
    root = _prepared(tmp_path)
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = RVW._case_report("valid_positive", base)
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.build_rv_packet(
            root=root, study="A", report=report,
            model_lineage={"analysis_receipt_sha": "0" * 64})
    assert exc.value.code == "PACKET_REPORT_RECEIPT"


def test_valid_calibration_cases_carry_statistics_and_nc_facts(tmp_path):
    root = _prepared(tmp_path)
    for study in ("A", "B"):
        cal = RVW.build_calibration_packet(root=root, study=study)
        for case in cal["cases"]:
            spec = RVW.CALIBRATION_CASES[case["case_id"]]
            if spec["critical"]:
                continue
            text = " ".join(row["claim"] for row in
                            case["report"]["heterogeneity_and_scope"])
            assert "95% interval" in text
            assert "n_blocks" in text
            if study == "A":
                assert "阴性对照" in text
            else:
                assert "没有登记阴性对照" in text
                assert "LOSS_CAP=10" in text


def test_review_is_not_run_when_study_has_no_valid_report(tmp_path, monkeypatch):
    from orchestrator.rsi_r1 import rv_cli as CLI
    from tests.fixtures import rv_synthetic as RS
    root = RS.build_initialized_root(tmp_path)
    prereg = RV.read_json(root / "contract" / "prereg.json")
    permit = RV.build_permit(
        round_id=root.name, kind="suffix_and_review", prereg=prereg,
        closure_sha256="a" * 64, worker_policy_sha="b" * 64,
        prompt_sha="c" * 64, schema_sha="d" * 64, tokenizer_sha="e" * 64,
        rubric_sha="f" * 64, config_sha="1" * 64,
        calibration_report_sha="2" * 64)
    RV.write_json_private(root / "contract" / "permit_suffix.json", permit)
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    assert CLI.main(["review", "--root", str(root), "--study", "A"]) == 0
    doc = RV.read_json(root / "review" / "final_A.json")
    assert doc["verdict"] == "REVIEW_NOT_RUN"
    assert not (root / "quota" / "ledger.jsonl").exists()


def test_packet_compaction_keeps_facts_and_drops_duplicates(tmp_path):
    root = _prepared(tmp_path)
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = RVW._case_report("valid_positive", base)
    packet = RVW.build_rv_packet(
        root=root, study="A", report=report, report_text="正文",
        model_lineage={"analysis_receipt_sha": str(
            (base.get("analysis_receipt_public") or {}).get("output_sha256"))})
    assert "candidate_block_effects" not in packet
    assert "contrast_specs" not in packet
    assert "rendered" not in packet["model_report"]
    # 报告实际引用的诊断段必须保留（Plan §4.4 只删等价重复）
    assert "contrasts" in packet["analysis_output"]["diagnostics"]
    assert "summaries" in packet["analysis_output"]["diagnostics"]
    assert len(packet["block_table"]) == 32
    assert all(row["values"] for row in packet["block_table"])
    for row in packet["raw_recomputation"]["contrasts"].values():
        assert "terms" not in row
    assert packet["compaction"]["dropped_duplicates"]
    assert "allowed_design_space" not in packet["task"]
    assert packet["task"]["primary"]


def test_block_table_carries_per_block_nuisance_values(tmp_path):
    root = _prepared(tmp_path)
    for study, key in (("A", "mass_kg"), ("B", "target_freq_hz")):
        packet = RVW.build_rv_packet(root=root, study=study, report=None)
        values = [row["nuisance"].get(key) for row in packet["block_table"]]
        assert all(v is not None and v != "" for v in values), (study, values[:3])
        assert len(set(values)) > 1, (study, values[:3])


def _report_with_ref(packet, pointer):
    report = RVW._case_report("valid_positive", packet)
    report["limitations"][0]["evidence_refs"] = [{
        "kind": "json_pointer", "file": "analysis/output.json",
        "sha256": str((packet.get("analysis_receipt_public") or {})
                      .get("output_sha256") or ""), "pointer": pointer}]
    return report


def test_packet_keeps_diagnostics_cited_by_reports(tmp_path):
    root = _prepared(tmp_path)
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = _report_with_ref(base, "/diagnostics/contrasts/0/block_effects/7/effect")
    packet = RVW.build_rv_packet(
        root=root, study="A", report=report, report_text="正文",
        model_lineage={"analysis_receipt_sha": str(
            (base.get("analysis_receipt_public") or {}).get("output_sha256"))})
    assert packet["model_report"]["report"]


def test_packet_rejects_dangling_report_ref(tmp_path):
    root = _prepared(tmp_path)
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = _report_with_ref(base, "/diagnostics/does_not_exist")
    with pytest.raises(RVW.ReviewError) as exc:
        RVW.build_rv_packet(
            root=root, study="A", report=report, report_text="正文",
            model_lineage={"analysis_receipt_sha": str(
                (base.get("analysis_receipt_public") or {}).get(
                    "output_sha256"))})
    assert exc.value.code == "PACKET_REPORT_REF_UNRESOLVED"


def test_precommit_rejects_real_b_report_types_and_plan_contrast_id():
    import json
    from pathlib import Path
    from orchestrator.rsi_r1 import task_specs as TS
    fixture = json.loads((Path("tests/fixtures") /
                          "rv_b_real_report_20261116.json").read_text())
    task = TS.build_new_b(commitment="c" * 64)
    problems = RVW.precommit_report_problems(
        fixture["research_report"], task=task, claims=fixture["claims"])
    assert any(p.startswith("question_and_hypotheses") for p in problems)
    assert any(p.startswith("methods_and_assignment_responsibility")
               for p in problems)
    assert any("primary_results[0].contrast_id" in p for p in problems)


def test_precommit_accepts_contract_valid_report(tmp_path):
    root = _prepared(tmp_path)
    base = RVW.build_rv_packet(root=root, study="A", report=None)
    report = RVW._case_report("valid_positive", base)
    from orchestrator.rsi_r1 import task_specs as TS
    task = TS.build_new_a(commitment="c" * 64)
    assert RVW.precommit_report_problems(report, task=task) == []
