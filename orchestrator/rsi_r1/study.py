"""RSI-R1 T7：两自主研究 study 的驱动面（A 48 / B 48 真实 POST）。

Plan §9 T7：两 study **各用独立会话工作区**（独立 ``ResearchLoop``/事件历史，
不互传脚本与答案），八动作链从探索修订走到唯一确认、确认批、独立判据与主张
提交。本模块只做**驱动与报告**：状态机在 ``state``、动作在 ``assembly``、判据在
``judge``——与 probe/practice 链共用同一生产面，不复制其中任何一条。

与协议链的三点差别（§8.1/§11.2）：

* 预算 lane 是 ``study_A`` / ``study_B``（各 48 POST），root 门分别是
  ``PRACTICED`` / ``STUDY_A``；
* **允许合同内自修**（字段错、代码报错都可在额度内重发），只有终止类拒绝、
  POST 帽尽、输入超限会停下；
* 本阶段产出的不是「协议走通」而是**研究事实**：主张与独立判据报告由
  dispatcher 在 ``submit_claim`` 时落盘，终态由 ``state`` 判定。驱动只如实
  记录，不替研究下结论、不改判。

root 推进策略：study 达到终态**且**有主张提交与独立判据（``claim/submission_X``
在场）才推进到 ``STUDY_A`` / ``STUDY_B``；``BLOCKED`` 或中途停止一律不推进
（§11.2：STOP 不自动继续正式研究）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from orchestrator.common import research_transport as T
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import backend as BK
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import lanes as LANES
from orchestrator.rsi_r1 import lock as LK
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import state as S

REPORT_SCHEMA = "simpraxis.rsi_r1.study_report.v1"
SCHEMA_VERSION = 1

#: study → 记账 lane / root 门 / 完成后 root（单一派生，不写字面量）。
STUDY_LANE: "Mapping[str, str]" = dict(A.STUDY_LANE)
STUDY_GATE: "Mapping[str, str]" = {s: A.ROOT_GATE[A.STUDY_LANE[s]]
                                   for s in S.STUDIES}
STUDY_NEXT: "Mapping[str, str]" = {"A": S.ROOT_STUDY_A, "B": S.ROOT_STUDY_B}
STUDY_POSTS: "Mapping[str, int]" = {s: LANES.LANE_POST_CAPS[A.STUDY_LANE[s]]
                                    for s in S.STUDIES}

#: 主张与判据齐备才算「研究完成」（BLOCKED/中途停止不算）。
STUDY_DONE_STATES = frozenset({S.ST_VERIFIED, S.ST_VALID_FAIL, S.ST_INVALID,
                               S.ST_ASSISTED})


class StudyError(RuntimeError):
    """研究驱动的门禁/配置错误（不是科学结论）。``code`` 公开。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def report_relpath(study: str) -> str:
    """研究报告件路径（``studies/`` 子目录；结案清点不看它，只作驱动取证）。"""
    return f"studies/report_{study}.json"


@dataclass
class StudyParts:
    """一条正式研究链的生产件（状态/预算/准入/分派/发送/循环）。"""

    study: str
    lane: str
    admission: A.CampaignAdmission
    slots: A.SlotBook
    state: S.ResearchState
    dispatcher: A.CampaignDispatcher
    store: L.EventStore
    sender: L.LockedSender
    loop: L.ResearchLoop


@dataclass
class StudyResult:
    study: str
    actions: "list[dict[str, Any]]" = field(default_factory=list)
    ok_count: int = 0
    stopped: str = ""
    terminal: bool = False
    study_state: str = ""

    def as_dict(self) -> "dict[str, Any]":
        return {"study": self.study, "actions": list(self.actions),
                "ok_count": self.ok_count, "stopped": self.stopped,
                "terminal": bool(self.terminal),
                "study_state": self.study_state}


def backend_factory_for(ctx: Any, *, runner: Any, image: str, lane: str
                        ) -> Callable[[str], BK.PhysicsBackend]:
    """真容器 backend：观测/证据/工作区全部落 campaign 根（研究无 scoped 试次）。"""
    def build(batch_id: str) -> BK.PhysicsBackend:
        return BK.PhysicsBackend(
            observations_root=ctx.subdir("observations"),
            evidence_root=ctx.subdir("evidence_pack"),
            work_root=ctx.subdir("trials") / f"work-{lane}",
            runner=runner, image=image, batch_id=batch_id)
    return build


