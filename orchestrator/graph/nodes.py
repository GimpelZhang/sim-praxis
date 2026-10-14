"""状态机节点与边（Task3 契约 L484-487）。

节点清单：direction / gate1 / agent_run / evaluate / gate2 /
persist_learning / human_failure / done。

设计要点：
- 节点是纯函数 state→state 更新；进程内组件（网关/飞书/setter/runner）经
  GraphDeps 注入，不进检查点。
- gate1/gate2 是「普通阻塞节点」：节点体内调用 deps.gate1/gate2（生产实现为
  FeishuGateway 的长连接等待）。kill -9 后 resume 时 LangGraph 重放该节点，
  生产实现内部复用 pending_gates 既有卡片（只重挂不重发，Task1 语义）。
- 打回计数语义按 Task3 契约 L485 与其强制单测④（两次打回 → human_failure）：
  首次发卡 → 打回(1) → 重跑 direction → 第二次发卡 → 打回(2) → human_failure。
  锁内适配（计划 Task2 L447 vs Task3 L485）：Task2 DirectionSetter.run_gate1_loop
  的 D10 计数是「3 张卡、第 3 次打回结案」（gate1_rejects=3）；Task3 图契约与
  单测④明文「2 次打回即结案」，图编排以 Task3 为准（2 张卡）。run_gate1_loop
  作为 Task2 CLI 路径原样保留、不改其语义与既测。
- No Fake Physics（L490）：evaluate 默认实现只从 telemetry_uri 指向的文件读数，
  节点代码里没有任何指标字面量；Task5 起 Rung1–3 判定委托
  orchestrator.judge.seam.assemble_file_metrics，Rung4（视觉）Task7 接入。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph

from orchestrator.judge.seam import assemble_file_metrics

from .state import SimPraxisState, run_dir_for

FAILURE_JSON = "FAILURE.json"
DONE_JSON = "DONE.json"

# 路由返回值（同时是条件边 path_map 的键）
TO_DIRECTION = "direction"
TO_AGENT_RUN = "agent_run"
TO_HUMAN_FAILURE = "human_failure"
TO_PERSIST = "persist_learning"

GATE_APPROVE = "approve"
GATE_REJECT = "reject_with_comments"
_VALID_VERDICTS = (GATE_APPROVE, GATE_REJECT)

# 与 D10/契约 L485 对齐：打回 2 次即结案转人工
GATE_REJECT_LIMIT = 2


class TaskSeamError(RuntimeError):
    """Task4/5/6 接缝在对应任务交付前被真实走到（禁止伪造成功）。"""


class GraphStateError(RuntimeError):
    """状态机收到协议外数据（如非法决策值、缺 telemetry 文件）。"""


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------

# 所有接缝统一签名：(state: dict) -> dict。
# direction  返回 {"proposals": [...], "question": str, "gate_round": int, ...}
# gate1/gate2 返回 {"decision": "approve|reject_with_comments",
#                   "choice": int|None, "human_notes": str}
# agent_run  返回 {"telemetry_uri": str, "coding_round": int, ...}（进 agent_runs）
# evaluate   返回 metrics dict
# persist    返回学习回写摘要 dict（Task6）
Seam = Callable[[dict], dict]


@dataclass
class GraphDeps:
    direction: Optional[Seam] = None
    gate1: Optional[Seam] = None
    agent_run: Optional[Seam] = None
    evaluate: Optional[Seam] = None      # None → file_backed_evaluate
    gate2: Optional[Seam] = None
    persist: Optional[Seam] = None       # Task6：LearningStore 接缝；None 时
    #   accept 路径硬失败、failure 路径不回写（仅 Task3 风格单测/拓扑允许）
    task_title: Optional[str] = None
    run_dir: Optional[Path] = None

    def require(self, name: str, state: dict) -> dict:
        fn = getattr(self, name)
        if fn is None:
            raise TaskSeamError(
                f"{name} 接缝未注入：该能力属于后续 Task（agent_run=Task4 / "
                f"evaluate 判定=Task5 / persist=Task6），Task3 不允许伪造结果")
        return fn(state)


def _ev(kind: str, **fields: Any) -> dict:
    ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": kind}
    ev.update(fields)
    return ev


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def node_direction(state: SimPraxisState, deps: GraphDeps) -> dict:
    result = deps.require("direction", state)
    proposals = result.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        raise GraphStateError("direction 接缝未返回非空 proposals")
    question = result.get("question") or state.get("question") or ""
    return {
        "question": question,
        "proposals": proposals,
        "events": [_ev("direction",
                       gate_round=result.get("gate_round",
                                            state.get("gate1_rejects", 0)),
                       n_proposals=len(proposals))],
    }


def node_gate1(state: SimPraxisState, deps: GraphDeps) -> dict:
    decision = deps.require("gate1", state)
    verdict = decision.get("decision")
    if verdict not in _VALID_VERDICTS:
        # 评审 M2：未知决策值不得被默默当成 reject 再烧一次 macro 调用
        raise GraphStateError(f"Gate1 决策值非法：{verdict!r}（决策字典：{decision}）")
    if verdict == GATE_APPROVE:
        idx = decision.get("choice")
        proposals = state.get("proposals", [])
        if idx is None:
            idx = 0
        try:
            chosen = proposals[int(idx)]
        except (IndexError, TypeError, ValueError) as exc:
            raise GraphStateError(
                f"Gate1 choice={idx!r} 越界（proposals={len(proposals)}）") from exc
        return {"chosen": chosen,
                "events": [_ev("gate1_approved", choice=int(idx))]}
    rejects = int(state.get("gate1_rejects", 0)) + 1
    notes = (decision.get("human_notes") or "").strip()
    if not notes:
        # Task2 M2 协议守卫：打回必须带意见；空意见直接结案，不退回去白烧 macro。
        return {"gate1_rejects": rejects,
                "events": [_ev("gate1_hard_failure", rejects=rejects,
                               reason="Gate1 打回但 human_notes 为空")]}
    return {"gate1_rejects": rejects,
            "events": [_ev("gate1_rejected", rejects=rejects,
                           human_notes=notes)]}


def node_agent_run(state: SimPraxisState, deps: GraphDeps) -> dict:
    record = deps.require("agent_run", state)
    runs = list(state.get("agent_runs", []))
    runs.append(record)
    # Task4：runner 5 轮用尽/补丁越界 → 失败态带轨迹进 human_failure，
    # 绝不进入 evaluate 伪造成功（计划 L502；§6.3 显式 FAILURE 原则）。
    if record.get("status") == "failed":
        return {
            "agent_runs": runs,
            "coding_round": record.get("coding_round",
                                       state.get("coding_round", 0)),
            "events": [_ev("agent_run_failed", run_index=len(runs),
                           coding_round=record.get("coding_round", 0),
                           reason=record.get("reason", "agent_run 失败"),
                           failure_json=record.get("failure_json"))],
        }
    uri = record.get("telemetry_uri")
    if not uri:
        raise GraphStateError("agent_run 接缝未返回 telemetry_uri")
    update = {
        "agent_runs": runs,
        "telemetry_uri": uri,
        "events": [_ev("agent_run", run_index=len(runs),
                       coding_round=record.get("coding_round",
                                               state.get("coding_round", 0)))],
    }
    if "coding_round" in record:
        update["coding_round"] = record["coding_round"]
    return update


def file_backed_evaluate(state: dict) -> dict:
    """Task5 接缝：metrics 只从 telemetry_uri 指向的真实文件读取并做 Rung1–3。

    文件由 agent_run（Task7 真实运行/测试夹具）写出；本函数不构造任何指标数值。
    夹具要求：数值由公式生成并在文件内自标 fixture，禁止「按期望结果倒填」
    （契约 L490）。判定装配委托 judge.seam；Rung4 视觉判定 Task7 接入。
    """
    uri = state.get("telemetry_uri")
    if not uri:
        raise GraphStateError("evaluate：state 无 telemetry_uri")
    path = Path(uri)
    if not path.is_file():
        raise GraphStateError(f"evaluate：telemetry 文件不存在：{uri}")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise GraphStateError("evaluate：telemetry 顶层必须是对象")
    return assemble_file_metrics(uri, data)


def node_evaluate(state: SimPraxisState, deps: GraphDeps) -> dict:
    evaluate = deps.evaluate or file_backed_evaluate
    metrics = evaluate(state)
    if not isinstance(metrics, dict):
        raise GraphStateError("evaluate 接缝必须返回 dict")
    return {"metrics": metrics,
            "events": [_ev("evaluate", fixture=metrics.get("fixture", False))]}


def node_gate2(state: SimPraxisState, deps: GraphDeps) -> dict:
    decision = deps.require("gate2", state)
    verdict = decision.get("decision")
    if verdict not in _VALID_VERDICTS:
        raise GraphStateError(f"Gate2 决策值非法：{verdict!r}（决策字典：{decision}）")
    if verdict == GATE_APPROVE:
        return {"events": [_ev("gate2_accepted")]}
    rejects = int(state.get("gate2_rejects", 0)) + 1
    notes = (decision.get("human_notes") or "").strip()
    if not notes:
        return {"gate2_rejects": rejects,
                "events": [_ev("gate2_hard_failure", rejects=rejects,
                               reason="Gate2 打回但 human_notes 为空")]}
    return {"gate2_rejects": rejects,
            "events": [_ev("gate2_rejected", rejects=rejects,
                           human_notes=notes)]}


def node_persist_learning(state: SimPraxisState, deps: GraphDeps) -> dict:
    # Task6：persist 接缝=LearningStore 回写（生产装配见 graph.learning）；
    # 未注入仍硬失败——验收通过却不回写是协议违规，不允许静默跳过。
    summary = deps.require("persist", {**state, "_learning": {"kind": "accept"}})
    return {"events": [_ev("persist_learning", summary=summary)]}


def _run_dir(state: dict, deps: GraphDeps) -> Path:
    if deps.run_dir is not None:
        deps.run_dir.mkdir(parents=True, exist_ok=True)
        return deps.run_dir
    return run_dir_for(state["thread_id"])


def failure_classification(state: dict) -> tuple[str, str]:
    """human_failure 结案分类（节点与 Task6 学习回写共用，禁止两处各写一份）。"""
    if _last_event(state, "agent_run_failed"):
        ev = _last_event(state, "agent_run_failed")
        return "agent_run", ev["reason"]
    if _last_event(state, "gate1_hard_failure"):
        return "gate1", _last_event(state, "gate1_hard_failure")["reason"]
    if _last_event(state, "gate2_hard_failure"):
        return "gate2", _last_event(state, "gate2_hard_failure")["reason"]
    stage = "gate2" if state.get("gate2_rejects", 0) >= GATE_REJECT_LIMIT \
        else "gate1"
    return stage, f"{stage} 连续 {GATE_REJECT_LIMIT} 次打回（D10），结案转人工"


def node_human_failure(state: SimPraxisState, deps: GraphDeps) -> dict:
    stage, reason = failure_classification(state)
    payload = {
        "status": "human_failure",
        "thread_id": state.get("thread_id"),
        "track": state.get("track"),
        "stage": stage,
        "reason": reason,
        "gate1_rejects": state.get("gate1_rejects", 0),
        "gate2_rejects": state.get("gate2_rejects", 0),
        "ts_iso": _ev("x")["ts"],
    }
    d = _run_dir(state, deps)
    out = d / FAILURE_JSON
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.chmod(out, 0o600)
    # Task6（计划 L534）：失败也是学习——FAILURE.json 落盘后经同一 persist
    # 接缝写 rung=0 一条。未注入 persist（Task3 风格单测/拓扑导出）则不回写，
    # 生产装配（graph.cli）从 Task6 起恒注入。
    learning: "dict | None" = None
    if deps.persist is not None:
        learning = deps.persist({**state, "_learning": {
            "kind": "failure", "stage": stage, "reason": reason,
            "evidence": str(out)}})
    ev_fields: dict[str, Any] = {"stage": stage, "reason": reason,
                                 "failure_json": str(out)}
    if learning is not None:
        ev_fields["learning"] = learning
    return {"events": [_ev("human_failure", **ev_fields)]}


def node_done(state: SimPraxisState, deps: GraphDeps) -> dict:
    payload = {
        "status": "done",
        "thread_id": state.get("thread_id"),
        "track": state.get("track"),
        "gate1_rejects": state.get("gate1_rejects", 0),
        "gate2_rejects": state.get("gate2_rejects", 0),
        "n_agent_runs": len(state.get("agent_runs", [])),
        "ts_iso": _ev("x")["ts"],
    }
    d = _run_dir(state, deps)
    out = d / DONE_JSON
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.chmod(out, 0o600)
    return {"events": [_ev("done", done_json=str(out))]}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def route_track(state: dict) -> str:
    track = state.get("track")
    if track not in ("track1", "track2"):
        raise GraphStateError(
            f"track 必须是 track1|track2，收到 {track!r}；双轨差异在 direction "
            f"节点内读数据处理，不做引擎名分支")
    return track


def route_after_agent_run(state: dict) -> str:
    if _last_event(state, "agent_run_failed"):
        return TO_HUMAN_FAILURE
    return "evaluate"


def route_after_gate1(state: dict) -> str:
    if _last_event(state, "gate1_approved"):
        return TO_AGENT_RUN
    if _last_event(state, "gate1_hard_failure"):
        return TO_HUMAN_FAILURE
    if int(state.get("gate1_rejects", 0)) >= GATE_REJECT_LIMIT:
        return TO_HUMAN_FAILURE
    return TO_DIRECTION


def route_after_gate2(state: dict) -> str:
    if _last_event(state, "gate2_accepted"):
        return TO_PERSIST
    if _last_event(state, "gate2_hard_failure"):
        return TO_HUMAN_FAILURE
    if int(state.get("gate2_rejects", 0)) >= GATE_REJECT_LIMIT:
        return TO_HUMAN_FAILURE
    return TO_AGENT_RUN


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------

def build_graph(deps: "GraphDeps | None" = None):
    """编译状态机。deps=None 仅供拓扑导出（节点不会被执行）。"""
    deps = deps or GraphDeps()
    graph = StateGraph(SimPraxisState)

    graph.add_node("direction", lambda s: node_direction(s, deps))
    graph.add_node("gate1", lambda s: node_gate1(s, deps))
    graph.add_node("agent_run", lambda s: node_agent_run(s, deps))
    graph.add_node("evaluate", lambda s: node_evaluate(s, deps))
    graph.add_node("gate2", lambda s: node_gate2(s, deps))
    graph.add_node("persist_learning", lambda s: node_persist_learning(s, deps))
    graph.add_node("human_failure", lambda s: node_human_failure(s, deps))
    graph.add_node("done", lambda s: node_done(s, deps))

    # 契约 L486：track1/track2 同入 direction（差异在节点内读数据）
    graph.set_conditional_entry_point(
        route_track, {"track1": "direction", "track2": "direction"})

    graph.add_edge("direction", "gate1")
    graph.add_conditional_edges("gate1", route_after_gate1, {
        TO_AGENT_RUN: "agent_run",
        TO_DIRECTION: "direction",
        TO_HUMAN_FAILURE: "human_failure",
    })
    # Task4：agent_run 失败（5 轮用尽/补丁越界）→ human_failure，否则进 evaluate
    graph.add_conditional_edges("agent_run", route_after_agent_run, {
        TO_HUMAN_FAILURE: "human_failure",
        "evaluate": "evaluate",
    })
    graph.add_edge("evaluate", "gate2")
    graph.add_conditional_edges("gate2", route_after_gate2, {
        TO_PERSIST: "persist_learning",
        TO_AGENT_RUN: "agent_run",
        TO_HUMAN_FAILURE: "human_failure",
    })
    graph.add_edge("persist_learning", "done")
    graph.add_edge("done", END)
    graph.add_edge("human_failure", END)
    return graph


def initial_state(*, thread_id: str, track: str, question: str) -> dict:
    """invoke 初始输入（events 给空 list 以触发 operator.add 归并）。"""
    return {
        "thread_id": thread_id,
        "track": track,
        "question": question,
        "proposals": [],
        "chosen": {},
        "gate1_rejects": 0,
        "agent_runs": [],
        "gate2_rejects": 0,
        "coding_round": 0,
        "events": [],
    }
