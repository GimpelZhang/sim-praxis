"""S1-8 结案机判：合成根内 problems=[] 全绿；篡改任一证据即红。

机判纪律：重读证据复算（不信自报）——本测试就是这条纪律的回归面。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator.common.fsutil import atomic_write_text
from orchestrator.rsi_bridge import review_s1 as RV
from orchestrator.rsi_bridge.session_ledger import SessionLedger

REPO = Path(__file__).resolve().parents[2]


def _module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_sha(doc) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                               indent=2) + "\n", encoding="utf-8")


def _k7(study: str) -> dict:
    contrasts = [{"id": "primary", "estimate": 0.01, "ci_low": 0.005,
                  "ci_high": 0.015, "n_blocks": 32, "claim": "SUPPORTED"}]
    if study == "A_N":
        contrasts.append({"id": "negative_torsional", "estimate": 0.0,
                          "ci_low": -0.0002, "ci_high": 0.0002, "n_blocks": 32,
                          "claim": "SUPPORTED"})
    return {"study": study, "diffs": [], "contrasts": contrasts,
            "recompute_report": {"n_diffs": 0}}


def _build_root(tmp: Path, monkeypatch) -> Path:
    monkeypatch.setenv("SIMPRAXIS_RSI_S1_ROOT", str(tmp))
    prereg = _module("s1_prereg_fixture", "scripts/s1/prereg_studies.py")
    prereg.build_prereg(tmp / "prereg")
    prereg.sign(signer="a", role="executor", root=tmp / "prereg")
    prereg.sign(signer="b", role="reviewer", root=tmp / "prereg")
    # 校准
    calib = {"schema": "x", "all_ok": True, "gates": []}
    calib["report_sha256"] = _canonical_sha(calib)
    _write(tmp / "closeout" / "calibration_report.json", calib)
    # 研究证据
    evidence = tmp / "evidence" / "run-20261118T000000Z"
    _write(evidence / "mujoco_denial_probe.json",
           {"all_denied": True, "routes": {}})
    for study in ("A_N", "B_N"):
        _write(evidence / "study_report.json" if study == "A_N" else
               evidence / f"study_report_{study}.json",
               {"studies": ["A_N", "B_N"], "gaps": [],
                "outcome": {"status": "done",
                            "stages": {s: {"status": "done", "code": "APPROVED"}
                                       for s in ("A_N", "B_N")}}})
        run_dir = evidence / "runs" / f"{study}_run001"
        for i in range(9):
            _write(run_dir / f"K{i}.json", {"stage": i})
        k7 = _k7(study)
        k8 = {"verdict": "SUPPORTED", "statement": "s",
              "contrasts": [{k: r[k] for k in ("id", "estimate", "ci_low",
                                               "ci_high", "n_blocks", "claim")}
                            for r in k7["contrasts"]]}
        _write(run_dir / "K7.json", k7)
        _write(run_dir / "K8.json", k8)
        for tp in ("propose_plan", "write_analysis", "submit_claim"):
            transcript = run_dir / f"touchpoint_{tp}" / "transcript.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("{}\n")
            _write(run_dir / f"touchpoint_{tp}.meta.json",
                   {"claude_session_id": f"sess-{tp}", "model": "m",
                    "transcript": str(transcript)})
    # 会话账（无未结束）
    ledger = SessionLedger(tmp / "sessions" / "sessions.jsonl")
    ledger.start(session_id="s1", model="m", task="t")
    ledger.finish(session_id="s1", usage=None, exit_status="rc=0")
    # 双审
    ready = RV.freeze_report({"ready": True, "misaccept": [], "verdict": "READY"})
    _write(tmp / "closeout" / "review" / "review_calibration.json", ready)
    dual = RV.freeze_report({"truth_table": {
        s: {"verdict": "PASS", "rule": "two_accept"} for s in ("A_N", "B_N")}})
    _write(tmp / "closeout" / "review" / "dual_review.json", dual)
    return tmp


@pytest.fixture()
def judge(tmp_path, monkeypatch):
    _build_root(tmp_path, monkeypatch)
    module = _module("s1_judge", "scripts/verification/check_s1_closure.py")
    return module, tmp_path


def test_judge_green_on_consistent_root(judge):
    module, _ = judge
    report = module.judge()
    assert report["problems"] == [], report["problems"]
    assert report["pass"] is True
    assert report["judge_sha256"]


def test_judge_red_on_claim_transcription_mismatch(judge):
    module, tmp = judge
    path = tmp / "evidence" / "run-20261118T000000Z" / "runs" / "A_N_run001" / "K8.json"
    doc = json.loads(path.read_text())
    doc["contrasts"][0]["estimate"] += 1e-6
    _write(path, doc)
    report = module.judge()
    assert any("CLAIM_MISMATCH" in p for p in report["problems"])


def test_judge_red_on_prereg_tamper(judge):
    module, tmp = judge
    study = tmp / "prereg" / "study_A_N.json"
    doc = json.loads(study.read_text())
    doc["n_confirm_runs"] = 159
    _write(study, doc)
    report = module.judge()
    assert any("PREREG_CHANGED" in p for p in report["problems"])


def test_judge_red_on_unready_review(judge):
    module, tmp = judge
    path = tmp / "closeout" / "review" / "review_calibration.json"
    _write(path, RV.freeze_report({"ready": False, "misaccept": ["x"]}))
    report = module.judge()
    assert any("REVIEW_INSTRUMENT_NOT_READY" in p for p in report["problems"])
    assert any("REVIEW_MISACCEPT" in p for p in report["problems"])


def test_judge_flags_sham_gap_when_not_equivalent(judge):
    module, tmp = judge
    path = tmp / "evidence" / "run-20261118T000000Z" / "runs" / "A_N_run001" / "K7.json"
    doc = json.loads(path.read_text())
    doc["contrasts"][1]["claim"] = "REFUTED"
    _write(path, doc)
    # K8 同步（否则先撞 CLAIM_MISMATCH）
    k8_path = path.parent / "K8.json"
    k8 = json.loads(k8_path.read_text())
    k8["contrasts"][1]["claim"] = "REFUTED"
    _write(k8_path, k8)
    report = module.judge()
    assert any(g.startswith("SHAM_NOT_EQUIVALENT") for g in report["gaps"])