def build_study_parts(
        ctx: Any, *, study: str, spend: A.SpendContract, lock: L.LockDocument,
        transport_factory: Callable[[], Callable[[T.Request], L.Reply]],
        runner: Any, image: str, counter: Callable[[str], int],
        backend_factory: "Callable[[str], Any] | None" = None,
        profile: Any = None, resource_ledger: Any = None) -> StudyParts:
    """按生产面装配一条研究链；root 门由 ``CampaignAdmission`` 二次把守。"""
    if study not in S.STUDIES:
        raise StudyError("STUDY_UNKNOWN", repr(study))
    lane = STUDY_LANE[study]
    factory = backend_factory or backend_factory_for(
        ctx, runner=runner, image=image, lane=lane)
    admission, slots, dispatcher, state = A.build_campaign_parts(
        ctx, spend=spend, lane=lane, study=study, runner=runner, image=image,
        backend_factory=factory, lock_sha256=lock.sha256,
        profile=profile, resource_ledger=resource_ledger)
    store = L.EventStore(state, study=study)
    envelope = A.Envelope.from_ctx(ctx)
    sender = L.build_production_sender(
        lock_reader=LK.lock_reader(ctx), transport_factory=transport_factory,
        admission=admission, budget=slots, envelope=envelope,
        spend_contract=spend, sha_of=PT.repo_sha)
    system = L.render_system(
        extra_rules=PT.render_study_brief(study, profile=profile))

    def messages() -> T.Request:
        return T.build_request(
            system=system,
            task_brief=L.render_task_brief(dispatcher.visible_state()),
            turns=store.history(), counter=counter,
            max_tokens=int(getattr(profile, "output_tokens", T.MAX_OUTPUT_TOKENS)))

    loop = L.ResearchLoop(
        sender=sender, parser=L.parse_action, dispatcher=dispatcher, store=store,
        messages=messages, counter=counter, admission=admission, budget=slots,
        envelope=envelope, root_lock=lock, spend_contract=spend)
    return StudyParts(study=study, lane=lane, admission=admission, slots=slots,
                      state=state, dispatcher=dispatcher, store=store,
                      sender=sender, loop=loop)


def drive_study(parts: StudyParts, *, cap: int) -> StudyResult:
    """跑到研究终态、POST 帽尽或终止类拒绝；合同内失败允许自修。"""
    result = StudyResult(study=parts.study)
    lane = parts.lane
    while True:
        state_now = parts.state.study_state(parts.study)
        if state_now in S.STUDY_TERMINAL:
            break
        if parts.admission.posts_used(lane) >= cap:
            result.stopped = "POSTS_EXHAUSTED"
            break
        try:
            outcome = parts.loop.turn()
        except L.LoopError as exc:  # 准入/槽的终止类拒绝（含 PAUSED）
            # 锁内适配（2026-11-12 T5）：被拦下的终止类拒绝必须留下**明细**。
            # 旧实现只记 code，演练里 ``STATE_UNKNOWN`` 不带 detail 时无法区分
            # 是 lane 门、动作绑定还是历史缺口，只能靠猜——证据面不许这样。
            detail = str(getattr(exc, "detail", "") or "")
            try:
                parts.store.stop_or_block(code=exc.code, detail=detail)
            except Exception:  # noqa: BLE001 —— 落证据失败不改变判定
                pass
            result.actions.append({"n": len(result.actions) + 1,
                                   "action": None, "ok": False,
                                   "code": exc.code, "detail": detail,
                                   "terminal": True, "raised": True})
            result.stopped = exc.code
            result.terminal = True
            break
        doc = outcome.as_dict()
        doc["n"] = len(result.actions) + 1
        result.actions.append(doc)
        if outcome.ok:
            result.ok_count += 1
            continue
        if outcome.terminal:
            result.stopped = outcome.code or "TERMINAL"
            result.terminal = True
            break
        # 非终止类（字段错/合同内拒绝）：如实记录，继续下一呼（额度照计）。
    result.study_state = parts.state.study_state(parts.study)
    return result


def _check_study_gate(ctx: Any, study: str) -> "tuple[L.LockDocument, Mapping[str, Any]]":
    lock, freeze_report = LK.require_frozen(ctx)
    want = STUDY_GATE[study]
    root = S.ResearchState(ctx).root_state()
    if root != want:
        raise StudyError(
            "ROOT_GATE",
            f"study {study} 需要 root={want}，当前 {root}（先完成前一阶段）")
    if lock.envelope_id != ctx.envelope_id:
        raise StudyError("LOCK_MISMATCH",
                         f"LOCK.envelope={lock.envelope_id!r} != {ctx.envelope_id!r}")
    return lock, freeze_report


