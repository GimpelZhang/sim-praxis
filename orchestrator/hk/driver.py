"""K0–K8 驱动器 + Gate1/Gate2 打回循环接面（语义照 `orchestrator/graph/nodes.py`）。

只接不改：本模块**不导入、不修改** `nodes.py`/`cards.py`；生产注入时把
`ResearchDriver.gate1_hook`/`gate2_hook` 适配到 `GraphDeps.gate1/gate2`（现役飞书
阻塞节点），S0 用假门测试。

打回语义（与 `nodes.py` 真值逐条对齐）：
- Gate2 打回必须带 human_notes，空意见直接结案 `REJECT_WITHOUT_NOTES`（nodes.py L235）；
- per-Stage 计数（CR-20，L254 教训：全局相加是错），`GATE2_MAX_REJECTS_PER_STAGE`
  默认 2（nodes.py L74 口径），耗尽转 human_failure `REJECT_BUDGET_EXHAUSTED`；
- 打回通过时 run_idx++ 生成全新 run 目录（`task8.py:160` 语义），意见注入补救线程
  （K1 的 actor 输入带 `human_notes`）后重跑；
- Gate1 在 K0 之前：批准前零付费、零引擎实跑（No Fake Approval）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

from . import contracts as C
from .guards import GuardCtx, GuardViolation, enforce
from . import skills as skills_mod  # 相对导入写成包内引用：milestone27 的
# AC-T2-4 AST 扫描把 `from .skills import ...` 误判成内容层 skills 包导入


__all__ = ["GateDecision", "DriverResult", "ResearchDriver",
           "TouchpointBudgetExceeded", "GATE2_MAX_REJECTS_PER_STAGE",
           "GATE1_MAX_REJECTS", "DIAGNOSE_BUDGET"]

GATE2_MAX_REJECTS_PER_STAGE = 2
GATE1_MAX_REJECTS = 2
TOUCHPOINT_BUDGET = 4
DIAGNOSE_BUDGET = 2


def _require_mapping_dict(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} 必须是 dict，收到 {type(value).__name__}")
    return value


class TouchpointBudgetExceeded(RuntimeError):
    """System 2 触点超过每研究 4 次预算（设计族 §1 触点预算）。"""


@dataclass
class GateDecision:
    """门判据；`approved=False` 时 `human_notes` 为空按结案处理。"""

    approved: bool
    human_notes: str = ""
    ref: str = ""


@dataclass
class DriverResult:
    status: str                      # done | human_failure
    code: str = ""
    run_idx: int = 0
    claim: Mapping[str, Any] | None = None
    rejects: dict[str, int] = field(default_factory=dict)
    detail: str = ""


class ResearchDriver:
    """顺序执行技能链；门与引擎全部注入（S0 假门/假 actor，S1 接生产）。"""

    def __init__(self, *, gate1_hook: Callable[[dict], GateDecision],
                 gate2_hook: Callable[[dict, str], GateDecision],
                 actor: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None,
                 evidence_root: Path,
                 batch_runner: Callable | None = None,
                 analyzer: Callable | None = None,
                 confirmer: Callable | None = None,
                 direction_reviser: Callable[[dict, str], dict] | None = None,
                 budget_remaining: int = 10_000,
                 touchpoint_budget: int = TOUCHPOINT_BUDGET,
                 max_rejects_per_stage: int = GATE2_MAX_REJECTS_PER_STAGE,
                 claim_guard: bool = False,
                 diagnose_budget: int = DIAGNOSE_BUDGET,
                 assignment_salt: str | None = None):
        self.gate1_hook = gate1_hook
        self.gate2_hook = gate2_hook
        self.evidence_root = Path(evidence_root)
        self.budget_remaining = budget_remaining
        self.touchpoint_budget = touchpoint_budget
        self.max_rejects_per_stage = max_rejects_per_stage
        self.rejects: dict[str, int] = {}
        self.touchpoints_used = 0
        self.diagnose_used = 0
        # S1 生产链开启 G-CLAIM（自报 vs host 复算逐字段）；S0 骨架的假 actor 由
        # 里程碑测试显式关着——S1 的 AC-17 负控覆盖「不符即 CLAIM_INVALID」。
        self.claim_guard = bool(claim_guard)
        self.diagnose_budget = int(diagnose_budget)
        # 评审 MEDIUM：block 分配的 host 私有 salt（run_study 以 os.urandom 生成并落
        # 0600 私有件）；只进 ctx 的批处理路径，**绝不注入触点 _ctx**。
        self.assignment_salt = assignment_salt
        # AC-13：打回意见含实质性算法修补 → 结论降级「人机共同改进」（claim 件标注）
        self.human_cofix: dict[str, bool] = {}
        self._direction_reviser = direction_reviser
        self._raw_actor = actor
        self._counted_actor = self._actor(actor)
        self.skills = skills_mod.build_skills(actor=self._counted_actor,
                                              batch_runner=batch_runner,
                                              analyzer=analyzer, confirmer=confirmer)

    # —— System 2 触点（计数 + 预算）——
    def _actor(self, actor):
        if actor is None:
            return None

        def counted(name: str, contract: str, value):
            if name == "diagnose_fail":  # 诊断触点独立预算（≤2/研究，计划 S1-4）
                if self.diagnose_used >= self.diagnose_budget:
                    raise TouchpointBudgetExceeded(
                        f"诊断触点预算 {self.diagnose_budget} 已用尽（试图 {name}）")
                self.diagnose_used += 1
                return actor(name, contract, value)
            if self.touchpoints_used >= self.touchpoint_budget:
                raise TouchpointBudgetExceeded(
                    f"触点预算 {self.touchpoint_budget} 已用尽（试图 {name}）")
            self.touchpoints_used += 1
            return actor(name, contract, value)

        return counted

    # —— 证据 ——
    def _run_dir(self, stage_id: str, run_idx: int) -> Path:
        root = self.evidence_root / "runs" / f"{stage_id}_run{run_idx:03d}"
        ensure_private_dir(root, mode=0o700)
        return root

    @staticmethod
    def _touchpoint_value(value: Any, ctx: Mapping[str, Any]) -> Any:
        """触点输入附带可序列化运行上下文（``_ctx``）。

        S1 事故（2026-11-18）：HeadlessActor 需要 run_dir 才能起 fresh 会话，
        但 actor 协议只有 (name, contract, value)——run 级上下文在 driver 的
        ctx 里，从未进入 value，导致首个触点即 RUN_DIR_MISSING。修复=在 K1/K4/K8
        输入上注入 ``_ctx``（剔除不可序列化对象）；下游 HASH 面只对 source/结果
        取值，不受影响。
        """
        if not isinstance(value, Mapping):
            return value
        # 私有面不外泄：driver 对象与 block 分配 salt 不进入 actor 可见的 _ctx。
        safe = {k: v for k, v in ctx.items()
                if k not in ("driver", "assignment_salt")}
        return dict(value, _ctx=safe)

    def _record(self, run_dir: Path, name: str, payload: Any) -> str:
        path = run_dir / name
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(path, text, mode=0o600)
        return path.name

    # —— 阶段执行 ——
    def run_stage(self, stage_id: str, spec: Mapping[str, Any],
                  notes: str = "") -> DriverResult:
        registered = self.skills["K0"].execute(dict(spec))
        try:
            enforce(["G-PREREG"], GuardCtx(stage=stage_id,
                                           registered_spec_sha=registered.get("sha256")))
        except GuardViolation as exc:
            return DriverResult("human_failure", exc.code, 0, detail=str(exc))

        run_idx = 0
        diagnose_doc = None
        while True:
            run_idx += 1
            self.touchpoints_used = 0  # 触点预算按 run 计（打回重跑＝新 run_idx 的研究尝试）
            run_dir = self._run_dir(stage_id, run_idx)
            self._record(run_dir, "stage_input.json",
                         {"spec": registered, "notes": notes, "run_idx": run_idx})
            value: Any = dict(registered)
            if notes:
                value = dict(value, human_notes=notes)
            if diagnose_doc is not None:
                # 打回意见的诊断结果进补救线程（K1 输入），与 human_notes 并列
                value = dict(value, diagnose=diagnose_doc)
            ctx = {"stage_id": stage_id, "run_idx": run_idx,
                   "run_dir": str(run_dir), "evidence_root": str(self.evidence_root),
                   "observations_root": str(self.evidence_root / "observations"),
                   "fresh_root": str(self.evidence_root / "recompute"
                                     / f"{stage_id}_run{run_idx:03d}"),
                   "batch_id": f"{stage_id}-run{run_idx:03d}",
                   "registered_plan_sha256": (
                       (registered.get("spec") or {}).get("plan_sha256")
                       if isinstance(registered, Mapping) else None),
                   "assignment_salt": self.assignment_salt,
                   "driver": self}
            if diagnose_doc is not None:
                ctx["diagnose"] = diagnose_doc
            k7_value: Any = None
            try:
                for key in ("K1", "K2", "K3", "K4", "K5", "K6", "K7"):
                    if key == "K3":
                        try:
                            enforce(["G-BUDGET"], GuardCtx(
                                stage=stage_id, remaining_budget=self.budget_remaining,
                                required_actions=1))
                        except GuardViolation as exc:
                            return DriverResult("human_failure", exc.code, run_idx,
                                                rejects=dict(self.rejects),
                                                detail=str(exc))
                    skill_input = (self._touchpoint_value(value, ctx)
                                   if key in ("K1", "K4", "K8") else value)
                    value = self.skills[key].execute(skill_input, ctx)
                    if key == "K7":
                        k7_value = value
                    self._record(run_dir, f"{key}.json", value)
                claim = self.skills["K8"].execute(
                    self._touchpoint_value(value, ctx), ctx)
                claim = dict(claim)
                if self.human_cofix.get(stage_id):
                    claim["human_cofix"] = True
                    claim["conclusion_grade"] = "human_machine_joint_improvement"
                self._record(run_dir, "K8.json", claim)
                if self.claim_guard:
                    # G-CLAIM（AC-17）：自报逐字段 vs host 复算；不符即 CLAIM_INVALID。
                    # host 真值取 confirm_result 的**主张投影**（contrasts 行）——
                    # 只比对可主张的数值面，信封元数据（时间戳/sha）不要求自报复制。
                    host_truth = k7_value if isinstance(k7_value, Mapping) else {}
                    host_contrasts = host_truth.get("contrasts")
                    if isinstance(host_contrasts, list):
                        host_truth = {"contrasts": host_contrasts}
                    enforce(["G-CLAIM"], GuardCtx(stage=stage_id, claim_doc=claim,
                                                  host_recomputed=dict(host_truth)))
                    # 评审 HIGH-2：总判词不得是纯自报——必须等于 host 对**主对比**
                    # （contrasts[0]，预注册前已冻结顺序）的分类。
                    if isinstance(host_contrasts, list) and host_contrasts:
                        expected = host_contrasts[0].get("claim")
                        declared = claim.get("verdict")
                        if expected is not None and declared != expected:
                            return DriverResult(
                                "human_failure", "CLAIM_INVALID", run_idx,
                                rejects=dict(self.rejects),
                                detail=(f"claim.verdict={declared!r} 与 host 主对比分类 "
                                        f"{expected!r} 不符（结论不得靠自述）"))
            except GuardViolation as exc:
                # 守卫拒绝 = 协议级停止码（如 G-CLAIM 的 CLAIM_INVALID），原码上报
                return DriverResult("human_failure", exc.code, run_idx,
                                    rejects=dict(self.rejects), detail=str(exc))
            except (skills_mod.SkillEngineNotWired, TouchpointBudgetExceeded) as exc:
                code = ("TOUCHPOINT_BUDGET_EXCEEDED"
                        if isinstance(exc, TouchpointBudgetExceeded) else "ENGINE_NOT_WIRED")
                return DriverResult("human_failure", code, run_idx,
                                    rejects=dict(self.rejects), detail=str(exc))
            except Exception as exc:  # noqa: BLE001 - 仪器失败如实结案，不吞
                return DriverResult("human_failure", "STAGE_RUNTIME_ERROR", run_idx,
                                    rejects=dict(self.rejects),
                                    detail=f"{type(exc).__name__}: {exc}")

            card = {"stage_id": stage_id, "run_idx": run_idx, "claim": claim,
                    "evidence": str(run_dir), "rejects": dict(self.rejects)}
            self._record(run_dir, "gate2_card.json", card)
            decision = self.gate2_hook(card, stage_id)
            self._record(run_dir, "gate2_decision.json",
                         {"approved": decision.approved,
                          "human_notes": decision.human_notes, "ref": decision.ref})
            if decision.approved:
                return DriverResult("done", "APPROVED", run_idx, claim=claim,
                                    rejects=dict(self.rejects))
            if not decision.human_notes:
                return DriverResult("human_failure", "REJECT_WITHOUT_NOTES", run_idx,
                                    rejects=dict(self.rejects),
                                    detail="打回未带审批意见，按 nodes.py L235 结案")
            # 打回意见注入诊断触点（计划 S1-4：human_notes 原文进 diagnose prompt；
            # 实质性算法修补 → claim 件标 human_cofix，结论降级人机共同改进）
            diagnose_doc = None
            if self.actor_available():
                try:
                    diagnose_doc = dict(_require_mapping_dict(
                        self._actor_call("diagnose_fail", C.CLAIM.name,
                                         {"human_notes": decision.human_notes,
                                          "claim": claim, "run_idx": run_idx,
                                          "evidence": str(run_dir)}),
                        "diagnose"))
                except TouchpointBudgetExceeded as exc:
                    diagnose_doc = {"skipped": "DIAGNOSE_BUDGET_EXHAUSTED",
                                    "detail": str(exc)}
            if diagnose_doc is not None:
                self._record(run_dir, "diagnose.json", diagnose_doc)
                if diagnose_doc.get("algorithm_patch") is True:
                    self.human_cofix[stage_id] = True
            self.rejects[stage_id] = self.rejects.get(stage_id, 0) + 1  # CR-20：per-Stage
            if self.rejects[stage_id] >= self.max_rejects_per_stage:
                return DriverResult("human_failure", "REJECT_BUDGET_EXHAUSTED", run_idx,
                                    rejects=dict(self.rejects),
                                    detail=f"{stage_id} 打回 {self.rejects[stage_id]} "
                                           f"次 > 上限 {self.max_rejects_per_stage}")
            notes = decision.human_notes  # 意见进补救线程（K1 输入）

    # —— 诊断触点（独立预算；打回意见注入）——
    def actor_available(self) -> bool:
        return getattr(self, "_raw_actor", None) is not None

    def _actor_call(self, name: str, contract: str, value: Any):
        if self._counted_actor is None:
            raise TouchpointBudgetExceeded("actor 未接线")
        return self._counted_actor(name, contract, value)

    # —— Gate1：研究目标门（批准前零实跑）——
    def run_campaign(self, goal_card: dict,
                     stages: Mapping[str, Mapping[str, Any]] | None = None
                     ) -> dict[str, Any]:
        card = dict(goal_card)
        gate1_rejects = 0
        while True:
            decision = self.gate1_hook(card)
            if decision.approved:
                break
            if not decision.human_notes:
                return {"status": "human_failure", "code": "REJECT_WITHOUT_NOTES",
                        "stage": "gate1"}
            gate1_rejects += 1
            if gate1_rejects >= GATE1_MAX_REJECTS:
                return {"status": "human_failure", "code": "REJECT_BUDGET_EXHAUSTED",
                        "stage": "gate1"}
            if self._direction_reviser is not None:
                card = dict(self._direction_reviser(card, decision.human_notes))
            else:
                # 无注入修订器时仍照"打回带意见重发卡"语义：把意见挂在卡上重发
                # （不伪造 AI 修订；生产注入真实 direction 重跑器）
                history = list(card.get("prior_reject_notes", []))
                card = dict(card, prior_reject_notes=history + [decision.human_notes])

        # 批准后才允许任何引擎/付费动作（No Fake Approval）
        if not stages:
            return {"status": "human_failure", "code": "NO_STAGES", "stage": "gate1"}
        results = {}
        for stage_id, spec in stages.items():
            result = self.run_stage(stage_id, spec)
            results[stage_id] = result
            if result.status != "done":
                break
        outcome = {
            "status": "done" if all(r.status == "done" for r in results.values()) else "human_failure",
            "stages": {k: {"status": v.status, "code": v.code, "run_idx": v.run_idx,
                           "claim": v.claim, "rejects": v.rejects, "detail": v.detail}
                       for k, v in results.items()},
            "rejects": dict(self.rejects),
            "touchpoints_last_run": self.touchpoints_used,
        }
        return outcome
