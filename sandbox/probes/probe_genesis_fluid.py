#!/usr/bin/env python3
"""Phase 2 Task8 Stage0：Genesis 流体能力零付费探针（容器内、真 GPU 执行）。

科学背景（硬规则 7：先证能力存在，禁止拿 KB 文本 claim 充当运行证据）：
Phase 0/1 只实跑过 Genesis 刚体接触；粒子流体（不可压粒子/位置基流体/
物质点三类族）能否在 genesis-world 1.3.3 + A800 容器内真跑，只有 KB 文本
（quickstart 水杯 demo、ai_knowledge P34「稳定 dt≤4e-4」）没有运行证据。
本探针在 Gate1 之前、零方舟付费给出三种机判结局之一：

    sph_ok     —— 首选粒子流体族通过（N≤2000、10 帧坐标全 finite）；
    pbd_ok     —— 首选族不通但次选族（位置基/物质点）同口径通过；
    both_fail  —— 两族均崩/API 不存在/坐标非有限：fail-closed 落报告，
                  不进 Stage1（计划 Task8 D-5：STOP 报用户）。

反射纪律（不把求解器 API 名硬编码成断言）：
  * 求解器构造器一律运行时从 ``dir(materials)`` 与其命名空间成员的
    ``dir()`` 两层反射枚举，完整名单落报告 reflected_materials；
  * 族偏好序只由**反射结构**导出（命名空间内具体材料叶子数越少=越专用的
    纯粒子流体族，越优先；同票按反射名字典序），在 1.3.3 反射结构上恰为
    「纯流体族 → 位置基族 → 物质点族」；分支代码不出现求解器名字面量；
  * 材料「流体」角色判定同样结构化：叶子角色名/构造参数粘度片段/文档串
    液体词，任一命中即为候选；实际使用的反射路径名（如
    ``"<命名空间>.<叶子>"``）逐 attempt 落 api_used。

显存看门狗（常量单点集中于本文件顶部，orchestrator/graph/task8_fluid.py
按路径加载复用，禁止第二处魔数）：每 solver 子步采样
``torch.cuda.max_memory_allocated``，越过 VRAM_ABORT_GB 立即优雅中止、
回 FAILURE 结构化结果，**绝不试真实 OOM**；参数下调（粒子预算/子步）
**每候选（每族）最多 MAX_REDUCTIONS 次，族间计数独立**——回退族必须拿到
完整两次预算，不被首选族的下调耗尽（计划 L608 配置点与 L732 按族风险表的
统一口径；Task8 冷读 2026-10-24 锁内适配），逐次记 reductions。

85% 入杯率语义（Stage1 才用，但纯几何函数此刻落地并单测，免事后争论）：
计数窗为仿真末 0.5 s，计数时刻为窗内中位帧；入杯 = 粒子在杯内壁半径以内、
杯底以上、杯口以下（严格边界，杯几何参数计算，不用像素）；结果取窗内逐帧
入杯比例的中位数；飞溅/越界粒子单列。

宿主安全：genesis/torch 全部惰性导入（仅 worker/子函数内 import），宿主
无 genesis-world 也能收集本模块、单测其纯函数。
用法：probe_genesis_fluid.py --out <probe.json> [--n 1000]
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import multiprocessing as mp
import os
import platform
import sys
import time
import traceback
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, median_low
from typing import Any, Callable

import numpy as np

# ---------------------------------------------------------------------------
# 看门狗/能力常量——唯一单点（计划 Task8 看门狗配置点；图接缝按路径加载）
# ---------------------------------------------------------------------------

#: Stage1 实跑粒子数硬天花板（计划 L609：粒子上限 20000 常量，任何路径不得越过）。
PARTICLE_CAP = 20000
#: 显存中止线（GB）：越过即优雅 FAILURE，绝不试真实 OOM。
VRAM_ABORT_GB = 35.0
#: 每 solver 子步采样 max_memory_allocated 的总开关。
CUDA_OOM_GUARD = True
#: 起始值，Stage0 实测后回填。依据仅 KB 文本：quickstart 水杯 demo
#: 「每帧 28 个子步」、ai_knowledge P34 称粒子流体稳定步长 dt≤4e-4；
#: 本探针 solver dt 固定 4e-4、每帧自走 SUBSTEP_MAX 子步（≈1.12e-2 s/帧）。
SUBSTEP_MAX = 28
#: 看门狗触发后参数下调最多次数（先砍粒子预算，再砍子步）。
MAX_REDUCTIONS = 2
#: Stage0 最小场景粒子数上限（探针只证能力存在；20000 是 Stage1 天花板）。
STAGE0_N_MAX = 2000
#: Stage0 固定步进帧数（机判口径，不开放 CLI 漂移）。
STAGE0_STEPS = 10
#: solver 固定步长 4e-4 s（KB P34 文本声称的稳定上限；本探针正是要实证它）。
SOLVER_DT = 4e-4
#: 85% 入杯语义：仿真末沉降窗长度（秒）。
SETTLING_WINDOW_S = 0.5
#: 85% 入杯语义：Stage1 预注册目标（仅登记，Stage0 不生产此结论）。
CUP_ENTRY_TARGET = 0.85
#: Stage0 粒子预算缺省值（箱形 morph 体积/粒径近似给 N，实际 N 建场后读回）。
DEFAULT_PARTICLE_BUDGET = 1000
#: 参数下调后粒子预算地板（再小不构成有意义流体场景）。
MIN_PARTICLE_BUDGET = 125

REPORT_SCHEMA = "simpraxis.task8.stage0_fluid.v1"
OUTCOME_FIRST_FAMILY = "sph_ok"
OUTCOME_SECOND_FAMILY = "pbd_ok"
OUTCOME_BOTH_FAIL = "both_fail"
OUTCOMES = (OUTCOME_FIRST_FAMILY, OUTCOME_SECOND_FAMILY, OUTCOME_BOTH_FAIL)

#: 退出码：0 能力证实；3 显存看门狗终态（下调两次仍越线）；4 两路皆败；2 用法错。
RC_OK = 0
RC_USAGE = 2
RC_VRAM_ABORT = 3
RC_BOTH_FAIL = 4

#: 子进程（每候选独立进程，避 KB P31「跨场景状态污染」）最长等待秒数。
WORKER_TIMEOUT_S = 600

#: 流体角色叶子名（角色词，非求解器名；真实 1.3.3 三族均以该名暴露液体材料）。
_FLUID_LEAF_TOKEN = "Liquid"
#: 构造参数粘度片段（重命名叶子的兜底结构特征）。
_VISCOSITY_FRAGMENT = "viscos"
#: 粒子实体位置读取方法探测序（wheel 1.3.3 实证，冷读 A 项）：ParticleEntity
#: 级公共 API get_particles_pos 三族皆有；SPHEntity 另包一层 get_position；
#: 全仓不存在 get_pos，保留为最末防御性回退。顺序即优先级，禁止按族分支。
POSITION_ACCESSORS = ("get_particles_pos", "get_position", "get_pos")
#: 三类方法皆无（或 worker 在位置读取前即崩）时落证的诚实占位值（非空字符串，
#: 满足报告 fail-closed 校验：accessor 字段必须显式交代实际用名/不可用）。
POSITION_ACCESSOR_UNAVAILABLE = "unavailable"
_GIB = float(1 << 30)
_MIB = float(1 << 20)


# ---------------------------------------------------------------------------
# CLI：粒子上限的 argparse 单点闸门
# ---------------------------------------------------------------------------

def bounded_particle_count(raw: str) -> int:
    """argparse 类型闸：粒子预算必须为 1..PARTICLE_CAP 整数，越界直接拒绝。

    单一硬天花板在 PARTICLE_CAP（20000）；Stage0 另有 STAGE0_N_MAX 最小场景
    策略闸（validate_stage0_budget），两层分工：CLI 类型层不允许任何配置越过
    硬天花板，main 层不允许 Stage0 越过最小场景上限。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"粒子数必须是整数，收到 {raw!r}")
    if n < 1 or n > PARTICLE_CAP:
        raise argparse.ArgumentTypeError(
            f"粒子数 {n} 越界：允许 1..{PARTICLE_CAP}（硬天花板常量，不得越过）")
    return n