def _judge_block(ctx: Any, study: str) -> "dict[str, Any]":
    rel = f"judge/report_{study}.json"
    if not ctx.exists(rel):
        return {"present": False, "verdict": None, "claim_status": None}
    doc = dict(ctx.read_json(rel) or {})
    layers = doc.get("layers") or {}
    return {"present": True, "verdict": doc.get("verdict"),
            "claim_status": layers.get("claim_status"),
            "evidence_valid": layers.get("evidence_valid")}


def run_study(
        ctx: Any, *, study: str, spend: A.SpendContract,
        transport_factory: Callable[[], Callable[[T.Request], L.Reply]],
        runner: Any, image: str, batch_id: str,
        counter: Callable[[str], int],
        clock: Callable[[], str] = utc_now_iso_z,
        backend_factory: "Callable[[str], Any] | None" = None,
        profile: Any = None, resource_ledger: Any = None,
) -> "dict[str, Any]":
    """跑一个正式研究 study 并落 ``studies/report_<study>.json``。

    同一 study 只允许跑一次（报告件在场即拒绝：研究不是可重放的演示）。
    完成后按终态推进 root；``BLOCKED`` 或中途停止不推进。
    """
    if study not in S.STUDIES:
        raise StudyError("STUDY_UNKNOWN", repr(study))
    if not str(batch_id).strip():
        raise StudyError("BATCH_MISSING", "batch_id 不得为空")
    lane = STUDY_LANE[study]
    rel = report_relpath(study)
    if ctx.exists(rel):
        raise StudyError("STUDY_ALREADY_RAN",
                         f"{rel} 已存在：同一 study 不得重跑（失败另开新 root）")
    lock, freeze_report = _check_study_gate(ctx, study)
    root_before = S.ResearchState(ctx).root_state()
    cap = STUDY_POSTS[study]
    used_before = PT.posts_used(ctx, lane=lane, lock=lock, spend=spend)
    started = clock()

    pump = PT.HeartbeatPump(ctx.subdir("state")).start()
    try:
        pump.set_phase(f"{lane}:study-{study}")
        parts = build_study_parts(
            ctx, study=study, spend=spend, lock=lock,
            transport_factory=transport_factory, runner=runner, image=image,
            counter=counter, backend_factory=backend_factory,
            profile=profile, resource_ledger=resource_ledger)
        result = drive_study(parts, cap=cap)
    finally:
        pump.stop()

    used_after = PT.posts_used(ctx, lane=lane, lock=lock, spend=spend)
    posts = used_after - used_before
    state = S.ResearchState(ctx)
    study_state = state.study_state(study)
    judge = _judge_block(ctx, study)
    claim_present = ctx.exists(f"claim/submission_{study}.json")
    done = bool(study_state in STUDY_DONE_STATES and claim_present)
    status = "COMPLETED" if done else "INCOMPLETE"

    report: "dict[str, Any]" = {
        "schema": REPORT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "study": study, "lane": lane,
        "envelope_id": ctx.envelope_id, "lock_sha256": lock.sha256,
        "lock_verified": bool(freeze_report.get("ok")),
        "image": image, "batch_id": batch_id,
        "started_at_utc": started, "ended_at_utc": clock(),
        "status": status, "study_state": study_state,
        "stopped": result.stopped, "terminal": bool(result.terminal),
        "policy": {"strict_first_error": False, "self_repair": True,
                   "post_cap": cap},
        "posts": {"used_before": used_before, "used_after": used_after,
                  "this_attempt": posts, "allowed": cap},
        "actions": result.actions, "ok_count": result.ok_count,
        "claim_submitted": claim_present, "judge": judge,
        "root_before": root_before, "root_after": root_before,
        "notes": ("正式研究轨迹：判据由独立 judge 给出，驱动不改判、不补写；"
                  "合同内失败照常计数。"),
    }
    if done:
        state.advance_root(STUDY_NEXT[study],
                           reason=f"study {study} 终态 {study_state}（judge "
                                  f"{judge.get('verdict')}）")
        report["root_after"] = state.root_state()
    ctx.write_json(rel, report)
    return report


__all__ = [
    "REPORT_SCHEMA", "SCHEMA_VERSION", "STUDY_DONE_STATES", "STUDY_GATE",
    "STUDY_LANE", "STUDY_NEXT", "STUDY_POSTS", "StudyError", "StudyParts",
    "StudyResult", "backend_factory_for", "build_study_parts", "drive_study",
    "report_relpath", "run_study",
]
