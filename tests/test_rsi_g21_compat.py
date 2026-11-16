"""RSI-G2.1 兼容：legacy 逐字、G2 动作表不变、分页回包不进通用裁剪。"""
from __future__ import annotations

import inspect
import json

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import loop as L


def test_legacy_clipping_is_byte_identical():
    small = T.Turn(model_text="m", result={"ok": True, "action": "analyze",
                                           "n_runs": 3})
    assert small.clipped(policy=T.POLICY_LEGACY).result == {
        "ok": True, "action": "analyze", "n_runs": 3}
    big = T.Turn(model_text="m", result={"ok": True, "action": "analyze",
                                         "blob": "x" * 9000})
    view = big.clipped(policy=T.POLICY_LEGACY)
    size = len(json.dumps(dict(view.result), ensure_ascii=False,
                          sort_keys=True).encode("utf-8"))
    assert size <= T.TOOL_REPLY_MAX_BYTES
    assert view.result.get("truncated") is True
    assert "action" in view.result or "code" in view.result


def test_read_page_is_atomic_under_g2_policy():
    data = ("z" * 8000).encode()
    page = {"ok": True, "action": "read_result_artifact",
            "artifact_id": "analysis/output.json",
            "artifact_sha256": T.sha_bytes(data) if hasattr(T, "sha_bytes")
            else "0" * 64,
            "offset": 0, "n_bytes": len(data), "total_bytes": len(data),
            "page_sha256": "0" * 64, "encoding": "utf-8",
            "data": data.decode(), "next_cursor": None}
    import hashlib
    page["artifact_sha256"] = hashlib.sha256(data).hexdigest()
    page["page_sha256"] = hashlib.sha256(data).hexdigest()
    view = T.Turn(model_text="m", result=page).clipped(
        policy=T.POLICY_G2_ATOMIC)
    assert view.result["data"] == data.decode()
    assert view.result["truncated"] is False
    # 超过页帽 ⇒ 发网前阻断
    oversized = dict(page, n_bytes=T.G2_PAGE_MAX_BYTES + 1,
                     data="z" * (T.G2_PAGE_MAX_BYTES + 1))
    with pytest.raises(T.ResultViewIncomplete):
        T.Turn(model_text="m", result=oversized).clipped(
            policy=T.POLICY_G2_ATOMIC)


def test_action_registries_are_separate():
    assert len(L.ACTION_NAMES) == 7
    assert "read_result_artifact" in L.G2_ACTION_NAMES
    assert "read_result_artifact" not in L.ACTION_NAMES
    system = L.render_system()
    for name in L.ACTION_NAMES:
        assert name in system
    assert "read_result_artifact" not in system
    action = L.parse_action(json.dumps({
        "action": "read_result_artifact", "artifact_id": "analysis/x.json",
        "output_sha256": "a" * 64, "cursor": 0, "page_bytes": 4096}))
    assert action.name == "read_result_artifact"


def test_read_action_field_validation():
    bad = {"action": "read_result_artifact", "artifact_id": "private/x.json",
           "output_sha256": "a" * 64, "cursor": 0, "page_bytes": 4096}
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps(bad))
    assert exc.value.code == "ARTIFACT_FORBIDDEN"
    bad["artifact_id"] = "analysis/x.json"
    bad["page_bytes"] = 8193
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps(bad))
    assert exc.value.code == "PAGE_TOO_LARGE"


def test_dispatcher_task_contrast_flag_defaults_off():
    params = inspect.signature(A.CampaignDispatcher.__init__).parameters
    assert params["require_task_contrasts"].default is False
    assert G2.derive_required_ids(_FakeTask()) == ["primary", "nc"]


class _FakeTask:
    class _Primary:
        id = "primary"

    primary = _Primary()
    negative_control_spec = {"contrast": {"id": "nc"}}
