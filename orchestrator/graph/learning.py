"""Task6 学习回写接缝：把图状态结构化为 phase1_learnings 条目（计划 L527-538）。

职责边界：
- 本模块只做「图状态 → 结构化摘要字段」的映射与证据路径换算，然后调用
  ``LearningStore.add_learning``；不碰 kb_chunks、不发网络、不做模型调用。
- 回写触发点在图节点：Gate2 验收通过走 persist_learning（rung 取本次评测
  实际到达的最高层级）；human_failure 结案走 rung=0（失败也是学习）。
- 无证据不写：accept 证据=最近一次成功 agent_run 的 telemetry 文件（evaluate
  已读过它），failure 证据=刚落盘的 FAILURE.json。证据根按计划 §1.8 双路：
  $SIMPRAXIS_PHASE1_ROOT（宿主侧证据）或 $SIMPRAXIS_RUN_DIR（Task7/8 真实
  rollout 产物），由 LearningStore.relativize_evidence 统一换算与越界校验。
- kill -9 resume 幂等由 LearningStore 的确定性主键保证（同结案重放先删后写）。

Task5 追加（ascent 升阶结案，standard 路径逐字节不变）：
- accept 且 state.run_mode=="ascent" 时，从 state.evolution_sources 读四份
  来源件（V1/V2 summary + Gate1/Gate2 真人意见件，契约见
  orchestrator.phase2.chronicle）渲染五段式演化志，落 phase2 证据根
  evolutions/，并向独立的 phase2_learnings 表写一行（EvolutionStore）；
- 顺序=先渲染落 md → phase1 行 → phase2 行，来源缺失在写库前 fail-loud；
  failure 结案不进入该分支，phase2 表永不写失败行（失败只进 phase1）。
"""
from __future__ import annotations

from typing import Any, Callable

from memory_flywheel.phase1.learning_store import (
    GATE_ACCEPTED,
    GATE_FAILURE_AGENT,
    GATE_FAILURE_GATE1,
    GATE_FAILURE_GATE2,
    LearningStore,
)
from memory_flywheel.phase2.evolution_store import EvolutionStore
from orchestrator.phase2 import chronicle

from .nodes import GraphStateError

Seam = Callable[[dict], dict]

# ascent accept 时，state["evolution_sources"] 必填的四来源件路径 + 轮次
# （Task6 出生节点负责写入该 state 通道并落 simpraxis.gate_opinion.v1 意见件；
# Task5 只锁定接缝契约：缺块/缺件即 fail-loud，不代笔、不渲染空段）。
_EVOLUTION_SOURCE_KEYS = (
    "v1_summary_path", "v2_summary_path",
    "gate1_opinion_path", "gate2_opinion_path", "evolution_round",
)

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


def _last_event(state: dict, event_type: str) -> dict | None:
    """取事件流中最后一个指定类型事件（失败归类看最近一次信号）。"""
    for ev in reversed(state.get("events", []) or []):
        if isinstance(ev, dict) and ev.get("type") == event_type:
            return ev
    return None


def _rel_evidence(store: LearningStore, abs_path: str) -> str:
    """证据路径换算委托 store：phase1/runs/phase2 三根（Task7 §1.8 双根 +
    锁内适配第三根 @phase2/，2026-10-26 run27 首次 Gate2 accept），无证据不写。"""
    try:
        return store.relativize_evidence(abs_path)
    except (ValueError, FileNotFoundError) as exc:
        raise GraphStateError(f"学习证据换算被拒：{exc}") from exc


def _rung_from_metrics(metrics: dict) -> "tuple[int, str | None]":
    """从评测结果取实际判定到的最高 rung（不臆造：rungs 全缺即 GraphStateError）。

    L-5：rung=4 仅当 Rung4 ``mode=="active"`` 且 status ok——record_only /
    uncalibrated 的视觉意见不具自动升级效力（D8：Rung4 永不单独放行），
    此时封顶 3，并返回 mode 供接缝在回写摘要上做注记。
    """
    rungs = metrics.get("rungs") if isinstance(metrics, dict) else None
    if not isinstance(rungs, dict):
        if (isinstance(metrics, dict) and metrics.get("rungs") is None
                and metrics.get("rungs_not_judged") is True):
            # 锁内适配（攢批85，2026-10-27 task7 首个 Gate2 accept persist
            # 首崩取证）：WJ-Lite 多任务套件无逐帧遥测，Rung1–3 结构性
            # 不可判——evaluate 接缝在机器取证（validate_telemetry 失败）
            # 后显式写 rungs_not_judged=True+原因，非静默缺失。Gate2
            # 真人验收行按核验级 rung=1 落表（Task0a 五条
            # gate2_accepted_supplementary 同约定：核验级如实记 rung=1，
            # 不得记 4）；Rung2+ 未判定不臆造。无标记的缺 rungs 仍硬失败。
            return 1, None
        raise GraphStateError("persist：metrics 缺 rungs 结构，无法确定 rung")
    rule = 0
    for i in (1, 2, 3):
        if rungs.get(f"rung{i}") is not None:
            rule = i
    r4 = metrics.get("rung4")
    r4_mode: "str | None" = None
    if isinstance(r4, dict):
        mode = r4.get("mode")
        if mode == "active" and r4.get("status") == "ok":
            rule = 4
        elif mode in ("record_only", "uncalibrated"):
            # 视觉已跑/未标定但无自动升级效力：规则层最高 3，附 mode 注记。
            r4_mode = str(mode)
    if rule == 0:
        raise GraphStateError("persist：metrics.rungs 无任何已判定层级")
    return rule, r4_mode


