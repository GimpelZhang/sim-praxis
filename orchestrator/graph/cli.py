#!/usr/bin/env python3
"""Task3 状态机 CLI：run / resume / topology。

  python -m orchestrator.graph.cli run --thread t1_demo_20261014 \
      --track track1 --index 1 --batch-id <已书面批准批次>
  python -m orchestrator.graph.cli run --thread t2_demo --track track2 \
      --question "..." --batch-id <...>
  python -m orchestrator.graph.cli resume --thread t1_demo_20261014 \
      --track track1 --index 1 --batch-id <...>
  python -m orchestrator.graph.cli topology [--out DIR]

run/resume 在 gate1/gate2 节点阻塞等真人飞书点按；进程可在等待期退出，
resume 以 invoke(None)+同 thread_id 重放：卡片不重发（pending_gates 重挂，
Task1 语义），SqliteSaver 从 $SIMPRAXIS_GRAPH_SQLITE 或
$SIMPRAXIS_PHASE1_ROOT/state/langgraph_checkpoints.db 恢复。

Task3 生产图走到 agent_run 会抛 TaskSeamError——Task4 接缝在 Task7 真实
运行时由装配代码注入；persist_learning 自 Task6 起接 LearningStore
（phase1_learnings 新表，0 付费本地嵌入）。
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from memory_flywheel.phase1.learning_store import LearningStore
from memory_flywheel.vector_engine import VectorEngine
from orchestrator.direction.inputs import get_input
from orchestrator.direction.setter import DirectionSetter
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context
from orchestrator.gateway.router import ModelGateway
from orchestrator.graph.learning import make_learning_seam
from orchestrator.graph.nodes import (
    GraphDeps,
    build_graph,
    initial_state,
)
from orchestrator.graph.state import graph_db_path, phase1_root
from orchestrator.graph.topology import export_svg, export_text

RECURSION_LIMIT = 25  # 契约 L487
TASK7 = 7             # Task7 Track1 真实容器实跑（billing task + 三 stage 批次）


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


@contextmanager
def _compiled(deps: GraphDeps, db_path: "Path | None" = None):
    db = Path(db_path or graph_db_path())
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(db)) as saver:
        yield build_graph(deps).compile(checkpointer=saver)


def _payload(track: str, index: "int | None", question: "str | None") -> dict:
    if index is not None:
        return get_input(track, index)
    if not question:
        raise SystemExit("必须提供 --index（冻结样本）或 --question")
    return {"name": question} if track == "track1" else {"question": question}


def _production_deps(args: argparse.Namespace) -> tuple[GraphDeps, dict, Any]:
    payload = _payload(args.track, args.index, args.question)
    title = args.task_title or (
        f"Track1 工具链巡礼：{payload['name']}" if args.track == "track1"
        else f"Track2 实证：{payload['question'][:30]}")

    gateway = ModelGateway(ledger=QuotaLedger())
    setter = DirectionSetter(gateway, VectorEngine())
    thread = args.thread
    batch_id = args.batch_id

    def direction_seam(state: dict) -> dict:
        ev = _last_event(state, "gate1_rejected")
        notes = ev["human_notes"] if ev else None
        prev = state.get("proposals") or None
        return setter.run_direction(
            track=args.track, payload=payload, thread_id=thread,
            batch_id=batch_id, gate_round=int(state.get("gate1_rejects", 0)),
            human_notes=notes, prev_proposals=prev)

    # 飞书长连接网关：只在真正需要时构造（topology 不需要）
    from orchestrator.feishu_gateway.client import gateway_from_env
    feishu = gateway_from_env()

    def gate1_seam(state: dict) -> dict:
        return feishu.request_gate1_approval(
            thread, title, state["proposals"])

    def gate2_seam(state: dict) -> dict:
        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        # Task7 真人 Gate2 打回（2026-10-15）：卡片须自解释任务/问题/视频。
        # 仅 Task7 真实 rollout 有场景叙事；其余任务退回纯指标卡。
        explanation = None
        if args.task == TASK7:
            from orchestrator.graph.task7 import build_gate2_explanation
            explanation = build_gate2_explanation(state, rec)
        return feishu.request_gate2_approval(
            thread, title, state["metrics"],
            video_path=rec.get("video_path"),
            intranet_url=rec.get("intranet_url"),
            duration_ms=rec.get("duration_ms"),
            cover_png=rec.get("cover_png"),
            explanation=explanation)

    # Task6：学习回写走同库独立新表 phase1_learnings（mpnet 768 复用，0 付费）
    learning = make_learning_seam(LearningStore())

    # Task7 Track1：注入真实 agent_run（容器/子进程实跑场景脚本，coding 批次）
    # 与真实 evaluate（Rung1–3 文件判定 + Rung4 视频实呼，visual 批次）。
    # 三 stage 凭据相互独立：direction 走外层 billing_context（macro 批次），
    # coding/visual 在各自接缝内开自己的 billing_context(task=7)；非 Task7
    # 保持 Task3 语义（接缝为 None，真实走到即 TaskSeamError / file_backed）。
    agent_run_seam_impl = None
    evaluate_seam_impl = None
    if args.task == TASK7:
        if not args.coding_batch_id or not args.visual_batch_id:
            raise SystemExit(
                "Task7 必须同时提供 --coding-batch-id 与 --visual-batch-id；"
                "--batch-id 为 direction 的 macro_ideation 批次")
        from orchestrator.agent_runner.runner import (
            AgentRunner, agent_run_seam as _agent_run_seam,
        )
        from orchestrator.graph.task7 import (
            make_evaluate_seam, make_spec_factory,
        )
        runner = AgentRunner(gateway, VectorEngine())
        spec_factory = make_spec_factory(
            thread, args.coding_batch_id, task_title=title)
        agent_run_seam_impl = _agent_run_seam(runner, spec_factory)
        evaluate_seam_impl = make_evaluate_seam(
            gateway, args.visual_batch_id, thread)

    deps = GraphDeps(
        direction=direction_seam,
        gate1=gate1_seam,
        agent_run=agent_run_seam_impl,  # Task7 注入；否则 TaskSeamError
        evaluate=evaluate_seam_impl,    # Task7 注入 Rung4；否则 file_backed
        gate2=gate2_seam,
        persist=learning,     # Task6：结案即写 phase1_learnings（无证据不写）
        task_title=title,
    )
    return deps, payload, feishu


def _invoke(args: argparse.Namespace, resume: bool) -> int:
    deps, _payload_obj, feishu = _production_deps(args)
    # 锁内适配（计划 L487；Task7 首次容器内真跑实证）：langgraph 0.2.28 的
    # invoke/stream 均不接受 recursion_limit 关键字（TypeError），只能放进
    # config（Pregel 在 config 内读取），两分支都不得再以 kwarg 传入。
    config = {"configurable": {"thread_id": args.thread},
              "recursion_limit": RECURSION_LIMIT}
    try:
        # direction（macro_ideation）在此外层账套内；Task7 的 coding/visual
        # 接缝内部各自开 task=7 的账套（ContextVar 进入重置、退出恢复外层）。
        with billing_context(task=args.task, batch_id=args.batch_id,
                             thread_id=args.thread):
            with _compiled(deps) as graph:
                if resume:
                    final = graph.invoke(None, config=config)
                else:
                    final = graph.invoke(
                        initial_state(thread_id=args.thread, track=args.track,
                                      question=args.question or ""),
                        config=config)
    finally:
        feishu.close()

    kinds = [ev.get("type") for ev in final.get("events", [])]
    print(json.dumps({"thread_id": args.thread, "events": kinds,
                      "chosen": final.get("chosen", {}).get("name"),
                      "gate1_rejects": final.get("gate1_rejects", 0),
                      "gate2_rejects": final.get("gate2_rejects", 0)},
                     ensure_ascii=False, indent=2))
    if "human_failure" in kinds:
        print("HUMAN_FAILURE：见 runs/<thread>/FAILURE.json", file=sys.stderr)
        return 3
    return 0


def _topology(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else phase1_root() / "task3_graph"
    out_dir.mkdir(parents=True, exist_ok=True)
    compiled = build_graph(None).compile()
    txt = export_text(compiled)
    txt_path = out_dir / "topology.txt"
    txt_path.write_text(txt, encoding="utf-8")
    svg_path = export_svg(compiled, out_dir / "topology.svg")
    result = {"topology_txt": str(txt_path), "topology_svg": str(svg_path)
              if svg_path else None,
              "note": "pygraphviz 不可用；按契约使用内置文本拓扑，未安装系统包"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase1 Task3 LangGraph 状态机")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_run(p: argparse.ArgumentParser) -> None:
        p.add_argument("--thread", required=True)
        p.add_argument("--track", required=True, choices=("track1", "track2"))
        p.add_argument("--index", type=int, default=None,
                       help="inputs/tracks 冻结样本序号（与 --question 二选一）")
        p.add_argument("--question", default=None)
        p.add_argument("--batch-id", required=True, dest="batch_id",
                       help="direction/macro_ideation 批次凭据")
        p.add_argument("--task", type=int, default=2,
                       help="计费任务号（Task2 方向=2，Task7 Track1=7）")
        p.add_argument("--coding-batch-id", dest="coding_batch_id", default=None,
                       help="Task7 coding_debug 批次凭据（task=7 必填）")
        p.add_argument("--visual-batch-id", dest="visual_batch_id", default=None,
                       help="Task7 visual_rollout 批次凭据（task=7 必填）")
        p.add_argument("--task-title", default=None)

    p_run = sub.add_parser("run", help="首次运行（gate 处阻塞等真人）")
    add_run(p_run)
    p_run.set_defaults(func=lambda a: _invoke(a, resume=False))

    p_resume = sub.add_parser("resume", help="进程退出后重挂（不重发卡片）")
    add_run(p_resume)
    p_resume.set_defaults(func=lambda a: _invoke(a, resume=True))

    p_topo = sub.add_parser("topology", help="导出结构图（文本/svg 可选）")
    p_topo.add_argument("--out", default=None)
    p_topo.set_defaults(func=_topology)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
