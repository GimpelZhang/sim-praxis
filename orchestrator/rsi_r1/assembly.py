"""RSI-R1 T5：生产接缝装配（准入 / 预算槽 / 动作分派，AC24–AC27）。

本模块把 T1–T4 的零件装成**唯一**的生产路径：``ResearchLoop`` 只经由这里的
``CampaignAdmission``/``SlotBook``/``CampaignDispatcher`` 与 ``LockedSender``
组合；任何绕开 CLI 的装配也要先过同一准入与预算。

三条纪律：

* **准入**（:class:`CampaignAdmission`）只认持久事实：``contract/lock.json``
  的 sha、``quota/reservations_<lane>.jsonl`` 与 ``quota/ledger.jsonl`` 的实呼、
  ``state/pause.json`` 的暂停件、``ResearchState`` 的 root 阶段。
  ``LockedSender`` 会用 ``phase=LK.PHASE`` 再调一次，所以 ``check_all``
  **忽略**传入 phase、恒返回同一 Permit；lane 由
  :meth:`CampaignAdmission.enter_lane` 在轮次之间显式设定。
* **预算**（:class:`SlotBook`）按 Plan §8.1 的五 lane 槽表预留；同一
  ``call_id`` 重入返回同一槽（幂等），槽耗尽即 ``BUDGET_REFUSED`` 终止。
  预留只落 ``quota/reservations_<lane>.jsonl``，**不碰账本**——实呼账由
  生产网关写，本层绝不伪造。
* **分派**（:class:`CampaignDispatcher`）执行七个动作；每个动作的产物都经
  ``ResearchState`` 落成不可篡改事实（回复→动作→产物绑定）。确认批是
  **一次**原子批次：``begin_confirm`` → 一个 ``execute_batch`` →
  ``complete_confirm`` → 全批校验后 ``publish_confirm``。

容器执行仍由 ``sandbox``/``PhysicsBackend`` 负责；本模块只决定「跑什么」。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import repair as RPR
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import watchdog as W

ENVELOPE_SCHEMA = "simpraxis.rsi_r1.envelope.v1"
RESERVATION_DIR = "quota"
ANALYSIS_OUTPUT = "analysis.json"
MATRIX_OUTPUT = "matrix.json"
PUBLIC_CONTRACT_RELPATH = "public/public_contract.json"

MODE_PAID = "paid"
MODE_ZERO_PAID = "zero_paid"
SPEND_MODES = (MODE_PAID, MODE_ZERO_PAID)

STUDY_LANE = {"A": "study_A", "B": "study_B"}

#: lane → 轮次开始前必须已经到达的 root 阶段（§9 状态机）。
ROOT_GATE = {
    "probe": S.ROOT_FROZEN,
    "practice": S.ROOT_PROBED,
    "study_A": S.ROOT_PRACTICED,
    "study_B": S.ROOT_STUDY_A,
}
#: repair_reserve 的门位由 ``repair`` 模块单一真值给出（§8.1：只在正式 study
#: 首 POST **之前**、经新 root 授权后重做 probe8+practice16；储备不借给 study）。
REPAIR_ALLOWED_ROOTS = RPR.ALLOWED_ROOTS


class AssemblyError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _sha_doc(doc: Any) -> str:
    return hashlib.sha256(json.dumps(
        doc, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _require_sha256(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise AssemblyError("SHA_INVALID", f"{where}={value!r}")
    return value


def _write_private(path: Path, data: bytes) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Envelope / SpendContract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    envelope_id: str
    budget_sha256: str

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": ENVELOPE_SCHEMA, "schema_version": 1,
                "envelope_id": self.envelope_id,
                "budget_sha256": self.budget_sha256}

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Envelope":
        doc = ctx.read_json("contract/envelope.json")
        if doc.get("schema") != ENVELOPE_SCHEMA:
            raise AssemblyError("ENVELOPE_SCHEMA", repr(doc.get("schema")))
        return cls(envelope_id=C.require_safe_id(doc.get("envelope_id"),
                                                 where="envelope"),
                   budget_sha256=_require_sha256(
                       doc.get("budget_sha256"), where="envelope.budget_sha256"))


@dataclass(frozen=True)
class SpendContract:
    """费用门：``paid`` 必须带有效授权；``zero_paid`` 必须账本零实呼。

    两条都 fail-closed。零付费模式**不能**被用来夹带付费调用：准入每次都要
    复核账本仍为空；付费模式没有 consent 直接 ``PAID_NOT_AUTHORIZED``。
    """

    mode: str
    consent: "C.Consent | None" = None

    def __post_init__(self) -> None:
        if self.mode not in SPEND_MODES:
            raise AssemblyError("SPEND_MODE", repr(self.mode))
        if self.mode == MODE_PAID:
            if self.consent is None:
                raise AssemblyError("PAID_NOT_AUTHORIZED", "paid 模式缺 consent")
            C.assert_paid_authorized(self.consent)
        elif self.consent is not None:
            raise AssemblyError("SPEND_MODE", "zero_paid 模式不得携带 consent")

    def check(self, *, envelope: Envelope,
              ledger_rows: Sequence[Mapping[str, Any]]) -> None:
        if self.mode == MODE_PAID:
            assert self.consent is not None
            if self.consent.envelope_id != envelope.envelope_id:
                raise AssemblyError(
                    "CONSENT_STALE",
                    f"consent.envelope={self.consent.envelope_id!r} != "
                    f"{envelope.envelope_id!r}")
            return
        paid_rows = [r for r in ledger_rows if r.get("event", "call") == "call"]
        if paid_rows:
            raise AssemblyError(
                "SPEND_MODE_VIOLATION",
                f"zero_paid 模式下账本出现 {len(paid_rows)} 条实呼行")


# ---------------------------------------------------------------------------
# 准入
# ---------------------------------------------------------------------------

class CampaignAdmission:
    """LCM：持久事实 → Permit（无副作用、可重复调用、结果确定）。"""

    def __init__(self, *, ctx: Any, state: "S.ResearchState",
                 budget: C.BudgetSpec, lock_sha256: str,
                 spend: SpendContract, lane: str,
                 clock: Callable[[], str] = utc_now_iso_z) -> None:
        if lane not in LANES.LANE_ORDER:
            raise AssemblyError("LANE_UNKNOWN", repr(lane))
        self.ctx = ctx
        self.state = state
        self.budget = budget
        self.lock_sha256 = _require_sha256(lock_sha256, where="lock_sha256")
        self.spend = spend
        self.lane = lane
        self.clock = clock
        self._slots: "SlotBook | None" = None

    # -- lane 生命周期 -------------------------------------------------
    def enter_lane(self, lane: str) -> None:
        if lane not in LANES.LANE_ORDER:
            raise AssemblyError("LANE_UNKNOWN", repr(lane))
        self.lane = lane
        if self._slots is not None:
            self._slots.set_lane(lane)

    def attach_slots(self, book: "SlotBook") -> None:
        self._slots = book
        book.set_lane(self.lane)

    # -- 事实读取 -------------------------------------------------------
    def posts_used(self, lane: str) -> int:
        booked = 0 if self._slots is None else self._slots.count(lane)
        ledger = sum(1 for r in CL.ledger_rows(self.ctx)
                     if r.get("event", "call") == "call" and r.get("lane") == lane)
        return max(booked, ledger)

    def _require_root(self) -> None:
        root = self.state.root_state()
        if self.lane == "repair_reserve":
            # 单次授权件 + 修复门位（FROZEN/PROBED）；probe/practice 配对由
            # protocol._check_gate 另行复核（准入层只认 root 集合）。
            try:
                RPR.require_repair_gate(self.ctx, root=root)
            except RPR.RepairError as exc:
                raise L.LoopError(exc.code, exc.detail, terminal=True) from None
            return
        want = ROOT_GATE[self.lane]
        if root != want:
            raise L.LoopError("STATE_UNKNOWN",
                              f"lane {self.lane} 需要 root={want}，现为 {root}",
                              terminal=True)

    def check_all(self, *, envelope: Any, root_lock: Any, phase: str,
                  spend_contract: Any) -> L.Permit:
        """``phase`` 被忽略：Permit.phase 恒为 ``LK.PHASE``（见 ``LockedSender``）。

        ``spend_contract`` 同样被忽略——准入自带经费合同，避免「换件绕过」。
        """
        del phase, spend_contract
        # 1) 暂停件是唯一 dispatch 闸（AC24）：恢复必须显式 resume。
        pause = W.read_pause(self.ctx.subdir("state"))
        if pause is not None:
            raise L.LoopError("PAUSED",
                              f"暂停件在：{pause.get('reason')!r}", terminal=True)
        env_id = getattr(envelope, "envelope_id", None)
        if env_id is None and isinstance(envelope, Mapping):
            env_id = envelope.get("envelope_id")
        if env_id != self.ctx.envelope_id:
            raise L.LoopError("LOCK_MISMATCH", "envelope 与 campaign 不一致",
                              terminal=True)
        if not isinstance(root_lock, L.LockDocument):
            raise L.LoopError("LOCK_MISSING", "root_lock 不是 LockDocument",
                              terminal=True)
        if root_lock.envelope_id != self.ctx.envelope_id:
            raise L.LoopError("LOCK_MISMATCH", "LOCK.envelope 不一致",
                              terminal=True)
        if root_lock.sha256 != self.lock_sha256:
            raise L.LoopError("LOCK_MISMATCH", "LOCK sha 与冻结件不一致",
                              terminal=True)
        if root_lock.budget_sha256 != self.budget.sha256:
            raise L.LoopError("LOCK_MISMATCH", "预算 sha 与 LOCK 不一致",
                              terminal=True)
        # 2) 费用门（AC01）：每次都查，不只在入口查一次。
        try:
            self.spend.check(envelope=Envelope(self.ctx.envelope_id,
                                               self.budget.sha256),
                             ledger_rows=CL.ledger_rows(self.ctx))
        except AssemblyError as exc:
            raise L.LoopError(exc.code, exc.detail, terminal=True) from None
        # 3) 整轮硬帽（独立字面量 144）。
        calls = sum(1 for r in CL.ledger_rows(self.ctx)
                    if r.get("event", "call") == "call")
        if self._slots is not None:
            calls = max(calls, sum(self._slots.count(l)
                                   for l in LANES.LANE_ORDER))
        if calls >= LANES.RSI_R1_HARD_CAP:
            raise L.LoopError("BUDGET_REFUSED",
                              f"实呼/预留 {calls} 已达整轮帽 "
                              f"{LANES.RSI_R1_HARD_CAP}", terminal=True)
        # 4) root 阶段门（§9）。
        self._require_root()
        return L.Permit(envelope_id=self.ctx.envelope_id,
                        lock_sha256=self.lock_sha256,
                        budget_sha256=self.budget.sha256, phase=LK.PHASE)

    # -- lane 空间门（§8.2，在 freeze_confirm 兑现）---------------------
    def require_lane_space(self, *, confirm_runs: int,
                           replay_runs: int) -> "dict[str, Any]":
        """冻结确认集前检查本 lane 的 POST 余量与确认/复跑算力。

        * 正式 study（study_A/study_B）：本 lane 的 POST 预留后**至少余 8**，
          且 confirm+replay 在 ``budget.max_runs_by_lane`` 内；
        * probe：第 6 次之后至少余 2（8 呼链不能在第 7 呼后断粮）；
        * practice：冻结后至少余 2；repair_reserve 不得借给正式 study。
        """
        lane = self.lane
        cap = int(self.budget.posts_by_lane[lane])
        used = self.posts_used(lane)
        remaining = cap - used
        if lane in STUDY_LANE.values():
            if remaining < 8:
                raise L.LoopError(
                    "RESOURCE_EXHAUSTED",
                    f"{lane} 余 {remaining} POST < 冻结后所需的 8", terminal=True)
            for kind, need in (("confirm", confirm_runs),
                               ("replay", replay_runs)):
                room = int(self.budget.max_runs_by_lane[kind])
                if need > room:
                    raise L.LoopError(
                        "RESOURCE_EXHAUSTED",
                        f"{lane} 需要 {kind} {need} runs > 每 study 帽 {room}",
                        terminal=True)
        elif lane == "probe":
            if used >= 6 and remaining < 2:
                raise L.LoopError(
                    "RESOURCE_EXHAUSTED",
                    f"probe 已用 {used}，余 {remaining} < 2", terminal=True)
        elif lane == "practice":
            if remaining < 2:
                raise L.LoopError(
                    "RESOURCE_EXHAUSTED",
                    f"practice 冻结后余 {remaining} < 2", terminal=True)
        elif lane == "repair_reserve":
            # 修复重做在**同一 lane**里依次跑 probe8+practice16（共 24）；
            # 冻结之后仍须给本协议的 run_confirm/submit 留 2 呼。
            if remaining < 2:
                raise L.LoopError(
                    "RESOURCE_EXHAUSTED",
                    f"repair_reserve 冻结后余 {remaining} < 2", terminal=True)
        else:
            raise L.LoopError("LANE_UNKNOWN", f"{lane} 无冻结面", terminal=True)
        return {"lane": lane, "posts_used": used, "posts_remaining": remaining,
                "confirm_runs": confirm_runs, "replay_runs": replay_runs}


# ---------------------------------------------------------------------------
# 预算槽
# ---------------------------------------------------------------------------

class SlotBook:
    """按 lane 槽表预留 POST；幂等（同一 call_id 返回同一槽）。"""

    active_lane: str = "probe"

    def __init__(self, ctx: Any, budget: C.BudgetSpec, *,
                 clock: Callable[[], str] = utc_now_iso_z) -> None:
        self.ctx = ctx
        self.budget = budget
        self.clock = clock
        self._slots = budget.slots()
        self._cache: "dict[str, L.Reservation]" = {}

    def set_lane(self, lane: str) -> None:
        if lane not in LANES.LANE_ORDER:
            raise AssemblyError("LANE_UNKNOWN", repr(lane))
        self.active_lane = lane

    # -- 持久面 ---------------------------------------------------------
    def _path(self, lane: str) -> Path:
        return self.ctx.relative(f"{RESERVATION_DIR}/reservations_{lane}.jsonl")

    def rows(self, lane: str) -> "list[dict[str, Any]]":
        path = self._path(lane)
        if not path.exists():
            return []
        out: "list[dict[str, Any]]" = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def count(self, lane: str) -> int:
        return len(self.rows(lane))

    def _lane_slots(self, lane: str) -> "list[dict[str, Any]]":
        return [s for s in self._slots if s["lane"] == lane]

    # -- Budget 协议 -----------------------------------------------------
    def reserve_atomic(self, *, call_id: str,
                       permit: L.Permit) -> L.Reservation:
        cached = self._cache.get(call_id)
        if cached is not None:
            return cached
        lane = self.active_lane
        rows = self.rows(lane)
        for row in rows:
            if row.get("call_id") == call_id:
                res = L.Reservation(call_id=call_id, permit=permit,
                                    slot_id=str(row["slot_id"]))
                self._cache[call_id] = res
                return res
        slots = self._lane_slots(lane)
        if len(rows) >= len(slots):
            raise L.LoopError("BUDGET_REFUSED",
                              f"lane {lane} 槽耗尽（{len(slots)}）", terminal=True)
        slot = slots[len(rows)]
        slot_id = f"{lane}#{slot['lane_index']}"
        row = {"schema": "simpraxis.rsi_r1.reservation.v1", "schema_version": 1,
               "call_id": call_id, "lane": lane, "slot_id": slot_id,
               "slot": int(slot["slot"]),
               "lane_task_id": int(slot["lane_task_id"]),
               "lane_index": int(slot["lane_index"]),
               "reserve": bool(slot["reserve"]), "ts_iso": self.clock()}
        path = self._path(lane)
        ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)
        res = L.Reservation(call_id=call_id, permit=permit, slot_id=slot_id)
        self._cache[call_id] = res
        return res

    def resolve_or_pause(self, reply: L.Reply) -> None:
        if reply.is_actionable:
            return
        W.pause_dispatch(self.ctx.subdir("state"),
                         reason=reply.reason or "NOT_ACTIONABLE",
                         source="sender", now=self.clock)


# ---------------------------------------------------------------------------
# 分派器
# ---------------------------------------------------------------------------

class CampaignDispatcher:
    """七个动作的唯一执行面；每步只落可复核事实，不写科学内容。"""

    def __init__(self, *, ctx: Any, state: "S.ResearchState",
                 admission: CampaignAdmission, budget: C.BudgetSpec,
                 study: str, runner: Any, image: str,
                 backend_factory: Callable[[str], Any],
                 clock: Callable[[], str] = utc_now_iso_z) -> None:
        if study not in S.STUDIES:
            raise AssemblyError("STUDY_UNKNOWN", repr(study))
        self.ctx = ctx
        self.state = state
        self.admission = admission
        self.budget = budget
        self.study = study
        self.runner = runner
        self.image = image
        self.backend_factory = backend_factory
        self.clock = clock
        self.runs_used: "dict[str, int]" = {"explore": 0, "confirm": 0,
                                            "replay": 0}
        self.last_batch: "dict[str, Any] | None" = None
        self.confirm_manifest: "E.ConfirmManifest | None" = None
        self.confirm_plan_doc: "dict[str, Any] | None" = None
        self.confirm_artifacts: "dict[str, dict[str, Any]]" = {}

    # -- 公共只读面 ------------------------------------------------------
    @property
    def study_lane(self) -> str:
        return STUDY_LANE[self.study]

    def visible_state(self) -> "L.VisibleState":
        status = self.state.study_status(self.study)
        lock = status.get("confirm_lock") or {}
        used = self.admission.posts_used(self.admission.lane)
        cap = int(self.budget.posts_by_lane[self.admission.lane])
        return L.make_visible_state(
            study=self.study, study_state=str(status["state"]),
            artifacts=status["artifacts"], last_batch=self.last_batch,
            budget_slots_remaining=max(0, cap - used),
            confirm_token=lock.get("token"),
            plan_doc=self._latest_plan_doc())

    # -- 动作绑定 --------------------------------------------------------
    def _binding(self, action: L.Action) -> "tuple[str, str]":
        want = _sha_doc(action.to_doc())
        for ev in reversed(self.state.journal.events()):
            if ev.event == "action":
                # A/B 两条链共用一个 attempt 级日志：只认本 study 的动作事件，
                # 否则另一条链刚登记的动作会让本链的绑定校验恒失败。
                if ev.payload.get("study") not in (None, self.study):
                    continue
                if ev.payload.get("action_sha256") != want:
                    raise L.LoopError(
                        "STATE_UNKNOWN",
                        "最新动作事件与即将执行的动作不一致", terminal=True)
                return str(ev.payload["response_id"]), want
        raise L.LoopError("STATE_UNKNOWN", "没有已登记的动作事件", terminal=True)

    def _ok(self, **fields: Any) -> "dict[str, Any]":
        out = {"ok": True}
        out.update(fields)
        return out

    def _fail(self, code: str, message: str = "") -> "dict[str, Any]":
        return {"ok": False, "code": code, "message": message}

    # -- 入口 ------------------------------------------------------------
    def execute_once(self, action: L.Action,
                     store: "L.Store") -> "Mapping[str, Any]":
        del store
        handler = getattr(self, f"_do_{action.name}", None)
        if handler is None:
            raise L.LoopError("UNKNOWN_ACTION", action.name, terminal=True)
        try:
            return handler(action)
        except L.LoopError as exc:
            # 可自修语义（RUN_UNKNOWN/UNBOUND_SOURCE/PLAN_MISSING/…）必须以
            # **回包**交给候选，而不是逃出循环被驱动当成终止——一次字段手滑
            # 烧掉整条 study 的 48 呼。真正终止类（terminal=True）照旧上抛。
            if exc.terminal:
                raise
            return self._fail(exc.code, exc.detail)
        except (C.ContractError, E.ExperimentError, S.StateError,
                J.JudgeError, RP.ReplayError, CL.ClosureError,
                SB.SandboxError, O.ObservationError, AssemblyError) as exc:
            # 合同/状态类失败是**可自修**反馈：只回公开条款，不终止。
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._fail(str(code), str(exc))

    # -- 1) write_artifact ----------------------------------------------
    def _do_write_artifact(self, action: L.Action) -> "dict[str, Any]":
        response_id, action_sha = self._binding(action)
        doc = action.doc
        rec = self.state.register_artifact(
            study=self.study, kind=str(doc["kind"]), source=str(doc["source"]),
            response_id=response_id, action_sha256=action_sha)
        return self._ok(kind=rec.kind, source_sha256=rec.source_sha256,
                        producer=rec.producer)

    # -- 2) propose_plan -------------------------------------------------
    def _do_propose_plan(self, action: L.Action) -> "dict[str, Any]":
        response_id, action_sha = self._binding(action)
        registered: "list[dict[str, Any]]" = []
        for opt, kind in (("design_source", "design"),
                          ("analysis_source", "analysis")):
            if opt in action.doc:
                rec = self.state.register_artifact(
                    study=self.study, kind=kind,
                    source=str(action.doc[opt]), response_id=response_id,
                    action_sha256=action_sha)
                registered.append({"kind": kind,
                                   "source_sha256": rec.source_sha256})
        plan_doc = dict(action.doc["plan"])
        out = self.state.append_plan(self.study, plan_doc)
        return self._ok(plan_sha256=out["plan_sha256"], revision=out["revision"],
                        registered=registered, plan=plan_doc)

    # -- 3) run_batch ----------------------------------------------------
    def _do_run_batch(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        plan = self._current_plan(phase="explore")
        if str(action.doc["plan_sha256"]) != plan.sha256:
            return self._fail("PLAN_MISMATCH", "plan_sha256 不是当前修订")
        design_sha = _require_sha256(action.doc["design_sha256"],
                                     where="design_sha256")
        source = self._source_text(design_sha)
        inputs = self._design_inputs(plan)
        ev = SB.run_design(
            source, inputs, evidence_root=self.ctx.subdir("public"),
            work_root=self.ctx.subdir("trials") / f"design-{design_sha[:12]}",
            runner=self.runner, image=self.image, retain_bytes=True)
        if ev.exit_code != 0 or ev.timed_out:
            # 失败反馈必须带容器 stderr 尾巴（截断）：T6 首跑只给了 exit=1，
            # 候选/操作者都看不到 ``IndexError: sys.argv[1]``，定位靠离线复现。
            tail = " ".join(str(ev.stderr_tail or "").split())[-600:]
            return self._fail(
                "DESIGN_FAILED",
                f"exit={ev.exit_code} timeout={ev.timed_out} stderr={tail!r}")
        payload = ev.blobs.get(MATRIX_OUTPUT)
        if payload is None:
            return self._fail("MATRIX_MISSING", f"design 未写 {MATRIX_OUTPUT}")
        try:
            matrix = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._fail("MATRIX_INVALID", str(exc))
        specs = E.validate_matrix(matrix, plan, self.budget)
        explore_cap = int(self.budget.max_runs_by_lane["explore"]) \
            - self.runs_used["explore"]
        if len(specs) > explore_cap:
            return self._fail("RESOURCE_EXCEEDED",
                              f"{len(specs)} runs > 探索余量 {explore_cap}")
        backend = self.backend_factory(
            f"explore-{self.study}-{self.runs_used['explore']}")
        book = E.ResourceBook(max_runs=explore_cap,
                              max_steps=int(self.budget.max_steps))
        evidence = E.execute_batch(specs, backend, book)
        self.runs_used["explore"] += book.used_runs
        run_ids = [o.run_id for o in evidence.outcomes if o.ok]
        manifest = backend.write_manifest()
        self.last_batch = {
            "phase": "explore", "n_runs": len(run_ids),
            "blocked": bool(evidence.blocked),
            "manifest_sha256": None if manifest is None else _sha_doc(manifest),
            "run_ids": run_ids[:64]}
        if evidence.blocked:
            return self._fail("BATCH_BLOCKED",
                              "基础设施失败：整批 BLOCKED，未补样本")
        return self._ok(run_ids=run_ids, n_runs=len(run_ids), blocked=False)

    # -- 4) analyze ------------------------------------------------------
    def _do_analyze(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        source_sha = _require_sha256(action.doc["source_sha256"],
                                     where="source_sha256")
        source = self._source_text(source_sha)
        wanted = [str(r) for r in action.doc["run_ids"]]
        entries: "list[SB.ViewEntry]" = []
        for run_id in wanted:
            entries.extend(self._run_view_entries(run_id))
        ev = SB.run_analysis(
            source, entries, evidence_root=self.ctx.subdir("observations"),
            work_root=self.ctx.subdir("trials") / f"analysis-{source_sha[:12]}",
            runner=self.runner, image=self.image, retain_bytes=True)
        if ev.exit_code != 0 or ev.timed_out:
            # 与 DESIGN_FAILED 对称：代码报错必须带容器 stderr 尾巴，
            # 否则候选只能看到 exit=1 盲改（T6 已证明这是最大摩擦点）。
            tail = " ".join(str(ev.stderr_tail or "").split())[-600:]
            return self._fail(
                "ANALYSIS_FAILED",
                f"exit={ev.exit_code} timeout={ev.timed_out} stderr={tail!r}")
        payload = ev.blobs.get(ANALYSIS_OUTPUT)
        if payload is None:
            return self._fail("ANALYSIS_MISSING",
                              f"analysis 未写 {ANALYSIS_OUTPUT}")
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._fail("ANALYSIS_INVALID", str(exc))
        produced = doc.get("n_runs") if isinstance(doc, Mapping) else None
        if not isinstance(produced, int) or produced != len(wanted):
            return self._fail(
                "ANALYSIS_RUNS_MISMATCH",
                f"analysis.n_runs={produced!r} != 请求的 {len(wanted)} 个 run"
                "（观测视图是**平铺**的 `*.jsonl`，不是 `*/*.jsonl`）")
        rel = f"analysis/{source_sha[:12]}-{_sha_doc(doc)[:12]}.json"
        self.ctx.write_json(rel, doc)
        # 候选必须看到自己程序的真实输出才能写 claims（§8.1「submit 必须消费
        # 真实分析结果」）；这里回传的是宿主原样保存的同一份文档。
        return self._ok(analysis_relpath=rel, n_runs=len(wanted),
                        analysis=doc)

    # -- 5) freeze_confirm ----------------------------------------------
    def _do_freeze_confirm(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        plan_doc = dict(action.doc["plan"])
        # 锁内适配（RSI-R1.1 / L-R1-07）：``parent_sha`` 允许字面 sentinel
        # ``"host"``，宿主替换成当前探索计划的真实 sha；64 hex 则做**值**校验
        # （round 1 的 `SOURCE_SHA_INVALID` 纯格式闸就此变成可解释的
        # `PARENT_SHA_MISMATCH`）。两者都不放宽任何其它字段。
        expect_parent = self._explore_plan_sha()
        if expect_parent is None:
            # 没有探索计划时 confirm 的 parent_sha 无锚点：放开会让「未经探索
            # 校准的确认集」照跑照判。此前的 None==None 相等恰好放行了
            # parent_sha=null 的冻结，是科学链上的缺口。
            return self._fail(
                "PARENT_MISSING",
                "尚无探索计划：confirm 计划必须以探索计划的 sha 为 parent_sha"
                "（先 propose_plan + run_batch，或写 \"host\" 前提是已有探索计划）")
        if plan_doc.get("parent_sha") == L.HOST_PLAN_SHA:
            plan_doc["parent_sha"] = expect_parent
        plan = C.ResearchPlan.from_dict(plan_doc)
        if plan.phase != "confirm":
            return self._fail("PLAN_PHASE_INVALID", "freeze 需要 confirm 计划")
        if plan.study_id != self.study:
            return self._fail("STUDY_MISMATCH", plan.study_id)
        if plan.parent_sha != expect_parent:
            return self._fail(
                "PARENT_SHA_MISMATCH",
                f"parent_sha={plan.parent_sha} 不是当前探索计划的 sha "
                f"（{expect_parent}）；可写 \"host\" 由宿主代填")
        claimed = str(action.doc["plan_sha256"])
        if claimed != L.HOST_PLAN_SHA and plan.sha256 != claimed:
            return self._fail("PLAN_MISMATCH", "plan 文档 sha 与字段不符")
        design_sha = _require_sha256(action.doc["design_sha256"],
                                     where="design_sha256")
        analysis_sha = _require_sha256(action.doc["analysis_sha256"],
                                       where="analysis_sha256")
        # 确认集先由 host 私有 keeper seed 展开，算力门要按真实分配数判。
        # seed 只在冻结真正落成后才落盘：一次非法 freeze 尝试不应留下
        # 未使用的私有 seed（证据包里会出现无归属件）。
        seed, seed_path, seed_is_new = self._keeper_seed_for_freeze()
        manifest = E.allocate_confirm(plan, seed)
        n = manifest.n_assignments
        self.admission.require_lane_space(confirm_runs=n, replay_runs=n)
        lock = self.state.freeze_confirm(study=self.study, plan=plan.to_dict(),
                                         design_sha256=design_sha,
                                         analysis_sha256=analysis_sha)
        if seed_is_new:
            _write_private(seed_path, (seed + "\n").encode("utf-8"))
        # 冻结件只留 plan sha；确认计划原文落到 confirm/ 供重入与判据复算
        # （锁内适配：append_plan 只收 explore 修订，confirm 计划由冻结动作携带）。
        self.ctx.write_json(f"confirm/plan_{self.study}.json", plan.to_dict())
        self.confirm_plan_doc = plan.to_dict()
        self.confirm_manifest = manifest
        CL.write_confirm_manifest(self.ctx, self.study, manifest)
        return self._ok(confirm_token=lock.token, block_count=lock.block_count,
                        manifest_sha256=manifest.sha256, n_assignments=n,
                        plan_sha256=plan.sha256)

    # -- 6) run_confirm --------------------------------------------------
    def _do_run_confirm(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        token = str(action.doc["confirm_token"])
        manifest = self._frozen_manifest(token)
        plan = self._current_plan(phase="confirm")
        specs = J.build_specs(plan, manifest)
        ordered = [specs[rid] for rid in sorted(specs)]
        specs_sha = _sha_doc([{"run_id": s.run_id, "spec_hash": s.spec_hash}
                              for s in ordered])
        intent = self.state.begin_confirm(study=self.study, token=token,
                                          specs_sha256=specs_sha)
        room = int(self.budget.max_runs_by_lane["confirm"]) \
            - self.runs_used["confirm"]
        if len(ordered) > room:
            self.state.complete_confirm(intent_id=intent.intent_id,
                                        result_sha256=_sha_doc({"blocked": True}),
                                        outcome=S.OUTCOME_INFRA_FAIL)
            return self._fail("RESOURCE_EXHAUSTED",
                              f"确认批 {len(ordered)} > 余量 {room}")
        backend = self.backend_factory(confirm_batch_id(self.study, token))
        book = E.ResourceBook(max_runs=len(ordered),
                              max_steps=int(self.budget.max_steps))
        evidence = E.execute_batch(ordered, backend, book)
        self.runs_used["confirm"] += book.used_runs
        outcome = (S.OUTCOME_INFRA_FAIL if evidence.blocked else S.OUTCOME_OK)
        result_doc = {"study": self.study, "intent_id": intent.intent_id,
                      "runs": [_outcome_doc(o) for o in evidence.outcomes],
                      "blocked": bool(evidence.blocked)}
        result_sha = _sha_doc(result_doc)
        self.state.complete_confirm(intent_id=intent.intent_id,
                                    result_sha256=result_sha, outcome=outcome)
        if evidence.blocked:
            return self._fail("CONFIRM_BLOCKED",
                              "基础设施失败：整批 BLOCKED，不开第二批")
        manifest_out = backend.write_manifest()
        if manifest_out is None:
            return self._fail("MANIFEST_MISSING", "确认批没有观测 manifest")
        O.verify_manifest(manifest_out, self.ctx.subdir("observations")
                          / str(backend.batch_id))
        validated = int(manifest_out.get("count", -1)) == len(ordered)
        self.state.publish_confirm(intent_id=intent.intent_id,
                                   manifest_sha256=_sha_doc(manifest_out),
                                   validated=validated)
        artifacts = self._collect_confirm_artifacts(backend, ordered)
        self.confirm_artifacts = artifacts
        run_ids = sorted(artifacts)
        self.last_batch = {"phase": "confirm", "n_runs": len(run_ids),
                           "blocked": False, "run_ids": run_ids[:64],
                           "manifest_sha256": _sha_doc(manifest_out)}
        # 第 8 步 claims 要逐字段复现宿主从**确认批**重算的值，但候选从未见过
        # 确认批的数值（观测只在容器里、run_confirm 只回 id）：不把宿主用公开
        # 估计量算出的参考值回灌，submit 分支在物理上不可能通过。这里给的是
        # 「你自己这批观测的测量结果」，判据仍从 raw 独立重算并 1e-8 比对。
        references = J.reference_claims(plan=plan, manifest=manifest,
                                        artifacts=artifacts)
        return self._ok(run_ids_head=run_ids[:8], n_runs=len(ordered),
                        result_sha256=result_sha, references=references)

    # -- 7) submit_claim -------------------------------------------------
    def _do_submit_claim(self, action: L.Action) -> "dict[str, Any]":
        response_id, _ = self._binding(action)
        claims = [dict(c) for c in action.doc["claims"]]
        report_text = str(action.doc["report_text"])
        plan = self._current_plan(phase="confirm")
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        if manifest is None:
            return self._fail("CONFIRM_NOT_LOCKED", "缺确认 manifest")
        submission = {
            "schema": J.SUBMISSION_SCHEMA, "study_id": self.study,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": plan.primary.id, "claims": claims}
        # 先判后记：judge 抛错/判负都不应把状态推到 CLAIM_SUBMITTED——
        # 旧顺序（先 state.submit_claim 再判）会让一次 judge 异常把 study
        # 钉死在不可重试的终态。
        report = J.judge_study(plan=plan, manifest=manifest,
                               artifacts=self._confirm_artifacts(),
                               submission=submission)
        self.state.submit_claim(study=self.study, claims=claims,
                                report_text=report_text,
                                response_id=response_id)
        CL.write_claim_submission(self.ctx, self.study, submission)
        CL.write_judge_report(self.ctx, self.study, report)
        verdict = self._verdict(report)
        self.state.conclude(self.study, verdict,
                            reason=f"judge {report['verdict']}")
        return self._ok(judge_verdict=report["verdict"], verdict=verdict,
                        claim_status=report["layers"].get("claim_status"),
                        evidence_valid=report["layers"].get("evidence_valid"))

    def _verdict(self, report: Mapping[str, Any]) -> str:
        layers = report.get("layers") or {}
        if not layers.get("schema_accepted"):
            return S.ST_INVALID
        if not layers.get("evidence_valid"):
            return S.ST_INVALID
        if report.get("verdict") == "JUDGE_OK" \
                and layers.get("claim_status") == "MATCH":
            return S.ST_VERIFIED
        return S.ST_VALID_FAIL

    # -- 内部工具 --------------------------------------------------------
    def _current_plan(self, *, phase: str) -> C.ResearchPlan:
        if phase == "confirm":
            doc = self.confirm_plan_doc
            if doc is None and self.ctx.exists(f"confirm/plan_{self.study}.json"):
                doc = dict(self.ctx.read_json(f"confirm/plan_{self.study}.json"))
            if doc is None:
                raise L.LoopError("PLAN_MISSING", "尚无已冻结的确认计划")
            plan = C.ResearchPlan.from_dict(doc)
        else:
            doc = self._latest_plan_doc()
            if doc is None:
                raise L.LoopError("PLAN_MISSING", "尚无已登记计划")
            plan = C.ResearchPlan.from_dict(doc)
        if plan.phase != phase:
            raise L.LoopError("PLAN_PHASE_INVALID",
                              f"需要 phase={phase}，现为 {plan.phase}")
        if plan.study_id != self.study:
            raise L.LoopError("STUDY_MISMATCH", plan.study_id)
        return plan

    def _latest_plan_doc(self) -> "dict[str, Any] | None":
        latest = None
        for ev in self.state.journal.events():
            if ev.event == "plan_revision" \
                    and ev.payload.get("study") == self.study:
                latest = ev.payload.get("doc")
        return dict(latest) if isinstance(latest, Mapping) else None

    def _explore_plan_sha(self) -> "str | None":
        """当前最新探索计划的 canonical sha（confirm.parent_sha 的期望值）。"""
        latest = self._latest_plan_doc()
        if latest is None:
            return None
        try:
            plan = C.ResearchPlan.from_dict(latest)
        except C.ContractError:
            return None
        if plan.study_id != self.study or plan.phase != "explore":
            return None
        return plan.sha256

    def _source_text(self, source_sha: str) -> str:
        path = self.ctx.relative(f"designs/{source_sha}.py")
        if not path.is_file():
            raise L.LoopError("UNBOUND_SOURCE", f"{source_sha[:12]} 未登记")
        return path.read_text(encoding="utf-8")

    def _design_inputs(self, plan: C.ResearchPlan) -> "list[SB.ViewEntry]":
        plan_rel = f"public/plan-{plan.sha256[:12]}.json"
        plan_path = self.ctx.relative(plan_rel)
        if not plan_path.is_file():
            self.ctx.write_json(plan_rel, plan.to_dict())
        contract = self.ctx.relative(PUBLIC_CONTRACT_RELPATH)
        if not contract.is_file():
            raise L.LoopError("PUBLIC_CONTRACT_MISSING", PUBLIC_CONTRACT_RELPATH,
                              terminal=True)
        return [SB.ViewEntry(plan_path.name, _sha_file(plan_path)),
                SB.ViewEntry(contract.name, _sha_file(contract))]

    def _run_view_entries(self, run_id: str) -> "list[SB.ViewEntry]":
        base = self.ctx.subdir("observations")
        for path in sorted(base.glob(f"*/{run_id}.jsonl")):
            entries = [SB.ViewEntry(path.relative_to(base).as_posix(),
                                    _sha_file(path))]
            meta = path.with_name(f"{run_id}.meta.json")
            if meta.is_file():
                entries.append(SB.ViewEntry(
                    meta.relative_to(base).as_posix(), _sha_file(meta)))
            return entries
        raise L.LoopError("RUN_UNKNOWN", f"没有已发布观测 {run_id}")

    def _keeper_seed(self) -> str:
        path = self.ctx.relative(f"private/keeper_{self.study}.seed")
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        seed = secrets.token_hex(32)
        _write_private(path, (seed + "\n").encode("utf-8"))
        return seed

    def _keeper_seed_for_freeze(self) -> "tuple[str, Any, bool]":
        """读已有 seed，或生成一个**待落盘**的新 seed（返回 (seed, path, is_new)）。"""
        path = self.ctx.relative(f"private/keeper_{self.study}.seed")
        if path.is_file():
            return path.read_text(encoding="utf-8").strip(), path, False
        return secrets.token_hex(32), path, True

    def _frozen_manifest(self, token: str) -> "E.ConfirmManifest":
        lock = self.state.confirm_lock(self.study)
        if token != lock.token:
            raise L.LoopError("CONFIRM_TOKEN_MISMATCH", "token 与冻结件不符")
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        if manifest is None:
            raise L.LoopError("CONFIRM_NOT_LOCKED", "缺 manifest")
        return manifest

    def _frozen_manifest_raw(self) -> "E.ConfirmManifest | None":
        rel = f"confirm/manifest_{self.study}.json"
        if not self.ctx.exists(rel):
            return None
        # 单一解析点（``E.manifest_from_doc`` 会重算并比对 manifest_sha256）。
        return E.manifest_from_doc(self.ctx.read_json(rel))

    def _collect_confirm_artifacts(
            self, backend: Any,
            specs: Sequence[E.ExperimentSpec]) -> "dict[str, dict[str, Any]]":
        return collect_batch_artifacts(self.ctx.subdir("observations"),
                                       str(backend.batch_id), specs)

    def _confirm_artifacts(self) -> "dict[str, dict[str, Any]]":
        """确认批原件的按需读回（进程重入后内存为空时从落盘件恢复）。

        旧实现只在 ``run_confirm`` 内存里赋值；同 root 续跑/崩溃重入后
        ``submit_claim`` 拿空 artifacts 判 ``J-RUN-SET``，把本可验证的确认批
        记成仪器失败。这里与 T8 复跑同一条读回路径（manifest 先验、逐件 sha）。
        """
        if self.confirm_artifacts:
            return self.confirm_artifacts
        manifest = self._frozen_manifest_raw()
        if manifest is None:
            raise L.LoopError("CONFIRM_NOT_LOCKED", "缺 confirm/manifest 落盘件")
        plan = self._current_plan(phase="confirm")
        specs = J.build_specs(plan, manifest)
        lock = self.state.confirm_lock(self.study)
        batch = confirm_batch_id(self.study, lock.token)
        base = self.ctx.subdir("observations") / batch
        doc_manifest = base / "manifest.json"
        if not doc_manifest.is_file():
            raise L.LoopError("OBSERVATION_MISSING", str(doc_manifest))
        O.verify_manifest(json.loads(doc_manifest.read_text(encoding="utf-8")),
                          base)
        self.confirm_artifacts = collect_batch_artifacts(
            self.ctx.subdir("observations"), batch,
            [specs[rid] for rid in sorted(specs)])
        return self.confirm_artifacts


def confirm_batch_id(study: str, token: str) -> str:
    """确认批的观测批次名（token 前 12 位；T8 复跑按同一规则定位原件）。"""
    return f"confirm-{study}-{str(token)[:12]}"


def collect_batch_artifacts(
        observations_root: "Path | str", batch_id: str,
        specs: Sequence[E.ExperimentSpec]) -> "dict[str, dict[str, Any]]":
    """从观测目录读回 ``run_id → {"record", "meta"}``（T8 复跑的新进程入口）。

    ``read_record_jsonl`` 已强制「恰一行 + 顶层对象」，返回单条记录；早先
    按列表取 ``records[0]`` 会拿到键名（T5 排演暴露）。缺件 fail-closed。
    """
    out: "dict[str, dict[str, Any]]" = {}
    base = Path(observations_root) / str(batch_id)
    for spec in specs:
        jsonl = base / f"{spec.run_id}.jsonl"
        meta = base / f"{spec.run_id}.meta.json"
        if not jsonl.is_file() or not meta.is_file():
            raise L.LoopError("OBSERVATION_MISSING", str(jsonl))
        record = O.read_record_jsonl(jsonl)
        out[spec.run_id] = {
            "record": record,
            "meta": json.loads(meta.read_text(encoding="utf-8"))}
    return out


def _outcome_doc(outcome: E.RunOutcome) -> "dict[str, Any]":
    return {"run_id": outcome.run_id, "condition_id": outcome.condition_id,
            "block_id": outcome.block_id, "ok": bool(outcome.ok),
            "stop_reason": outcome.stop_reason, "error_code": outcome.error_code}


# ---------------------------------------------------------------------------
# 便利入口
# ---------------------------------------------------------------------------

def honest_zero_row_ledger(ctx: Any) -> Path:
    """零付费账本入口（委托 ``closure.ensure_ledger_file``，不在这里造行）。"""
    return CL.ensure_ledger_file(ctx)


def build_campaign_parts(
        ctx: Any, *, spend: SpendContract, lane: str, study: str,
        runner: Any, image: str, backend_factory: Callable[[str], Any],
        lock_sha256: str
) -> "tuple[CampaignAdmission, SlotBook, CampaignDispatcher, S.ResearchState]":
    """标准装配：状态 + 预算 + 准入 + 槽 + 分派（同 root 共享）。"""
    budget = C.BudgetSpec.from_dict(ctx.read_json("contract/budget.json"))
    state = S.ResearchState(ctx)
    admission = CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                  lock_sha256=lock_sha256, spend=spend, lane=lane)
    slots = SlotBook(ctx, budget)
    admission.attach_slots(slots)
    dispatcher = CampaignDispatcher(ctx=ctx, state=state, admission=admission,
                                    budget=budget, study=study, runner=runner,
                                    image=image, backend_factory=backend_factory)
    return admission, slots, dispatcher, state
