#!/usr/bin/env python3
"""Phase 2.7 Task 5 真人飞书演练（计划 §T5.4-F / AC-T5-3，0 付费）。

把一条**合成的两 Stage 路线图**推给真人，记录三次点击的全过程：

  ① Gate 1：整条路线图核准（`build_roadmap_card`）
  ② Stage 1 终审：放行（`build_stage_gate_card`，approve）
  ③ Stage 2 终审：驳回（`build_stage_gate_card`，reject_with_comments）

驳回之后立即停止（不再发第四张卡）：检查点里必须留下 gate2_rejected 事件、
Stage 2 的 stage_status 未变成 approved、stage_index 未前进——即 AC-T5-1
「Stage 2 被驳回只能重试 Stage 2」的真人版证据。

纪律：
- **No Fake Approval**：三次结论全部来自真人卡片回调（owner 白名单 + 空意见
  二次校验 + 跨 Gate 交叉校验全在 `FeishuGateway._handle_card` 既有路径上），
  脚本里没有任何「如果等太久就代点」的分支；超时只抛 TimeoutError。
- **0 付费**：只走飞书通道，不构网关、不触 ledger。
- **证据三件互证**（AC-T5-3）：每条记录同时落
  ① 卡片 `message_id`（飞书侧可按时间窗查到）
  ② 回调到达时刻（`on_card_action` 里记录的本机 UTC 时间戳）
  ③ 检查点里的决策事件（`gate1_approved` / `gate2_accepted` / `gate2_rejected`
     + `stage_id`）与 pending 行终态。

用法：
    source scripts/00_env.sh
    /mnt/venvs/simpraxis/bin/python scripts/phase27/drill_roadmap_cards.py \
        --max-wait 3600
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402
from orchestrator.feishu_gateway.cards import (  # noqa: E402
    build_roadmap_card,
    build_stage_gate_card,
)
from orchestrator.feishu_gateway.client import gateway_from_env  # noqa: E402
from orchestrator.graph.nodes import GraphDeps, build_graph  # noqa: E402
from orchestrator.graph.roadmap import (  # noqa: E402
    ROADMAP_SCHEMA,
    grants_for_stage,
    stage_statuses,
    stage_to_mapping,
    validate_roadmap,
)

THREAD_ID = "p27-t5-drill-001"
TASK_TITLE = "Phase 2.7 Task 5 多 Stage 路线图演练（合成路线图，0 付费）"

# 全局上限 = Gate 1 选中提案携带的 tool_grants（计划 §T5.3 D5-4：全局只是上限）。
GLOBAL_GRANTS = ("read_file", "list_dir", "write:", "run:python3:-c")

ROADMAP_DOC: dict[str, Any] = {
    "schema": ROADMAP_SCHEMA,
    "title": "演练：环境出生 → 接触推箱（合成，不跑真仿真）",
    "stages": [
        {
            "stage_id": "s1_env_boot",
            "title": "环境出生与可加载性",
            "objective": "证明沙盒能起来、依赖可导入；不要求任何物理指标",
            "exit_ac": ["AC-S1-1 环境构建成功", "AC-S1-2 依赖版本与 pin 一致"],
            # 全部落在全局上限内：本 Stage 拿到的授予 = 声明的三项
            "tool_grants": ["read_file", "list_dir", "run:python3:-c"],
            "max_rounds": 8,
        },
        {
            "stage_id": "s2_contact_push",
            "title": "接触推箱与遥测落盘",
            "objective": "产出可核验的遥测文件；数值由公式生成并自标 fixture",
            "exit_ac": ["AC-S2-1 telemetry 文件存在", "AC-S2-2 指标可读且非 NaN"],
            # 刻意含两项：write: 在全局上限内；grep **不在**全局上限里
            # ⇒ grants_for_stage 必须把它丢掉（AC-T5-2 的真人版证据）
            "tool_grants": ["write:", "grep"],
            "max_rounds": 8,
        },
    ],
}


class DrillRecords:
    """三次点击的机读记录（message_id / 回调时刻 / 决策）。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.callbacks: list[dict[str, Any]] = []

    def on_card_action(self, info: dict[str, Any]) -> None:
        """网关回调观察者：记录**回调到达时刻**（本机 UTC）与原始判定。"""
        self.callbacks.append({
            "received_at_utc": utc_now_iso_z(),
            "received_at_monotonic": time.monotonic(),
            "message_id": info.get("message_id"),
            "gate": info.get("gate"),
            "decided": info.get("decided"),
            "decision": info.get("decision"),
            "choice": info.get("choice"),
            "reason": info.get("reason"),
        })

    def card_sent(self, gate: int, card: dict, sent: dict, label: str) -> None:
        self.rows.append({
            "gate": gate,
            "label": label,
            "card_schema": card.get("schema"),
            "message_id": sent.get("message_id"),
            "chat_id": sent.get("chat_id"),
            "wire_schema": sent.get("schema"),
            "sent_at_utc": utc_now_iso_z(),
        })


