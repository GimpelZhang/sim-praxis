"""RSI 版本化 campaign profile：预算 schema / 协议策略 / 资源上限 / 费用 scope 的单一真值。

RSI-R1.2 Plan §1 的落地模块。两条不可变 profile：

* ``rsi-r1-legacy``：既有两个评价轮使用的旧合同（lane 8/16/48/48/24=144、
  逐字首错即停、judge v1、账本期号 2903、家族根 ``rsi_r1``）。**逐字节不变**，
  旧 golden/旧 LOCK 解析继续走它；
* ``rsi-r1.2``：新 profile（lane 12/16/48/48=124、无 repair_reserve、有界自修、
  公开资源合同 v2、judge v1.2、账本期号 2904、家族根 ``rsi_r12``、
  跨轮研究族硬帽 155=历史 31+本轮 124）。

设计约束（Plan §1）：

* profile 是**不可变数据**，不是可运行时改的开关；``resolve_profile`` 只接受
  已登记 id，未知 id 拒；
* 历史 envelope 无 ``profile_id`` 字段时**只**解析为 legacy，绝不默升 R1.2；
* 新 profile 的所有资源数值从本模块读出（渲染/校验/最终请求审计共用同一对象），
  不允许任何一个消费者再写一份字面量；
* 本模块零第三方 import、不触网、不读写任何路径，避免与 contracts/ledger 循环。

数值来源：``~/文档/RSI_R1.2_Plan_detailed.md`` §1.1（预算/费用）、§2.1（资源）、
§3.1（协议/恢复）、§4（aux 子信封）。
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator.rsi_r1.lanes import (
    LANE_ORDER as LEGACY_LANE_ORDER,
    LANE_POST_CAPS as LEGACY_LANE_POST_CAPS,
    LANE_TASK_ID as LEGACY_LANE_TASK_ID,
    RSI_R1_HARD_CAP as LEGACY_HARD_CAP,
    RSI_R1_NORMAL_BUDGET as LEGACY_NORMAL_BUDGET,
    RSI_R1_TASK_CAPS as LEGACY_TASK_CAPS,
)

# ---------------------------------------------------------------------------
# profile id / schema id
# ---------------------------------------------------------------------------

PROFILE_LEGACY = "rsi-r1-legacy"
PROFILE_R12 = "rsi-r1.2"
PROFILES: "tuple[str, ...]" = (PROFILE_LEGACY, PROFILE_R12)

SCHEMA_PREFIX = "simpraxis.rsi_r1"
SCHEMA_VERSION = 1

BUDGET_SCHEMA_V1 = f"{SCHEMA_PREFIX}.budget.v1"
BUDGET_SCHEMA_V2 = "simpraxis.rsi_r12.budget.v1"
CONSENT_SCHEMA_V1 = f"{SCHEMA_PREFIX}.consent.v1"
CONSENT_SCHEMA_V2 = "simpraxis.rsi_r12.consent.v1"
ENVELOPE_SCHEMA_V1 = f"{SCHEMA_PREFIX}.envelope.v1"
ENVELOPE_SCHEMA_V2 = "simpraxis.rsi_r12.envelope.v1"
PUBLIC_CONTRACT_SCHEMA_V2 = "simpraxis.rsi_r12.public_contract.v2"
PROFILE_SCHEMA = "simpraxis.rsi_r12.profile.v1"

# ---------------------------------------------------------------------------
# R1.2 lane 表（Plan §1.1；**独立字面量**，不复用 legacy 表）
# ---------------------------------------------------------------------------

LANE_ORDER_R12: "tuple[str, ...]" = ("probe", "practice", "study_A", "study_B")
LANE_TASK_ID_R12: "Mapping[str, int]" = types.MappingProxyType({
    "probe": 0,
    "practice": 1,
    "study_A": 2,
    "study_B": 3,
})
LANE_POST_CAPS_R12: "Mapping[str, int]" = types.MappingProxyType({
    "probe": 12,
    "practice": 16,
    "study_A": 48,
    "study_B": 48,
})
RSI_R12_HARD_CAP = 124
RSI_R12_NORMAL_BUDGET = 124  # 无 repair_reserve；正常预算 == 硬帽
RSI_R12_TASK_CAPS: "Mapping[int, int]" = types.MappingProxyType({
    LANE_TASK_ID_R12[lane]: LANE_POST_CAPS_R12[lane] for lane in LANE_ORDER_R12
})

#: 跨轮研究族硬帽：历史 31（R1 9 + R1.1 22，两独立 envelope 的最终根实呼）
#: + 已关闭轮实花 5（`r12-paid-20261113a`，仪器缺陷轮，花掉的不归还）
#: + 本轮 124 = **160**。历史 31 的逐字节导入在 ``family.py``。
#:
#: 锁内适配（2026-11-13，用户逐字裁决）：原帽 155 = 31 + 124，上一轮以
#: INVALID_INSTRUMENT 结案时已真实花掉 5 呼；若仍按 155 开新轮，31+124=155
#: 会把已花的 5 呼算成「不存在」。用户裁决把帽抬到 160 使
#: 「历史 + 已关闭轮实花 + 新轮」恰好闭合（Plan 明写「改变 155 需新用户裁决」）。
FAMILY_CAP = 160
FAMILY_HISTORY_POSTS = 31
#: 已关闭轮的实花（只能由族文档的 closed 成员逐行复算，不得手填）。
FAMILY_CLOSED_ROUND_POSTS = 5
#: 历史族帽取值（旧 envelope/consent 仍须可读；新轮一律用当前 ``FAMILY_CAP``）。
FAMILY_CAP_HISTORY: "tuple[int, ...]" = (155, FAMILY_CAP)
FAMILY_NEW_ROUND_POSTS = RSI_R12_HARD_CAP

#: 历史来源的**两本最终根账**（Plan §0 基线；行数/哈希在 family.py 复核）。
HISTORY_SOURCES: "tuple[Mapping[str, Any], ...]" = (
    types.MappingProxyType({
        "envelope_id": "r1_20261111",
        "root": "/mnt/artifacts/simpraxis/rsi_r1_repair1/r1_20261111",
        "ledger_relpath": "quota/ledger.jsonl",
        "ledger_sha256":
            "5ee8f1acefcde2168957bf13dff1b48bad564d31206ae787da19362326e72ffa",
        "rows_sha256":
            "181d07727c5171b8d37235a251062acc58c0601aa3329040cc28d14faff22ca1",
        "call_rows": 9,
        "ok_rows": 9,
        "fail_rows": 0,
        "seq_range": [1, 9],
    }),
    types.MappingProxyType({
        "envelope_id": "r1b_20261111",
        "root": "/mnt/artifacts/simpraxis/rsi_r1_repair2/r1b_20261111",
        "ledger_relpath": "quota/ledger.jsonl",
        "ledger_sha256":
            "53a703a771d53da5e636bb2f64077996c003d6d67a6d00bc7b0d26d53d4f8a78",
        "rows_sha256":
            "1b85bf92dd9cad0ea8a65a574821d1c878a55bbdee998d46f63e56e019678d19",
        "call_rows": 22,
        "ok_rows": 21,
        "fail_rows": 1,
        "seq_range": [1, 22],
    }),
)

# ---------------------------------------------------------------------------
# 计算资源上限（Plan §1.1 末段；legacy 数值逐字保留在 lanes.py）
# ---------------------------------------------------------------------------

MAX_EXPLORE_RUNS_PER_STUDY = 64
#: R1.2 新增：探索每 study 至多 8 个 batch，单 batch ≤ 当前累计剩余，
#: 累计 ≤ 64（相对旧「8batch×8run」的显式协议资源分配变更，随 Plan 审批）。
MAX_EXPLORE_BATCHES_PER_STUDY = 8
MAX_CONFIRM_RUNS_PER_STUDY = 192
MAX_CONFIRM_BLOCKS = 32
MAX_CONFIRM_CONDITIONS = 6
MAX_REPLAY_RUNS_PER_STUDY = 192
MAX_TOTAL_RUNS = 896
MAX_STEPS_PER_RUN = 8000
MAX_STUDY_STEPS = MAX_TOTAL_RUNS * MAX_STEPS_PER_RUN      # 7,168,000
#: A/B 仪器实测成本下限 2,048,000（见上条注释）> Plan 的 2,000,000，故 aux
#: 与整轮总帽随子信封之和上抬（锁内适配，2026-11-12 T5）。
MAX_AUX_STEPS = 5_520_000     # = AUX_SUBENVELOPE_TOTAL（见下条注释）
MAX_TOTAL_STEPS = 12_688_000
MAX_SANDBOX_EXECUTIONS_PER_STUDY = 32
MAX_ACTIVE_WALLCLOCK_S = 2 * 60 * 60
MAX_CONTAINER_WALLCLOCK_S = 12 * 60 * 60
MAX_GPU_WALLCLOCK_S = 4 * 60 * 60
REQUEST_TIMEOUT_S = 120
ANALYSIS_TIMEOUT_S = 30
PHYSICS_RUN_TIMEOUT_S = 60
#: 冻结确认后的保留余量门（Plan §2.1/§3.1）：扣当次 POST 后仍需 ≥8（paid
#: study）与 ≥2（probe/practice），不得借 lane 余额。
CONFIRM_POST_MARGIN = 8
PROTOCOL_POST_MARGIN = 2
MAX_USD_LITERAL_DECIMAL = "100"

#: 动态 SHA 引用字段（可见状态 / 最终请求审计共用的单一清单）。
DYNAMIC_REF_FIELDS: "tuple[str, ...]" = (
    "plan_sha256", "analysis_source_sha256", "confirm_manifest_sha256", "nonce")
#: 引用关系名（references.*）：值必须是当前有效动态 SHA 之一。
REFERENCE_NAMES: "tuple[str, ...]" = (
    "plan", "design", "analysis", "confirm_manifest", "nonce")

#: aux 五子信封（Plan §4 表；逐 spec 求和恰等于 ``AUX_SUBENVELOPE_TOTAL``，
#: 不要求耗完）。
#:
#: 锁内适配（2026-11-12 T5 演练实测，见 ``.omc/notepads/rsi_r12/t5.md``）：
#: Plan L148 把 A/B 仪器成本写成 ``3×2000×32×2 + 8000×32×2 = 896000``，其中
#: A 的 2000 step 假定 dt=0.002；但冻结的 R1 仪器把 A 的积分步长写死在宿主侧
#: （``experiments.A_FIXED_DT_S = 0.0005``，A 场景公开参数里根本没有 dt，候选
#: 不可覆盖）⇒ A 每 run 恒 4s/0.0005 = **8000 step**，32 block × 3 条件 = 96
#: run/pass，确认+全量 replay 两趟 = **1,536,000 step**；B 按 Plan 登记的三条件
#: (dt=.002/.002/.001) 每 block 8000 step，两趟 = 512,000。合计 2,048,000，
#: 已超过 Plan 的 aux 全额 2,000,000，故「修订离线子分配、保持 aux 总帽」在
#: 实测成本下不可行。这里改的是**计算预算**（非科学阈值/断言），按用户对 Plan
#: 的授权做最小上调并留痕：ab_instrument 子信封 = 实测 2,048,000 + 112,000
#: 余量 = 2,160,000；aux 总帽同步到五子信封之和 3,264,000；整轮总帽
#: 7,168,000 + 3,264,000 = 10,432,000。不得据此推断 A 的物理步长可改。
#:
#: 第二笔上调（同一 T5 演练，第三次整链实测）：``offline_probe_practice_recovery``
#: 在 Plan 表里只有 192,000 step —— 恰好等于「一条 toy 链一次 32×3 确认」
#: （96 run × 2000 step）的**单趟**成本。但冻结合同要求 freeze 的确认集恰 32
#: block（``CONFIRM_BLOCKS=32``），而 T5 固定套要求 probe 一条 + practice 两条
#: （A 与 B）共三次 toy 确认，每次都是 96 run；toy A 每 run 8000 step
#: （同上条 A 步长固定），toy B 每 run 8000 step，另各带一次探索批 48,000 step。
#: 实测合计 3 × (48,000 + 768,000) = 2,448,000（toy 链的 replay 预留从不
#: 执行——离线套的独立 replay 记在 study lane 自己的账上——故不计入物理成本，
#: 见 ``resources.ResourceLedger._toy_steps``）。故该子信封 =
#: 2,448,000，aux 总帽 = 3,264,000 − 192,000 + 2,448,000 = 5,520,000，
#: 整轮总帽 = 7,168,000 + 5,520,000 = 12,688,000。同前：改的是计算预算，
#: **不动**任何科学阈值、确认 block 数、run 帽或断言；toy 链的配额按 lane
#: 分域（``resources._rows_for``），不与 study 科学配额互相挤占。
AUX_SUBENVELOPES: "Mapping[str, int]" = types.MappingProxyType({
    "ab_instrument_confirm_replay": 2_160_000,
    "v2_calibration": 224_000,
    "ab_explore_boundary_fault": 240_000,
    "offline_probe_practice_recovery": 2_448_000,
    "t6_real_probe_practice": 448_000,
})
AUX_SUBENVELOPE_TOTAL = 5_520_000


def task_caps_sum_r12() -> int:
    return sum(LANE_POST_CAPS_R12.values())


def assert_r12_table_consistent() -> None:
    """R1.2 数值自洽（T0 自检 + AC04/AC57 机判共用；不成立即 fail-fast）。"""
    if tuple(LANE_POST_CAPS_R12) != LANE_ORDER_R12:
        raise AssertionError(
            f"R1.2 lane 表顺序与 LANE_ORDER_R12 不一致：{tuple(LANE_POST_CAPS_R12)}")
    if sorted(LANE_TASK_ID_R12.values()) != list(range(len(LANE_ORDER_R12))):
        raise AssertionError(
            f"R1.2 task id 必须是 0..{len(LANE_ORDER_R12) - 1}：{dict(LANE_TASK_ID_R12)}")
    if task_caps_sum_r12() != RSI_R12_HARD_CAP:
        raise AssertionError(
            f"R1.2 lane 帽之和 {task_caps_sum_r12()} != 硬帽 {RSI_R12_HARD_CAP}")
    if RSI_R12_HARD_CAP != RSI_R12_NORMAL_BUDGET:
        raise AssertionError("R1.2 无 repair_reserve，正常预算必须等于硬帽 124")
    if "repair_reserve" in LANE_POST_CAPS_R12:
        raise AssertionError("R1.2 不得登记 repair_reserve（付费后无本轮修复入口）")
    if FAMILY_CAP != (FAMILY_HISTORY_POSTS + FAMILY_CLOSED_ROUND_POSTS
                      + RSI_R12_HARD_CAP):
        raise AssertionError(
            f"族帽 {FAMILY_CAP} != 历史 {FAMILY_HISTORY_POSTS} + 已关闭轮 "
            f"{FAMILY_CLOSED_ROUND_POSTS} + 本轮 {RSI_R12_HARD_CAP}")
    if sum(int(s["call_rows"]) for s in HISTORY_SOURCES) != FAMILY_HISTORY_POSTS:
        raise AssertionError("历史来源行数和 != 31")
    if sum(AUX_SUBENVELOPES.values()) != AUX_SUBENVELOPE_TOTAL:
        raise AssertionError(
            f"aux 子信封合计 {sum(AUX_SUBENVELOPES.values())} != {AUX_SUBENVELOPE_TOTAL}")
    if AUX_SUBENVELOPE_TOTAL != MAX_AUX_STEPS:
        raise AssertionError("aux 总帽与子信封合计不一致")
    if MAX_STUDY_STEPS + MAX_AUX_STEPS > MAX_TOTAL_STEPS:
        raise AssertionError("R1.2 物理步数上限自相矛盾")
    if MAX_TOTAL_RUNS != 2 * (MAX_EXPLORE_RUNS_PER_STUDY
                              + MAX_CONFIRM_RUNS_PER_STUDY
                              + MAX_REPLAY_RUNS_PER_STUDY):
        raise AssertionError("MAX_TOTAL_RUNS 与 2×(64+192+192) 不一致")
    if MAX_CONFIRM_RUNS_PER_STUDY > MAX_CONFIRM_BLOCKS * MAX_CONFIRM_CONDITIONS:
        raise AssertionError("确认 run 帽超过 32 block × 6 条件的组合上限")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResourceLimits:
    """R1.2 资源帽（typed 对象；渲染与校验都从这里读，不再复制字面量）。"""

    explore_runs_per_study: int = MAX_EXPLORE_RUNS_PER_STUDY
    explore_batches_per_study: int = MAX_EXPLORE_BATCHES_PER_STUDY
    confirm_runs_per_study: int = MAX_CONFIRM_RUNS_PER_STUDY
    confirm_blocks: int = MAX_CONFIRM_BLOCKS
    confirm_max_conditions: int = MAX_CONFIRM_CONDITIONS
    replay_runs_per_study: int = MAX_REPLAY_RUNS_PER_STUDY
    total_runs: int = MAX_TOTAL_RUNS
    steps_per_run: int = MAX_STEPS_PER_RUN
    study_steps: int = MAX_STUDY_STEPS
    aux_steps: int = MAX_AUX_STEPS
    total_steps: int = MAX_TOTAL_STEPS
    sandbox_executions_per_study: int = MAX_SANDBOX_EXECUTIONS_PER_STUDY
    active_wallclock_s: int = MAX_ACTIVE_WALLCLOCK_S
    container_wallclock_s: int = MAX_CONTAINER_WALLCLOCK_S
    gpu_wallclock_s: int = MAX_GPU_WALLCLOCK_S
    request_timeout_s: int = REQUEST_TIMEOUT_S
    analysis_timeout_s: int = ANALYSIS_TIMEOUT_S
    physics_run_timeout_s: int = PHYSICS_RUN_TIMEOUT_S
    confirm_post_margin: int = CONFIRM_POST_MARGIN
    protocol_post_margin: int = PROTOCOL_POST_MARGIN

    def to_dict(self) -> "dict[str, Any]":
        return {
            "explore_runs_per_study": self.explore_runs_per_study,
            "explore_batches_per_study": self.explore_batches_per_study,
            "confirm_runs_per_study": self.confirm_runs_per_study,
            "confirm_blocks": self.confirm_blocks,
            "confirm_max_conditions": self.confirm_max_conditions,
            "replay_runs_per_study": self.replay_runs_per_study,
            "total_runs": self.total_runs,
            "steps_per_run": self.steps_per_run,
            "study_steps": self.study_steps,
            "aux_steps": self.aux_steps,
            "total_steps": self.total_steps,
            "sandbox_executions_per_study": self.sandbox_executions_per_study,
            "active_wallclock_s": self.active_wallclock_s,
            "container_wallclock_s": self.container_wallclock_s,
            "gpu_wallclock_s": self.gpu_wallclock_s,
            "request_timeout_s": self.request_timeout_s,
            "analysis_timeout_s": self.analysis_timeout_s,
            "physics_run_timeout_s": self.physics_run_timeout_s,
            "confirm_post_margin": self.confirm_post_margin,
            "protocol_post_margin": self.protocol_post_margin,
        }


#: 八个规范动作（两条 profile 共用；R1.2 的 practice 只跑一条玩具链）。
CHAIN_ACTIONS: "tuple[str, ...]" = (
    "write_artifact", "propose_plan", "run_batch", "write_artifact",
    "analyze", "freeze_confirm", "run_confirm", "submit_claim",
)

#: 规范动作名（Plan §3.1 八位置；重复 write_artifact 用 action+slot 区分）。
CANONICAL_POSITIONS: "tuple[str, ...]" = (
    "write_design", "propose_plan", "run_batch", "write_analysis",
    "analyze", "freeze_confirm", "run_confirm", "submit_claim",
)

#: R1.2 可恢复错误白名单（Plan §3.1「可恢复」；**只在 T0 注册、运行中不可扩**）。
RECOVERABLE_ERROR_CODES: "tuple[str, ...]" = (
    "MALFORMED_JSON",          # 已知前执行 schema/JSON 错误
    "UNKNOWN_KEY",
    "MISSING_KEY",
    "WRONG_TYPE",
    "NAN_OR_INF",
    "BOOL_AS_NUMBER",
    "PATH_TRAVERSAL",          # 设计/分析程序里被拒绝的路径，非越权访问
    "PLAN_MISMATCH",           # 公开引用不匹配（plan/analysis source）
    "SOURCE_SHA_INVALID",
    "RESOURCE_EXCEEDED",       # 矩阵资源拒绝（前执行校验，side_effect=NONE）
    "CONDITION_UNKNOWN",
    "SPEC_TAMPERED",
    "CONFIRM_BLOCK_COUNT",
    "MAX_CONDITIONS",
    "STEP_LIMIT",
    "TICK_NONDIVISIBLE",
    "DESIGN_NOT_SEPARABLE",
    "HYPOTHESIS_INSUFFICIENT",
    "DISTRIBUTION",
    "DISTRIBUTION_DEGENERATE",
    "CANDIDATE_PROCESS_EXIT",  # 可安全归因的候选设计/分析退出非 0（确认冻结前）
    "ARTIFACT_MALFORMED",
)

#: R1.2 不可恢复/终态（Plan §3.1「不可恢复」；命中即整轮停，不重发、不重跑）。
TERMINAL_ERROR_CODES: "tuple[str, ...]" = (
    "PATH_ESCAPE",             # 越权/路径逃逸/私有信息访问
    "PRIVATE_ACCESS",
    "SEND_UNKNOWN",            # 发送未知
    "TOOL_INTENT_UNKNOWN",     # 工具执行未知
    "LOCK_DRIFT",
    "METHOD_CHANGE_AFTER_CONFIRM",
    "INSTRUMENT_FAILURE",      # 宿主实现异常/观测不可信
    "BUDGET_EXHAUSTED",
    "TRANSPORT_EMPTY_RESPONSE",
    "TRANSPORT_TIMEOUT",
)


@dataclass(frozen=True)
class ProtocolPolicy:
    """协议策略：完成判据、恢复面、余量门。

    ``completion_is_cursor_and_cap`` 为 True 时 PASS 不再要求
    ``posts == wanted_posts``（旧 profile 为 False，逐字保留首错即停/等值判据）。
    """

    mode: str                       # "strict" | "bounded_recovery"
    single_practice_chain: bool
    completion_is_cursor_and_cap: bool
    max_extra_posts_probe: int
    max_extra_posts_practice: int
    recoverable_codes: "tuple[str, ...]"
    terminal_codes: "tuple[str, ...]"
    confirm_post_margin: int
    protocol_post_margin: int

    def to_dict(self) -> "dict[str, Any]":
        return {
            "mode": self.mode,
            "single_practice_chain": self.single_practice_chain,
            "completion_is_cursor_and_cap": self.completion_is_cursor_and_cap,
            "max_extra_posts_probe": self.max_extra_posts_probe,
            "max_extra_posts_practice": self.max_extra_posts_practice,
            "recoverable_codes": list(self.recoverable_codes),
            "terminal_codes": list(self.terminal_codes),
            "confirm_post_margin": self.confirm_post_margin,
            "protocol_post_margin": self.protocol_post_margin,
        }


#: legacy 策略：逐字首错即停（Plan v1.1 §11.2），PASS 要求 posts==wanted。
LEGACY_POLICY = ProtocolPolicy(
    mode="strict",
    single_practice_chain=False,
    completion_is_cursor_and_cap=False,
    max_extra_posts_probe=0,
    max_extra_posts_practice=0,
    recoverable_codes=(),
    terminal_codes=(),
    confirm_post_margin=CONFIRM_POST_MARGIN,
    protocol_post_margin=PROTOCOL_POST_MARGIN,
)

R12_POLICY = ProtocolPolicy(
    mode="bounded_recovery",
    single_practice_chain=True,
    completion_is_cursor_and_cap=True,
    max_extra_posts_probe=LANE_POST_CAPS_R12["probe"] - 8,
    max_extra_posts_practice=LANE_POST_CAPS_R12["practice"] - 8,
    recoverable_codes=RECOVERABLE_ERROR_CODES,
    terminal_codes=TERMINAL_ERROR_CODES,
    confirm_post_margin=CONFIRM_POST_MARGIN,
    protocol_post_margin=PROTOCOL_POST_MARGIN,
)


@dataclass(frozen=True)
class CampaignProfile:
    """不可变 campaign profile：一次选定，贯穿入口/解析/预算/sender/LOCK/判器。"""

    profile_id: str
    budget_schema: str
    protocol_policy: ProtocolPolicy
    resource_limits: ResourceLimits
    consent_scope: str
    judge_version: str
    namespace: int
    lane_order: "tuple[str, ...]"
    lane_task_id: "Mapping[str, int]"
    lane_post_caps: "Mapping[str, int]"
    hard_cap: int
    normal_budget: int
    family_cap: int
    family_history_posts: int
    data_root_name: str
    envelope_schema: str
    consent_schema: str
    public_contract_schema: str
    closure_groups_key: str = "legacy"
    extra: "Mapping[str, Any]" = field(default_factory=lambda: types.MappingProxyType({}))

    # ---- 派生即单一真值 -------------------------------------------------

    @property
    def is_legacy(self) -> bool:
        return self.profile_id == PROFILE_LEGACY

    @property
    def is_r12(self) -> bool:
        return self.profile_id == PROFILE_R12

    def task_caps(self) -> "Mapping[int, int]":
        return types.MappingProxyType({
            self.lane_task_id[lane]: self.lane_post_caps[lane]
            for lane in self.lane_order
        })

    def total_posts(self) -> int:
        return sum(self.lane_post_caps[lane] for lane in self.lane_order)

    def lane_of_task(self, task: int) -> str:
        for lane, task_id in self.lane_task_id.items():
            if task_id == task:
                return lane
        raise KeyError(f"task={task} 不在 profile {self.profile_id} 的 lane 表")

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": PROFILE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "budget_schema": self.budget_schema,
            "protocol_policy": self.protocol_policy.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
            "consent_scope": self.consent_scope,
            "judge_version": self.judge_version,
            "namespace": self.namespace,
            "lane_order": list(self.lane_order),
            "lane_task_id": dict(self.lane_task_id),
            "lane_post_caps": dict(self.lane_post_caps),
            "hard_cap": self.hard_cap,
            "normal_budget": self.normal_budget,
            "family_cap": self.family_cap,
            "family_history_posts": self.family_history_posts,
            "data_root_name": self.data_root_name,
            "envelope_schema": self.envelope_schema,
            "consent_schema": self.consent_schema,
            "public_contract_schema": self.public_contract_schema,
            "closure_groups_key": self.closure_groups_key,
            "extra": dict(self.extra),
        }


PROFILE_LEGACY_DEF = CampaignProfile(
    profile_id=PROFILE_LEGACY,
    budget_schema=BUDGET_SCHEMA_V1,
    protocol_policy=LEGACY_POLICY,
    resource_limits=ResourceLimits(),
    consent_scope="R1",
    judge_version="r1",
    namespace=2903,
    lane_order=LEGACY_LANE_ORDER,
    lane_task_id=LEGACY_LANE_TASK_ID,
    lane_post_caps=LEGACY_LANE_POST_CAPS,
    hard_cap=LEGACY_HARD_CAP,
    normal_budget=LEGACY_NORMAL_BUDGET,
    family_cap=0,
    family_history_posts=0,
    data_root_name="rsi_r1",
    envelope_schema=ENVELOPE_SCHEMA_V1,
    consent_schema=CONSENT_SCHEMA_V1,
    public_contract_schema=f"{SCHEMA_PREFIX}.public_contract.v1",
    closure_groups_key="legacy",
    extra=types.MappingProxyType({"legacy_contract_document": True}),
)

PROFILE_R12_DEF = CampaignProfile(
    profile_id=PROFILE_R12,
    budget_schema=BUDGET_SCHEMA_V2,
    protocol_policy=R12_POLICY,
    resource_limits=ResourceLimits(),
    consent_scope="R1.2",
    judge_version="r1.2",
    namespace=2904,
    lane_order=LANE_ORDER_R12,
    lane_task_id=LANE_TASK_ID_R12,
    lane_post_caps=LANE_POST_CAPS_R12,
    hard_cap=RSI_R12_HARD_CAP,
    normal_budget=RSI_R12_NORMAL_BUDGET,
    family_cap=FAMILY_CAP,
    family_history_posts=FAMILY_HISTORY_POSTS,
    data_root_name="rsi_r12",
    envelope_schema=ENVELOPE_SCHEMA_V2,
    consent_schema=CONSENT_SCHEMA_V2,
    public_contract_schema=PUBLIC_CONTRACT_SCHEMA_V2,
    closure_groups_key="r12",
    extra=types.MappingProxyType({
        "offline_validation_states": (
            "registered", "offline_authorized", "calibrated",
            "validation_locked", "validated",
        ),
        "paid_states": (
            "registered", "authorized", "calibrated", "frozen",
            "qualified", "study", "closed",
        ),
        "aux_subenvelopes": dict(AUX_SUBENVELOPES),
        "history_sources": [dict(s) for s in HISTORY_SOURCES],
        "no_repair_entry": True,
        "confirm_analysis_in_run_confirm": True,
        "analysis_receipt_required": True,
    }),
)

_BY_ID: "Mapping[str, CampaignProfile]" = types.MappingProxyType({
    PROFILE_LEGACY: PROFILE_LEGACY_DEF,
    PROFILE_R12: PROFILE_R12_DEF,
})


class ProfileError(ValueError):
    """profile 解析/一致性拒绝（不依赖 contracts 的 ContractError，避免循环）。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