def validate_stage0_budget(n: int) -> int:
    """Stage0 策略闸：最小场景粒子预算不得超过 STAGE0_N_MAX。"""
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"Stage0 粒子预算必须是正整数，收到 {n!r}")
    if n > STAGE0_N_MAX:
        raise ValueError(
            f"Stage0 最小场景粒子预算 {n} > {STAGE0_N_MAX}：Stage0 只证能力，"
            "20000 天花板属于 Stage1（计划 Task8）")
    return n


def positive_float(raw: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"必须是浮点值，收到 {raw!r}")
    if not math.isfinite(v) or v <= 0.0:
        raise argparse.ArgumentTypeError(f"必须是正有限值，收到 {raw!r}")
    return v


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Phase2 Task8 Stage0 Genesis 流体能力探针（容器内真 GPU）")
    ap.add_argument("--out", required=True, help="探针 JSON 报告输出路径")
    ap.add_argument("--n", type=bounded_particle_count,
                    default=DEFAULT_PARTICLE_BUDGET,
                    help=f"粒子预算 1..{PARTICLE_CAP}（Stage0 ≤{STAGE0_N_MAX}）")
    ap.add_argument("--particle-size", type=positive_float, default=0.01,
                    help="粒子直径（米），默认 0.01")
    return ap


# ---------------------------------------------------------------------------
# 反射：求解器候选完全从 dir() 枚举（分支不含求解器名字面量）
# ---------------------------------------------------------------------------

def _public_names(obj: Any) -> list[str]:
    """dir() 反射的公开名（dunder/下划线内部件不入证）。"""
    return [n for n in dir(obj) if not n.startswith("_")]


def _is_namespace(member: Any) -> bool:
    """材料命名空间：模块，或自身含公开嵌套类属性的类。

    真实 1.3.3 反射结构里粒子流体族都是子模块（含 Base 与若干具体材料），
    刚体/运动学等是直接材料类（无公开嵌套类），据此结构区分而非按名认领。
    """
    if inspect.ismodule(member):
        return True
    if not inspect.isclass(member):
        return False
    for name in _public_names(member):
        try:
            attr = getattr(member, name)
        except Exception:  # noqa: BLE001 —— 反射只取证，属性描述符异常不致命
            continue
        if inspect.isclass(attr):
            return True
    return False


