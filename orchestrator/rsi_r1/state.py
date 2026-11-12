"""RSI-R1 状态机与恢复：root/study 状态、append-only 事件日志、确认单批（AC14）。

纪律（Plan §5.5、§9 T2）：

* **root**：``INIT→AUTHORIZED→CALIBRATED→FROZEN→PROBED→PRACTICED→STUDY_A→
  STUDY_B→REPLAY→REVIEW→CLOSED``，只许向前；``CLOSED`` 可从任意未终结态进入。
* **study**：``EXPLORING→CONFIRM_LOCKED→CONFIRM_RUNNING→RESULTS_RELEASED→
  CLAIM_SUBMITTED→VERIFIED | VALID_FAIL | BLOCKED | INVALID | ASSISTED``。
* ``propose_plan`` 只在 EXPLORING 追加修订；``freeze_confirm`` 原子绑定
  plan/design/analysis hash、32 block 分布、主对比与指标，之后分析程序不可改。
* ``run_confirm`` **一次**：先落持久 intent，再执行；重复 token 拒绝；
  未闭 intent 遇崩溃 ⇒ ``TOOL_OUTCOME_UNKNOWN``，**禁止自动重跑**；
  已有完整可信结果时只做幂等发布，不再物理执行。
* 所有事实走 **append-only 事件日志**（JSONL + 前后 sha 链 + seq 连续），
  崩溃恢复只靠日志，不靠内存。日志篡改/断链一律 fail-closed。
* 本模块不触网、不调模型、不跑仿真；文件经 ``CampaignContext`` 写守卫落盘。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r1 import analysis_receipt as AR
from orchestrator.rsi_r1 import contracts as C

EVENT_SCHEMA = "simpraxis.rsi_r1.event.v1"
SNAPSHOT_SCHEMA = "simpraxis.rsi_r1.state_snapshot.v1"
JOURNAL_RELPATH = "state/journal.jsonl"

# ---------------------------------------------------------------------------
# root 状态
# ---------------------------------------------------------------------------

ROOT_INIT = "INIT"
ROOT_AUTHORIZED = "AUTHORIZED"
ROOT_CALIBRATED = "CALIBRATED"
ROOT_FROZEN = "FROZEN"
ROOT_PROBED = "PROBED"
ROOT_PRACTICED = "PRACTICED"
ROOT_STUDY_A = "STUDY_A"
ROOT_STUDY_B = "STUDY_B"
ROOT_REPLAY = "REPLAY"
ROOT_REVIEW = "REVIEW"
ROOT_CLOSED = "CLOSED"

ROOT_ORDER: "tuple[str, ...]" = (
    ROOT_INIT, ROOT_AUTHORIZED, ROOT_CALIBRATED, ROOT_FROZEN, ROOT_PROBED,
    ROOT_PRACTICED, ROOT_STUDY_A, ROOT_STUDY_B, ROOT_REPLAY, ROOT_REVIEW,
    ROOT_CLOSED,
)
ROOT_TERMINAL = frozenset({ROOT_CLOSED})

# ---------------------------------------------------------------------------
# study 状态
# ---------------------------------------------------------------------------

ST_EXPLORING = "EXPLORING"
ST_CONFIRM_LOCKED = "CONFIRM_LOCKED"
ST_CONFIRM_RUNNING = "CONFIRM_RUNNING"
ST_RESULTS_RELEASED = "RESULTS_RELEASED"
ST_CLAIM_SUBMITTED = "CLAIM_SUBMITTED"
ST_VERIFIED = "VERIFIED"
ST_VALID_FAIL = "VALID_FAIL"
ST_BLOCKED = "BLOCKED"
ST_INVALID = "INVALID"
ST_ASSISTED = "ASSISTED"

STUDY_ORDER: "tuple[str, ...]" = (
    ST_EXPLORING, ST_CONFIRM_LOCKED, ST_CONFIRM_RUNNING, ST_RESULTS_RELEASED,
    ST_CLAIM_SUBMITTED,
)
STUDY_TERMINAL = frozenset({ST_VERIFIED, ST_VALID_FAIL, ST_BLOCKED, ST_INVALID,
                            ST_ASSISTED})
#: 允许的向前跳（正常顺序之外只有这些）；其余跳转一律拒绝。
STUDY_FORWARD_EXTRA: "Mapping[str, tuple[str, ...]]" = {
    ST_EXPLORING: (ST_BLOCKED, ST_INVALID, ST_ASSISTED),
    ST_CONFIRM_LOCKED: (ST_BLOCKED, ST_INVALID, ST_ASSISTED),
    ST_CONFIRM_RUNNING: (ST_BLOCKED, ST_INVALID),
    ST_RESULTS_RELEASED: (ST_BLOCKED, ST_VERIFIED, ST_VALID_FAIL, ST_INVALID),
    ST_CLAIM_SUBMITTED: (ST_VERIFIED, ST_VALID_FAIL, ST_INVALID, ST_ASSISTED),
}

STUDIES = ("A", "B")

PRODUCER_CANDIDATE = "candidate_model"
PRODUCER_MAINTAINER = "maintainer"
ARTIFACT_KINDS = ("design", "analysis")

OUTCOME_OK = "OK"
OUTCOME_INFRA_FAIL = "INFRA_FAIL"

RECOVERY_IDLE = "IDLE"
RECOVERY_RESUME_PUBLISH = "RESUME_PUBLISH"
RECOVERY_STOP_UNKNOWN = "STOP_UNKNOWN"


class StateError(RuntimeError):
    """状态机/日志/确认批次错误；``code`` 是公开拒绝条款。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_doc(obj: Any) -> str:
    return hashlib.sha256(C.canonical_json(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# append-only 日志
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    seq: int
    ts_iso: str
    event: str
    payload: Mapping[str, Any]
    prev_sha256: "str | None"
    sha256: str

    def as_dict(self) -> "dict[str, Any]":
        return {"schema": EVENT_SCHEMA, "seq": self.seq, "ts_iso": self.ts_iso,
                "event": self.event, "payload": dict(self.payload),
                "prev_sha256": self.prev_sha256, "sha256": self.sha256}


def _event_sha(seq: int, ts_iso: str, event: str, payload: Mapping[str, Any],
               prev: "str | None") -> str:
    return _sha_doc({"schema": EVENT_SCHEMA, "seq": seq, "ts_iso": ts_iso,
                     "event": event, "payload": payload, "prev_sha256": prev})


class Journal:
    """campaign 内单文件事件日志：flock 串行、fsync 落盘、前后 sha 链。"""

    def __init__(self, ctx: Any, *, relpath: str = JOURNAL_RELPATH,
                 clock: Callable[[], str] = _utc_now_z,
                 on_event: "Callable[[Event], None] | None" = None):
        self.ctx = ctx
        self.relpath = relpath
        self.clock = clock
        self.on_event = on_event
        self.path = ctx.relative(relpath)

    # ---- 读 ----

    def events(self) -> "list[Event]":
        p = self.path
        if not p.exists():
            return []
        out: "list[Event]" = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StateError("JOURNAL_TAMPERED", f"第 {i} 行不是 JSON：{exc}") from None
            if doc.get("schema") != EVENT_SCHEMA:
                raise StateError("JOURNAL_TAMPERED", f"第 {i} 行 schema 不符")
            prev = out[-1].sha256 if out else None
            if doc.get("seq") != len(out) + 1:
                raise StateError("JOURNAL_TAMPERED",
                                 f"第 {i} 行 seq={doc.get('seq')} 不连续")
            if doc.get("prev_sha256") != prev:
                raise StateError("JOURNAL_TAMPERED", f"第 {i} 行 prev_sha256 断链")
            want = _event_sha(int(doc["seq"]), str(doc["ts_iso"]),
                              str(doc["event"]), doc.get("payload") or {}, prev)
            if doc.get("sha256") != want:
                raise StateError("JOURNAL_TAMPERED", f"第 {i} 行 sha256 不符")
            out.append(Event(seq=int(doc["seq"]), ts_iso=str(doc["ts_iso"]),
                             event=str(doc["event"]),
                             payload=doc.get("payload") or {},
                             prev_sha256=prev, sha256=str(doc["sha256"])))
        return out

    # ---- 写 ----

    def append(self, event: str, payload: "Mapping[str, Any] | None" = None) -> Event:
        target = self.ctx.assert_write_allowed(self.path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = target.with_name(target.name + ".lock")
        fd_lock = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd_lock, fcntl.LOCK_EX)
            existing = self.events()
            prev = existing[-1].sha256 if existing else None
            seq = len(existing) + 1
            ts = self.clock()
            sha = _event_sha(seq, ts, event, payload or {}, prev)
            line = json.dumps({"schema": EVENT_SCHEMA, "seq": seq, "ts_iso": ts,
                               "event": event, "payload": payload or {},
                               "prev_sha256": prev, "sha256": sha},
                              ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n"
            fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(fd_lock, fcntl.LOCK_UN)
            os.close(fd_lock)
        ev = Event(seq=seq, ts_iso=ts, event=event, payload=dict(payload or {}),
                   prev_sha256=prev, sha256=sha)
        if self.on_event is not None:
            self.on_event(ev)
        return ev

    def append_once(self, event: str, payload: "Mapping[str, Any]",
                    *, key: str) -> Event:
        """幂等追加：同 ``event`` 且 payload[key] 相同的事件已存在则直接返回。"""
        for ev in self.events():
            if ev.event == event and ev.payload.get(key) == payload.get(key):
                return ev
        return self.append(event, payload)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactRecord:
    study: str
    kind: str
    source_sha256: str
    producer: str
    response_id: "str | None"
    seq: int
    assisted: bool


@dataclass(frozen=True)
class ConfirmLock:
    study: str
    plan_sha256: str
    design_source_sha256: str
    analysis_source_sha256: str
    distribution_sha256: str
    block_count: int
    primary_id: str
    metric_id: str
    token: str
    #: R1.2（T4）：冻结时绑定的候选来源与探索分析凭据（legacy 恒 None）。
    analysis_reply_id: "str | None" = None
    analysis_action_id: "str | None" = None
    analysis_receipt_sha256: "str | None" = None
    design_reply_id: "str | None" = None
    design_action_id: "str | None" = None

    def as_dict(self) -> "dict[str, Any]":
        return {"study": self.study, "plan_sha256": self.plan_sha256,
                "design_source_sha256": self.design_source_sha256,
                "analysis_source_sha256": self.analysis_source_sha256,
                "distribution_sha256": self.distribution_sha256,
                "block_count": self.block_count, "primary_id": self.primary_id,
                "metric_id": self.metric_id, "token": self.token,
                "analysis_reply_id": self.analysis_reply_id,
                "analysis_action_id": self.analysis_action_id,
                "analysis_receipt_sha256": self.analysis_receipt_sha256,
                "design_reply_id": self.design_reply_id,
                "design_action_id": self.design_action_id}


@dataclass(frozen=True)
class Intent:
    intent_id: str
    study: str
    token: str
    specs_sha256: str
    seq: int
    result_sha256: "str | None" = None
    outcome: "str | None" = None
    published: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    mode: str
    study: str
    intent_id: "str | None" = None
    detail: str = ""


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class ResearchState:
    """从事件日志折叠出的状态机 + 受控写入口。"""

    def __init__(self, ctx: Any, *, journal: "Journal | None" = None):
        self.ctx = ctx
        self.journal = journal or Journal(ctx)
        # 构造即校验日志完整性：断链/篡改在第一次读时就抛。
        self.journal.events()

    # ---- 折叠 ----

    def _events(self) -> "list[Event]":
        return self.journal.events()

    def root_state(self) -> str:
        state = ROOT_INIT
        for ev in self._events():
            if ev.event == "root_transition":
                state = str(ev.payload["to"])
        return state

    def study_state(self, study: str) -> str:
        _require_study(study)
        state = ST_EXPLORING
        for ev in self._events():
            if ev.event == "study_transition" and ev.payload.get("study") == study:
                state = str(ev.payload["to"])
        return state

    def study_status(self, study: str) -> "dict[str, Any]":
        """对候选可见的公开状态：状态、产物 hash、确认锁、未闭 intent。"""
        _require_study(study)
        state = self.study_state(study)
        artifacts: "dict[str, list[str]]" = {"design": [], "analysis": []}
        lock: "ConfirmLock | None" = None
        intent: "Intent | None" = None
        published: "str | None" = None
        for ev in self._events():
            p = ev.payload
            if ev.event == "artifact" and p.get("study") == study:
                artifacts[str(p["kind"])].append(str(p["source_sha256"]))
            elif ev.event == "confirm_lock" and p.get("study") == study:
                base = {k: p[k] for k in (
                    "study", "plan_sha256", "design_source_sha256",
                    "analysis_source_sha256", "distribution_sha256", "block_count",
                    "primary_id", "metric_id", "token")}
                for key in ("analysis_reply_id", "analysis_action_id",
                            "analysis_receipt_sha256", "design_reply_id",
                            "design_action_id"):
                    if p.get(key) is not None:
                        base[key] = p[key]
                lock = ConfirmLock(**base)
            elif ev.event == "confirm_intent" and p.get("study") == study:
                intent = Intent(intent_id=str(p["intent_id"]), study=study,
                                token=str(p["token"]), specs_sha256=str(p["specs_sha256"]),
                                seq=ev.seq)
            elif ev.event == "confirm_result" and p.get("study") == study:
                if intent is not None and intent.intent_id == p.get("intent_id"):
                    intent = Intent(
                        intent_id=intent.intent_id, study=study, token=intent.token,
                        specs_sha256=intent.specs_sha256, seq=intent.seq,
                        result_sha256=str(p.get("result_sha256") or ""),
                        outcome=str(p.get("outcome") or ""),
                        published=intent.published)
            elif ev.event == "confirm_publish" and p.get("study") == study:
                published = str(p.get("manifest_sha256") or "")
                if intent is not None:
                    intent = Intent(
                        intent_id=intent.intent_id, study=study, token=intent.token,
                        specs_sha256=intent.specs_sha256, seq=intent.seq,
                        result_sha256=intent.result_sha256,
                        outcome=intent.outcome, published=True)
        return {"study": study, "state": state, "artifacts": artifacts,
                "confirm_lock": None if lock is None else lock.as_dict(),
                "open_intent": None if intent is None or intent.published
                else {"intent_id": intent.intent_id, "token": intent.token,
                      "result_sha256": intent.result_sha256,
                      "outcome": intent.outcome},
                "published_manifest_sha256": published}

    # ---- root ----

    def advance_root(self, to: str, *, reason: str = "") -> None:
        cur = self.root_state()
        if to not in ROOT_ORDER:
            raise StateError("UNKNOWN_STATE", f"root 未知状态 {to!r}")
        if cur in ROOT_TERMINAL:
            raise StateError("ROOT_CLOSED", f"root 已 {cur}，不可再转 {to}")
        if to == cur:
            raise StateError("NOOP_TRANSITION", f"root 已在 {cur}")
        if to != ROOT_CLOSED and ROOT_ORDER.index(to) <= ROOT_ORDER.index(cur):
            raise StateError("BACKWARD_TRANSITION", f"root {cur}→{to} 不是向前")
        if to != ROOT_CLOSED and ROOT_ORDER.index(to) != ROOT_ORDER.index(cur) + 1:
            raise StateError("STATE_SKIP", f"root {cur}→{to} 跳过中间状态")
        self.journal.append("root_transition",
                            {"from": cur, "to": to, "reason": reason})

    def close_root(self, *, reason: str) -> None:
        if self.root_state() == ROOT_CLOSED:
            return
        self.journal.append("root_transition",
                            {"from": self.root_state(), "to": ROOT_CLOSED,
                             "reason": reason})

    # ---- study ----

    def _advance_study(self, study: str, to: str, *, reason: str) -> None:
        cur = self.study_state(study)
        if to not in STUDY_ORDER and to not in STUDY_TERMINAL:
            raise StateError("UNKNOWN_STATE", f"study 未知状态 {to!r}")
        if cur in STUDY_TERMINAL:
            if to == cur:
                raise StateError("NOOP_TRANSITION", f"study 已在 {cur}")
            raise StateError("STUDY_TERMINAL", f"study {study} 已终结于 {cur}")
        if to == cur:
            raise StateError("NOOP_TRANSITION", f"study 已在 {cur}")
        allowed: "tuple[str, ...]"
        if cur in STUDY_ORDER and to in STUDY_ORDER:
            allowed = (STUDY_ORDER[STUDY_ORDER.index(cur) + 1]
                       if STUDY_ORDER.index(cur) + 1 < len(STUDY_ORDER) else "")
        else:
            allowed = STUDY_FORWARD_EXTRA.get(cur, ())
        if to not in (allowed if isinstance(allowed, tuple) else (allowed,)):
            raise StateError("ILLEGAL_TRANSITION", f"study {study} {cur}→{to} 不允许")
        self.journal.append("study_transition",
                            {"study": study, "from": cur, "to": to, "reason": reason})

    def cancel_study(self, study: str, *, reason: str) -> None:
        """未跑的分配标 ``CANCELLED_WITH_REASON``（分母不丢，§5.5）。"""
        _require_study(study)
        self.journal.append_once("study_cancelled",
                                 {"study": study, "reason": reason},
                                 key="study")

    def mark_assisted(self, study: str, *, reason: str) -> None:
        _require_study(study)
        cur = self.study_state(study)
        if cur == ST_ASSISTED:
            return
        if cur in STUDY_TERMINAL:
            raise StateError("STUDY_TERMINAL", f"study {study} 已终结于 {cur}")
        self.journal.append("study_transition",
                            {"study": study, "from": cur, "to": ST_ASSISTED,
                             "reason": reason})

    # ---- 模型回复与动作（AC10 谱系）----

    def record_reply(self, *, response_id: str, text: str, phase: str,
                     nonce: str = "", study: str = "") -> "dict[str, Any]":
        if not response_id:
            raise StateError("MISSING_KEY", "response_id 必填")
        rel = f"state/replies/{response_id}.txt"
        self.ctx.write_text(rel, text)
        payload = {
            "response_id": response_id, "text_sha256": _sha_text(text),
            "n_bytes": len(text.encode("utf-8")), "phase": phase,
            "nonce": nonce, "relpath": rel}
        if study:
            payload["study"] = str(study)
        return self.journal.append("reply", payload).payload | {
                "response_id": response_id, "text_sha256": _sha_text(text)}

    def record_action(self, *, response_id: str, action: Mapping[str, Any],
                      study: str = "") -> "dict[str, Any]":
        """把解析后的动作登记为事实：response→action_sha 的唯一绑定点。"""
        if not self._reply_event(response_id):
            raise StateError("UNKNOWN_REPLY",
                             f"response_id={response_id!r} 没有登记的原始回复")
        action_sha = _sha_doc(action)
        # 锁内适配（2026-11-12 T5）：幂等去重必须**按 study 作用域**判重。
        # 旧实现只比 (response_id, action_sha256)，跨 study 的同字节回合会被
        # 当成重复而静默跳过——A 的首呼与 B 的首呼回复逐字相同时正是如此：
        # B 的动作事件根本没落盘，``_binding`` 过滤 study 后判 ``STATE_UNKNOWN``。
        # 去重的本意是「同一 study 内重放同一动作不重记」，故单位是 study。
        scope = str(study) if study else None
        if any(ev.event == "action"
               and ev.payload.get("response_id") == response_id
               and ev.payload.get("action_sha256") == action_sha
               and (ev.payload.get("study") or None) == scope
               for ev in self._events()):
            return {"response_id": response_id, "action_sha256": action_sha}
        payload = {
            "response_id": response_id, "action_sha256": action_sha,
            "name": str(action.get("action") or ""),
            "payload": dict(action)}
        if study:
            payload["study"] = str(study)
        self.journal.append("action", payload)
        return {"response_id": response_id, "action_sha256": action_sha}

    def _reply_event(self, response_id: str) -> "Event | None":
        for ev in self._events():
            if ev.event == "reply" and ev.payload.get("response_id") == response_id:
                return ev
        return None

    def register_artifact(self, *, study: str, kind: str, source: str,
                          response_id: "str | None" = None,
                          action_sha256: "str | None" = None) -> ArtifactRecord:
        """登记候选/维护侧源码。候选须绑定已登记回复与动作（AC10）。"""
        _require_study(study)
        if kind not in ARTIFACT_KINDS:
            raise StateError("UNKNOWN_ARTIFACT_KIND", kind)
        source_sha = _sha_text(source)
        prior = self._artifact_producer(source_sha)
        if response_id is None:
            if action_sha256 is not None:
                raise StateError("MISSING_KEY", "维护侧改动不得携带 action_sha256")
            self.journal.append("artifact", {
                "study": study, "kind": kind, "source_sha256": source_sha,
                "producer": PRODUCER_MAINTAINER, "response_id": None,
                "original_producer": prior, "assisted": True})
            self.mark_assisted(study, reason=f"维护侧改动 {kind} {source_sha[:12]}")
            return ArtifactRecord(study=study, kind=kind, source_sha256=source_sha,
                                  producer=PRODUCER_MAINTAINER, response_id=None,
                                  seq=self._events()[-1].seq, assisted=True)
        reply = self._reply_event(response_id)
        if reply is None:
            raise StateError("UNKNOWN_REPLY",
                             f"response_id={response_id!r} 没有登记的原始回复")
        # 锁内适配（2026-11-12 T5 独立评审：跨 study 盗绑）。回复带 study 标签时，
        # 只能用**同一 study** 的回合登记产物；否则 B 可以拿 A 的回复+动作把
        # 自己的产物伪装成候选产物（跨 study 污染）。无 study 标签的旧件不拦。
        bound_study = reply.payload.get("study")
        if bound_study and str(bound_study) != study:
            raise StateError(
                "STUDY_MISMATCH",
                f"回复绑定 study={bound_study!r}，登记 study={study!r}")
        if not action_sha256:
            raise StateError("MISSING_KEY",
                             "候选产物必须携带 action_sha256（response→source 绑定）")
        if not any(ev.event == "action"
                   and ev.payload.get("response_id") == response_id
                   and ev.payload.get("action_sha256") == action_sha256
                   for ev in self._events()):
            raise StateError("UNBOUND_SOURCE",
                             "该动作不是从这条回复派发的（预造 successor 不可信）")
        self.ctx.write_text(f"designs/{source_sha}.py", source)
        self.journal.append("artifact", {
            "study": study, "kind": kind, "source_sha256": source_sha,
            "producer": PRODUCER_CANDIDATE, "response_id": response_id,
            "action_sha256": action_sha256, "original_producer": prior,
            "assisted": False})
        return ArtifactRecord(study=study, kind=kind, source_sha256=source_sha,
                              producer=PRODUCER_CANDIDATE, response_id=response_id,
                              seq=self._events()[-1].seq, assisted=False)

    def _artifact_producer(self, source_sha: str) -> "str | None":
        for ev in self._events():
            if ev.event == "artifact" and ev.payload.get("source_sha256") == source_sha:
                return str(ev.payload.get("producer") or "")
        return None

    def _artifact_event(self, source_sha: str) -> "dict[str, Any]":
        for ev in reversed(self._events()):
            if ev.event == "artifact" \
                    and ev.payload.get("source_sha256") == source_sha:
                return dict(ev.payload)
        raise StateError("UNBOUND_SOURCE", f"{source_sha[:12]} 未登记为产物")

    def _latest_explore_plan_sha(self, study: str) -> "str | None":
        latest = None
        for ev in self._events():
            if ev.event == "plan_revision" \
                    and ev.payload.get("study") == study:
                doc = ev.payload.get("doc") or {}
                if doc.get("phase") == "explore":
                    try:
                        latest = C.ResearchPlan.from_dict(doc).sha256
                    except C.ContractError:
                        latest = None
        return latest

    # ---- 计划修订 ----

    def append_plan(self, study: str, plan: Mapping[str, Any]) -> "dict[str, Any]":
        _require_study(study)
        if self.study_state(study) != ST_EXPLORING:
            raise StateError("PLAN_NOT_EDITABLE",
                             f"study {study} 在 {self.study_state(study)} 不接受新修订")
        doc = C.ResearchPlan.from_dict(plan)
        if doc.study_id != study:
            raise StateError("STUDY_MISMATCH", f"plan.study_id={doc.study_id} != {study}")
        if doc.phase != "explore":
            raise StateError("PLAN_PHASE_INVALID",
                             "EXPLORING 只接受 phase=explore 的修订")
        prior = [ev.payload for ev in self._events()
                 if ev.event == "plan_revision" and ev.payload.get("study") == study]
        if prior and doc.revision != max(int(p["revision"]) for p in prior) + 1:
            raise StateError("REVISION_GAP",
                             f"revision={doc.revision} 不是下一个修订号")
        if not prior and doc.revision != 1:
            raise StateError("REVISION_GAP", "首个修订必须是 revision=1")
        self.journal.append("plan_revision", {
            "study": study, "revision": doc.revision, "plan_sha256": doc.sha256,
            "parent_sha256": prior[-1]["plan_sha256"] if prior else None,
            "doc": plan})
        return {"study": study, "revision": doc.revision, "plan_sha256": doc.sha256}

    # ---- 确认锁与单批 ----

    def freeze_confirm(self, *, study: str, plan: Mapping[str, Any],
                       design_sha256: str, analysis_sha256: str,
                       r12: bool = False,
                       ) -> ConfirmLock:
        _require_study(study)
        state = self.study_state(study)
        if state in STUDY_TERMINAL:
            # 终态比「不是 EXPLORING」更具体：候选/维护者需要知道重试无意义
            raise StateError("STUDY_TERMINAL", f"study {study} 已终结于 {state}")
        if state != ST_EXPLORING:
            raise StateError("CONFIRM_STATE",
                             f"study {study} 在 {state}，只有 EXPLORING 可冻结")
        doc = C.ResearchPlan.from_dict(plan)
        if doc.study_id != study:
            raise StateError("STUDY_MISMATCH", f"plan.study_id={doc.study_id} != {study}")
        check = C.validate_plan(doc, state=ST_EXPLORING)
        for sha, label in ((design_sha256, "design"), (analysis_sha256, "analysis")):
            C._require_sha256_hex(sha, where=f"freeze.{label}")
            if self._artifact_producer(sha) is None:
                raise StateError("UNBOUND_SOURCE",
                                 f"{label} 源码 {sha[:12]} 未登记为产物")
        binding: "dict[str, Any]" = {}
        if r12:
            # T4：plan/制品登记/实际文件字节/探索凭据 四者一致；确认后
            # 不允许同路径换源码、也不允许 plan 指向另一个合法 hash。
            if doc.analysis_source_sha != analysis_sha256:
                raise StateError(
                    "SOURCE_SHA_MISMATCH",
                    "plan.analysis_source_sha 与 freeze.analysis_sha256 不一致")
            if doc.design_source_sha != design_sha256:
                raise StateError(
                    "SOURCE_SHA_MISMATCH",
                    "plan.design_source_sha 与 freeze.design_sha256 不一致")
            for sha, label in ((design_sha256, "design"),
                               (analysis_sha256, "analysis")):
                path = self.ctx.relative(f"designs/{sha}.py")
                if not path.is_file():
                    raise StateError("SOURCE_FILE_MISSING", f"designs/{sha[:12]}.py")
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise StateError("SOURCE_FILE_DRIFT",
                                     f"{label} 登记 hash 与磁盘字节不一致")
            art = self._artifact_event(analysis_sha256)
            receipt_rel = f"private/receipts/{study}-explore.json"
            if not self.ctx.exists(receipt_rel):
                raise StateError("ANALYSIS_RECEIPT_MISSING",
                                 "冻结前必须有探索期分析执行凭据")
            receipt = self.ctx.read_json(receipt_rel)
            if not isinstance(receipt, Mapping) \
                    or receipt.get("schema") != AR.SCHEMA:
                raise StateError("ANALYSIS_RECEIPT_INVALID", "凭据结构非法")
            if receipt.get("source_sha256") != analysis_sha256 \
                    or receipt.get("phase") != "explore":
                raise StateError("SOURCE_SHA_MISMATCH",
                                 "探索凭据不属于冻结的分析源码")
            if not receipt.get("validity"):
                raise StateError("ANALYSIS_INVALID",
                                 "探索期分析未通过执行有效门")
            explore_sha = self._latest_explore_plan_sha(study)
            if explore_sha is None or receipt.get("plan_sha256") != explore_sha:
                raise StateError("RECEIPT_PLAN_MISMATCH",
                                 "探索凭据的 plan sha 不是当前探索计划")
            binding = {
                "analysis_reply_id": art.get("response_id"),
                "analysis_action_id": art.get("action_sha256"),
                "analysis_receipt_sha256": hashlib.sha256(
                    self.ctx.relative(receipt_rel).read_bytes()).hexdigest(),
            }
            design_art = self._artifact_event(design_sha256)
            binding["design_reply_id"] = design_art.get("response_id")
            binding["design_action_id"] = design_art.get("action_sha256")
        lock_payload = {
            "study": study, "plan_sha256": doc.sha256,
            "design_source_sha256": design_sha256,
            "analysis_source_sha256": analysis_sha256,
            "distribution_sha256": _sha_doc(_distribution_doc(doc)),
            "block_count": int(doc.nuisance_distribution.block_count),
            "primary_id": doc.primary.id,
            "metric_id": doc.primary.metric,
        }
        payload = dict(lock_payload)
        payload["token"] = _sha_doc(lock_payload)[:32]
        payload.update(binding)
        payload["checks"] = [list(c) for c in check.checks]
        self.journal.append("confirm_lock", payload)
        self._advance_study(study, ST_CONFIRM_LOCKED, reason="freeze_confirm")
        return ConfirmLock(**lock_payload, token=payload["token"], **binding)

    def confirm_lock(self, study: str) -> "ConfirmLock":
        doc = self.study_status(study)["confirm_lock"]
        if doc is None:
            raise StateError("CONFIRM_NOT_LOCKED", f"study {study} 尚未冻结")
        return ConfirmLock(**doc)

    def begin_confirm(self, *, study: str, token: str, specs_sha256: str) -> Intent:
        _require_study(study)
        state = self.study_state(study)
        lock = self.confirm_lock(study)
        if token != lock.token:
            raise StateError("CONFIRM_TOKEN_MISMATCH", "确认 token 与冻结件不一致")
        C._require_sha256_hex(specs_sha256, where="confirm.specs_sha256")
        already = [ev for ev in self._events()
                   if ev.event == "confirm_intent" and ev.payload.get("study") == study]
        if already:
            # §5.5：一次批次，重复 token 拒绝（恢复走 publish 幂等，不开新批）。
            # 这条先于状态判定：状态 CONFIRM_RUNNING 时最具体的拒绝是「已开过批」，
            # 而不是泛化的「状态不对」——后者会让候选误以为换个时机就能重跑。
            raise StateError("CONFIRM_ALREADY_RUN",
                             "该 study 的确认批次已开过；不得重跑")
        if state != ST_CONFIRM_LOCKED:
            raise StateError("CONFIRM_STATE",
                             f"study {study} 在 {state}，只有 CONFIRM_LOCKED 可开批")
        intent_id = f"confirm-{study}-{lock.token[:12]}"
        self.journal.append("confirm_intent", {
            "intent_id": intent_id, "study": study, "token": token,
            "specs_sha256": specs_sha256})
        self._advance_study(study, ST_CONFIRM_RUNNING, reason="run_confirm")
        return Intent(intent_id=intent_id, study=study, token=token,
                      specs_sha256=specs_sha256, seq=self._events()[-1].seq)

    def complete_confirm(self, *, intent_id: str, result_sha256: str,
                         outcome: str) -> None:
        intent = self._intent(intent_id)
        if intent is None:
            raise StateError("UNKNOWN_INTENT", intent_id)
        if intent.result_sha256 is not None:
            if intent.result_sha256 == result_sha256 and intent.outcome == outcome:
                return  # 幂等：同样的结果重复登记不改变事实
            raise StateError("INTENT_ALREADY_CLOSED",
                             "该 intent 已有不同的结果；不得覆盖")
        if outcome not in (OUTCOME_OK, OUTCOME_INFRA_FAIL):
            raise StateError("UNKNOWN_OUTCOME", outcome)
        C._require_sha256_hex(result_sha256, where="confirm.result_sha256")
        self.journal.append("confirm_result", {
            "intent_id": intent_id, "study": intent.study,
            "result_sha256": result_sha256, "outcome": outcome})
        if outcome == OUTCOME_INFRA_FAIL:
            self._advance_study(intent.study, ST_BLOCKED,
                                reason="基础设施丢失某 run：整批 BLOCKED，不补样本")

    def publish_confirm(self, *, intent_id: str, manifest_sha256: str,
                        validated: bool) -> bool:
        """校验全批后发布；同一 intent 重复发布返回 True 且不写第二行。"""
        intent = self._intent(intent_id)
        if intent is None:
            raise StateError("UNKNOWN_INTENT", intent_id)
        if intent.published:
            return True
        if intent.result_sha256 is None or intent.outcome is None:
            raise StateError("INTENT_OPEN", "结果尚未登记，禁止发布")
        if intent.outcome != OUTCOME_OK:
            raise StateError("CONFIRM_BLOCKED", "批次结果非 OK，不可发布")
        if not validated:
            raise StateError("NOT_VALIDATED", "发布前必须完成全批校验")
        C._require_sha256_hex(manifest_sha256, where="confirm.manifest_sha256")
        self.journal.append("confirm_publish", {
            "intent_id": intent_id, "study": intent.study,
            "manifest_sha256": manifest_sha256})
        self._advance_study(intent.study, ST_RESULTS_RELEASED,
                            reason="confirm 全批校验通过并发布")
        return True

    def _intent(self, intent_id: str) -> "Intent | None":
        intent: "Intent | None" = None
        for ev in self._events():
            p = ev.payload
            if ev.event == "confirm_intent" and p.get("intent_id") == intent_id:
                intent = Intent(intent_id=intent_id, study=str(p["study"]),
                                token=str(p["token"]),
                                specs_sha256=str(p["specs_sha256"]), seq=ev.seq)
            elif (intent is not None and ev.event == "confirm_result"
                  and p.get("intent_id") == intent_id):
                intent = Intent(intent_id=intent_id, study=intent.study,
                                token=intent.token, specs_sha256=intent.specs_sha256,
                                seq=intent.seq,
                                result_sha256=str(p["result_sha256"]),
                                outcome=str(p["outcome"]), published=intent.published)
            elif (intent is not None and ev.event == "confirm_publish"
                  and p.get("intent_id") == intent_id):
                intent = Intent(intent_id=intent_id, study=intent.study,
                                token=intent.token, specs_sha256=intent.specs_sha256,
                                seq=intent.seq,
                                result_sha256=intent.result_sha256,
                                outcome=intent.outcome, published=True)
        return intent

    def recover(self, study: str, *,
                trusted_result_sha256: "str | None" = None) -> RecoveryDecision:
        """崩溃恢复裁决：绝不自动重跑，已有可信结果只做重发布。"""
        _require_study(study)
        status = self.study_status(study)
        intent = status["open_intent"]
        if intent is None:
            return RecoveryDecision(RECOVERY_IDLE, study, detail="无未闭 intent")
        intent_id = str(intent["intent_id"])
        known = self._intent(intent_id)
        if known is None:
            return RecoveryDecision(RECOVERY_IDLE, study, detail="intent 未知")
        if known.result_sha256 is not None and known.outcome == OUTCOME_OK:
            return RecoveryDecision(RECOVERY_RESUME_PUBLISH, study,
                                    intent_id=intent_id,
                                    detail="结果已登记，仅幂等发布")
        if trusted_result_sha256 is not None:
            return RecoveryDecision(RECOVERY_RESUME_PUBLISH, study,
                                    intent_id=intent_id,
                                    detail="发现可信结果，登记后幂等发布")
        return RecoveryDecision(RECOVERY_STOP_UNKNOWN, study, intent_id=intent_id,
                                detail="intent 未闭合且无可信结果：TOOL_OUTCOME_UNKNOWN")

    # ---- 提交 ----

    def submit_claim(self, *, study: str, claims: Sequence[Mapping[str, Any]],
                     report_text: str, response_id: str) -> "dict[str, Any]":
        _require_study(study)
        state = self.study_state(study)
        if state != ST_RESULTS_RELEASED:
            raise StateError("CLAIM_STATE",
                             f"study {study} 在 {state}，只有 RESULTS_RELEASED 可提交")
        if not isinstance(report_text, str) or not report_text.strip():
            raise StateError("REPORT_MISSING", "submit_claim 必须携带非空报告正文")
        n_bytes = len(report_text.encode("utf-8"))
        if n_bytes > REPORT_MAX_BYTES:
            raise StateError("REPORT_TOO_LARGE", f"{n_bytes} > {REPORT_MAX_BYTES} 字节")
        if not self._reply_event(response_id):
            raise StateError("UNKNOWN_REPLY", response_id)
        report_sha = _sha_text(report_text)
        report_artifact_id = f"report-{study}-{report_sha[:12]}"
        self.journal.append("claim", {
            "study": study, "response_id": response_id,
            "claims": [dict(c) for c in claims],
            "report_text_sha256": report_sha, "report_bytes": n_bytes,
            "report_artifact_id": report_artifact_id,
            "producer": PRODUCER_CANDIDATE})
        self.ctx.write_text(f"research_packets/{study}/{report_artifact_id}.txt",
                            report_text)
        self._advance_study(study, ST_CLAIM_SUBMITTED, reason="submit_claim")
        return {"study": study, "report_artifact_id": report_artifact_id,
                "report_text_sha256": report_sha}

    # ---- 终局（来自 judge，不来自 submit 被接收）----

    def conclude(self, study: str, verdict: str, *, reason: str) -> None:
        _require_study(study)
        allowed = (ST_VERIFIED, ST_VALID_FAIL, ST_INVALID)
        if verdict not in allowed:
            raise StateError("UNKNOWN_VERDICT", verdict)
        if self.study_state(study) != ST_CLAIM_SUBMITTED:
            raise StateError("CONCLUSION_STATE",
                             "只有 CLAIM_SUBMITTED 才可由判据给终局")
        self.journal.append("study_conclusion",
                            {"study": study, "verdict": verdict, "reason": reason})
        self._advance_study(study, verdict, reason=reason)

    # ---- 快照 ----

    def snapshot(self) -> "dict[str, Any]":
        return {"schema": SNAPSHOT_SCHEMA, "schema_version": 1,
                "root_state": self.root_state(),
                "studies": {s: self.study_status(s) for s in STUDIES},
                "events": len(self._events())}


#: §5.2：submit_claim 正文 UTF-8 ≤ 24 KiB。
REPORT_MAX_BYTES = 24 * 1024


def _require_study(study: str) -> None:
    if study not in STUDIES:
        raise StateError("UNKNOWN_STUDY", f"{study!r} 不是 A/B")


def _distribution_doc(plan: C.ResearchPlan) -> "dict[str, Any]":
    d = plan.nuisance_distribution
    return {"generator": d.generator, "block_count": d.block_count,
            "varying": {k: list(v) for k, v in sorted(d.varying.items())}}


def load_state(ctx: Any) -> ResearchState:
    return ResearchState(ctx)
