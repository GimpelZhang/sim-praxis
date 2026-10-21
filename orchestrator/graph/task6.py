#!/usr/bin/env python3
"""Phase 2 Task6：ascent V1→V2 升阶端到端工程接缝（计划 L410 接缝出生）。

本模块是**引擎层**接缝，自身零网络、零付费、零模型调用；实呼只可能发生在
图运行期注入的网关（macro 在外层账套，coding/visual 各自开 task=6 账套）。
四件事：

1. ``make_spec_factory``：Task6 V2 batch 的 RunSpec 工厂。与 task8 工厂的关键
   差异——run 级 coding 上限收窄为 3（``RUN_CODING_CAP``；runner 全局
   ``MAX_CODING_ROUNDS=5`` 一字不动，runner 本任务零改动，它只认
   ``RunSpec.max_coding_rounds``）；cmd 目标是 workdir 内**出生时不存在**的
   ``genesis_humanoid_dr_v2.py``：resolver 只 stage 冻结 V1 脚本作基线参照，
   V2 文件由 runner coding_debug 的新文件补丁在付费 workdir 内出生。
   **引擎仓永远不携带 genesis_humanoid_dr_v2.py**（三层分离：解题内容归
   内容层/付费 workdir，引擎只有合成 fixture；出生测试以 git ls-files 钉死）。
2. ``make_evaluate_seam``：委托 ``task8.make_evaluate_seam``（ascent 加严
   profile=tight_profile() + visual_active_only=True），在其结果上做 V1/V2
   Pareto 六指标对账、增强版 V1/V2 summary 落盘。视觉通道：Task4 终裁
   record_only（2026-10-19 用户签收；D7_NOT_ACHIEVED 两次，矛盾即结论，
   B3 不得自授 active）——mode != "active" 时 task8 接缝零抽帧/零拼图/
   零 CallReplay/零 call_visual，本模块不加第五个 CallReplay 构造点。
3. ``make_gate1_seam`` / ``make_gate2_seam``：Gate1 升阶假设卡（V1 冻结
   指标摘要）与 Gate2 四媒体严格顺序 Diff（V1 split1→V1 split2→V2 split1
   →V2 split2，随后恰好一张 Δ 卡）。No Fake Approval：两缝都只阻塞等真人，
   批准后从 pending_gates 已决行（DECIDED 行在下一轮 _reopen_round 归档前
   仍可读）复算决定时间，落 ``simpraxis.gate_opinion.v1`` 意见件；打回路径
   不写意见件、节点 reject 行为一字不动。
4. ``evolution_sources``：Gate1 批准写部分块（轮次 + V1 summary/意见件
   路径），Gate2 批准补齐五键全块，经节点加法透传进 state，供 learning
   的 Task5 五段式演化志消费。

锁内适配（计划 L415 只命名六指标，未规定跨臂/跨 run 的标量化与显存来源）：
- success_rate/slip_dist_mean_m/postpush_vmax_mean_mps/tilt_max_deg_max 四
  个组级指标按 normal/ice_nominal/ice_dr 三臂**算术均值**汇总到 batch 标量；
- solve_wallclock_s 取 9 行 CSV ``sim_wall_s`` 算术均值（缺列/空值/非有限
  fail-closed，绝不伪造）；
- vram_peak_gb 取显式 sidecar（V2=batch workdir/vram_peak.json，由同法
  gpu_sampler wrapper 设备级采样随批落盘，不信 V2 脚本自报；V1=本线程
  evolution_sources 目录下 scripts/phase2/provision_v1_vram.py 以同一 wrapper
  预置的 v1_vram_peak.json，冻结 V1 本体零仪器化），缺失即 GraphStateError，
  绝不编造、不拿别的量顶替。

锁内适配（计划 v1.2 外；seq42-47 能力矩阵 + 用户 2026-10-20 14:1xZ 裁决
「拆块交付（改引擎流程）」）：seq43/45/46/47 实证三模型在
Anthropic-Messages shim 上对「单次出生 600-900 行 V2」同形失败（唯一
thinking block 吃满 16384 output、0 text），而小任务可收敛。故 V2 出生轮
的 coding 调用改为 4 次顺序 JSON 拆块出生（ChunkSpec，n 固定来自规格、
禁止模型自报），runner 本地逐块校验、拼接后 ast/重名/总长三验，仅以单个
old="" 补丁经既有 _parse_and_apply 出生；任一坏块整轮失败、无半成品落盘。
合同/骨架文本常量在 task6_v2_prompts.py（纯描述性契约文本，硬规则 3：引擎
层不携带可运行解题脚本）。RUN_CODING_CAP=3 不变；非出生修补轮与其他 task
字节级旧行为。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from orchestrator.agent_runner.runner import ChunkSpec, RunSpec
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.common.runtime_paths import phase2_root, phase2_runs_root
from orchestrator.graph import task8
from orchestrator.graph.ascent import render_v1_metrics
from orchestrator.graph.nodes import GraphStateError
from orchestrator.graph.task6_v2_prompts import (
    V2_CHUNK_TITLES,
    V2_CONTRACT,
    V2_SKELETON,
    build_chunk_boundary,
)
from orchestrator.graph.task8 import (
    BATCH_TIMEOUT_S,
    CALIBRATION_NAME,
    COVER_NAME,
    ENGINE_SCENARIOS,
    MANIFEST_NAME,
    SEEDS,
    SPLITS,
    SUMMARY_NAME,
    VENV_PY,
)
from orchestrator.judge.ascent_thresholds import tight_profile
from orchestrator.judge.gait_rungs import evaluate_batch, load_batch
from orchestrator.judge.gait_thresholds import TELEMETRY_SCHEMA
from orchestrator.judge.pareto import METRIC_DIRECTIONS, pareto_verdict
from orchestrator.phase2.chronicle import GATE_OPINION_SCHEMA
from orchestrator.solutions import SolutionError, resolve_solution

TASK = 6

# 同法 VRAM 仪器（锁内适配：计划只规定 sidecar 契约，未规定仪器形态）：
# V2 batch 与 V1 冻结回放（scripts/phase2/provision_v1_vram.py）由同一
# wrapper 设备级 pynvml 轮询，verdict 只信设备实测、不信脚本自报；冻结 V1
# 本体零仪器化。runner cwd=workdir，wrapper 以绝对路径启动且自包含。
REPO_ROOT = Path(__file__).resolve().parents[2]
GPU_SAMPLER = REPO_ROOT / "orchestrator" / "common" / "gpu_sampler.py"

# 计划 L193：ascent run 的 coding_debug 上限 run 级收窄到 3（全局
# MAX_CODING_ROUNDS=5 不动；runner 零改动，只消费 RunSpec.max_coding_rounds）。
RUN_CODING_CAP = 3

# 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
# seq43/45/46/47 实证三模型在 Anthropic-Messages shim 上「单次出生 600-900
# 行 V2」同形失败——唯一 thinking block 吃满 16384 output、0 text；同款模型
# 小任务可收敛（macro seq29、seq42 8KB 易任务）。拆 4 个顺序小编排调用后
# 每块创造量小，16384 输出余量给思考通道；单调用含合同/骨架注入的长流式
# 响应，网关超时由默认 120s 放宽到 600s（与探针超时口径一致）。
# 2026-10-21 锁内适配（用户选项 A；图外接受性探针 seq56，提交 d5846b5，
# 证据 runs/task6_ascent/ 下 32768 接受性探针 JSON）：seq50/54/55 三轮
# 拆块生产均在 16384 输出顶截断（JSON 信封未闭合）；seq56 accepted=true、
# output_tokens=32768、ordered_prefix 1..11080、output_at_cap=true，证实
# 16384 非硬墙、shim 接受 32768——但探针在 32768 处仍被截断（线顶 ≥32768，
# 未探更高；口径同探针 docstring），够不够出生留给生产首验；n=4 与四块行
# 预算不变。常量 16384→32768。
CODING_MAX_TOKENS_V2 = 32768
CODING_TIMEOUT_S_V2 = 600

# V2 解题脚本文件名：只允许出生在付费 workdir（runner 新文件补丁）。
# 引擎仓（tests 合成 fixture 之外）任何位置携带该文件即三层分离违规，
# 由出生测试 test_v2_track_script_telemetry_contract 以 git ls-files 钉死。
V2_ENTRYPOINT = "genesis_humanoid_dr_v2.py"

# V2 batch 随批扩展件（与 summary.json/CSV 同一信任边界：缺件 fail-closed，
# 接缝不代笔）。innovation.json 是脚本随批机产结构化交付件；vram_peak.json
# 由 gpu_sampler wrapper 设备级采样写出（不信脚本自报，见 GPU_SAMPLER）。
INNOVATION_NAME = "innovation.json"
VRAM_NAME = "vram_peak.json"
VRAM_KEY = "vram_peak_gb"

# 跨 lane 常量：必须与 ascent._GROUPS_ARMS / chronicle._GROUPS_ARMS 逐字
# 等值（Task5 已用 chronicle/ascent 跨 lane 测试钉死；本处是第三个副本，
# 出生测试对三副本等值做断言）。
_GROUPS_ARMS = ("normal", "ice_nominal", "ice_dr")

# V1 两张冻结分屏的生产字节数（2026-10-16 落盘实证，与 v1_index.json 登记
# sha256 双锁）：路径只许 join(host_workdir, 登记 relpath)，字节数与 sha
# 任一不符即冻结档案被改动，fail-closed。夹具用 monkeypatch 改写本表。
LOCKED_V1_SPLIT_SIZES: dict[str, int] = {
    SPLITS[0][1]: 126530,   # split_normal_vs_ice_nominal_s0.mp4
    SPLITS[1][1]: 186119,   # split_ice_nominal_vs_ice_dr_s0.mp4
}

_OPINION_SOURCES = ("card_form", "p2p_text", "cli")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def task6_run_dir(thread_id: str, run_idx: int) -> Path:
    """V2 batch 工作目录：phase2 runs 根/task6_ascent/run_{thread}_{idx}。"""
    d = phase2_runs_root() / "task6_ascent" / f"run_{thread_id}_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sources_dir(thread_id: str) -> Path:
    """本线程四来源件目录（0700；phase2 账根下，测试被 conftest 重定向到 tmp）。"""
    return ensure_private_dir(
        phase2_root() / "evolution_sources" / thread_id)


def _require_archive(state: dict) -> dict:
    arc = state.get("v1_archive")
    if not isinstance(arc, dict) or not arc.get("thread_id") \
            or not arc.get("host_workdir"):
        raise GraphStateError(
            "Task6 是 ascent 专用接缝：state.v1_archive 缺失（本题未命中冻结 "
            "V1 档案）；standard 运行不得走 task=6")
    return arc


# ---------------------------------------------------------------------------
# spec 工厂
# ---------------------------------------------------------------------------

def make_spec_factory(thread_id: str, coding_batch: str,
                      task_title: str = "") -> Callable[[dict], RunSpec]:
    """agent_run 接缝的 V2 spec 工厂。

    resolver stage 冻结 V1 方案（genesis_humanoid_dr.py + manifest + README，
    逐件 sha256/C4 校验）作基线参照；cmd 指向出生时不存在的 V2 入口，首次
    子进程必然以「文件不存在」失败，runner coding_debug（≤3 轮）以新文件
    补丁在付费 workdir 内出生 V2 脚本——引擎仓自始至终不持有该文件。
    Gate2 打回后的再跑使用全新 run 目录（run_idx 递增），补丁不继承。
    """
    def factory(state: dict) -> RunSpec:
        chosen = state.get("chosen") or {}
        engine = chosen.get("sim_engine")
        scenario = ENGINE_SCENARIOS.get(engine)
        if scenario is None:
            raise GraphStateError(
                f"Task6 ascent 场景映射缺 sim_engine={engine!r}；"
                f"V1→V2 升阶只支持 genesis（{sorted(ENGINE_SCENARIOS)}）")
        _require_archive(state)
        run_idx = len(state.get("agent_runs", [])) + 1
        workdir = task6_run_dir(thread_id, run_idx)

        try:
            staged = resolve_solution(scenario["solution_id"]).stage(workdir)
        except SolutionError as exc:
            raise GraphStateError(
                f"Task6 基线方案 stage 失败（{scenario['solution_id']}）：{exc}"
            ) from exc
        # 冻结 V1 入口 staged.entrypoint 作 coding 参照；V2 目标文件此刻
        # 不存在也不得由工厂创建（无 V2 文件断言：resume 同 run 重入时，
        # 上一轮 coding 补丁出生的文件必须保留，工厂既不出生也不删除）。
        v2_target = workdir / V2_ENTRYPOINT
        if not staged.entrypoint.is_file():
            raise GraphStateError(
                f"Task6 V1 基线入口 stage 后缺失：{staged.entrypoint}")

        # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
        # 仅 V2 出生轮（target 不存在）挂 ChunkSpec；同 run 内出生后的修补轮
        # 与 Gate2 打回后的**新 run 目录**（target 仍 absent）由 runner/工厂
        # 各自按磁盘现状选路。chosen 必须是真实提案（state["chosen"] 裸取，
        # 缺失即 KeyError fail-closed，禁止 {} 静默装配）；骨架 sha 对 staged
        # V1 入口现算（前 16 位），每轮工厂现算、不写 checkpoint 新字段。
        chunked = None
        if not v2_target.exists():
            chosen_doc = state["chosen"]
            v1_sha16 = hashlib.sha256(
                staged.entrypoint.read_bytes()).hexdigest()[:16]
            skeleton_text = V2_SKELETON.replace("@@SHA@@", v1_sha16)
            chunked = ChunkSpec(
                target_path=V2_ENTRYPOINT,
                chunk_count=4,
                chunk_titles=V2_CHUNK_TITLES,
                per_call_sections=(
                    ("V2 升阶任务合同", V2_CONTRACT),
                    ("已批准 macro 提案",
                     json.dumps(chosen_doc, ensure_ascii=False, indent=2)),
                    ("V1 结构骨架", skeleton_text),
                ),
                chunk_boundaries=tuple(
                    build_chunk_boundary(i) for i in range(len(V2_CHUNK_TITLES))),
            )

        versions = workdir / "versions.txt"
        if not versions.exists():
            atomic_write_text(versions, task8._installed_versions())
        cal = workdir / CALIBRATION_NAME
        if not cal.exists():
            task8._stage_calibration(cal)

        notes = task8._last_event(state, "gate2_rejected")
        gate2_notes = notes["human_notes"] if notes else None
        env_extra = {
            "HOME": "/mnt/caches/home",
            "HF_HOME": "/mnt/caches/hf",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "SIMPRAXIS_USE_PROXY": "0",
        }
        env_extra.update(scenario["env"])

        # VRAM sidecar 由 wrapper 设备级采样落 workdir/vram_peak.json（两侧
        # 同包裹、同设备口径；V2 脚本不自报显存）。双重防孤儿：可捕获的
        # SIGTERM/SIGINT 由 wrapper killpg 转发整组；runner 超时对 wrapper 发
        # 不可捕获的 SIGKILL 时，孙进程 preexec 的 PR_SET_PDEATHSIG 由内核
        # 兜底 SIGKILL，不留孤儿 Genesis 进程继续占 GPU/污染后续批次采样；
        # wrapper 透传子进程 rc。
        return RunSpec(
            thread_id=thread_id,
            workdir=workdir,
            cmd=[VENV_PY, str(GPU_SAMPLER),
                 "--sidecar", str(workdir / VRAM_NAME), "--",
                 VENV_PY, str(v2_target), "batch",
                 "--out-dir", str(workdir),
                 "--seeds", str(SEEDS),
                 "--calibration-json", str(cal)],
            telemetry_relpath=SUMMARY_NAME,
            run_idx=run_idx,
            batch_id=coding_batch,
            billing_task=TASK,
            gate2_notes=gate2_notes,
            env_extra=env_extra,
            run_timeout_s=BATCH_TIMEOUT_S,
            task_title=task_title or "Task6 ascent Genesis 控制器/DR V1→V2 升阶",
            video_relpath=SPLITS[0][1],
            cover_relpath=COVER_NAME,
            manifest_relpath=MANIFEST_NAME,
            max_coding_rounds=RUN_CODING_CAP,
            # 锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
            # Task6 V2 coding 调用统一 32768 输出余量/600s 超时（出生拆块与出生后
            # 修补轮同口径）；chunked_birth 仅出生轮非 None，修补轮由 runner 走
            # 既有单补丁路径。（2026-10-21 升 32768，见上 seq56 探针）
            coding_max_tokens=CODING_MAX_TOKENS_V2,
            coding_timeout_s=CODING_TIMEOUT_S_V2,
            chunked_birth=chunked,
        )

    return factory


# ---------------------------------------------------------------------------
# 批产物读取与 Pareto 六指标装配（锁内适配，见模块 docstring）
# ---------------------------------------------------------------------------

def _csv_sim_wall_mean(rows: list[dict], seeds: int) -> float:
    """9 行 CSV 的 sim_wall_s 算术均值；空/非数/非正 fail-closed。"""
    expected = seeds * len(_GROUPS_ARMS)
    if len(rows) != expected:
        raise GraphStateError(
            f"Task6 CSV 行数 {len(rows)} != 三臂×{seeds} 籽={expected}")
    vals: list[float] = []
    for i, row in enumerate(rows):
        raw = str(row.get("sim_wall_s") or "").strip()
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise GraphStateError(
                f"Task6 CSV 第 {i} 行 sim_wall_s 非法/为空：{raw!r}"
                "（solve_wallclock_s 不允许伪造）") from None
        if not math.isfinite(v) or v <= 0.0:
            raise GraphStateError(
                f"Task6 CSV 第 {i} 行 sim_wall_s={v!r} 必须是正有限值")
        vals.append(v)
    return sum(vals) / len(vals)


def _vram_gb(path: Path) -> float:
    """读显式 vram sidecar（{"vram_peak_gb": 正有限数}）；缺失 fail-closed。"""
    if not path.is_file():
        raise GraphStateError(
            f"Task6 显存 sidecar 缺失：{path}（vram_peak_gb 只认真实 sidecar，"
            "禁止估算/伪造/用他量顶替）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphStateError(f"Task6 显存 sidecar 不可读/非法 JSON：{exc}") \
            from exc
    if not isinstance(data, dict):
        raise GraphStateError(f"Task6 显存 sidecar 顶层必须是对象：{path}")
    v = data.get(VRAM_KEY)
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(float(v)) or float(v) <= 0.0:
        raise GraphStateError(
            f"Task6 显存 sidecar {VRAM_KEY} 必须是正有限实数，收到 {v!r}")
    return float(v)


def _arm_mean(groups: dict[str, dict], key: str) -> float:
    vals = []
    for arm in _GROUPS_ARMS:
        v = groups.get(arm, {}).get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)):
            raise GraphStateError(
                f"Task6 组指标 {arm}.{key}={v!r} 必须是 finite 实数")
        vals.append(float(v))
    return sum(vals) / len(vals)


def _pareto_vector(groups: dict[str, dict], sim_wall_s: float,
                   vram_gb: float) -> dict[str, float]:
    """六指标 batch 标量（顺序/键集逐字对齐 pareto.METRIC_DIRECTIONS）。"""
    if not math.isfinite(sim_wall_s) or sim_wall_s <= 0.0:
        raise GraphStateError(f"Task6 solve_wallclock_s 非法：{sim_wall_s!r}")
    if not math.isfinite(vram_gb) or vram_gb <= 0.0:
        raise GraphStateError(f"Task6 vram_peak_gb 非法：{vram_gb!r}")
    return {
        "success_rate": _arm_mean(groups, "success_rate"),
        "slip_dist_mean_m": _arm_mean(groups, "slip_dist_mean_m"),
        "postpush_vmax_mean_mps": _arm_mean(
            groups, "postpush_vmax_mean_mps"),
        "tilt_max_deg_max": _arm_mean(groups, "tilt_max_deg_max"),
        "solve_wallclock_s": float(sim_wall_s),
        "vram_peak_gb": float(vram_gb),
    }


def _recompute_default(workdir: Path) -> tuple[dict, list[dict], int]:
    """V1 冻结档案：默认 Task8 profile 独立重算（加严 profile 不溯及 V1）。"""
    result = evaluate_batch(workdir, profile=None)
    if result.get("schema_errors"):
        raise GraphStateError(
            "Task6 V1 冻结档案重算 schema 失败："
            + "；".join(map(str, result["schema_errors"][:10])))
    _, rows, summary, errs = load_batch(workdir)
    if errs:
        raise GraphStateError(
            "Task6 V1 冻结档案 CSV/telemetry 读取失败：" + "；".join(errs[:10]))
    return result, rows, int(summary["seeds"])


def _load_innovation(v2_workdir: Path) -> dict:
    """V2 innovation.json：控制律原文 + DR 分布机产 JSON（chronicle 必填）。"""
    p = v2_workdir / INNOVATION_NAME
    if not p.is_file():
        raise GraphStateError(
            f"Task6 V2 创新件缺失：{p}（control_law/dr_distribution 必须随批 "
            "机产交付，接缝不代笔、不从提案文本编造）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphStateError(f"Task6 V2 创新件不可读/非法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise GraphStateError(f"Task6 V2 创新件顶层必须是对象：{p}")
    law = data.get("control_law")
    if not isinstance(law, str) or not law.strip():
        raise GraphStateError(
            f"Task6 V2 创新件 control_law 必须是非空字符串：{p}")
    dist = data.get("dr_distribution")
    if not isinstance(dist, dict) or not dist:
        raise GraphStateError(
            f"Task6 V2 创新件 dr_distribution 必须是非空机产对象：{p}")
    out = {"control_law": law, "dr_distribution": dist}
    eliminated = data.get("eliminated_assumptions")
    if eliminated is not None:
        if not isinstance(eliminated, list) or not eliminated \
                or not all(isinstance(x, str) and x.strip()
                           for x in eliminated):
            raise GraphStateError(
                f"Task6 V2 创新件 eliminated_assumptions 必须是非空字符串数组：{p}")
        out["eliminated_assumptions"] = eliminated
    return out


def _augmented_summary(*, run_id: str, groups: dict[str, dict],
                       pareto_metrics: dict[str, float],
                       contrast: dict, parent_run_id: "str | None" = None,
                       innovation: "dict | None" = None) -> dict:
    """增强版 summary（schema 沿用 telemetry.v1；chronicle 扩展块随附）。"""
    doc: dict[str, Any] = {
        "schema": TELEMETRY_SCHEMA,
        "run_id": run_id,
        # 只取三臂键、顺序固定；臂内额外键（fall_frames/mu_per_seed 等）
        # chronicle 容忍，保留机产值不删改。
        "groups": {arm: groups[arm] for arm in _GROUPS_ARMS},
        "pareto_metrics": {m: float(pareto_metrics[m])
                           for m in METRIC_DIRECTIONS},
        "contrast": contrast,
    }
    if parent_run_id is not None:
        doc["parent_run_id"] = parent_run_id
    if innovation is not None:
        doc["innovation"] = {
            "control_law": innovation["control_law"],
            "dr_distribution": innovation["dr_distribution"],
        }
        if "eliminated_assumptions" in innovation:
            doc["eliminated_assumptions"] = innovation["eliminated_assumptions"]
    return doc


def _write_json(path: Path, payload: dict) -> Path:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False,
                                       indent=2, sort_keys=True))
    return path


def materialize_v1_summary(thread_id: str, v1_archive: dict) -> tuple[dict, Path]:
    """重算冻结 V1 → 增强 V1 summary 落盘（Gate1 preflight 与 evaluate 幂等共用）。

    Q1 红线（硬规则 7）：V1 contrast 由 gait_rungs.contrast_block 从重算
    groups 现算，contradiction_control_not_robust / 0.824<2.0 原样进入产物，
    本函数不做任何「清洗」/改判/改名。
    """
    v1_wd = Path(v1_archive["host_workdir"])
    result, rows, seeds = _recompute_default(v1_wd)
    sdir = sources_dir(thread_id)
    v1_vram = _vram_gb(sdir / f"v1_{VRAM_NAME}")
    vector = _pareto_vector(result["groups"],
                            _csv_sim_wall_mean(rows, seeds), v1_vram)
    doc = _augmented_summary(
        run_id=str(v1_archive["thread_id"]),
        groups=result["groups"],
        pareto_metrics=vector,
        contrast=result["contrast"])
    return doc, _write_json(sdir / "v1_summary.json", doc)


# ---------------------------------------------------------------------------
# evaluate 接缝：task8 tight/active-only 批次评估 + ascent 对账
# ---------------------------------------------------------------------------

def make_evaluate_seam(gateway, visual_batch: str, thread_id: str):
    # V2 走 ascent 加严 profile；视觉 active-only：record_only/uncalibrated
    # 下内层接缝零视觉实呼（zero_visual_calls=True），Task6 永不自授 active。
    inner = task8.make_evaluate_seam(
        gateway, visual_batch, thread_id,
        threshold_profile=tight_profile(), visual_active_only=True)

    def seam(state: dict) -> dict:
        arc = _require_archive(state)
        metrics = inner(state)
        v2_wd = Path(metrics["workdir"])
        sdir = sources_dir(thread_id)

        v1_doc, v1_path = materialize_v1_summary(thread_id, arc)

        # M1 锁内适配（计划 L415-423：Pareto 六指标两侧必须同定义重算，
        # ε=5%；「加严的是验收严苛度」，不重定义 Pareto 指标）：tight
        # profile 的 contact_force_n=1.6N 会进入 recompute_run 的滑移积分
        # 门（FALL/35N 锚点不随 profile 变）。冻结 V1 9 条 telemetry
        # 实测 5/9 run 的滑移被改写（normal 0.1424→0.1427、
        # ice_nominal 0.1834→0.1835），若 V2 用 tight groups、V1 用默认
        # 口径，slip_dist_mean_m 跨版本不可比且系统性抬高 V2。故同一
        # v2_wd 再跑一次默认 profile（2.0N 门，与 V1 同口径），V2 Pareto
        # 向量与 v2_summary.groups/contrast 一律取默认结果；tight 结果
        # 只保留在 metrics.rungs/rules_pass 与下方加严证据段，升阶验收
        # 仍按加严。两结果都必须 schema 干净。
        default_result = evaluate_batch(v2_wd, profile=None)
        if default_result.get("schema_errors"):
            raise GraphStateError(
                "Task6 V2 batch 默认口径重算 schema 失败（Pareto 同口径"
                "硬要求，禁止回退加严口径）："
                + "；".join(map(str, default_result["schema_errors"][:10])))

        _, v2_rows, v2_summary, load_errs = load_batch(v2_wd)
        if load_errs:
            raise GraphStateError(
                "Task6 V2 batch CSV/telemetry 读取失败："
                + "；".join(load_errs[:10]))
        v2_seeds = int(v2_summary["seeds"])
        v2_vram = _vram_gb(v2_wd / VRAM_NAME)
        v2_vector = _pareto_vector(
            default_result["groups"],
            _csv_sim_wall_mean(v2_rows, v2_seeds), v2_vram)
        # 顺序敏感：第一参数必须是冻结 V1（容差带锚定 V1），禁止反序。
        verdict = pareto_verdict(v1_doc["pareto_metrics"], v2_vector)

        innovation = _load_innovation(v2_wd)
        v2_doc = _augmented_summary(
            run_id=thread_id,
            groups=default_result["groups"],
            pareto_metrics=v2_vector,
            contrast=default_result.get("contrast") or {},
            parent_run_id=str(arc["thread_id"]),
            innovation=innovation)
        v2_path = _write_json(sdir / "v2_summary.json", v2_doc)

        diff_rows = [{"metric": m,
                      "v1": v1_doc["pareto_metrics"][m],
                      "v2": v2_vector[m]}
                     for m in METRIC_DIRECTIONS]
        metrics["ascent"] = {
            "parent_run_id": str(arc["thread_id"]),
            "pareto_verdict": verdict,
            "metrics_diff": diff_rows,
            "v1_summary_path": str(v1_path),
            "v2_summary_path": str(v2_path),
            "v1_pareto_metrics": v1_doc["pareto_metrics"],
            "v2_pareto_metrics": dict(v2_vector),
            # 加严证据段（M1）：升阶验收按 tight profile；Pareto 按默认口径。
            "tight_acceptance": {
                "profile": "ascent_tight",
                "rules_pass": bool(metrics.get("rules_pass")),
                "rungs_pass": {
                    r: bool(metrics["rungs"][r].get("pass"))
                    for r in ("rung1", "rung2", "rung3")},
            },
        }
        return task8._json_safe(metrics)

    return seam


# ---------------------------------------------------------------------------
# Gate 意见件（simpraxis.gate_opinion.v1；ts 取自已决行 decided_at 毫秒）
# ---------------------------------------------------------------------------

def _opinion_payload(feishu, thread_id: str, gate: int,
                     decision: dict) -> dict:
    row = feishu.store.get_pending(thread_id, gate)
    if not isinstance(row, dict) or row.get("decided_at") is None:
        raise GraphStateError(
            f"Task6 Gate{gate} 批准后读不到已决行/decided_at（thread="
            f"{thread_id}）；禁止自造裁决时间")
    try:
        ms = int(row["decided_at"])
    except (TypeError, ValueError):
        raise GraphStateError(
            f"Task6 Gate{gate} decided_at 非整数毫秒：{row['decided_at']!r}") \
            from None
    if ms <= 0:
        raise GraphStateError(f"Task6 Gate{gate} decided_at={ms} 非正")
    notes = str(decision.get("human_notes") or "").strip()
    if not notes:
        raise GraphStateError(
            f"Task6 Gate{gate} 批准意见 human_notes 为空：演化志动机/踩坑段"
            "必须是真人原文，空意见不得代笔")
    source = decision.get("source")
    if source not in _OPINION_SOURCES:
        raise GraphStateError(
            f"Task6 Gate{gate} 意见 source={source!r} 不在生产词表"
            f"{list(_OPINION_SOURCES)}")
    ts_iso = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return {"schema": GATE_OPINION_SCHEMA, "gate": f"gate{gate}",
            "decision": "approve", "human_notes": notes,
            "source": source, "ts_iso": ts_iso}


# ---------------------------------------------------------------------------
# V1 冻结分屏解析（路径/字节数/sha 三锁）
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_v1_splits(v1_archive: dict) -> list[Path]:
    """两张 V1 split：只许 host_workdir + 登记 relpath，sha/锁定字节双对质。

    N1 锁内适配：实际核验路径一律取档案登记的 relpath（允许子目录登记，
    如 splits/x.mp4），禁止退回 wd/<basename>——否则根目录同名替身（即使
    字节相同）会顶替登记件通过对质。绝对路径/.. 分段/符号链接解析后逃逸
    host_workdir 一律 fail-closed。
    """
    wd = Path(v1_archive["host_workdir"])
    wd_resolved = wd.resolve()
    files = [f for f in v1_archive.get("files", []) if isinstance(f, dict)]
    out: list[Path] = []
    for _, name in SPLITS:
        # 先按登记 relpath 精确匹配；再按 basename 兜底子目录登记，
        # 多义（两个登记件同 basename）fail-closed。
        exact = [f for f in files if f.get("relpath") == name]
        rec = exact[0] if len(exact) == 1 else None
        if rec is None:
            base_hits = [f for f in files
                         if isinstance(f.get("relpath"), str)
                         and Path(f["relpath"]).name == name]
            if len(base_hits) > 1:
                raise GraphStateError(
                    f"Task6 V1 档案 split 登记多义：{name} 有 "
                    f"{[f['relpath'] for f in base_hits]}（禁止猜选）")
            rec = base_hits[0] if base_hits else None
        if rec is None or not isinstance(rec.get("sha256"), str) \
                or not isinstance(rec.get("relpath"), str) \
                or not rec["relpath"]:
            raise GraphStateError(
                f"Task6 V1 档案未登记 split：{name}（禁止用档案外视频对质）")
        rel_path = Path(rec["relpath"])
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise GraphStateError(
                "Task6 V1 split 登记 relpath 非法（绝对路径/.. 越界）："
                f"{rec['relpath']}")
        p = wd / rel_path
        try:
            contained = p.resolve().is_relative_to(wd_resolved)
        except OSError:
            contained = False
        if not contained or not p.is_file():
            raise GraphStateError(f"Task6 V1 split 不存在或越界：{p}")
        locked = LOCKED_V1_SPLIT_SIZES.get(name)
        size = p.stat().st_size
        if locked is not None and size != locked:
            raise GraphStateError(
                f"Task6 V1 冻结分屏 {name} 字节数 {size} != 锁定值 {locked}"
                "（冻结档案被改动，STOP）")
        if _sha256_file(p) != rec["sha256"]:
            raise GraphStateError(
                f"Task6 V1 冻结分屏 {name} sha256 与登记不符（冻结档案被改动，STOP）")
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Gate1 / Gate2 接缝
# ---------------------------------------------------------------------------

def make_gate1_seam(feishu, thread_id: str, task_title: str):
    """ascent Gate1：升阶假设卡（V1 冻结指标摘要）；批准后落 V1 意见件。"""

    def seam(state: dict) -> dict:
        arc = _require_archive(state)
        # Preflight 先于真人卡片：V1 重算/vram sidecar 缺件在此 fail-loud，
        # 避免把人请进来点批准后才崩（副作用：幂等覆写同字节 V1 summary）。
        _, v1_path = materialize_v1_summary(thread_id, arc)
        decision = feishu.request_gate1_ascent_approval(
            thread_id, task_title, state["proposals"],
            v1_summary=render_v1_metrics(arc))
        if decision.get("decision") != "approve":
            # 打回/未知值：不写意见件，decision 原样回节点（reject 路径不变）。
            return decision
        sdir = sources_dir(thread_id)
        opinion_path = _write_json(
            sdir / "gate1_opinion.json",
            _opinion_payload(feishu, thread_id, 1, decision))
        evolution_round = int(state.get("gate1_rejects", 0)) + 1
        return {**decision, "evolution_sources": {
            "evolution_round": evolution_round,
            "v1_summary_path": str(v1_path),
            "gate1_opinion_path": str(opinion_path)}}

    return seam


def make_gate2_seam(feishu, thread_id: str, task_title: str, *,
                    trim_for: "Callable[[dict, dict], dict | None] | None" = None):
    """ascent Gate2：四媒体严格顺序 Diff + 恰好一张 Δ 卡；批准补齐来源块。"""

    def seam(state: dict) -> dict:
        arc = _require_archive(state)
        metrics = state.get("metrics")
        if not isinstance(metrics, dict):
            raise GraphStateError("Task6 Gate2：state 缺 metrics")
        asc = metrics.get("ascent")
        if not isinstance(asc, dict) \
                or not isinstance(asc.get("metrics_diff"), list) \
                or "pareto_verdict" not in asc:
            raise GraphStateError(
                "Task6 Gate2：metrics.ascent 缺 Δ 行/Pareto 机判"
                "（evaluate 接缝未产出 ascent 块）")
        partial = state.get("evolution_sources")
        if not isinstance(partial, dict):
            raise GraphStateError(
                "Task6 Gate2：缺 Gate1 批准写入的 evolution_sources 部分块")
        round_no = partial.get("evolution_round")
        if isinstance(round_no, bool) or not isinstance(round_no, int) \
                or round_no < 1:
            raise GraphStateError(
                f"Task6 Gate2 evolution_round 非法：{round_no!r}")
        for key in ("v1_summary_path", "gate1_opinion_path"):
            p = partial.get(key)
            if not isinstance(p, str) or not Path(p).is_file():
                raise GraphStateError(
                    f"Task6 Gate2：Gate1 来源件缺失 {key}={p!r}")
        v2_summary_path = asc.get("v2_summary_path")
        if not isinstance(v2_summary_path, str) \
                or not Path(v2_summary_path).is_file():
            raise GraphStateError(
                f"Task6 Gate2：V2 增强 summary 缺失：{v2_summary_path!r}")

        v1_splits = resolve_v1_splits(arc)
        v2_wd = Path(metrics["workdir"])
        v2_splits: list[Path] = []
        for _, name in SPLITS:
            p = v2_wd / name
            if not p.is_file():
                raise GraphStateError(f"Task6 V2 分屏缺失：{p}（四媒体缺一不可）")
            v2_splits.append(p)

        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        trim = trim_for(state, rec) if trim_for is not None else None
        decision = feishu.request_gate2_diff4_approval(
            thread_id, task_title,
            str(v1_splits[0]), str(v1_splits[1]),
            str(v2_splits[0]), str(v2_splits[1]),
            asc["metrics_diff"], asc["pareto_verdict"],
            duration_ms=rec.get("duration_ms"),
            cover_png=rec.get("cover_png"),
            trim=trim,
            explanation=build_gate2_explanation(state, rec))
        if decision.get("decision") != "approve":
            return decision
        sdir = sources_dir(thread_id)
        gate2_path = _write_json(
            sdir / "gate2_opinion.json",
            _opinion_payload(feishu, thread_id, 2, decision))
        return {**decision, "evolution_sources": {
            "v1_summary_path": partial["v1_summary_path"],
            "v2_summary_path": v2_summary_path,
            "gate1_opinion_path": partial["gate1_opinion_path"],
            "gate2_opinion_path": str(gate2_path),
            "evolution_round": round_no}}

    return seam


# ---------------------------------------------------------------------------
# Gate2 四媒体卡三问说明（只写实验事实与判定口径；数字由卡片指标段渲染）
# ---------------------------------------------------------------------------

def build_gate2_explanation(state: dict, rec: dict) -> "dict[str, str]":
    scheme = (state.get("chosen") or {}).get("name") or "（未命名方案）"
    task = (
        f"Gate1 批准的 V2 候选：{scheme}\n"
        "面试题 Case A 的 V1→V2 升阶对质：RL 策略端一字未改，V2 只改控制器/"
        "DR 参数与实验构造。下面四条视频按冻结顺序发来：①② 是 V1 冻结档案的"
        "两张对照分屏（2026-10-16 批次，登记 sha256 与字节数锁定），③④ 是"
        "本次 V2 batch（三臂 normal/ice_nominal/ice_dr 各 3 籽，8 s、50 Hz，"
        "35 N×0.2 s 标准推力）同机位同 seed 0 分屏。"
    )
    problem = (
        "要对质的是 V2 是否在六项 Pareto 指标上不差于 V1（success_rate 容差"
        "绝对 0，其余相对 V1 基准 5%）且至少一项严格更优；互有胜负即 "
        "non_dominated，机判仅供参考、不代你裁决，禁止凑加权总分翻案。视觉上"
        "请重点对质：冰面侧足部滑移、倾覆过程连续性、DR 侧与名义冰面侧差异；"
        "穿地/瞬移/高频抖动等几何时序不自洽是缺陷，冰面打滑与倾覆本身是预期"
        "物理。V1 的 Q1 预注册矛盾（对照组本身不稳健）是冻结结论，V2 不负责"
        "「洗掉」它——矛盾原样报告。"
    )
    video = (
        "顺序固定：① V1 normal vs ice_nominal → ② V1 ice_nominal vs ice_dr "
        "→ ③ V2 normal vs ice_nominal → ④ V2 ice_nominal vs ice_dr；每条约 8 s。"
        "请同屏逐段对比 ①③ 与 ②④，再结合下方六指标 Δ 表做人类最终裁决。"
    )
    dur_ms = rec.get("duration_ms")
    if isinstance(dur_ms, (int, float)) and not isinstance(dur_ms, bool) \
            and dur_ms > 0:
        video += f"（V2 主分屏时长约 {dur_ms / 1000:.0f} s）"
    return {"task": task, "problem": problem, "video": video}