def profile_by_id(profile_id: str) -> CampaignProfile:
    if not isinstance(profile_id, str) or profile_id not in _BY_ID:
        raise ProfileError("PROFILE_UNKNOWN",
                           f"profile_id={profile_id!r} 未登记；合法值 {PROFILES}")
    return _BY_ID[profile_id]


def declared_profile(envelope_doc: Mapping[str, Any] | None) -> "str | None":
    """从 envelope 文档读 profile 声明；历史 envelope 无此字段 → None（legacy）。"""
    if not isinstance(envelope_doc, Mapping):
        return None
    value = envelope_doc.get("profile_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError("PROFILE_UNKNOWN", f"profile_id 必须是字符串，实为 {value!r}")
    return value


def resolve_profile(envelope_doc: Mapping[str, Any] | None = None, *,
                    requested_campaign: "str | None" = None) -> CampaignProfile:
    """选择 profile；未知/冲突一律拒绝，绝不静默回退。

    * ``requested_campaign`` 为 CLI 的 ``--campaign``（可省略；省略时按 envelope
      声明，历史无声明即 legacy）；
    * envelope 已声明 id 且与请求不一致 → 拒绝（防止新表套旧根/旧表套新根）；
    * R1.2 根若 envelope 无声明也拒绝（新 profile 强制显式声明）。
    """
    declared = declared_profile(envelope_doc)
    requested = requested_campaign
    if requested is None:
        if declared is None:
            return PROFILE_LEGACY_DEF
        return profile_by_id(declared)
    if requested not in _BY_ID:
        raise ProfileError("PROFILE_UNKNOWN",
                           f"--campaign={requested!r} 未登记；合法值 {PROFILES}")
    if declared is not None and declared != requested:
        raise ProfileError(
            "PROFILE_MISMATCH",
            f"envelope 声明 profile_id={declared!r} 与 --campaign={requested!r} 不一致")
    if requested == PROFILE_R12 and envelope_doc is not None and declared is None:
        raise ProfileError(
            "PROFILE_MISMATCH",
            "R1.2 根必须在 contract/envelope.json 显式声明 profile_id=rsi-r1.2")
    return _BY_ID[requested]


# ---------------------------------------------------------------------------
# 公开资源合同（§2.1 的单一真值；渲染/最终请求审计共用）
# ---------------------------------------------------------------------------

def _public_method_sections() -> "dict[str, Any]":
    """公开方法段（lazy import：profiles 模块级保持零第三方依赖，且规避环）。"""
    from orchestrator.rsi_r1 import analysis_receipt as AR
    from orchestrator.rsi_r1 import statistics as ST
    return {
        "statistics": ST.public_method_document(),
        "analysis_output": AR.public_output_schema(),
    }


def public_contract_document(profile: CampaignProfile, *, envelope_id: str,
                             budget_sha256: str,
                             scenes: "tuple[str, ...]" = (),
                             family_used_before: int = FAMILY_HISTORY_POSTS) -> "dict[str, Any]":
    """构造 R1.2 公开资源合同文档（不含任何私有 seed/答案/尚未发布观测）。

    旧 profile 不走本函数（legacy 继续用 ``contracts.public_contract_document``，
    逐字节不变）。所有数值从 profile/budget 派生，不接受调用者覆盖。
    """
    if not profile.is_r12:
        raise ProfileError("PROFILE_UNKNOWN",
                           "public_contract_document(v2) 只接受 rsi-r1.2 profile")
    limits = profile.resource_limits
    if budget_sha256 and (len(budget_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in budget_sha256)):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "budget_sha256 非 64 位十六进制")
    lanes = {lane: profile.lane_post_caps[lane] for lane in profile.lane_order}
    doc: "dict[str, Any]" = {
        "schema": profile.public_contract_schema,
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "envelope_id": envelope_id,
        "budget_sha256": budget_sha256,
        "family": {
            "cap_posts": profile.family_cap,
            "history_posts": profile.family_history_posts,
            "closed_rounds_posts": FAMILY_CLOSED_ROUND_POSTS,
            "used_before_this_round": family_used_before,
            "remaining_including_this_round":
                profile.family_cap - family_used_before,
            "this_round_cap_posts": profile.hard_cap,
            "note": ("历史 31 呼 = R1 9 + R1.1 22（两独立 envelope 的最终根实呼，"
                     "carryover 不重复计）；已关闭轮实花 5 呼（"
                     "`r12-paid-20261113a`，花掉的不归还）；族帽 160 由 2026-11-13 "
                     "用户裁决设立；本轮不重置家族余额"),
        },
        "posts": {
            "per_lane": lanes,
            "total": profile.hard_cap,
            "scope": {
                "unit": "一次模型 HTTP POST（含 sent/failed/unknown）",
                "cumulative_per_lane": "该 lane 自本轮开始累计，不借其他 lane 余额",
                "no_repair_reserve": True,
            },
            "lane_consumption": {
                "probe": "单链 8 个规范动作 + 至多 4 个有界恢复 POST",
                "practice": "单链 8 个规范动作 + 至多 8 个有界恢复 POST",
                "study_A": "一个自主 episode，48 内自分配",
                "study_B": "一个自主 episode，48 内自分配",
            },
        },
        "compute": {
            "explore": {
                "cell": ["condition_id", "block_id"],
                "max_runs_per_study": limits.explore_runs_per_study,
                "max_batches_per_study": limits.explore_batches_per_study,
                "batch_rule": ("每个 batch 的 run 数 ≤ 该 study 当前累计剩余；"
                               "累计 ≤ 64；第 9 个 batch 即使仍有余额也拒绝"),
                "cumulative_scope": ("64 是**每 study 累计**上限，不是每条件 64，"
                                     "也不是要求 32 block"),
                "candidate_choice": ("候选可在 64 内自行选择矩阵规模；宿主不得自动"
                                     "截断 96 为 64，也不代选“最佳方案”"),
            },
            "confirm": {
                "blocks": limits.confirm_blocks,
                "max_conditions": limits.confirm_max_conditions,
                "max_runs_per_study": limits.confirm_runs_per_study,
                "assignment": "由独立宿主分配并公开算法/分布；不公开私有 seed",
                "post_margin_after_charge": limits.confirm_post_margin,
            },
            "replay": {
                "max_runs_per_study": limits.replay_runs_per_study,
                "note": "独立新根真实动力学复跑；不复制原数组缓存",
            },
            "runs": {
                "total_per_round": limits.total_runs,
                "steps_per_run": limits.steps_per_run,
                "study_steps": limits.study_steps,
                "aux_steps": limits.aux_steps,
                "total_steps": limits.total_steps,
            },
            "sandbox": {
                "executions_per_study": limits.sandbox_executions_per_study,
                "note": "含自动 confirm analysis 与 replay analysis；freeze 时原子预留两次",
            },
            "wallclock": {
                "active_per_study_s": limits.active_wallclock_s,
                "container_total_s": limits.container_wallclock_s,
                "gpu_total_s": limits.gpu_wallclock_s,
                "request_timeout_s": limits.request_timeout_s,
                "analysis_timeout_s": limits.analysis_timeout_s,
                "physics_run_timeout_s": limits.physics_run_timeout_s,
            },
        },
        "aux_subenvelopes": {
            "limits": dict(AUX_SUBENVELOPES),
            "total": AUX_SUBENVELOPE_TOTAL,
            "note": "离线与付费资格同计 aux；不在两处各给 2m；执行前先占额",
        },
        "units": {
            "run": "一次物理 rollout（step 数 = ceil(duration/dt)）",
            "step": "一个离散积分步；每 run ≤ 8000 step",
            "post": "一次模型请求（不计 host 计算/物理执行）",
            "block": "一个配对确认单元（条件 × block_id）",
        },
        "dynamic_sha": {
            "meaning": ("envelope/plan/design/analysis/confirm manifest 的 SHA-256 "
                        "由宿主派生，候选必须逐字引用；动态值只从真实公开结果读取"),
            "fields": ["plan_sha256", "analysis_source_sha256",
                       "confirm_manifest_sha256", "nonce"],
            "rule": "缺失/非法引用在发送前拒绝；不得预填或硬编码",
        },
        "error_classes": {
            "recoverable": list(RECOVERABLE_ERROR_CODES),
            "terminal": list(TERMINAL_ERROR_CODES),
            "classification_inputs": ["phase", "open_intent", "known_effect",
                                      "freeze_state", "exception_class"],
            "unknown_default": "terminal",
            "note": ("同一 RESOURCE_EXCEEDED 可能发生在执行前矩阵校验或已部分执行后；"
                     "分类不能只看错误码"),
        },
        "recovery": {
            "mode": profile.protocol_policy.mode,
            "single_practice_chain": profile.protocol_policy.single_practice_chain,
            "completion": ("规范动作游标完整 + submit 终态真实 + 必要产物/消费关系完整 "
                           "+ 无 terminal stop + POST ≤ 本 lane 帽；绝不要求恰 12/16"),
            "first_pass": "无失败/额外修正且八动作完整；逐位置记录 first_attempt_ok",
            "budgeted_completion": "在 lane 帽内闭合的尝试数/总尝试数",
            "extra_posts_probe": profile.protocol_policy.max_extra_posts_probe,
            "extra_posts_practice": profile.protocol_policy.max_extra_posts_practice,
            "min_remaining_actions_rule": "每次发送前按依赖图算，不足即 BUDGET_UNREACHABLE",
        },
        "science": {
            "studies": {
                "A": "接触/摩擦作用条件（含透明校准阴性控制 + 独立灵敏度控制）",
                "B": "无接触单关节跟踪：控制采样 Tc 与积分步长 dt 的耦合（开放问题）",
            },
            "negative_gate": "A 阴性 INCONCLUSIVE 不满足阴性门（原界不变）",
            "claim": "两案例最小自主研究可行性；不外推普遍科研能力",
            "receipt_required": True,
            "review": "两名独立科学审查者五条量表逐项 PASS；不追加第三票",
        },
        "refusal_policy": {
            "matrix_over_cap": "前执行拒绝，物理 run=0，不截断、不代选",
            "budget_unreachable": "不发明知无法闭合的调用",
            "transport_failure": "空回复/超时记传输失败并终止本轮付费；禁止隐式重试",
            "unknown_send": "不重发原请求，也不换 request_id 绕过",
            "instrument_defect_after_paid": "停止本轮，不修代码续测，无 repair 入口",
        },
        "scenes": list(scenes),
    }
    doc.update(_public_method_sections())
    return doc


