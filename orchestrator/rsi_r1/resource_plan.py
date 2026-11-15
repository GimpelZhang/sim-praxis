"""R1.3 计算资源计划：**从实际 spec 推导**，不猜数字（Plan §6/§8.2）。

为什么需要本模块：R1.2 的预算表把 A 的步长按 ``dt=0.002`` 假设写成 2000
step/run，而冻结仪器对 A 实际固定 ``dt=0.0005`` ⇒ 8000 step/run，导致子信封
在 T5 实测时超帽（Complete:410–430）。R1.3 起，任何资源上限都必须由
``scenes.run_shape`` 对**具体条件**求出的整数步数逐项求和，并给出裕度依据。

计划项（Plan §6/§9.1）：

* 维护参考链：两任务各 32 block 确认 + 完整 replay（独立执行，另计）；
* 维护 pilot/控制：固定点 pilot 正控 + 分布声明错配负控（共享同一批 run）；
* 资格玩具链：probe 一条 + practice 两条（既有离线套的冻结形状 32×3）；
* 边界/故障套：suite2/3 的探索与边界步数；
* 候选：探索 ≤64/study（按最坏 dt）、确认 32 block、replay 全确认集；
* 复审/报告/审查适配：零物理步（一次性文本/合成件）。

本模块只做算术，不执行物理、不触网、不写盘。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import scenes as S
from orchestrator.rsi_r1 import task_specs as TS

RESOURCE_PLAN_SCHEMA = P.RESOURCE_PLAN_SCHEMA_V13

#: 维护参考 / 候选正式 / 探索 / pilot / 资格套 / 边界套的子信封名。
ENV_MAINTENANCE = "maintenance_reference"
ENV_CANDIDATE = "candidate_formal"
ENV_EXPLORE = "candidate_explore"
ENV_PILOT = "maintenance_pilot_controls"
ENV_QUALIFICATION = "offline_qualification"
ENV_BOUNDARY = "boundary_fault"

#: 资格玩具链：probe 1 条 + practice 2 条，每条 32 block × 3 条件（旧冻结形状）。
TOY_CHAINS = 3
TOY_BLOCKS = P.MAX_CONFIRM_BLOCKS
TOY_CONDITIONS = 3
#: 玩具链每 run 步数（toy A/B 的最坏值；见 R1.2 实测 2,448,000 = 3×96×8000 的
#: 一部分探索 48,000/batch；这里按全 8000 记，宁多勿少）。
TOY_STEPS_PER_RUN = 8000
#: 边界/故障套（suite2/3）历史实测上限；R1.3 不重设科学边界，沿用同额。
BOUNDARY_STEPS = 240_000
#: 硬帽裕度（相对计划总量）；向上取整到 100000。
HARD_CAP_MARGIN = Decimal("0.08")


class ResourcePlanError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


@dataclass(frozen=True)
class SuiteEntry:
    suite_id: str
    owner: str            # maintenance｜candidate｜instrument
    task_id: str
    phase: str            # confirm｜replay｜explore｜pilot｜qualification｜boundary
    conditions: int
    blocks: int
    repeats: int
    steps_per_run: int
    runs: int
    steps: int
    execution_id: str
    depends_on: "tuple[str, ...]"
    subenvelope: str
    note: str = ""

    def to_dict(self) -> "dict[str, Any]":
        return {
            "suite_id": self.suite_id,
            "owner": self.owner,
            "task_id": self.task_id,
            "phase": self.phase,
            "conditions": self.conditions,
            "blocks": self.blocks,
            "repeats": self.repeats,
            "steps_per_run": self.steps_per_run,
            "runs": self.runs,
            "steps": self.steps,
            "execution_id": self.execution_id,
            "depends_on": list(self.depends_on),
            "subenvelope": self.subenvelope,
            "note": self.note,
        }


@dataclass(frozen=True)
class ResourcePlanV13:
    entries: "tuple[SuiteEntry, ...]"
    subenvelopes: "dict[str, int]"
    total_steps: int
    hard_cap_steps: int
    margin: str

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": RESOURCE_PLAN_SCHEMA,
            "schema_version": 1,
            "profile_id": P.PROFILE_R13,
            "method": {
                "steps_per_run": ("scenes.run_shape(实际 dt/uration) 的整数 nsteps；"
                                  "A 的 dt 由宿主固定 0.0005"),
                "runs": "conditions × blocks × repeats",
                "shared_execution": "同一 execution_id 的只读物理产物只计一次；"
                                    "replay 是独立执行，另计",
                "hard_cap": "计划总量 × (1 + 8%) 向上取整到 100000",
            },
            "entries": [e.to_dict() for e in self.entries],
            "subenvelopes": dict(self.subenvelopes),
            "total_steps": self.total_steps,
            "hard_cap_steps": self.hard_cap_steps,
            "margin": self.margin,
            "baseline_compare": {
                "r12_total_cap_steps": P.MAX_TOTAL_STEPS,
                "r12_aux_cap_steps": P.MAX_AUX_STEPS,
                "note": "旧值仅作比较，不作新任务承诺",
            },
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()

    def task_sha256(self, task_id: str) -> str:
        """绑定到单个任务的资源子摘要（写进 TaskSpec.resource_spec_sha）。"""
        rows = [e.to_dict() for e in self.entries if e.task_id in (task_id, "*")]
        body = {"task_id": task_id, "entries": rows,
                "subenvelopes": {e.subenvelope: self.subenvelopes[e.subenvelope]
                                 for e in self.entries}}
        return hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()


def steps_per_run(condition: Any, *, scene_id: str) -> int:
    """由条件的实际 dt/duration 求整数步数（A 的 dt 由宿主附加）。"""
    if scene_id == S.SCENE_A:
        dt = E.A_FIXED_DT_S
        shape = S.run_shape(dt)
    else:
        dt = Decimal(condition.params["dt_s"])
        tc = Decimal(condition.params["tc_s"])
        shape = S.run_shape(dt, tc_s=tc)
    return int(shape["nsteps"])


def _worst_steps(scene_id: str) -> int:
    """探索最坏步数：全允许 dt 中最大（A 固定；B 取 0.0005 ⇒ 8000）。"""
    if scene_id == S.SCENE_A:
        return int(S.run_shape(E.A_FIXED_DT_S)["nsteps"])
    return int(S.run_shape(min(S.ALLOWED_DT_S))["nsteps"])


def build_plan(tasks: "tuple[TS.TaskSpecV13, ...]", *,
               explore_runs_per_task: int = P.MAX_EXPLORE_RUNS_PER_STUDY,
               confirm_blocks: int = P.MAX_CONFIRM_BLOCKS,
               pilot_blocks: int = 4) -> ResourcePlanV13:
    entries: "list[SuiteEntry]" = []
    subenv: "dict[str, int]" = {}
    for task in tasks:
        if task.split != TS.SPLIT_HOLDOUT:
            continue
        n = len(task.conditions)
        per_run = [steps_per_run(c, scene_id=task.mechanism_id)
                   for c in task.conditions]
        worst = _worst_steps(task.mechanism_id)
        # 维护参考确认 + 独立 replay
        for phase, sub in (("confirm", ENV_MAINTENANCE),
                           ("replay", ENV_MAINTENANCE)):
            runs = confirm_blocks * n
            steps = sum(confirm_blocks * s for s in per_run)
            e = SuiteEntry(
                suite_id=f"maint-{phase}-{task.task_id}", owner="maintenance",
                task_id=task.task_id, phase=phase, conditions=n,
                blocks=confirm_blocks, repeats=1, steps_per_run=max(per_run),
                runs=runs, steps=steps,
                execution_id=f"exec-maint-{phase}-{task.task_id}",
                depends_on=() if phase == "confirm" else
                (f"exec-maint-confirm-{task.task_id}",),
                subenvelope=sub,
                note="32 block 全条件；replay 独立执行另计")
            entries.append(e)
            subenv[sub] = subenv.get(sub, 0) + steps
        # 维护 pilot/控制：同一批 run 复用（execution_id 共享，计一次）
        runs = pilot_blocks * n
        steps = sum(pilot_blocks * s for s in per_run)
        entries.append(SuiteEntry(
            suite_id=f"maint-pilot-{task.task_id}", owner="maintenance",
            task_id=task.task_id, phase="pilot", conditions=n,
            blocks=pilot_blocks, repeats=1, steps_per_run=max(per_run),
            runs=runs, steps=steps,
            execution_id=f"exec-maint-pilot-{task.task_id}",
            depends_on=(), subenvelope=ENV_PILOT,
            note=("固定点 pilot 正控 + 分布声明错配负控共享此批物理 run；"
                  "负控不另起执行")))
        subenv[ENV_PILOT] = subenv.get(ENV_PILOT, 0) + steps
        # 候选探索（最坏 dt）
        exp_steps = explore_runs_per_task * worst
        entries.append(SuiteEntry(
            suite_id=f"cand-explore-{task.task_id}", owner="candidate",
            task_id=task.task_id, phase="explore", conditions=0,
            blocks=0, repeats=1, steps_per_run=worst,
            runs=explore_runs_per_task, steps=exp_steps,
            execution_id=f"exec-cand-explore-{task.task_id}",
            depends_on=(), subenvelope=ENV_EXPLORE,
            note="≤64/study 累计；按最坏 dt 记，宁多勿少"))
        subenv[ENV_EXPLORE] = subenv.get(ENV_EXPLORE, 0) + exp_steps
        # 候选正式确认 + replay
        for phase in ("confirm", "replay"):
            runs = confirm_blocks * n
            steps = sum(confirm_blocks * s for s in per_run)
            entries.append(SuiteEntry(
                suite_id=f"cand-{phase}-{task.task_id}", owner="candidate",
                task_id=task.task_id, phase=phase, conditions=n,
                blocks=confirm_blocks, repeats=1, steps_per_run=max(per_run),
                runs=runs, steps=steps,
                execution_id=f"exec-cand-{phase}-{task.task_id}",
                depends_on=(() if phase == "confirm" else
                            (f"exec-cand-confirm-{task.task_id}",)),
                subenvelope=ENV_CANDIDATE,
                note="确认 32 block；replay 全确认集、独立执行"))
            subenv[ENV_CANDIDATE] = subenv.get(ENV_CANDIDATE, 0) + steps
    # 资格玩具链（既有冻结形状；与新任务条件无关）
    toy_runs = TOY_CHAINS * TOY_BLOCKS * TOY_CONDITIONS
    toy_steps = toy_runs * TOY_STEPS_PER_RUN
    entries.append(SuiteEntry(
        suite_id="qualification-toy-chains", owner="instrument", task_id="*",
        phase="qualification", conditions=TOY_CONDITIONS, blocks=TOY_BLOCKS,
        repeats=1, steps_per_run=TOY_STEPS_PER_RUN, runs=toy_runs,
        steps=toy_steps, execution_id="exec-qualification-toy",
        depends_on=(), subenvelope=ENV_QUALIFICATION,
        note="probe 1 + practice 2 条 32×3 玩具确认（旧冻结形状）"))
    subenv[ENV_QUALIFICATION] = toy_steps
    entries.append(SuiteEntry(
        suite_id="boundary-fault-suites", owner="instrument", task_id="*",
        phase="boundary", conditions=0, blocks=0, repeats=1,
        steps_per_run=0, runs=0, steps=BOUNDARY_STEPS,
        execution_id="exec-boundary-fault", depends_on=(),
        subenvelope=ENV_BOUNDARY,
        note="suite2/3 超帽/边界矩阵的物理步数上限（历史实测额）"))
    subenv[ENV_BOUNDARY] = BOUNDARY_STEPS

    total = sum(subenv.values())
    hard = int((Decimal(total) * (Decimal("1") + HARD_CAP_MARGIN)).to_integral_value(
        rounding="ROUND_CEILING"))
    hard = int(math.ceil(hard / 100_000.0)) * 100_000
    return ResourcePlanV13(entries=tuple(entries), subenvelopes=dict(subenv),
                           total_steps=total, hard_cap_steps=hard,
                           margin=str(HARD_CAP_MARGIN))


def build_t1_plan() -> "tuple[TS.TaskSpecV13, TS.TaskSpecV13, dict[str, Any]]":
    """T1 主入口：构造两留出任务、资源计划与任务集清单（零物理/零 POST）。

    私有实例承诺：由维护侧**封存件**给出（本函数只接受 64 hex；不生成、
    不打印种子）。T1 实跑时由调用方传入。
    """
    raise ResourcePlanError(
        "SEALED_INPUT_REQUIRED",
        "私有实例承诺必须由 T1 封存件提供；本函数不接受凭空的默认值")