def _evidence_dir(override: "str | None") -> Path:
    if override:
        root = Path(override)
    else:
        base = os.environ.get("SIMPRAXIS_PHASE27_ROOT")
        if not base:
            raise SystemExit(
                "SIMPRAXIS_PHASE27_ROOT 未设置；先 source scripts/00_env.sh")
        root = Path(base) / "task5_roadmap"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


class DrillGate:
    """发一张卡、等真人点击、落三件互证记录（绝不代点）。"""

    def __init__(self, gw, records: DrillRecords, evidence: Path, *,
                 max_wait: float) -> None:
        self.gw = gw
        self.records = records
        self.evidence = evidence
        self.max_wait = max_wait
        self.snapshots: list[dict[str, Any]] = []

    def ask(self, gate: int, card: dict, label: str) -> dict:
        snap = self.evidence / "card_snapshots" / (
            f"gate{gate}_{label}_{int(time.time())}.json")
        snap.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(snap.parent, 0o700)
        _write_json(snap, card)
        os.chmod(snap, 0o600)
        # 同一 (thread, gate) 的上一轮终态行先归档，保证新卡新点击（Task7 D10）。
        self.gw._reopen_round(THREAD_ID, gate)
        sent = self.gw._send_interactive(self.gw.owner_open_id, card, card,
                                         form_required=True)
        if not sent.get("message_id"):
            raise RuntimeError(f"Gate{gate} 卡片发送失败：{sent}")
        self.gw.store.upsert_pending(
            THREAD_ID, gate, card_message_id=sent["message_id"],
            open_chat_id=sent.get("chat_id"), task_title=TASK_TITLE)
        self.records.card_sent(gate, card, sent, label)
        self.gw._event_for(THREAD_ID, gate)
        decision = self.gw._await_decision(THREAD_ID, gate,
                                           max_wait=self.max_wait)
        row = self.gw.store.get_pending(THREAD_ID, gate) or {}
        self.records.rows[-1].update({
            "decision": decision.get("decision"),
            "human_notes": decision.get("human_notes"),
            "choice": decision.get("choice"),
            "decided_at_utc": utc_now_iso_z(),
            "pending_status": row.get("status"),
            "pending_source": row.get("source"),
        })
        self.snapshots.append(dict(self.records.rows[-1]))
        return decision


def _make_agent_run(run_root: Path, stage_log: list[dict[str, Any]]):
    """公式生成的 telemetry 夹具（No Fake Physics：数值由公式给出并自标 fixture）。

    逐 Stage 产出独立文件（Stage 隔离：驳回只重跑当前 Stage，前序文件不重写）。
    """

    def seam(state: dict) -> dict:
        frame = state.get("stage_frame") or {}
        stage_id = frame.get("stage_id") or "?"
        n = len(stage_log) + 1
        t = 8
        tele = {
            "_fixture": True,
            "_note": "Phase 2.7 Task5 演练合成遥测；数值由公式生成，非真实仿真",
            "_formula": "penetration_m=0.001*m; force_k=1.0+0.1*sin(k)",
            "stage_id": stage_id,
            "engine": "drill-fixture",
            "run_id": f"drill-run-{n}",
            "dt": 0.016,
            "penetrations": [0.001 * m for m in range(t)],
            "contact_forces": [1.0 + 0.1 * (((k - 3) % 3) - 1) for k in range(t)],
        }
        path = run_root / f"telemetry_{n}_{stage_id}.json"
        _write_json(path, tele)
        os.chmod(path, 0o600)
        stage_log.append({"n": n, "stage_id": stage_id,
                          "telemetry": str(path),
                          "granted_tools": list(frame.get("tool_grants") or [])})
        return {"telemetry_uri": str(path), "coding_round": 0}

    return seam


