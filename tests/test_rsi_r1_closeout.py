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
    assert readout["human"]["known_minutes_total"] == 0.0
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