def assert_public_contract_v2_complete(doc: Mapping[str, Any]) -> None:
    """公开合同自洽（T0 冻结前 + T1 渲染前共用）。缺任一条目即拒。"""
    if doc.get("schema") != PUBLIC_CONTRACT_SCHEMA_V2:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", f"schema={doc.get('schema')!r}")
    required = ("profile_id", "budget_sha256", "family", "posts", "compute",
                "aux_subenvelopes", "units", "dynamic_sha", "error_classes",
                "recovery", "science", "refusal_policy", "statistics",
                "analysis_output")
    for key in required:
        if key not in doc:
            raise ProfileError("PUBLIC_CONTRACT_INVALID", f"缺必需段 {key!r}")
    if doc["posts"]["total"] != RSI_R12_HARD_CAP:
        raise ProfileError("PUBLIC_CONTRACT_INVALID",
                           f"posts.total={doc['posts']['total']} != {RSI_R12_HARD_CAP}")
    if dict(doc["posts"]["per_lane"]) != dict(LANE_POST_CAPS_R12):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "posts.per_lane 与 lane 表不符")
    if doc["family"]["cap_posts"] != FAMILY_CAP:
        raise ProfileError("PUBLIC_CONTRACT_INVALID",
                           f"family.cap_posts != {FAMILY_CAP}")
    if doc["compute"]["explore"]["max_runs_per_study"] != MAX_EXPLORE_RUNS_PER_STUDY:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "explore 帽不是 64")
    if doc["compute"]["explore"]["cell"] != ["condition_id", "block_id"]:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "探索 cell 定义不符")
    if doc["compute"]["confirm"]["blocks"] != MAX_CONFIRM_BLOCKS:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "确认 block 数不是 32")
    if doc["aux_subenvelopes"]["total"] != MAX_AUX_STEPS:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "aux 总帽不符")
    if not doc["dynamic_sha"]["fields"]:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "动态 SHA 字段为空")
    if set(doc["error_classes"]["recoverable"]) & set(doc["error_classes"]["terminal"]):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "可恢复/终态错误码有交集")
    _assert_public_method_sections(doc)


