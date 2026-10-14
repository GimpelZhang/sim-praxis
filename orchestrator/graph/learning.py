"""Task6 学习回写接缝：把图状态结构化为 phase1_learnings 条目（计划 L527-538）。

职责边界：
- 本模块只做「图状态 → 结构化摘要字段」的映射与证据路径换算，然后调用
  ``LearningStore.add_learning``；不碰 kb_chunks、不发网络、不做模型调用。
- 回写触发点在图节点：Gate2 验收通过走 persist_learning（rung 取本次评测
  实际到达的最高层级）；human_failure 结案走 rung=0（失败也是学习）。
- 无证据不写：accept 证据=最近一次成功 agent_run 的 telemetry 文件（evaluate
  已读过它），failure 证据=刚落盘的 FAILURE.json；两者都必须真实位于
  $SIMPRAXIS_PHASE1_ROOT 之下。
- kill -9 resume 幂等由 LearningStore 的确定性主键保证（同结案重放先删后写）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from memory_flywheel.phase1.learning_store import (
    GATE_ACCEPTED,
    GATE_FAILURE_AGENT,
    GATE_FAILURE_GATE1,
    GATE_FAILURE_GATE2,
    LearningStore,
)

from .nodes import GraphStateError

Seam = Callable[[dict], dict]

_FAILURE_GATE_BY_STAGE = {
    "agent_run": GATE_FAILURE_AGENT,
    "gate1": GATE_FAILURE_GATE1,
    "gate2": GATE_FAILURE_GATE2,
}


def _human_notes(state: dict) -> str:
    """汇总 Gate1/Gate2 打回时的真人意见（过程级信号，去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for ev in state.get("events", []):
        if ev.get("type") in ("gate1_rejected", "gate2_rejected",
                              "gate1_hard_failure", "gate2_hard_failure"):
            note = (ev.get("human_notes") or ev.get("reason") or "").strip()
            if note and note not in seen:
                seen.add(note)
                out.append(f"{ev['type']}：{note}")
    return "\n".join(out)


def _fix_actions(state: dict) -> str:
    """汇总各轮 agent_run 真正应用过的补丁（来自 timeline.applied 路径）。"""
    lines: list[str] = []
    for i, run in enumerate(state.get("agent_runs", []), start=1):
        timeline = run.get("timeline") or []
        for step in timeline:
            applied = step.get("applied") if isinstance(step, dict) else None
            if applied:
                cr = step.get("coding_round", run.get("coding_round", "?"))
                paths = ", ".join(str(p) for p in applied)
                lines.append(f"第 {i} 次 run / coding_round {cr}：{paths}")
    return "\n".join(lines)


def _rel_evidence(store: LearningStore, abs_path: str) -> str:
    root = store.evidence_root.resolve()
    target = Path(abs_path).resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise GraphStateError(
            f"学习证据 {target} 不在 $SIMPRAXIS_PHASE1_ROOT（{root}）之下；"
            f"无证据不写") from exc
    if not target.is_file():
        raise GraphStateError(f"学习证据文件不存在：{target}（无证据不写）")
    return rel


def _rung_from_metrics(metrics: dict) -> int:
    """从评测结果取实际判定到的最高 rung（不臆造：rungs 全缺即 GraphStateError）。"""
    rungs = metrics.get("rungs") if isinstance(metrics, dict) else None
    if not isinstance(rungs, dict):
        raise GraphStateError("persist：metrics 缺 rungs 结构，无法确定 rung")
    rule = 0
    for i in (1, 2, 3):
        if rungs.get(f"rung{i}") is not None:
            rule = i
    r4 = metrics.get("rung4")
    if isinstance(r4, dict) and r4.get("status") == "ok":
        rule = 4
    if rule == 0:
        raise GraphStateError("persist：metrics.rungs 无任何已判定层级")
    return rule


def _accept_entry(state: dict, store: LearningStore) -> dict[str, Any]:
    runs = state.get("agent_runs") or []
    ok_runs = [r for r in runs if r.get("status", "ok") != "failed"
               and r.get("telemetry_uri")]
    if not ok_runs:
        raise GraphStateError("persist：无成功 agent_run 的 telemetry 证据")
    metrics = state.get("metrics")
    if not isinstance(metrics, dict):
        raise GraphStateError("persist：state 缺 metrics（Gate2 验收依据）")
    telemetry_uri = ok_runs[-1]["telemetry_uri"]
    notes = _human_notes(state)
    return dict(
        run_id=state["thread_id"],
        track=state["track"],
        rung=_rung_from_metrics(metrics),
        sim_engine=(state.get("chosen") or {}).get("sim_engine", ""),
        gate_source=GATE_ACCEPTED,
        evidence_relpath=_rel_evidence(store, telemetry_uri),
        failure_symptom=notes,      # 打回意见即「曾经错在哪」；一次通过则为空
        fix_action=_fix_actions(state),
        human_opinion=notes,
        effective=True,
    )


def _failure_entry(state: dict, ctx: dict, store: LearningStore) -> dict[str, Any]:
    stage = ctx["stage"]
    gate_source = _FAILURE_GATE_BY_STAGE.get(stage)
    if gate_source is None:
        raise GraphStateError(f"persist：未知失败阶段 {stage!r}")
    notes = _human_notes(state)
    return dict(
        run_id=state["thread_id"],
        track=state.get("track", "track1"),
        rung=0,
        sim_engine=(state.get("chosen") or {}).get("sim_engine", ""),
        gate_source=gate_source,
        evidence_relpath=_rel_evidence(store, ctx["evidence"]),
        failure_symptom=ctx["reason"],
        fix_action=_fix_actions(state),
        human_opinion=notes,
        effective=False,
    )


def make_learning_seam(store: "LearningStore | None" = None) -> Seam:
    """生产装配：``GraphDeps(persist=make_learning_seam(LearningStore()))``。

    节点经 state["_learning"] 告知结案语境（该键只在调用时临时拼入，不进
    LangGraph 检查点）；缺省按事件流推断（直接单测时用）。
    """
    store = store or LearningStore()

    def seam(state: dict) -> dict:
        ctx = state.get("_learning") or {}
        kind = ctx.get("kind")
        if kind is None:
            kind = "accept" if any(
                ev.get("type") == "gate2_accepted"
                for ev in state.get("events", [])) else "failure"
        if kind == "accept":
            kwargs = _accept_entry(state, store)
        elif kind == "failure":
            if "stage" not in ctx or "reason" not in ctx or "evidence" not in ctx:
                raise GraphStateError("failure 学习回写缺 stage/reason/evidence")
            kwargs = _failure_entry(state, ctx, store)
        else:
            raise GraphStateError(f"未知 _learning.kind：{kind!r}")
        return store.add_learning(**kwargs)

    return seam
