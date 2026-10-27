#!/usr/bin/env python3
"""Phase 2 Task8 流固耦合（Q5 厨房倒水）图接缝：Stage0 能力闸 + Stage1 契约。

计划锚点（Phase_2_Plan_detailed.md Task8，L592-616；附录 A 五测 L958-962）：
先证容器内粒子流体能力存在（Stage0，0 方舟付费，Gate1 前），才谈研究结论。
Phase 0/1 只有刚体实跑，流体仅有 KB 文本 claim——Stage0 报告三种机判结局
``sph_ok`` / ``pbd_ok`` / ``both_fail``；both_fail 走 D-5：白皮书落证据包、
STOP 报用户，禁止拿文本 claim 充当能力证据。

本模块是**引擎层**接缝，自身零网络、零付费、零 genesis/torch 导入：

1. 常量单一源：看门狗/能力常量按路径加载
   ``sandbox/probes/probe_genesis_fluid.py``（sandbox/ 不是包，沿用仓内
   importlib spec_from_file_location 加载独立脚本的既有模式，如测试加载
   scripts/phase2/provision_v1_vram.py、host_shim 链式加载），probe 模块
   顶层只依赖 stdlib+numpy，宿主收集安全；本文件不复制任何魔数。
2. ``validate_stage0_report``：Stage0 报告 fail-closed 校验（schema/结局
   枚举/反射名单/attempt 必备键/api_used 必须能在反射名单中找到/GPU 段/
   时间戳/ok 与结局一致性），手造或缺件即 GraphStateError。
3. ``stage_gate1_approved_solution``：Gate1 批准后经 resolver 从 KB 内容层
   stage 方案（orchestrator.solutions.resolve_solution，引擎不携带题目脚本
   genesis_fluid_pour.py）；Stage0 阶段方案在内容层尚不存在，此函数在
   Gate1 批准前不得被图运行调用。
4. ``build_stage1_contract`` / ``execute_stage1``：Stage1 RunSpec 契约
   （executed=False，对齐 orchestrator/judge/frames.py「只生成命令契约、
   宿主机绝不执行」纪律）；execute_stage1 任何调用都显式拒绝。
5. ``fluid_evidence_pack``：证据包清单（Stage0 探针报告 + 能力缺口白皮书
   whitepaper_fluid_rigid_coupling.md）；白皮书只在 both_fail 路径入包，
   引擎不生成其内容（无 fake content）。
6. Stage1 CLI 接线（攢批第 74 条，task7_libero 家法镜像）：
   ``gate1_proposal``/``make_gate1_seam``（sph_ok/pbd_ok 提案、both_fail
   D-5 零卡片）、``make_spec_factory``（出生/回收双态 + 出生合同注入）、
   ``make_evaluate_seam``（results.json + trajectory.npz → 引擎现算入杯率
   /守恒 → simpraxis.wj_fluid_report.v1，零网关零视觉实呼）、
   ``build_gate2_explanation``（Gate2 三问）。85% 语义数字全部由探针
   几何函数从轨迹复算，脚本自报衍生字段一律 fail-closed。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from orchestrator.agent_runner.runner import ChunkSpec, RunSpec
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.common.runtime_paths import phase2_runs_root
from orchestrator.graph import task8_fluid_chunks as fchunks
from orchestrator.graph import task8_fluid_prompts as prompts
from orchestrator.graph.nodes import GraphStateError
from orchestrator.graph.task8 import VENV_PY
from orchestrator.judge.modality_probes import fluid_conservation
from orchestrator.solutions import (
    SolutionError,
    resolve_solution,
    solutions_root,
)

TASK = 8

# 图线程前缀（direction/index 出生的 Task8 Q5 线程统一此前缀，供 resolver
# staging 与 task=8 账套路由；前缀即字面常量，不在这里做分类）。
FLUID_THREAD_PREFIX = "t8q5-fluid-"

# Gate1 批准后才允许从 KB 内容层 stage 的解题方案 id（三层分离硬规则 3）：
# 方案脚本 genesis_fluid_pour.py 生于付费 workdir、Gate2 后回收进
# KB solutions/track2_questions/03_kitchen_pour/，引擎仓永不携带。
FLUID_SOLUTION_ID = "track2_questions/03_kitchen_pour"
FLUID_ENTRYPOINT = "genesis_fluid_pour.py"

# Stage0 探针报告/白皮书在 run 目录与证据包内的登记名。
STAGE0_REPORT_NAME = "stage0_fluid_report.json"
WHITEPAPER_NAME = "whitepaper_fluid_rigid_coupling.md"

# ---------------------------------------------------------------------------
# 常量单一源：按路径加载探针模块（sandbox 非包；仓内既有 spec 加载模式）
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_PATH = _REPO_ROOT / "sandbox" / "probes" / "probe_genesis_fluid.py"


def _load_probe_module():
    if not _PROBE_PATH.is_file():
        raise ImportError(f"流体探针模块缺失（引擎安装不完整）：{_PROBE_PATH}")
    spec = importlib.util.spec_from_file_location(
        "simpraxis_sandbox_probe_genesis_fluid", _PROBE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为流体探针构造模块 spec：{_PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # 探针内含 from __future__ annotations + dataclass：exec 前必须先入
    # sys.modules，否则 dataclasses KW_ONLY 字符串解析按 __module__ 查不到。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


_probe = _load_probe_module()

# 常量再导出（值与注释单点都在探针模块；本处只显式列名，禁止赋值字面量）。
PARTICLE_CAP = _probe.PARTICLE_CAP
VRAM_ABORT_GB = _probe.VRAM_ABORT_GB
CUDA_OOM_GUARD = _probe.CUDA_OOM_GUARD
SUBSTEP_MAX = _probe.SUBSTEP_MAX
MAX_REDUCTIONS = _probe.MAX_REDUCTIONS
STAGE0_N_MAX = _probe.STAGE0_N_MAX
STAGE0_STEPS = _probe.STAGE0_STEPS
SETTLING_WINDOW_S = _probe.SETTLING_WINDOW_S
CUP_ENTRY_TARGET = _probe.CUP_ENTRY_TARGET
STAGE0_REPORT_SCHEMA = _probe.REPORT_SCHEMA
OUTCOME_FIRST_FAMILY = _probe.OUTCOME_FIRST_FAMILY
OUTCOME_SECOND_FAMILY = _probe.OUTCOME_SECOND_FAMILY
OUTCOME_BOTH_FAIL = _probe.OUTCOME_BOTH_FAIL
FLUID_OUTCOMES = _probe.OUTCOMES

# 纯函数再导出（Stage1 评估/图节点复用，避免第二份几何/看门狗实现）。
cup_entry_metrics = _probe.cup_entry_metrics
CupGeometry = _probe.CupGeometry
vram_watchdog = _probe.vram_watchdog
discover_fluid_candidates = _probe.discover_fluid_candidates
classify_particles = _probe.classify_particles
settling_window_frame_count = _probe.settling_window_frame_count

# ---------------------------------------------------------------------------
# Stage1 接线常量（攢批第 74 条；命名/超时沿 task7_libero/task6 V2 家法）
# ---------------------------------------------------------------------------

STAGE1_POUR = "stage1_pour"
PLATFORM = "genesis_fluid"

# state 键（state.py 已声明 stage0_report_path；与 task7 同名字面量、各自模块
# 命名空间，CLI 按 variant 注入其一）。
STAGE0_REPORT_STATE_KEY = "stage0_report_path"
WJ_REPORT_STATE_KEY = "wj_report_path"

# WJ 流体报表（与 libero 的 simpraxis.wj_report.v1 平行：聚合口径不同——
# 入杯率/守恒/飞溅/越界，无 per-task 成功率——故独立 schema，不复用 v1）。
WJ_FLUID_SCHEMA = "simpraxis.wj_fluid_report.v1"
WJ_FLUID_VERSION = 1
JUDGE_GEOMETRY = "几何机判"
WJ_REPORT_NAME = "wj_report.json"
CURVE_CSV_NAME = "cup_entry_curve.csv"

# workdir 五件套（出生合同与组装器同一来源）。
RESULTS_NAME = "results.json"
TRAJECTORY_NAME = "trajectory.npz"
VIDEO_NAME = "video.mp4"
COVER_NAME = "cover.png"
MANIFEST_NAME = "manifest.json"

# 脚本自报禁止键（衍生量全部引擎现算；出现即整线程结案）。
_FORBIDDEN_DERIVED = (
    "in_cup_ratio", "entry_ratio_median", "ratio_at_count_moment",
    "splash_ratio", "splash_ratio_median", "out_of_bounds_ratio",
    "out_of_bounds_ratio_median", "conservation", "cup_entry_rate",
    "window_median", "success_rate", "aggregate",
)

# 出生合同 coding 单次上限（task6 V2 / task7 libero CODING_* 同值家法）。
CODING_MAX_TOKENS = 32768
CODING_TIMEOUT_S = 600
# 倒水 batch：容器内 GS init/编译 + 粒子仿真 + 渲染；沿 task8 BATCH_TIMEOUT_S
# 家法 2400 s（coding 重跑每轮都是完整实跑）。
STAGE1_RUN_TIMEOUT_S = 2400

# D-5：Stage0 both_fail → 白皮书 STOP（对齐 libero D-4 语义位）。
DEGRADATION_D5 = "D-5"

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_KINDS = ("video", "image", "log")

_REQUIRED_ATTEMPT_KEYS = (
    "api_used", "n_particles", "steps", "completed_steps", "finite_coords",
    "passed", "vram_abort", "position_accessor", "vram_peak_gb",
    "reductions", "failure_verbatim")
_REQUIRED_GPU_KEYS = ("name", "baseline_mib", "peak_mib")


# ---------------------------------------------------------------------------
# Stage0 报告 fail-closed 校验
# ---------------------------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise GraphStateError(f"Task8 Stage0 报告校验失败：{msg}")


def validate_stage0_report(data: Any) -> dict:
    """校验 Stage0 流体能力报告；任何手造/缺件/不一致即 GraphStateError。

    Gate1 preflight 用：Stage0 事实是 Gate1 卡片的必填输入，报告不合法不得
    请真人批准（不把人请进来后才崩）。校验只承认探针写出的结构，绝不代为
    补字段或把 both_fail「修」成通过。
    """
    _require(isinstance(data, dict), "顶层必须是 JSON 对象")
    _require(data.get("schema") == STAGE0_REPORT_SCHEMA,
             f"schema 必须是 {STAGE0_REPORT_SCHEMA}，收到 {data.get('schema')!r}")

    ok = data.get("ok")
    _require(isinstance(ok, bool), "ok 必须是布尔值")
    outcome = data.get("outcome")
    _require(outcome in FLUID_OUTCOMES,
             f"outcome 必须属于 {FLUID_OUTCOMES}，收到 {outcome!r}")

    attempts = data.get("attempts")
    _require(isinstance(attempts, list),
             "attempts 必须是数组（冷读 C 项：反射/init 灾难路径允许空数组）")
    # 冷读 C 项：attempts 为空只在 ok=false + both_fail（gs.init 即失败/API
    # 不存在等反射前灾难）合法，且必须带非空 note 交代灾难原文；任何其他
    # 结局（尤其能力证实）空 attempts 一律拒绝。
    early_failure = (ok is False and outcome == OUTCOME_BOTH_FAIL
                     and len(attempts) == 0)
    if early_failure:
        note = data.get("note")
        _require(isinstance(note, str) and bool(note.strip()),
                 "attempts 为空的 both_fail 灾难报告必须带非空 note（灾难原文）")
    else:
        _require(len(attempts) > 0,
                 "attempts 非空是能力声明/逐候选失败的必备证据")

    reflected = data.get("reflected_materials")
    _require(isinstance(reflected, dict),
             "reflected_materials 必须是对象（两层反射名单）")
    top = reflected.get("top")
    nested = reflected.get("nested")
    # 与 early_failure 同一灾难路径：反射子进程尚未产出名单时 top 允许为空，
    # 且仅此时允许；nested 任何路径都必须是 dict。
    top_ok = (isinstance(top, list)
              and (early_failure or len(top) > 0)
              and all(isinstance(x, str) for x in top))
    _require(top_ok,
             "reflected_materials.top 必须是字符串数组"
             "（非空，除 both_fail+空 attempts 反射前灾难路径）")
    _require(isinstance(nested, dict)
             and all(isinstance(k, str) and isinstance(v, list)
                     and all(isinstance(x, str) for x in v)
                     for k, v in nested.items()),
             "reflected_materials.nested 必须是 {命名空间: [叶子名]}")

    for i, row in enumerate(attempts):
        _require(isinstance(row, dict), f"attempts[{i}] 必须是对象")
        for key in _REQUIRED_ATTEMPT_KEYS:
            _require(key in row, f"attempts[{i}] 缺键 {key}")
        _require(isinstance(row["api_used"], str) and "." in row["api_used"],
                 f"attempts[{i}].api_used 必须是 '命名空间.叶子' 反射路径")
        ns, _leaf = row["api_used"].split(".", 1)
        _require(ns in nested,
                 f"attempts[{i}].api_used={row['api_used']!r} 的命名空间不在"
                 "反射名单中（禁止声明未反射到的 API）")
        _require(_leaf in nested[ns],
                 f"attempts[{i}].api_used={row['api_used']!r} 的叶子不在"
                 f" {ns} 的反射名单中")
        _require(isinstance(row["n_particles"], int)
                 and not isinstance(row["n_particles"], bool)
                 and 0 <= row["n_particles"] <= STAGE0_N_MAX,
                 f"attempts[{i}].n_particles 必须是 0..{STAGE0_N_MAX} 整数")
        _require(isinstance(row["steps"], int)
                 and not isinstance(row["steps"], bool)
                 and 0 < row["steps"] <= STAGE0_STEPS,
                 f"attempts[{i}].steps 必须是 1..{STAGE0_STEPS} 整数"
                 "（机判口径固定，不接受手造漂移）")
        _require(isinstance(row["completed_steps"], int)
                 and not isinstance(row["completed_steps"], bool)
                 and 0 <= row["completed_steps"] <= row["steps"],
                 f"attempts[{i}].completed_steps 必须是 0..steps 整数")
        _require(isinstance(row["finite_coords"], bool),
                 f"attempts[{i}].finite_coords 必须是布尔")
        _require(isinstance(row["passed"], bool),
                 f"attempts[{i}].passed 必须是布尔（冷读 E 项）")
        _require(isinstance(row["vram_abort"], bool),
                 f"attempts[{i}].vram_abort 必须是布尔（冷读 E 项）")
        _require(isinstance(row["position_accessor"], str)
                 and bool(row["position_accessor"].strip()),
                 f"attempts[{i}].position_accessor 必须是非空字符串"
                 "（冷读 A 项：实际位置读取方法名必须落证）")
        _require(isinstance(row["reductions"], int)
                 and 0 <= row["reductions"] <= MAX_REDUCTIONS,
                 f"attempts[{i}].reductions 必须是 0..{MAX_REDUCTIONS}")
        vram = row["vram_peak_gb"]
        _require(isinstance(vram, (int, float)) and not isinstance(vram, bool)
                 and float(vram) >= 0.0,
                 f"attempts[{i}].vram_peak_gb 必须是非负实数")
        verbatim = row["failure_verbatim"]
        if row["passed"]:
            _require(verbatim is None,
                     f"attempts[{i}] passed=true 时 failure_verbatim 必须为 null")
        else:
            _require(isinstance(verbatim, str) and bool(verbatim.strip()),
                     f"attempts[{i}] passed=false 必须带非空 failure_verbatim"
                     "（失败原文 fail-closed，禁止手造宽松件）")

    gpu = data.get("gpu")
    _require(isinstance(gpu, dict), "gpu 段必须是对象")
    for key in _REQUIRED_GPU_KEYS:
        _require(key in gpu, f"gpu 缺键 {key}")
    _require(isinstance(gpu["name"], str) and gpu["name"],
             "gpu.name 必须是非空字符串（真 GPU 取证）")
    for key in ("baseline_mib", "peak_mib"):
        _require(isinstance(gpu[key], (int, float))
                 and not isinstance(gpu[key], bool) and float(gpu[key]) >= 0.0,
                 f"gpu.{key} 必须是非负实数")

    ts = data.get("ts_utc")
    _require(isinstance(ts, str) and len(ts) >= 10,
             "ts_utc 必须是 UTC 时间戳字符串")

    passed_rows = [r for r in attempts if r.get("passed") is True
                   and r.get("finite_coords") is True]
    if ok:
        _require(outcome in (OUTCOME_FIRST_FAMILY, OUTCOME_SECOND_FAMILY),
                 "ok=true 的结局必须是能力证实结局")
        _require(bool(passed_rows),
                 "ok=true 但没有任何 passed 且 finite_coords 的 attempt（禁止假能力）")
        # 冷读 E 项：能力证实行必须跑完全部固定帧且非看门狗中止
        # （其前的下调/失败行不受此限——它们本来就是 passed=false）。
        for i, row in enumerate(attempts):
            if row.get("passed") is True:
                _require(row["vram_abort"] is False
                         and row["completed_steps"] == row["steps"]
                         == STAGE0_STEPS,
                         f"attempts[{i}] passed=true 但未跑满 "
                         f"{STAGE0_STEPS} 帧或带 vram_abort（禁止手造宽松件）")
    else:
        _require(outcome == OUTCOME_BOTH_FAIL,
                 f"ok=false 必须对应 {OUTCOME_BOTH_FAIL}，收到 {outcome!r}")
        _require(not passed_rows,
                 "ok=false 却存在 passed attempt（结局与实跑记录矛盾）")
    return data


def load_stage0_report(path: "str | Path") -> dict:
    """从 run 目录读 Stage0 报告并校验；缺件/坏 JSON fail-closed。"""
    p = Path(path)
    _require(p.is_file(), f"Stage0 报告缺失：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphStateError(
            f"Task8 Stage0 报告不可读/非法 JSON：{p}: {exc}") from exc
    return validate_stage0_report(data)


def _runs_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))


def default_stage0_report_path() -> Path:
    """CLI --stage0-report 缺省落点（与 Stage0 探针产物目录同口径：
    RUN_DIR/phase2/task8/stage0，t7 default_stage0_report_path 家法）。"""
    return _runs_root() / "phase2" / "task8" / "stage0" / STAGE0_REPORT_NAME


def stage0_report_from_state(state: dict) -> dict:
    """从 state 解析 Stage0 报告路径并装载（chosen 优先，兼容顶层键）。"""
    chosen = state.get("chosen") or {}
    path = chosen.get(STAGE0_REPORT_STATE_KEY) or state.get(
        STAGE0_REPORT_STATE_KEY)
    if not isinstance(path, str) or not path:
        raise GraphStateError(
            "Task8 流体缺 Stage0 能力报告路径"
            f"（chosen.{STAGE0_REPORT_STATE_KEY}）：未跑 Stage0 禁止提 Gate1 运行案")
    return load_stage0_report(path)


def gate1_proposal(state: dict) -> dict:
    """Stage0 事实 → Gate1 提案机判（确定性，零付费）。

    sph_ok/pbd_ok：可提 Stage1 运行案；both_fail：propose_run=False，按 D-5
    降级为白皮书 STOP 路径，绝不生成 Gate1 运行案，更不伪装跑通。
    """
    report = stage0_report_from_state(state)
    passed = next((r for r in report["attempts"] if r.get("passed") is True),
                  None)
    attempt = passed or (report["attempts"][0] if report["attempts"] else None)
    base = {
        "platform": PLATFORM,
        "stage0_schema": report["schema"],
        "outcome": report["outcome"],
        "solver_api": attempt["api_used"] if attempt else None,
        "n_particles": attempt["n_particles"] if attempt else None,
        "gpu": report["gpu"],
        "versions": report.get("versions"),
        "ts_utc": report["ts_utc"],
        "particle_cap": PARTICLE_CAP,
        "cup_entry_target": CUP_ENTRY_TARGET,
        "settling_window_s": SETTLING_WINDOW_S,
    }
    if report["outcome"] in (OUTCOME_FIRST_FAMILY, OUTCOME_SECOND_FAMILY):
        return {"propose_run": True, **base}
    return {"propose_run": False, "degradation": DEGRADATION_D5, **base}


def _last_event(state: dict, kind: str) -> "dict | None":
    for ev in reversed(state.get("events", [])):
        if ev.get("type") == kind:
            return ev
    return None


# ---------------------------------------------------------------------------
# Gate1 批准方案的 resolver staging（内容层；Stage0 阶段不调用）
# ---------------------------------------------------------------------------

def fluid_run_dir(thread_id: str, run_idx: int) -> Path:
    """Task8 流体 run 工作目录（phase2 runs 根/task8_fluid/run_<thread>_<n>）。"""
    d = phase2_runs_root() / "task8_fluid" / f"run_{thread_id}_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_gate1_approved_solution(
        workdir: "str | Path", *,
        resolver: Callable[[str], Any] = resolve_solution):
    """Gate1 批准后：从 KB 内容层 stage Q5 倒水方案（逐件 sha256/C4 fail-closed）。

    resolver 参数仅为接缝注入/单测；生产路径恒走
    orchestrator.solutions.resolve_solution。Stage0 阶段内容层尚无此方案，
    任何早于 Gate1 批准的调用都会在 resolver 处抛 SolutionError——这是正确行为。
    """
    return resolver(FLUID_SOLUTION_ID).stage(workdir)


# ---------------------------------------------------------------------------
# Stage1 RunSpec 契约（executed=False；对齐 judge/frames.py 命令契约纪律）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage1FluidContract:
    """Stage1 容器实跑的冻结契约：本接缝只出生契约，绝不在图导入/主机执行。"""
    executed: bool
    stage0_outcome: str
    solution_id: str
    entrypoint: str
    cmd: tuple[str, ...]
    particle_cap: int
    vram_abort_gb: float
    substep_start: int
    max_reductions: int
    settling_window_s: float
    cup_entry_target: float


def build_stage1_contract(*, stage0_outcome: str, workdir: "str | Path",
                          thread_id: str, run_idx: int = 1) -> Stage1FluidContract:
    """出生 Stage1 命令契约（executed=False）。

    cmd 指向 Gate1 后生于付费 workdir 的解题脚本（出生时不存在，与 Task6
    V2 同构：首个 coding 轮以「文件不存在」失败，补丁在 workdir 内出生，
    引擎仓永不携带）。粒子上限/看门狗/沉降窗/85% 口径全部引用探针单点常量。
    """
    if stage0_outcome not in (OUTCOME_FIRST_FAMILY, OUTCOME_SECOND_FAMILY):
        raise GraphStateError(
            f"只有能力证实结局可出生 Stage1 契约，收到 {stage0_outcome!r}"
            "（both_fail 走白皮书 STOP 路径）")
    target = Path(workdir) / FLUID_ENTRYPOINT
    cmd = (
        VENV_PY, str(target),
        "--particle-cap", str(PARTICLE_CAP),
        "--vram-abort-gb", str(VRAM_ABORT_GB),
        "--substep-start", str(SUBSTEP_MAX),
        "--settling-window-s", str(SETTLING_WINDOW_S),
        "--thread", thread_id,
        "--run-idx", str(run_idx),
    )
    return Stage1FluidContract(
        executed=False,
        stage0_outcome=stage0_outcome,
        solution_id=FLUID_SOLUTION_ID,
        entrypoint=FLUID_ENTRYPOINT,
        cmd=cmd,
        particle_cap=PARTICLE_CAP,
        vram_abort_gb=VRAM_ABORT_GB,
        substep_start=SUBSTEP_MAX,
        max_reductions=MAX_REDUCTIONS,
        settling_window_s=SETTLING_WINDOW_S,
        cup_entry_target=CUP_ENTRY_TARGET)


def execute_stage1(*_args: Any, **_kwargs: Any) -> None:
    """显式拒绝执行：Stage1 只能在 Gate1 批准后由容器侧 runner 跑 cmd。"""
    raise RuntimeError(
        "Task8 Stage1 流体实跑在图接缝内 executed=False：Gate1 批准前禁止"
        "容器实跑（No Fake Approval），宿主机永不执行该 cmd")


# ---------------------------------------------------------------------------
# Stage1 agent_run 工厂（出生/回收双态，task7_libero 家法镜像）
# ---------------------------------------------------------------------------

def make_spec_factory(thread_id: str, coding_batch: str, *,
                      task_title: str = "") -> Callable[[dict], RunSpec]:
    """Stage1 agent_run 工厂（出生/回收双态，task6 V2/task7 家法、单次合同）。

    三层分离闸：内容层尚无 ``track2_questions/03_kitchen_pour``（Gate2 后才
    回收）时走**出生态**——cmd 指向出生时不存在的入口文件，首次 exec 必然
    「文件不存在」失败，coding 环按 ``chunk_birth_contract`` 以 runner n=6
    sentinel 围栏拆块通道出生脚本（攢批93，T8-D2 取证：kimi-k3.1-pro 单次
    出生 probe E 605.1s/126682 字独白截断，六块家法镜像 Task6 V2 run4）；
    回收后自动切 resolve+stage 逐件 sha256 态（清单损坏
    SolutionError 包成 GraphStateError fail-closed，**绝不**回退出生把损坏
    当不存在）。

    被调用即表示已过 Gate1——Stage0 both_fail 在这里是机器硬闸（D-5，不是
    卡片文案问题）。
    """
    def factory(state: dict) -> RunSpec:
        proposal = gate1_proposal(state)
        if not proposal["propose_run"]:
            raise GraphStateError(
                "Task8 流体 Stage0 结局 "
                f"{proposal['outcome']}：按 {DEGRADATION_D5} 降级白皮书 STOP，"
                "不得构造容器运行 RunSpec（No Fake Approval）")

        run_idx = len(state.get("agent_runs", [])) + 1
        workdir = fluid_run_dir(thread_id, run_idx)

        birth = False
        if (solutions_root() / FLUID_SOLUTION_ID).is_dir():
            # 三层分离：已回收 → 题目脚本只来自内容层 manifest（sha256/C4
            # 全量校验）；任何损坏 fail-closed，不静默回退出生。
            try:
                staged = resolve_solution(FLUID_SOLUTION_ID).stage(workdir)
            except SolutionError as exc:
                raise GraphStateError(
                    f"Task8 流体方案 stage 失败（{FLUID_SOLUTION_ID}，"
                    f"内容层已回收但清单/落件不合规）：{exc}"
                ) from exc
            target = staged.entrypoint
            if target.name != FLUID_ENTRYPOINT:
                raise GraphStateError(
                    f"Task8 流体入口应为 {FLUID_ENTRYPOINT}，"
                    f"manifest 给的是 {target.name}")
        else:
            # 出生态：工厂只登记目标路径，既不创建也不删除（同 run 重入时已
            # 出生的文件保留——runner 失败重试/coding 补丁在自己那层语义）。
            birth = True
            target = workdir / FLUID_ENTRYPOINT

        # cmd 单一来源：出生合同与 RunSpec 同一 build_stage1_contract 产物。
        contract = build_stage1_contract(
            stage0_outcome=proposal["outcome"], workdir=workdir,
            thread_id=thread_id, run_idx=run_idx)
        ev = _last_event(state, "gate2_rejected")
        notes = ev["human_notes"] if ev else None
        env_extra = {
            # GS_ENABLE_ZEROCOPY=0：默认 zerocopy 路径 int32 gather 抛错的
            # 必需正确性开关（task8 Track2 生产脚本同款）；HOME 指向持久缓存
            # （大文件只落 /mnt）；HF 走镜像，构建/运行直连。
            "HOME": "/mnt/caches/home",
            "HF_HOME": "/mnt/caches/hf",
            "HF_ENDPOINT": "https://hf-mirror.com",
            "SIMPRAXIS_USE_PROXY": "0",
            "GS_ENABLE_ZEROCOPY": "0",
            "PYOPENGL_PLATFORM": "egl",
        }
        # 攢批93（T8-D2 账外取证后的锁内适配，2026-10-27）：出生态固定挂
        # runner n=6 拆块出生规格（sentinel 围栏，task6 V2 run4 家法）。
        # kimi-k3.1-pro 对完整流体出生合同单次出活结构性不可行（probe E：
        # thinking disabled+temp0.6 下 605.1s/126682 字规划独白走 text 通道、
        # stop=max_tokens 截断）；n/标题/边界在 task8_fluid_chunks 起飞前
        # 冻结。同 run 重入（文件已出生）不挂 spec，修补轮回单补丁通道；
        # Gate2 打回后的新 run 目录 target 仍 absent → 重新挂六块规格。
        chunked = None
        if birth and not target.exists():
            chunked = ChunkSpec(
                target_path=FLUID_ENTRYPOINT,
                chunk_count=fchunks.FLUID_CHUNK_COUNT,
                chunk_titles=fchunks.FLUID_CHUNK_TITLES,
                per_call_sections=(
                    ("Task8 Q5 流体拆块出生合同",
                     prompts.chunk_birth_contract(
                         script_name=FLUID_ENTRYPOINT,
                         cmd_line=" ".join(contract.cmd),
                         stage0_facts_text=prompts.stage0_facts_text(proposal),
                         particle_cap=PARTICLE_CAP,
                         vram_abort_gb=VRAM_ABORT_GB,
                         max_reductions=MAX_REDUCTIONS,
                         settling_window_s=SETTLING_WINDOW_S,
                         cup_entry_target=CUP_ENTRY_TARGET,
                         n_chunks=fchunks.FLUID_CHUNK_COUNT)),
                    ("已批准 macro 提案",
                     json.dumps(state["chosen"], ensure_ascii=False, indent=2)),
                ),
                chunk_boundaries=tuple(
                    fchunks.build_chunk_boundary(i)
                    for i in range(fchunks.FLUID_CHUNK_COUNT)),
            )
        return RunSpec(
            thread_id=thread_id,
            workdir=workdir,
            cmd=list(contract.cmd),
            # telemetry = results.json：rc0 缺件修复轮与 evaluate 产物定位
            # 同一文件（workdir = telemetry_uri.parent）。
            telemetry_relpath=RESULTS_NAME,
            run_idx=run_idx,
            batch_id=coding_batch,
            billing_task=TASK,
            gate2_notes=notes,
            env_extra=env_extra,
            run_timeout_s=STAGE1_RUN_TIMEOUT_S,
            task_title=task_title or "Task8 Q5 厨房倒水流体实跑（Genesis SPH）",
            video_relpath=VIDEO_NAME,
            cover_relpath=COVER_NAME,
            manifest_relpath=MANIFEST_NAME,
            coding_max_tokens=CODING_MAX_TOKENS,
            coding_timeout_s=CODING_TIMEOUT_S,
            chunked_birth=chunked,
        )

    return factory


# ---------------------------------------------------------------------------
# WJ 流体报表：simpraxis.wj_fluid_report.v1（版本化常量 + 手写 fail-closed
# 校验；不引第三方 jsonschema）。WJ-Lite 溯源原则保留：metrics_ref/
# trajectory_ref sha256、判分来源显式标注、Gate2 媒体齐备。
# ---------------------------------------------------------------------------

_WJ_REQUIRED = ["schema", "version", "task", "platform", "thread_id", "stage",
                "ts_utc", "solver_api", "stage0_compat_ref", "cup", "metrics",
                "conservation", "metrics_ref", "trajectory_ref", "judge_source",
                "media", "gate2_ready"]


class WjFluidReportError(ValueError):
    """WJ 流体报告缺失/结构违规/数字不可溯源（图接缝据此 fail-closed）。"""


def _werr(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def _ratio01(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(float(v)) and 0.0 <= float(v) <= 1.0


def _cup_dict_ok(cup: Any) -> bool:
    if not isinstance(cup, dict):
        return False
    try:
        CupGeometry(inner_radius=float(cup["inner_radius"]),
                    bottom_z=float(cup["bottom_z"]),
                    rim_z=float(cup["rim_z"]))
    except (KeyError, TypeError, ValueError):
        return False
    return True


def validate_wj_fluid_report(obj: Any) -> list[str]:
    """返回错误列表（空 = 合法）。缺溯源/坏判分器来源标签/媒体错配均拒。"""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["WJ 流体报告顶层必须是对象"]
    for k in _WJ_REQUIRED:
        if k not in obj:
            errs.append(f"缺必填字段：{k}")
    if errs:
        return errs

    _werr(obj["schema"] == WJ_FLUID_SCHEMA,
          f"schema 必须是 {WJ_FLUID_SCHEMA}", errs)
    _werr(obj["version"] == WJ_FLUID_VERSION,
          f"version 必须是 {WJ_FLUID_VERSION}", errs)
    _werr(obj["task"] == TASK, "task 必须是 8", errs)
    _werr(obj["platform"] == PLATFORM,
          f"platform 必须是 {PLATFORM}", errs)
    _werr(isinstance(obj["thread_id"], str) and bool(obj["thread_id"]),
          "thread_id 必须是非空字符串", errs)
    _werr(obj["stage"] == STAGE1_POUR,
          f"stage 必须是 {STAGE1_POUR}", errs)
    _werr(isinstance(obj["ts_utc"], str) and bool(_TS_RE.match(obj["ts_utc"])),
          "ts_utc 必须是严格 UTC Z", errs)
    _werr(isinstance(obj["solver_api"], str) and bool(obj["solver_api"]),
          "solver_api 必须是非空字符串", errs)
    _werr(isinstance(obj["stage0_compat_ref"], str)
          and bool(obj["stage0_compat_ref"]),
          "stage0_compat_ref 必须非空（数字须可回链 Stage0 事实）", errs)
    _werr(_cup_dict_ok(obj["cup"]),
          "cup 必须含有限实数 inner_radius>0/bottom_z/rim_z>bottom_z", errs)

    m = obj["metrics"]
    if not isinstance(m, dict):
        errs.append("metrics 必须是对象")
    else:
        for k in ("entry_ratio_median", "ratio_at_count_moment",
                  "splash_ratio_median", "out_of_bounds_ratio_median"):
            _werr(_ratio01(m.get(k)), f"metrics.{k} 必须是 [0,1] 实数", errs)
        _werr(isinstance(m.get("count_moment_frame"), int)
              and not isinstance(m.get("count_moment_frame"), bool)
              and m.get("count_moment_frame") >= 0,
              "metrics.count_moment_frame 必须是 ≥0 整数", errs)
        _werr(isinstance(m.get("window_frames"), int)
              and not isinstance(m.get("window_frames"), bool)
              and m.get("window_frames") >= 1,
              "metrics.window_frames 必须是 ≥1 整数", errs)
        _werr(isinstance(m.get("target"), (int, float))
              and not isinstance(m.get("target"), bool)
              and float(m["target"]) > 0.0,
              "metrics.target 必须是正实数", errs)
        _werr(isinstance(m.get("meets_target"), bool),
              "metrics.meets_target 必须是布尔", errs)
        if _ratio01(m.get("entry_ratio_median")) and _ratio01(m.get("target")) \
                and isinstance(m.get("meets_target"), bool):
            _werr(m["meets_target"] == (m["entry_ratio_median"] >= m["target"]),
                  "metrics.meets_target 必须 == entry_ratio_median >= target",
                  errs)
        _werr(isinstance(m.get("particle_count"), int)
              and not isinstance(m.get("particle_count"), bool)
              and 1 <= m["particle_count"] <= PARTICLE_CAP,
              f"metrics.particle_count 必须是 1..{PARTICLE_CAP} 整数", errs)
        _werr(isinstance(m.get("frame_dt_s"), (int, float))
              and not isinstance(m.get("frame_dt_s"), bool)
              and math.isfinite(float(m["frame_dt_s"]))
              and float(m["frame_dt_s"]) > 0.0,
              "metrics.frame_dt_s 必须是正有限实数", errs)

    cons = obj["conservation"]
    if not isinstance(cons, dict):
        errs.append("conservation 必须是对象")
    else:
        _werr(isinstance(cons.get("pass"), bool),
              "conservation.pass 必须是布尔", errs)
        _werr(isinstance(cons.get("violations"), list)
              and all(isinstance(v, str) for v in cons["violations"]),
              "conservation.violations 必须是字符串数组", errs)
        _werr(isinstance(cons.get("checks"), dict),
              "conservation.checks 必须是对象", errs)

    for ref_name in ("metrics_ref", "trajectory_ref"):
        ref = obj[ref_name]
        if not isinstance(ref, dict):
            errs.append(f"{ref_name} 必须是对象（数字不可溯源）")
            continue
        if ref_name == "metrics_ref":
            _werr(isinstance(ref.get("path"), str) and bool(ref["path"]),
                  "metrics_ref.path 必须非空", errs)
            _werr(isinstance(ref.get("keys"), list) and ref["keys"]
                  and all(isinstance(k, str) and k for k in ref["keys"]),
                  "metrics_ref.keys 必须是非空字符串数组", errs)
        else:
            _werr(ref.get("path") == TRAJECTORY_NAME,
                  f"trajectory_ref.path 必须是 {TRAJECTORY_NAME}", errs)
        sha = ref.get("sha256")
        _werr(sha is None or (isinstance(sha, str)
                              and bool(_SHA256_RE.match(sha))),
              f"{ref_name}.sha256 必须是 64 位小写 hex 或 null", errs)

    _werr(obj["judge_source"] == JUDGE_GEOMETRY,
          f"judge_source 必须是 {JUDGE_GEOMETRY}", errs)

    media = obj["media"]
    media_paths: set[str] = set()
    if not isinstance(media, list) or not media:
        errs.append("media 必须是非空数组")
    else:
        for i, md in enumerate(media):
            if not isinstance(md, dict):
                errs.append(f"media[{i}] 必须是对象")
                continue
            _werr(md.get("kind") in MEDIA_KINDS,
                  f"media[{i}].kind 必须是 {MEDIA_KINDS} 之一", errs)
            if not isinstance(md.get("path"), str) or not md["path"]:
                errs.append(f"media[{i}].path 必须非空")
            elif md["path"] in media_paths:
                errs.append(f"media[{i}].path 重复：{md['path']}")
            else:
                media_paths.add(md["path"])
            sha = md.get("sha256")
            _werr(sha is None or (isinstance(sha, str)
                                  and bool(_SHA256_RE.match(sha))),
                  f"media[{i}].sha256 必须是 64 位小写 hex 或 null", errs)

    _werr(isinstance(obj["gate2_ready"], bool),
          "gate2_ready 必须是布尔", errs)
    if obj.get("gate2_ready") is True and isinstance(media, list):
        if not any(isinstance(md, dict) and md.get("kind") == "video"
                   for md in media):
            errs.append("gate2_ready=true 时顶层 media 至少含一条 video")
    return errs


def build_wj_fluid_report(*, thread_id: str, solver_api: str, cup: dict,
                          metrics: dict, conservation: dict,
                          metrics_ref: dict, trajectory_ref: dict,
                          media: list[dict], stage0_compat_ref: str,
                          gate2_ready: bool,
                          ts_utc: "str | None" = None) -> dict:
    """由 Stage1 评估产物组装 wj_fluid_report v1，组装后立即自校验；
    非法输入抛 WjFluidReportError——不让非法件出引擎。"""
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "schema": WJ_FLUID_SCHEMA,
        "version": WJ_FLUID_VERSION,
        "task": TASK,
        "platform": PLATFORM,
        "thread_id": thread_id,
        "stage": STAGE1_POUR,
        "ts_utc": ts_utc,
        "solver_api": solver_api,
        "stage0_compat_ref": stage0_compat_ref,
        "cup": cup,
        "metrics": metrics,
        "conservation": conservation,
        "metrics_ref": metrics_ref,
        "trajectory_ref": trajectory_ref,
        "judge_source": JUDGE_GEOMETRY,
        "media": media,
        "gate2_ready": bool(gate2_ready),
    }
    errs = validate_wj_fluid_report(report)
    if errs:
        raise WjFluidReportError(
            "WJ 流体报告组装自校验失败：" + "; ".join(errs[:5]))
    return report


def load_wj_fluid_report(path: "str | Path") -> dict:
    """图接缝入口：缺件/坏 JSON/契约违规统一 WjFluidReportError。"""
    p = Path(path)
    if not p.is_file():
        raise WjFluidReportError(f"WJ 流体报告不存在：{p}")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WjFluidReportError(f"WJ 流体报告无法解析：{p}: {exc}") from exc
    errs = validate_wj_fluid_report(obj)
    if errs:
        raise WjFluidReportError(
            f"WJ 流体报告契约违规（fail-closed）：{p}: " + "; ".join(errs[:5]))
    return obj


# ---------------------------------------------------------------------------
# evaluate 接缝：wj_fluid_report 路径推导 + 两级装入（已有报表直接读；
# 只有 results.json+trajectory.npz 原始件时引擎现算组装并落盘）。入杯率/
# 守恒数字全部引擎计算（探针几何函数 + modality_probes 单点），不采信脚本
# 自报的任何衍生比率。
# ---------------------------------------------------------------------------

def _safe_media_rel(workdir: Path, rel: str, *, what: str) -> str:
    """C4 路径守卫：相对、无 ..、resolve 后仍在 workdir 内且文件存在。"""
    if not isinstance(rel, str) or not rel:
        raise WjFluidReportError(f"{what} 路径必须是非空字符串")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise WjFluidReportError(f"{what} 路径越界（绝对/..）：{rel}")
    target = workdir / p
    try:
        contained = target.resolve().is_relative_to(workdir.resolve())
    except OSError:
        contained = False
    if not contained or not target.is_file():
        raise WjFluidReportError(f"{what} 文件不存在或越界：{rel}")
    return p.as_posix()


def _assemble_wj_from_results(results_path: Path, workdir: Path, *,
                              thread_id: str, stage0_compat_ref: str) -> dict:
    """results.json + trajectory.npz → wj_fluid_report v1（引擎现算全部数字）。

    每个字段界与 validate_wj_fluid_report 同口径 fail-closed；入杯语义经
    探针 cup_entry_metrics（末沉降窗中位）、守恒经 modality_probes
    .fluid_conservation 三查；metrics_ref/trajectory_ref 钉两件的 sha256
    （数字可回指到字节）。窗内逐帧入杯比例另落 cup_entry_curve.csv
    （引擎生成证据件，0600）。
    """
    try:
        raw = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WjFluidReportError(
            f"results.json 无法解析：{results_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WjFluidReportError("results.json 顶层必须是对象")
    if raw.get("schema") != prompts.RESULTS_SCHEMA:
        raise WjFluidReportError(
            f"results.schema 必须是 {prompts.RESULTS_SCHEMA}")
    for derived in _FORBIDDEN_DERIVED:
        if derived in raw:
            raise WjFluidReportError(
                f"results.json 禁止自报衍生字段 {derived}"
                "（引擎从轨迹现算）")
    solver_api = raw.get("solver_api")
    if not isinstance(solver_api, str) or not solver_api.strip():
        raise WjFluidReportError("results.solver_api 必须是非空字符串")
    n_particles = raw.get("n_particles")
    if not (isinstance(n_particles, int) and not isinstance(n_particles, bool)
            and 1 <= n_particles <= PARTICLE_CAP):
        raise WjFluidReportError(
            f"results.n_particles 必须是 1..{PARTICLE_CAP} 整数"
            "（粒子硬天花板）")
    frame_dt = raw.get("frame_dt_s")
    if not (isinstance(frame_dt, (int, float)) and not isinstance(frame_dt, bool)
            and math.isfinite(float(frame_dt)) and float(frame_dt) > 0.0):
        raise WjFluidReportError("results.frame_dt_s 必须是正有限实数")
    frame_dt = float(frame_dt)
    cup_raw = raw.get("cup")
    if not isinstance(cup_raw, dict):
        raise WjFluidReportError("results.cup 必须是对象")
    try:
        cup = CupGeometry(inner_radius=float(cup_raw["inner_radius"]),
                          bottom_z=float(cup_raw["bottom_z"]),
                          rim_z=float(cup_raw["rim_z"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise WjFluidReportError(f"results.cup 几何非法：{exc}") from exc

    traj_path = workdir / TRAJECTORY_NAME
    if not traj_path.is_file():
        raise WjFluidReportError(
            f"{TRAJECTORY_NAME} 缺失（入杯语义必须从轨迹几何现算，不补不猜）")
    try:
        with np.load(traj_path, allow_pickle=False) as traj:
            if "positions" not in traj:
                raise WjFluidReportError(
                    f"{TRAJECTORY_NAME} 缺 positions 键")
            positions = np.asarray(traj["positions"])
    except WjFluidReportError:
        raise
    except (OSError, ValueError) as exc:
        raise WjFluidReportError(
            f"{TRAJECTORY_NAME} 无法加载：{exc}") from exc
    if positions.ndim != 3 or positions.shape[2] != 3:
        raise WjFluidReportError(
            f"positions 形状必须是 (F,N,3)，收到 {positions.shape}")
    if positions.shape[1] != n_particles:
        raise WjFluidReportError(
            f"positions N={positions.shape[1]} 与 results.n_particles="
            f"{n_particles} 不一致")
    if not (np.issubdtype(positions.dtype, np.number)
            and not np.issubdtype(positions.dtype, np.bool_)):
        raise WjFluidReportError(
            f"positions 必须是数值 dtype，收到 {positions.dtype}")
    if not bool(np.isfinite(positions).all()):
        raise WjFluidReportError("positions 含非有限坐标（NaN/Inf）")

    # manifest.json 交叉对账（存在即严查：视频/轨迹必须同一次运行产出）。
    manifest_path = workdir / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WjFluidReportError(
                f"manifest.json 无法解析：{exc}") from exc
        if not isinstance(man, dict):
            raise WjFluidReportError("manifest.json 顶层必须是对象")
        man_frames = man.get("n_frames")
        if not (isinstance(man_frames, int) and not isinstance(man_frames, bool)
                and man_frames == positions.shape[0]):
            raise WjFluidReportError(
                f"manifest.n_frames={man_frames} 与轨迹帧数 "
                f"{positions.shape[0]} 不一致（视频/轨迹必须同一次运行产出）")
        man_dt = man.get("frame_dt_s")
        if not (isinstance(man_dt, (int, float))
                and not isinstance(man_dt, bool)
                and math.isfinite(float(man_dt))
                and abs(float(man_dt) - frame_dt) <= 1e-12):
            raise WjFluidReportError(
                f"manifest.frame_dt_s={man_dt} 与 results.frame_dt_s="
                f"{frame_dt} 不一致")

    # 85% 语义：末沉降窗逐帧中位（探针单点）；算不出即 STOP（不补不猜）。
    try:
        m = cup_entry_metrics(positions, cup, frame_dt)
    except ValueError as exc:
        raise WjFluidReportError(f"入杯语义无法从几何算出：{exc}") from exc

    # 守恒三查：逐帧四分类计数 → modality_probes 流体守恒探针（输入纪律：
    # numpy 度量数组，非像素帧）。
    counts_t, boundary_t, cup_t = [], [], []
    for frame in positions:
        c = classify_particles(frame, cup)
        counts_t.append(float(c["n"]))
        boundary_t.append(float(c["out_of_bounds"]))
        cup_t.append(float(c["in_cup"]))
    cons = fluid_conservation(np.asarray(counts_t, dtype=np.float64),
                              np.asarray(boundary_t, dtype=np.float64),
                              np.asarray(cup_t, dtype=np.float64))

    # 引擎生成证据件：窗内逐帧入杯比例曲线（0600 原子写）。
    window_frames = int(m["window_frames"])
    offset = positions.shape[0] - window_frames
    csv_lines = ["frame,in_cup_ratio"]
    for i, ratio in enumerate(m["in_cup_ratios"]):
        csv_lines.append(f"{offset + i},{ratio:.6f}")
    curve_path = workdir / CURVE_CSV_NAME
    atomic_write_text(curve_path, "\n".join(csv_lines) + "\n")

    media: list[dict] = []
    seen: set[str] = set()

    def _add(kind: str, rel: str) -> str:
        norm = _safe_media_rel(workdir, rel, what=f"media({kind})")
        if norm not in seen:
            seen.add(norm)
            media.append({"kind": kind, "path": norm, "sha256": None})
        return norm

    for kind, rel in (("video", VIDEO_NAME), ("image", COVER_NAME),
                      ("log", RESULTS_NAME), ("log", TRAJECTORY_NAME),
                      ("log", CURVE_CSV_NAME)):
        if (workdir / rel).is_file():
            _add(kind, rel)
    if not any(md["kind"] == "video" for md in media):
        raise WjFluidReportError(
            f"{VIDEO_NAME} 缺失（Gate2 真人裁决媒体必写；rc0 修复轮已拦，"
            "此处仍 fail-closed）")

    metrics = {
        "entry_ratio_median": float(m["entry_ratio_median"]),
        "ratio_at_count_moment": float(m["ratio_at_count_moment"]),
        "count_moment_frame": int(m["count_moment_frame"]),
        "window_frames": window_frames,
        "splash_ratio_median": float(m["splash_ratio_median"]),
        "out_of_bounds_ratio_median": float(m["out_of_bounds_ratio_median"]),
        "target": float(CUP_ENTRY_TARGET),
        "meets_target": bool(m["entry_ratio_median"] >= CUP_ENTRY_TARGET),
        "particle_count": n_particles,
        "frame_dt_s": frame_dt,
    }
    results_sha = hashlib.sha256(results_path.read_bytes()).hexdigest()
    traj_sha = hashlib.sha256(traj_path.read_bytes()).hexdigest()
    return build_wj_fluid_report(
        thread_id=thread_id, solver_api=solver_api.strip(),
        cup={"inner_radius": float(cup.inner_radius),
             "bottom_z": float(cup.bottom_z),
             "rim_z": float(cup.rim_z)},
        metrics=metrics, conservation=cons,
        metrics_ref={"path": RESULTS_NAME,
                     "keys": ["solver_api", "n_particles", "frame_dt_s",
                              "cup"],
                     "sha256": results_sha},
        trajectory_ref={"path": TRAJECTORY_NAME, "sha256": traj_sha},
        media=media, stage0_compat_ref=stage0_compat_ref,
        gate2_ready=True)  # video 缺失已在上方 fail-closed


def _derive_wj_path(state: dict, rec: dict) -> "Path | None":
    """wj_fluid_report 路径三级推导：state 键 → rec 键 → results 同目录缺省名。

    state 键一旦给出（即使文件缺）即采用——缺件由 load_wj_fluid_report
    fail-closed 响亮报错，不静默降级改道。"""
    path = state.get(WJ_REPORT_STATE_KEY) or rec.get(WJ_REPORT_STATE_KEY)
    if isinstance(path, str) and path:
        return Path(path)
    tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if isinstance(tele, str) and tele:
        cand = Path(tele).parent / WJ_REPORT_NAME
        if cand.is_file():
            return cand
    return None


def make_evaluate_seam(thread_id: str) -> Callable[[dict], dict]:
    """evaluate 节点接缝（零网关/零视觉实呼）：不构造任何 Genesis 环境，
    只读产物并 fail-closed 校验。两级装入——
    ① 推导出的 wj_report.json 已存在（state/rec/results 同目录）→ 严格读；
    ② 只有 results.json+trajectory.npz 原始件 → 引擎组装
       （_assemble_wj_from_results）并原子落盘 0600 到 workdir/wj_report.json
       再读（节点可重放：重入时 ① 先命中）；
    ③ 两者皆无 → GraphStateError（不静默编报告）。
    gate2_ready=False 是合法人判输入（不抛），契约违规才抛。"""
    def seam(state: dict) -> dict:
        runs = state.get("agent_runs", [])
        rec = runs[-1] if runs else {}
        path = _derive_wj_path(state, rec)
        if path is None:
            tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
            if not isinstance(tele, str) or not tele:
                raise GraphStateError(
                    "task8_fluid evaluate：state 缺 wj_report_path "
                    "且无 telemetry_uri（无法定位产物）")
            workdir = Path(tele).parent
            stage0_ref = state.get(STAGE0_REPORT_STATE_KEY) or (
                (state.get("chosen") or {}).get(STAGE0_REPORT_STATE_KEY))
            if not isinstance(stage0_ref, str) or not stage0_ref:
                raise GraphStateError(
                    "task8_fluid evaluate：state 缺 stage0_report_path"
                    "（WJ stage0_compat_ref 溯源链要求）")
            try:
                report = _assemble_wj_from_results(
                    workdir / RESULTS_NAME, workdir, thread_id=thread_id,
                    stage0_compat_ref=stage0_ref)
            except WjFluidReportError as exc:
                raise GraphStateError(f"task8_fluid evaluate：{exc}") from exc
            out = workdir / WJ_REPORT_NAME
            atomic_write_text(
                out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            path = out
        try:
            report = load_wj_fluid_report(path)
        except WjFluidReportError as exc:
            raise GraphStateError(f"task8_fluid evaluate：{exc}") from exc
        m = report["metrics"]
        return {
            "ok": True,
            "thread_id": thread_id,
            "wj_report_path": str(path),
            "schema": report["schema"],
            "stage": report["stage"],
            "aggregate": {
                "cup_entry_rate": m["entry_ratio_median"],
                "target": m["target"],
                "meets_target": m["meets_target"],
                "conservation_pass": report["conservation"]["pass"],
                "splash_rate": m["splash_ratio_median"],
                "out_of_bounds_rate": m["out_of_bounds_ratio_median"],
            },
            "judge_sources_used": [report["judge_source"]],
            "gate2_ready": report["gate2_ready"],
            "provenance": f"file:{WJ_REPORT_STATE_KEY}",
        }

    return seam


# ---------------------------------------------------------------------------
# Gate1 卡接缝 + Gate2 三问组装（真人均由 feishu_gateway 阻塞等待；本模块
# 零假批准——D-5 降级时连卡都不发，直接 GraphStateError 结案）
# ---------------------------------------------------------------------------

def make_gate1_seam(feishu: Any, thread_id: str, task_title: str):
    """Gate1 接缝：Stage0 流体能力事实机判 + 倒水方案卡（真人批准才放行）。

    propose_run=False（D-5 降级）时**不发卡**、不生成运行案，直接
    GraphStateError 结案——绝不把降级伪装成运行案等一个「本不该存在的批准」。
    """
    def seam(state: dict) -> dict:
        proposal = gate1_proposal(state)
        if not proposal["propose_run"]:
            raise GraphStateError(
                f"Task8 流体 Stage0 both_fail（{DEGRADATION_D5} 降级）："
                "禁止提 Gate1 运行案（No Fake Approval）")
        return feishu.request_gate1_fluid_approval(
            thread_id, task_title, state["proposals"],
            stage0_facts=proposal)

    return seam


def build_gate2_explanation(state: dict,
                            rec: dict) -> "dict[str, str] | None":
    """Gate2 卡三问（fluid 变体，键必须是 task/problem/video 家法）。

    数字从已验证的 wj_fluid_report（evaluate 已落 state.metrics.wj_report_path）
    与 rec（duration/manifest）现读，不编引擎叙事——缺报表返回 None，卡片
    退回纯指标（同 task7 家法：不编故事）。
    """
    metrics = state.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    wj_path = metrics.get("wj_report_path")
    report = None
    if isinstance(wj_path, str) and wj_path:
        try:
            report = load_wj_fluid_report(wj_path)
        except WjFluidReportError:
            report = None

    chosen = state.get("chosen") or {}
    scheme = str(chosen.get("name") or "（未命名方案）").strip()
    task = (
        f"Gate1 批准方案：{scheme}\n"
        "本次是 Q5 厨房倒水流固耦合（Genesis 粒子流体）Stage1 实跑——"
        "入杯率/守恒数字全部由引擎从粒子轨迹几何现算。")
    lines = [
        "它要回答的不是「脚本有没有跑通」，而是「这次倒水实跑能否作为流体"
        "能力的可信证据」：",
        "· 85% 入杯口径：末 0.5s 沉降窗逐帧中位，严格几何 r<inner_radius 且 "
        "bottom_z<z<rim_z（贴壁/贴底/贴口不入杯），飞溅与越界分列；",
        "· 守恒机判：粒子总数恒定 / 边界溢出 ≤2% / 末帧入杯 ≥80%（探针三查）；",
        "· 数字可溯源：metrics_ref/trajectory_ref 回指 results.json 与 "
        "trajectory.npz（sha256）。",
    ]
    if report is not None:
        m = report["metrics"]
        cons = report["conservation"]
        cons_s = "通过" if cons.get("pass") else \
            f"违例 {len(cons.get('violations') or [])} 条"
        lines.append(
            f"本次聚合：入杯率中位数 {m['entry_ratio_median']:.3f}"
            f"（目标 {m['target']}，{'达成' if m['meets_target'] else '未达成'}）；"
            f"守恒机判{cons_s}；飞溅中位 {m['splash_ratio_median']:.3f}、"
            f"越界中位 {m['out_of_bounds_ratio_median']:.3f}"
            f"（solver={report.get('solver_api')}）。")
    problem = "\n".join(lines)

    parts: list[str] = []
    tele = rec.get("telemetry_uri") or state.get("telemetry_uri")
    if isinstance(tele, str) and tele:
        mp = Path(tele).parent / MANIFEST_NAME
        if mp.is_file():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                n_frames = loaded.get("n_frames") if isinstance(
                    loaded, dict) else None
                if isinstance(n_frames, int) and n_frames > 0:
                    parts.append(f"{n_frames} 帧")
            except (ValueError, OSError):
                pass
    dur_ms = rec.get("duration_ms")
    if isinstance(dur_ms, (int, float)) and not isinstance(dur_ms, bool) \
            and dur_ms > 0:
        parts.append(f"{dur_ms / 1000:.1f} s")
    length = ("本段约 " + "、".join(parts)) if parts else ""
    video = (
        "请重点看：倾倒→液体流动→入杯/飞溅全过程是否真实连续（无瞬移、无"
        "穿模、透明渲染可辨液面），末 0.5s 是否沉降稳定；视频与上方数字是否"
        "同一次运行产出。"
        + (f"（{length}）" if length else ""))
    return {"task": task, "problem": problem, "video": video}


# ---------------------------------------------------------------------------
# 证据包清单（白皮书仅 both_fail 路径；引擎不生成其内容）
# ---------------------------------------------------------------------------

def fluid_evidence_pack(*, stage0_report_id: str) -> list[dict]:
    """Task8 证据包清单：Stage0 探针报告 +（能力缺口路径）白皮书。

    白皮书 whitepaper_fluid_rigid_coupling.md 只在 Stage0 both_fail 时作为
    能力缺口报告入包，必须回链 Stage0 探针报告编号；本函数只列清单，引擎不
    生成白皮书内容（无 fake content），也不允许无报告编号入包。
    """
    if not isinstance(stage0_report_id, str) or not stage0_report_id.strip():
        raise GraphStateError(
            "Task8 证据包清单要求非空 stage0_report_id（白皮书必须回链探针编号）")
    return [
        {
            "name": STAGE0_REPORT_NAME,
            "kind": "stage0_capability_probe",
            "schema": STAGE0_REPORT_SCHEMA,
            "report_id": stage0_report_id,
            "required_outcomes": list(FLUID_OUTCOMES),
        },
        {
            "name": WHITEPAPER_NAME,
            "kind": "capability_gap_whitepaper",
            "stage0_fail_only": True,
            "source_report_id": stage0_report_id,
            "content_present": False,
            "note": "Stage0 both_fail 时由后续人工/分析流程撰写能力缺口白皮书；"
                    "引擎只登记清单，不生成内容",
        },
    ]