def _assert_public_method_sections(doc: Mapping[str, Any]) -> None:
    """公开方法段必须与代码常量逐值同源（防渲染件与实现漂移）。"""
    from orchestrator.rsi_r1 import analysis_receipt as AR
    from orchestrator.rsi_r1 import rubric as R
    from orchestrator.rsi_r1 import statistics as ST
    stats = doc["statistics"]
    if stats.get("schema") != ST.METHOD_SCHEMA:
        raise ProfileError("PUBLIC_CONTRACT_INVALID",
                           f"statistics.schema={stats.get('schema')!r}")
    boot = stats.get("bootstrap") or {}
    if boot.get("resamples") != int(R.BOOTSTRAP_RESAMPLES):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "bootstrap resamples 与 rubric 不符")
    if boot.get("unit") != str(R.BOOTSTRAP_UNIT):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "bootstrap unit 与 rubric 不符")
    if boot.get("seed") != int(ST.STATISTICS_SEED):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "bootstrap seed 与实现不符")
    rng = boot.get("rng") or {}
    if rng.get("bit_generator") != "PCG64" or rng.get("seed_is_fixed") is not True:
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "bootstrap RNG 描述不完整")
    interval = boot.get("interval") or {}
    if interval.get("quantile_method") != str(R.BOOTSTRAP_QUANTILE):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "quantile 约定与 rubric 不符")
    levels = interval.get("levels") or {}
    if levels.get("primary") != str(R.CI_PRIMARY) or \
            levels.get("negative") != str(R.CI_NEGATIVE):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "区间水平与 rubric 不符")
    tol = stats.get("tolerance") or {}
    if tol.get("abs_tol") != str(R.CLAIM_ABS_TOL) or \
            tol.get("rel_tol") != str(R.CLAIM_REL_TOL):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "容差与 rubric 不符")
    cls = stats.get("claim_classification") or {}
    if cls.get("propositions") != list(R.PROPOSITIONS):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "主张类型表与 rubric 不符")
    output = doc["analysis_output"]
    if output.get("output_schema") != AR.OUTPUT_SCHEMA:
        raise ProfileError("PUBLIC_CONTRACT_INVALID",
                           f"analysis_output.output_schema="
                           f"{output.get('output_schema')!r}")
    if output.get("fields") != list(AR.CONTRAST_FIELDS):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "输出字段表与解析器不符")
    if output.get("statuses") != list(AR.STATUSES):
        raise ProfileError("PUBLIC_CONTRACT_INVALID", "状态表与解析器不符")


