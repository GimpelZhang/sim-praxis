"""RSI-R1 预算 lane 的**单一真值**（Plan §8.1；零 import，避免包间循环）。

本模块是 Plan §8.1 那张表的唯一代码化版本。``orchestrator.gateway.quota_ledger``
（账本命名空间）与 ``orchestrator.rsi_r1.contracts``（BudgetSpec）都从这里取数，
**不允许**任何一方再写一份字面量表——AC02 的机判就是比对这几处派生值。

计划原文（§8.1）：
    probe 8 / practice 16 / study_A 48 / study_B 48 / repair_reserve 24
    合计 144 = 正常 120 + 条件储备 24；槽不转移。

lane 在账本里以整数 task id 记账（沿用 RSI-R0 家族 ``task_caps`` 的
``Mapping[int, int]`` 形态）：
    0=probe 1=practice 2=study_A 3=study_B 4=repair_reserve
"""

from __future__ import annotations

import types
from typing import Mapping

#: lane 有序名（顺序即 Plan §8.1 表序；机判按此序渲染公开文本与槽表）。
LANE_ORDER: tuple[str, ...] = (
    "probe",
    "practice",
    "study_A",
    "study_B",
    "repair_reserve",
)

#: lane → 账本 task id（唯一映射；反查见 ``LANE_BY_TASK``）。
LANE_TASK_ID: Mapping[str, int] = types.MappingProxyType({
    "probe": 0,
    "practice": 1,
    "study_A": 2,
    "study_B": 3,
    "repair_reserve": 4,
})

#: task id → lane 名（由 LANE_TASK_ID 反推，不另写字面量）。
LANE_BY_TASK: Mapping[int, str] = types.MappingProxyType({
    task: lane for lane, task in LANE_TASK_ID.items()
})

#: lane → POST 帽（Plan §8.1 表内值，逐字）。
LANE_POST_CAPS: Mapping[str, int] = types.MappingProxyType({
    "probe": 8,
    "practice": 16,
    "study_A": 48,
    "study_B": 48,
    "repair_reserve": 24,
})

#: 整轮硬帽（独立字面量，**不是** sum(LANE_POST_CAPS) 的运行时计算——
#: 与 PHASE26/27/281/RSI-R0/R0.1/R0.2 同一先例：任一处单方面改动都不该
#: 悄悄让另一处跟着变；一致性由 AC02 机判显式比对）。
RSI_R1_HARD_CAP = 144

#: 正常预算（不含条件储备）：8+16+48+48 = 120。同样是独立字面量。
RSI_R1_NORMAL_BUDGET = 120

#: 账本 task_caps：task id → 帽（由两块表合成，不重复字面量）。
RSI_R1_TASK_CAPS: Mapping[int, int] = types.MappingProxyType({
    LANE_TASK_ID[lane]: LANE_POST_CAPS[lane] for lane in LANE_ORDER
})

# ---- 计算资源上限（Plan §8.2）----
#: 每 study 探索 runs 上限。
MAX_EXPLORE_RUNS_PER_STUDY = 64
#: 每 study 确认 runs 上限（≤6 条件 × 32 block）。
MAX_CONFIRM_RUNS_PER_STUDY = 192
#: 每 study 独立复跑 runs 上限。
MAX_REPLAY_RUNS_PER_STUDY = 192
#: 两 study 最多 runs（Plan §8.2「两题最多 896runs」）。
MAX_TOTAL_RUNS = 896
#: 每 run 步数上限。
MAX_STEPS_PER_RUN = 8000
#: 两 study 物理步数上限（896 × 8000）。
MAX_STUDY_STEPS = MAX_TOTAL_RUNS * MAX_STEPS_PER_RUN
#: 校准/探针/练习物理步数上限。
MAX_AUX_STEPS = 2_000_000
#: 整轮物理步数总上限。
MAX_TOTAL_STEPS = 10_000_000
#: 每 study 候选设计/分析程序沙盒执行次数上限。
MAX_SANDBOX_EXECUTIONS_PER_STUDY = 32
#: 每 study 活跃墙钟秒上限（2h）。
MAX_ACTIVE_WALLCLOCK_S = 2 * 60 * 60
#: 整轮容器累计秒上限（12h）。
MAX_CONTAINER_WALLCLOCK_S = 12 * 60 * 60
#: GPU 分配累计秒上限（4h）。
MAX_GPU_WALLCLOCK_S = 4 * 60 * 60


def task_caps_sum() -> int:
    """lane 帽之和（机判用；必须恰等于 ``RSI_R1_HARD_CAP``）。"""
    return sum(LANE_POST_CAPS.values())


def assert_lane_table_consistent() -> None:
    """自洽检查：名字集合/帽和/硬帽/正常预算/步数不变量。

    AC02 机判与 T0 自检共用；任何一项不成立即 fail-fast，不静默修正。
    """
    if tuple(LANE_POST_CAPS) != LANE_ORDER:
        raise AssertionError(
            f"lane 表顺序与 LANE_ORDER 不一致：{tuple(LANE_POST_CAPS)} != {LANE_ORDER}")
    if sorted(LANE_TASK_ID.values()) != list(range(len(LANE_ORDER))):
        raise AssertionError(
            f"task id 必须是 0..{len(LANE_ORDER) - 1} 的连续整数：{dict(LANE_TASK_ID)}")
    total = task_caps_sum()
    if total != RSI_R1_HARD_CAP:
        raise AssertionError(
            f"lane 帽之和 {total} != 硬帽 {RSI_R1_HARD_CAP}（Plan §8.1 要求 144）")
    if RSI_R1_HARD_CAP - LANE_POST_CAPS["repair_reserve"] != RSI_R1_NORMAL_BUDGET:
        raise AssertionError(
            f"正常预算 {RSI_R1_NORMAL_BUDGET} != 硬帽 - 储备 "
            f"{RSI_R1_HARD_CAP - LANE_POST_CAPS['repair_reserve']}")
    if dict(RSI_R1_TASK_CAPS) != {
            LANE_TASK_ID[lane]: LANE_POST_CAPS[lane] for lane in LANE_ORDER}:
        raise AssertionError("RSI_R1_TASK_CAPS 与 lane 表不一致")
    if MAX_TOTAL_RUNS != 2 * (MAX_EXPLORE_RUNS_PER_STUDY
                              + MAX_CONFIRM_RUNS_PER_STUDY
                              + MAX_REPLAY_RUNS_PER_STUDY):
        # 64+192+192 = 448；两题 = 896（Plan §8.2 末句）。
        raise AssertionError(
            f"MAX_TOTAL_RUNS {MAX_TOTAL_RUNS} != 2×(64+192+192)")
    if MAX_STUDY_STEPS + MAX_AUX_STEPS > MAX_TOTAL_STEPS:
        raise AssertionError(
            f"物理步数上限自相矛盾：{MAX_STUDY_STEPS}+{MAX_AUX_STEPS}"
            f" > {MAX_TOTAL_STEPS}")
