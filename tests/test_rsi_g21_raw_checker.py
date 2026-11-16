"""RSI-G2.1 独立 raw checker：复算、变异矩阵、来源篡改与独立性。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g21_bridge as G21
from orchestrator.rsi_r1 import g21_raw_checker as CHK
from tests.fixtures import g21_synthetic as SYN

FORBIDDEN = ("orchestrator", "g2_contract", "g2_validation", "metrics",
             "statistics", "contrasts")


def _manifest_doc(tmp_path: Path, info=None) -> dict:
    return SYN.replay_manifest_doc(tmp_path)


def _inputs(tmp_path: Path) -> dict:
    doc = _manifest_doc(tmp_path)
    root = tmp_path / "diag"
    stats = G21.stat_contract(root)
    manifest = G21.load_manifest_dict(doc)
    info = {"task": doc["studies"]["A"]["task"],
            "output_raw": doc["studies"]["A"]["output_raw"],
            "observations": doc["studies"]["A"]["observations_batch"],
            "sealed_scope": doc["studies"]["A"]["scope_contract"]}
    G21.build_scopes(root, manifest)
    task = json.loads(Path(info["task"]).read_text())
    scope = json.loads((root / "contract" / "scope_A.json").read_text())
    candidate = json.loads(Path(info["output_raw"]).read_text())
    return {"info": info, "root": root, "stats": stats, "task": task,
            "scope": scope, "candidate": candidate,
            "raw": Path(info["observations"])}


def test_checker_source_is_independent():
    path = Path(CHK.__file__)
    text = path.read_text(encoding="utf-8")
    hits = [name for name in FORBIDDEN
            if re.search(rf"^\s*(from|import)\s+{re.escape(name)}\b", text,
                         re.MULTILINE)]
    assert hits == []
    assert CHK.run_check.__module__ == "orchestrator.rsi_r1.g21_raw_checker"


def test_recompute_matches_candidate_and_axes(tmp_path):
    payload = _inputs(tmp_path)
    report = CHK.run_check(
        study="A", task=payload["task"], scope=payload["scope"],
        stats=payload["stats"], raw_dir=payload["raw"],
        candidate=payload["candidate"])
    assert report["ok"] is True, report["problems"]
    assert report["independent"] is True
    assert report["raw"]["n_runs"] == 160
    assert report["raw"]["n_blocks"] == 32
    primary = report["recomputed_primary"]
    candidate = {c["contrast_id"]: c for c in payload["candidate"]["contrasts"]}
    row = candidate["friction_x_speed"]
    assert abs(primary["estimate"] - row["estimate"]) < 1e-12
    assert report["axes"]["science_status"] == row["status"]
    # MATCH 不等于 SUPPORTED：状态来自独立分类
    assert report["axes"]["science_status"] in ("SUPPORTED", "REFUTED",
                                                "INCONCLUSIVE")


@pytest.mark.parametrize("mutation", sorted(CHK.MUTATIONS))
def test_registered_mutations_match_expectation(tmp_path, mutation):
    payload = _inputs(tmp_path)
    report = CHK.run_check(
        study="A", task=payload["task"], scope=payload["scope"],
        stats=payload["stats"], raw_dir=payload["raw"],
        candidate=payload["candidate"], mutation=mutation)
    expected = list(CHK.MUTATIONS[mutation])
    assert report["ok"] is True, (report["problems"], report["observed"])
    if expected:
        assert set(expected) <= set(report["problems"])
    else:
        assert report["problems"] == []


def test_tampered_raw_is_rejected(tmp_path):
    payload = _inputs(tmp_path)
    batch = payload["raw"]
    victim = sorted(batch.glob("run-*.jsonl"))[0]
    victim.write_text(victim.read_text(encoding="utf-8") + " ")
    with pytest.raises(CHK.CheckError) as exc:
        CHK.run_check(study="A", task=payload["task"], scope=payload["scope"],
                      stats=payload["stats"], raw_dir=batch,
                      candidate=payload["candidate"])
    assert exc.value.code == "RAW_TAMPERED"


def test_run_raw_check_end_to_end(tmp_path):
    doc = _manifest_doc(tmp_path)
    root = tmp_path / "diag"
    manifest = G21.load_manifest_dict(doc)
    G21.freeze_inputs(root, manifest)
    G21.build_scopes(root, manifest)
    G21.materialize_study(root, manifest, "A")
    report = G21.run_raw_check(root, manifest,
                               python=__import__("sys").executable)
    assert report["ok"] is True, report
    assert report["independent"] is True
    assert all(report["studies"][s]["raw_ok"] for s in ("A", "B"))
    assert len(report["mutations"]) == len(CHK.MUTATIONS)


def test_scope_unit_drift_is_detected(tmp_path):
    payload = _inputs(tmp_path)
    scope = dict(payload["scope"], unit="km")
    report = CHK.run_check(study="A", task=payload["task"], scope=scope,
                           stats=payload["stats"], raw_dir=payload["raw"],
                           candidate=payload["candidate"])
    assert "SCOPE_DRIFT" in report["problems"]
    assert report["axes"]["scope_valid"] is False
