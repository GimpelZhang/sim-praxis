"""RV-01/RV-02：两源清单元数据、公开包完整性与漂移检测（零 POST）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import rv_contract as RV
from tests.fixtures import rv_synthetic as RS


def _manifest(tmp_path: Path) -> "tuple[dict, dict]":
    _, source_map = RS.build_rv_source_roots(tmp_path)
    doc = RV.build_source_manifest(source_map)
    return doc, source_map


def test_manifest_binds_two_studies_and_required_sets(tmp_path):
    doc, _ = _manifest(tmp_path)
    RV.verify_source_manifest(doc)
    assert set(doc["studies"]) == {"A", "B"}
    assert doc["studies"]["A"]["required_contrast_ids"] == [
        "friction_x_speed", "negative_torsional"]
    assert doc["studies"]["B"]["required_contrast_ids"] == ["tc_x_inertia"]
    assert doc["studies"]["A"]["n_blocks"] == 32
    assert doc["studies"]["B"]["n_blocks"] == 32
    assert doc["studies"]["A"]["interval_level"] == 0.95
    assert doc["studies"]["B"]["unit"] == "1"


def test_study_set_must_be_exactly_ab(tmp_path):
    _, source_map = RS.build_rv_source_roots(tmp_path)
    broken = {**source_map, "studies": {"A": source_map["studies"]["A"]}}
    with pytest.raises(RV.RVError) as exc:
        RV.build_source_manifest(broken)
    assert exc.value.code == "SOURCE_MAP_INVALID"


def test_missing_source_file_blocks(tmp_path):
    doc, _ = _manifest(tmp_path)
    path = Path(doc["studies"]["A"]["paths"]["output_raw"]["path"])
    path.write_bytes(b"{}")
    with pytest.raises(RV.RVError) as exc:
        RV.verify_source_manifest(doc)
    assert exc.value.code == "SOURCE_NOT_READY"


def test_receipt_binding_mismatch_blocks(tmp_path):
    doc, _ = _manifest(tmp_path)
    receipt_path = Path(doc["studies"]["B"]["paths"]["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["study_id"] = "A"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(RV.RVError) as exc:
        RV.verify_source_manifest(doc)
    assert exc.value.code == "SOURCE_NOT_READY"


def test_public_package_contains_required_facts(tmp_path):
    doc, _ = _manifest(tmp_path)
    root = tmp_path / "round"
    allow = RV.build_public_package(root, doc)
    RV.verify_public_package(root, allow)
    for study in ("A", "B"):
        for name in RV.PUBLIC_FILES:
            assert (root / "public" / study / name).is_file(), (study, name)
        assert (root / "public" / study / "analysis_output.json").read_bytes() == \
            Path(doc["studies"][study]["paths"]["output_raw"]["path"]).read_bytes()
        blocks = json.loads((root / "public" / study /
                             "raw_block_values.json").read_text())
        assert len(blocks["values"]) == 32
    brief = json.loads((root / "public" / "A" / "brief.json").read_text())
    assert brief["negative_control_expectation"]
    assert brief["scope"]["interval_level"] == 0.95


def test_public_package_drift_is_detected(tmp_path):
    doc, _ = _manifest(tmp_path)
    root = tmp_path / "round"
    allow = RV.build_public_package(root, doc)
    target = root / "public" / "A" / "brief.json"
    target.write_text(target.read_text().replace("friction", "frictionX"))
    with pytest.raises(RV.RVError) as exc:
        RV.verify_public_package(root, allow)
    assert exc.value.code == "PUBLIC_NOT_READY"


def test_prereg_binds_sources_and_lanes(tmp_path):
    doc, _ = _manifest(tmp_path)
    root = tmp_path / "round"
    allow = RV.build_public_package(root, doc)
    closure = RV.verify_source_manifest(doc)
    prereg = RV.build_prereg(round_id="r1", manifest=doc,
                             public_allowlist=allow,
                             source_closure=closure,
                             user_authorization="fixture")
    RV.verify_prereg(prereg)
    assert prereg["hard_cap"] == 24
    assert prereg["ledger_phase"] == RV.LEDGER_RSI_RV == 2906
    assert prereg["lanes"] == dict(RV.RV_LANE_POST_CAPS)
    tampered = dict(prereg, hard_cap=25)
    with pytest.raises(RV.RVError) as exc:
        RV.verify_prereg(tampered)
    assert exc.value.code == "PREREG_DRIFT"
