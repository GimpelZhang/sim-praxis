"""RSI-R1 T5：人工时间与救援令的独立记账（Plan §8.4，AC25）。

三条不能含糊的规则：

1. **begin/end 成对**：每个 ``HumanEvent`` 都有开始与结束。缺 ``end`` 的事件
   记作 ``UNKNOWN_HUMAN_TIME``——**结案时不得填 0、不得估算**。
2. **未知不填 0**：汇总把「已知秒数」与「未知事件」分列；有未知事件时
   ``complete=False``，并阻塞任何「完整成本」声明（:func:`assert_complete_for_closeout`）。
3. **救援令独立且默认不成功**：``rescue`` 事件单独成行，``success`` 在创建时
   恒为 ``False``；只有外部裁决事件（:func:`adjudicate_rescue`）能把它改成
   ``True``。运维（``maintenance``）不自动变成研究救援，也不触发 assisted。

事件日志与状态机同纪律：append-only JSONL + seq/prev_sha 链（复用
:class:`orchestrator.rsi_r1.state.Journal`），路径
``human_events/events.jsonl``，0700/0600。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1.state import Journal

RELPATH = "human_events/events.jsonl"

#: Plan §8.4 登记的四种人工事件。
KINDS: "tuple[str, ...]" = ("approval", "maintenance", "scientific_review",
                            "rescue")

#: 触发 ``assisted`` 的人工接触类别（关键解释/代码修订）。
ASSISTED_TOUCHES: "tuple[str, ...]" = ("code", "interpretation")

EV_BEGIN = "human_event_begin"
EV_END = "human_event_end"
EV_ADJUDICATE = "human_event_rescue_adjudication"


class HumanEventError(RuntimeError):
    """人工时间记账错误（未知事件/重复结束/时间倒流/未裁决）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(value: str, *, where: str) -> datetime:
    """只接受严格 ``Z`` 结尾的 UTC 时间戳（与账本/timestamp 纪律一致）。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HumanEventError("TIMESTAMP_INVALID", f"{where}={value!r} 不是严格 Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise HumanEventError("TIMESTAMP_INVALID", f"{where}={value!r}: {exc}") from None


@dataclass(frozen=True)
class HumanEvent:
    event_id: str
    kind: str
    actor: str
    reason: str
    start_utc: str
    end_utc: "str | None"
    artifact_refs: "tuple[str, ...]"
    study: "str | None"
    touch: "str | None"
    success: bool
    adjudicated: bool

    @property
    def open(self) -> bool:
        return self.end_utc is None

    def duration_s(self) -> "float | None":
        """已结束事件的实际秒数；未结束返回 ``None``（**不是** 0）。"""
        if self.end_utc is None:
            return None
        start = parse_z(self.start_utc, where="start_utc")
        end = parse_z(self.end_utc, where="end_utc")
        return (end - start).total_seconds()

    def as_dict(self) -> "dict[str, Any]":
        doc = {"event_id": self.event_id, "kind": self.kind, "actor": self.actor,
               "reason": self.reason, "start_utc": self.start_utc,
               "end_utc": self.end_utc,
               "artifact_refs": list(self.artifact_refs),
               "study": self.study, "touch": self.touch,
               "duration_s": self.duration_s(),
               "unknown_human_time": self.open}
        if self.kind == "rescue":
            doc["success"] = self.success
            doc["adjudicated"] = self.adjudicated
        return doc


class HumanEventLog:
    """``human_events/events.jsonl`` 的读写面（append-only，shā 链）。"""

    def __init__(self, ctx: Any, *, clock=_utc_now_z) -> None:
        self.ctx = ctx
        self.journal = Journal(ctx, relpath=RELPATH, clock=clock)

    # ---- 读 ----

    def _fold(self) -> "list[HumanEvent]":
        rows: "dict[str, dict[str, Any]]" = {}
        order: "list[str]" = []
        for ev in self.journal.events():
            p = ev.payload
            if ev.event == EV_BEGIN:
                eid = str(p.get("event_id") or "")
                if not eid or eid in rows:
                    raise HumanEventError("DUPLICATE_EVENT_ID", eid)
                rows[eid] = {"event_id": eid, "kind": str(p.get("kind")),
                             "actor": str(p.get("actor")),
                             "reason": str(p.get("reason")),
                             "start_utc": str(p.get("start_utc")),
                             "end_utc": None,
                             "artifact_refs": tuple(p.get("artifact_refs") or ()),
                             "study": p.get("study"), "touch": p.get("touch"),
                             "success": False, "adjudicated": False}
                order.append(eid)
            elif ev.event == EV_END:
                eid = str(p.get("event_id") or "")
                if eid not in rows:
                    raise HumanEventError("UNKNOWN_EVENT", eid)
                if rows[eid]["end_utc"] is not None:
                    raise HumanEventError("ALREADY_ENDED", eid)
                rows[eid]["end_utc"] = str(p.get("end_utc"))
            elif ev.event == EV_ADJUDICATE:
                eid = str(p.get("event_id") or "")
                if eid not in rows:
                    raise HumanEventError("UNKNOWN_EVENT", eid)
                if rows[eid]["kind"] != "rescue":
                    raise HumanEventError("NOT_A_RESCUE", eid)
                rows[eid]["success"] = bool(p.get("success"))
                rows[eid]["adjudicated"] = True
        out: "list[HumanEvent]" = []
        for eid in order:
            row = rows[eid]
            ev = HumanEvent(**row)
            if ev.end_utc is not None:
                start = parse_z(ev.start_utc, where="start_utc")
                end = parse_z(ev.end_utc, where="end_utc")
                if end < start:
                    raise HumanEventError("TIME_REVERSED",
                                          f"{eid}: {ev.start_utc} → {ev.end_utc}")
            out.append(ev)
        return out

    def events(self) -> "list[HumanEvent]":
        return self._fold()

    def get(self, event_id: str) -> HumanEvent:
        for ev in self._fold():
            if ev.event_id == event_id:
                return ev
        raise HumanEventError("UNKNOWN_EVENT", event_id)

    # ---- 写 ----

    def begin(self, *, kind: str, actor: str, reason: str,
              artifact_refs: Sequence[str] = (), start_utc: "str | None" = None,
              study: "str | None" = None, touch: "str | None" = None,
              event_id: "str | None" = None) -> HumanEvent:
        if kind not in KINDS:
            raise HumanEventError("UNKNOWN_KIND", repr(kind))
        for name, value in (("actor", actor), ("reason", reason)):
            if not isinstance(value, str) or not value.strip():
                raise HumanEventError("MISSING_KEY", f"{name} 不得为空")
        if touch is not None and touch not in ASSISTED_TOUCHES:
            raise HumanEventError("UNKNOWN_TOUCH", repr(touch))
        start = start_utc or self.journal.clock()
        parse_z(start, where="start_utc")
        eid = event_id or f"he-{len(self.journal.events()) + 1:04d}-{kind}"
        self.journal.append(EV_BEGIN, {
            "event_id": eid, "kind": kind, "actor": actor, "reason": reason,
            "artifact_refs": list(artifact_refs), "start_utc": start,
            "study": study, "touch": touch})
        return self.get(eid)

    def end(self, *, event_id: str, end_utc: "str | None" = None) -> HumanEvent:
        current = self.get(event_id)
        if current.end_utc is not None:
            raise HumanEventError("ALREADY_ENDED", event_id)
        end = end_utc or self.journal.clock()
        parse_z(end, where="end_utc")
        if parse_z(end, where="end_utc") < parse_z(current.start_utc,
                                                   where="start_utc"):
            raise HumanEventError("TIME_REVERSED",
                                  f"{event_id}: {current.start_utc} → {end}")
        self.journal.append(EV_END, {"event_id": event_id, "end_utc": end})
        return self.get(event_id)

    # ---- 救援令（独立、默认不成功）----

    def record_rescue_order(self, *, actor: str, reason: str,
                            order_ref: str, start_utc: "str | None" = None,
                            end_utc: "str | None" = None,
                            study: "str | None" = None,
                            touch: "str | None" = None) -> HumanEvent:
        """登记一条救援令：``success`` 恒为 ``False``，且单独成行。

        救援令**不是**科学成功，也**不**自动解锁储备；只有另一次显式裁决
        （:meth:`adjudicate_rescue`）能改成功标记。
        """
        if not isinstance(order_ref, str) or not order_ref.strip():
            raise HumanEventError("MISSING_KEY", "order_ref 不得为空")
        ev = self.begin(kind="rescue", actor=actor, reason=reason,
                        artifact_refs=(order_ref,), start_utc=start_utc,
                        study=study, touch=touch)
        return self.end(event_id=ev.event_id,
                        end_utc=end_utc or start_utc or self.journal.clock())

    def adjudicate_rescue(self, *, event_id: str, success: bool,
                          actor: str, evidence_ref: str) -> HumanEvent:
        ev = self.get(event_id)
        if ev.kind != "rescue":
            raise HumanEventError("NOT_A_RESCUE", event_id)
        if ev.adjudicated:
            raise HumanEventError("ALREADY_ADJUDICATED", event_id)
        if not isinstance(actor, str) or not actor.strip():
            raise HumanEventError("MISSING_KEY", "actor 不得为空")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise HumanEventError("MISSING_KEY", "evidence_ref 不得为空")
        self.journal.append(EV_ADJUDICATE, {
            "event_id": event_id, "success": bool(success), "actor": actor,
            "evidence_ref": evidence_ref})
        return self.get(event_id)

    # ---- 汇总与结案门 ----

    def summary(self) -> "dict[str, Any]":
        events = self._fold()
        by_kind: "dict[str, dict[str, Any]]" = {}
        unknown: "list[str]" = []
        total_known = 0.0
        for ev in events:
            row = by_kind.setdefault(ev.kind, {"count": 0, "known_seconds": 0.0,
                                               "unknown": 0})
            row["count"] += 1
            duration = ev.duration_s()
            if duration is None:
                row["unknown"] += 1
                unknown.append(ev.event_id)
            else:
                row["known_seconds"] += duration
                total_known += duration
        rescues = [ev.as_dict() for ev in events if ev.kind == "rescue"]
        return {
            "schema": "simpraxis.rsi_r1.human_events_summary.v1",
            "schema_version": 1,
            "kinds": by_kind,
            "known_seconds_total": total_known,
            "unknown_event_ids": unknown,
            "unknown_human_time": bool(unknown),
            # 「完整成本」只在无未知事件时成立；未知**永远**不进 0。
            "complete": not unknown,
            "rescue_orders": rescues,
            "rescue_success_count": sum(1 for r in rescues if r["success"]),
            "n_events": len(events),
        }

    def assert_complete_for_closeout(self) -> None:
        summary = self.summary()
        if summary["unknown_event_ids"]:
            raise HumanEventError(
                "UNKNOWN_HUMAN_TIME",
                f"未结束人工事件 {summary['unknown_event_ids'][:5]}；"
                "结案不得填 0、不得估算")

    def apply_assisted_rule(self, state: Any, event_id: str, *,
                            include_rescue: bool = False) -> bool:
        """关键解释/代码修订触发 ``ASSISTED``；纯运维不触发。

        ``include_rescue=True`` 时，**实质性救援**（touch 为 code/interpretation）
        同样触发（R1.2 T3）；无效救援/运维命令仍不触发。返回是否实际把 study
        推进到 ``ASSISTED``。``state`` 是
        :class:`orchestrator.rsi_r1.state.ResearchState`（鸭子类型即可）。
        """
        ev = self.get(event_id)
        kinds = ("scientific_review", "rescue") if include_rescue \
            else ("scientific_review",)
        if ev.kind not in kinds:
            return False
        if ev.touch not in ASSISTED_TOUCHES:
            return False
        if ev.study is None:
            raise HumanEventError("MISSING_KEY",
                                  "触发 assisted 的人工事件必须携带 study")
        state.mark_assisted(ev.study, reason=f"{ev.kind}/{ev.touch}: {ev.reason}")
        return True


def load(ctx: Any, *, clock=_utc_now_z) -> HumanEventLog:
    return HumanEventLog(ctx, clock=clock)