def classify_failure_stage(state: dict) -> str:
    """L-2：节点外异常（GraphStateError/KeyError/JSONDecodeError，含 evaluate
    接缝抛出者）归一到学习通道既有的 gate_source。

    学习表 gate_source 白名单只有 agent_run/gate1/gate2（learning_store 钉死，
    不在本 lane 修改）：evaluate/state 协议异常按 agent_run 失败归类，精确
    异常阶段由 reason 文案携带（CLI 侧拼接），不新增白名单值。
    """
    if _last_event(state, "agent_run_failed"):
        return "agent_run"
    if _last_event(state, "gate1_hard_failure"):
        return "gate1"
    if _last_event(state, "gate2_hard_failure"):
        return "gate2"
    return "agent_run"


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
    rung, r4_mode = _rung_from_metrics(metrics)
    kwargs = dict(
        run_id=state["thread_id"],
        track=state["track"],
        rung=rung,
        sim_engine=(state.get("chosen") or {}).get("sim_engine", ""),
        gate_source=GATE_ACCEPTED,
        evidence_relpath=_rel_evidence(store, telemetry_uri),
        failure_symptom=notes,      # 打回意见即「曾经错在哪」；一次通过则为空
        fix_action=_fix_actions(state),
        human_opinion=notes,
        effective=True,
    )
    return kwargs, r4_mode


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


def _ascent_evolution(state: dict, accept_kwargs: dict[str, Any],
                      evolution_store: EvolutionStore) -> dict[str, Any]:
    """Task5：ascent accept 追加通路——五段式演化志 + phase2_learnings 行。

    顺序刻意为「先渲染落 md，再由外层写 phase1，最后写 phase2」：来源件
    缺/坏时在任何写库前 fail-loud（ChronicleError 归一 GraphStateError）；
    failure 结案根本不进本函数，phase2 表永不写失败行。md 落 phase2 证据根
    evolutions/（0700/0600 原子写），其 relpath 即 phase2 行 evidence_relpath。
    返回 add_evolution 所需参数；md 与 relpath 同时返回供外层做返回注记。
    """
    archive = state.get("v1_archive")
    if not isinstance(archive, dict) or not archive.get("thread_id"):
        raise GraphStateError(
            "ascent 演化回写缺 state.v1_archive.thread_id（V1 父运行锚点）")
    parent_run_id = str(archive["thread_id"]).strip()
    sources_block = state.get("evolution_sources")
    if not isinstance(sources_block, dict):
        raise GraphStateError(
            "ascent 结案缺 state.evolution_sources（四来源件+轮次）；"
            "Task6 出生节点负责填充，缺块不代笔、不渲染空演化志")
    missing = [k for k in _EVOLUTION_SOURCE_KEYS if not sources_block.get(k)]
    if missing:
        raise GraphStateError(
            f"ascent evolution_sources 缺必填键：{missing}")
    evolution_round = sources_block["evolution_round"]
    if isinstance(evolution_round, bool) or not isinstance(evolution_round, int) \
            or evolution_round < 1:
        raise GraphStateError(
            "evolution_sources.evolution_round 必须是 >=1 的整数，收到 "
            f"{evolution_round!r}")
    try:
        sources = chronicle.load_evolution_sources(
            v1_summary_path=sources_block["v1_summary_path"],
            v2_summary_path=sources_block["v2_summary_path"],
            gate1_opinion_path=sources_block["gate1_opinion_path"],
            gate2_opinion_path=sources_block["gate2_opinion_path"])
        md = chronicle.render_evolution_changelog(
            v1_summary=sources["v1_summary"],
            v2_summary=sources["v2_summary"],
            gate1_opinion=sources["gate1_opinion"],
            gate2_opinion=sources["gate2_opinion"],
            parent_run_id=parent_run_id,
            evolution_round=evolution_round,
            # 用 Gate2 裁决时间做生成时间：kill-9 resume 同参重放必须渲染出
            # 逐字节相同的 md（phase2 确定性主键含 text），墙钟时间会破坏幂等。
            generated_at=sources["gate2_opinion"]["ts_iso"])
        # nit-3（评审）：文件名校验并入渲染同一 try——非法 slug 抛的
        # ChronicleError(ValueError) 必须与渲染失败同口径归一 GraphStateError，
        # 不得从接缝裸逃逸。
        filename = chronicle.evolution_filename(
            parent_run_id=parent_run_id,
            v2_run_id=sources["v2_summary"]["run_id"].strip(),
            evolution_round=evolution_round)
    except chronicle.ChronicleError as exc:
        raise GraphStateError(f"演化志渲染被拒：{exc}") from exc

    v2_run_id = sources["v2_summary"]["run_id"].strip()
    if v2_run_id != state["thread_id"]:
        # 落盘前对质：避免线程/summary 错配时在证据根留下孤儿演化志。
        raise GraphStateError(
            f"v2_summary.run_id={v2_run_id!r} 与当前线程 "
            f"{state['thread_id']!r} 不一致（V2 结案必须同运行对质）")
    try:
        relpath = evolution_store.persist_chronicle(filename, md)
    except (OSError, ValueError) as exc:
        raise GraphStateError(f"演化志落盘被拒：{exc}") from exc

    ratio = sources_block.get("pareto_gain_ratio")
    return {
        "add_kwargs": dict(
            run_id=v2_run_id,
            track=state["track"],
            rung=accept_kwargs["rung"],
            sim_engine=accept_kwargs["sim_engine"],
            gate_source=GATE_ACCEPTED,
            evidence_relpath=relpath,
            evolution_round=evolution_round,
            parent_run_id=parent_run_id,
            text=md,
            pareto_gain_ratio=ratio,
        ),
        "relpath": relpath,
        "evolution_round": evolution_round,
        "parent_run_id": parent_run_id,
    }


