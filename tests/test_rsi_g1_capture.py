"""RSI-G1 边界捕获、状态机、隔离与预算守卫（零模型/零物理/零网络）。"""
from __future__ import annotations

import inspect
import json
import os

import pytest

from orchestrator.rsi_r1 import g1_capture as GC
from orchestrator.rsi_r1 import g1_delivery as DV
from orchestrator.rsi_r1 import g1_probe as GP
from orchestrator.rsi_r1 import g1_trace as GT
from orchestrator.rsi_r1 import loop as L


def test_capture_demo_records_four_boundaries(tmp_path):
    summary = GC.run_capture_demo(root=tmp_path, diagnostic_id="g1t")
    assert summary["turn_ok"] and summary["action"] == "submit_claim"
    assert GT.verify_trace(tmp_path / "trace" / "capture")["ok"]
    for kind in ("request_serialized", "response_raw", "action_parsed",
                 "dispatch_return", "clipped_view"):
        assert kind in summary["kinds"], kind


def test_capture_shows_candidate_output_lost_before_next_request(tmp_path):
    """新运行证据：下一请求（真实 build_request）里没有 candidate_output。"""
    summary = GC.run_capture_demo(root=tmp_path, diagnostic_id="g1t")
    assert summary["next_request"]["carries_candidate_output"] is False
    assert summary["next_request"]["carries_estimate"] is False


def test_loop_capture_is_opt_in_default_none():
    sig = inspect.signature(L.ResearchLoop.__init__)
    assert sig.parameters["capture"].default is None


def test_state_machine_strict_and_advisory(tmp_path):
    st = GT.G1State(tmp_path, diagnostic_id="g1t")
    assert st.load()["step"] == "REGISTERED"
    with pytest.raises(GT.G1TraceError) as exc:
        st.advance("TRACED", strict=True)
    assert exc.value.code == "STEP_ORDER"
    doc = st.advance("TRACED")          # advisory
    assert doc["warnings"][-1]["out_of_order_from"] == "REGISTERED"
    doc = st.advance("INVENTORIED", strict=True)
    assert doc["step"] == "INVENTORIED"
    with pytest.raises(GT.G1TraceError):
        st.advance("CLOSED", strict=True)


def test_verify_inputs_detects_change(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.json"
    f.write_text("{}", encoding="utf-8")
    os.chmod(f, 0o600)
    man = GT.inventory_evidence([GT.RootSpec("r", src)],
                                [GT.FileSpec("r", "a.json", "x")],
                                out_root=tmp_path / "o", diagnostic_id="g1t")
    path = tmp_path / "o" / "manifest" / "input_manifest.json"
    assert GT.verify_inputs_unchanged(path)["ok"]
    f.write_text('{"changed":1}', encoding="utf-8")
    report = GT.verify_inputs_unchanged(path)
    assert report["ok"] is False and report["changed"] == ["r:a.json"]


def test_maintenance_store_is_rejected_by_compiler(tmp_path):
    class _Maint:
        maintenance = True
    with pytest.raises(DV.DeliveryError) as exc:
        DV.compile_claim(None, _Maint())
    assert exc.value.code == "STORE_ISOLATION"


def test_a_layer_guard_blocks_sender(tmp_path):
    with GP.a_layer_guard():
        with pytest.raises(GP.ProbeError) as exc:
            GP.build_sender(root=tmp_path, diagnostic_id="g1t")
        assert exc.value.code == "G1_BLOCKED_PAID_GATE"
