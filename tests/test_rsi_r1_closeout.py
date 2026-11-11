"""RSI-R1 T9 离线结案：builder + 独立机判的 closeout 分支（AC34–AC36）。

本测试**不触网、不调用模型、不跑容器**：``build`` 的容器探针用注入桩，
其余全部是合成盘面。重点证三件事：

* AC35：无 env（``adopt_context`` 显式根）也能出 readout，且缺失照实挂牌，
  绝不因为「没东西可报」就写 PASS；
* AC34：tokens/posts 记 EXACT、USD 记 UNKNOWN + BOUND，carryover 旧根
  sha 逐字节复核；
* AC36：机判独立重算锚点/账本/旧根/声明，伪造的强结论必被拒。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import closeout as CL
from orchestrator.rsi_r1 import runtime as RT

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verification" / \
    "check_rsi_r1_closure.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_rsi_r1_closure_co",
                                                  _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHK = _load_checker()

LANE_CAPS = {"probe": 8, "practice": 16, "study_A": 48, "study_B": 48,
             "repair_reserve": 24}
BATCH = "R1-RP1-probe1"
FIXED_CLOCK = lambda: "2026-11-11T12:00:00Z"  # noqa: E731


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                               indent=1) + "\n", encoding="utf-8")


def _ledger_rows() -> "list[dict]":
    rows = [{"event": "call", "seq": 1, "ts_iso": "2026-11-11T10:00:00Z",
             "task": 0, "stage": "probe", "batch_id": BATCH, "ok": True,
             "model": "m", "tokens_in": 10, "tokens_out": 5,
             "tokens_cache_create": 0, "tokens_cache_read": 0}]
    for i in range(2, 8):
        rows.append({"event": "call", "seq": i,
                     "ts_iso": f"2026-11-11T10:0{i}:00Z", "task": 4,
                     "stage": "probe", "batch_id": BATCH, "ok": True,
                     "model": "m", "tokens_in": 100, "tokens_out": 50,
                     "tokens_cache_create": 0, "tokens_cache_read": 0})
    return rows


def _campaign(tmp_path: Path, *, rows=None, with_approval: bool = True,
              with_probe_report: bool = True) -> Path:
    root = tmp_path / "r1_root"
    _write(root / "contract" / "scope.json", {"scope": "test"})
    _write(root / "contract" / "envelope.json",
           {"envelope_id": "r1_test", "consent_sha256": "a" * 64,
            "consent_document_sha256": "b" * 64, "cost_decision": "C"})
    _write(root / "contract" / "budget.json",
           {"posts_by_lane": dict(LANE_CAPS), "max_usd": "12.24"})
    ledger = _ledger_rows() if rows is None else rows
    (root / "quota").mkdir(parents=True, exist_ok=True)
    (root / "quota" / "ledger.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ledger),
        encoding="utf-8")
    if with_approval:
        _write(root / "quota" / "approvals" / f"{BATCH}.json",
               {"approved": 8, "task": 4})
    if with_probe_report:
        _write(root / "protocol" / "report_probe.json",
               {"verdict": "NOT_MEASURED", "billing_lane": "repair_reserve",
                "batch_id": BATCH, "attempt": 1, "posts": 6,
                "root_before": "FROZEN", "root_after": "FROZEN",
                "studies": [{"stopped": "SOURCE_SHA_INVALID", "ok_count": 6}]})
    return root


def _ctx(root: Path):
    return RT.adopt_context("r1_test", root)


def _build(root: Path, **kwargs):
    kwargs.setdefault("clock", FIXED_CLOCK)
    kwargs.setdefault("container_facts",
                      {"probe": "docker ps -a", "available": True, "own": 0})
    return CL.build(_ctx(root), **kwargs)


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

def test_closeout_without_env_reads_explicit_root(tmp_path: Path,
                                                  monkeypatch) -> None:
    """AC35：env 全空也能结案（显式根）。"""
    monkeypatch.delenv("SIMPRAXIS_RSI_R1_CAMPAIGN", raising=False)
    root = _campaign(tmp_path)
    readout = _build(root)
    assert readout["axes"] == {"process": "COMPLETE",
                               "measurement": "INCOMPLETE",
                               "study_execution": "NOT_STARTED",
                               "claim": "NONE", "r1": "NOT_MEASURED"}
    assert readout["process_reasons"] == []
    for rel in (CL.READOUT_RELPATH, CL.FAILURE_MAP_RELPATH,
                CL.R2_READINESS_RELPATH, CL.SHADOW_RELPATH,
                CL.DELIVERABLE_RELPATH, "research_packets/A/status.json",
                "research_packets/B/status.json"):
        assert (root / rel).is_file(), rel
    assert (root / CL.SHADOW_RELPATH).read_text(encoding="utf-8") == ""


def test_cost_accounting_exact_and_unknown(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    readout = _build(root)
    cost = readout["cost"]
    assert cost["classification"] == {"posts": "EXACT", "tokens": "EXACT",
                                      "usd": "UNKNOWN"}
    assert readout["ledger"]["calls"] == 7
    assert readout["ledger"]["tokens"] == {"in": 10 + 600, "out": 5 + 300,
                                           "cache_create": 0, "cache_read": 0}
    assert cost["usd"]["value"] is None
    assert cost["usd"]["bound_max"] == "12.24"
    assert readout["approvals_not_used"] == {BATCH: 1}
    # AC34「未知不填 0」：没有机器计时的人工事件时，分钟数是 null 不是 0.0。
    assert readout["human"]["timing"] == "UNKNOWN"
    assert readout["human"]["known_minutes_total"] is None
    assert readout["human"]["known_seconds_total"] is None
    assert readout["human"]["out_of_band"]["machine_timed"] is False
    assert readout["human"]["complete"] is True


def test_anchor_is_self_verifying(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    readout = _build(root)
    body = dict(readout)
    anchor = body.pop("document_sha256")
    assert anchor == hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def test_missing_approval_and_bad_protocol_are_honest(tmp_path: Path) -> None:
    """缺批准件 ⇒ CLOSEOUT_INCOMPLETE，不写 COMPLETE；失败进 failure_map。"""
    root = _campaign(tmp_path, with_approval=False)
    readout = _build(root)
    assert readout["axes"]["process"] == "CLOSEOUT_INCOMPLETE"
    assert any("缺批准件" in r for r in readout["process_reasons"])
    failure_map = json.loads((root / CL.FAILURE_MAP_RELPATH).read_text(
        encoding="utf-8"))
    stages = [e["stage"] for e in failure_map["entries"]]
    assert "protocol/probe" in stages


def test_no_study_artifacts_are_required_for_not_started(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    _build(root)
    out = CHK.Checker(root).run()
    assert out["verdict"] == "PASS", out["problems"]
    assert out["closeout_present"] is True
    assert out["studies_started"] is False


# ---------------------------------------------------------------------------
# checker：结案分支必须拒伪造
# ---------------------------------------------------------------------------

def _mutate(path: Path, fn) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    _write(path, doc)


def _reanchor(doc) -> None:
    body = dict(doc)
    body.pop("document_sha256", None)
    doc["document_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def test_checker_rejects_upgraded_claim(tmp_path: Path) -> None:
    """研究未启动却写 MINIMAL_RESEARCH_LOOP_ESTABLISHED ⇒ 必拒。"""
    root = _campaign(tmp_path)
    _build(root)

    def fn(doc):
        doc["axes"].update({"study_execution": "COMPLETED",
                            "measurement": "VALID",
                            "claim": "SUPPORTED",
                            "r1": "MINIMAL_RESEARCH_LOOP_ESTABLISHED"})
        _reanchor(doc)

    _mutate(root / CL.READOUT_RELPATH, fn)
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-INVENTORY") for p in out["problems"])


def test_checker_rejects_broken_anchor(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    _build(root)
    _mutate(root / CL.READOUT_RELPATH,
            lambda d: d["ledger"].__setitem__("calls", 0))  # 不重算锚点
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-ANCHOR") for p in out["problems"])


def test_checker_rejects_ledger_rewrite(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    _build(root)
    _mutate(root / CL.READOUT_RELPATH,
            lambda d: (d["ledger"].__setitem__("ok", 1), _reanchor(d)))
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-LEDGER-ok") for p in out["problems"])


def test_checker_rejects_phantom_study_artifact(tmp_path: Path) -> None:
    """声明未启动，盘上却有 confirm 产物 ⇒ 矛盾。"""
    root = _campaign(tmp_path)
    _build(root)
    _write(root / "confirm" / "manifest_A.json", {"study_id": "A"})
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-INVENTORY") for p in out["problems"])


def test_checker_rejects_deliverable_overclaim(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    _build(root)
    _mutate(root / CL.DELIVERABLE_RELPATH,
            lambda d: d["studies"]["A"].update({"claim": "VERIFIED"}))
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-DELIVERABLE-A") for p in out["problems"])


def test_checker_rejects_mutated_old_root(tmp_path: Path) -> None:
    """AC36：carryover 登记 sha 与旧根现值不符 ⇒ FAIL。"""
    prior = tmp_path / "prior"
    (prior / "quota").mkdir(parents=True)
    prior_ledger = prior / "quota" / "ledger.jsonl"
    prior_ledger.write_text('{"event": "call", "seq": 1}\n', encoding="utf-8")
    declared = hashlib.sha256(prior_ledger.read_bytes()).hexdigest()
    root = _campaign(tmp_path)
    first_row = _ledger_rows()[0]
    carried_sha = hashlib.sha256(json.dumps(
        [first_row], ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    _write(root / "quota" / "carryover.json",
           {"source_root": str(prior), "source_ledger_sha256": declared,
            "carried_rows": 1, "carried_rows_sha256": carried_sha,
            "carried_calls": 1,
            "reason": "AC34 整轮新 root 不归零"})
    readout = _build(root)
    assert readout["old_root"]["unchanged"] is True
    assert CHK.Checker(root).run()["verdict"] == "PASS"
    prior_ledger.write_text('{"event": "call", "seq": 1}\n{"event": "call"}\n',
                            encoding="utf-8")
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert any(p.startswith("X-OLD-ROOT-UNCHANGED")
               for p in out["problems"])


def test_checker_still_requires_studies_without_readout(tmp_path: Path) -> None:
    """无 readout 不是「自动未启动」：仍按旧口径要求研究产物（缺件即 FAIL）。"""
    root = _campaign(tmp_path)
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    assert out["closeout_present"] is False
    assert any("manifest_A" in p for p in out["problems"])


def test_adopt_context_refuses_foreign_root(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    with pytest.raises(RT.CampaignRootRefused):
        RT.adopt_context("other_env", root)


# ---------------------------------------------------------------------------
# 评审修复的负面用例（CRITICAL/M1–M7）
# ---------------------------------------------------------------------------

def _journal_lines(entries) -> str:
    """按生产 Journal 的链式 sha 生成 human_events 行（测试侧独立实现）。"""
    prev, out = None, []
    for i, (event, payload, ts) in enumerate(entries, 1):
        doc = {"schema": "simpraxis.rsi_r1.event.v1", "seq": i, "ts_iso": ts,
               "event": event, "payload": payload, "prev_sha256": prev}
        sha = hashlib.sha256(json.dumps(
            doc, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        doc["sha256"] = sha
        prev = sha
        out.append(json.dumps(doc, ensure_ascii=False) + "\n")
    return "".join(out)


def _human_event_file(root: Path, *, end: bool = True) -> None:
    entries = [("human_event_begin",
                {"event_id": "he-1", "kind": "approval", "actor": "user",
                 "reason": "Gate2 真人复核", "artifact_refs": [],
                 "start_utc": "2026-11-11T10:00:00Z", "study": None,
                 "touch": None}, "2026-11-11T10:00:00Z")]
    if end:
        entries.append(("human_event_end",
                        {"event_id": "he-1", "end_utc": "2026-11-11T10:30:00Z"},
                        "2026-11-11T10:30:00Z"))
    path = root / "human_events" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_journal_lines(entries), encoding="utf-8")


def test_human_minutes_come_from_machine_timing(tmp_path: Path) -> None:
    root = _campaign(tmp_path)
    _human_event_file(root)
    readout = _build(root)
    assert readout["human"]["n_events"] == 1
    assert readout["human"]["timing"] == "TIMED"
    assert readout["human"]["known_minutes_total"] == 30.0
    assert CHK.Checker(root).run()["verdict"] == "PASS"
    # 改分钟数（并重算锚点）必须被独立折叠识破。
    def fn(doc):
        doc["human"]["known_minutes_total"] = 0.0
        doc["human"]["known_seconds_total"] = 0.0
        _reanchor(doc)
    _mutate(root / CL.READOUT_RELPATH, fn)
    out = CHK.Checker(root).run()
    assert any(p.startswith("X-HUMAN-MINUTES") for p in out["problems"])


def test_human_unknown_event_never_zero(tmp_path: Path) -> None:
    """未闭合事件 ⇒ timing UNKNOWN、分钟 null；机判不得把它读成 0。"""
    root = _campaign(tmp_path)
    _human_event_file(root, end=False)
    readout = _build(root)
    assert readout["human"]["timing"] == "UNKNOWN"
    assert readout["human"]["known_minutes_total"] is None
    assert readout["human"]["complete"] is False
    out = CHK.Checker(root).run()
    assert out["verdict"] == "PASS", out["problems"]


def test_checker_rejects_forged_axes_with_recomputed_anchor(tmp_path: Path) -> None:
    """CRITICAL：把 claim/r1 抬到最强并重算锚点，独立机判仍须拒。"""
    root = _campaign(tmp_path)
    _build(root)

    def fn(doc):
        doc["axes"].update({"study_execution": "COMPLETED",
                            "measurement": "VALID", "claim": "SUPPORTED",
                            "r1": "MINIMAL_RESEARCH_LOOP_ESTABLISHED"})
        doc["process_reasons"] = []
        _reanchor(doc)

    _mutate(root / CL.READOUT_RELPATH, fn)
    out = CHK.Checker(root).run()
    assert out["verdict"] == "FAIL"
    ids = {p.split(":")[0] for p in out["problems"]}
    assert "X-AXES-R1-MINIMAL" in ids
    assert "X-AXES-NO-JUDGE" in ids


def test_checker_rejects_approval_removed_after_build(tmp_path: Path) -> None:
    """M1：批报件在 build 后被删，机判必须从盘面重算出来，不信 readout。"""
    root = _campaign(tmp_path)
    _build(root)
    assert CHK.Checker(root).run()["verdict"] == "PASS"
    (root / "quota" / "approvals" / f"{BATCH}.json").unlink()
    out = CHK.Checker(root).run()
    ids = {p.split(":")[0] for p in out["problems"]}
    assert "X-BATCH-R1-RP1-probe1-APPROVAL" in ids
    assert "X-PROCESS" in ids


def test_checker_rejects_failure_evidence_sha_mismatch(tmp_path: Path) -> None:
    """M5：失败谱系证据的登记 sha 与盘上字节不符 ⇒ FAIL。"""
    root = _campaign(tmp_path)
    _build(root)
    evidence = root / "protocol" / "report_probe.json"

    def fn(doc):
        doc["entries"][0]["evidence"][0]["sha256"] = "0" * 64

    _mutate(root / CL.FAILURE_MAP_RELPATH, fn)
    assert evidence.is_file()
    out = CHK.Checker(root).run()
    assert any(p.startswith("X-FAILURE-EVIDENCE-SHA") for p in out["problems"])


def test_repair_evidence_retained_with_sha(tmp_path: Path) -> None:
    """§8.1 授权件的证据项必须进失败谱系（旧实现会静默丢成 evidence: []）。"""
    root = _campaign(tmp_path)
    defect = tmp_path / "defect.txt"
    defect.write_text("boom", encoding="utf-8")
    sha = hashlib.sha256(b"boom").hexdigest()
    repair_doc = {"evidence": {"d01": {"path": str(defect), "sha256": sha,
                                       "bytes": 4}}}
    entries = CL._failure_entries(root, {}, repair_doc, {})
    auth = [e for e in entries if e["id"] == "repair_authorization"][0]
    assert auth["evidence"], "授权件证据不得为空"
    assert auth["evidence"][0]["sha256"] == sha
    assert auth["evidence"][0]["declared_sha256"] == sha
    assert auth["evidence"][0]["sha_match"] is True


def test_adopt_context_refuses_archived_root(tmp_path: Path) -> None:
    """M6：§8.1 换 root 后旧根进家族登记，结案工具拒绝往里写。"""
    root = _campaign(tmp_path)
    RT.register_archived_root(root, reason="§8.1 换 root 旧根只读",
                              clock="2026-11-11T12:00:00Z")
    assert RT.is_archived_root(root) is not None
    with pytest.raises(RT.CampaignRootRefused):
        RT.adopt_context("r1_test", root)


def test_pack_must_bind_to_the_same_root(tmp_path: Path) -> None:
    """MINOR：manifest 的 campaign_root 必须是被核对的根，否则换个根也「验过」。"""
    root = _campaign(tmp_path)
    _build(root)
    pack = tmp_path / "pack"
    pack.mkdir()
    payload = pack / "a.txt"
    payload.write_text("x", encoding="utf-8")
    _write(pack / "evidence_manifest.json", {
        "campaign_root": "/somewhere/else", "n_files": 1, "total_bytes": 1,
        "files": {"a.txt": hashlib.sha256(b"x").hexdigest()}})
    checker = CHK.Checker(root)
    checker.verify_pack(pack)
    root_check = [c for c in checker.checks if c["id"] == "P-MANIFEST-ROOT"][0]
    assert root_check["ok"] is False
