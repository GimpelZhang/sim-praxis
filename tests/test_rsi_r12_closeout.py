"""RSI-R1.2 T9 结案/证据包/独立复算（AC47–AC49 同构；全部零 POST）。

三件事分开测：

1. ``closeout_r12.build`` 的五轴只由盘面事实推（不读自述），仪器缺陷
   （候选源码 == 宿主模板 且 回执 OUTPUT_SCHEMA_MISMATCH）判 INVALID_INSTRUMENT；
2. ``closure.build_evidence_pack`` 逐件 sha 与包卫生（0700/0600、无未登记件）；
3. ``scripts/verification/check_rsi_r12_closure.py`` 作为**外部 stdlib 进程**
   复算；正例 PASS，五类篡改各判 FAIL（判据可失败才算判据）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import closeout_r12 as C12
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1.cli_r12 import R12Context

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "verification" / "check_rsi_r12_closure.py"
ENVELOPE = "r12-test-paid"
TEMPLATE_SHA = "daf30754f8eb9b0e6b468d6d714c9579b73a2b6afbf668b9b7edafcc3e7d4f63"


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    path.chmod(0o600)


def _ledger_rows(n: int = 2) -> "list[dict]":
    return [{
        "seq": i + 1, "event": "call", "batch_id": "R12-T9-PROBE-01",
        "task": 0, "stage": "coding_debug", "ok": True,
        "tokens_in": 100 + i, "tokens_out": 10 + i,
        "tokens_cache_create": 0, "tokens_cache_read": 0,
        "model": "deepseek-v4.1-flash-1017",
    } for i in range(n)]


@pytest.fixture()
def campaign(tmp_path: Path, monkeypatch):
    """合成 R1.2 根：账 2 呼、批准 2、probe NOT_MEASURED（仪器缺陷）、族 31+2。"""
    family = tmp_path / "rsi_r12"
    root = family / ENVELOPE
    _write(root / "contract" / "envelope.json",
           {"envelope_id": ENVELOPE, "profile_id": "rsi-r1.2"})
    ledger = root / "quota" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                              for r in _ledger_rows(2)), encoding="utf-8")
    ledger.chmod(0o600)
    _write(root / "quota" / "approvals" / "R12-T9-PROBE-01.json",
           {"approved": 2, "task": "probe"})
    attempt = "protocol/probe/attempt-1"
    _write(root / "protocol" / "report_probe.json", {
        "schema": "simpraxis.rsi_r1.protocol_report.v1", "attempt": 1,
        "attempt_relpath": attempt, "batch_id": "R12-T9-PROBE-01",
        "lane": "probe", "verdict": "NOT_MEASURED",
        "reason": "study A 停于 UNCLASSIFIED（里程碑 4/8）",
        "posts": {"this_attempt": 2, "used_before": 0, "allowed": 12},
        "qualification": {"first_attempt_ok_count": 4, "attempted_positions": 5,
                          "first_error": {"name": "analyze", "code": "UNCLASSIFIED",
                                          "action_index": 5, "position": 4}},
    })
    src = root / attempt / "designs" / f"{TEMPLATE_SHA}.py"
    src.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src.write_text(PT.ANALYSIS_TEMPLATE, encoding="utf-8")
    src.chmod(0o600)
    _write(root / attempt / "private" / "receipts" / "A-explore.json", {
        "schema": "simpraxis.rsi_r1.analysis_receipt.v1",
        "reason_codes": ["OUTPUT_SCHEMA_MISMATCH"],
        "source_sha256": TEMPLATE_SHA, "output_schema": "",
        "output_run_ids": [], "validity": False, "exit_code": 0,
    })
    _write(family / "family" / "family.json", {
        "schema": "simpraxis.rsi_r12.family.v1", "family_cap": 155,
        "history": {"total_call_rows": 31},
        "members": [{"envelope_id": ENVELOPE, "profile_id": "rsi-r1.2",
                     "posts_reserved": 124, "posts_used": 2,
                     "state": "registered"}],
    })
    monkeypatch.setenv("SIMPRAXIS_RSI_R12_ROOT", str(family))
    return root


def _ctx(root: Path) -> R12Context:
    return R12Context(root, ENVELOPE)


def _build(root: Path, *, repo_root=None):
    ctx = _ctx(root)
    closed = C12.close_round(ctx, reason="测试结案")
    body = C12.build(ctx, container_facts={"available": False, "own": None,
                                           "note": "test"},
                     repo_root=repo_root)
    return closed, body


def _judge(root: Path, *extra: str):
    proc = subprocess.run(
        [sys.executable, "-I", str(CHECKER), "--root", str(root), *extra],
        capture_output=True, text=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_instrument_facts_requires_byte_identity(campaign: Path):
    facts = C12.instrument_facts(campaign)
    assert facts["template_sha256"] == TEMPLATE_SHA
    assert facts["source_sha_equals_template"] is True
    assert facts["analysis_reason_codes"] == ["OUTPUT_SCHEMA_MISMATCH"]
    assert facts["classification"] == C12.CLASS_INSTRUMENT_TEMPLATE


def test_instrument_facts_rejects_non_template_source(campaign: Path):
    receipt = (campaign / "protocol/probe/attempt-1/private/receipts"
               / "A-explore.json")
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    doc["source_sha256"] = "0" * 64
    _write(receipt, doc)
    facts = C12.instrument_facts(campaign)
    assert facts["source_sha_equals_template"] is False
    assert facts["classification"] == C12.CLASS_MODEL_FAILURE


def test_build_axes_from_disk_facts_only(campaign: Path):
    closed, body = _build(campaign)
    assert closed["root_state"] == "CLOSED"
    assert body["axes"] == {
        "process": "COMPLETE", "measurement": "INVALID_INSTRUMENT",
        "study_execution": "NOT_STARTED", "claim": "NONE", "r1": "NOT_MEASURED"}
    assert body["totals"]["history_posts"] == 31
    assert body["totals"]["round_calls"] == 2
    assert body["totals"]["family_total"] == 33
    assert body["family"]["member_usage_matches_ledger"] is True
    # gaps 只登记有意限制，不把仪器缺陷算成「记录矛盾」
    assert any("cost_usd" in g for g in body["gaps"])
    assert not any("仪器缺陷" in g for g in body["gaps"])


def test_build_records_family_usage_idempotently(campaign: Path):
    _build(campaign)
    _build(campaign)  # 重跑不得回退/累加
    doc = json.loads((campaign.parent / "family" / "family.json")
                     .read_text(encoding="utf-8"))
    assert doc["members"][0]["posts_used"] == 2


def test_pack_and_independent_checker_pass(campaign: Path):
    _build(campaign)
    ctx = _ctx(campaign)
    manifest = C12.build_pack(ctx, repo_root=REPO)
    assert manifest["n_files"] >= 5
    assert manifest["files"][f"checker/{CHECKER.name}"] is not None
    assert C12.verify_pack(campaign / "evidence_pack")["ok"] is True
    rc, report = _judge(campaign, "--verify", str(campaign / "evidence_pack"))
    assert rc == 0, report["problems"]
    assert report["verdict"] == "PASS"
    assert report["recomputed"]["family_total"] == 33
    assert report["recomputed"]["studies_started"] is False
    assert report["pack"]["n_files"] == manifest["n_files"]


def test_checker_fails_on_ledger_tamper(campaign: Path):
    _build(campaign)
    ledger = campaign / "quota" / "ledger.jsonl"
    rows = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(rows[0] + "\n", encoding="utf-8")
    rc, report = _judge(campaign)
    assert rc == 1
    assert any("posts_used" in p or "call 行" in p for p in report["problems"])


def test_checker_fails_on_family_tamper(campaign: Path):
    _build(campaign)
    path = campaign.parent / "family" / "family.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["members"][0]["posts_used"] = 1
    _write(path, doc)
    rc, report = _judge(campaign)
    assert rc == 1
    assert any("族成员 posts_used" in p for p in report["problems"])


def test_checker_fails_when_axes_disagree(campaign: Path):
    _build(campaign)
    path = campaign / "closeout" / "r12_readout.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["axes"]["measurement"] = "VALID"
    _write(path, doc)
    rc, report = _judge(campaign)
    assert rc == 1
    assert any("measurement" in p for p in report["problems"])


def test_checker_fails_on_pack_tamper(campaign: Path):
    _build(campaign)
    ctx = _ctx(campaign)
    C12.build_pack(ctx, repo_root=REPO)
    target = campaign / "evidence_pack" / "closeout" / "r12_readout.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    rc, report = _judge(campaign, "--verify", str(campaign / "evidence_pack"))
    assert rc == 1
    assert any(p.startswith("pack:") for p in report["problems"])


def test_checker_fails_when_root_not_closed(campaign: Path):
    ctx = _ctx(campaign)
    body = C12.build(ctx, container_facts={"available": False})
    assert body["root_state"] != "CLOSED"
    rc, report = _judge(campaign)
    assert rc == 1
    assert any("root_state" in p for p in report["problems"])


def _fake_lock(root: Path, rel: str, sha: str) -> None:
    _write(root / "contract" / "lock.json", {
        "schema": "simpraxis.rsi_r1.lock.v1",
        "closure": {"closure_sha256": "f" * 64, "groups_key": "r12",
                    "groups": {"entry": {"files": [
                        {"path": rel, "sha256": sha}]}}},
    })


def test_post_freeze_drift_is_recorded_and_cross_checked(campaign: Path):
    rel = "scripts/verification/check_rsi_r12_closure.py"
    _fake_lock(campaign, rel, "0" * 64)  # 与仓库实际字节不符 ⇒ 漂移
    _, body = _build(campaign, repo_root=REPO)
    drift = body["post_freeze_drift"]
    assert drift["verify"] == "CLOSURE_DRIFT"
    assert [m["path"] for m in drift["mismatches"]] == [rel]
    rc, report = _judge(campaign, "--repo-root", str(REPO))
    assert rc == 0, report["problems"]
    assert report["recomputed"]["post_freeze_drift"]["verify"] == "CLOSURE_DRIFT"
    # 瞒报漂移必须被抓：readout 写 OK 而重算是漂移 ⇒ FAIL
    path = campaign / "closeout" / "r12_readout.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["post_freeze_drift"]["verify"] = "OK"
    doc["post_freeze_drift"]["mismatches"] = []
    _write(path, doc)
    rc, report = _judge(campaign, "--repo-root", str(REPO))
    assert rc == 1
    assert any("post_freeze_drift" in p for p in report["problems"])
