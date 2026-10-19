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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph

from orchestrator.common.fsutil import atomic_write_text
from orchestrator.judge.ascent_thresholds import AscentThresholdError
from orchestrator.judge.seam import assemble_file_metrics

from .ascent import (
    V1IndexCorrupt,
    V1IndexMissing,
    classify_run,
    load_v1_index,
)
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
    # Phase 2 Task2：V1 档案索引装载接缝。None → load_v1_index()（生产读
    # $SIMPRAXIS_PHASE2_ROOT/v1_index.json）；测试注入确定性 dict 或 callable。
    # 注：只装载已校验的索引 dict；分类纯逻辑在 ascent.classify_run。
    v1_index: Optional[Any] = None

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

def _resolve_v1_index(deps: GraphDeps) -> "dict | None":
    """取已校验索引：注入 dict 直用、注入 callable 调用；None 读默认路径。"""
    if deps.v1_index is None:
        return load_v1_index()
    if callable(deps.v1_index):
        return deps.v1_index()
    return deps.v1_index


def node_classify(state: SimPraxisState, deps: GraphDeps) -> dict:
    """Phase 2 Task2：零付费确定性升阶分类（不发事件，保 Phase1 事件序列）。

    索引文件缺失（V1IndexMissing）= 空索引（新克隆/CI 无 V1 档案，所有
    运行 standard，标准题目不受影响）；索引损坏/schema 不符
    （V1IndexCorrupt），或命中路径上题面 sha 对质失败、dr_envelope 与
    登记基线漂移（AscentThresholdError）= 协议外状态，一律归一为
    GraphStateError fail-closed（QA-1 M1：try 必须包到 classify_run）。
    KeyError/TypeError 等真 bug 不在此吞，保持 fail-loud。
    """
    try:
        index = _resolve_v1_index(deps)
    except V1IndexMissing:
        index = None
    except V1IndexCorrupt as exc:
        raise GraphStateError(f"V1 档案索引损坏，禁止静默降级：{exc}") from exc
    try:
        result = classify_run(
            {"track": state.get("track"), "question": state.get("question")},
            index)
    except V1IndexCorrupt as exc:
        # 题面逐字命中但 sha256 对质失败：索引自相矛盾（档案损坏）。
        raise GraphStateError(
            f"V1 档案题面 sha 对质失败，禁止静默降级：{exc}") from exc
    except AscentThresholdError as exc:
        # 命中条目 dr_envelope 与登记基线漂移：加严目标不可信（STOP 重取）。
        raise GraphStateError(
            f"V1 档案 DR 包络与登记基线漂移，禁止静默降级：{exc}") from exc
    return {"run_mode": result["run_mode"],
            "v1_archive": result["v1_archive"],
            "ascent_targets": result["ascent_targets"]}


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
        update = {"chosen": chosen,
                  "events": [_ev("gate1_approved", choice=int(idx))]}
        # Task6 ascent：Gate1 接缝批准时带回 evolution_sources 部分块
        # （轮次/V1 summary/Gate1 意见件路径）；dict 以外类型一律不透传。
        sources = decision.get("evolution_sources")
        if isinstance(sources, dict):
            update["evolution_sources"] = sources
        return update
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
        update = {"events": [_ev("gate2_accepted")]}
        # Task6 ascent：Gate2 接缝批准时把五键全块写回 state（learning
        # 演化志消费）；其余任务/非 dict 载荷不透传，行为逐字节不变。
        sources = decision.get("evolution_sources")
        if isinstance(sources, dict):
            update["evolution_sources"] = sources
        return update
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


def _read_json_if_present(path: Path) -> "dict | None":
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _patch_workdir_failure_ref(workdir_failure: str, graph_failure: Path) -> None:
    """L-4：在 runner workdir 级 FAILURE.json 上反向登记 graph_failure_json。

    只增字段、保留首写 ts_iso 与既有字段；文件缺失/损坏不抛（图级结案不能
    被反向引用失败拖住），runner 侧自身的 _fail 已做同样的首 ts 保留。
    """
    wpath = Path(workdir_failure)
    data = _read_json_if_present(wpath)
    if data is None:
        return
    if data.get("graph_failure_json") == str(graph_failure):
        return
    data["graph_failure_json"] = str(graph_failure)
    # LOW-2/S-6：手工 tmp/replace/chmod 收口到 fsutil 唯一实现。
    atomic_write_text(wpath, json.dumps(data, ensure_ascii=False, indent=2))