def reflect_material_names(materials_module: Any) -> dict:
    """两层反射：顶层 dir() 名单 + 每个命名空间一层 dir() 名单。

    返回 {"top": [...], "nested": {ns: [...]}}，完整落报告；候选发现只允许
    消费这份运行时名单，禁止另写字面分支。
    """
    top = sorted(_public_names(materials_module))
    nested: dict[str, list[str]] = {}
    for name in top:
        try:
            member = getattr(materials_module, name)
        except Exception:  # noqa: BLE001
            continue
        if _is_namespace(member):
            nested[name] = sorted(_public_names(member))
    return {"top": top, "nested": nested}


def _is_fluid_constructor(cls: Any) -> bool:
    """叶子类是否承担「液体」角色——纯结构判定，不认领求解器名。

    三条任一：叶子角色名命中液体角色词；构造参数名含粘度片段；类文档串含
    液体词。真实 1.3.3 三族液体叶子均命中第一条；无相互作用的自由粒子
    （仅 rho/采样参数）三条都不命中，正确排除。
    """
    if not inspect.isclass(cls):
        return False
    if cls.__name__ == _FLUID_LEAF_TOKEN:
        return True
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError):
        sig = None
    if sig is not None and any(
            _VISCOSITY_FRAGMENT in p.lower() for p in sig.parameters):
        return True
    doc = inspect.getdoc(cls) or ""
    return "liquid" in doc.lower()


@dataclass(frozen=True)
class FluidCandidate:
    """反射发现的一个液体构造器候选。"""
    family_index: int          # 0=首选族，1=次选族
    namespace: str
    leaf: str
    constructor: Callable[..., Any]

    @property
    def api_path(self) -> str:
        """报告用反射路径名（运行时拼接，非字面分支）。"""
        return f"{self.namespace}.{self.leaf}"

    def as_wire(self) -> dict:
        """跨进程序列化形态（构造器不跨进程，子进程内重新反射定位）。"""
        return {"path": [self.namespace, self.leaf],
                "family": self.family_index, "api_path": self.api_path}


def discover_fluid_candidates(
        materials_module: Any) -> tuple[list[FluidCandidate], dict]:
    """从反射名单发现液体候选并排族偏好序。

    偏好规则全部来自反射结构本身：命名空间内具体类叶子越少，越是只做粒子
    流体的专用族，越优先；同票按反射名字典序确定。真实 1.3.3 反射结构下
    该规则给出「纯流体族 → 位置基族（4 叶子）→ 物质点族（6 叶子）」的顺序；
    若实际安装版本反射结构不同，报告如实显示 api_used，禁止按名声称。
    """
    listing = reflect_material_names(materials_module)
    picked: dict[str, tuple[str, type]] = {}
    leaf_counts: dict[str, int] = {}
    for ns_name, leaves in listing["nested"].items():
        ns_obj = getattr(materials_module, ns_name)
        concrete = 0
        fluid_leaf: tuple[str, type] | None = None
        for leaf_name in leaves:
            cls = getattr(ns_obj, leaf_name, None)
            if not inspect.isclass(cls):
                continue
            if cls.__name__ != "Base":
                concrete += 1
            if _is_fluid_constructor(cls) and fluid_leaf is None:
                fluid_leaf = (leaf_name, cls)
        leaf_counts[ns_name] = concrete
        if fluid_leaf is not None:
            picked[ns_name] = fluid_leaf
    ordered_ns = sorted(picked, key=lambda n: (leaf_counts[n], n))
    candidates = [
        FluidCandidate(0 if i == 0 else 1, ns_name,
                       picked[ns_name][0], picked[ns_name][1])
        for i, ns_name in enumerate(ordered_ns)]
    return candidates, listing


def outcome_for_family(family_index: int) -> str:
    """族序 → 计划三结局枚举（0=首选族，其余=次选族）。"""
    return (OUTCOME_FIRST_FAMILY if family_index == 0
            else OUTCOME_SECOND_FAMILY)


# ---------------------------------------------------------------------------
# 纯几何/纯判定：finite 坐标闸、VRAM 看门狗、参数下调
# ---------------------------------------------------------------------------

def all_coords_finite(positions: np.ndarray) -> bool:
    """粒子坐标全 finite 闸：形状必须 (N>=1, 3) 且无 NaN/Inf，否则不放行。

    不发明任何「运动幅度」阈值（Stage0 只证数值健康；非退化运动签名不是
    Stage0 机判条件）。
    """
    try:
        arr = np.asarray(positions, dtype=np.float64)
    except (TypeError, ValueError):
        return False
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] == 0:
        return False
    return bool(np.isfinite(arr).all())


@dataclass(frozen=True)
class WatchdogVerdict:
    abort: bool
    reason: str


def vram_watchdog(peak_gb: float, *, abort_gb: float = VRAM_ABORT_GB,
                  guard: bool = CUDA_OOM_GUARD) -> WatchdogVerdict:
    """显存看门狗纯判定：只给「优雅中止与否」，绝不抛 OOM/不执行任何分配。

    采样不可解析/非有限/负值也判中止（仪器失效按 fail-closed 处理）。
    """
    if not guard:
        return WatchdogVerdict(False, "guard_off")
    try:
        peak = float(peak_gb)
    except (TypeError, ValueError):
        return WatchdogVerdict(True, f"显存采样不可解析：{peak_gb!r}")
    if not math.isfinite(peak):
        return WatchdogVerdict(True, f"显存采样非有限：{peak_gb!r}")
    if peak < 0.0:
        return WatchdogVerdict(True, f"显存采样为负：{peak_gb!r}")
    if peak > abort_gb:
        return WatchdogVerdict(
            True, f"显存 {peak:.3f}GB 超过中止线 {abort_gb:g}GB")
    return WatchdogVerdict(False, "ok")


