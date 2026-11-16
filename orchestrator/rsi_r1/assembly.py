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
from orchestrator.common import research_transport as RT
from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import closure as CL
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import design_intent as DI
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import judge as J
from orchestrator.rsi_r1 import judge_v13 as JV
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import observations as O
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import repair as RPR
from orchestrator.rsi_r1 import replay as RP
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import resources as RS
from orchestrator.rsi_r1 import sandbox as SB
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_r1 import watchdog as W

ENVELOPE_SCHEMA = "simpraxis.rsi_r1.envelope.v1"
RESERVATION_DIR = "quota"
ANALYSIS_OUTPUT = "analysis.json"
MATRIX_OUTPUT = "matrix.json"
PUBLIC_CONTRACT_RELPATH = "public/public_contract.json"

MODE_PAID = "paid"
MODE_ZERO_PAID = "zero_paid"
#: R1.2 T5 离线生产演练（Plan §4）：必须配 ``envelope.mode=offline_validation``，
#: 不得携带 consent，账本恒零实呼；与 ``zero_paid`` 分开，避免「零付费」被
#: 当成正式（清醒）模式的同义词。
MODE_OFFLINE_VALIDATION = "offline_validation"
SPEND_MODES = (MODE_PAID, MODE_ZERO_PAID, MODE_OFFLINE_VALIDATION)

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


# 生产者词汇两套：注册表（state.py）用 ``candidate_model`` 标注模型写出的源件，
# 凭据 schema（analysis_receipt）只认 ``candidate``/``maintainer``。凭据的构造
# （build_receipt）与复核（validate_receipt）必须经**同一映射**，否则要么
# PRODUCER_INVALID（2026-11-12 T5 离线演练首次走到 analyze 时实测），要么把
# 同一个差异误报成 PRODUCER_MISMATCH。未知词汇映射成空串 ⇒ 凭据侧 fail-closed。
RECEIPT_PRODUCERS = {S.PRODUCER_CANDIDATE: AR.PRODUCER_CANDIDATE,
                     S.PRODUCER_MAINTAINER: AR.PRODUCER_MAINTAINER}


def receipt_producer(art: Mapping[str, Any]) -> str:
    """注册表 producer 词汇 → 凭据词汇（**唯一映射点**，analyze/confirm/replay 共用）。

    锁内适配（2026-11-12 T5 全量演练）：replay 路径此前把注册表原词
    ``candidate_model`` 直塞 ``AR.build_receipt`` ⇒ PRODUCER_INVALID 处死
    整条 replay 链。三处构造 + 两处复核统一走本函数，未知词汇 ⇒ 空串
    （凭据侧 fail-closed，绝不默认放行）。
    """
    return RECEIPT_PRODUCERS.get(str(art.get("producer") or ""), "")


def analysis_host_inputs(ctx: Any, plan: C.ResearchPlan
                         ) -> "tuple[tuple[str, bytes], ...]":
    """分析视图的宿主机输入：``plan-<sha12>.json`` + ``public_contract.json``。

    锁内适配（2026-11-12 T5 全量演练）：生产 analyze 一直把这两件写进视图，
    但 replay 复跑路径没写 ⇒ 同一份冻结分析源码在复跑视图里 ``exit=1``
    （候选模板按公开合同从视图读计划来配对，缺计划只能 ``SystemExit``）。
    复跑必须喂**与生产同一份字节**，否则 replay 判的不是「同一分析重算」。
    唯一构造点：生产 ``_r12_analysis`` 与 ``replay_driver`` 共用本函数。
    """
    plan_rel = f"public/plan-{plan.sha256[:12]}.json"
    plan_path = ctx.relative(plan_rel)
    if not plan_path.is_file():
        ctx.write_json(plan_rel, plan.to_dict())
    contract = ctx.relative(PUBLIC_CONTRACT_RELPATH)
    if not contract.is_file():
        raise L.LoopError("PUBLIC_CONTRACT_MISSING", PUBLIC_CONTRACT_RELPATH,
                          terminal=True)
    return ((plan_path.name, plan_path.read_bytes()),
            (contract.name, contract.read_bytes()))


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


