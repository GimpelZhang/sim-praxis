"""RSI-R1 T4：独立闭合复算器的坏例测试（Plan §5.6 / AC23）。

复算器只读标准库、不 import 生产判决函数；这里用合成 campaign 根逐条喂坏例，
证明它不会把伪造 PASS、漏 run、错 CI、改包、hash 篡改放过去。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import experiments as E

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verification" / \
    "check_rsi_r1_closure.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_rsi_r1_closure", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CL = _load_checker()
PLAN_SHA = "c" * 64
CONDS = {"A": ("base", "low_mu"), "B": ("base", "tc_only")}


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                               indent=2) + "\n", encoding="utf-8")


def _manifest(study: str) -> dict:
    assignments = []
    for cid in CONDS[study]:
        for i in range(1, 33):
            assignments.append({"condition_id": cid, "block_id": f"b{i:02d}",
                                "params": {"mass_kg": "0.5"}})
    return {"study_id": study, "plan_sha256": PLAN_SHA,
            "keeper_seed_sha256": "d" * 64, "block_count": 32,
            "varying": {"mass_kg": ["0.2", "1.0"]},
            "assignments": assignments, "manifest_sha256": "e" * 64}


def _run_ids(study: str) -> "list[str]":
    return [E.run_id_for(plan_sha256=PLAN_SHA, study_id=study,
                         condition_id=cid, block_id=f"b{i:02d}")
            for cid in CONDS[study] for i in range(1, 33)]


def _report(study: str) -> dict:
    if study == "A":
        sci = [
            {"id": "primary", "kind": "primary", "metric_id": "displacement_x_m",
             "proposition": "decrease_gt", "delta": "0.001", "level": "0.95",
             "n_blocks": 32, "effect": -0.01, "lower": -0.0105,
             "upper": -0.0095, "status": "SUPPORTED"},
            {"id": "negative", "kind": "negative",
             "metric_id": "displacement_x_m_negative_equivalence",
             "proposition": "equivalent", "delta": "0.001", "level": "0.9",
             "n_blocks": 32, "effect": 0.0, "lower": -0.0005,
             "upper": 0.0005, "status": "SUPPORTED"},
        ]
    else:
        sci = [
            {"id": "primary", "kind": "primary",
             "metric_id": "normalized_tracking_rmse",
             "proposition": "increase_gt", "delta": "0.01", "level": "0.95",
             "n_blocks": 32, "effect": 0.02, "lower": 0.015, "upper": 0.025,
             "status": "SUPPORTED"},
        ]
    return {"schema": "simpraxis.rsi_r1.judge_report.v1", "schema_version": 1,
            "study_id": study, "plan_sha256": PLAN_SHA,
            "layers": {"schema_accepted": True, "evidence_valid": True,
                       "claim_status": "MATCH", "scientific": sci},
            "verdict": "JUDGE_OK"}


def _comparison(study: str) -> dict:
    runs = [{"run_id": rid, "block_id": f"b{i:02d}", "ok": True,
             "problems": [], "independence": {"execution_id": f"e-{rid}"}}
            for i, rid in enumerate(_run_ids(study), 1)]
    return {"schema": "simpraxis.rsi_r1.replay_comparison.v1",
            "schema_version": 1, "study_id": study, "plan_sha256": PLAN_SHA,
            "runs": runs, "problems": [], "ok": True}


def _ledger(n_calls: int = 3, *, task: int = 2) -> "list[dict]":
    rows = [{"event": "selfcheck", "seq": 1, "ts_iso": "2026-11-11T00:00:00Z",
             "task": None, "ok": True}]
    for i in range(n_calls):
        rows.append({"event": "call", "seq": i + 2,
                     "ts_iso": f"2026-11-11T00:0{i}:00Z", "task": task,
                     "ok": True})
    return rows


def _root(tmp_path: Path, *, ledger=None) -> Path:
    root = tmp_path / "campaign"
    _write(root / "contract" / "budget.json",
           {"posts_by_lane": dict(CL.LANE_CAPS), "max_usd": "100",
            "max_runs_by_lane": {"explore": 64, "confirm": 192, "replay": 192},
            "max_steps": 2000000})
    for study in ("A", "B"):
        _write(root / "confirm" / f"manifest_{study}.json", _manifest(study))
        _write(root / "judge" / f"report_{study}.json", _report(study))
        _write(root / "replay" / f"comparison_{study}.json", _comparison(study))
    qdir = root / "quota"
    qdir.mkdir(parents=True, exist_ok=True)
    rows = _ledger() if ledger is None else ledger
    (qdir / "ledger.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    return root


def _edit(path: Path, mutate) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    _write(path, doc)


# ---------------------------------------------------------------------------
# 干净根
# ---------------------------------------------------------------------------

def test_clean_root_passes(tmp_path: Path) -> None:
    out = CL.Checker(_root(tmp_path)).run()
    assert out["verdict"] == "PASS", out["problems"]
    assert out["validator_source_sha256"] == CL.hashlib.sha256(
        _SCRIPT.read_bytes()).hexdigest()
    assert out["run_at_utc"].endswith("Z")


def test_run_id_derivation_matches_production() -> None:
    for study in ("A", "B"):
        for cid, block in ((CONDS[study][0], "b01"), (CONDS[study][1], "b32")):
            assert CL.run_id_for(PLAN_SHA, study, cid, block) == E.run_id_for(
                plan_sha256=PLAN_SHA, study_id=study,
                condition_id=cid, block_id=block)


# ---------------------------------------------------------------------------
# 科学/证据坏例
# ---------------------------------------------------------------------------

def test_forged_pass_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _edit(root / "judge" / "report_A.json",
          lambda d: d["layers"].__setitem__("evidence_valid", False))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("J-A-VERDICT-CONSISTENT") for p in out["problems"])


def test_missing_run_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _edit(root / "replay" / "comparison_B.json",
          lambda d: d["runs"].pop())
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("R-B-COVERAGE") for p in out["problems"])


def test_wrong_ci_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _edit(root / "judge" / "report_A.json",
          lambda d: d["layers"]["scientific"][0].__setitem__("lower", 0.5))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("J-A-ORDER-primary") for p in out["problems"])


def test_status_mismatch_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    # 区间没变，却把 REFUTED 写成 SUPPORTED
    _edit(root / "judge" / "report_B.json",
          lambda d: d["layers"]["scientific"][0].update(
              {"lower": -0.03, "upper": -0.02, "status": "SUPPORTED"}))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("J-B-STATUS-primary") for p in out["problems"])


def test_denominator_change_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _edit(root / "judge" / "report_A.json",
          lambda d: d["layers"]["scientific"][0].__setitem__("n_blocks", 31))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("J-A-N-primary") for p in out["problems"])


def test_missing_replay_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "replay" / "comparison_B.json").unlink()
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any("comparison_B" in p for p in out["problems"])


# ---------------------------------------------------------------------------
# 预算坏例
# ---------------------------------------------------------------------------

def test_over_hard_cap_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path, ledger=_ledger(n_calls=145))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("B-HARD-CAP") for p in out["problems"])


def test_task_cap_overflow_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path, ledger=_ledger(n_calls=49, task=2))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("B-TASK-CAPS") for p in out["problems"])


def test_non_utc_timestamp_is_rejected(tmp_path: Path) -> None:
    rows = _ledger()
    rows[1]["ts_iso"] = "2026-11-11T00:00:00+08:00"
    out = CL.Checker(_root(tmp_path, ledger=rows)).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("B-TS-UTC") for p in out["problems"])


def test_budget_declaration_drift_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _edit(root / "contract" / "budget.json",
          lambda d: d["posts_by_lane"].__setitem__("probe", 9))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("B-DECLARED-CAPS") for p in out["problems"])


# ---------------------------------------------------------------------------
# 证据包核对
# ---------------------------------------------------------------------------

def _pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    (pack / "files").mkdir(parents=True)
    (pack / "files" / "a.json").write_text("{\"a\":1}\n", encoding="utf-8")
    (pack / "files" / "b.txt").write_text("b\n", encoding="utf-8")
    files = {}
    for rel in ("files/a.json", "files/b.txt"):
        files[rel] = CL.hashlib.sha256((pack / rel).read_bytes()).hexdigest()
    _write(pack / "evidence_manifest.json", {"files": files})
    return pack


def test_pack_verify_passes(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    out = CL.Checker(_root(tmp_path)).run(pack=pack)
    assert out["verdict"] == "PASS", out["problems"]


def test_pack_tamper_is_rejected(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    (pack / "files" / "a.json").write_text("{\"a\":2}\n", encoding="utf-8")
    out = CL.Checker(_root(tmp_path)).run(pack=pack)
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("P-HASH") for p in out["problems"])


def test_pack_extra_file_is_rejected(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    (pack / "files" / "c.txt").write_text("c\n", encoding="utf-8")
    out = CL.Checker(_root(tmp_path)).run(pack=pack)
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("P-EXTRA") for p in out["problems"])


def test_pack_symlink_is_rejected(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    target = pack / "files" / "b.txt"
    link = pack / "files" / "link.txt"
    os.symlink(target, link)
    manifest = json.loads((pack / "evidence_manifest.json").read_text())
    manifest["files"]["files/link.txt"] = CL.hashlib.sha256(
        target.read_bytes()).hexdigest()
    _write(pack / "evidence_manifest.json", manifest)
    out = CL.Checker(_root(tmp_path)).run(pack=pack)
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("P-HASH") for p in out["problems"])


# ---------------------------------------------------------------------------
# 终态声明
# ---------------------------------------------------------------------------

def test_unsupported_complete_claim_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root / "deliverable" / "closure.json",
           {"studies": {"A": {"claim": "COMPLETE"},
                        "B": {"claim": "COMPLETE"}}})
    _edit(root / "judge" / "report_A.json",
          lambda d: (d.__setitem__("verdict", "JUDGE_REJECT"),
                     d["layers"].__setitem__("claim_status", "CLAIM_INVALID")))
    out = CL.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("D-A-SUPPORTED") for p in out["problems"])


def test_cli_rc_and_report_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    out_path = tmp_path / "judge_out" / "closure_judge.json"
    rc = CL.main(["--root", str(root), "--report", str(out_path)])
    assert rc == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["verdict"] == "PASS"
    assert oct(out_path.stat().st_mode & 0o777) == "0o600"