def reduced_attempt_params(used: int, n_budget: int,
                           substeps: int) -> tuple[int, int]:
    """看门狗越界后的参数下调（每候选/每族最多 MAX_REDUCTIONS 次，族间独立）。

    第 1 次：粒子预算减半（显存主要随粒子数增长），子步不动；
    第 2 次：粒子预算再减半，子步减半（稳定性代价，如实落报告）。
    used = 本候选已下调次数；used>=MAX_REDUCTIONS 时调用方不得再调
    （ValueError）。计数器由 main 在进入每个候选时重置（冷读 B 项）。
    """
    if (not isinstance(used, int) or isinstance(used, bool) or used < 0):
        raise ValueError(f"已下调次数必须是非负整数，收到 {used!r}")
    if used >= MAX_REDUCTIONS:
        raise ValueError(
            f"参数下调已达上限 {MAX_REDUCTIONS} 次：禁止继续下调/试 OOM")
    if not isinstance(n_budget, int) or isinstance(n_budget, bool) \
            or n_budget < 1:
        raise ValueError(f"粒子预算必须是正整数，收到 {n_budget!r}")
    if not isinstance(substeps, int) or isinstance(substeps, bool) \
            or substeps < 1:
        raise ValueError(f"子步数必须是正整数，收到 {substeps!r}")
    # 调用方（main）始终传「当前」预算：第一次 1000→500，第二次 500→250，
    # 即原预算四分一；与 docstring「再减半」同口径，禁止此处再 //4 变八分一。
    new_n = max(MIN_PARTICLE_BUDGET, n_budget // 2)
    if used == 0:
        return new_n, substeps
    return new_n, max(1, substeps // 2)


def budget_box_size(n_budget: int, particle_size: float) -> float:
    """粒子预算 → 箱形 morph 边长：每边格子数 ≈ N^(1/3)，边长=格数×粒径。

    实际粒子数由求解器采样器决定，建场后读回 n_particles 并按
    STAGE0_N_MAX fail-closed，不相信预算即实际值。
    """
    if n_budget < 1 or particle_size <= 0.0:
        raise ValueError("粒子预算与粒径必须为正")
    cells = max(1, int(round(n_budget ** (1.0 / 3.0))))
    return cells * particle_size


def vram_abort_attempt(api_path: str, *, n_particles: int, steps: int,
                       reductions: int, peak_gb: float,
                       reason: str, completed_steps: int = 0,
                       position_accessor: str = POSITION_ACCESSOR_UNAVAILABLE) -> dict:
    """看门狗越界的优雅 FAILURE attempt 件（结构化、JSON 可序列化，不抛 OOM）。"""
    return {
        "api_used": str(api_path),
        "n_particles": int(n_particles),
        "steps": int(steps),
        "completed_steps": int(completed_steps),
        "finite_coords": False,
        "passed": False,
        "vram_abort": True,
        "vram_peak_gb": round(float(peak_gb), 6),
        "reductions": int(reductions),
        "position_accessor": str(position_accessor),
        "failure_verbatim": str(reason),
    }


# ---------------------------------------------------------------------------
# 85% 入杯率纯几何语义（Stage1 使用，Stage0 落地并单测）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CupGeometry:
    """杯几何（米）：内壁圆柱半径 inner_radius、杯底/杯口高度。"""
    inner_radius: float
    bottom_z: float
    rim_z: float

    def __post_init__(self) -> None:
        for name, v in (("inner_radius", self.inner_radius),
                        ("bottom_z", self.bottom_z), ("rim_z", self.rim_z)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(float(v)):
                raise ValueError(f"杯几何 {name} 必须是有限数，收到 {v!r}")
        if self.inner_radius <= 0.0:
            raise ValueError("inner_radius 必须为正")
        if self.rim_z <= self.bottom_z:
            raise ValueError("rim_z 必须高于 bottom_z")


def classify_particles(positions: np.ndarray, cup: CupGeometry) -> dict:
    """逐粒子四分类（严格边界）：

    - in_cup：r < inner_radius（严格）且 z > bottom_z（严格）且 z < rim_z（严格）；
    - splash（飞溅）：z >= rim_z（达/越杯口，含恰在杯口边界——入口严格故不计入）；
    - below_bottom（杯底以下/贴底）：z <= bottom_z；
    - outside_wall（侧壁外）：bottom_z < z < rim_z 且 r >= inner_radius
      （恰在壁面：入口严格，归越界）。

    四类穷尽且互斥；out_of_bounds = below_bottom + outside_wall，与 splash
    分列（计划：飞溅/越界粒子单列）。纯坐标计算，无任何像素/渲染量。
    """
    arr = np.asarray(positions, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] == 0:
        raise ValueError(f"粒子坐标形状必须是 (N>=1,3)，收到 {arr.shape}")
    r = np.sqrt(arr[:, 0] ** 2 + arr[:, 1] ** 2)
    z = arr[:, 2]
    splash = z >= cup.rim_z
    below = z <= cup.bottom_z
    mid = (~splash) & (~below)
    in_cup = mid & (r < cup.inner_radius)
    outside = mid & (r >= cup.inner_radius)
    n = int(arr.shape[0])
    counts = {
        "n": n,
        "in_cup": int(np.count_nonzero(in_cup)),
        "splash": int(np.count_nonzero(splash)),
        "below_bottom": int(np.count_nonzero(below)),
        "outside_wall": int(np.count_nonzero(outside)),
    }
    parts = (counts["in_cup"] + counts["splash"]
             + counts["below_bottom"] + counts["outside_wall"])
    if parts != n:
        raise AssertionError(f"四分类不穷尽：{parts} != {n}")
    counts["out_of_bounds"] = counts["below_bottom"] + counts["outside_wall"]
    counts["in_cup_ratio"] = counts["in_cup"] / n
    return counts


def settling_window_frame_count(frame_dt: float,
                                window_s: float = SETTLING_WINDOW_S) -> int:
    """沉降窗帧数 = round(window_s/frame_dt)；非法入参 fail-closed。"""
    frame_dt = float(frame_dt)
    window_s = float(window_s)
    if not (math.isfinite(frame_dt) and math.isfinite(window_s)) \
            or frame_dt <= 0.0 or window_s <= 0.0:
        raise ValueError(
            f"frame_dt/window_s 必须为正有限值，收到 {frame_dt},{window_s}")
    count = int(round(window_s / frame_dt))
    if count < 1:
        raise ValueError(
            f"frame_dt={frame_dt} 对沉降窗 {window_s}s 连一帧都放不下，"
            "入杯率语义无法从几何算出（计划 STOP 条件）")
    return count


def cup_entry_metrics(positions: np.ndarray, cup: CupGeometry,
                      frame_dt: float, *,
                      window_s: float = SETTLING_WINDOW_S) -> dict:
    """末 0.5s 沉降窗入杯语义（纯几何，零像素）。

    入参 positions 形状 (F, N, 3)（整条或末段轨迹均可，函数只取末窗）。
    返回窗内逐帧入杯比例序列、中位计数帧（窗内中位下标）该帧比例，以及
    各比例中位数（结果口径）。帧数不足以覆盖整窗 → ValueError（计划：
    入杯率任一字段无法从几何算出即 STOP，不补不猜）。
    """
    arr = np.asarray(positions, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"位置轨迹形状必须是 (F,N,3)，收到 {arr.shape}")
    total_frames = arr.shape[0]
    need = settling_window_frame_count(frame_dt, window_s)
    if total_frames < need:
        raise ValueError(
            f"轨迹只有 {total_frames} 帧，放不下沉降窗 {need} 帧"
            f"（{window_s}s @ dt={frame_dt}s）")
    window = arr[-need:]
    per_frame = [classify_particles(frame, cup) for frame in window]
    ratios = [c["in_cup_ratio"] for c in per_frame]
    moment_idx = median_low(range(need))  # 窗内中位帧（计数时刻）
    return {
        "window_frames": need,
        "count_moment_frame": int(moment_idx),
        "ratio_at_count_moment": float(ratios[moment_idx]),
        "entry_ratio_median": float(median(ratios)),
        "in_cup_ratios": [float(r) for r in ratios],
        "splash_ratio_median": float(
            median([c["splash"] / c["n"] for c in per_frame])),
        "out_of_bounds_ratio_median": float(
            median([c["out_of_bounds"] / c["n"] for c in per_frame])),
    }


# ---------------------------------------------------------------------------
# 子进程 worker（genesis/torch 惰性导入；一进程一候选，避跨场景污染）
# ---------------------------------------------------------------------------

def _resolve_position_accessor(entity: Any) -> str:
    """反射定位粒子实体实际可用的位置读取方法名（wheel 1.3.3 实证优先级）。

    冷读 A 项：PBD/MPM 的 ParticleEntity 只有 ``get_particles_pos``（基类
    line 555；PBD line 56、MPM line 367），SPH 的 get_position 内部转调它，
    全仓无 get_pos。若只试 get_position/get_pos，回退族在 build+step 全健康
    后仍必败 → 假 both_fail 架空 D-5。三者皆无才 raise（原文如实落证）。
    """
    for name in POSITION_ACCESSORS:
        if callable(getattr(entity, name, None)):
            return name
    raise RuntimeError(
        "粒子实体无 get_particles_pos/get_position/get_pos（反射实况落证）")


def _entity_positions(entity: Any) -> tuple[np.ndarray, str]:
    """读粒子实体位置，返回 (坐标 (N,3) numpy, 实际访问方法名)。

    方法名由 ``_resolve_position_accessor`` 反射决定（不按族分支）；单环境
    场景这些 getter 内部已剥 env 轴，仍保留 ndim==3 → arr[0] 的防御。
    """
    name = _resolve_position_accessor(entity)
    pos = getattr(entity, name)()
    if hasattr(pos, "detach"):
        pos = pos.detach().cpu().numpy()
    arr = np.asarray(pos, dtype=np.float64)
    if arr.ndim == 3:
        arr = arr[0]
    return arr, name


def _build_solver_options(gs: Any, ns_name: str,
                         particle_size: float) -> Any | None:
    """按命名空间反射名拼 solver options 类（<名>Options），不存在则 None。

    紧边界（0.6m³ 工作区）随 particle_size 一起传入（签名有 lower_bound 才传），
    避开默认 ±100m 空间哈希网格的无谓显存；全部由反射签名决定，不按名硬写。
    """
    opts_cls = getattr(gs.options, f"{ns_name}Options", None)
    if opts_cls is None or not inspect.isclass(opts_cls):
        return None
    kwargs: dict[str, Any] = {"particle_size": float(particle_size)}
    try:
        params = inspect.signature(opts_cls).parameters
    except (TypeError, ValueError):
        params = {}
    if "lower_bound" in params:
        kwargs["lower_bound"] = (-0.3, -0.3, 0.0)
        kwargs["upper_bound"] = (0.3, 0.3, 0.8)
    return opts_cls(**kwargs)


def _reflect_worker(payload: dict) -> dict:
    """子进程：init 真 CUDA + 反射材料名单与候选（失败由 guard 转结构化错误）。"""
    import torch
    import genesis as gs  # 惰性：宿主无 genesis 时本函数不被调用即可收集

    gs.init(backend=gs.gpu, seed=0)
    backend_name = str(getattr(gs, "backend", "?"))
    if "cuda" not in backend_name.lower():
        raise RuntimeError(
            f"Genesis 未取得 CUDA 后端（{backend_name}）：A800 沙盒不允许 CPU 回退")
    candidates, listing = discover_fluid_candidates(gs.materials)
    return {
        "backend": backend_name,
        "gpu_name": torch.cuda.get_device_name(0),
        "genesis_version": getattr(gs, "__version__", "?"),
        "torch_version": torch.__version__,
        "reflected": listing,
        "candidates": [c.as_wire() for c in candidates],
    }


def _attempt_worker(payload: dict) -> dict:
    """子进程：对一个反射候选建最小场景，步进并做 finite/显存机判。"""
    import torch
    import genesis as gs

    gs.init(backend=gs.gpu, seed=0)
    backend_name = str(getattr(gs, "backend", "?"))
    if "cuda" not in backend_name.lower():
        raise RuntimeError(
            f"Genesis 未取得 CUDA 后端（{backend_name}）：A800 沙盒不允许 CPU 回退")

    ns_name, leaf_name = payload["path"]
    candidates, _ = discover_fluid_candidates(gs.materials)
    match = next((c for c in candidates
                  if c.namespace == ns_name and c.leaf == leaf_name), None)
    if match is None:
        raise RuntimeError(
            f"反射候选 {ns_name}.{leaf_name} 在 worker 内重新反射时消失")

    torch.cuda.reset_peak_memory_stats()
    baseline_mib = torch.cuda.max_memory_allocated() / _MIB
    scene_kwargs: dict[str, Any] = {
        "sim_options": gs.options.SimOptions(dt=SOLVER_DT, substeps=1),
        "show_viewer": False,
    }
    opts = _build_solver_options(gs, ns_name, payload["particle_size"])
    if opts is not None:
        # 键名由反射命名空间名小写化运行时拼接（sph/pbd/mpm 同构）。
        scene_kwargs[f"{ns_name.lower()}_options"] = opts
    scene = gs.Scene(**scene_kwargs)
    scene.add_entity(gs.morphs.Plane())
    box = float(payload["box_size"])
    liquid = scene.add_entity(
        material=match.constructor(),
        morph=gs.morphs.Box(pos=(0.0, 0.0, 0.4), size=(box, box, box)))
    scene.build()

    n_particles = int(getattr(liquid, "n_particles", 0))
    if n_particles <= 0:
        raise RuntimeError("建场后 n_particles<=0：采样器未产出粒子（拒绝假能力）")
    if n_particles > STAGE0_N_MAX:
        raise RuntimeError(
            f"实际粒子数 {n_particles} > Stage0 上限 {STAGE0_N_MAX}："
            "最小场景被采样器突破，fail-closed")

    # 位置访问器在建场后、步进前反射定位（冷读 A 项）：三族 API 不同，
    # 先证可读再花步进预算；此处 raise 由 _worker_guard 转结构化失败。
    position_accessor = _resolve_position_accessor(liquid)

    steps = int(payload["steps"])
    substeps = int(payload["substeps"])
    vram_peak_gb = 0.0
    finite_coords = True
    completed_steps = 0
    for _frame in range(steps):
        for _sub in range(substeps):
            scene.step()
            peak_gb = torch.cuda.max_memory_allocated() / _GIB
            vram_peak_gb = max(vram_peak_gb, peak_gb)
            verdict = vram_watchdog(peak_gb)
            if verdict.abort:
                return {
                    "passed": False,
                    "vram_abort": True,
                    "api_used": match.api_path,
                    "n_particles": n_particles,
                    "steps": steps,
                    "completed_steps": completed_steps,
                    "finite_coords": finite_coords,
                    "position_accessor": position_accessor,
                    "vram_peak_gb": round(vram_peak_gb, 6),
                    "baseline_mib": round(baseline_mib, 3),
                    "failure_verbatim": verdict.reason,
                }
        pos, _used_accessor = _entity_positions(liquid)
        if not all_coords_finite(pos):
            finite_coords = False
        completed_steps += 1

    return {
        "passed": bool(finite_coords),
        "vram_abort": False,
        "api_used": match.api_path,
        "n_particles": n_particles,
        "steps": steps,
        "completed_steps": completed_steps,
        "finite_coords": bool(finite_coords),
        "position_accessor": position_accessor,
        "vram_peak_gb": round(vram_peak_gb, 6),
        "baseline_mib": round(baseline_mib, 3),
        "failure_verbatim": None if finite_coords else "粒子坐标出现 NaN/Inf",
    }


def _worker_guard(target: Callable[[dict], dict], conn: Any,
                  payload: dict) -> None:
    """子进程兜底：任何异常（含 interpreter 级错误文本）都结构化回传父进程。"""
    try:
        conn.send(target(payload))
    except BaseException as exc:  # noqa: BLE001 —— 探针不许子进程裸崩带走信息
        conn.send({"_worker_error": True,
                   "error_type": type(exc).__name__,
                   "traceback": traceback.format_exc(limit=10)})
    finally:
        conn.close()


def _spawn_call(target: Callable[[dict], dict], payload: dict,
                timeout_s: int = WORKER_TIMEOUT_S) -> dict:
    """spawn 子进程执行 target；超时/非零退出/segfault 全部转结构化失败。"""
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker_guard,
                       args=(target, child, payload))
    proc.start()
    child.close()
    result: dict | None = None
    if parent.poll(timeout_s):
        try:
            result = parent.recv()
        except EOFError:
            result = None
    else:
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
        result = {"_worker_error": True,
                  "error_type": "WorkerTimeout",
                  "traceback": f"子进程超过 {timeout_s}s 已被 terminate/kill"}
    parent.close()
    proc.join()
    if result is None:
        result = {"_worker_error": True,
                  "error_type": "AbnormalExit",
                  "traceback": (f"子进程无回传且退出码 {proc.exitcode}"
                                "（可能 segfault；不声称能力）")}
    return result


def _error_attempt(api_path: str, *, steps: int, reductions: int,
                   result: dict) -> dict:
    """worker 失败/异常 → attempt 记录（vram_abort 已自带结构的原样保留关键字段）。"""
    if result.get("vram_abort"):
        row = dict(result)
        row.setdefault("reductions", reductions)
        row.setdefault("steps", steps)
        row.setdefault("position_accessor", POSITION_ACCESSOR_UNAVAILABLE)
        return row
    if result.get("_worker_error"):
        verbatim = (f"{result.get('error_type', 'Error')}: "
                    f"{result.get('traceback', '')}")[-4000:]
    else:
        verbatim = str(result.get("failure_verbatim", result))[-4000:]
    return {
        "api_used": str(api_path),
        "n_particles": int(result.get("n_particles", 0) or 0),
        "steps": int(steps),
        "completed_steps": int(result.get("completed_steps", 0) or 0),
        "finite_coords": bool(result.get("finite_coords", False)),
        "passed": False,
        "vram_abort": False,
        "vram_peak_gb": float(result.get("vram_peak_gb", 0.0) or 0.0),
        "baseline_mib": float(result.get("baseline_mib", 0.0) or 0.0),
        "reductions": int(reductions),
        "position_accessor": str(
            result.get("position_accessor") or POSITION_ACCESSOR_UNAVAILABLE),
        "failure_verbatim": verbatim,
    }


def _normalise_attempt(row: dict, *, reductions: int, steps: int) -> dict:
    """成功/结构化 attempt 的统一键序与缺省补齐。"""
    out = {
        "api_used": str(row["api_used"]),
        "n_particles": int(row.get("n_particles", 0)),
        "steps": int(row.get("steps", steps)),
        "completed_steps": int(row.get("completed_steps", 0)),
        "finite_coords": bool(row.get("finite_coords", False)),
        "passed": bool(row.get("passed", False)),
        "vram_abort": bool(row.get("vram_abort", False)),
        "vram_peak_gb": float(row.get("vram_peak_gb", 0.0)),
        "baseline_mib": float(row.get("baseline_mib", 0.0)),
        "reductions": int(row.get("reductions", reductions)),
        "position_accessor": str(
            row.get("position_accessor") or POSITION_ACCESSOR_UNAVAILABLE),
        "failure_verbatim": row.get("failure_verbatim"),
    }
    return out


# ---------------------------------------------------------------------------
# 报告装配与主流程
# ---------------------------------------------------------------------------

def assemble_report(*, ok: bool, outcome: str, reflected: dict,
                    attempts: list[dict], gpu: dict, versions: dict,
                    wall_s: float, note: str | None = None) -> dict:
    """报告纯装配（schema simpraxis.task8.stage0_fluid.v1；both_fail 也良构）。"""
    if outcome not in OUTCOMES:
        raise ValueError(f"非法 outcome：{outcome!r}")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "ok": bool(ok),
        "outcome": outcome,
        "reflected_materials": reflected,
        "attempts": attempts,
        "gpu": gpu,
        "versions": versions,
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wall_s": round(float(wall_s), 3),
    }
    if note is not None:
        report["note"] = note
    return report


def _write_report(path: str, report: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    # 对齐 common.fsutil 原子写 0600 单点纪律（冷读 D 项；目录权限沿用 mkdir，
    # 容器 1000:1000 runs 区不强制改目录）。
    os.chmod(out, 0o600)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    budget = validate_stage0_budget(args.n)
    t0 = time.time()

    versions = {"python": platform.python_version(),
                "genesis": None, "torch": None}
    gpu = {"name": "unknown", "baseline_mib": 0.0, "peak_mib": 0.0}
    reflected: dict = {"top": [], "nested": {}}

    # 1) 反射子进程：真 CUDA init + dir() 名单
    reflect_res = _spawn_call(_reflect_worker, {})
    if reflect_res.get("_worker_error") or "candidates" not in reflect_res:
        report = assemble_report(
            ok=False, outcome=OUTCOME_BOTH_FAIL, reflected=reflected,
            attempts=[], gpu=gpu, versions=versions,
            wall_s=time.time() - t0,
            note="反射/init 阶段即失败："
                 + str(reflect_res.get("traceback", reflect_res))[:3500])
        _write_report(args.out, report)
        print("GENESIS_FLUID_PROBE_BOTH_FAIL",
              json.dumps({"outcome": OUTCOME_BOTH_FAIL}, ensure_ascii=False))
        return RC_BOTH_FAIL

    reflected = reflect_res["reflected"]
    versions["genesis"] = reflect_res.get("genesis_version")
    versions["torch"] = reflect_res.get("torch_version")
    gpu["name"] = reflect_res.get("gpu_name", "unknown")
    candidates = reflect_res["candidates"]

    attempts: list[dict] = []
    passed_wire: dict | None = None
    terminal_vram_abort = False

    # 2) 按反射族序逐候选；每候选（每族）看门狗越界最多下调 MAX_REDUCTIONS 次。
    # 锁内适配（Task8 冷读，计划 L608/L732 口径统一为族级，2026-10-24）：
    # 下调计数在进入每个候选时重置，回退族拿完整两次预算，不被首选族耗尽
    # （否则回退族被饿死，结局偏向 both_fail，架空 D-5 族间回退）；
    # spawn 总数有界 = 候选数 ×（MAX_REDUCTIONS+1），1.3.3 三族即 3×3=9。
    for wire in candidates:
        reductions_used = 0
        n_budget = budget
        substeps = SUBSTEP_MAX
        while True:
            box_size = budget_box_size(n_budget, args.particle_size)
            result = _spawn_call(_attempt_worker, {
                "path": wire["path"],
                "steps": STAGE0_STEPS,
                "substeps": substeps,
                "particle_size": args.particle_size,
                "box_size": box_size,
            })
            row = _error_attempt(
                wire["api_path"], steps=STAGE0_STEPS,
                reductions=reductions_used, result=result) \
                if (result.get("_worker_error")
                    or not result.get("passed", False)) \
                else _normalise_attempt(result, reductions=reductions_used,
                                        steps=STAGE0_STEPS)
            attempts.append(row)
            if row["passed"]:
                passed_wire = wire
                break
            if row["vram_abort"] and reductions_used < MAX_REDUCTIONS:
                # 本族仍有下调预算：减半后重跑（注意不置终态——下调后若通过，
                # 终态标记必须保持 False；终态只在耗尽两次仍越线时置真）。
                n_budget, substeps = reduced_attempt_params(
                    reductions_used, n_budget, substeps)
                reductions_used += 1
                continue
            if row["vram_abort"]:
                # 本族两次下调已耗尽仍越线：看门狗终态（rc 3 口径保留）。
                terminal_vram_abort = True
            break
        if passed_wire is not None:
            break

    peak_mib = max(
        (float(a.get("vram_peak_gb", 0.0)) * 1024.0 for a in attempts),
        default=0.0)
    base_mib = max((float(a.get("baseline_mib", 0.0)) for a in attempts),
                   default=0.0)
    gpu["baseline_mib"] = round(base_mib, 3)
    gpu["peak_mib"] = round(peak_mib, 3)

    if passed_wire is not None:
        outcome = outcome_for_family(passed_wire["family"])
        report = assemble_report(
            ok=True, outcome=outcome, reflected=reflected, attempts=attempts,
            gpu=gpu, versions=versions, wall_s=time.time() - t0)
        _write_report(args.out, report)
        print("GENESIS_FLUID_PROBE_OK",
              json.dumps({"outcome": outcome,
                          "api_used": attempts[-1]["api_used"]},
                         ensure_ascii=False))
        return RC_OK

    report = assemble_report(
        ok=False, outcome=OUTCOME_BOTH_FAIL, reflected=reflected,
        attempts=attempts, gpu=gpu, versions=versions,
        wall_s=time.time() - t0,
        note=("全部反射候选均未通过；fail-closed，不进 Stage1"
              + "（含看门狗终态）" if terminal_vram_abort else
              "全部反射候选均未通过；fail-closed，不进 Stage1"))
    _write_report(args.out, report)
    print("GENESIS_FLUID_PROBE_BOTH_FAIL",
          json.dumps({"outcome": OUTCOME_BOTH_FAIL,
                      "terminal_vram_abort": terminal_vram_abort},
                     ensure_ascii=False))
    return RC_VRAM_ABORT if terminal_vram_abort else RC_BOTH_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