def _make_persist():
    """演练专用 persist：**不写任何库**，只回一份摘要（0 付费、0 表写入）。"""

    def seam(state: dict) -> dict:
        return {"written": 0,
                "note": "drill：不写 phase1_learnings/phase2_learnings"
                        "（本 Task 0 付费且不动活表）"}

    return seam


def _build_deps(gw, records, evidence: Path, run_root: Path,
                stage_log: list[dict[str, Any]], *, max_wait: float) -> GraphDeps:
    gate = DrillGate(gw, records, evidence, max_wait=max_wait)
    stages = validate_roadmap(ROADMAP_DOC)

    def direction(state: dict) -> dict:
        # 全局上限随 Gate 1 提案下达（D5-4：全局 grants 只是上限）。
        return {"proposals": [{
            "name": "演练路线图（合成）",
            "sim_engine": "drill-fixture",
            "first_principles": "两 Stage：环境出生 → 接触推箱",
            "expected_artifacts": ["telemetry_*.json"],
            "tool_grants": list(GLOBAL_GRANTS),
        }], "question": state.get("question", "")}

    def gate1(state: dict) -> dict:
        return gate.ask(1, build_roadmap_card(ROADMAP_DOC), "roadmap")

    def gate2(state: dict) -> dict:
        frame = state.get("stage_frame") or {}
        index = int(state.get("stage_index", 0) or 0)
        arts = [{"name": Path(a["telemetry"]).name,
                 "detail": f"stage={a['stage_id']}（合成 fixture）"}
                for a in stage_log if a["stage_id"] == frame.get("stage_id")]
        card = build_stage_gate_card(frame, arts, stage_index=index,
                                     total_stages=len(stages))
        return gate.ask(2, card, f"stage{index + 1}_{frame.get('stage_id')}")

    return GraphDeps(
        direction=direction,
        gate1=gate1,
        agent_run=_make_agent_run(run_root, stage_log),
        gate2=gate2,
        persist=_make_persist(),
        task_title=TASK_TITLE,
        run_dir=run_root,
    ), gate


def _checkpoint_state(db_path: Path, thread_id: str) -> dict:
    """读最新检查点里的状态（sqlite 直读，不经 langgraph 反序列化）。"""
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT checkpoint FROM checkpoints WHERE thread_id = ? "
            "ORDER BY checkpoint_id DESC LIMIT 1", (thread_id,)).fetchall()
    finally:
        con.close()
    if not rows:
        return {}
    import base64
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    serde = JsonPlusSerializer()
    raw = rows[0][0]
    if isinstance(raw, str):
        raw = raw.encode()
    return serde.loads_typed((serde.__class__.__name__, raw)) \
        if False else serde.loads(raw)


