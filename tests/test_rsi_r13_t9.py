"""RSI-R1.3 T9：五轴结案、独立 checker 与 T5 脚本形状（零物理/零 POST）。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from orchestrator.rsi_r1 import closeout_r13 as CO


def _taskset(tmp_path: Path) -> Path:
    d = tmp_path / "taskset"
    d.mkdir()
    (d / "taskset_manifest.json").write_text(json.dumps({
        "schema": "simpraxis.rsi_r13.taskset_manifest.v1",
        "holdout_task_ids": ["r13-a-friction-speed-interaction",
                             "r13-b-tc-inertia-interaction"]}))
    (d / "resource_plan.json").write_text(json.dumps({
        "schema": "simpraxis.rsi_r13.resource_plan.v1",
        "total_steps": 10_960_000, "hard_cap_steps": 11_900_000,
        "subenvelopes": {"maintenance_reference": 3_584_000}}))
    return d


def _t5(tmp_path: Path, *, ok=True) -> Path:
    p = tmp_path / "t5.json"
    p.write_text(json.dumps({"all_ok": ok, "paid_posts": 0,
                             "studies": [{"study": "A", "all_ok": ok},
                                         {"study": "B", "all_ok": ok}]}))
    return p


def _checker():
    path = Path("scripts/verification/check_rsi_r13_closure.py")
    spec = importlib.util.spec_from_file_location("checker13", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_closeout_axes_without_candidate_studies(tmp_path):
    ts = _taskset(tmp_path)
    t5 = _t5(tmp_path)
    doc = CO.build_closeout(campaign="rsi-r1.3", taskset_dir=ts, t5_summary=t5)
    axes = doc["axes"]
    assert axes["study_execution"] == "NOT_STARTED"
    assert axes["claim"] == "NONE"
    assert axes["r1"] == "NOT_MEASURED"
    assert axes["measurement"] == "VALID"
    assert axes["process"] == "CLOSEOUT_INCOMPLETE"
    out = tmp_path / "closeout.json"
    sha = CO.write_closeout(out, doc)
    assert len(sha) == 64


def test_closeout_measurement_incomplete_without_t5(tmp_path):
    ts = _taskset(tmp_path)
    doc = CO.build_closeout(campaign="rsi-r1.3", taskset_dir=ts,
                            t5_summary=None)
    assert doc["axes"]["measurement"] == "INCOMPLETE"


def test_independent_checker_accepts_consistent_and_flags_tamper(tmp_path):
    ts = _taskset(tmp_path)
    t5 = _t5(tmp_path)
    doc = CO.build_closeout(campaign="rsi-r1.3", taskset_dir=ts, t5_summary=t5)
    out = tmp_path / "closeout.json"
    CO.write_closeout(out, doc)
    mod = _checker()
    rc = mod.main(["--taskset", str(ts), "--t5-summary", str(t5),
                   "--closeout", str(out)])
    assert rc == 0
    # 篡改 holdout ids → checker 必须拒绝
    bad = json.loads(out.read_text())
    bad["facts"]["holdout_task_ids"] = ["other"]
    out.write_text(json.dumps(bad))
    rc = mod.main(["--taskset", str(ts), "--t5-summary", str(t5),
                   "--closeout", str(out)])
    assert rc == 1


def test_t5_script_imports_and_declares_zero_post():
    path = Path("scripts/rsi_r13/t5_offline.py")
    spec = importlib.util.spec_from_file_location("t5_offline", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["t5_offline"] = mod
    spec.loader.exec_module(mod)
    assert mod.BASE_IMAGE.startswith("sim-praxis/sandbox@sha256:")
    assert hasattr(mod, "run_chain") and hasattr(mod, "main")
