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
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from orchestrator.common.runtime_paths import phase2_runs_root
from orchestrator.graph.nodes import GraphStateError
from orchestrator.graph.task8 import VENV_PY
from orchestrator.solutions import resolve_solution

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