def _render_report(records: DrillRecords, snapshots: list[dict[str, Any]],
                   checkpoint: dict, stage_log: list[dict[str, Any]],
                   stages) -> str:
    lines = ["# Phase 2.7 Task 5 真人飞书演练记录", ""]
    lines.append(f"- 线程：`{THREAD_ID}`")
    lines.append(f"- 路线图：`{ROADMAP_SCHEMA}`，{len(stages)} 个 Stage")
    lines.append(f"- 生成时刻（本机 UTC）：{utc_now_iso_z()}")
    lines.append("")
    lines.append("## 三条真人点击记录（message_id + 回调时刻 + 决策）")
    lines.append("")
    lines.append("| # | 卡 | gate | message_id | 发出（UTC） | 回调时刻（UTC） | 决策 | 意见 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, row in enumerate(snapshots, 1):
        cb = next((c for c in records.callbacks
                   if c.get("message_id") == row.get("message_id")
                   and c.get("decided")), {})
        lines.append(
            f"| {i} | {row['label']} | {row['gate']} | `{row['message_id']}` | "
            f"{row['sent_at_utc']} | {cb.get('received_at_utc', '-')} | "
            f"{row.get('decision', '-')} | {(row.get('human_notes') or '-')} |")
    lines.append("")
    lines.append("## 检查点侧互证（LangGraph checkpoint）")
    lines.append("")
    events = checkpoint.get("events") or []
    lines.append(f"- `stage_index` = {checkpoint.get('stage_index')}")
    lines.append(f"- `stage_status` = {json.dumps(checkpoint.get('stage_status'), ensure_ascii=False)}")
    lines.append(f"- 决策事件：{json.dumps([e['type'] for e in events if str(e.get('type','')).startswith('gate')], ensure_ascii=False)}")
    rejects = [e for e in events if e.get("type") == "gate2_rejected"]
    if rejects:
        lines.append(f"- 驳回事件：{json.dumps(rejects[-1], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 逐 Stage 执行（Stage 隔离）")
    lines.append("")
    for row in stage_log:
        lines.append(f"- run#{row['n']} stage=`{row['stage_id']}` "
                     f"授予={row['granted_tools']} 产物=`{row['telemetry']}`")
    lines.append("")
    return "\n".join(lines)


def run(*, evidence: Path, max_wait: float) -> dict:
    run_root = evidence / "drill_runs"
    run_root.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, 0o700)
    fixture = evidence / "roadmap_fixture.json"
    _write_json(fixture, ROADMAP_DOC)
    os.chmod(fixture, 0o600)
    stages = validate_roadmap(ROADMAP_DOC)

    records = DrillRecords()
    stage_log: list[dict[str, Any]] = []
    gw = gateway_from_env(
        db_path=evidence / "drill_pending.sqlite",
        evidence_dir=evidence / "feishu",
        start_ws=True,
        on_card_action=records.on_card_action,
    )
    db_path = evidence / "drill_checkpoints.db"
    if db_path.exists():
        db_path.unlink()
    deps, gate = _build_deps(gw, records, evidence, run_root, stage_log,
                             max_wait=max_wait)
    compiled = build_graph(deps).compile(
        checkpointer=SqliteSaver(sqlite3.connect(str(db_path))))
    state = {
        "thread_id": THREAD_ID,
        "track": "track1",
        "question": "演练：两 Stage 路线图（合成）",
        "proposals": [], "chosen": {}, "gate1_rejects": 0, "agent_runs": [],
        "gate2_rejects": 0, "coding_round": 0, "events": [],
        "stages": [stage_to_mapping(s) for s in stages],
        "stage_index": 0,
        "stage_status": stage_statuses(stages),
        "stage_frame": {},
    }
    config = {"configurable": {"thread_id": THREAD_ID}, "recursion_limit": 40}
    last: dict = {}
    stream = compiled.stream(state, config=config, stream_mode="values")
    try:
        for snapshot in stream:
            last = snapshot
            events = snapshot.get("events") or []
            if events and events[-1].get("type") == "stage_frame":
                rejects = [e for e in events if e.get("type") == "gate2_rejected"]
                if rejects:
                    # 驳回已落检查点、stage_frame 刚跑完：立即停（不发第四张卡）。
                    break
    finally:
        stream.close()
    ckpt = _checkpoint_state(db_path, THREAD_ID)
    if not ckpt:
        ckpt = last
    report = _render_report(records, gate.snapshots, ckpt, stage_log, stages)
    report_path = evidence / "drill_report_phase27.md"
    atomic_write_text(report_path, report)
    os.chmod(report_path, 0o600)
    _write_json(evidence / "drill_records.json", {
        "thread_id": THREAD_ID,
        "cards": gate.snapshots,
        "callbacks": records.callbacks,
        "stage_log": stage_log,
        "stage_index": ckpt.get("stage_index"),
        "stage_status": ckpt.get("stage_status"),
        "events": [e.get("type") for e in (ckpt.get("events") or [])],
    })
    gw.close()
    return {"report": str(report_path),
            "cards": len(gate.snapshots),
            "stage_index": ckpt.get("stage_index"),
            "stage_status": ckpt.get("stage_status")}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2.7 Task5 真人飞书演练")
    ap.add_argument("--evidence-dir", default=None)
    ap.add_argument("--max-wait", type=float, default=3600.0,
                    help="单张卡最长等待秒数（超时抛错，绝不代点）")
    args = ap.parse_args(argv)
    evidence = _evidence_dir(args.evidence_dir)
    out = run(evidence=evidence, max_wait=args.max_wait)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