def read_confirm_output(ctx: Any, study: str) -> "tuple[bytes, str]":
    """按确认阶段分析指针取**原始输出字节**与错误码（凭据绑定面）。

    锁内适配（2026-11-12 T5 全量演练）：凭据 ``output_sha256`` 绑的是**沙盒
    原始输出字节**，``analysis/`` 下归档件是宿主规范化文档（contrasts 按计划
    重排、缩进不同），两者字节必然不同。旧实现拿归档件去校验凭据 ⇒ 恒
    ``OUTPUT_SHA_MISMATCH`` ⇒ study judge 以 ``INVALID_ANALYSIS`` 拒绝（drill16
    suite1 唯一失败点）。指针的 ``raw_relpath``/``raw_sha256`` 由
    ``_r12_analysis`` 写入私有区（0600）；读到与 sha 不符按漂移 fail-closed。
    """
    pointer_rel = f"private/analysis/{study}-confirm.json"
    if not ctx.exists(pointer_rel):
        return b"", "ANALYSIS_OUTPUT_MISSING"
    try:
        pointer = dict(ctx.read_json(pointer_rel) or {})
    except (OSError, ValueError):
        return b"", "ANALYSIS_OUTPUT_MISSING"
    raw_rel = str(pointer.get("raw_relpath") or "")
    out_rel = str(pointer.get("relpath") or "")
    try:
        data = ctx.relative(raw_rel or out_rel).read_bytes()
    except OSError:
        return b"", "ANALYSIS_OUTPUT_MISSING"
    expect_sha = str(pointer.get("raw_sha256") or "")
    if raw_rel and expect_sha and hashlib.sha256(data).hexdigest() != expect_sha:
        return b"", "ANALYSIS_OUTPUT_DRIFT"
    return data, ""


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
    #: R1.2 起 envelope 显式声明 ``paid`` | ``offline_validation``；legacy 文档
    #: 没有该键，解析为 ``paid``（行为逐字节不变）。
    mode: str = "paid"

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": ENVELOPE_SCHEMA, "schema_version": 1,
                "envelope_id": self.envelope_id,
                "budget_sha256": self.budget_sha256}

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Envelope":
        doc = ctx.read_json("contract/envelope.json")
        schema = doc.get("schema")
        if schema not in (ENVELOPE_SCHEMA, P.ENVELOPE_SCHEMA_V2,
                          P.ENVELOPE_SCHEMA_V13):
            raise AssemblyError(
                "ENVELOPE_SCHEMA",
                f"{schema!r} 不在（{ENVELOPE_SCHEMA!r}, {P.ENVELOPE_SCHEMA_V2!r}, "
                f"{P.ENVELOPE_SCHEMA_V13!r}）")
        return cls(envelope_id=C.require_safe_id(doc.get("envelope_id"),
                                                 where="envelope"),
                   budget_sha256=_require_sha256(
                       doc.get("budget_sha256"), where="envelope.budget_sha256"),
                   mode=str(doc.get("mode") or "paid"))


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
            C.assert_paid_authorized_any(self.consent)
        elif self.consent is not None:
            raise AssemblyError("SPEND_MODE",
                                f"{self.mode} 模式不得携带 consent")

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
        if self.mode == MODE_OFFLINE_VALIDATION and \
                getattr(envelope, "mode", "paid") != "offline_validation":
            raise AssemblyError(
                "OFFLINE_AUTH_REQUIRED",
                "offline_validation 费用模式要求 envelope.mode=offline_validation")
        paid_rows = [r for r in ledger_rows if r.get("event", "call") == "call"]
        if paid_rows:
            raise AssemblyError(
                "SPEND_MODE_VIOLATION",
                f"{self.mode} 模式下账本出现 {len(paid_rows)} 条实呼行")


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
        #    离线验证模式要求 envelope 自始声明 ``offline_validation``；
        #    这里从 envelope 文档读回 mode（不信任构造参数）。
        env_mode = str((self.ctx.read_json("contract/envelope.json") or {})
                       .get("mode") or "paid")
        try:
            self.spend.check(envelope=Envelope(self.ctx.envelope_id,
                                               self.budget.sha256,
                                               mode=env_mode),
                             ledger_rows=CL.ledger_rows(self.ctx))
        except AssemblyError as exc:
            raise L.LoopError(exc.code, exc.detail, terminal=True) from None
        # 3) 整轮硬帽（R1 独立字面量 144；R1.2 profile 预算为 124——一律取
        #    当前预算信封的 total_posts，单真值不另抄字面量）。
        calls = sum(1 for r in CL.ledger_rows(self.ctx)
                    if r.get("event", "call") == "call")
        if self._slots is not None:
            calls = max(calls, sum(self._slots.count(l)
                                   for l in tuple(self.budget.posts_by_lane)))
        if calls >= int(self.budget.total_posts):
            raise L.LoopError("BUDGET_REFUSED",
                              f"实呼/预留 {calls} 已达整轮帽 "
                              f"{int(self.budget.total_posts)}", terminal=True)
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
                 clock: Callable[[], str] = utc_now_iso_z,
                 profile: "P.CampaignProfile | None" = None,
                 resource_ledger: "RS.ResourceLedger | None" = None,
                 task: "TS.TaskSpecV13 | None" = None,
                 batch_workers: int = 1,
                 delivery_policy: str = RT.POLICY_LEGACY,
                 scope_contract: "G2.ScopeContractV1 | None" = None,
                 require_task_contrasts: bool = False) -> None:
        if study not in S.STUDIES:
            raise AssemblyError("STUDY_UNKNOWN", repr(study))
        # RSI-R1.2 接缝（T1）：传 profile rsi-r1.2 时必须同时给 campaign 级资源账；
        # 旧 profile/不传则维持 R1 行为，逐字节不变。
        if profile is not None and profile.is_r13:
            # R1.3：正式 study 必须绑定 TaskSpecV13；probe/practice 是玩具
            # 协议演练（R1.2 语义），允许 task=None。
            if task is None and admission.lane not in ("probe", "practice"):
                raise AssemblyError("TASK_SPEC_MISSING",
                                    "rsi-r1.3 正式 study 必须绑定 TaskSpecV13")
        elif profile is not None and profile.is_r12:
            if resource_ledger is None:
                raise AssemblyError(
                    "RESOURCE_LEDGER_MISSING",
                    "rsi-r1.2 分派器必须绑定 ResourceLedger（无法无账执行）")
            if resource_ledger.profile.profile_id != profile.profile_id:
                raise AssemblyError("PROFILE_MISMATCH",
                                    "资源账 profile 与分派器 profile 不一致")
        elif resource_ledger is not None:
            raise AssemblyError(
                "PROFILE_MISMATCH", "旧 profile 不得挂 R1.2 资源账")
        self.ctx = ctx
        self.state = state
        self.admission = admission
        self.budget = budget
        self.study = study
        self.runner = runner
        self.image = image
        self.backend_factory = backend_factory
        self.clock = clock
        self.profile = profile
        self.resource_ledger = resource_ledger
        self.task = task
        # R1.3 任务绑定仅在正式 study 成立；probe/practice 走 R1.2 语义。
        self.r13_bound = bool(profile is not None and profile.is_r13
                              and task is not None)
        # RSI-G2（Plan §2.4）：交付策略是冻结运行配置，缺任一接线即拒绝。
        if delivery_policy not in RT.DELIVERY_POLICIES:
            raise AssemblyError("POLICY_UNKNOWN", repr(delivery_policy))
        self.delivery_policy = delivery_policy
        self.g2 = delivery_policy == RT.POLICY_G2_ATOMIC
        self.scope_contract = scope_contract
        # RSI-G2.1（Plan §3.0 第 2 行 / AC G21-22）：必需对比全集由冻结 TaskSpec
        # 派生（主对比 + 阴性对照），不从候选 output 现有项反推。默认 False ⇒
        # G2 既有装配逐字不变；G2.1 证据桥显式打开。
        self.require_task_contrasts = bool(require_task_contrasts)
        if self.g2:
            if scope_contract is None:
                raise AssemblyError("POLICY_BINDING_MISSING",
                                    "g2_atomic_v1 必须显式绑定 ScopeContractV1")
            if not self.r13_bound:
                raise AssemblyError(
                    "POLICY_BINDING_MISSING",
                    "g2_atomic_v1 只用于 r13_bound 离线装配（需 r13 profile+TaskSpec）")
            try:
                scope_contract.validate()
            except G2.G2Error as exc:
                raise AssemblyError(exc.code, exc.detail) from None
        # 维护参考链可选并行（T5）；默认 1 保持生产逐 spec 语义
        self.batch_workers = max(1, int(batch_workers))
        self.r13_explore_intent: "DI.DesignIntentV13 | None" = None
        self.r13_confirm_intent: "DI.DesignIntentV13 | None" = None
        self.r13_design_counts: "dict[str, int] | None" = None
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
        if self.resource_ledger is not None:
            return self._visible_state_v2(status, lock, max(0, cap - used))
        return L.make_visible_state(
            study=self.study, study_state=str(status["state"]),
            artifacts=status["artifacts"], last_batch=self.last_batch,
            budget_slots_remaining=max(0, cap - used),
            confirm_token=lock.get("token"),
            plan_doc=self._latest_plan_doc())

    def _visible_state_v2(self, status: Mapping[str, Any],
                          lock: Mapping[str, Any], slots: int) -> "L.VisibleState":
        """R1.2 精确公开面：资源快照 + 动态 SHA 全来自宿主事实。"""
        # 配额按 (study, lane) 分域（见 resources._rows_for 的锁内适配注释）
        snap = self.resource_ledger.snapshot(self.study, lane=self.admission.lane)
        plan_doc = self._latest_plan_doc()
        plan_sha = None
        if plan_doc is not None:
            try:
                plan_sha = C.ResearchPlan.from_dict(plan_doc).sha256
            except C.ContractError:
                plan_sha = None
        artifacts = status["artifacts"]
        analyses = artifacts.get("analysis") or ()
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        phase = "confirm" if (lock or manifest is not None) else "explore"
        latest = {
            "plan_sha256": plan_sha,
            "analysis_source_sha256": str(analyses[-1]) if analyses else None,
            "confirm_manifest_sha256": None if manifest is None else manifest.sha256,
            # host nonce：随账本行/study 状态确定变化，不是可见面自身哈希。
            "nonce": _sha_doc({"study": self.study,
                               "study_state": str(status["state"]),
                               "phase": phase,
                               "ledger_rows": snap["ledger"]["rows"],
                               "ledger_head": snap["ledger"]["head_sha256"]}),
        }
        references = {"plan": plan_sha}
        confirm_remaining = None
        try:
            confirm_remaining = int((snap.get("confirm") or {}).get("remaining"))
        except (TypeError, ValueError):
            confirm_remaining = None
        return L.make_visible_state_v2(
            study=self.study, study_state=str(status["state"]),
            artifacts=artifacts, last_batch=self.last_batch,
            budget_slots_remaining=slots, confirm_token=lock.get("token"),
            phase=phase, resources=snap, latest_sha=latest,
            references=references, plan_doc=plan_doc,
            confirm_runs_remaining=confirm_remaining)

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

    def _fail(self, code: str, message: str = "", *,
              side_effect_status: str = "UNKNOWN") -> "dict[str, Any]":
        """失败回包。

        ``side_effect_status`` 是给 R1.2 有界自修判别用的**宿主证据**：
        前执行校验/沙盒内候选失败显式标 ``NONE``；缺省 ``UNKNOWN``（fail-closed，
        判别层据此直接 terminal，不让「不知道有没有执行」的失败被当可自修）。
        """
        return {"ok": False, "code": code, "message": message,
                "side_effect_status": side_effect_status}

    def _analysis_failure(self, summary: Mapping[str, Any], message: str,
                          *, side_effect_status: str = "KNOWN_COMPLETE"
                          ) -> "dict[str, Any]":
        """候选分析无效时的回包：**首个原因码若是 T0 注册的可恢复码就原样透出**。

        轮 M（R13-D7）：过去一律折成 ``ANALYSIS_FAILED``——该码不在可恢复白名单，
        于是「分析程序在空组上算出 NaN」这种合同内字段错误被当成终止类，烧掉整条
        study。宿主侧修复点是显式拒绝非有限数（NAN_OR_INF，早就在 T0 注册）；
        这里只做**码的透传**，不扩白名单、不改判定面。
        """
        tail = next((str(c) for c in (summary.get("reason_codes") or ())), "")
        code = tail if tail in P.RECOVERABLE_ERROR_CODES else "ANALYSIS_FAILED"
        return self._fail(code, f"{tail}; {message}" if tail else message,
                          side_effect_status=side_effect_status)

    # -- 入口 ------------------------------------------------------------
    def execute_once(self, action: L.Action,
                     store: "L.Store") -> "Mapping[str, Any]":
        del store
        handler = getattr(self, f"_do_{action.name}", None)
        if handler is None:
            raise L.LoopError("UNKNOWN_ACTION", action.name, terminal=True)
        # 轮 N（R13-D8）：8 位置协议在 R13 study 上不是「建议顺序」而是状态机。
        # 2026-11-15 轮 M study A 实证：run_confirm 后 study 已进 RESULTS_RELEASED
        # （只允许 submit_claim），但宿主仍放行 write_artifact/analyze，模型在
        # 「再修一次分析」里烧掉 39 呼直至 POSTS_EXHAUSTED、零主张提交。
        # 这里把状态机的唯一出口显式挡在入口：拒绝**非终止**（可自修），
        # 副作用 NONE（未写任何事件），但那一呼照常计账。
        if (self.profile is not None and self.profile.is_r13
                and self.study in S.STUDIES):
            # G2（Plan §2.4）：RESULTS_RELEASED 后的只读证据分页动作在**新注册表**
            # 里显式放行；旧 R13 仍只许 submit_claim（本分支不因 G2 放宽 legacy）。
            g2_read = self.g2 and action.name == "read_result_artifact"
            if (self.state.study_state(self.study) == S.ST_RESULTS_RELEASED
                    and action.name != "submit_claim" and not g2_read):
                return self._fail(
                    "CLAIM_REQUIRED",
                    "study 已在 RESULTS_RELEASED：第 8 位 submit_claim 是唯一"
                    f"合法动作；{action.name} 被拒（本呼照计预算）",
                    side_effect_status="NONE")
        try:
            return handler(action)
        except L.LoopError as exc:
            # 可自修语义（RUN_UNKNOWN/UNBOUND_SOURCE/PLAN_MISSING/…）必须以
            # **回包**交给候选，而不是逃出循环被驱动当成终止——一次字段手滑
            # 烧掉整条 study 的 48 呼。真正终止类（terminal=True）照旧上抛。
            if exc.terminal:
                raise
            return self._fail(exc.code, exc.detail)
        except S.StateError as exc:
            # StateError 来自 append_plan/freeze 等原子状态操作：拒绝路径不写
            # 任何事件（成功才 append），因此副作用恒为 NONE，候选可在同一
            # attempt 自修（R1.3 轮 J 实证：REVISION_GAP 曾误标 UNKNOWN）。
            return self._fail(str(exc.code), str(exc),
                              side_effect_status="NONE")
        except (C.ContractError, E.ExperimentError,
                J.JudgeError, RP.ReplayError, CL.ClosureError,
                SB.SandboxError, O.ObservationError, AssemblyError) as exc:
            # 合同/状态类失败是**可自修**反馈：只回公开条款，不终止。
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._fail(str(code), str(exc))

    # -- 1) write_artifact ----------------------------------------------
    def _do_write_artifact(self, action: L.Action) -> "dict[str, Any]":
        response_id, action_sha = self._binding(action)
        doc = action.doc
        if self._r12() and str(doc["kind"]) in ("design", "analysis") \
                and self.state.study_state(self.study) != S.ST_EXPLORING:
            # 冻结后换源码 = 换研究；R1.2 一律拒绝（legacy 语义不变）。
            return self._fail("SOURCE_FROZEN",
                              "确认冻结后不得再登记 design/analysis 源码",
                              side_effect_status="NONE")
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
        if self.r13_bound:
            raw_intent = action.doc.get("design_intent")
            # 兼容：模型常把 design_intent 塞进 plan 文档内（R1.3 轮 G 实证）。
            # 顶层字段优先；plan 内的键在合同校验前取出，语义不变。
            if raw_intent is None and "design_intent" in plan_doc:
                raw_intent = plan_doc.pop("design_intent")
            if raw_intent is None:
                return self._fail(
                    "DESIGN_INTENT_MISSING",
                    "R1.3 的 propose_plan 必须声明 design_intent"
                    "（固定点 pilot 也要显式声明范围）",
                    side_effect_status="NONE")
            try:
                intent = DI.parse_design_intent(raw_intent,
                                                profile=self.profile,
                                                task_spec=self.task)
            except DI.DesignError as exc:
                return self._fail(exc.code, exc.detail,
                                  side_effect_status="NONE")
            if intent.phase_role == "confirmation":
                return self._fail(
                    "INTENT_PHASE", "propose_plan 只接受 fixed_pilot/"
                    "distributional_exploration 意图；确认意图在 freeze_confirm 提交",
                    side_effect_status="NONE")
            self.r13_explore_intent = intent
        out = self.state.append_plan(self.study, plan_doc)
        if self.r13_bound:
            rel = f"design_audits/intent_explore_{out['plan_sha256'][:12]}.json"
            self.ctx.write_json(rel, self.r13_explore_intent.to_dict())
        return self._ok(plan_sha256=out["plan_sha256"], revision=out["revision"],
                        registered=registered, plan=plan_doc,
                        intent_sha256=(self.r13_explore_intent.sha256
                                       if self.r13_explore_intent else None))

    # -- 3) run_batch ----------------------------------------------------
    def _do_run_batch(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        plan = self._current_plan(phase="explore")
        if str(action.doc["plan_sha256"]) != plan.sha256:
            return self._fail("PLAN_MISMATCH", "plan_sha256 不是当前修订",
                              side_effect_status="NONE")
        design_sha = _require_sha256(action.doc["design_sha256"],
                                     where="design_sha256")
        source = self._source_text(design_sha)
        inputs = self._design_inputs(plan)
        if self.resource_ledger is not None:
            # 候选沙盒执行：即使设计脚本随后出错/矩阵被拒，这一次也计数。
            self.resource_ledger.reserve_sandbox(
                self.study, purpose="explore-design",
                lane=self.admission.lane)
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
                f"exit={ev.exit_code} timeout={ev.timed_out} stderr={tail!r}",
                side_effect_status="NONE")
        payload = ev.blobs.get(MATRIX_OUTPUT)
        if payload is None:
            return self._fail("MATRIX_MISSING", f"design 未写 {MATRIX_OUTPUT}",
                              side_effect_status="NONE")
        try:
            matrix = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._fail("MATRIX_INVALID", str(exc), side_effect_status="NONE")
        if self.r13_bound:
            # 前执行审计：声明 varying 却全用占位值必须拒绝（R1.2 §16 的教训）。
            if self.r13_explore_intent is None:
                return self._fail("DESIGN_INTENT_MISSING",
                                  "run_batch 前必须声明探索意图",
                                  side_effect_status="NONE")
            try:
                audit = DI.audit_design(
                    self.r13_explore_intent, task=self.task,
                    proposed_cells=matrix.get("cells") or [])
            except DI.DesignError as exc:
                return self._fail(exc.code, exc.detail,
                                  side_effect_status="NONE")
            self.ctx.write_json(
                f"design_audits/explore_pre_{_sha_doc(action.to_doc())[:12]}.json",
                audit.to_dict())
        if self.resource_ledger is not None:
            # 逐 cell 形状仍由 experiments.validate_matrix 单一实现裁决；
            # 累计 run/batch/步数/沙盒由资源账在 run_batch 前原子预留。
            # 锁内适配（Plan T5 恢复轨迹）：矩阵/资源前执行拒绝**没有物理副作用**，
            # 必须如实标 NONE，否则候选无法在同一 attempt 内自修（会被判 terminal）。
            try:
                specs, _res = RS.validate_matrix_v2(
                    matrix, plan, ledger=self.resource_ledger, study=self.study,
                    batch_id=f"explore-{_sha_doc(action.to_doc())[:16]}",
                    lane=self.admission.lane)
            except (RS.ResourceError, E.ExperimentError,
                    C.ContractError) as exc:
                code = getattr(exc, "code", None) or type(exc).__name__
                return self._fail(str(code), str(exc),
                                  side_effect_status="NONE")
            explore_cap = len(specs)
        else:
            try:
                specs = E.validate_matrix(matrix, plan, self.budget)
            except (E.ExperimentError, C.ContractError) as exc:
                code = getattr(exc, "code", None) or type(exc).__name__
                return self._fail(str(code), str(exc),
                                  side_effect_status="NONE")
            explore_cap = int(self.budget.max_runs_by_lane["explore"]) \
                - self.runs_used["explore"]
            if len(specs) > explore_cap:
                return self._fail("RESOURCE_EXCEEDED",
                                  f"{len(specs)} runs > 探索余量 {explore_cap}",
                                  side_effect_status="NONE")
        backend = self.backend_factory(
            f"explore-{self.study}-{self.runs_used['explore']}")
        book = E.ResourceBook(max_runs=explore_cap,
                              max_steps=int(self.budget.max_steps))
        evidence = E.execute_batch(specs, backend, book,
                                   workers=self.batch_workers)
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
        if self._r12() or (self.profile is not None and self.profile.is_r13):
            return self._r12_analyze(action)
        self._binding(action)
        source_sha = _require_sha256(action.doc["source_sha256"],
                                     where="source_sha256")
        source = self._source_text(source_sha)
        wanted = [str(r) for r in action.doc["run_ids"]]
        entries: "list[SB.ViewEntry]" = []
        for run_id in wanted:
            entries.extend(self._run_view_entries(run_id))
        if self.resource_ledger is not None:
            # analysis 容器同样计数（物理 step=0 也不免费）。
            self.resource_ledger.reserve_sandbox(
                self.study, purpose="analysis", lane=self.admission.lane)
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
                f"exit={ev.exit_code} timeout={ev.timed_out} stderr={tail!r}",
                side_effect_status="NONE")
        payload = ev.blobs.get(ANALYSIS_OUTPUT)
        if payload is None:
            return self._fail("ANALYSIS_MISSING",
                              f"analysis 未写 {ANALYSIS_OUTPUT}",
                              side_effect_status="NONE")
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._fail("ANALYSIS_INVALID", str(exc), side_effect_status="NONE")
        produced = doc.get("n_runs") if isinstance(doc, Mapping) else None
        if not isinstance(produced, int) or produced != len(wanted):
            return self._fail(
                "ANALYSIS_RUNS_MISMATCH",
                f"analysis.n_runs={produced!r} != 请求的 {len(wanted)} 个 run"
                "（观测视图是**平铺**的 `*.jsonl`，不是 `*/*.jsonl`）",
                side_effect_status="NONE")
        rel = f"analysis/{source_sha[:12]}-{_sha_doc(doc)[:12]}.json"
        self.ctx.write_json(rel, doc)
        # 候选必须看到自己程序的真实输出才能写 claims（§8.1「submit 必须消费
        # 真实分析结果」）；这里回传的是宿主原样保存的同一份文档。
        return self._ok(analysis_relpath=rel, n_runs=len(wanted),
                        analysis=doc)

    # -- R1.2 分析凭据（T4） --------------------------------------------
    def _r12(self) -> bool:
        return self.profile is not None and bool(
            getattr(self.profile, "is_r12", False))

    def _artifact_event(self, source_sha: str) -> "dict[str, Any]":
        for ev in reversed(self.state.journal.events()):
            if ev.event == "artifact" \
                    and ev.payload.get("source_sha256") == source_sha:
                return dict(ev.payload)
        raise L.LoopError("UNBOUND_SOURCE", f"{source_sha[:12]} 未登记",
                          terminal=True)

    def _analysis_source(self, source_sha: str) -> str:
        text = self._source_text(source_sha)
        actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual != source_sha:
            raise L.LoopError("SOURCE_FILE_DRIFT",
                              "登记源码与磁盘字节不一致", terminal=True)
        return text

    def _analysis_context(self, phase: str,
                          allowed: Sequence[str]) -> "SB.AnalysisContext":
        limits = self.profile.resource_limits
        if phase == "explore":
            cap = int(limits.explore_runs_per_study)
        elif phase == "confirm":
            cap = int(limits.confirm_runs_per_study)
        elif phase == "replay":
            cap = int(limits.replay_runs_per_study)
        else:
            raise AssemblyError("PHASE_INVALID", repr(phase))
        return SB.AnalysisContext(phase=phase, max_runs=cap,
                                  allowed_run_ids=tuple(sorted(allowed)))

    def _receipt_rel(self, study: str, phase: str) -> str:
        # host 私有凭据：0600，候选不可写；固定文件名按 study/phase 唯一。
        return f"private/receipts/{study}-{phase}.json"

    def _store_receipt(self, receipt: Mapping[str, Any]) -> "dict[str, Any]":
        rel = self._receipt_rel(str(receipt["study_id"]), str(receipt["phase"]))
        data = (json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True,
                           indent=1, allow_nan=False) + "\n").encode("utf-8")
        _write_private(self.ctx.relative(rel), data)
        return {"relpath": rel, "sha256": AR.sha256_bytes(data),
                "validity": bool(receipt.get("validity")),
                "phase": receipt.get("phase"),
                "reason_codes": list(receipt.get("reason_codes") or [])}

    @staticmethod
    def _entry_run_id(entry: "SB.ViewEntry") -> str:
        name = Path(entry.relpath).name
        if name.endswith(".meta.json"):
            return name[: -len(".meta.json")]
        if name.endswith(".jsonl"):
            return name[: -len(".jsonl")]
        return name

    @classmethod
    def _entries_run_ids(cls, entries: Sequence["SB.ViewEntry"]) -> "list[str]":
        return sorted({cls._entry_run_id(e) for e in entries})

    @classmethod
    def _entry_inputs(cls, entries: Sequence["SB.ViewEntry"]
                      ) -> "list[dict[str, Any]]":
        return [{"run_id": cls._entry_run_id(entry),
                 "relative_path": entry.relpath, "sha256": entry.sha256}
                for entry in entries]

    def _r12_plan_contrast_ids(self, plan: C.ResearchPlan) -> "list[str]":
        ids = [plan.primary.id]
        if plan.negative_control is not None:
            ids.append(plan.negative_control.id)
        return ids

    def _r12_analysis(self, *, phase: str, source_sha: str,
                      run_ids: Sequence[str], plan: C.ResearchPlan,
                      manifest_sha: "str | None",
                      expected_contrasts: "Sequence[str] | None" = None
                      ) -> "tuple[dict[str, Any], dict[str, Any], bytes]":
        """执行冻结分析并落凭据；返回 ``(output_doc, receipt_summary, bytes)``。

        预执行拒绝（视图/作用域）抛 ``SandboxError``；执行后失败由调用方按
        ``SandboxError`` 转成公开回包。凭据只在结构/身份/执行全过时才 validity。
        """
        entries: "list[SB.ViewEntry]" = []
        for run_id in run_ids:
            entries.extend(self._run_view_entries(run_id))
        context = self._analysis_context(phase, run_ids)
        text = self._analysis_source(source_sha)
        # 锁内适配（T5）：分析视图必须带上公开合同承诺的 plan-<sha12>.json 与
        # public_contract.json。R1.2 的候选分析要按计划里的对比定义配对（run
        # meta 只有 condition_id，没有主张/δ），视图缺计划则合规脚本只能瞎猜。
        # 这两件由**宿主**按 `_design_inputs` 的同一份字节写入（候选不可覆盖）。
        extra_files = analysis_host_inputs(self.ctx, plan)
        if self.r13_bound:
            # R1.3：分析视图额外放任务公开件（条件/主对比/nuisance/阴性控制）。
            task_view = {
                "task_id": self.task.task_id,
                "study_id": self.task.mechanism_id.split("_")[0],
                "conditions": [c.to_dict() for c in self.task.conditions],
                "primary": self.task.primary.to_dict(),
                "nuisance_schema": dict(self.task.nuisance_schema),
                "negative_control_spec": self.task.negative_control_spec,
            }
            extra_files = extra_files + ((
                "task.json",
                (json.dumps(task_view, sort_keys=True, ensure_ascii=False)
                 + "\n").encode("utf-8")),)
        if self.resource_ledger is not None:
            self.resource_ledger.reserve_sandbox(
                self.study, purpose=f"analysis-{phase}",
                lane=self.admission.lane)
        ev = SB.run_analysis(
            text, entries, evidence_root=self.ctx.subdir("observations"),
            work_root=self.ctx.subdir("trials") / f"analysis-{phase}-{source_sha[:12]}",
            runner=self.runner, image=self.image, retain_bytes=True,
            analysis_context=context, extra_files=extra_files)
        payload = ev.blobs.get(ANALYSIS_OUTPUT)
        output_doc: "dict[str, Any] | None" = None
        reasons: "list[str]" = []
        if ev.timed_out:
            reasons.append("ANALYSIS_TIMEOUT")
        if int(ev.exit_code) != 0:
            reasons.append("ANALYSIS_EXIT_NONZERO")
        if payload is None:
            reasons.append("ANALYSIS_OUTPUT_MISSING")
        else:
            try:
                raw_doc = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                reasons.append("ANALYSIS_OUTPUT_BAD_JSON")
            else:
                wanted = (list(expected_contrasts)
                          if expected_contrasts is not None
                          else self._r12_plan_contrast_ids(plan))
                try:
                    parsed = AR.parse_output(
                        raw_doc, expected_run_ids=run_ids,
                        expected_contrasts=wanted)
                    output_doc = dict(raw_doc)
                    output_doc["contrasts"] = [
                        parsed["contrasts"][cid] for cid in wanted]
                except AR.ReceiptError as exc:
                    reasons.append(exc.code)
                else:
                    # 轮 M（R13-D7）：候选分析文档的**对比字段**由 parse_output
                    # 查过有限性，但块表/覆盖率/诊断等自由字段没有——2026-11-15
                    # 轮 L study A 的分析程序在空组上算出 NaN，宿主 write_json
                    # （allow_nan=False）抛裸 ValueError，整条 study 崩在宿主异常上。
                    # 输出文档是候选产物，不是宿主缺陷：按 T0 注册的可恢复码
                    # NAN_OR_INF 拒绝（受领报告 validity=false），不让它落盘。
                    try:
                        C.require_no_nan(output_doc, where="analysis_output")
                    except C.ContractError as exc:
                        reasons.append(exc.code)
                        output_doc = None
        output_bytes = payload or b""
        art = self._artifact_event(source_sha)
        receipt = AR.build_receipt(
            profile_id=str(self.profile.profile_id), study_id=self.study,
            phase=phase, plan_sha256=plan.sha256,
            confirm_manifest_sha256=manifest_sha,
            source_sha256=source_sha,
            registered_reply_id=art.get("response_id"),
            registered_action_id=art.get("action_sha256"),
            producer=receipt_producer(art),
            inputs=self._entry_inputs(entries),
            execution_id=ev.container, sandbox_status=("failed" if reasons else "ok"),
            exit_code=int(ev.exit_code), timed_out=bool(ev.timed_out),
            output_bytes=output_bytes, output_doc=output_doc,
            validity=not reasons, reason_codes=reasons)
        summary = self._store_receipt(receipt)
        if output_doc is not None:
            rel = f"analysis/{phase}-{source_sha[:12]}-{_sha_doc(output_doc)[:12]}.json"
            self.ctx.write_json(rel, output_doc)
            summary["output_relpath"] = rel
            # 锁内适配（2026-11-12 T5 全量演练）：凭据 ``output_sha256`` 绑的是
            # **沙盒原始输出字节**（``AR.build_receipt(output_bytes=payload)``），
            # 而 ``analysis/`` 下归档件是宿主规范化后的文档（contrasts 按计划
            # 顺序重排、缩进不同），两者字节必然不同。submit_claim 原来拿归档件
            # 去校验凭据 ⇒ 恒 ``OUTPUT_SHA_MISMATCH`` ⇒ study judge 以
            # ``INVALID_ANALYSIS`` 拒绝（drill16 suite1 唯一失败点）。原始字节
            # 单独落私有区（0600），指针登记 ``raw_relpath``/``raw_sha256``，
            # 下游校验只认原始件。
            raw_rel = f"private/analysis/{self.study}-{phase}.raw.json"
            _write_private(self.ctx.relative(raw_rel), output_bytes)
            summary["output_raw_relpath"] = raw_rel
            pointer = {"schema": "simpraxis.rsi_r1.analysis_pointer.v1",
                       "schema_version": 1, "study_id": self.study,
                       "phase": phase, "relpath": rel,
                       "raw_relpath": raw_rel,
                       "sha256": AR.sha256_bytes(output_bytes),
                       "raw_sha256": AR.sha256_bytes(output_bytes)}
            data = (json.dumps(pointer, ensure_ascii=False, sort_keys=True,
                               indent=1) + "\n").encode("utf-8")
            _write_private(self.ctx.relative(
                f"private/analysis/{self.study}-{phase}.json"), data)
        return (output_doc or {}), summary, output_bytes

    def _r12_analyze(self, action: L.Action) -> "dict[str, Any]":
        self._binding(action)
        source_sha = _require_sha256(action.doc["source_sha256"],
                                     where="source_sha256")
        plan = self._current_plan(phase="explore")
        wanted = [str(r) for r in action.doc["run_ids"]]
        if not wanted:
            return self._fail("ANALYSIS_RUNS_MISMATCH", "run_ids 为空",
                              side_effect_status="NONE")
        try:
            expected = ([self.task.primary.id] if self.r13_bound else None)
            output, summary, _ = self._r12_analysis(
                phase="explore", source_sha=source_sha, run_ids=wanted,
                plan=plan, manifest_sha=None, expected_contrasts=expected)
        except SB.SandboxError as exc:
            # 视图/作用域等**执行前**拒绝：零副作用，可自修。
            return self._fail(exc.code, exc.detail, side_effect_status="NONE")
        if not summary["validity"]:
            return self._analysis_failure(summary, "候选分析未通过执行有效门")
        # 只回候选自己的输出与它的凭据摘要；宿主不填参考答案。
        return self._ok(analysis_relpath=summary.get("output_relpath"),
                        n_runs=len(wanted), analysis=output,
                        analysis_receipt=summary)

    # -- 5) freeze_confirm ----------------------------------------------
    def _do_freeze_confirm(self, action: L.Action) -> "dict[str, Any]":
        # D-13b（2026-11-14 评审 / 轮 M 实证）：确认集只能冻结一次。
        # 旧实现先写 freeze 预留、再由 state.freeze_confirm 拒绝重复冻结——
        # 每次被拒的重复 freeze 都永久占掉 128 run（confirm+replay），B 因此
        # 把 192 的确认帽打到 0。这里在任何预留写入**之前**拦截并终态。
        cur_state = str((self.state.study_status(self.study) or {}).get("state"))
        if cur_state != S.ST_EXPLORING:
            return self._fail(
                "METHOD_CHANGE",
                f"确认集只能冻结一次（study {self.study} 当前 {cur_state}）；"
                "冻结后不得重写 design/plan/analysis 或再次冻结",
                side_effect_status="NONE")
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
                "（先 propose_plan + run_batch，或写 \"host\" 前提是已有探索计划）",
                side_effect_status="NONE")
        if plan_doc.get("parent_sha") == L.HOST_PLAN_SHA:
            plan_doc["parent_sha"] = expect_parent
        plan = C.ResearchPlan.from_dict(plan_doc)
        if plan.phase != "confirm":
            return self._fail("PLAN_PHASE_INVALID", "freeze 需要 confirm 计划",
                                  side_effect_status="NONE")
        if plan.study_id != self.study:
            return self._fail("STUDY_MISMATCH", plan.study_id,
                                  side_effect_status="NONE")
        if self.r13_bound:
            code, detail = self._r13_confirm_precheck(plan, action)
            if code:
                return self._fail(code, detail, side_effect_status="NONE")
        # 量表固定 32 block：在任何预留/种子写入之前拒绝（预执行，零副作用）。
        if int(plan.nuisance_distribution.block_count) != 32:
            return self._fail(
                "CONFIRM_BLOCK_COUNT",
                f"confirm block_count={plan.nuisance_distribution.block_count} "
                "!= 量表固定 32", side_effect_status="NONE")
        if plan.parent_sha != expect_parent:
            return self._fail(
                "PARENT_SHA_MISMATCH",
                f"parent_sha={plan.parent_sha} 不是当前探索计划的 sha "
                f"（{expect_parent}）；可写 \"host\" 由宿主代填",
                side_effect_status="NONE")
        claimed = str(action.doc["plan_sha256"])
        if claimed != L.HOST_PLAN_SHA and plan.sha256 != claimed:
            return self._fail("PLAN_MISMATCH", "plan 文档 sha 与字段不符",
                                  side_effect_status="NONE")
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
        if self.resource_ledger is not None:
            # T1：freeze 一次原子预留 confirm+replay 算力，并过本 lane POST 余量门
            #（≥8，不得借其他 lane 余额）。
            built = J.build_specs(plan, manifest)
            batch_steps = sum(int(s.nsteps) for s in built.values())
            lane_cap = int(self.budget.posts_by_lane[self.admission.lane])
            remaining = lane_cap - self.admission.posts_used(self.admission.lane)
            reservation = self.resource_ledger.reserve_freeze(
                self.study, lane=self.admission.lane,
                blocks=int(plan.nuisance_distribution.block_count),
                n_conditions=len(plan.conditions), replay_runs=n,
                confirm_steps=batch_steps, replay_steps=batch_steps,
                posts_remaining_after_charge=remaining,
                purpose=f"freeze-{self.study}")
        else:
            self.admission.require_lane_space(confirm_runs=n, replay_runs=n)
        try:
            lock = self.state.freeze_confirm(
                study=self.study, plan=plan.to_dict(),
                design_sha256=design_sha,
                analysis_sha256=analysis_sha,
                r12=bool(self._r12() or (self.profile is not None
                                          and self.profile.is_r13)),
                profile=self.profile)
        except Exception:
            # D-13a：state 侧拒绝（重复冻结/终态/凭据不符）时回滚预留，
            # 不让失败尝试永久吃掉确认预算；原异常照常上抛。
            if self.resource_ledger is not None:
                self.resource_ledger.release_freeze(
                    self.study, lane=self.admission.lane,
                    reservation=reservation, reason="freeze rejected by state")
            raise
        if seed_is_new:
            _write_private(seed_path, (seed + "\n").encode("utf-8"))
        if (self.profile is not None and self.profile.is_r13
                and self.r13_confirm_intent is not None):
            self.ctx.write_json(
                f"design_audits/intent_confirm_{plan.sha256[:12]}.json",
                self.r13_confirm_intent.to_dict())
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
        if self.resource_ledger is not None:
            # 确认批只能消费 freeze 时已预留的算力（不新增 runs 计费）。
            self.resource_ledger.consume_confirm(
                self.study, kind=RS.KIND_CONFIRM, runs=len(ordered),
                steps=sum(int(s.nsteps) for s in ordered),
                batch_id=confirm_batch_id(self.study, token),
                lane=self.admission.lane)
            self.resource_ledger.reserve_sandbox(
                self.study, purpose="confirm-batch",
                lane=self.admission.lane)
        else:
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
        evidence = E.execute_batch(ordered, backend, book,
                                   workers=self.batch_workers)
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
        if self._r12() or (self.profile is not None and self.profile.is_r13
                           and not self.r13_bound):
            # R1.2 / R1.3 probe-practice 玩具链：自动在确认批完整视图上执行
            # 冻结分析并落凭据；不回灌维护侧参考值。
            return self._r12_confirm_payload(run_ids, result_sha, plan, manifest)
        if self.r13_bound:
            # R1.3：同样自动跑冻结分析（候选脚本），但**只**校验任务级主对比；
            # 不回传任何宿主参考值。
            self._r13_post_audit(plan, manifest)
            return self._r13_confirm_payload(run_ids, result_sha, plan, manifest)
        # 第 8 步 claims 要逐字段复现宿主从**确认批**重算的值，但候选从未见过
        # 确认批的数值（观测只在容器里、run_confirm 只回 id）：不把宿主用公开
        # 估计量算出的参考值回灌，submit 分支在物理上不可能通过。这里给的是
        # 「你自己这批观测的测量结果」，判据仍从 raw 独立重算并 1e-8 比对。
        references = J.reference_claims(plan=plan, manifest=manifest,
                                        artifacts=artifacts)
        return self._ok(run_ids_head=run_ids[:8], n_runs=len(ordered),
                        result_sha256=result_sha, references=references)

    def _r12_confirm_payload(self, run_ids: "list[str]", result_sha: str,
                             plan: C.ResearchPlan,
                             manifest: "E.ConfirmManifest") -> "dict[str, Any]":
        lock = self.state.confirm_lock(self.study)
        source_sha = str(lock.analysis_source_sha256)
        if plan.analysis_source_sha is not None \
                and plan.analysis_source_sha != source_sha:
            return self._fail("SOURCE_SHA_MISMATCH",
                              "冻结计划与 LOCK 的分析源码不一致",
                              side_effect_status="KNOWN_COMPLETE")
        try:
            output, summary, _ = self._r12_analysis(
                phase="confirm", source_sha=source_sha, run_ids=run_ids,
                plan=plan, manifest_sha=manifest.sha256)
        except SB.SandboxError as exc:
            return self._fail(exc.code, exc.detail,
                              side_effect_status="KNOWN_COMPLETE")
        if not summary["validity"]:
            return self._analysis_failure(
                summary, "确认批冻结分析未通过执行有效门；"
                         "候选不得借用宿主数值继续提交")
        return self._ok(run_ids_head=run_ids[:8], n_runs=len(run_ids),
                        result_sha256=result_sha,
                        candidate_output=output, analysis_receipt=summary)

    # -- 7) submit_claim -------------------------------------------------
    def _do_submit_claim(self, action: L.Action) -> "dict[str, Any]":
        if self.r13_bound:
            return self._r13_submit(action)
        if self._r12() or (self.profile is not None and self.profile.is_r13):
            return self._r12_submit(action)
        response_id, _ = self._binding(action)
        claims = [dict(c) for c in action.doc["claims"]]
        report_text = str(action.doc["report_text"])
        plan = self._current_plan(phase="confirm")
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        if manifest is None:
            return self._fail("CONFIRM_NOT_LOCKED", "缺确认 manifest",
                                  side_effect_status="NONE")
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

    # -- R1.3：确认前校验 / 后审计 / 自动分析 / 提交 --------------------
    def _r13_confirm_precheck(self, plan: C.ResearchPlan,
                              action: L.Action) -> "tuple[str, str]":
        """确认计划的 R1.3 合同：任务条件固定、意图/分布/对比/阴性控制一致。"""
        task = self.task
        want = {c.id: dict(c.params) for c in task.conditions}
        got = {c.id: dict(c.params) for c in plan.conditions}
        if got != want:
            return ("TASK_CONDITIONS_MISMATCH",
                    "确认条件必须逐字等于任务登记的干预条件（含 sham/角点）；"
                    f"实得 id={sorted(got)}")
        dist = plan.nuisance_distribution
        want_var = {k: (str(v["support"][0]), str(v["support"][1]))
                    for k, v in task.nuisance_schema.items()
                    if v["mode"] == "varying"}
        got_var = {k: (str(v[0]), str(v[1])) for k, v in dist.varying.items()}
        if int(dist.block_count) != 32 or got_var != want_var:
            return ("NUISANCE_DISTRIBUTION_MISMATCH",
                    f"block_count={dist.block_count} varying={got_var}；"
                    f"任务要求 32 与 {want_var}")
        if (plan.primary.metric != task.primary.metric
                or plan.primary.proposition != task.primary.proposition
                or plan.primary.delta != task.primary.delta):
            return ("PRIMARY_CONTRAST_MISMATCH",
                    "plan.primary 的 metric/proposition/delta 必须与任务登记一致")
        ids = set(plan.primary.condition_ids)
        if not ids <= set(want):
            return ("PRIMARY_CONTRAST_MISMATCH", f"条件 {sorted(ids)} 不在任务内")
        a, b = (next(c for c in plan.conditions if c.id == cid) for cid in ids)
        diff = sorted(k for k in a.params if a.params.get(k) != b.params.get(k))
        if len(diff) != 1:
            return ("PRIMARY_CONTRAST_MISMATCH",
                    f"plan.primary 必须是单因子组件对比，实为 {diff}")
        if task.negative_control_spec is not None:
            nc = task.negative_control_spec["contrast"]
            if plan.negative_control is None:
                return ("NEGATIVE_CONTROL_MISSING", "任务登记了阴性控制")
            if (plan.negative_control.id != nc["id"]
                    or plan.negative_control.metric != nc["metric"]
                    or tuple(sorted(plan.negative_control.condition_ids))
                    != tuple(sorted(t["condition_id"] for t in nc["terms"]))):
                return ("NEGATIVE_CONTROL_MISMATCH",
                        "阴性控制必须逐字复用任务登记件")
        elif plan.negative_control is not None:
            return ("NEGATIVE_CONTROL_MISMATCH",
                    "开放研究任务不登记阴性控制，不得自加有利对比")
        raw_intent = action.doc.get("design_intent")
        if raw_intent is None:
            return ("DESIGN_INTENT_MISSING",
                    "freeze_confirm 必须提交 confirmation 意图")
        try:
            intent = DI.parse_design_intent(raw_intent, profile=self.profile,
                                            task_spec=task)
        except DI.DesignError as exc:
            return (exc.code, exc.detail)
        if intent.phase_role != "confirmation":
            return ("INTENT_PHASE", "freeze 需要 phase_role=confirmation")
        self.r13_confirm_intent = intent
        return ("", "")

    def _r13_post_audit(self, plan: C.ResearchPlan,
                        manifest: "E.ConfirmManifest") -> None:
        """后执行审计：以宿主 manifest 为唯一事实源，逐 run 核 actual。"""
        rows: "list[dict[str, Any]]" = []
        for a in manifest.assignments:
            rows.append({"condition_id": a.condition_id, "block_id": a.block_id,
                         "params": dict(a.params)})
        actual = [dict(r, verified_by_host=True) for r in rows]
        try:
            audit = DI.audit_design(self.r13_confirm_intent, task=self.task,
                                    proposed_cells=rows,
                                    actual_manifest=actual)
        except DI.DesignError as exc:
            raise L.LoopError(exc.code, exc.detail, terminal=True) from None
        try:
            counts = DI.counts_document(
                run_count=audit.run_count,
                unique_design_count=audit.unique_design_count,
                independent_unit_count=audit.independent_unit_count)
        except DI.DesignError as exc:
            raise L.LoopError(exc.code, exc.detail, terminal=True) from None
        self.r13_design_counts = counts
        self.ctx.write_json(
            f"design_audits/confirm_post_{plan.sha256[:12]}.json",
            {**audit.to_dict(), "counts": counts})

    def _expected_contrasts(self) -> "list[str]":
        """G2.1：必需对比集合（默认只主对比，保持 G2/旧路径逐字行为）。"""
        if self.require_task_contrasts:
            return G2.derive_required_ids(self.task)
        return [self.task.primary.id]

    def _r13_confirm_payload(self, run_ids: "list[str]", result_sha: str,
                             plan: C.ResearchPlan,
                             manifest: "E.ConfirmManifest") -> "dict[str, Any]":
        lock = self.state.confirm_lock(self.study)
        source_sha = str(lock.analysis_source_sha256)
        try:
            output, summary, output_bytes = self._r12_analysis(
                phase="confirm", source_sha=source_sha, run_ids=run_ids,
                plan=plan, manifest_sha=manifest.sha256,
                expected_contrasts=self._expected_contrasts())
        except SB.SandboxError as exc:
            return self._fail(exc.code, exc.detail,
                              side_effect_status="KNOWN_COMPLETE")
        if not summary["validity"]:
            return self._analysis_failure(
                summary, "确认批冻结分析未通过执行有效门（检查输出 schema/"
                         f"run 集/主对比 {self.task.primary.id}）")
        receipt_bytes = b""
        rel = self._receipt_rel(self.study, "confirm")
        if self.ctx.exists(rel):
            receipt_bytes = self.ctx.relative(rel).read_bytes()
        if self.g2:
            # RSI-G2：最小交付单元（不是 4096 裁剪后的普通 dict）。
            return self._g2_confirm_payload(run_ids, result_sha, plan,
                                            output, output_bytes, receipt_bytes)
        evidence = self._r13_evidence(plan, manifest, output_bytes,
                                      receipt_bytes)
        index = {name: hashlib.sha256(data).hexdigest()
                 for name, data in sorted(evidence.items())}
        return self._ok(run_ids_head=run_ids[:8], n_runs=len(run_ids),
                        result_sha256=result_sha,
                        candidate_output=output, analysis_receipt=summary,
                        primary_contrast_id=self.task.primary.id,
                        evidence_index=index)

    def _r13_evidence(self, plan: C.ResearchPlan,
                      manifest: "E.ConfirmManifest",
                      output_bytes: bytes, receipt_bytes: bytes
                      ) -> "dict[str, bytes]":
        specs = J.build_specs(plan, manifest)
        per_run = J.recompute_metrics(plan=plan, artifacts=self._confirm_artifacts(),
                                      specs=specs)
        metric = self.task.primary.metric
        table: "dict[str, Any]" = {"task_id": self.task.task_id,
                                   "metric": metric, "blocks": []}
        blocks: "dict[str, dict[str, Any]]" = {}
        for run_id, spec in specs.items():
            got = ((per_run.get(run_id) or {}).get("values") or {}).get(metric)
            blocks.setdefault(spec.block_id, {})[spec.condition_id] = got
        for block in sorted(blocks):
            table["blocks"].append({"block_id": block,
                                    "values": blocks[block]})
        return {
            "analysis/output.json": output_bytes,
            "analysis/receipt.json": receipt_bytes,
            "confirm/blocks.json": (json.dumps(table, sort_keys=True,
                                               ensure_ascii=False) + "\n").encode(),
        }

    def _g2_confirm_payload(self, run_ids: "list[str]", result_sha: str,
                            plan: C.ResearchPlan, output: Mapping[str, Any],
                            output_bytes: bytes, receipt_bytes: bytes
                            ) -> "dict[str, Any]":
        """G2：从候选自己的 output/receipt 投影 ResultEnvelopeV2（零宿主补值）。"""
        raw_contrasts = output.get("contrasts")
        present: "set[str]" = set()
        if isinstance(raw_contrasts, list):
            present = {str(item.get("contrast_id")) for item in raw_contrasts
                       if isinstance(item, Mapping)}
        elif isinstance(raw_contrasts, Mapping):
            present = {str(key) for key in raw_contrasts}
        if self.require_task_contrasts:
            # G2.1：必需集合只由冻结 TaskSpec 派生，**不**从 output 现有项反推。
            required = G2.derive_required_ids(self.task)
            missing = [cid for cid in required if cid not in present]
            if missing:
                return self._fail(
                    "SOURCE_EVIDENCE_INCOMPLETE",
                    f"候选输出缺必需对比 {missing}；不以宿主 raw 回填",
                    side_effect_status="KNOWN_COMPLETE")
        else:
            required = [self.task.primary.id]
            nc = (self.task.negative_control_spec or {}).get("contrast") or {}
            nc_id = str(nc.get("contrast_id") or nc.get("id") or "")
            if nc_id and nc_id in present and nc_id not in required:
                required.append(nc_id)
        try:
            envelope = G2.envelope_from_output(
                study_id=self.study, phase="confirm", plan_sha256=plan.sha256,
                output=output, output_bytes=output_bytes,
                receipt_bytes=receipt_bytes,
                scope_contract=self.scope_contract,
                required_contrast_ids=required,
                primary_contrast_id=self.task.primary.id)
            G2.verify_envelope(envelope, scope_contract=self.scope_contract)
        except G2.G2Error as exc:
            # 投影失败 = 仪器交付失败：不改写已成功 confirm 的 raw，不伪称
            # 候选科学 FAIL；回包给出可机读阻断码，由驱动停止。
            return self._fail(exc.code, exc.detail,
                              side_effect_status="KNOWN_COMPLETE")
        self.ctx.write_json("state/g2_artifacts.json",
                            {"study": self.study, "plan_sha256": plan.sha256,
                             "envelope_sha256": envelope["envelope_sha256"],
                             "artifacts": list(envelope["artifacts"])})
        return self._ok(run_ids_head=run_ids[:8], n_runs=len(run_ids),
                        result_sha256=result_sha,
                        result_envelope=envelope,
                        envelope_sha256=envelope["envelope_sha256"])

    def _do_read_result_artifact(self, action: L.Action) -> "dict[str, Any]":
        """G2 只读分页：仅当前 study 已登记且候选自产的 analysis 产物。"""
        if not self.g2:
            raise L.LoopError("UNKNOWN_ACTION", action.name, terminal=True)
        self._binding(action)
        state_now = self.state.study_state(self.study)
        if state_now != S.ST_RESULTS_RELEASED:
            return self._fail("RESULTS_NOT_RELEASED",
                              f"study 在 {state_now}；读取只在结果发布后允许",
                              side_effect_status="NONE")
        if not self.ctx.exists("state/g2_artifacts.json"):
            return self._fail("ARTIFACT_MISSING", "无已登记 envelope 产物",
                              side_effect_status="NONE")
        registry_doc = self.ctx.read_json("state/g2_artifacts.json")
        plan = self._current_plan(phase="confirm")
        if str(registry_doc.get("study")) != self.study \
                or str(registry_doc.get("plan_sha256")) != plan.sha256:
            return self._fail("PLAN_MISMATCH", "登记产物与当前 study/plan 不符",
                              side_effect_status="NONE")
        registry = G2.ArtifactRegistry(registry_doc.get("artifacts") or [])
        try:
            page = G2.read_page(
                registry=registry,
                read_bytes=lambda rel: self.ctx.relative(rel).read_bytes(),
                artifact_id=str(action.doc["artifact_id"]),
                output_sha256=str(action.doc["output_sha256"]),
                cursor=int(action.doc["cursor"]),
                page_bytes=int(action.doc["page_bytes"]))
        except G2.G2Error as exc:
            return self._fail(exc.code, exc.detail, side_effect_status="NONE")
        return self._ok(**page)

    def _r13_submit(self, action: L.Action) -> "dict[str, Any]":
        """R1.3 提交：凭据/数值/诊断/报告/claim 全过才 JUDGE_OK。"""
        response_id, action_sha = self._binding(action)
        claims = [dict(c) for c in action.doc["claims"]]
        report_text = str(action.doc["report_text"])
        report_doc = action.doc.get("research_report")
        if report_doc is None:
            return self._fail("REPORT_MISSING",
                              "R1.3 提交必须携带 research_report",
                              side_effect_status="NONE")
        plan = self._current_plan(phase="confirm")
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        if manifest is None:
            return self._fail("CONFIRM_NOT_LOCKED", "缺确认 manifest",
                              side_effect_status="NONE")
        lock = self.state.confirm_lock(self.study)
        source_sha = str(lock.analysis_source_sha256)
        receipt_rel = self._receipt_rel(self.study, "confirm")
        receipt_bytes = (self.ctx.relative(receipt_rel).read_bytes()
                         if self.ctx.exists(receipt_rel) else b"")
        receipt = (json.loads(receipt_bytes.decode("utf-8"))
                   if receipt_bytes else None)
        receipt_sha = (hashlib.sha256(receipt_bytes).hexdigest()
                       if receipt_bytes else "")
        output_bytes, output_error = read_confirm_output(self.ctx, self.study)
        output_norm = None
        if not output_error:
            try:
                raw = json.loads(output_bytes.decode("utf-8"))
                parsed = AR.parse_output(
                    raw, expected_run_ids=sorted(self._confirm_artifacts()),
                    expected_contrasts=self._expected_contrasts())
                output_norm = {"run_ids": parsed["run_ids"],
                               "contrasts": dict(parsed["contrasts"]),
                               "diagnostics": raw.get("diagnostics")}
            except (ValueError, UnicodeDecodeError):
                output_error = "ANALYSIS_OUTPUT_BAD_JSON"
            except AR.ReceiptError as exc:
                output_error = exc.code
        if receipt is None:
            receipt_ok, receipt_reasons = False, ["NO_CONFIRM_RECEIPT"]
        else:
            run_ids = sorted(self._confirm_artifacts())
            entries: "list[SB.ViewEntry]" = []
            for run_id in run_ids:
                entries.extend(self._run_view_entries(run_id))
            source_file_sha = ""
            try:
                source_file_sha = hashlib.sha256(
                    self.ctx.relative(f"designs/{source_sha}.py").read_bytes()
                ).hexdigest()
            except OSError:
                pass
            art = self._artifact_event(source_sha)
            expect = AR.ReceiptExpectation(
                study_id=self.study, phase="confirm", plan_sha256=plan.sha256,
                profile_id=str(self.profile.profile_id),
                source_sha256=source_sha, source_file_sha256=source_file_sha,
                producer=receipt_producer(art),
                registered_reply_id=art.get("response_id"),
                registered_action_id=art.get("action_sha256"),
                confirm_manifest_sha256=manifest.sha256,
                allowed_run_ids=tuple(run_ids),
                expected_inputs=tuple(self._entry_inputs(entries)),
                output_bytes=output_bytes,
                execution_id=str(receipt.get("execution_id") or ""),
                exit_code=int(receipt.get("exit_code", -1)),
                timed_out=bool(receipt.get("timed_out", False)))
            receipt_ok, receipt_reasons = AR.validate_receipt(receipt, expect)
        submission = {
            "schema": J.SUBMISSION_SCHEMA, "study_id": self.study,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": self.task.primary.id,
            "analysis_source_sha": source_sha,
            "analysis_receipt_sha": receipt_sha,
            "claims": claims}
        evidence = self._r13_evidence(plan, manifest, output_bytes,
                                      receipt_bytes)
        rendered_sha = hashlib.sha256(
            RR.render_report(report_doc).encode("utf-8")).hexdigest()
        if self.g2:
            # RSI-G2（Plan §3.0 state 行）：raw reply / action / report doc /
            # rendered 四路 sha **分开保存**，不能互代；旧代码把 action 规范
            # sha 叫 reply_sha，是 G1 复盘指出的混用。
            reply_ev = self.state._reply_event(response_id)
            raw_reply_sha = str(((reply_ev.payload if reply_ev else {}) or {})
                                .get("text_sha256") or "")
            origin = {"producer": "candidate", "reply_sha256": raw_reply_sha,
                      "raw_reply_sha256": raw_reply_sha,
                      "action_sha256": action_sha,
                      "report_doc_sha256": _sha_doc(report_doc),
                      "rendered_sha256": rendered_sha,
                      "assisted": False}
        else:
            origin = {"producer": "candidate",
                      "reply_sha256": _sha_doc(action.to_doc()),
                      "assisted": False}
        report = JV.judge_study_v13(
            task=self.task, plan=plan, manifest=manifest,
            artifacts=self._confirm_artifacts(),
            specs=J.build_specs(plan, manifest),
            submission=submission, report_doc=report_doc,
            receipt_ok=receipt_ok, receipt_reasons=receipt_reasons,
            output=output_norm, output_error=output_error,
            candidate_origin=origin,
            expected_reply_sha256=str(origin["reply_sha256"]),
            evidence_files=evidence,
            design_counts=self.r13_design_counts,
            # G2 严格面：报告正文必须与确定性 render 逐字相等（旧路径不传）。
            report_text=report_text if self.g2 else None)
        self.state.submit_claim(study=self.study, claims=claims,
                                report_text=report_text,
                                response_id=response_id,
                                origin=origin if self.g2 else None)
        CL.write_claim_submission(self.ctx, self.study, submission)
        CL.write_judge_report(self.ctx, self.study, report)
        self.ctx.write_json(
            f"reports/{self.study}-{plan.sha256[:12]}.json",
            ({"report": dict(report_doc), "rendered": None,
              "reply_sha256": origin["reply_sha256"]}
             if not self.g2 else
             {"report": dict(report_doc),
              "raw_reply_sha256": origin["raw_reply_sha256"],
              "action_sha256": origin["action_sha256"],
              "report_doc_sha256": origin["report_doc_sha256"],
              "rendered_sha256": origin["rendered_sha256"]}))
        verdict = self._verdict_v13(report)
        self.state.conclude(self.study, verdict,
                            reason=f"judge {report['verdict']}")
        layers = report.get("layers") or {}
        return self._ok(
            judge_verdict=report["verdict"], verdict=verdict,
            claim_status=layers.get("claim_status"),
            receipt_valid=layers.get("receipt_valid"),
            numerics_valid=layers.get("numerics_valid"),
            diagnostics_valid=layers.get("diagnostics_valid"),
            report_valid=layers.get("report_valid"),
            reason_codes=list(report.get("reason_codes") or [])[:8])

    def _verdict_v13(self, report: Mapping[str, Any]) -> str:
        layers = report.get("layers") or {}
        if not (layers.get("schema_accepted") and layers.get("receipt_valid")
                and layers.get("numerics_valid") and layers.get("evidence_valid")):
            return S.ST_INVALID
        if (report.get("verdict") == "JUDGE_OK"
                and layers.get("claim_status") == "MATCH"
                and layers.get("diagnostics_valid")
                and layers.get("report_valid")):
            return S.ST_VERIFIED
        return S.ST_VALID_FAIL

    def _r12_submit(self, action: L.Action) -> "dict[str, Any]":
        """R1.2 提交：凭据 → 数值 → claim 三门 AND；回包不含宿主数值。"""
        response_id, _ = self._binding(action)
        claims = [dict(c) for c in action.doc["claims"]]
        report_text = str(action.doc["report_text"])
        plan = self._current_plan(phase="confirm")
        manifest = self.confirm_manifest or self._frozen_manifest_raw()
        if manifest is None:
            return self._fail("CONFIRM_NOT_LOCKED", "缺确认 manifest",
                              side_effect_status="NONE")
        lock = self.state.confirm_lock(self.study)
        source_sha = str(lock.analysis_source_sha256)
        submission = {
            "schema": J.SUBMISSION_SCHEMA, "study_id": self.study,
            "plan_sha256": plan.sha256,
            "confirm_manifest_sha256": manifest.sha256,
            "primary_contrast_id": plan.primary.id, "claims": claims}

        receipt = None
        receipt_rel = self._receipt_rel(self.study, "confirm")
        if self.ctx.exists(receipt_rel):
            doc = self.ctx.read_json(receipt_rel)
            receipt = dict(doc) if isinstance(doc, Mapping) else None
        output_norm: "dict[str, Any] | None" = None
        # 凭据绑定面 = 沙盒原始字节（见 ``read_confirm_output`` 的锁内适配说明）
        output_bytes, output_error = read_confirm_output(self.ctx, self.study)
        if not output_error:
            try:
                raw = json.loads(output_bytes.decode("utf-8"))
                output_norm = AR.parse_output(
                    raw, expected_run_ids=sorted(self._confirm_artifacts()),
                    expected_contrasts=self._r12_plan_contrast_ids(plan))
            except (ValueError, UnicodeDecodeError):
                output_error = "ANALYSIS_OUTPUT_BAD_JSON"
            except AR.ReceiptError as exc:
                output_error = exc.code

        run_ids = sorted(self._confirm_artifacts())
        entries: "list[SB.ViewEntry]" = []
        for run_id in run_ids:
            entries.extend(self._run_view_entries(run_id))
        try:
            source_file_sha = hashlib.sha256(
                self.ctx.relative(f"designs/{source_sha}.py").read_bytes()
            ).hexdigest()
        except OSError:
            source_file_sha = ""
        art = self._artifact_event(source_sha)
        expect = AR.ReceiptExpectation(
            study_id=self.study, phase="confirm", plan_sha256=plan.sha256,
            source_sha256=source_sha, source_file_sha256=source_file_sha,
            producer=receipt_producer(art),
            registered_reply_id=art.get("response_id"),
            registered_action_id=art.get("action_sha256"),
            confirm_manifest_sha256=manifest.sha256,
            allowed_run_ids=tuple(run_ids),
            expected_inputs=tuple(self._entry_inputs(entries)),
            output_bytes=output_bytes,
            execution_id=str((receipt or {}).get("execution_id") or ""),
            exit_code=int((receipt or {}).get("exit_code", -1)),
            timed_out=bool((receipt or {}).get("timed_out", False)))
        if receipt is None:
            receipt_ok, receipt_reasons = False, ["NO_CONFIRM_RECEIPT"]
        else:
            receipt_ok, receipt_reasons = AR.validate_receipt(receipt, expect)

        report = J.judge_study_r12(
            plan=plan, manifest=manifest, artifacts=self._confirm_artifacts(),
            submission=submission, receipt_ok=receipt_ok,
            receipt_reasons=receipt_reasons, output=output_norm,
            output_error=output_error)
        self.state.submit_claim(study=self.study, claims=claims,
                                report_text=report_text,
                                response_id=response_id)
        CL.write_claim_submission(self.ctx, self.study, submission)
        CL.write_judge_report(self.ctx, self.study, report)
        verdict = self._verdict_r12(report)
        self.state.conclude(self.study, verdict,
                            reason=f"judge {report['verdict']}")
        layers = report.get("layers") or {}
        return self._ok(
            judge_verdict=report["verdict"], verdict=verdict,
            claim_status=layers.get("claim_status"),
            receipt_valid=layers.get("receipt_valid"),
            numerics_valid=layers.get("numerics_valid"),
            evidence_valid=layers.get("evidence_valid"),
            reason_codes=list(report.get("reason_codes") or [])[:8],
            claim_mismatches=list(report.get("claim_mismatches") or [])[:8])

    def _verdict_r12(self, report: Mapping[str, Any]) -> str:
        layers = report.get("layers") or {}
        if not layers.get("schema_accepted"):
            return S.ST_INVALID
        if not layers.get("receipt_valid"):
            return S.ST_INVALID
        if not layers.get("numerics_valid"):
            return S.ST_INVALID
        if not layers.get("evidence_valid"):
            return S.ST_INVALID
        if report.get("verdict") == "JUDGE_OK" \
                and layers.get("claim_status") == "MATCH":
            return S.ST_VERIFIED
        return S.ST_VALID_FAIL

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
        entries = [SB.ViewEntry(plan_path.name, _sha_file(plan_path)),
                   SB.ViewEntry(contract.name, _sha_file(contract))]
        if self.r13_bound:
            task_rel = "public/task.json"
            task_path = self.ctx.relative(task_rel)
            # 每个 study 的视图都必须写**本 study** 的任务件（A/B 串行复用同一
            # 路径；只在缺失时写会让 B 拿到 A 的条件）。
            task_view = {
                "task_id": self.task.task_id,
                "study_id": self.task.mechanism_id.split("_")[0],
                "conditions": [c.to_dict() for c in self.task.conditions],
                "primary": self.task.primary.to_dict(),
                "nuisance_schema": dict(self.task.nuisance_schema),
                "negative_control_spec": self.task.negative_control_spec,
            }
            self.ctx.write_json(task_rel, task_view)
            entries.append(SB.ViewEntry(task_path.name, _sha_file(task_path)))
        return entries

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
        lock_sha256: str,
        profile: "P.CampaignProfile | None" = None,
        resource_ledger: "RS.ResourceLedger | None" = None,
        task: "TS.TaskSpecV13 | None" = None,
) -> "tuple[CampaignAdmission, SlotBook, CampaignDispatcher, S.ResearchState]":
    """标准装配：状态 + 预算 + 准入 + 槽 + 分派（同 root 共享）。"""
    budget = C.budget_from_dict(ctx.read_json("contract/budget.json"))
    state = S.ResearchState(ctx)
    admission = CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                  lock_sha256=lock_sha256, spend=spend, lane=lane)
    slots = SlotBook(ctx, budget)
    admission.attach_slots(slots)
    dispatcher = CampaignDispatcher(ctx=ctx, state=state, admission=admission,
                                    budget=budget, study=study, runner=runner,
                                    image=image, backend_factory=backend_factory,
                                    profile=profile,
                                    resource_ledger=resource_ledger,
                                    task=task)
    return admission, slots, dispatcher, state
