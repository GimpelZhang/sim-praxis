"""Phase 2.8.1（＝Roadmap Phase 1）实验常量的**单一源**。

任何模块想要这三个任务、两个臂、帽值或预算，都必须从这里取——测试里
另写一份字面量即视为违规（AC-P281-0-05 用字面等值反查，不用表达式自推）。

预算推导（计划 §1.3）::

    10 呼/ Trial × 3 任务 × 2 臂 × 1 rep（Pilot）  =  60
    10 呼/ Trial × 3 任务 × 2 臂 × 2 rep（确认性）  = 120
                                                     ---
                                                     180

**为什么 HARD_CAP 不写成 sum(PILOT, CONFIRMATORY)**：Phase 2.6 的
``PHASE26_HARD_CAP`` 与 Phase 2.7 的 ``PHASE27_HARD_CAP`` 都是独立字面量
（缓冲/授权信封不可由任务帽相加推出）。本阶段虽然恰好相等，仍按独立字面量
写，防止将来有人「顺手改一处」让两个数字悄悄分叉。
"""
from __future__ import annotations

EXPERIMENT_ID = "roadmap_phase1_v1"
PHASE_NAME = "Phase 2.8.1"

ARMS = ("legacy_v1", "agentic_v1")
#: 计划 §4.3 的导出名与 §4.5.1 的常量名并存（同一对象，不是两份）
ARM_IDS = ARMS

#: task_no 是账本 ``task`` 字段（1/2/3），必须与预注册 ``tasks.yaml`` 逐字一致。
#: ``runtime`` 决定该任务的 harness：``host`` 用宿主进程，``docker`` 用兄弟容器。
TASK_ORDER = (
    {"task_no": 1, "task_id": "mujoco_contact_push_v1", "short": "M1",
     "runtime": "host"},
    {"task_no": 2, "task_id": "libero_env_adapter_v1", "short": "L1",
     "runtime": "docker"},
    {"task_no": 3, "task_id": "icy_ground_dr_v1", "short": "R1",
     "runtime": "host"},
)

TASK_IDS = tuple(t["task_id"] for t in TASK_ORDER)
TASK_NO_BY_ID = {t["task_id"]: t["task_no"] for t in TASK_ORDER}
RUNTIME_BY_ID = {t["task_id"]: t["runtime"] for t in TASK_ORDER}

CALL_CAP_PER_TRIAL = 10          # 两臂同帽
STEP_CAP_PER_TRIAL = 60          # 仅 agentic
TOKEN_CAP_PER_TRIAL = 100_000    # 仅 agentic
WALLCLOCK_CAP_S = 1800           # 两臂同值（30 分钟）
MAX_CODING_ROUNDS = 10           # 两臂同值（E9：与 10 呼帽对齐）
REPS_PILOT = 1
REPS_CONFIRMATORY = 2
REPS_TOTAL = REPS_PILOT + REPS_CONFIRMATORY   # 3

PILOT_BUDGET = len(TASK_ORDER) * len(ARMS) * REPS_PILOT * CALL_CAP_PER_TRIAL        # 60
CONFIRMATORY_BUDGET = len(TASK_ORDER) * len(ARMS) * REPS_CONFIRMATORY * CALL_CAP_PER_TRIAL  # 120
HARD_CAP = 180                   # 独立字面量（用户 2026-11-06 裁决 180），不由上式相加得出

#: 每任务帽（账本 ``PHASE281_TASK_CAPS`` 的同源值；两处出现必须逐字一致）
TASK_CAP = 60


def trial_id(task_id: str, arm_id: str, rep: int) -> str:
    """``<task_id>__<arm_id>__rep<k>``——**不带任何前缀**（计划 §1.4.3）。

    ``thread_id`` / ``run_id`` 与它逐字相同；四处（trial_id、目录名、
    thread_id、run_id）由本函数单点派生，任何一处手写即 AC-P281-1-14 红。
    """
    if arm_id not in ARMS:
        raise ValueError(f"未知臂：{arm_id!r}（合法值 {ARMS}）")
    if task_id not in TASK_NO_BY_ID:
        raise ValueError(f"未知任务：{task_id!r}（合法值 {TASK_IDS}）")
    if rep not in range(REPS_TOTAL):
        raise ValueError(f"rep 必须在 [0, {REPS_TOTAL}) 内：{rep!r}")
    return f"{task_id}__{arm_id}__rep{rep}"
