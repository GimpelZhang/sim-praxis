#!/usr/bin/env python3
"""Task3 单测⑤的子进程靶标：真实 SqliteSaver + 真实 pending_gates，
零付费（direction/feishu 全 fake，仅持久层与 LangGraph 重放为真）。

被 pytest 以 run/resume 两阶段启动；run 阶段在 gate1 阻塞点被 kill -9，
resume 阶段必须只重挂（GATE1_REATTACH）不重发卡片并一路走到 GRAPH_DONE。

stdout 行首标记（测试靠它们判定阶段，勿随意改名）：
  PHASE_RUN / PHASE_RESUME / DIRECTION_CALLED / GATE{1,2}_CARD_SENT /
  GATE{1,2}_REATTACH / AGENT_RAN / PERSISTED / GRAPH_DONE / ERROR
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from orchestrator.feishu_gateway.store import PendingStore
from orchestrator.graph import (
    GraphDeps,
    build_graph,
    file_backed_evaluate,
    initial_state,
)
from orchestrator.graph.state import graph_db_path

THREAD = "t-drill-kill9"
PROPOSALS = [{"name": "P0-drill"}, {"name": "P1-drill"}]


def log(line: str) -> None:
    print(line, flush=True)


class FakeDirection:
    def __call__(self, state: dict) -> dict:
        notes = ""
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate1_rejected":
                notes = ev.get("human_notes", "")
                break
        log(f"DIRECTION_CALLED r={state.get('gate1_rejects', 0)} "
            f"notes={1 if notes else 0}")
        return {"proposals": PROPOSALS, "question": "drill question",
                "gate_round": state.get("gate1_rejects", 0)}


class PollingGate:
    """模拟 FeishuGateway.request_gate*：真实 pending_gates 行 + 轮询等待。

    决策由父进程用 PendingStore.mark_decided 注入——与生产 WS 回调线程
    只写库/置 Event 的接口同构（这里用轮询替代跨进程 Event）。
    """

    def __init__(self, store: PendingStore, gate: int) -> None:
        self.store = store
        self.gate = gate

    def __call__(self, state: dict) -> dict:
        row = self.store.get_pending(THREAD, self.gate)
        if row is not None and row["status"] == "DECIDED":
            # 进程死亡期间真人已点按、回调落库：同样只重挂，严禁重发卡片
            log(f"GATE{self.gate}_REATTACH already-decided")
        elif row is not None and row["status"] == "PENDING" \
                and row["card_message_id"]:
            log(f"GATE{self.gate}_REATTACH card={row['card_message_id']}")
        else:
            # 真实发卡的等价物：写 PENDING 行 + 记录卡片 message_id
            self.store.upsert_pending(
                THREAD, self.gate,
                card_message_id=f"drill-card-g{self.gate}",
                open_chat_id=None)
            log(f"GATE{self.gate}_CARD_SENT card=drill-card-g{self.gate}")
        for _ in range(200):  # 40s 上限（测试节奏内足够）
            row = self.store.get_pending(THREAD, self.gate)
            if row is not None and row["status"] == "DECIDED":
                return {"decision": row["decision"], "choice": row["choice"],
                        "human_notes": row["human_notes"] or ""}
            time.sleep(0.2)
        raise TimeoutError(f"gate{self.gate} 等待决策超时")


class FakeAgent:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.n = 0

    def __call__(self, state: dict) -> dict:
        self.n += 1
        notes = ""
        for ev in reversed(state.get("events", [])):
            if ev.get("type") == "gate2_rejected":
                notes = ev.get("human_notes", "")
                break
        # 显式合成 telemetry 夹具：数值全部公式生成（非按期望倒填），
        # 供 file_backed_evaluate 读取——节点本身不产任何指标字面量。
        t = 8
        tele = {
            "_fixture": True,
            "_formula": "penetration_m=0.001*m; force_k=1.0+0.1*sin(k)",
            "engine": "fixture-engine",
            "run_id": f"drill-run-{self.n}",
            "dt": 0.016,
            "penetrations": [0.001 * m for m in range(t)],
            "contact_forces": [1.0 + 0.1 * ((k - 3) % 3) for k in range(t)],
            "saw_gate2_notes": bool(notes),
        }
        path = self.root / f"telemetry_drill_{self.n}.json"
        path.write_text(json.dumps(tele), encoding="utf-8")
        log(f"AGENT_RAN n={self.n} notes={1 if notes else 0}")
        return {"telemetry_uri": str(path), "coding_round": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=("run", "resume"))
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    log(f"PHASE_{args.phase.upper()}")
    store = PendingStore()  # $SIMPRAXIS_PHASE1_ROOT/state/pending_gates.db

    deps = GraphDeps(
        direction=FakeDirection(),
        gate1=PollingGate(store, 1),
        agent_run=FakeAgent(Path.cwd()),
        evaluate=file_backed_evaluate,
        gate2=PollingGate(store, 2),
        persist=lambda state: (log("PERSISTED"), {"ok": True})[1],
    )
    config = {"configurable": {"thread_id": THREAD}, "recursion_limit": 25}
    try:
      with SqliteSaver.from_conn_string(str(graph_db_path())) as saver:
        graph = build_graph(deps).compile(checkpointer=saver)
        if args.phase == "run":
            final = graph.invoke(
                initial_state(thread_id=THREAD, track="track1",
                              question="drill"),
                config=config)
        else:
            final = graph.invoke(None, config=config)
        kinds = [ev.get("type") for ev in final.get("events", [])]
        log("GRAPH_DONE events=" + ",".join(kinds))
    except Exception as exc:  # noqa: BLE001
        log("ERROR " + type(exc).__name__ + ": " + str(exc)[:300])
        traceback.print_exc()
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