def render_public_contract_v2(doc: Mapping[str, Any]) -> str:
    """公开合同的确定性文本渲染（与实际校验共用同一 dict，不复制字面量）。"""
    assert_public_contract_v2_complete(doc)
    posts = doc["posts"]
    compute = doc["compute"]
    lines = [
        "# RSI-R1.2 公开资源合同",
        "",
        f"profile: {doc['profile_id']}  envelope: {doc['envelope_id']}",
        f"budget_sha256: {doc['budget_sha256']}",
        "",
        "## 1. POST 预算（每 lane 累计；sent/failed/unknown 都占槽）",
        *[f"- {lane}: 最大 {cap} 次 POST" for lane, cap in posts["per_lane"].items()],
        f"- 本轮合计: {posts['total']}",
        f"- 跨轮研究族: 硬帽 {doc['family']['cap_posts']} = 历史 "
        f"{doc['family']['history_posts']} + 本轮 {doc['family']['this_round_cap_posts']}",
        "",
        "## 2. 探索（每 study 累计）",
        f"- cell = (condition_id, block_id)；全组合 run 数 = 条件数 × 每条件 block 数",
        f"- 累计上限: {compute['explore']['max_runs_per_study']} run",
        f"- batch 上限: {compute['explore']['max_batches_per_study']} 个，单 batch ≤ 当前剩余",
        f"- {compute['explore']['max_runs_per_study']} 是每 study 累计上限，"
        f"不是每条件 {compute['explore']['max_runs_per_study']}，也不要求 "
        f"{compute['confirm']['blocks']} block",
        f"- 候选在 {compute['explore']['max_runs_per_study']} 内自选规模；"
        f"宿主不得自动截断、不得代选",
        "",
        "## 3. 确认与复跑",
        f"- 确认: {compute['confirm']['blocks']} 配对 block，最多 "
        f"{compute['confirm']['max_conditions']} 条件，≤ {compute['confirm']['max_runs_per_study']} run",
        f"- 复跑: ≤ {compute['replay']['max_runs_per_study']} run（独立新根真实动力学）",
        f"- 总量: {compute['runs']['total_per_round']} run / "
        f"{compute['runs']['total_steps']} step（每 run ≤ {compute['runs']['steps_per_run']} step）",
        "",
        "## 4. 沙盒/时间",
        f"- 每 study 候选沙盒执行 ≤ {compute['sandbox']['executions_per_study']} 次",
        f"- 每 study 活跃 ≤ {compute['wallclock']['active_per_study_s']} s；"
        f"容器累计 ≤ {compute['wallclock']['container_total_s']} s；"
        f"GPU 累计 ≤ {compute['wallclock']['gpu_total_s']} s",
        f"- 请求 {compute['wallclock']['request_timeout_s']} s；"
        f"分析 {compute['wallclock']['analysis_timeout_s']} s；"
        f"物理单 run {compute['wallclock']['physics_run_timeout_s']} s",
        "",
        "## 5. 动态 SHA 引用",
        *[f"- {f}" for f in doc["dynamic_sha"]["fields"]],
        f"- {doc['dynamic_sha']['rule']}",
        "",
        "## 6. 错误分类（可恢复 / 终态）",
        f"- 可恢复: {', '.join(doc['error_classes']['recoverable'])}",
        f"- 终态: {', '.join(doc['error_classes']['terminal'])}",
        f"- 分类输入: {', '.join(doc['error_classes']['classification_inputs'])}；"
        f"缺证据默认 {doc['error_classes']['unknown_default']}",
        "",
        "## 7. 完成判据",
        f"- {doc['recovery']['completion']}",
        f"- 首次链正确: {doc['recovery']['first_pass']}",
        f"- 预算内完成: {doc['recovery']['budgeted_completion']}",
        "",
        "## 8. 拒绝策略",
        *[f"- {k}: {v}" for k, v in doc["refusal_policy"].items()],
        "",
        "## 9. aux 子信封（离线与付费资格同计；执行前先占额）",
        *[f"- {k}: {v} step" for k, v in doc["aux_subenvelopes"]["limits"].items()],
        f"- 合计: {doc['aux_subenvelopes']['total']} step",
        "",
        "## 10. 科学任务（原合同不变）",
        *[f"- {k}: {v}" for k, v in doc["science"]["studies"].items()],
        "",
        "## 11. 单位",
        *[f"- {k}: {v}" for k, v in doc["units"].items()],
        "",
        "## 12. 公开统计方法（方法不是隐藏答案；可独立复现）",
        f"- 估计对象: {doc['statistics']['metric_scope']}；抽样单位 "
        f"{doc['statistics']['bootstrap']['unit']}",
        f"- 效应: {doc['statistics']['estimator']['effect']}",
        f"- 样本排序: {doc['statistics']['sample_order']['rule']}",
        f"- bootstrap: percentile，{doc['statistics']['bootstrap']['resamples']} 次重抽样，"
        f"{doc['statistics']['bootstrap']['rng']['bit_generator']}，"
        f"seed={doc['statistics']['bootstrap']['seed']}（固定）",
        f"- 随机数: {doc['statistics']['bootstrap']['rng']['construction']}；"
        f"{doc['statistics']['bootstrap']['rng']['draw']}；"
        f"{doc['statistics']['bootstrap']['rng']['statistic']}",
        f"- 区间: {doc['statistics']['bootstrap']['interval']['bounds']}，"
        f"quantile={doc['statistics']['bootstrap']['interval']['quantile_method']}，"
        f"dtype={doc['statistics']['bootstrap']['dtype']}",
        f"- 区间水平: 主 {doc['statistics']['bootstrap']['interval']['levels']['primary']}"
        f" / 阴性 {doc['statistics']['bootstrap']['interval']['levels']['negative']}"
        f"（{doc['statistics']['bootstrap']['interval']['level_rule']}）",
        f"- 容差: {doc['statistics']['tolerance']['formula']}，"
        f"abs={doc['statistics']['tolerance']['abs_tol']}, "
        f"rel={doc['statistics']['tolerance']['rel_tol']}",
        f"- 分类: δ 只取自冻结目录；主张 {'/'.join(doc['statistics']['claim_classification']['propositions'])}",
        *[f"  - {k}: {v}" for k, v in
          doc["statistics"]["claim_classification"]["rules"].items()],
        f"- {doc['statistics']['finite_sample_note']}",
        "",
        "## 13. 候选分析输出 schema",
        f"- schema 必须逐字等于 {doc['analysis_output']['output_schema']}",
        f"- 顶层键: schema / run_ids / contrasts（"
        f"{doc['analysis_output']['top_level']['run_ids']}）",
        f"- run_ids: {doc['analysis_output']['run_ids_rule']['identity']}",
        f"- 每条对比字段: {', '.join(doc['analysis_output']['fields'])}",
        f"- status 枚举: {', '.join(doc['analysis_output']['statuses'])}",
        f"- 宿主比较: {doc['analysis_output']['host_comparison']['policy']}；"
        f"{doc['analysis_output']['host_comparison']['response']}",
        f"- {doc['analysis_output']['no_host_values']}",
        "",
    ]
    return "\n".join(lines)