def write_graph_failure(state: dict, deps: GraphDeps, *, stage: str,
                        reason: str, error_type: "str | None" = None) -> Path:
    """写图级 run_dir/FAILURE.json（节点结案与 CLI 异常归一结案共用）。

    L-4 语义：kill-9 重放/二次结案时保留首次写入的 ts_iso（结案时间不可被
    重放刷新）；若最近一次 agent_run_failed 带 workdir FAILURE.json，则写入
    agent_failure_json 交叉引用，并反向回写 workdir 文件的 graph_failure_json。
    """
    d = _run_dir(state, deps)
    out = d / FAILURE_JSON
    prev = _read_json_if_present(out)
    prev_ts = prev.get("ts_iso") if prev else None
    payload: dict[str, Any] = {
        "status": "human_failure",
        "thread_id": state.get("thread_id"),
        "track": state.get("track"),
        "stage": stage,
        "reason": reason,
        "gate1_rejects": state.get("gate1_rejects", 0),
        "gate2_rejects": state.get("gate2_rejects", 0),
        "ts_iso": prev_ts or _ev("x")["ts"],
    }
    if error_type:
        # L-2：节点外异常归一结案时标注原始异常类型，与常规 reject 结案区分。
        payload["error_type"] = error_type
    failed_ev = _last_event(state, "agent_run_failed")
    workdir_failure = None
    if isinstance(failed_ev, dict):
        workdir_failure = failed_ev.get("failure_json")
    if workdir_failure:
        payload["agent_failure_json"] = workdir_failure
    # LOW-2/S-6：fsutil 原子 0600 写。
    atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2))
    if workdir_failure:
        _patch_workdir_failure_ref(workdir_failure, out)
    return out


def close_graph_failure(state: dict, deps: GraphDeps, *, stage: str,
                        reason: str, error_type: "str | None" = None) -> dict:
    """L-2/L-4 共用失败结案：图 FAILURE.json + rung=0 学习 + human_failure 事件。

    节点常规结案（node_human_failure）与 CLI 捕获节点外协议异常后的归一结案
    都走这里，保证两处字段/目录/学习语义一致，不各写一份。
    """
    out = write_graph_failure(state, deps, stage=stage, reason=reason,
                              error_type=error_type)
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
    if error_type:
        ev_fields["error_type"] = error_type
    if learning is not None:
        ev_fields["learning"] = learning
    return {"events": [_ev("human_failure", **ev_fields)]}


def node_human_failure(state: SimPraxisState, deps: GraphDeps) -> dict:
    stage, reason = failure_classification(state)
    return close_graph_failure(state, deps, stage=stage, reason=reason)


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
    # LOW-2/S-6：fsutil 原子 0600 写。
    atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2))
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

    # Phase 2 Task2：条件入口先过零付费 classify，再单 edge 进 direction。
    graph.add_node("classify", lambda s: node_classify(s, deps))
    graph.add_node("direction", lambda s: node_direction(s, deps))
    graph.add_node("gate1", lambda s: node_gate1(s, deps))
    graph.add_node("agent_run", lambda s: node_agent_run(s, deps))
    graph.add_node("evaluate", lambda s: node_evaluate(s, deps))
    graph.add_node("gate2", lambda s: node_gate2(s, deps))
    graph.add_node("persist_learning", lambda s: node_persist_learning(s, deps))
    graph.add_node("human_failure", lambda s: node_human_failure(s, deps))
    graph.add_node("done", lambda s: node_done(s, deps))

    # 契约 L486：track1/track2 同入分类（差异在 direction 节点内读数据）。
    # Phase 2 Task2：route_track 校验后先进零付费 classify，再无条件进 direction。
    graph.set_conditional_entry_point(
        route_track, {"track1": "classify", "track2": "classify"})
    graph.add_edge("classify", "direction")

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
