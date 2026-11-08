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
from orchestrator.rsi_r0 import budget_contract, gates, seals
from orchestrator.rsi_r0.attempts import (
    AttemptJournal,
    BudgetReservations,
    _append_jsonl,
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


def write_artifact_with_history(path: Path, payload: Mapping[str, Any],
                                history: Path) -> dict[str, Any]:
    """写「最新件」，并把每个**不同版本**的哈希追加进只增日志（评审 N-6）。

    dry-run 报告与零付费结案件原先都是 ``write_text`` 覆盖——先跑出
    INVALID_INSTRUMENT、修好后再覆盖成干净件，审计上就只剩最后一份。现在最新件
    仍是那个固定文件名（下游读取路径不变），但每次内容变化都会在历史里留一行
    （时间/新哈希/被覆盖的旧哈希/主判），覆盖不等于抹除。
    """
    # 两个哈希必须同口径：调用方在**插入** ``report_sha256`` 之前算哈希（body 哈希），
    # 若旧件却按整文档哈希算，``changed`` 会恒为 True、（旧件+1 行的 previous）永不等于
    # 上一行的 sha256，历史行就不再能表达"这次到底变没变"（第三份评审 MINOR-1）。
    def _body_digest(doc: Mapping[str, Any]) -> str:
        return sha256_of({k: v for k, v in doc.items() if k != "report_sha256"})

    digest = str(payload.get("report_sha256") or _body_digest(payload))
    previous: str | None = None
    if path.exists():
        try:
            previous = _body_digest(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            previous = "unreadable"
    _atomic_write_json(path, payload)
    line = {"written_at_utc": _utc_now(), "artifact": path.name,
            "sha256": digest, "previous_sha256": previous,
            "changed": previous is not None and previous != digest,
            "primary_verdict": payload.get("primary_verdict"),
            "verdict": payload.get("verdict"),
            "posts": payload.get("posts")}
    _append_jsonl(history, line)
    return line


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
    #
    # 每条门事实都必须来自**产物**，不能来自「调用者说已经批准了」的环境变量：
    # 环境变量谁都能 export，批准件有自哈希与写一次锁，基线清单有逐件 sha256。
    #   model_ids_approved     → protocol/model_approval.json 的 stage_models_sha256
    #                            与当前 stage_models.yaml 逐字节相等（模型一改即失效）
    #   cost_upper_bound_known → protocol/pricing_contract.json 通过 contract_ok，且
    #                            本批批准件的 usd_ceiling 不低于最坏总量
    #   seals_verified         → 用 protocol/baseline_manifest.json 现算旧资产哈希
    def model_approval_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "model_approval.json"

    def gate_approval_path(self, batch_id: str) -> Path:
        return rp.rsi_r0_protocol_dir() / "approvals" / f"{batch_id}.json"

    def stage_models_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "gateway" / "stage_models.yaml"

    def current_stage_models(self) -> dict[str, Any]:
        import yaml
        doc = yaml.safe_load(self.stage_models_path().read_text(encoding="utf-8")) or {}
        return {k: (v or {}).get("model_id")
                for k, v in (doc.get("stage_models") or {}).items()}

    def model_ids_approved(self) -> tuple[bool, dict[str, Any]]:
        """模型 ID 批准事实：**批准件记录的配置哈希 == 当前配置哈希**（硬规则 1 机判）。"""
        p = self.model_approval_path()
        detail: dict[str, Any] = {"artifact": str(p), "artifact_present": p.exists()}
        if not p.exists():
            return False, {**detail, "why": "缺 model_approval.json（模型 ID 未登记批准）"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, {**detail, "why": f"批准件不可解析：{exc}"}
        body = {k: v for k, v in doc.items() if k != "approval_sha256"}
        detail["artifact_sha256"] = doc.get("approval_sha256")
        if sha256_of(body) != doc.get("approval_sha256"):
            return False, {**detail, "why": "批准件自哈希不符（被改过）"}
        cfg = self.stage_models_path()
        if not cfg.exists():
            return False, {**detail, "why": f"配置不存在：{cfg}"}
        now_hash = sha256_of(cfg.read_text(encoding="utf-8"))
        detail["stage_models_now_sha256"] = now_hash
        detail["stage_models_approved_sha256"] = doc.get("stage_models_sha256")
        if now_hash != doc.get("stage_models_sha256"):
            return False, {**detail,
                           "why": "stage_models.yaml 已改（模型变更须重新请示用户）"}
        approved = doc.get("stage_models") or {}
        now_models = self.current_stage_models()
        detail["stage_models_now"] = now_models
        mismatch = {k: {"approved": approved.get(k), "now": v}
                    for k, v in now_models.items() if approved.get(k) != v}
        if mismatch or set(approved) != set(now_models):
            return False, {**detail, "why": "模型 ID 与批准件不一致",
                           "mismatch": mismatch}
        if not str(doc.get("basis") or "").strip():
            return False, {**detail, "why": "批准件缺 basis（用户逐字裁决出处）"}
        return True, detail

    def baseline_manifest_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "baseline_manifest.json"

    def seals_verified(self) -> tuple[bool, dict[str, Any]]:
        verdict = seals.verify_baseline(self.baseline_manifest_path())
        return bool(verdict.get("ok")), verdict

    # ---- T3/T4 验收与独立评审：G-P 的产物条件（评审 MAJOR-2） ----
    #
    # 旧版 G-P 只认"有个批准件 + 有个锁"。campaign 自己的 gates.json 却写着
    # 「T0–T4 验收 / 独立评审」。两边不一致时能变绿的就是可执行的那一边——
    # 于是没跑控制矩阵、没做 dry-run、没人评审也能放行六次真实发网。以下三个
    # 事实只从**产物**推，缺件/自哈希不符/哈希对不上当前定义一律为假。
    def controls_report_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "t3_controls_report.json"

    def dryrun_report_path(self) -> Path:
        return rp.rsi_r0_closeout_dir() / "dryrun_report.json"

    def review_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "independent_review.json"

    def _self_hashed(self, doc: dict[str, Any], key: str = "report_sha256") -> bool:
        """产物自哈希：报告里记的哈希必须等于"去掉该键后整份文档"的哈希。"""
        body = {k: v for k, v in doc.items() if k != key}
        return doc.get(key) == sha256_of(body)

    def t3_acceptance(self) -> tuple[bool, dict[str, Any]]:
        """T3 控制矩阵是否真的跑齐：21 负控全检出、6 mutation 全杀、权限全 ok。"""
        from orchestrator.rsi_r0 import controls as ctl
        from orchestrator.rsi_r0.instances import PLANS

        p = self.controls_report_path()
        detail: dict[str, Any] = {"artifact": str(p), "artifact_present": p.exists()}
        if not p.exists():
            return False, {**detail, "why": "缺 t3_controls_report.json（控制矩阵没跑过）"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, {**detail, "why": f"报告不可解析：{exc}"}
        neg = doc.get("negatives") or {}
        mut = doc.get("mutations") or {}
        pos = doc.get("positives") or {}
        perm = doc.get("permissions") or {}
        facts = {
            "report_passed": doc.get("passed") is True,
            "report_self_hash": self._self_hashed(doc),
            "negatives_complete": (int(neg.get("total") or 0) == 21
                                   and int(neg.get("passed") or 0) == 21
                                   and not (neg.get("missed") or [])),
            "mutations_killed": (int(mut.get("total") or 0) == 6
                                 and int(mut.get("killed") or 0) == 6
                                 and not (mut.get("survivors") or [])),
            "positives_passed": (int(pos.get("total") or 0) > 0
                                 and int(pos.get("passed") or 0)
                                 == int(pos.get("total") or 0)),
            "permissions_ok": perm.get("ok") is True,
            # 报告必须是**当前**判据下的产物：改了判据还拿旧报告过门就是作弊。
            "criteria_matches_now": doc.get("criteria_hash") == ctl.criteria_hash(PLANS),
        }
        return all(facts.values()), {**detail, "facts": facts,
                                     "negatives": {"total": neg.get("total"),
                                                   "passed": neg.get("passed")},
                                     "mutations": {"total": mut.get("total"),
                                                   "killed": mut.get("killed")}}

    def t4_acceptance(self) -> tuple[bool, dict[str, Any]]:
        """T4 零付费端到端是否真的跑过：POST=0 且 13 个 slot 全 COMPLETED。"""
        p = self.dryrun_report_path()
        detail: dict[str, Any] = {"artifact": str(p), "artifact_present": p.exists()}
        if not p.exists():
            return False, {**detail, "why": "缺 dryrun_report.json（零付费端到端没跑过）"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, {**detail, "why": f"报告不可解析：{exc}"}
        slots = doc.get("slots") or []
        completed = [s for s in slots if s.get("state") == "COMPLETED"]
        # 期望的是**实例 slot**集合（13 个：P1–P4 + 9 个 E/D/R×dev/v1/v2），
        # 不是批次个数（10）。旧版拿 len(BATCH_SLOTS)=10 去比 13 个 slot，
        # 于是真 dry-run 报告永远过不了门，而只有恰好 10 条的假报告能过。
        expected_slots = {sid for slots_, _cap in BATCH_SLOTS.values() for sid in slots_}
        observed_ids = [s.get("slot_id") for s in slots]
        facts = {
            "report_self_hash": self._self_hashed(doc),
            "posts_zero": doc.get("posts") == 0,
            "paid_zero": float(doc.get("paid_usd") or 0.0) == 0.0,
            "backend_mock": doc.get("simulation_backend") == "mock",
            "slots_all_completed": (len(slots) == len(expected_slots)
                                    and len(completed) == len(slots)
                                    and set(observed_ids) == expected_slots),
        }
        return all(facts.values()), {**detail, "facts": facts,
                                     "slots": {"total": len(slots),
                                               "completed": len(completed)}}

    def independent_review(self) -> tuple[bool, dict[str, Any]]:
        """独立评审件：必须绑**当前 code 集**，verdict 必须是 APPROVE。

        绑定 ``code_set_sha256`` 而不是 git commit：评审件自身要提交，提交会改
        HEAD，绑 commit 就等于一提交就失效。绑代码集则精确表达"评审的是这份实现"，
        评审后任何代码改动都会让条件立刻变假，必须重新评审。
        """
        p = self.review_path()
        detail: dict[str, Any] = {"artifact": str(p), "artifact_present": p.exists()}
        if not p.exists():
            return False, {**detail, "why": "缺 independent_review.json（没有独立评审件）"}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, {**detail, "why": f"评审件不可解析：{exc}"}
        current = self.lock_snapshot().get("code_set_sha256")
        facts = {
            "self_hash": doc.get("review_sha256") == sha256_of(
                {k: v for k, v in doc.items() if k != "review_sha256"}),
            "verdict_approve": str(doc.get("verdict", "")).upper() == "APPROVE",
            "bound_to_current_code": doc.get("reviewed_code_set_sha256") == current,
            "blocking_findings_zero": int(doc.get("blocking_findings") or 0) == 0,
        }
        return all(facts.values()), {**detail, "facts": facts,
                                     "verdict": doc.get("verdict"),
                                     "reviewed_code_set_sha256": doc.get("reviewed_code_set_sha256"),
                                     "current_code_set_sha256": current}

    def write_independent_review(self, *, verdict: str, reviewer_lane: str,
                                 reviewed_commit: str, findings: dict[str, int],
                                 basis: str) -> dict[str, Any]:
        """写一次即锁的独立评审件（G-P 的 independent_review 条件读它）。"""
        if len((basis or "").strip()) < 20:
            raise RunnerError("basis 太短：请写清评审车道、被评范围与结论出处（≥20 字）")
        if not reviewer_lane.strip():
            raise RunnerError("reviewer_lane 不能为空")
        code_set = self.lock_snapshot().get("code_set_sha256")
        blocking = int(findings.get("major", 0)) + int(findings.get("critical", 0))
        doc = {
            "schema": "rsi_r0.independent_review.v1",
            "created_at_utc": self.clock(),
            "reviewer_lane": reviewer_lane.strip(),
            "reviewed_commit": reviewed_commit.strip(),
            "reviewed_code_set_sha256": code_set,
            "reviewed_lock_sha256": self.verify_lock().get("lock_sha256"),
            "verdict": verdict.strip().upper(),
            "findings": {k: int(v) for k, v in findings.items()},
            "blocking_findings": blocking,
            "basis": basis.strip(),
        }
        doc["review_sha256"] = sha256_of(doc)
        data = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = self.review_path()
        if path.exists():
            # 同内容幂等：``created_at_utc`` 与随之变化的 ``review_sha256`` 不算内容，
            # 否则同一份评审件在下一秒重写就会被当成「异内容」误拒（评审 N-3）。
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = None
            volatile = ("created_at_utc", "review_sha256")

            def _stable(d: Mapping[str, Any]) -> dict[str, Any]:
                return {k: v for k, v in d.items() if k not in volatile}

            if existing is None or _stable(existing) != _stable(doc):
                raise RunnerError(f"评审件已存在且内容不同，拒绝覆盖：{path}")
            # 幂等分支也做一次自哈希核对（第三份评审 LOW-6）：磁盘上的旧件若被
            # 手改成「内容一致但哈希不实」，不能原样返还当成有效评审件。
            body = {k: v for k, v in existing.items() if k != "review_sha256"}
            if existing.get("review_sha256") != sha256_of(body):
                raise RunnerError(f"评审件自哈希不符，拒绝复用：{path}")
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        return json.loads(data)

    def reported_ledger_seqs(self) -> list[tuple[str, list[int]]]:
        """从已落盘的成品报告里取回它们各自记录过的账本 seq（第四份痕迹）。

        评审 N-2：账本/attempts/预留**三份一起删**时对账曾返回真（"未开跑"与
        "抹掉证据"不可区分）。报告是被写一次即锁的成品件，里面的
        ``requests.ledger_seqs`` 不会跟着被删，所以把它当独立来源再核一遍。
        """
        proto = rp.rsi_r0_protocol_dir()
        out: list[tuple[str, list[int]]] = []
        for name, sources in (
            ("protocol", ("protocol_probe_report.json",)),
            ("development", ("development_report.json",)),
            ("validation", ("validation_report.json",)),
        ):
            seqs: list[int] = []
            for fname in sources:
                path = proto / fname
                if not path.exists():
                    continue
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                req = doc.get("requests") or {}
                seqs.extend(s for s in (req.get("ledger_seqs") or ())
                            if isinstance(s, int))
                # 开发报告的每族、验证报告的每个 slot 各自也记着 ledger_seqs。
                for key in ("families", "slots"):
                    for entry in doc.get(key) or ():
                        seqs.extend(s for s in (entry.get("ledger_seqs") or ())
                                    if isinstance(s, int))
                        seqs.extend(s for s in ((entry.get("requests") or {})
                                                .get("ledger_seqs") or ())
                                    if isinstance(s, int))
            if seqs:
                out.append((name, sorted(set(seqs))))
        return out

    def reconciled(self) -> tuple[bool, dict[str, Any]]:
        """AC31：账本/attempts/预留三方逐 request 对账（评审 MAJOR-5）。"""
        from orchestrator.rsi_r0 import reconcile as reconcile_mod

        journal = AttemptJournal(rp.rsi_r0_attempts_path(),
                                 BudgetReservations(self.caps, rp.rsi_r0_reservations_dir()))
        journal.recover()
        return reconcile_mod.reconcile_campaign(
            rp.rsi_r0_ledger_path(), journal, journal.reservations,
            report_seqs=self.reported_ledger_seqs())

    def lock_status(self) -> tuple[bool, dict[str, Any]]:
        try:
            verdict = self.verify_lock()
        except RunnerError as exc:
            return False, {"why": str(exc)}
        return bool(verdict.get("ok")), verdict

    def validation_snapshot_path(self) -> Path:
        return rp.rsi_r0_protocol_dir() / "validation_snapshot.json"

    def write_validation_snapshot(self) -> dict[str, Any]:
        """T6 交付的验证快照：代码/配置/任务/评测/镜像/判据 + 开发报告哈希。

        写入后**不接受**后续变更：``validation_snapshot_status`` 每次重算当前
        锁与报告哈希，任何一项对不上都算快照不成立（G-V 的 snapshot_frozen）。
        """
        report_path = rp.rsi_r0_protocol_dir() / "development_report.json"
        report = (json.loads(report_path.read_text(encoding="utf-8"))
                  if report_path.exists() else None)
        snap = {
            "schema": "rsi_r0.validation_snapshot.v1",
            "created_at_utc": self.clock(),
            "campaign": str(self.root),
            "lock": self.lock_snapshot(),
            "development_report_sha256": (report or {}).get("report_sha256"),
            "development_report_present": report is not None,
        }
        snap["snapshot_sha256"] = sha256_of(
            {k: v for k, v in snap.items() if k != "snapshot_sha256"})
        out = self.validation_snapshot_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(out, snap)
        return snap

    def validation_snapshot_status(self) -> tuple[bool, dict[str, Any]]:
        path = self.validation_snapshot_path()
        if not path.exists():
            return False, {"why": f"缺验证快照：{path}"}
        doc = json.loads(path.read_text(encoding="utf-8"))
        now = self.lock_snapshot()
        drift = [k for k in ("code_set_sha256", "code", "criteria_hash",
                             "stage_models_sha256", "tasks_set_sha256", "gates",
                             "pricing_contract_sha256", "image",
                             "develop_protocol_sha256")
                 if (doc.get("lock") or {}).get(k) != now.get(k)]
        report_path = rp.rsi_r0_protocol_dir() / "development_report.json"
        report_sha = (json.loads(report_path.read_text(encoding="utf-8"))
                      .get("report_sha256")) if report_path.exists() else None
        if doc.get("development_report_sha256") != report_sha:
            drift.append("development_report_sha256")
        if not report_sha:
            return False, {"why": "开发报告缺失，快照无法与报告对上", "drift": drift}
        return (not drift), {"why": "" if not drift else f"{len(drift)} 项漂移",
                             "drift": drift, "path": str(path),
                             "snapshot_sha256": doc.get("snapshot_sha256")}

    def gate_facts(self, gate: str, *, batch_id: str = "R0-P") -> dict[str, Any]:
        # 门侧批准件与账本侧批次票是**两个文件**：前者回答「这一批被授权了多少，
        # 币种与美元帽是什么」（写一次即锁），后者是账本 preflight 认的计费凭据。
        # 二者同批号、同 approved 数，但绝不共用一个文件名（否则互相覆盖）。
        approval = self.gate_approval_path(batch_id)
        approval_ok = gates.approval_ok(approval, batch_id=batch_id)
        approval_doc: dict[str, Any] = {}
        if approval_ok:
            try:
                approval_doc = gates.read_approval(approval)
            except gates.ApprovalError:
                approval_ok = False
        pre = self.preflight()
        model_ok, model_detail = self.model_ids_approved()
        contract = budget_contract.read_contract(
            rp.rsi_r0_protocol_dir() / "pricing_contract.json")
        contract_ok, contract_why = budget_contract.contract_ok(contract)
        worst_total = float((contract or {}).get("worst_case_total_usd") or 0.0)
        ceiling_ok = (worst_total > 0.0
                      and float(approval_doc.get("usd_ceiling") or 0.0) >= worst_total)
        seals_ok, seal_detail = self.seals_verified()
        lock_ok, lock_detail = self.lock_status()
        t3_ok, t3_detail = self.t3_acceptance()
        t4_ok, t4_detail = self.t4_acceptance()
        review_ok, review_detail = self.independent_review()
        recon_ok, recon_detail = self.reconciled()

        gm_facts = {
            "budget_contract_approved": approval_ok,
            "model_ids_approved": model_ok,
            "cost_upper_bound_known": contract_ok and ceiling_ok,
            "namespace_isolated": bool(pre.get("ledger_outside_old_roots")
                                       and pre.get("ledger_namespace_ok")),
            "seals_verified": seals_ok,
        }
        facts: dict[str, Any] = {
            **gm_facts,
            "gate_m_passed": gates.evaluate_gate("G-M", gm_facts)["passed"],
            "approval_artifact": approval_ok,
            "no_unknown_attempts": bool(pre.get("no_unknown_attempts")),
            "criteria_frozen": bool(pre.get("criteria_matches_now")),
            "lock_present": lock_ok,
            "t3_acceptance": t3_ok,
            "t4_acceptance": t4_ok,
            "independent_review": review_ok,
            "ledger_attempts_reconciled": recon_ok,
            "detail": {
                "batch_id": batch_id,
                "approval": {"path": str(approval), "ok": approval_ok,
                             "usd_ceiling": approval_doc.get("usd_ceiling")},
                "model_approval": model_detail,
                "pricing_contract": {"ok": contract_ok, "why": contract_why,
                                     "worst_case_total_usd": worst_total,
                                     "ceiling_covered_by_approval": ceiling_ok},
                "seals": seal_detail,
                "lock": lock_detail,
                "t3_acceptance": t3_detail,
                "t4_acceptance": t4_detail,
                "independent_review": review_detail,
                "reconcile": recon_detail,
            },
        }
        facts["gate_p_passed"] = gates.evaluate_gate("G-P", facts)["passed"]
        if gate == "G-V":
            # G-V 的三条非 G-P 条件全部由产物推：开发报告、开发门事实、验证快照。
            from orchestrator.rsi_r0 import develop as develop_mod
            report_path = rp.rsi_r0_protocol_dir() / "development_report.json"
            report = (json.loads(report_path.read_text(encoding="utf-8"))
                      if report_path.exists() else None)
            dev_facts = (develop_mod.gate_v_facts(report) if report
                         else {"development_report_present": False,
                               "development_gate_met": False})
            snap_ok, snap_detail = self.validation_snapshot_status()
            facts.update({**dev_facts, "snapshot_frozen": snap_ok,
                          "detail": {**facts.get("detail", {}),
                                     "development_report": {
                                         "path": str(report_path),
                                         "present": report is not None,
                                         "report_sha256": (report or {}).get("report_sha256")},
                                     "validation_snapshot": snap_detail}})
            facts["gate_v_passed"] = gates.evaluate_gate("G-V", facts)["passed"]
        return facts

    def require_gate(self, gate: str, *, batch_id: str = "R0-P") -> dict[str, Any]:
        return gates.assert_gate(gate, self.gate_facts(gate, batch_id=batch_id))

    def gates_state(self, *, write: bool = True) -> dict[str, Any]:
        """由**可执行门定义**生成 ``protocol/gates.json``（评审 MAJOR-2 的第二半）。

        旧版 gates.json 是手写的，写着「G-P 需要 T0–T4 验收 + 独立评审」，而
        ``gates.py`` 只认五条——两套清单并存时，能变绿的总是宽松的那一套。现在
        状态与条件清单都从 ``gate_facts`` 现算，手写件一次都留不下。
        """
        out: dict[str, Any] = {"schema": "rsi_r0.gates.v1",
                               "updated_at_utc": self.clock(),
                               "authority": "R0Runner.gates_state（与 gates.py 同源现算）"}
        passed_key = {"G-M": "gate_m_passed", "G-P": "gate_p_passed",
                      "G-V": "gate_v_passed"}
        for name in ("G-M", "G-P", "G-V"):
            facts = self.gate_facts(name)
            verdict = gates.evaluate_gate(name, facts)
            out[name] = {
                "state": "PASSED" if verdict["passed"] else "PENDING",
                "definition_hash": verdict["definition_hash"],
                "conditions": [{"key": r["key"], "ok": r["ok"],
                                "description": r["description"]} for r in verdict["conditions"]],
                "missing": verdict["missing"],
                "passed_key": passed_key[name],
            }
        out["unresolved"] = [f"{g}: {out[g]['missing']}"
                             for g in ("G-M", "G-P", "G-V") if out[g]["missing"]]
        if write:
            _atomic_write_json(rp.rsi_r0_protocol_dir() / "gates.json", out)
        return out

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
        # 评审 N-6：T4 报告也是可覆盖件——每次内容变化都留一行历史，覆盖≠抹除。
        write_artifact_with_history(out, report,
                                    out.parent / "dryrun_history.jsonl")
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
        from orchestrator.rsi_r0 import develop as develop_mod
        from orchestrator.rsi_r0.instances import PLANS

        here = Path(__file__).resolve().parent
        code = {}
        for name in ("contracts", "instances", "scenes", "trusted", "service",
                     "evaluator", "controls", "attempts", "gates", "runner",
                     "reference_analysis", "faults", "image_pin",
                     "budget_contract", "protocol_probe", "probe_tool", "seals",
                     "develop", "validate", "reconcile", "cli", "matrix"):
            p = here / f"{name}.py"
            code[name] = sha256_of(p.read_text(encoding="utf-8")) if p.exists() else None
        # 评审 MAJOR-1：入口、控制矩阵与**真正决定"发不发、发几次、记到哪"**的
        # 通用模块也必须是实验定义的一部分。旧版锁只收 rsi_r0/ 包内 19 个文件，
        # 于是把 router 改成重试 5 次、把 task0 帽改成 60、删掉 cli 里的门检查，
        # `lock --verify` 一律照样绿——那等于"悄悄改代码检不出"。
        for dotted, rel in {
            "gateway.router": ("gateway", "router.py"),
            "gateway.quota_ledger": ("gateway", "quota_ledger.py"),
            "common.runtime_paths": ("common", "runtime_paths.py"),
            "common.fsutil": ("common", "fsutil.py"),
        }.items():
            p = here.parent / rel[0] / rel[1]
            code[dotted] = sha256_of(p.read_text(encoding="utf-8")) if p.exists() else None
        # 同一逻辑的延伸：**产出"门要读的产物"的脚本**也属于实验定义。
        # 改了 t3_controls.py 就能让 21/0 的结论凭空成立，改了 admission.py 就能
        # 重造基线清单；两者若在锁外，"锁校验通过"就管不住门的输入。
        for dotted, rel in {
            "scripts.rsi_r0.admission": ("scripts", "rsi_r0", "admission.py"),
            "scripts.rsi_r0.t3_controls": ("scripts", "rsi_r0", "t3_controls.py"),
            # 评审 N-7：T2 的三个生成脚本以前不在锁内，改脚本而产物不变时
            # `lock --verify` 看不见（尤其 t2_golden.json 既不入锁也不被门读取，
            # 它却是 T2 参考基线）。三个脚本一律入锁。
            "scripts.rsi_r0.t2_build": ("scripts", "rsi_r0", "t2_build.py"),
            "scripts.rsi_r0.t2_convergence": ("scripts", "rsi_r0", "t2_convergence.py"),
            "scripts.rsi_r0.t2_reference": ("scripts", "rsi_r0", "t2_reference.py"),
        }.items():
            p = here.parent.parent.joinpath(*rel)
            code[dotted] = sha256_of(p.read_text(encoding="utf-8")) if p.exists() else None
        # 明确排除（第三份评审 LOW-5，这是选择不是遗漏）：
        # `scripts/rsi_r0/probes/probe_render.py` 是运维侧 GPU/EGL 探针，不进候选
        # 容器、不被任何门读取、仓库内无引用；把它钉进锁只会在换机器时制造假漂移。
        # 一旦它开始影响证据（例如探针结果进 gate_facts），必须改为入锁。
        protocol = rp.rsi_r0_protocol_dir()
        tasks_lock = protocol / "t2_tasks_lock.json"
        criteria = protocol / "t3_criteria.json"
        stage_models = here.parent / "gateway" / "stage_models.yaml"   # 唯一配置点
        from orchestrator.rsi_r0 import protocol_probe
        contract = budget_contract.read_contract(protocol / "pricing_contract.json")
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
            # §3.1：文本协议本体（工具解析/上下文/重试/错误回传策略）也是实验定义，
            # 协议一改就不是同一个实验，因此与代码/判据同级进 LOCK。
            "protocol_spec_sha256": protocol_probe.protocol_spec_hash(),
            "protocol_spec": dict(protocol_probe.PROTOCOL_SPEC),
            "develop_protocol_sha256": develop_mod.protocol_spec_hash(),
            "develop_protocol": dict(develop_mod.PROTOCOL_SPEC),
            # §6.2：资源与报价合同进 LOCK——不许开场后再悄悄改单价/帽。
            "pricing_contract_sha256": (contract or {}).get("contract_sha256"),
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

    # ---- 模型批准登记（硬规则 1 的产物化） ----
    def write_model_approval(self, *, decided_at_utc: str, basis: str,
                             approved_by: str = "user") -> dict[str, Any]:
        """把「用户已逐字裁决这三阶段模型」落成可机判产物（写一次即锁）。

        这不是授权模型变更的快捷方式：它记录的是**当前配置哈希**；日后任何改
        ``stage_models.yaml`` 的举动都会让 ``model_ids_approved`` 立刻变假，必须
        重新请示用户。``decided_at_utc`` 由调用方显式给出（裁决实际发生的时间），
        这样重复调用逐字节幂等。
        """
        if len(basis.strip()) < 20:
            raise RunnerError("basis 太短：请逐字记录用户裁决出处（≥20 字）")
        cfg = self.stage_models_path()
        if not cfg.exists():
            raise RunnerError(f"配置不存在：{cfg}")
        body = {
            "schema": "rsi_r0.model_approval.v1",
            "approved_by": approved_by,
            "decided_at_utc": decided_at_utc,
            "stage_models_sha256": sha256_of(cfg.read_text(encoding="utf-8")),
            "stage_models": self.current_stage_models(),
            "basis": basis.strip(),
            "note": ("硬规则 1：模型变更须停止→清点→用户批准；本件只记录既有裁决，"
                     "不构成任何变更许可。"),
        }
        body["approval_sha256"] = sha256_of(body)
        data = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = self.model_approval_path()
        if path.exists():
            if path.read_text(encoding="utf-8") != data:
                raise RunnerError(f"模型批准件已存在且内容不同，拒绝覆盖：{path}")
            return json.loads(data)
        os.makedirs(path.parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        return json.loads(data)

    def supersede_lock(self, reason: str) -> dict[str, Any]:
        """显式取代旧 LOCK：只在**尚无任何发网 attempt**时允许，旧件不删。

        旧件归档为 ``t4_lock.superseded.<sha8>.json`` 并追加 ``lock_history.jsonl``
        一行（时间/旧哈希/理由/新哈希）。理由 <20 字拒绝——取代 LOCK 等于改实验
        定义，留痕必须够读。已有发网 attempt 还取代 Lock 就不是修锁而是改实验，
        一律拒（那种事只能另开 campaign）。
        """
        if len((reason or "").strip()) < 20:
            raise RunnerError("取代理由太短：请写清为何旧 LOCK 需被取代（≥20 字）")
        path = self.lock_path()
        if not path.exists():
            raise RunnerError(f"没有可取代的快照锁：{path}")
        journal = AttemptJournal(rp.rsi_r0_attempts_path())
        sent = [r for r in journal.latest_by_request().values()
                if int(r.get("attempts_http") or 0) > 0]
        if sent:
            raise RunnerError(
                f"已有 {len(sent)} 条发网 attempt，禁止取代快照锁"
                "（那是改实验，不是修锁；须另开 campaign）")
        # 评审 MAJOR-5 残留：只看 attempts 不够——删掉 attempts.jsonl 就能让上面的
        # 守卫空转（账本里明明有付费 call 行）。取代锁前必须跑三方对账，任何漂移都拒。
        recon_ok, recon = self.reconciled()
        if not recon_ok:
            raise RunnerError(
                f"账本/attempts/预留对账存在漂移，禁止取代快照锁：{recon.get('why')}"
                "（取代 LOCK 只允许在「确实还没花过钱」时进行）")
        old = json.loads(path.read_text(encoding="utf-8"))
        old_sha = str(old.get("lock_sha256") or "")
        archive = path.parent / f"t4_lock.superseded.{old_sha[:8] or 'nosha'}.json"
        if archive.exists():
            raise RunnerError(f"归档件已存在，拒绝二次归档：{archive}")
        _atomic_write_json(archive, old)
        path.unlink()
        try:
            new = self.write_lock()
        except Exception:
            # 新锁写不出来就把旧锁放回去：宁可保留旧件，也不留无锁窗口。
            _atomic_write_json(path, old)
            raise
        line = {"superseded_at_utc": self.clock(), "old_lock_sha256": old_sha,
                "old_created_at_utc": old.get("created_at_utc"),
                "archive": archive.name, "reason": reason.strip(),
                "new_lock_sha256": new["lock_sha256"]}
        # 评审 MAJOR-6：取代理由曾经是手写的，于是出现过"声称 cli.py 已入锁、实际
        # 没入"这种自相矛盾。审计行里的 code 键差异一律**由代码生成**，手写部分
        # 只表达"为什么"，不表达"改了哪些"。
        old_code = old.get("code") or {}
        new_code = new.get("code") or {}
        added = sorted(set(new_code) - set(old_code))
        removed = sorted(set(old_code) - set(new_code))
        changed = sorted(k for k in set(old_code) & set(new_code)
                         if old_code[k] != new_code[k])
        line.update({"code_keys_added": added, "code_keys_removed": removed,
                     "code_keys_changed": changed,
                     "code_diff_generated_by": "R0Runner.supersede_lock",
                     "reconcile_ok_at_supersede": recon_ok,
                     "ledger_seqs_at_supersede": (recon.get("ledger") or {}).get("seqs")})
        line["reason"] = (f"{reason.strip()}；[机生成] code 键 "
                          f"+{added} ~{changed} -{removed}")
        hist = path.parent / "lock_history.jsonl"
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return {"superseded": old_sha, "archive": str(archive),
                "new_lock_sha256": new["lock_sha256"], "reason": reason.strip()}

    # ---- 付费入口 ----
    def run_paid_slot(self, spec: TrialSpec) -> dict[str, Any]:
        if spec.cache_policy != "disabled":
            raise RunnerError("正式试次禁用缓存（AC22）")
        if not os.environ.get("SIMPRAXIS_ALLOW_PAID"):
            raise PaidPathNotArmed("SIMPRAXIS_ALLOW_PAID 未设置：付费入口拒开")
        self.require_gate("G-P", batch_id=spec.batch_id)
        raise PaidPathNotArmed(
            "R0-P 的真实发网走 ProtocolProbe（至多 6 次、无重试、逐请求记账）；"
            "本方法只保留门与拒绝路径，不做第二条发网通路")


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