def make_learning_seam(store: "LearningStore | None" = None,
                       evolution_store: "EvolutionStore | None" = None) -> Seam:
    """生产装配：``GraphDeps(persist=make_learning_seam(LearningStore()))``。

    节点经 state["_learning"] 告知结案语境（该键只在调用时临时拼入，不进
    LangGraph 检查点）；缺省按事件流推断（直接单测时用）。

    Task5：kind=="accept" 且 state.run_mode=="ascent" 时，在标准 phase1
    回写之外追加五段式演化志（落 phase2 证据根 evolutions/）与
    phase2_learnings 独立表一行；standard 路径逐字节不变，failure 路径
    永远不触达演化志/phase2 表。evolution_store 缺省在首次 ascent 结案时
    才构造（standard 单测/生产路径零副作用）。
    """
    store = store or LearningStore()

    def seam(state: dict) -> dict:
        ctx = state.get("_learning") or {}
        kind = ctx.get("kind")
        if kind is None:
            kind = "accept" if any(
                ev.get("type") == "gate2_accepted"
                for ev in state.get("events", [])) else "failure"
        r4_mode: "str | None" = None
        ascent_plan: "dict[str, Any] | None" = None
        evo_store = evolution_store   # 仅 ascent 分支惰性构造，standard 零副作用
        if kind == "accept":
            kwargs, r4_mode = _accept_entry(state, store)
            if state.get("run_mode") == "ascent":
                # 先渲染/落 md（任何来源缺失在此抛，phase1/phase2 均未写）。
                evo_store = evo_store or EvolutionStore()
                ascent_plan = _ascent_evolution(state, kwargs, evo_store)
        elif kind == "failure":
            if "stage" not in ctx or "reason" not in ctx or "evidence" not in ctx:
                raise GraphStateError("failure 学习回写缺 stage/reason/evidence")
            kwargs = _failure_entry(state, ctx, store)
        else:
            raise GraphStateError(f"未知 _learning.kind：{kind!r}")
        row = store.add_learning(**kwargs)
        if ascent_plan is not None:
            try:
                evo_row = evo_store.add_evolution(  # type: ignore[union-attr]
                    **ascent_plan["add_kwargs"])
            except ValueError as exc:
                raise GraphStateError(f"phase2_learnings 写入被拒：{exc}") from exc
            if isinstance(row, dict):
                row["evolution"] = {
                    "table": evo_row["table"],
                    "id": evo_row["id"],
                    "evidence_relpath": ascent_plan["relpath"],
                    "evolution_round": ascent_plan["evolution_round"],
                    "parent_run_id": ascent_plan["parent_run_id"],
                }
        # L-5：learning_store 12 列 schema 冻结，非 active 的 Rung4 封顶 3 已落库；
        # mode 信息只在返回摘要上做注记，供接缝/调用方可见，不写表。
        if r4_mode is not None and isinstance(row, dict):
            row["rung4_mode"] = r4_mode
            row["rung4_note"] = (
                f"Rung4 mode={r4_mode}：视觉意见无自动升级效力，rung 封顶 3")
        return row

    return seam
