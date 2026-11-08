"""RSI-R0 runner：薄组合根 + 串行调度 + 恢复（AC21–AC24）。

这一层只做**编排**，不重复实现任何判据：

* 预算/发网事实 → :mod:`orchestrator.rsi_r0.attempts`（T1）
* 数据合同与臂/层/帽 → :mod:`orchestrator.rsi_r0.contracts`
* 判决 → :mod:`orchestrator.rsi_r0.evaluator`（T3）
* 门与批准件 → :mod:`orchestrator.rsi_r0.gates`
* 路径 → :mod:`orchestrator.common.runtime_paths`（**唯一**真值，绝不自己拼）

三条纪律写死在代码里，而不是靠调用者守规矩：

1. **显式 R0**：构造时把 `--campaign` 落成 `SIMPRAXIS_RSI_R0_CAMPAIGN` 并核对
   `rsi_r0_campaign_root()` 与给定路径逐字相等，之后一切路径都从 runtime_paths 取。
   任何解析到别处的请求立刻 fail-closed（AC21）。
2. **正式试次禁缓存**：`TrialSpec` 只允许 `cache_policy="disabled"`；`replay_debug`
   仅限 `dry-run` 批次，付费批次带上它直接拒（AC22）。
3. **恢复不重发**：slice 的终态只由 `runs/<trial_id>/trial.json` 决定。付费 slice
   若停在 `RUNNING`，恢复只能标 `UNKNOWN_SENT` 并保留 slot，绝不重新发网。
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import gates
from orchestrator.rsi_r0.attempts import (
    AttemptJournal,
    BudgetReservations,
    _atomic_write_json,
    _utc_now,
    campaign_lock,
)
from orchestrator.rsi_r0.contracts import (
    DEVELOPMENT_ORDER,
    INSTANCE_IDS,
    SLOT_REQUEST_CAP,
    TASK_ID_FAMILY,
    VALIDATION_ORDER,
    InstanceSpec,
    ResourceCaps,
    SlotState,
    canonical_json,
    instance_family,
    sha256_of,
)
from orchestrator.rsi_r0.image_pin import IMAGE_REF

#: 批次 → (slot 列表, 每 slot 请求帽)。全部来自计划 §6.1，不在这里另立数字。
BATCH_SLOTS: Mapping[str, tuple[Sequence[str], int]] = {
    "R0-P": (("P1", "P2", "P3", "P4"), 2),
    "R0-D-E": (("E-dev",), SLOT_REQUEST_CAP),
    "R0-D-D": (("D-dev",), SLOT_REQUEST_CAP),
    "R0-D-R": (("R-dev",), SLOT_REQUEST_CAP),
    "R0-V-E1": (("E-v1",), SLOT_REQUEST_CAP),
    "R0-V-D1": (("D-v1",), SLOT_REQUEST_CAP),
    "R0-V-R1": (("R-v1",), SLOT_REQUEST_CAP),
    "R0-V-E2": (("E-v2",), SLOT_REQUEST_CAP),
    "R0-V-D2": (("D-v2",), SLOT_REQUEST_CAP),
    "R0-V-R2": (("R-v2",), SLOT_REQUEST_CAP),
}

#: 零付费批次：``R0-`` 前插 ``DRY``。它们只走 dry-run、永不触网，因此允许
#: ``replay_debug``；正式批次带上任何缓存策略都会被 ``TrialSpec`` 直接拒（AC22）。
DRY_TO_REAL: Mapping[str, str] = {
    f"R0-DRY-{k[3:]}": k for k in BATCH_SLOTS
}
DRY_BATCHES: tuple[str, ...] = tuple(DRY_TO_REAL)

#: 故障注入点（AC23 要求这六处都能被打断）。
FAULT_POINTS: tuple[str, ...] = (
    "reserve", "pre_send", "post_send", "settle", "evaluate", "report")


class RunnerError(RuntimeError):
    """组合根拒绝执行（路径/批次/spec/恢复语义不符）。"""


class InjectedFault(RuntimeError):
    """测试注入的故障：用来证明每个阶段都可中断且恢复后不重发。"""


class PaidPathNotArmed(RunnerError):
    """付费入口在 T5 之前不接线：显式拒绝，绝不静默什么都不做。"""


# ---------------------------------------------------------------- 试次合同
@dataclass(frozen=True)
class TrialSpec:
    """一个 slot 一次运行的显式合同。所有字段必填，没有隐式默认。"""

    trial_id: str
    slot_id: str
    batch_id: str
    actor: Mapping[str, Any]
    cache_policy: str = "disabled"
    session_id: str = ""
    request_cap: int = SLOT_REQUEST_CAP

    def __post_init__(self) -> None:
        if self.batch_id not in BATCH_SLOTS and self.batch_id not in DRY_BATCHES:
            raise RunnerError(f"未声明的批次：{self.batch_id}")
        if self.cache_policy not in ("disabled", "replay_debug"):
            raise RunnerError(f"未声明的缓存策略：{self.cache_policy}")
        if self.cache_policy == "replay_debug" and self.batch_id not in DRY_BATCHES:
            raise RunnerError(
                f"批次 {self.batch_id} 是正式试次，禁用响应缓存（AC22）")
        if not self.actor or not self.actor.get("name"):
            raise RunnerError("trial 缺少 actor：actor 来源必须可审计（AC22）")
        if not self.actor.get("source"):
            raise RunnerError("actor 缺少 source（cli/env/test）：来源不可审计")
        if not self.trial_id or "/" in self.trial_id or ".." in self.trial_id:
            raise RunnerError(f"非法 trial_id：{self.trial_id!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"trial_id": self.trial_id, "slot_id": self.slot_id,
                "batch_id": self.batch_id, "actor": dict(self.actor),
                "cache_policy": self.cache_policy,
                "session_id": self.session_id, "request_cap": self.request_cap}


def actor_record(name: str, *, source: str = "cli",
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """actor 取证：谁发起的 + 从哪来 + 机器侧可核对的事实。"""
    if source not in ("cli", "env", "test"):
        raise RunnerError(f"未声明的 actor source：{source}")
    rec = {"name": str(name), "source": source, "uid": os.getuid(),
           "host": socket.gethostname(), "pid": os.getpid(),
           "recorded_at_utc": _utc_now()}
    if extra:
        rec["extra"] = dict(extra)
    return rec


def new_session_id(trial_id: str) -> str:
    """每次 trial 一个新会话 id（AC22：新会话隔离，不复用上次上下文）。"""
    return sha256_of({"trial_id": trial_id, "nonce": os.urandom(16).hex()})[:32]


# ---------------------------------------------------------------- 组合根
class R0Runner:
    def __init__(self, campaign: Path | str, *,
                 faults: Mapping[str, str] | None = None,
                 clock: Callable[[], str] = _utc_now,
                 gateway: Any = None,
                 caps: ResourceCaps | None = None) -> None:
        self.campaign = Path(campaign).expanduser()
        self._bind_campaign(self.campaign)
        self.faults = dict(faults or {})
        unknown = sorted(set(self.faults) - set(FAULT_POINTS))
        if unknown:
            raise RunnerError(f"未声明的故障点：{unknown}（合法：{list(FAULT_POINTS)}）")
        self.clock = clock
        self.gateway = gateway
        self.caps = caps or ResourceCaps()
        self.root = rp.rsi_r0_campaign_root()

    # ---- 显式 R0 绑定（AC21） ----
    @staticmethod
    def _bind_campaign(campaign: Path) -> None:
        """把给定路径**落成** R0 环境并核对；解析到别处就拒。"""
        campaign = campaign.expanduser()
        if not campaign.is_absolute():
            raise RunnerError(f"--campaign 必须是绝对路径：{campaign}")
        os.environ["SIMPRAXIS_RSI_R0_ROOT"] = str(campaign.parent)
        os.environ["SIMPRAXIS_RSI_R0_CAMPAIGN"] = campaign.name
        resolved = rp.rsi_r0_campaign_root().resolve()
        if resolved != campaign.resolve():
            raise RunnerError(
                f"R0 路径解析到别处：{resolved} ≠ {campaign.resolve()}（显式 R0 失败）")
        rp.assert_rsi_r0_roots_isolated()

    # ---- campaign 落盘 ----
    def ensure_campaign(self) -> dict[str, Any]:
        """**先验证再创建**：隔离断言通过后才建目录（AC03）。

        只建六族骨架；已存在则只核对权限位，不做任何"修正"。
        """
        rp.assert_rsi_r0_roots_isolated()
        root = self.root
        created = False
        if not root.exists():
            root.mkdir(parents=True, mode=0o700)
            created = True
        os.chmod(root, 0o700)
        for fn in (rp.rsi_r0_protocol_dir, rp.rsi_r0_private_dir,
                   rp.rsi_r0_quota_dir, rp.rsi_r0_gateway_state_dir,
                   rp.rsi_r0_closeout_dir):
            fn()
        mode = root.stat().st_mode & 0o777
        if mode != 0o700:
            raise RunnerError(f"campaign 根权限 {oct(mode)} ≠ 0700：{root}")
        return {"campaign": str(root), "created": created, "mode": oct(mode)}

    # ---- 故障注入 ----
    def _maybe_fail(self, point: str, ctx: Mapping[str, Any]) -> None:
        if self.faults.get(point):
            raise InjectedFault(f"{point}: {self.faults[point]}")

    # ---- 路径 ----
    def trial_dir(self, trial_id: str) -> Path:
        d = rp.rsi_r0_runs_root() / trial_id
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def trial_state(self, trial_id: str) -> dict[str, Any] | None:
        p = self.trial_dir(trial_id) / "trial.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _write_trial(self, trial_id: str, doc: Mapping[str, Any]) -> dict[str, Any]:
        _atomic_write_json(self.trial_dir(trial_id) / "trial.json", doc)
        return dict(doc)

    # ---- preflight ----
    def preflight(self) -> dict[str, Any]:
        """零付费前置体检：路径、账套、封存、旧账拒写、控制矩阵冻结件。"""
        facts: dict[str, Any] = {}
        facts["campaign_root"] = str(self.root)
        facts["campaign_exists"] = self.root.is_dir()
        rp.assert_rsi_r0_roots_isolated()          # 失败即抛（不返回布尔量）
        facts["paths_isolated"] = True
        facts["ledger_path"] = str(rp.rsi_r0_ledger_path())
        facts["ledger_outside_old_roots"] = not any(
            str(rp.rsi_r0_ledger_path()).startswith(str(p))
            for p in rp.sealed_ledger_paths())
        # 旧账封存：写打开必须被拒（AC05 由 T1 保证，这里只读事实）
        from orchestrator.gateway.quota_ledger import LEDGER_RSI_R0, QuotaLedger
        from orchestrator.gateway.quota_ledger import read_lines
        ledger = QuotaLedger.rsi_r0()
        facts["ledger_namespace"] = ledger.ns.phase
        facts["ledger_namespace_ok"] = ledger.ns.phase == LEDGER_RSI_R0
        facts["ledger_rows"] = len(read_lines(ledger._ledger_path()))
        # 判据冻结件必须存在且与当前定义相等（AC20 的运行时检查）
        crit = rp.rsi_r0_protocol_dir() / "t3_criteria.json"
        facts["criteria_path"] = str(crit)
        facts["criteria_present"] = crit.exists()
        if crit.exists():
            doc = json.loads(crit.read_text(encoding="utf-8"))
            from orchestrator.rsi_r0.controls import criteria_hash
            from orchestrator.rsi_r0.instances import PLANS
            facts["criteria_hash"] = doc.get("criteria_hash")
            facts["criteria_matches_now"] = doc.get("criteria_hash") == criteria_hash(PLANS)
        # 发网事实账健康（有 UNKNOWN 就拒新付费批次）
        journal = AttemptJournal(rp.rsi_r0_attempts_path(),
                                 BudgetReservations(self.caps,
                                                    rp.rsi_r0_reservations_dir()))
        journal.recover()
        try:
            journal.assert_no_unknown()
            facts["no_unknown_attempts"] = True
        except RuntimeError as exc:
            facts["no_unknown_attempts"] = False
            facts["unknown_detail"] = str(exc)
        facts["image"] = IMAGE_REF
        facts["ok"] = all(bool(facts.get(k)) for k in (
            "campaign_exists", "paths_isolated", "ledger_outside_old_roots",
            "ledger_namespace_ok", "criteria_present", "criteria_matches_now",
            "no_unknown_attempts"))
        return facts

    # ---- 门 ----
    def gate_facts(self, gate: str, *, batch_id: str = "R0-P") -> dict[str, bool]:
        approval = rp.rsi_r0_approvals_dir() / f"{batch_id}.json"
        pre = self.preflight()
        journal = AttemptJournal(rp.rsi_r0_attempts_path())
        facts = {
            "budget_contract_approved": approval.exists(),
            "model_ids_approved": bool(os.environ.get("SIMPRAXIS_MODEL_IDS_APPROVED")),
            "cost_upper_bound_known": bool(os.environ.get("SIMPRAXIS_COST_UPPER_BOUND_USD")),
            "namespace_isolated": pre["ledger_outside_old_roots"],
            "seals_verified": bool(os.environ.get("SIMPRAXIS_SEALS_VERIFIED")),
            "approval_artifact": gates.approval_ok(approval, batch_id=batch_id),
            "no_unknown_attempts": pre["no_unknown_attempts"],
            "criteria_frozen": pre.get("criteria_matches_now", False),
            "lock_present": (rp.rsi_r0_protocol_dir() / "t4_lock.json").exists(),
            "gate_m_passed": gates.evaluate_gate("G-M", {
                "budget_contract_approved": approval.exists(),
                "model_ids_approved": bool(os.environ.get("SIMPRAXIS_MODEL_IDS_APPROVED")),
                "cost_upper_bound_known": bool(
                    os.environ.get("SIMPRAXIS_COST_UPPER_BOUND_USD")),
                "namespace_isolated": pre["ledger_outside_old_roots"],
                "seals_verified": bool(os.environ.get("SIMPRAXIS_SEALS_VERIFIED")),
            })["passed"],
        }
        facts["gate_p_passed"] = gates.evaluate_gate("G-P", facts)["passed"]
        del journal
        return facts

    def require_gate(self, gate: str, *, batch_id: str = "R0-P") -> dict[str, Any]:
        return gates.assert_gate(gate, self.gate_facts(gate, batch_id=batch_id))

    # ---- dry-run（零付费端到端） ----
    def dry_run(self, *, resume: bool = True, fresh: bool = False) -> dict[str, Any]:
        """mock 模型跑通 protocol→develop→validate→closeout，POST 恒 0。

        证据边界写死在报告里：`simulation_backend="mock"`、`layers_verified=[]`。
        这一趟证明的是**控制流**（顺序、门、记账、恢复、零发网），不证明任何能力。
        """
        if fresh:
            self._clear_dry_runs()
        actor = actor_record("dry-run", source="cli",
                                extra={"component": "R0Runner.dry_run"})
        plan = self._dry_plan()
        slots: list[dict[str, Any]] = []
        with campaign_lock(rp.rsi_r0_quota_dir()):
            for batch_id, slot_ids, stage in plan:
                for slot_id in slot_ids:
                    trial_id = f"dry_{batch_id}_{slot_id}"
                    prior = self.trial_state(trial_id)
                    if prior and prior.get("state") == SlotState.COMPLETED.value:
                        slots.append({**prior, "resumed": True})
                        continue
                    resume_from = None
                    if prior and resume:
                        resume_from = prior.get("state")
                    slots.append(self._dry_trial(trial_id, batch_id, slot_id, stage,
                                                 actor, resume_from=resume_from))
        posts = sum(int(s.get("posts", 0)) for s in slots)   # mock 恒 0，仍逐条汇总
        report = {
            "schema": "rsi_r0.dryrun.v1",
            "campaign": str(self.root),
            "created_at_utc": self.clock(),
            "actor": actor,
            "simulation_backend": "mock",
            "evidence_scope": "control_flow_only",
            "layers_verified": [],
            "posts": posts,
            "paid_usd": 0.0,
            "cache_policy": "replay_debug",
            "slots": slots,
            "batching": [{"batch_id": b, "real_batch": DRY_TO_REAL[b],
                          "slots": list(s), "stage": st} for b, s, st in plan],
            "resumable": True,
        }
        report["report_sha256"] = sha256_of(report)
        out = rp.rsi_r0_closeout_dir() / "dryrun_report.json"
        _atomic_write_json(out, report)
        return report

    def _dry_plan(self) -> list[tuple[str, Sequence[str], str]]:
        """dry 批次 = 正式批次前插 DRY：名字分得开，才不会把 mock 证据当正式试次。"""
        stage_of = {"R0-P": "protocol", "R0-D-E": "development",
                    "R0-D-D": "development", "R0-D-R": "development"}
        plan = []
        for real, (slots, _cap) in BATCH_SLOTS.items():
            plan.append((f"R0-DRY-{real[3:]}", slots,
                         stage_of.get(real, "validation")))
        return plan

    def _clear_dry_runs(self) -> None:
        runs = rp.rsi_r0_runs_root()
        if not runs.exists():
            return
        for d in sorted(runs.glob("dry_*")):            # 只清 dry 前缀，绝不清真实 trial
            for p in sorted(d.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            d.rmdir()

    def _dry_trial(self, trial_id: str, batch_id: str, slot_id: str, stage: str,
                   actor: Mapping[str, Any], *, resume_from: str | None) -> dict[str, Any]:
        spec = TrialSpec(trial_id=trial_id, slot_id=slot_id, batch_id=batch_id,
                         actor=actor, cache_policy="replay_debug",
                         session_id=new_session_id(trial_id),
                         request_cap=BATCH_SLOTS[DRY_TO_REAL[batch_id]][1])
        doc = {**spec.as_dict(), "stage": stage, "state": "RUNNING",
               "started_at_utc": self.clock(), "posts": 0, "resume_from": resume_from}
        self._write_trial(trial_id, doc)
        steps: list[str] = []
        try:
            self._maybe_fail("reserve", {"trial": trial_id})
            steps.append("reserve")
            self._maybe_fail("pre_send", {"trial": trial_id})
            steps.append("pre_send")
            answer = _mock_answer(slot_id, stage)
            posts = 0                                    # mock 从不发网
            steps.append("post_send")
            self._maybe_fail("post_send", {"trial": trial_id})
            self._maybe_fail("settle", {"trial": trial_id})
            steps.append("settle")
            self._maybe_fail("evaluate", {"trial": trial_id})
            verdict = {"execution": "COMPLETE", "scientific": "NOT_APPLICABLE",
                       "mock": True, "answer_keys": sorted(answer)}
            steps.append("evaluate")
            self._maybe_fail("report", {"trial": trial_id})
            steps.append("report")
        except InjectedFault as exc:
            doc.update({"state": "INTERRUPTED", "interrupted_at": str(exc),
                        "steps": steps, "updated_at_utc": self.clock()})
            self._write_trial(trial_id, doc)
            return doc
        doc.update({"state": SlotState.COMPLETED.value, "steps": steps,
                    "verdict": verdict, "posts": posts,
                    "updated_at_utc": self.clock()})
        return self._write_trial(trial_id, doc)

    # ---- 冻结快照锁（T4 第一版 LOCK） ----
    def lock_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "t4_lock.json"

    def lock_snapshot(self) -> dict[str, Any]:
        """完整快照：代码/配置/任务/判据/镜像/门定义。任一变化都不是同一个实验。

        **必须是确定性的**：同一棵树连算两次逐字节相同，否则 ``verify_lock`` 会在
        时间戳噪声上永远报漂移（那等于校验形同虚设）。写入时间戳由 ``write_lock``
        放在哈希之外单独记。
        """
        from orchestrator.rsi_r0 import controls as ctl
        from orchestrator.rsi_r0.instances import PLANS

        here = Path(__file__).resolve().parent
        code = {}
        for name in ("contracts", "instances", "scenes", "trusted", "service",
                     "evaluator", "controls", "attempts", "gates", "runner",
                     "reference_analysis", "faults", "image_pin"):
            p = here / f"{name}.py"
            code[name] = sha256_of(p.read_text(encoding="utf-8"))
        protocol = rp.rsi_r0_protocol_dir()
        tasks_lock = protocol / "t2_tasks_lock.json"
        criteria = protocol / "t3_criteria.json"
        stage_models = Path(__file__).resolve().parents[2] / "stage_models.yaml"
        return {
            "schema": "rsi_r0.lock.v1",
            "campaign": str(self.root),
            "image": IMAGE_REF,
            "code": code,
            "code_set_sha256": sha256_of(code),
            "stage_models_sha256": (sha256_of(stage_models.read_text(encoding="utf-8"))
                                    if stage_models.exists() else None),
            "tasks_set_sha256": (json.loads(tasks_lock.read_text(encoding="utf-8"))
                                 .get("tasks_set_sha256") if tasks_lock.exists() else None),
            "criteria_hash": (json.loads(criteria.read_text(encoding="utf-8"))
                              .get("criteria_hash") if criteria.exists() else None),
            "criteria_now": ctl.criteria_hash(PLANS),
            "gates": {g: gates.gate_definition_hash(g) for g in ("G-M", "G-P", "G-V")},
            "task_caps": {k.value: v for k, v in TASK_ID_FAMILY.items()},
            "slot_request_cap": SLOT_REQUEST_CAP,
            "instances": list(INSTANCE_IDS),
            "development_order": list(DEVELOPMENT_ORDER),
            "validation_order": list(VALIDATION_ORDER),
        }

    def write_lock(self) -> dict[str, Any]:
        """写一次即锁；已存在则只允许**逐字节同快照**的幂等重入，其余一律拒。"""
        snap = self.lock_snapshot()
        digest = sha256_of(snap)
        path = self.lock_path()
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            stored = {k: v for k, v in old.items()
                      if k not in ("created_at_utc", "lock_sha256")}
            if old.get("lock_sha256") != sha256_of(stored):
                raise RunnerError(
                    f"快照锁自哈希不符（文件被改过），拒绝覆盖：{path}")
            if old.get("lock_sha256") != digest:
                raise RunnerError(
                    "快照锁已存在且不同，拒绝覆盖（lock 只写一次；"
                    "确需新实验请另开 campaign）")
            return old
        doc = {**snap, "created_at_utc": self.clock(), "lock_sha256": digest}
        _atomic_write_json(path, doc)
        return doc

    def verify_lock(self) -> dict[str, Any]:
        """只校验，不回填：先查锁文件自身没被改，再逐项比对当前快照。"""
        path = self.lock_path()
        if not path.exists():
            raise RunnerError(f"快照锁不存在：{path}")
        old = json.loads(path.read_text(encoding="utf-8"))
        stored = {k: v for k, v in old.items()
                  if k not in ("created_at_utc", "lock_sha256")}
        drift: dict[str, Any] = {}
        if old.get("lock_sha256") != sha256_of(stored):
            drift["lock_sha256"] = {"locked": old.get("lock_sha256"),
                                    "now": sha256_of(stored), "why": "锁文件自哈希不符"}
        now = self.lock_snapshot()
        drift.update({k: {"locked": old.get(k), "now": now.get(k)}
                      for k in now if old.get(k) != now.get(k)})
        return {"ok": not drift, "drift": drift, "lock_sha256": old.get("lock_sha256")}

    # ---- 付费入口（T5 起接线；现在只做门与拒绝） ----
    def run_paid_slot(self, spec: TrialSpec) -> dict[str, Any]:
        if spec.cache_policy != "disabled":
            raise RunnerError("正式试次禁用缓存（AC22）")
        if not os.environ.get("SIMPRAXIS_ALLOW_PAID"):
            raise PaidPathNotArmed("SIMPRAXIS_ALLOW_PAID 未设置：付费入口拒开")
        self.require_gate("G-P", batch_id=spec.batch_id)
        if self.gateway is None:
            raise PaidPathNotArmed(
                "付费入口在 T5 接线：本阶段只交付门与拒绝路径（不静默空转）")
        raise PaidPathNotArmed("T5 才发网：T4 不实现真实请求构造")


# ---------------------------------------------------------------- mock 模型
_MOCK_ANSWERS: Mapping[str, Mapping[str, Any]] = {
    "P1": {"kind": "text", "stop_reason": "end_turn", "usage": {"in": 12, "out": 8}},
    "P2": {"kind": "text", "stop_reason": "end_turn", "usage": {"in": 12, "out": 8}},
    "P3": {"kind": "tool", "stop_reason": "tool_use", "usage": {"in": 12, "out": 8}},
    "P4": {"kind": "tool", "stop_reason": "tool_use", "usage": {"in": 12, "out": 8}},
}


def _mock_answer(slot_id: str, stage: str) -> dict[str, Any]:
    if stage == "protocol":
        return dict(_MOCK_ANSWERS[slot_id])
    return {"family": instance_family(slot_id).value, "run_id": "mock",
            "claim": {}, "mock": True, "stage": stage}


def slot_specs(batch_id: str, actor: Mapping[str, Any]) -> list[TrialSpec]:
    """按批次生成**显式** TrialSpec（每个 slot 一份，session 各新）。"""
    if batch_id not in BATCH_SLOTS:
        raise RunnerError(f"未声明的批次：{batch_id}")
    slots, cap = BATCH_SLOTS[batch_id]
    out = []
    for slot_id in slots:
        trial_id = f"{batch_id}_{slot_id}"
        out.append(TrialSpec(trial_id=trial_id, slot_id=slot_id, batch_id=batch_id,
                             actor=actor, cache_policy="disabled",
                             session_id=new_session_id(trial_id), request_cap=cap))
    return out


def instance_spec(instance_id: str) -> InstanceSpec | None:
    """预留的实例视图（T4 只核对 id 合法，任务书由 T2 冻结件提供）。"""
    if instance_id not in INSTANCE_IDS:
        return None
    return None


def trial_digest(spec: TrialSpec) -> str:
    return sha256_of(canonical_json(spec.as_dict()))
