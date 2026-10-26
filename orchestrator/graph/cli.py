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
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver

from memory_flywheel.phase1.learning_store import LearningStore
from memory_flywheel.recall import ProductionRecall
from memory_flywheel.vector_engine import VectorEngine
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.common.validators import validate_batch_id, validate_thread_id
from orchestrator.direction.inputs import get_input
from orchestrator.direction.setter import DirectionSetter
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context
from orchestrator.gateway.router import ModelGateway
from orchestrator.graph.learning import classify_failure_stage, make_learning_seam
from orchestrator.graph.nodes import (
    GraphDeps,
    GraphStateError,
    build_graph,
    close_graph_failure,
    initial_state,
)
from orchestrator.graph.state import graph_db_path, phase1_root
from orchestrator.graph.topology import export_svg, export_text

RECURSION_LIMIT = 25  # 契约 L487
TASK6 = 6             # Task6 ascent V1→V2 升阶（ascent 模式；run 级 coding cap 3）
TASK7 = 7             # Task7 Track1 真实容器实跑（billing task + 三 stage 批次）
TASK8 = 8             # Task8 Track2 Genesis DR 对照 batch（三臂×3 种子）
TASK7_VARIANT_LIBERO = "libero"  # Task7 B 线：LIBERO 第五平台（--variant 仅此值）
TASK8_VARIANT_FLUID = "fluid"    # Task8 C 线：Q5 厨房倒水流体（--variant 仅 task=8）

# H-2/C1：Gate2 卡片 30s 截窗（飞书侧视频大小/可看性约束）；纯函数
# compute_gate2_trim 决定窗口，request_gate2_approval 的 trim 形参由
# feishu_gateway（HITL lane）实现，本模块只按 C1 传参。
GATE2_TRIM_SECONDS = 30.0

# L-2：节点外协议异常（含 task7/8 evaluate 接缝主动抛的 GraphStateError、
# telemetry/状态缺键的 KeyError、JSON 解码错误）一律归一 human_failure 结案。
# GatewayPausedError（含其子类 CodingLimitError）刻意不在元组内：预算熔断
# 必须原样上抛交回配额处置，严禁被包装成 agent 失败而落 rung=0 学习。
_CLOSURE_EXC = (GraphStateError, KeyError, json.JSONDecodeError)


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


# ---------------------------------------------------------------------------
# H-2/C1：Gate2 30s 截窗（纯函数；只允许本地 ffprobe 探测，不发网）
# ---------------------------------------------------------------------------

