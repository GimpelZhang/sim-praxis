"""RSI-G1 边界捕获（**新运行**；零模型 POST / 零物理）。

Plan §3 G1 与 §5.1 的核心缺口：历史请求字节没归档，所以只能 `UNOBSERVABLE`。
本模块在**生产边界**旁边记录四个事实（全部由宿主在发生时写下，不是事后重建）：

* ``request_serialized``：``T.build_request`` 的 ``serialized`` 原字节（送网关前）；
* ``response_raw``：``L.Reply.text`` 原字节；
* ``action_parsed`` / ``dispatch_return``：解析后的动作与 dispatcher 返回对象；
* ``clipped_view``：``Turn.clipped()`` 的**实际对象**（下一请求真正会看到的视图）。

捕获只写 G1 根，不改变任何生产 payload。`run_capture_demo` 用一个
**脚本化 sender + 合成 dispatcher** 跑通一次真实 `ResearchLoop.turn()`（零
模型、零物理、零网络），证明铺设正确；它不产生任何科学结论。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.common import research_transport as T
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import g1_trace as GT
from orchestrator.rsi_r1 import loop as L

SCHEMA_SUMMARY = "simpraxis.rsi_g1.capture_demo.v1"


class CaptureRecorder:
    """把四个生产边界事实写成 G1 事件（``observed``）。"""

    def __init__(self, root: Path, *, diagnostic_id: str,
                 source_commit: str = "", study: str = "A") -> None:
        self.root = Path(root)
        self.study = study
        self.writer = GT.TraceWriter(Path(root) / "trace" / "capture",
                                     diagnostic_id=diagnostic_id,
                                     source_commit=source_commit)

    def _add(self, kind: str, data: bytes, *, phase: str, extra=None,
             request_sha: "str | None" = None) -> str:
        digest = GT.sha_bytes(data)
        rel = GT.materialize(self.root, "capture", data)
        self.writer.add(GT.TraceEvent(
            diagnostic_id=self.writer.diagnostic_id, seq=0, event_kind=kind,
            visibility="observed", study_id=self.study, phase=phase,
            artifact_ref=rel, bytes_sha256=digest, n_bytes=len(data),
            source_commit=self.writer.source_commit,
            request_sha256=request_sha, extra=dict(extra or {})))
        return rel

    def record_request(self, request: "T.Request", *, phase: str) -> str:
        return self._add("request_serialized",
                         request.serialized.encode("utf-8"), phase=phase,
                         request_sha=request.sha256)

    def record_reply(self, reply: Any, request: "T.Request", *,
                     phase: str) -> None:
        self._add("response_raw", str(reply.text).encode("utf-8"), phase=phase,
                  request_sha=request.sha256,
                  extra={"response_id": reply.response_id})

    def record_dispatch(self, action: Any, result: Any, *,
                        phase: str) -> str:
        self._add("action_parsed", GT.canonical(action.to_doc()), phase=phase)
        return self._add("dispatch_return", GT.canonical(result), phase=phase)

    def record_clipped(self, turn: "T.Turn", *, phase: str) -> None:
        self._add("clipped_view", GT.canonical(dict(turn.result)), phase=phase,
                  extra={"retained_keys": sorted(turn.result)})

    def close(self) -> "dict[str, Any]":
        return self.writer.write()


# ---------------------------------------------------------------------------
# 演示运行（脚本化 sender + 合成 dispatcher；零模型/零物理/零网络）
# ---------------------------------------------------------------------------

class _StubStore:
    def __init__(self) -> None:
        self.turns: "list[T.Turn]" = []
        self.events: "list[dict[str, Any]]" = []

    def history(self) -> "list[T.Turn]":
        return list(self.turns)

    def record_reply(self, *, response_id: str, text: str, phase: str,
                     nonce: str = "") -> None:
        self.events.append({"kind": "reply", "id": response_id})

    def record_action(self, *, response_id: str, action: Any) -> None:
        self.events.append({"kind": "action", "id": response_id})

    def record_result(self, *, response_id: str, result: Any) -> None:
        self.events.append({"kind": "result", "id": response_id})
        self.turns.append(T.Turn(model_text=self._last_text,
                                 result=dict(result)))

    def stop_or_block(self, *, code: str, detail: str = "") -> None:
        self.events.append({"kind": "stop", "code": code, "detail": detail})

    _last_text = ""

    def remember_text(self, text: str) -> None:
        self._last_text = text


class _StubSender:
    def __init__(self, text: str) -> None:
        self.text = text

    def send_once(self, request: "T.Request", reservation: Any) -> Any:
        return L.Reply(text=self.text, response_id="g1cap-1",
                       is_actionable=True)


class _StubDispatcher:
    """返回超帽的 confirm 形状结果（用来演示裁剪丢字段）。"""

    def execute_once(self, action: Any, store: Any) -> "dict[str, Any]":
        contrasts = [{"contrast_id": "friction_x_speed",
                      "metric_id": "displacement_x_m",
                      "proposition": "increase_gt", "delta": "0.001",
                      "estimate": 0.040867675677873194,
                      "lower": 0.040860710191191175,
                      "upper": 0.04087292938493192,
                      "status": "SUPPORTED", "n_blocks": 32}]
        diagnostics = {"contrasts": [{
            "contrast_id": "friction_x_speed",
            "effect": 0.040867675677873194,
            "block_effects": [{"block_id": f"b{i:02d}",
                               "effect": 0.040867675677873194 + i * 1e-6}
                              for i in range(32)]}],
            "coverage": {"blocks": [f"b{i:02d}" for i in range(32)],
                         "run_count": 160}}
        return {"ok": True, "action": action.name,
                "candidate_output": {"schema": "simpraxis.rsi_r1.analysis_output.v1",
                                     "schema_version": 1,
                                     "run_ids": [f"run-{i:04d}" for i in range(160)],
                                     "contrasts": contrasts,
                                     "diagnostics": diagnostics},
                "analysis_receipt": {"sha256": "f" * 64},
                "evidence_index": {"analysis/output.json": "a" * 64,
                                   "analysis/receipt.json": "b" * 64,
                                   "confirm/blocks.json": "c" * 64},
                "primary_contrast_id": "friction_x_speed", "n_runs": 160,
                "result_sha256": "d" * 64, "run_ids_head": ["run-0000"]}


class _StubAdmission:
    def check_all(self, **kwargs: Any) -> Any:
        return SimpleNamespace(envelope_id="g1-capture-demo")


class _StubBudget:
    def reserve_atomic(self, *, call_id: str, permit: Any) -> Any:
        return SimpleNamespace(call_id=call_id)

    def resolve_or_pause(self, reply: Any) -> None:
        return None


def run_capture_demo(*, root: Path, diagnostic_id: str,
                     source_commit: str = "") -> "dict[str, Any]":
    """跑一次真实 ``ResearchLoop.turn()``（脚本化 sender），记录边界字节。"""
    root = Path(root)
    ensure_private_dir(root / "trace" / "capture")
    recorder = CaptureRecorder(root, diagnostic_id=diagnostic_id,
                               source_commit=source_commit)
    store = _StubStore()
    action_doc = {"action": "submit_claim",
                  "claims": [{"hypothesis_id": "h", "proposition": "increase_gt",
                              "metric_id": "displacement_x_m", "delta": 0.001,
                              "estimate": 0.0407, "lower": 0.0401,
                              "upper": 0.0413}],
                  "report_text": "x",
                  "research_report": {"task_id": "capture-demo"}}
    text = json.dumps(action_doc, ensure_ascii=False)
    sender = _StubSender(text)
    dispatcher = _StubDispatcher()

    def messages() -> "T.Request":
        return T.build_request(system="g1 capture demo（无模型）",
                               task_brief="证明边界捕获铺设；不产生科学结论。",
                               turns=store.history(),
                               counter=lambda s: max(1, len(s) // 4),
                               max_tokens=8192)

    original_send = sender.send_once

    def send_once(request: "T.Request", reservation: Any) -> Any:
        reply = original_send(request, reservation)
        store.remember_text(reply.text)
        return reply

    sender.send_once = send_once  # type: ignore[method-assign]
    loop = L.ResearchLoop(sender=sender, parser=L.parse_action,
                          dispatcher=dispatcher, store=store,
                          messages=messages, counter=lambda s: 1,
                          admission=_StubAdmission(), budget=_StubBudget(),
                          envelope=None, root_lock=None,
                          spend_contract=None, phase="confirm",
                          capture=recorder)
    outcome = loop.turn()

    # 下一回合请求（真实 build_request）：它看到上一回合的裁剪视图
    next_request = messages()
    next_ref = recorder.record_request(next_request, phase="confirm")
    # R1：本运行没有网关发送适配器捕获 ⇒ 按 Plan §2.1 记 MISSING，不补造
    recorder.writer.add(GT.TraceEvent(
        diagnostic_id=recorder.writer.diagnostic_id, seq=0,
        event_kind="gateway_capture", visibility="missing",
        study_id=recorder.study, phase="confirm", status="MISSING",
        error_code="MISSING",
        missing_reason="演示运行不经网关；生产无 gateway capture 适配器",
        source_commit=recorder.writer.source_commit))
    manifest = recorder.close()
    payload = next_request.serialized
    # R2：相邻 OBSERVED 节点的首个分歧（本运行可定位）
    est_marker = "0.040867675677873194"
    dispatch_ref = [event for event in recorder.writer.events
                    if event.event_kind == "dispatch_return"][-1].artifact_ref
    first_divergence = ("RETURN_VIEW" if est_marker not in next_request.serialized
                        else "PERSISTED_CLAIM")
    summary = {
        "schema": SCHEMA_SUMMARY, "schema_version": 1,
        "kind": "capture_demo",
        "first_divergence": first_divergence,
        "divergence_evidence": {"dispatch_return": dispatch_ref,
                                "next_request": next_ref,
                                "claim_in_next_request": est_marker in
                                next_request.serialized},
        "diagnostic_id": diagnostic_id,
        "turn_ok": bool(outcome.ok), "action": outcome.action,
        "next_request": {"sha256": next_request.sha256,
                         "n_bytes": next_request.n_bytes,
                         "carries_candidate_output": "candidate_output" in payload,
                         "carries_estimate": "0.040867675677873194" in payload},
        "n_events": manifest["n_events"],
        "kinds": manifest["kinds"],
        "created_at_utc": utc_now_iso_z(),
        "rule": ("新运行的边界捕获只证明本次运行；不 retrofit 轮 O、"
                 "不产生科学结论"),
    }
    ensure_private_dir(root / "trace")
    text_out = json.dumps(summary, ensure_ascii=False, sort_keys=True,
                          indent=1) + "\n"
    atomic = root / "trace" / "capture_summary.json"
    from orchestrator.common.fsutil import atomic_write_text
    atomic_write_text(atomic, text_out, mode=0o600)
    summary["summary_sha256"] = GT.sha_bytes(text_out.encode("utf-8"))
    return summary