def _is_pos_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def _ffprobe_duration_s(video_path: Any) -> "float | None":
    """本地 ffprobe 取片长（秒）；无 ffprobe/非本地文件/探测失败一律 None。"""
    if not video_path or not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        return float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _task7_contact_and_duration(
        rec: dict, state: dict) -> "tuple[float | None, float | None]":
    """从 telemetry/manifest 取 (首次有效接触时刻 s, telemetry 侧片长 s)。

    接触时刻 = contact_active 首个 true 帧下标 / fps；fps 优先级：
    manifest.frame_dt_s → manifest.n_frames/duration_ms → telemetry.dt。
    """
    uri = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if not uri:
        return None, None
    tele_path = Path(uri)
    if not tele_path.is_file():
        return None, None
    try:
        data = json.loads(tele_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    contact_idx = None
    contact_active = data.get("contact_active")
    if isinstance(contact_active, list):
        contact_idx = next(
            (i for i, v in enumerate(contact_active) if v is True), None)

    manifest: dict[str, Any] = {}
    mp = tele_path.parent / "manifest.json"
    try:
        loaded = json.loads(mp.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded
    except (OSError, json.JSONDecodeError):
        manifest = {}

    fps: "float | None" = None
    frame_dt = manifest.get("frame_dt_s")
    n_frames = manifest.get("n_frames")
    manifest_dur_ms = manifest.get("duration_ms")
    if _is_pos_num(frame_dt):
        fps = 1.0 / float(frame_dt)
    elif isinstance(n_frames, int) and not isinstance(n_frames, bool) \
            and n_frames > 0 and _is_pos_num(manifest_dur_ms):
        fps = n_frames * 1000.0 / float(manifest_dur_ms)
    elif _is_pos_num(data.get("dt")):
        fps = 1.0 / float(data["dt"])

    tel_duration = None
    if isinstance(n_frames, int) and not isinstance(n_frames, bool) \
            and n_frames > 0 and fps:
        tel_duration = n_frames / fps
    contact_moment_s = contact_idx / fps \
        if contact_idx is not None and fps else None
    return contact_moment_s, tel_duration


def compute_gate2_trim(task: int, state: dict,
                       rec: "dict | None") -> "dict | None":
    """算 Gate2 视频 30s 截窗（纯函数，H-2）。

    返回精确 4 键：trim_start/trim_seconds/contact_moment_s/source_duration_s；
    不截窗（源片 ≤30s 或时长不可判定）返回 None。
    - Task7：以首次有效接触为中心取窗，边界夹紧 [0, duration−30]；接触缺失
      从 0 起；时长优先 ffprobe，否则 rec.duration_ms，再否则 manifest 推算。
    - Task8：batch 双分屏无单一接触时刻，contact_moment_s=None、从头截。
    """
    rec = rec or {}
    contact_moment_s: "float | None" = None
    source_duration_s: "float | None" = None
    trim_start = 0.0

    if task == TASK7:
        contact_moment_s, tel_duration = _task7_contact_and_duration(rec, state)
        source_duration_s = _ffprobe_duration_s(rec.get("video_path"))
        if source_duration_s is None and _is_pos_num(rec.get("duration_ms")):
            source_duration_s = float(rec["duration_ms"]) / 1000.0
        if source_duration_s is None and tel_duration is not None:
            source_duration_s = tel_duration
        # 时长完全不可判定时不截窗（无法保证窗口落在片内）。
        if source_duration_s is None:
            return None
        if contact_moment_s is not None:
            upper = max(0.0, source_duration_s - GATE2_TRIM_SECONDS)
            trim_start = min(
                max(contact_moment_s - GATE2_TRIM_SECONDS / 2.0, 0.0), upper)
    elif task in (TASK6, TASK8):
        # Task6/8 都是 batch 分屏（V2 两张 + V1 冻结两张），无单一接触时刻，
        # contact_moment_s=None、从头截；时长优先 ffprobe，其次 rec。
        source_duration_s = _ffprobe_duration_s(rec.get("video_path"))
        if source_duration_s is None and _is_pos_num(rec.get("duration_ms")):
            source_duration_s = float(rec["duration_ms"]) / 1000.0
        trim_start = 0.0
    else:
        return None

    if source_duration_s is not None and source_duration_s <= GATE2_TRIM_SECONDS:
        return None
    return {
        "trim_start": float(trim_start),
        "trim_seconds": float(GATE2_TRIM_SECONDS),
        "contact_moment_s": (float(contact_moment_s)
                             if contact_moment_s is not None else None),
        "source_duration_s": (float(source_duration_s)
                              if source_duration_s is not None else None),
    }


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


def _libero_stage0_path(args: argparse.Namespace) -> Path:
    """--stage0-report 解析（缺省 = Stage0 探针产物落点）。"""
    from orchestrator.graph.task7_libero import default_stage0_report_path
    return Path(args.stage0_report) if args.stage0_report \
        else default_stage0_report_path()


def _fluid_stage0_path(args: argparse.Namespace) -> Path:
    """Task8 fluid 的 --stage0-report 解析（缺省 = Stage0 流体探针产物）。"""
    from orchestrator.graph.task8_fluid import default_stage0_report_path
    return Path(args.stage0_report) if args.stage0_report \
        else default_stage0_report_path()


def _initial_state(args: argparse.Namespace, payload: dict) -> dict:
    """从已解析冻结 payload 构造图初始状态（纯函数；QA-1 C1）。

    --index 入口下 args.question 为 None：入图题面必须取自 payload
    （track2=question / track1=name），否则 classify 对已登记 ascent 题
    拿到空题面静默判 standard（生产路径上 Task2 主体失效）。

    libero/fluid variant：注入 stage0_report_path 声明通道（langgraph
    0.2.28 对 TypedDict 未声明键静默丢弃，state.py 里已声明——Gate1
    提案机判唯一输入）。
    """
    question = payload.get("question") if args.track == "track2" \
        else payload.get("name")
    state = initial_state(thread_id=args.thread, track=args.track,
                          question=question or "")
    variant = getattr(args, "variant", None)
    if variant == TASK7_VARIANT_LIBERO:
        from orchestrator.graph.task7_libero import STAGE0_REPORT_STATE_KEY
        state[STAGE0_REPORT_STATE_KEY] = str(_libero_stage0_path(args))
    elif variant == TASK8_VARIANT_FLUID:
        from orchestrator.graph.task8_fluid import STAGE0_REPORT_STATE_KEY
        state[STAGE0_REPORT_STATE_KEY] = str(_fluid_stage0_path(args))
    return state


def _production_deps(args: argparse.Namespace) -> tuple[GraphDeps, dict, Any]:
    libero = getattr(args, "variant", None) == TASK7_VARIANT_LIBERO
    fluid = getattr(args, "variant", None) == TASK8_VARIANT_FLUID
    if libero:
        # D-4 fail-closed 前移：Stage0 报告缺件/坏件在花任何 macro 之前就拒
        # （未过 Stage0 禁止提 Gate1 运行案，更不允许方向模型白烧一轮）。
        from orchestrator.graph import task7_libero as t7l
        s0 = _libero_stage0_path(args)
        if not s0.is_file():
            raise SystemExit(
                f"Task7 LIBERO Stage0 兼容报告不存在：{s0}"
                "（先跑 sandbox/probes/probe_libero_compat.py；D-4）")
        t7l.load_stage0_report(s0)  # 坏 JSON/schema/伪造 → GraphStateError
    if fluid:
        # 同 D-4 fail-closed 前移：Stage0 流体能力报告缺件/坏件在花任何
        # macro 之前就拒（both_fail 情形下甚至不会构造任何运行案——D-5）。
        from orchestrator.graph import task8_fluid as t8f
        s0 = _fluid_stage0_path(args)
        if not s0.is_file():
            raise SystemExit(
                f"Task8 流体 Stage0 能力报告不存在：{s0}"
                "（先跑 sandbox/probes/probe_genesis_fluid.py；D-4/D-5）")
        t8f.load_stage0_report(s0)  # 坏 JSON/schema/伪造 → GraphStateError

    payload = _payload(args.track, args.index, args.question)
    title = args.task_title or (
        f"Track1 工具链巡礼：{payload['name']}" if args.track == "track1"
        else f"Track2 实证：{payload['question'][:30]}")

    gateway = ModelGateway(ledger=QuotaLedger())
    # Phase 2 Task1：双路召回中枢——一个 VectorEngine 句柄 + 一个
    # LearningStore 同时供 setter/runner/学习回写接缝复用（不新建第二个
    # embedding 句柄）；recall=None 等价回滚到 Phase 1 行为。
    vector_engine = VectorEngine()
    learning_store = LearningStore()
    recall = ProductionRecall(learning_store, vector_engine)
    setter = DirectionSetter(gateway, vector_engine, recall=recall)
    thread = args.thread
    batch_id = args.batch_id

    def direction_seam(state: dict) -> dict:
        ev = _last_event(state, "gate1_rejected")
        notes = ev["human_notes"] if ev else None
        prev = state.get("proposals") or None
        # Phase 2 Task2：classify 节点把 run_mode/v1_archive/ascent_targets
        # 写入 state；standard 时 archive=None/targets={}（传 None 保 Phase 1
        # prompt 逐字节不变），ascent 时 setter 插批判段并校验 ascent_claims。
        v1_archive = state.get("v1_archive") or None
        ascent_targets = state.get("ascent_targets") or None
        return setter.run_direction(
            track=args.track, payload=payload, thread_id=thread,
            batch_id=batch_id, gate_round=int(state.get("gate1_rejects", 0)),
            human_notes=notes, prev_proposals=prev,
            v1_archive=v1_archive, ascent_targets=ascent_targets)

    # 飞书长连接网关：只在真正需要时构造（topology 不需要）
    # M2：Task6 卡片批准强制真人原文（空批准只 toast 不落决策），防
    # DECIDED 已落而 opinion 接缝空 notes fail-loud 楔死真人 Gate；其余
    # 任务网关行为逐字节不变（flag 默认 False）。
    from orchestrator.feishu_gateway.client import gateway_from_env
    feishu = gateway_from_env(require_approval_notes=(args.task == TASK6))

    def gate1_seam(state: dict) -> dict:
        if libero:
            # Task7 B 线：Stage0 兼容事实随卡（D-4 降级时 seam 内直接
            # GraphStateError 零卡片；No Fake Approval 仍阻塞等真人）。
            from orchestrator.graph import task7_libero as t7l
            return t7l.make_gate1_seam(feishu, thread, title)(state)
        if fluid:
            # Task8 C 线：Stage0 流体能力事实随卡（both_fail 降级时 seam
            # 内直接 GraphStateError 零卡片；No Fake Approval 仍阻塞等真人）。
            from orchestrator.graph import task8_fluid as t8f
            return t8f.make_gate1_seam(feishu, thread, title)(state)
        if args.task == TASK6:
            # Task6 ascent：升阶假设卡（V1 冻结指标摘要）；批准后接缝落
            # gate1_opinion.v1 与部分 evolution_sources（No Fake Approval：
            # 仍阻塞等真人，任何分支无硬编码 approve）。
            from orchestrator.graph import task6 as task6_mod
            return task6_mod.make_gate1_seam(
                feishu, thread, title)(state)
        return feishu.request_gate1_approval(
            thread, title, state["proposals"])

    def gate2_seam(state: dict) -> dict:
        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        if args.task == TASK6:
            # Task6 ascent：V1/V2 四媒体严格顺序 Diff + 恰好一张 Δ 卡；
            # 批准后接缝落 gate2_opinion.v1 并补齐 evolution_sources 五键。
            from orchestrator.graph import task6 as task6_mod
            return task6_mod.make_gate2_seam(
                feishu, thread, title,
                trim_for=lambda st, r: compute_gate2_trim(TASK6, st, r)
            )(state)
        # Task7 真人 Gate2 打回（2026-10-15）：卡片须自解释任务/问题/视频。
        # 仅 Task7 真实 rollout 有场景叙事；其余任务退回纯指标卡。
        explanation = None
        if libero:
            from orchestrator.graph import task7_libero as t7l
            explanation = t7l.build_gate2_explanation(state, rec)
        elif fluid:
            # Task8 流体：85% 入杯口径 + 守恒机判叙事（数字从 wj_fluid_report
            # 现读；缺报表返回 None 退回纯指标，不编故事——同 task7 家法）。
            from orchestrator.graph import task8_fluid as t8f
            explanation = t8f.build_gate2_explanation(state, rec)
        elif args.task == TASK7:
            from orchestrator.graph.task7 import build_gate2_explanation
            explanation = build_gate2_explanation(state, rec)
        elif args.task == TASK8:
            from orchestrator.graph.task8 import build_gate2_explanation
            explanation = build_gate2_explanation(state, rec)
        # H-2/C1：30s 截窗参数由本侧纯函数算定；trim 形参由 feishu_gateway
        # （HITL lane）按 C1 接收（≤30s 短片/时长不可判时为 None=不截）。
        trim = compute_gate2_trim(args.task, state, rec)
        return feishu.request_gate2_approval(
            thread, title, state["metrics"],
            video_path=rec.get("video_path"),
            intranet_url=rec.get("intranet_url"),
            duration_ms=rec.get("duration_ms"),
            cover_png=rec.get("cover_png"),
            explanation=explanation,
            trim=trim)

    # Task6：学习回写走同库独立新表 phase1_learnings（mpnet 768 复用，0 付费）
    learning = make_learning_seam(learning_store)

    # Task6 ascent / Task7 Track1 / Task8 Track2：注入真实 agent_run
    # （容器/子进程实跑场景脚本，coding 批次）与真实 evaluate（Rung1–3 文件
    # 判定 + Rung4 视频实呼，visual 批次）。三 stage 凭据相互独立：direction
    # 走外层 billing_context（macro 批次），coding/visual 在各自接缝内开自己
    # 的 billing_context（task=6/7/8）；其余任务保持 Task3 语义（接缝为 None，
    # 真实走到即 TaskSeamError / file_backed）。
    agent_run_seam_impl = None
    evaluate_seam_impl = None
    if args.task in (TASK6, TASK7, TASK8):
        # libero variant：evaluate 只读 wj_report 产物（零视觉实呼），
        # --visual-batch-id 可省；其余任务两批次齐缺即拒（措辞与 Phase1
        # 契约逐字节同——milestone1 seam 测试对该消息做正则对账）。
        if libero:
            if not args.coding_batch_id:
                raise SystemExit(
                    "Task7 libero 必须提供 --coding-batch-id；--batch-id 为 "
                    "direction 的 macro_ideation 批次（libero 的 evaluate "
                    "零视觉实呼，--visual-batch-id 可省）")
        elif fluid:
            if not args.coding_batch_id:
                raise SystemExit(
                    "Task8 fluid 必须提供 --coding-batch-id；--batch-id 为 "
                    "direction 的 macro_ideation 批次（fluid 的 evaluate "
                    "零视觉实呼，--visual-batch-id 可省）")
        elif not args.coding_batch_id or not args.visual_batch_id:
            raise SystemExit(
                f"Task{args.task} 必须同时提供 --coding-batch-id 与 "
                "--visual-batch-id；--batch-id 为 direction 的 macro_ideation 批次")
        from orchestrator.agent_runner.runner import (
            AgentRunner, agent_run_seam as _agent_run_seam,
        )
        if args.task == TASK8 and not fluid:
            # Task8 spec 跑 batch 子命令（三臂×3 种子聚合产物）；evaluate 走
            # gait_rungs 三级文件判定 + 两张分屏的步态 Rung4。
            from orchestrator.graph.task8 import (
                make_evaluate_seam, make_spec_factory,
            )
        elif args.task == TASK6:
            # Task6 ascent：V2 入口 genesis_humanoid_dr_v2.py 出生时不在
            # workdir（引擎仓永不携带），由 runner coding_debug（run 级 cap 3）
            # 在付费 workdir 新文件补丁出生；evaluate 委托 task8 加严 profile
            # 且 visual_active_only（record_only 下零视觉实呼）。
            from orchestrator.graph.task6 import (
                make_evaluate_seam, make_spec_factory,
            )
        elif not libero and not fluid:
            from orchestrator.graph.task7 import (
                make_evaluate_seam, make_spec_factory,
            )
        runner = AgentRunner(gateway, vector_engine, recall=recall)
        if libero:
            # Task7 B 线（LIBERO 第五平台）：出生/回收双态工厂 + WJ-Lite
            # evaluate（推导 wj_report 路径，缺件时由 results.json 引擎现算
            # 组装）；evaluate 接缝签名只有 thread_id（无视觉网关）。
            from orchestrator.graph import task7_libero as t7l
            spec_factory = t7l.make_spec_factory(
                thread, args.coding_batch_id, task_title=title)
            agent_run_seam_impl = _agent_run_seam(runner, spec_factory)
            evaluate_seam_impl = t7l.make_evaluate_seam(thread)
        elif fluid:
            # Task8 C 线（Q5 厨房倒水流体）：出生/回收双态工厂 + 几何机判
            # evaluate（从 results.json+trajectory.npz 引擎现算组装
            # wj_fluid_report）；evaluate 接缝签名只有 thread_id（零视觉网关、
            # 零 Genesis 环境——只读产物）。
            from orchestrator.graph import task8_fluid as t8f
            spec_factory = t8f.make_spec_factory(
                thread, args.coding_batch_id, task_title=title)
            agent_run_seam_impl = _agent_run_seam(runner, spec_factory)
            evaluate_seam_impl = t8f.make_evaluate_seam(thread)
        else:
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
    deps, payload_obj, feishu = _production_deps(args)
    # 锁内适配（计划 L487；Task7 首次容器内真跑实证）：langgraph 0.2.28 的
    # invoke/stream 均不接受 recursion_limit 关键字（TypeError），只能放进
    # config（Pregel 在 config 内读取），两分支都不得再以 kwarg 传入。
    config = {"configurable": {"thread_id": args.thread},
              "recursion_limit": RECURSION_LIMIT}
    closure_exc: "BaseException | None" = None
    closure_state: "dict | None" = None
    closure_next: str = ""
    try:
        # direction（macro_ideation）在此外层账套内；Task7 的 coding/visual
        # 接缝内部各自开 task=7 的账套（ContextVar 进入重置、退出恢复外层）。
        with billing_context(task=args.task, batch_id=args.batch_id,
                             thread_id=args.thread):
            with _compiled(deps) as graph:
                try:
                    if resume:
                        final = graph.invoke(None, config=config)
                    else:
                        final = graph.invoke(
                            _initial_state(args, payload_obj),
                            config=config)
                except _CLOSURE_EXC as exc:
                    # L-2：不重发任何卡片——从最后一个超步检查点取状态，归一为
                    # human_failure 结案（图级 FAILURE.json + rung=0 学习）。
                    # GatewayPausedError/CodingLimitError 不在 _CLOSURE_EXC，
                    # 从此处原样穿透（feishu.close 仍在 finally 执行）。
                    snapshot = graph.get_state(config)
                    values = snapshot.values if snapshot is not None \
                        and isinstance(snapshot.values, dict) else {}
                    closure_state = values
                    closure_exc = exc
                    if snapshot is not None and snapshot.next:
                        closure_next = ",".join(map(str, snapshot.next))
    finally:
        feishu.close()

    if closure_exc is not None:
        base = _initial_state(args, payload_obj)
        state = {**base, **(closure_state or {})}
        stage = classify_failure_stage(state)
        node_hint = f"（异常时待执行节点：{closure_next}）" if closure_next else ""
        reason = (f"[{type(closure_exc).__name__}] 节点外协议异常{node_hint}，"
                  f"按 {stage} 失败结案：{closure_exc}")
        update = close_graph_failure(
            state, deps, stage=stage, reason=reason,
            error_type=type(closure_exc).__name__)
        final = {**state,
                 "events": list(state.get("events", [])) + update["events"]}

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
    # S-6：拓扑导出原子写 0600 收口 fsutil（svg 由 graphviz 直写，不在此列）。
    atomic_write_text(txt_path, txt)
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
                       help="计费任务号（Task2 方向=2，Task6 ascent=6，"
                            "Task7 Track1=7，Task8 Track2=8）")
        p.add_argument("--coding-batch-id", dest="coding_batch_id", default=None,
                       help="coding_debug 批次凭据（task=6/7/8 必填）")
        p.add_argument("--visual-batch-id", dest="visual_batch_id", default=None,
                       help="visual_rollout 批次凭据（task=6/7/8 必填；"
                            "task=7 --variant libero 与 task=8 --variant fluid "
                            "可省——evaluate 零视觉实呼）")
        p.add_argument("--variant", default=None,
                       choices=(TASK7_VARIANT_LIBERO, TASK8_VARIANT_FLUID),
                       help="变体：libero = Task7 B 线第五平台（仅 --task 7）；"
                            "fluid = Task8 C 线 Q5 厨房倒水流体（仅 --task 8）；"
                            "缺省 = Phase1 Track1 路径")
        p.add_argument("--stage0-report", dest="stage0_report", default=None,
                       help="libero/fluid Stage0 兼容/能力报告路径（缺省 "
                            "runs/phase2/task7/stage0/stage0_compat_report.json "
                            "或 runs/phase2/task8/stage0/stage0_fluid_report.json）")
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
    # C4：线程/批次 ID 在 CLI 边界先验（只允许 1-64 位 [A-Za-z0-9_-]），
    # 非法值直接退出 2，不进账套、不构网关、不建 run 目录。
    if args.cmd in ("run", "resume"):
        # libero/fluid 组合守卫（fail-closed，exit 2 不进账套/不构网关）：
        # --variant 各自只在对应 task 合法；--stage0-report 只在 variant 下
        # 有意义，静默忽略会把用户以为生效的路径悄悄丢掉——直接拒。
        if args.variant == TASK7_VARIANT_LIBERO and args.task != TASK7:
            print(f"--variant {args.variant} 仅支持 --task 7", file=sys.stderr)
            return 2
        if args.variant == TASK8_VARIANT_FLUID and args.task != TASK8:
            print(f"--variant {args.variant} 仅支持 --task 8", file=sys.stderr)
            return 2
        if args.stage0_report and not args.variant:
            print("--stage0-report 仅在 --variant libero|fluid 下生效",
                  file=sys.stderr)
            return 2
        try:
            validate_thread_id(args.thread)
            validate_batch_id(args.batch_id, what="batch_id(macro_ideation)")
            if args.coding_batch_id:
                validate_batch_id(args.coding_batch_id,
                                  what="coding_batch_id")
            if args.visual_batch_id:
                validate_batch_id(args.visual_batch_id,
                                  what="visual_batch_id")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
