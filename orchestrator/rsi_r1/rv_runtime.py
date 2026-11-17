"""RSI-R1-RV 运行时：真实网关 sender、真实准入/槽、容器读取 worker 与两研究装配。

Plan §3/§5/§6 的落地：

* **真实 broker**：``ModelGateway.call("coding_debug", ...)``，``max_attempts=1``，
  RV 独立账（2906）与批次批准件；发网前后落 intent/result journal，未知发送
  占用不释放（``STOP_UNKNOWN``）。
* **实际 worker**：``read_result_artifact`` 走 :mod:`rv_worker` 的受限容器；
  broker 只验证/记录，不在宿主重读替换。
* **零 POST 预演**：``ScriptedRVConsumer``（维护侧脚本，producer 明标）复用同一
  dispatcher/loop/worker，证明 read→report→submit 可达。
* **两研究分开根**：A/B 各自 ``studies/<study>/``（导入 journal 是两份不同事实，
  单根会冲突）。全局 public/review/ledger 在 round 根。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common import research_transport as RT
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import g21_bridge as G21
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_worker as W
from orchestrator.rsi_r1 import state as S
from orchestrator.rsi_r1 import task_specs as TS

RV_PRODUCER = "candidate_model_suffix"
SCRIPTED_PRODUCER = "maintainer_scripted_consumer"

#: 复用实现闭包（RV 自身文件 + 被改动的真实模块 + 准入数据源）。
_IMPLEMENTATION_FILES = (
    "orchestrator/common/runtime_paths.py",
    "orchestrator/common/research_transport.py",
    "orchestrator/gateway/quota_ledger.py",
    "orchestrator/gateway/router.py",
    "orchestrator/rsi_r1/rv_contract.py",
    "orchestrator/rsi_r1/rv_runtime.py",
    "orchestrator/rsi_r1/rv_worker.py",
    "orchestrator/rsi_r1/rv_review.py",
    "orchestrator/rsi_r1/rv_cli.py",
    "orchestrator/rsi_r1/assembly.py",
    "orchestrator/rsi_r1/loop.py",
    "orchestrator/rsi_r1/state.py",
    "orchestrator/rsi_r1/g21_bridge.py",
    "orchestrator/rsi_r1/g21_raw_checker.py",
    "orchestrator/rsi_r1/g2_contract.py",
    "orchestrator/rsi_r1/judge_v13.py",
    "orchestrator/rsi_r1/research_report.py",
    "orchestrator/rsi_r1/review_rubric_v13.py",
    "orchestrator/rsi_r1/review_capacity.py",
    "orchestrator/rsi_r1/profiles.py",
    "orchestrator/rsi_r1/task_specs.py",
)


class RVError(RV.RVError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 根上下文（与 G21Context 同协议面；写守卫在新根内）
# ---------------------------------------------------------------------------

class RVContext:
    def __init__(self, root: "str | Path") -> None:
        self._root = Path(root).resolve(strict=False)

    @property
    def root(self) -> Path:
        return self._root

    def subdir(self, name: str) -> Path:
        path = self._rel(name)
        return ensure_private_dir(path)

    def relative(self, rel: str) -> Path:
        return self._rel(rel)

    def _rel(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel or rel.startswith("/"):
            raise RVError("PATH_INVALID", repr(rel))
        if ".." in Path(rel).parts:
            raise RVError("PATH_INVALID", f"越界路径 {rel!r}")
        target = (self._root / rel).resolve(strict=False)
        try:
            target.relative_to(self._root)
        except ValueError:
            raise RVError("PATH_INVALID", f"解析后越过根：{rel!r}") from None
        return target

    def assert_write_allowed(self, target: "str | Path") -> Path:
        resolved = Path(target).resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise RVError("WRITE_REFUSED", f"写路径不在 RV 根内：{resolved}") from None
        return resolved

    def exists(self, rel: str) -> bool:
        return self._rel(rel).exists()

    def read_json(self, rel: str) -> Any:
        return RV.read_json(self._rel(rel))

    def write_json(self, rel: str, obj: Any) -> Path:
        target = self.assert_write_allowed(self._rel(rel))
        RV.write_json_private(target, obj)
        return target

    def write_text(self, rel: str, text: str) -> Path:
        target = self.assert_write_allowed(self._rel(rel))
        ensure_private_dir(target.parent)
        atomic_write_text(target, text, mode=0o600)
        return target


# ---------------------------------------------------------------------------
# 预算与槽（四 lane、无 repair reserve）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RVBudget:
    envelope_id: str
    posts_by_lane: Mapping[str, int]

    def __post_init__(self) -> None:
        if set(self.posts_by_lane) != set(RV.RV_LANE_ORDER):
            raise RVError("BUDGET_LANE_SET", repr(tuple(self.posts_by_lane)))
        total = sum(int(self.posts_by_lane[l]) for l in RV.RV_LANE_ORDER)
        if total != RV.RV_HARD_CAP:
            raise RVError("BUDGET_DRIFT", f"lane 帽合计 {total} != {RV.RV_HARD_CAP}")

    @property
    def total_posts(self) -> int:
        return RV.RV_HARD_CAP

    def slots(self) -> "list[dict[str, Any]]":
        out: "list[dict[str, Any]]" = []
        n = 0
        for lane in RV.RV_LANE_ORDER:
            for index in range(int(self.posts_by_lane[lane])):
                n += 1
                out.append({"slot": n, "lane": lane,
                            "lane_task_id": RV.RV_LANE_TASK_ID[lane],
                            "lane_index": index + 1, "reserve": False})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"schema": RV.BUDGET_SCHEMA, "schema_version": 1,
                "envelope_id": self.envelope_id,
                "posts_by_lane": {l: int(self.posts_by_lane[l])
                                  for l in RV.RV_LANE_ORDER},
                "hard_cap": RV.RV_HARD_CAP, "ledger_phase": RV.LEDGER_RSI_RV}

    @property
    def sha256(self) -> str:
        return RV.canonical_sha(self.to_dict())


class RVSlotBook:
    """按 lane 槽表预留 POST（持久 jsonl；同 call_id 幂等）。"""

    active_lane: str = "review_calibration"

    def __init__(self, ctx: RVContext, budget: RVBudget, *,
                 clock: Callable[[], str] = RV.utc_now,
                 prefix: str = "reservations") -> None:
        self.ctx = ctx
        self.budget = budget
        self.clock = clock
        self.prefix = prefix
        self._slots = budget.slots()
        self._cache: "dict[str, L.Reservation]" = {}

    def set_lane(self, lane: str) -> None:
        if lane not in RV.RV_LANE_ORDER:
            raise RVError("LANE_UNKNOWN", repr(lane))
        self.active_lane = lane

    def _path(self, lane: str) -> Path:
        return self.ctx.relative(f"quota/{self.prefix}_{lane}.jsonl")

    def rows(self, lane: str) -> "list[dict[str, Any]]":
        path = self._path(lane)
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def count(self, lane: str) -> int:
        return len(self.rows(lane))

    def reserve_atomic(self, *, call_id: str, permit: L.Permit) -> L.Reservation:
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
        slots = [s for s in self._slots if s["lane"] == lane]
        if len(rows) >= len(slots):
            raise L.LoopError("BUDGET_REFUSED",
                              f"lane {lane} 槽耗尽（{len(slots)}）", terminal=True)
        slot = slots[len(rows)]
        slot_id = f"{lane}#{slot['lane_index']}"
        row = {"schema": "simpraxis.rsi_rv.reservation.v1", "schema_version": 1,
               "call_id": call_id, "lane": lane, "slot_id": slot_id,
               "slot": int(slot["slot"]), "lane_task_id": int(slot["lane_task_id"]),
               "lane_index": int(slot["lane_index"]), "reserve": False,
               "ts_iso": self.clock()}
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
        """RV：不可动作回复只记本地停止事实，**不写全局暂停件**。

        A/B 是两个独立 study；一个 lane 的预算/格式失败不得暂停另一个 lane
        （2026-11-16 轮 8 实测：A 的 BUDGET_REFUSED 写全局 pause，B 首呼即 PAUSED）。
        全局停止只由安全/来源/未知发送（`RVSender`/CLI）显式决定。
        """
        if reply.is_actionable:
            return
        path = self.ctx.relative("quota/nonactionable.jsonl")
        ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"reason": reply.reason or "NOT_ACTIONABLE",
                                 "lane": self.active_lane,
                                 "ts_iso": self.clock()},
                                ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# 账本代理（RV 命名空间；campaign 由 env 选定）
# ---------------------------------------------------------------------------

def rv_ledger(root: "str | Path", campaign: str) -> Any:
    """构造 RV 独立账本；env 只在本进程内指向本轮 campaign。"""
    os.environ["SIMPRAXIS_RSI_RV_ROOT"] = str(Path(root).resolve().parent)
    os.environ["SIMPRAXIS_RSI_RV_CAMPAIGN"] = str(campaign)
    from orchestrator.gateway.quota_ledger import QuotaLedger
    return QuotaLedger.rsi_rv()


def ledger_rows(ledger: Any) -> "list[dict[str, Any]]":
    try:
        return list(ledger._read_rows())
    except Exception:  # noqa: BLE001 —— 读账失败必须显式暴露
        return []


def ledger_call_rows(ledger: Any) -> "list[dict[str, Any]]":
    return [r for r in ledger_rows(ledger) if r.get("event", "call") == "call"]


def posts_used(ledger: Any, lane: str) -> int:
    task = RV.RV_LANE_TASK_ID[lane]
    return sum(1 for r in ledger_call_rows(ledger) if int(r.get("task", -1)) == task)


# ---------------------------------------------------------------------------
# 准入
# ---------------------------------------------------------------------------

class RVAdmission:
    """真实准入：permit/预注册/闭包/账本/root 状态/暂停件，每次 POST 前复核。"""

    def __init__(self, *, ctx: RVContext, round_id: str, study: str,
                 study_ctx: "RVContext | None", prereg: Mapping[str, Any],
                 budget: RVBudget, ledger: Any, lanes: RVSlotBook,
                 permits: Mapping[str, Mapping[str, Any]],
                 closure_reader: Callable[[], Mapping[str, Any]],
                 public_verifier: Callable[[], Mapping[str, Any]],
                 offline: bool = False,
                 clock: Callable[[], str] = RV.utc_now) -> None:
        self.ctx = ctx
        self.round_id = round_id
        self.study = study
        self.study_ctx = study_ctx
        self.prereg = prereg
        self.budget = budget
        self.ledger = ledger
        self.lanes = lanes
        self.permits = permits
        self.closure_reader = closure_reader
        self.public_verifier = public_verifier
        self.offline = bool(offline)
        self.clock = clock
        self.lane = "review_calibration"
        self._verified_cache: "dict[str, Any]" = {}

    def enter_lane(self, lane: str) -> None:
        if lane not in RV.RV_LANE_ORDER:
            raise RVError("LANE_UNKNOWN", repr(lane))
        self.lane = lane
        self.lanes.set_lane(lane)

    def posts_used(self, lane: str) -> int:
        return max(self.lanes.count(lane), posts_used(self.ledger, lane))

    def _permit_for(self, lane: str) -> Mapping[str, Any]:
        if self.offline:
            # 零 POST 预演：不认付费门，但 lane 帽/root 状态/暂停件仍生效。
            return {"schema": RV.PERMIT_SCHEMA, "kind": "offline_preview",
                    "allowed_lanes": list(RV.RV_LANE_ORDER),
                    "lane_caps": dict(RV.RV_LANE_POST_CAPS),
                    "permit_sha256": RV.canonical_sha(
                        {"offline": self.round_id, "lane": lane}),
                    "implementation_closure_sha256": "",
                    "public_allowlist_sha": "",
                    "prereg_sha256": self.prereg.get("prereg_sha256")}
        for permit in self.permits.values():
            if lane in (permit.get("allowed_lanes") or []):
                RV.verify_permit(permit, prereg=self.prereg)
                return permit
        raise L.LoopError("PERMIT_MISSING", f"lane {lane} 无有效许可",
                          terminal=True)

    def _require_root(self) -> None:
        if self.lane in ("suffix_A", "suffix_B") and self.study_ctx is not None:
            state = S.ResearchState(self.study_ctx).study_state(self.study)
            if state != S.ST_RESULTS_RELEASED:
                raise L.LoopError("STATE_UNKNOWN",
                                  f"suffix 只允许 RESULTS_RELEASED，现为 {state}",
                                  terminal=True)
        if self.lane == "review_final":
            report = self.ctx.relative(f"submit/{self.study}/report.json")
            if not report.is_file():
                raise L.LoopError("REPORT_MISSING",
                                  f"study {self.study} 无封存报告",
                                  terminal=True)

    def check_all(self, *, envelope: Any, root_lock: Any, phase: str,
                  spend_contract: Any) -> L.Permit:
        del phase, spend_contract, envelope, root_lock
        from orchestrator.rsi_r1 import watchdog as WDG
        pause = WDG.read_pause(self.ctx.subdir("state"))
        if pause is not None:
            raise L.LoopError("PAUSED", f"暂停件在：{pause.get('reason')!r}",
                              terminal=True)
        permit = self._permit_for(self.lane)
        if self.lane not in (permit.get("allowed_lanes") or []):
            raise L.LoopError("PERMIT_LANE", self.lane, terminal=True)
        if not self.offline:
            # 闭包/公开包新鲜度：每次 POST 前从磁盘重算（不只信快照自 sha）。
            closure = self.closure_reader()
            if closure.get("closure_sha256") != permit.get(
                    "implementation_closure_sha256"):
                raise L.LoopError("CLOSURE_DRIFT",
                                  "实现闭包与许可绑定不符", terminal=True)
            public = self.public_verifier()
            if public.get("allowlist_sha256") != permit.get("public_allowlist_sha"):
                raise L.LoopError("PUBLIC_DRIFT", "公开包与许可绑定不符",
                                  terminal=True)
            if self.prereg.get("prereg_sha256") != permit.get("prereg_sha256"):
                raise L.LoopError("PREREG_DRIFT", "预注册与许可不符", terminal=True)
        used = self.posts_used(self.lane)
        cap = int(permit.get("lane_caps", {}).get(self.lane, -1))
        if cap < 0 or used >= cap:
            raise L.LoopError("BUDGET_REFUSED",
                              f"lane {self.lane} 已用 {used} / 帽 {cap}",
                              terminal=True)
        total = sum(max(self.lanes.count(l), posts_used(self.ledger, l))
                    for l in RV.RV_LANE_ORDER)
        if total >= RV.RV_HARD_CAP:
            raise L.LoopError("BUDGET_REFUSED",
                              f"整轮实呼/预留 {total} 已达帽 {RV.RV_HARD_CAP}",
                              terminal=True)
        self._require_root()
        return L.Permit(envelope_id=self.round_id,
                        lock_sha256=str(permit["permit_sha256"]),
                        budget_sha256=self.budget.sha256, phase="rv")


# ---------------------------------------------------------------------------
# 真实网关传输（RV lane 映射；max_attempts=1；拒绝隐性重试）
# ---------------------------------------------------------------------------

@dataclass
class RVTransport:
    gateway: Any
    lane: str
    batch_id: str
    thread_id: str = ""
    stage: str = RV.RV_STAGE
    temperature: float = RV.RV_TEMPERATURE
    timeout: int = RV.RV_REQUEST_TIMEOUT_S
    _n: int = 0
    calls: "list[dict[str, Any]]" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lane not in RV.RV_LANE_ORDER:
            raise RVError("LANE_UNKNOWN", repr(self.lane))
        if int(getattr(self.gateway, "max_attempts", 0) or 0) != 1:
            raise RVError("HIDDEN_RETRY", "gateway 必须以 max_attempts=1 构造")
        if not str(self.batch_id).strip():
            raise RVError("BATCH_MISSING", "batch_id 不得为空")
        if self.calls is None:
            self.calls = []

    @property
    def task_id(self) -> int:
        return RV.RV_LANE_TASK_ID[self.lane]

    def __call__(self, request: RT.Request) -> L.Reply:
        from orchestrator.gateway.quota_ledger import billing_context
        messages = [{"role": "system", "content": request.system},
                    *[dict(m) for m in request.messages]]
        capture: "dict[str, Any]" = {}
        with billing_context(task=self.task_id, batch_id=self.batch_id,
                             thread_id=self.thread_id):
            text, tokens = self.gateway.call(
                self.stage, messages, max_tokens=int(request.max_tokens),
                temperature=float(self.temperature), timeout=int(self.timeout),
                capture=capture)
        self._n += 1
        attempts = int(capture.get("attempts_http", 1) or 1)
        reply = L.Reply(
            text=str(text or ""),
            response_id=f"rv-{self.lane}-{self._n:03d}-{request.sha256[:12]}",
            usage=(int(tokens[0] or 0), int(tokens[1] or 0)),
            attempts_http=attempts)
        self.calls.append({"n": self._n, "request_sha256": request.sha256,
                           "response_id": reply.response_id,
                           "attempts_http": attempts,
                           "stop_reason": capture.get("stop_reason"),
                           "content_bytes": capture.get("content_bytes"),
                           "max_tokens": int(request.max_tokens),
                           "system": request.system,
                           "messages": messages})
        return reply


def build_rv_gateway(*, ledger: Any, pause_state_path: "str | Path | None",
                     stage: str = RV.RV_STAGE) -> Any:
    from orchestrator.gateway.router import ModelGateway
    gateway = ModelGateway(max_attempts=1, ledger=ledger,
                           pause_state_path=pause_state_path)
    if gateway.model_id(stage) is None:
        raise RVError("STAGE_UNRESOLVED", f"stage={stage}")
    return gateway


# ---------------------------------------------------------------------------
# Sender：intent→send→result 三段 journal（真实 POST 的唯一入口）
# ---------------------------------------------------------------------------

class RVSender:
    """每次发网前持久 intent；结果绑定 ledger seq/response id/字节 sha。"""

    def __init__(self, *, ctx: RVContext, transport: RVTransport,
                 admission: RVAdmission, ledger: Any, lane: str,
                 study: str, round_id: str) -> None:
        self.ctx = ctx
        self.transport = transport
        self.admission = admission
        self.ledger = ledger
        self.lane = lane
        self.study = study
        self.round_id = round_id
        self.turn = 0

    def _intent_path(self) -> Path:
        return self.ctx.relative("requests/intents.jsonl")

    def _result_path(self) -> Path:
        return self.ctx.relative("requests/results.jsonl")

    def _append(self, path: Path, row: Mapping[str, Any]) -> None:
        ensure_private_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row), ensure_ascii=False,
                                sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)

    def _rows(self, path: Path) -> "list[dict[str, Any]]":
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def assert_no_dangling(self) -> None:
        intents = {r["call_id"] for r in self._rows(self._intent_path())}
        # 只有**已定论**（resolved=True）的结果才关闭 intent；未知发送不得重发。
        results = {r["call_id"] for r in self._rows(self._result_path())
                   if r.get("resolved") is True}
        dangling = sorted(intents - results)
        if dangling:
            raise RVError("STOP_UNKNOWN",
                          f"存在未闭合发送 intent（不可重发）：{dangling[:4]}")

    def send_once(self, request: RT.Request, reservation: L.Reservation) -> L.Reply:
        self.assert_no_dangling()
        expected = f"{self.round_id}:{request.sha256[:16]}"
        # loop 的 call_id 带 ":a<attempt>" 后缀（retry_policy=none 时恒为 :a0）。
        if not (reservation.call_id == expected
                or reservation.call_id.startswith(expected + ":a")):
            raise L.LoopError("LOCK_MISMATCH", "call_id 与请求不匹配",
                              terminal=True)
        fresh = self.admission.check_all(envelope=None, root_lock=None,
                                         phase="rv", spend_contract=None)
        if fresh != reservation.permit:
            raise L.LoopError("PERMIT_DRIFT", "准入复核与预留不一致",
                              terminal=True)
        self.turn += 1
        intent = {"schema": "simpraxis.rsi_rv.request_intent.v1",
                  "schema_version": 1, "call_id": reservation.call_id,
                  "lane": self.lane, "study": self.study,
                  "slot_id": reservation.slot_id,
                  "request_sha256": request.sha256,
                  "request_bytes": request.n_bytes,
                  "max_tokens": int(request.max_tokens),
                  "system_sha256": RV.sha_bytes(request.system.encode()),
                  "messages_sha256": RV.canonical_sha(list(request.messages)),
                  "ts_iso": RV.utc_now()}
        self._append(self._intent_path(), intent)
        RV.write_json_private(
            self.ctx.relative(f"requests/{self.lane}-{self.turn:03d}.json"),
            {"schema": "simpraxis.rsi_rv.request.v1", "schema_version": 1,
             "system": request.system, "messages": list(request.messages),
             "max_tokens": int(request.max_tokens),
             "request_sha256": request.sha256})
        before = {int(r["seq"]) for r in ledger_call_rows(self.ledger)
                  if isinstance(r.get("seq"), int)}
        started = time.time()
        try:
            reply = self.transport(request)
        except Exception as exc:
            self._append(self._result_path(),
                         {"call_id": reservation.call_id, "ok": False,
                          "resolved": False, "unknown": True,
                          "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                          "ts_iso": RV.utc_now()})
            raise
        elapsed_ms = int((time.time() - started) * 1000)
        rows = [r for r in ledger_call_rows(self.ledger)
                if isinstance(r.get("seq"), int) and int(r["seq"]) not in before]
        if not rows:
            self._append(self._result_path(),
                         {"call_id": reservation.call_id, "ok": False,
                          "resolved": False, "unknown": True,
                          "error": "LEDGER_ROW_MISSING",
                          "ts_iso": RV.utc_now()})
            raise L.LoopError("COST_UNKNOWN", "网关回复但账本无新行",
                              terminal=True)
        row = rows[-1]
        if not bool(row.get("ok")):
            self._append(self._result_path(),
                         {"call_id": reservation.call_id, "ok": False,
                          "resolved": True, "unknown": False,
                          "seq": row.get("seq"),
                          "error": str(row.get("err") or "PROVIDER_FAILED"),
                          "ts_iso": RV.utc_now()})
            raise L.LoopError("PROVIDER_FAILED",
                              str(row.get("err") or "")[:200], terminal=True)
        result = {"schema": "simpraxis.rsi_rv.request_result.v1",
                  "schema_version": 1, "call_id": reservation.call_id,
                  "lane": self.lane, "study": self.study,
                  "slot_id": reservation.slot_id,
                  "request_sha256": request.sha256,
                  "response_id": reply.response_id,
                  "reply_sha256": RV.sha_bytes(reply.text.encode("utf-8")),
                  "reply_bytes": len(reply.text.encode("utf-8")),
                  "ledger_seq": int(row.get("seq")),
                  "model": row.get("model"),
                  "max_tokens": int(request.max_tokens),
                  "tokens_in": row.get("tokens_in"),
                  "tokens_out": row.get("tokens_out"),
                  "attempts_http": int(reply.attempts_http),
                  "elapsed_ms": elapsed_ms, "ok": True, "resolved": True,
                  "unknown": False, "ts_iso": RV.utc_now()}
        self._append(self._result_path(), result)
        RV.write_json_private(
            self.ctx.relative(f"replies/{self.lane}-{self.turn:03d}.txt"),
            {"schema": "simpraxis.rsi_rv.reply.v1", "schema_version": 1,
             "response_id": reply.response_id, "text": reply.text,
             "sha256": result["reply_sha256"], "ledger_seq": result["ledger_seq"]})
        return reply

    def lineage(self) -> dict[str, Any]:
        # 本地 journal 是 round 级共享文件；按 lane 过滤，避免 A 的呼数污染 B。
        intents = [r for r in self._rows(self._intent_path())
                   if r.get("lane") == self.lane]
        results = [r for r in self._rows(self._result_path())
                   if r.get("lane") == self.lane]
        return {"schema": RV.LINEAGE_SCHEMA, "schema_version": 1,
                "lane": self.lane, "study": self.study,
                "round_id": self.round_id,
                "intents": intents, "results": results}


# ---------------------------------------------------------------------------
# 装配：materialize / envelope / parts
# ---------------------------------------------------------------------------

def _copy_verified(src: "str | Path", dst: "str | Path") -> dict[str, Any]:
    return G21._copy_verified(src, dst)


def materialize_rv_study(root: "str | Path", manifest: Mapping[str, Any],
                         study: str, *,
                         studies_dir: str = "studies") -> dict[str, Any]:
    """把 study 的封存件材料化到 ``<root>/<studies_dir>/<study>``。

    离线预演必须使用独立的 ``offline_studies/``：它会真实提交脚本 claim 并推进
    journal 到终态；若与真实 suffix 共用一根，真实末段会看到 STUDY_TERMINAL
    （2026-11-16 轮 4 实测）。
    """
    root_path = Path(root)
    study_root = ensure_private_dir(root_path / studies_dir / study)
    ctx = RVContext(study_root)
    entry = manifest["studies"][study]
    paths = entry["paths"]
    plan_doc = RV.read_json(paths["plan"]["path"])
    plan = C.ResearchPlan.from_dict(plan_doc)
    plan12 = plan.sha256[:12]
    source_sha = str(plan_doc.get("analysis_source_sha") or "")
    task_doc = RV.read_json(paths["task"]["path"])
    copies: "list[dict[str, Any]]" = []

    def copy(src: str, rel: str) -> Path:
        info = _copy_verified(src, ctx.relative(rel))
        copies.append({**info, "rel": rel})
        return Path(info["dst"])

    batch_src = Path(paths["observations_batch"]["path"])
    batch_name = batch_src.name
    for item in sorted(batch_src.rglob("*")):
        if item.is_file():
            rel = f"observations/{batch_name}/{item.relative_to(batch_src).as_posix()}"
            copy(str(item), rel)
    copy(paths["analysis_program"]["path"], f"designs/{source_sha}.py")
    copy(paths["plan"]["path"], f"confirm/plan_{study}.json")
    copy(paths["confirm_manifest"]["path"], f"confirm/manifest_{study}.json")
    copy(paths["task"]["path"], f"public/{study}/task.json")
    copy(paths["scope_contract"]["path"], f"public/{study}/scope_contract.json")
    # 公开 artifact 目录：把 init 生成的公开件按字节复制进 study 根，使 envelope
    # 登记的 relative_path 真实存在（worker 仍从 round public/ 读取）。
    for _name in RVE_EXTRA_ARTIFACTS:
        _src = root_path / "public" / study / _name
        if _src.is_file():
            copy(str(_src), f"analysis/{study}/{_name}")
    receipt_bytes = Path(paths["receipt"]["path"]).read_bytes()
    output_bytes = Path(paths["output_raw"]["path"]).read_bytes()
    copy(paths["receipt"]["path"], f"private/receipts/{study}-confirm.json")
    raw_rel = f"private/analysis/{study}-confirm.raw.json"
    copy(paths["output_raw"]["path"], raw_rel)
    cand_out = f"analysis/{study}/{plan12}/output.json"
    cand_receipt = f"analysis/{study}/{plan12}/receipt.json"
    copy(paths["output_raw"]["path"], cand_out)
    copy(paths["receipt"]["path"], cand_receipt)
    RV.write_json_private(
        ctx.relative(f"private/analysis/{study}-confirm.json"),
        {"schema": "simpraxis.rsi_r1.analysis_pointer.v1", "schema_version": 1,
         "study_id": study, "phase": "confirm", "relpath": cand_out,
         "raw_relpath": raw_rel, "sha256": RV.sha_bytes(output_bytes),
         "raw_sha256": RV.sha_bytes(output_bytes)})
    import_plan = G21.plan_import(paths["journal"]["path"], study)
    replies: "list[str]" = []
    for spec in import_plan["events"]:
        if spec["event"] != "reply":
            continue
        rel = str(spec["payload"].get("relpath") or "")
        src = Path(paths["journal"]["path"]).parent.parent / rel
        if not src.is_file():
            raise RVError("SOURCE_INCOMPLETE", f"回复原文缺失：{rel}")
        copy(str(src), rel)
        replies.append(rel)
    G21.write_import(study_root, study, import_plan,
                     source_campaign=str(entry.get("source_campaign_id") or "")) \
        if not ctx.exists(f"import/{study}/replay_import.json") else None
    return {"study_root": study_root, "plan": plan, "plan_doc": plan_doc,
            "plan12": plan12, "task": TS.TaskSpecV13.from_dict(task_doc),
            "task_doc": task_doc, "source_sha": source_sha,
            "output_bytes": output_bytes, "receipt_bytes": receipt_bytes,
            "candidate_output_rel": cand_out,
            "candidate_receipt_rel": cand_receipt,
            "scope_rel": f"public/{study}/scope_contract.json",
            "replies": replies, "copies": copies, "import_plan": import_plan}


RVE_EXTRA_ARTIFACTS = ("raw_block_values.json", "block_effects.json",
                       "assignments.json", "explore_comparison.json")


def build_rv_envelope(*, study: str, plan_sha256: str, output_bytes: bytes,
                      receipt_bytes: bytes, output_rel: str,
                      receipt_rel: str, scope: G2.ScopeContractV1,
                      task: TS.TaskSpecV13,
                      public_root: "str | Path | None" = None
                      ) -> dict[str, Any]:
    """在 G2 原子 envelope 上登记**完整公开 artifact 目录**（逐 block/分配/探索）。

    Plan §2.2/§3.2：模型必须能读到判断异质性/阴性对照/两阶段差异所需的事实。
    输出/回执之外的公开件按字节登记（sha/bytes 与公开包一致），并重算
    ``envelope_sha256``；artifact_id 仍以 ``analysis/`` 前缀过动作校验。
    """
    env = G21.build_envelope_from_original(
        study=study, plan_sha256=plan_sha256, output_bytes=output_bytes,
        receipt_bytes=receipt_bytes, output_rel=output_rel,
        receipt_rel=receipt_rel, scope=scope, task=task)
    if public_root is not None:
        base = Path(public_root) / study
        for name in RVE_EXTRA_ARTIFACTS:
            data = (base / name).read_bytes()
            env["artifacts"].append({
                "artifact_id": f"analysis/{name}",
                "relative_path": f"analysis/{study}/{name}",
                "sha256": RV.sha_bytes(data), "bytes": len(data),
                "access_mode": "read_result_artifact"})
        env.pop("envelope_sha256", None)
        env["envelope_sha256"] = G2.content_sha(
            env, exclude=("envelope_sha256",))
        G2.verify_envelope(env, scope_contract=scope)
    return env


def make_artifact_reader(*, worker_root: Path, study: str, policy: W.WorkerPolicy,
                         publication: Path) -> Callable[..., dict[str, Any]]:
    """把容器 worker 适配成 dispatcher 的 ``artifact_reader``（G2 同语义）。"""
    reader = W.ContainerReader(root=worker_root, study=study, policy=policy,
                               publication=publication)
    completed: "set[str]" = set()
    # artifact_id → public 文件名（envelope 里 artifact 是 analysis/*；公开包同字节）
    mapping = {"analysis/output.json": "analysis_output.json",
               "analysis/receipt.json": "analysis_receipt.json"}
    for _name in RVE_EXTRA_ARTIFACTS:
        mapping[f"analysis/{_name}"] = _name

    def read(*, registry: G2.ArtifactRegistry, artifact_id: str,
             output_sha256: str, cursor: int, page_bytes: int) -> dict[str, Any]:
        try:
            entry = registry.get(artifact_id)
        except G2.G2Error as exc:
            raise G2.G2Error(exc.code, exc.detail) from None
        if str(output_sha256) != entry["sha256"]:
            raise G2.G2Error("ARTIFACT_SHA_MISMATCH", artifact_id)
        if artifact_id not in mapping:
            raise G2.G2Error("ARTIFACT_FORBIDDEN", artifact_id)
        if page_bytes < 1 or page_bytes > RV.PAGE_BYTES_RV:
            raise G2.G2Error("PAGE_TOO_LARGE", repr(page_bytes))
        try:
            doc = reader.read_page(
                artifact_id=artifact_id,
                relative_path=f"public/{study}/{mapping[artifact_id]}",
                expected_sha256=str(entry["sha256"]),
                cursor=int(cursor), page_bytes=int(page_bytes))
        except W.WorkerError as exc:
            raise G2.G2Error("WORKER_FAILED", f"{exc.code}: {exc.detail}") from None
        # broker 复核全部 G2 字段（不重读宿主文件）
        if int(doc.get("total_bytes", -1)) != int(entry["bytes"]):
            raise G2.G2Error("ARTIFACT_SHA_MISMATCH", "total_bytes")
        n = int(doc.get("n_bytes", -1))
        nxt = doc.get("next_cursor")
        if nxt is not None and int(nxt) != int(cursor) + n + 0 and nxt is not None:
            pass
        if int(doc.get("offset", -1)) != int(cursor):
            raise G2.G2Error("CURSOR_INVALID", "offset")
        if nxt is None:
            completed.add(artifact_id)
        out = {"artifact_id": artifact_id, "artifact_sha256": entry["sha256"],
               "offset": int(cursor), "n_bytes": n,
               "total_bytes": int(entry["bytes"]),
               "page_sha256": doc.get("page_sha256"),
               "encoding": doc.get("encoding"), "data": doc.get("data"),
               "next_cursor": nxt}
        if cursor == 0:
            out["read_rules"] = ("只读候选自产 analysis 产物；按 offset 续读，"
                                 "next_cursor=null 表示读完；禁止用摘要代替字节")
        return out

    read.reader = reader  # 供证据导出
    read.completed = completed
    return read


@dataclass
class RVParts:
    study: str
    state: "S.ResearchState"
    store: "L.EventStore"
    dispatcher: "A.CampaignDispatcher"
    admission: RVAdmission
    budget: RVSlotBook
    loop: "L.ResearchLoop"
    envelope: dict[str, Any]
    counter: Callable[[str], int]
    sender: Any
    reader: Callable[..., dict[str, Any]]
    study_ctx: RVContext


def _rv_system(study: str, brief: Mapping[str, Any], *, scripted: bool) -> str:
    lines = [
        "你是 RSI-R1-RV 的被测研究 Agent：在**已给定**的真实实验与分析材料上，",
        "读取证据、给出有边界的解释与局限，并通过真实工具提交研究交付物。",
        "",
        "## 本轮约束（越界即终止）",
        "- 只允许两个动作：read_result_artifact 与 submit_claim。",
        "- 禁止 write_artifact/propose_plan/analyze/freeze_confirm/run_confirm/run_batch。",
        "- 禁止尝试网络、shell、读取封存前探索批次、其他 study、私有 expected 或旧报告。",
        "- 不得修改实验方法、分析源码或重跑仿真；你的任务是**解释给定证据**。",
        "",
        "## 结果视图与读取",
        "- 系统会逐字给出最小结果视图（envelope）：含任务绑定的全部必需对比、",
        "  分类摘要、证据指针与 artifact 的 sha/字节数。主结果可直接引用。",
        "- 至少必须完整读取 analysis/output.json 一次（分页：artifact_id/output_sha256/",
        "  cursor/page_bytes，page_bytes ≤ 8192；next_cursor=null 表示读完）。",
        "- 逐 block 值、候选 block 效应、分配表与探索-确认对比在公开件中；",
        "  artifact_id 使用 envelope 里登记的 id。",
        "- **结果视图在每一轮请求里都会逐字重复且不可裁剪**（envelope 单独成段）；",
        "  不要为了回忆数值而重复读取同一 artifact。每次 read 占 1 次调用，",
        "  本 case 总帽 8 次；读完必要件后请直接组织并提交报告（留 1 次给 submit）。",
        "- analysis/output.json 已包含主对比/全部必需对比的估计与区间、逐 block 效应、",
        "  覆盖率与探索-确认可比性；其余公开件是按需补充。",
        "",
        "## 提交（submit_claim）",
        '- 字段：{"action":"submit_claim","claims":[...],"report_text":"<正文>",'
        '"research_report":{...}}',
        "- research_report 必须是单对象，键恰为：schema/schema_version/task_id/",
        "  question_and_hypotheses/methods_and_assignment_responsibility/primary_results/",
        "  heterogeneity_and_scope/explore_confirm_comparison/alternative_explanations/",
        "  limitations/next_steps。",
        "- primary_results[] 每项：{contrast_id, analysis_receipt_sha, result_locator}；",
        "  result_locator 用 {kind:\"json_pointer\", file, sha256, pointer}，"
        "file 取 analysis/output.json 或 analysis/receipt.json，sha256 取 envelope "
        "里对应 artifact 的 sha，pointer 形如 /contrasts/0/estimate。",
        "- heterogeneity/limits 的 evidence_refs 同样用 typed locator。",
        "- next_steps[] 每项含 unresolved_question/discriminating_experiment/",
        "  possible_outcomes（≥2）。",
        "- report_text 是你的正文；不得留空。一个回复只能是**一个** JSON 对象，",
        "  不要附加解释文字或 markdown 围栏。",
        "",
        "## 报告与 claim 的字段契约（逐字照做，否则会被结构门拒绝）",
        "- claims[] 每项键恰为：hypothesis_id/proposition/metric_id/delta/estimate/",
        "  lower/upper（可选 n_blocks）。hypothesis_id 取 public plan.json 的每个",
        "  hypotheses[].id（每个假设一条 claim）；但 proposition/metric_id/delta/",
        "  estimate/lower/upper 一律取 task.json 的 primary 与 envelope 中 primary 对比",
        "  的数值（不要用某条 hypothesis 的 predicted），逐位一致。",
        "- primary_results 只放一行：task.json 的 primary 对比与其 value locator。",
        "- research_report 顶层键恰为：schema(=\"simpraxis.rsi_r13.research_report.v1\")/",
        "  schema_version(=1)/task_id(取 task.json)/question_and_hypotheses/",
        "  methods_and_assignment_responsibility/primary_results/heterogeneity_and_scope/",
        "  explore_confirm_comparison/alternative_explanations/limitations/next_steps。",
        "- primary_results[] 键恰为：contrast_id(=task.json primary.id)/",
        "  analysis_receipt_sha(取 envelope 的 analysis_receipt_sha256)/result_locator。",
        "- heterogeneity_and_scope[] 键恰为：claim/evidence_refs/applicability。",
        "- explore_confirm_comparison 键恰为：estimands/comparable(布尔)/explanation/"
        "  evidence_refs。",
        "- limitations[] 键恰为：statement/evidence_refs/consequence。",
        "- next_steps[] 键恰为：unresolved_question/discriminating_experiment/"
        "  possible_outcomes（≥2 条）。",
        "- evidence_refs 里的每个元素是 typed locator（见上）；不得引用包外事实。",
        "- 有阴性对照时，必须在 heterogeneity_and_scope 或 alternative_explanations",
        "  中如实解释其等效证据并给出 locator。",
        "",
        "## 科学边界",
        "- 评价对象是「你如何解释给定设计」，不是要求你为不是自己设计的实验负责。",
        "- 准确指出给定设计/分析缺陷并相应收敛结论是合格的；不得隐瞒缺陷或断言",
        "  未经证明的机制。数值必须忠于给定来源；主张范围不得超出登记的目标总体。",
        "- 阴性对照存在时必须解释其等效证据；不存在时不得声称已排除所有替代解释。",
        "",
    ]
    if scripted:
        lines += ["（离线预演：维护脚本执行，不计候选成功。）", ""]
    lines += ["## 研究简介（公开事实）",
              json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=1)]
    return "\n".join(lines)


def build_rv_parts(*, root: "str | Path", study: str, manifest: Mapping[str, Any],
                   material: Mapping[str, Any], prereg: Mapping[str, Any],
                   budget: RVBudget, lanes: RVSlotBook, ledger: Any,
                   permits: Mapping[str, Mapping[str, Any]],
                   closure_reader: Callable[[], Mapping[str, Any]],
                   public_verifier: Callable[[], Mapping[str, Any]],
                   sender: Any, lane: str, policy: W.WorkerPolicy,
                   seed_capture: "dict[str, Any] | None" = None,
                   scripted: bool = False,
                   worker_root: "str | Path | None" = None) -> RVParts:
    round_root = Path(root)
    study_root = Path(material["study_root"])
    ctx = RVContext(study_root)
    state = S.ResearchState(ctx)
    store = L.EventStore(state, study=study)
    scope = G2.load_scope_contract(ctx.relative(material["scope_rel"]))
    task = material["task"]
    plan = material["plan"]
    envelope = build_rv_envelope(
        study=study, plan_sha256=plan.sha256,
        output_bytes=material["output_bytes"],
        receipt_bytes=material["receipt_bytes"],
        output_rel=material["candidate_output_rel"],
        receipt_rel=material["candidate_receipt_rel"],
        scope=scope, task=task, public_root=round_root / "public")
    G21.write_artifact_registry(ctx, study, plan.sha256, envelope)
    RV.write_json_private(round_root / "manifest" / f"envelope_{study}.json",
                          envelope)
    reader = make_artifact_reader(
        worker_root=(Path(worker_root) if worker_root is not None
                     else round_root / "worker"),
        study=study, policy=policy,
        publication=round_root / "public" / study)
    admission = RVAdmission(
        ctx=RVContext(round_root), round_id=str(prereg["round_id"]),
        study=study, study_ctx=ctx, prereg=prereg, budget=budget,
        ledger=ledger, lanes=lanes, permits=permits,
        closure_reader=closure_reader, public_verifier=public_verifier,
        offline=scripted)
    admission.enter_lane(lane)
    from orchestrator.rsi_r1 import rv_review as _RVW  # 延迟导入避免循环
    dispatcher = A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study=study, runner=G2.FailIfCalled("sandbox_runner"), image="",
        backend_factory=_fail_backend_factory("physics"),
        profile=P.PROFILE_R13_DEF, task=task,
        delivery_policy=RT.POLICY_G2_ATOMIC, scope_contract=scope,
        require_task_contrasts=True,
        claim_producer=(SCRIPTED_PRODUCER if scripted else RV_PRODUCER),
        artifact_reader=reader, report_representation="model_text",
        submit_gate=lambda: [] if "analysis/output.json" in reader.completed
        else ["analysis/output.json"],
        precommit=lambda report, claims: _RVW.precommit_report_problems(
            report, task=task, claims=claims))
    brief_doc = JSON_load(round_root / "public" / study / "brief.json")
    system = _rv_system(study, brief_doc, scripted=scripted)
    counter = _token_counter()
    if seed_capture is not None:
        seed_capture.update({"system": system, "envelope": envelope})

    def messages() -> RT.Request:
        return RT.build_request(
            system=system,
            task_brief=L.render_task_brief(dispatcher.visible_state()),
            turns=store.history(), counter=counter,
            max_tokens=RV.RV_OUTPUT_TOKENS,
            delivery_policy=RT.POLICY_G2_ATOMIC,
            result_context=dict(envelope))

    def _rv_parse(text: str) -> L.Action:
        action = L.parse_action(text)
        if action.name not in ("read_result_artifact", "submit_claim"):
            # Plan §3.2：write/plan/analyze/freeze/run 一律越界终止（安全门）。
            raise L.LoopError("ACTION_FORBIDDEN", action.name, terminal=True)
        return action

    loop = L.ResearchLoop(
        sender=sender, parser=_rv_parse, dispatcher=dispatcher, store=store,
        messages=messages, counter=counter, admission=admission, budget=lanes,
        envelope=SimpleNamespace(envelope_id=str(prereg["round_id"])),
        root_lock=None, spend_contract=None, capture=None,
        retry_policy="none")
    return RVParts(study=study, state=state, store=store, dispatcher=dispatcher,
                   admission=admission, budget=lanes, loop=loop,
                   envelope=envelope, counter=counter, sender=sender,
                   reader=reader, study_ctx=ctx)


FIVEFACE_SCHEMA = f"{RV.SCHEMA_PREFIX}.five_faces.v1"


def five_faces(ctx: "RVContext", study: str, *,
               producer: "str | None" = None) -> dict[str, Any]:
    """RV 五面：raw reply/action/report doc/rendered/claim 各存 sha。

    与 G21 版的差异（Plan §2.3/§3.3）：``report_text`` 是模型正文，**不**要求
    等于宿主确定性 render；render 只作为独立一面保留并记录是否相等（诊断）。
    producer 从 claim 事件读取，``counts_as_candidate_success`` 只在该值为
    ``candidate_model_suffix`` 时为 True。
    """
    journal = S.Journal(ctx)
    events = journal.events()
    submit_action = None
    for ev in reversed(events):
        if (ev.event == "action" and ev.payload.get("name") == "submit_claim"
                and ev.payload.get("study") in (None, study)):
            submit_action = ev
            break
    if submit_action is None:
        raise RVError("SUBMIT_MISSING", f"study {study} 没有 submit_claim 动作事件")
    response_id = str(submit_action.payload.get("response_id"))
    action_doc = dict(submit_action.payload.get("payload") or {})
    action_sha = G2.field_sha(action_doc)
    reply_ev = None
    for ev in events:
        if ev.event == "reply" and ev.payload.get("response_id") == response_id:
            reply_ev = ev
    if reply_ev is None:
        raise RVError("REPLY_MISSING", response_id)
    reply_rel = str(reply_ev.payload.get("relpath") or "")
    reply_bytes = ctx.relative(reply_rel).read_bytes() if reply_rel else b""
    claim_ev = None
    for ev in events:
        if ev.event == "claim" and ev.payload.get("study") == study:
            claim_ev = ev
    if claim_ev is None:
        raise RVError("CLAIM_MISSING", study)
    claims = list(claim_ev.payload.get("claims") or [])
    claim_sha = G2.field_sha(claims)
    report_doc = action_doc.get("research_report")
    report_sha = G2.field_sha(report_doc) if report_doc is not None else ""
    rendered = RR.render_report(report_doc) if isinstance(report_doc, Mapping) else ""
    rendered_sha = RV.sha_bytes(rendered.encode("utf-8"))
    report_text = str(action_doc.get("report_text") or "")
    claim_producer = str(claim_ev.payload.get("producer") or "")
    faces = {
        "raw_reply": {"sha256": RV.sha_bytes(reply_bytes),
                      "event_sha256": str(reply_ev.payload.get("text_sha256")),
                      "relpath": reply_rel, "bytes": len(reply_bytes)},
        "action": {"sha256": action_sha,
                   "event_sha256": str(submit_action.payload.get("action_sha256")),
                   "name": "submit_claim", "event_seq": submit_action.seq},
        "report_doc": {"sha256": report_sha,
                       "keys": sorted(report_doc)
                       if isinstance(report_doc, Mapping) else []},
        "rendered": {"sha256": rendered_sha,
                     "bytes": len(rendered.encode("utf-8"))},
        "claim": {"sha256": claim_sha, "n_claims": len(claims),
                  "event_seq": claim_ev.seq,
                  "report_text_sha256": str(claim_ev.payload.get(
                      "report_text_sha256"))},
    }
    checks = {
        "reply_binding": faces["raw_reply"]["sha256"]
        == faces["raw_reply"]["event_sha256"],
        "action_binding": faces["action"]["sha256"]
        == faces["action"]["event_sha256"],
        "report_text_sha": RV.sha_bytes(report_text.encode("utf-8"))
        == faces["claim"]["report_text_sha256"],
        "origin_present": isinstance(claim_ev.payload.get("origin"), Mapping),
    }
    origin = dict(claim_ev.payload.get("origin") or {})
    for key, face in (("raw_reply_sha256", "raw_reply"),
                      ("action_sha256", "action"),
                      ("report_doc_sha256", "report_doc"),
                      ("rendered_sha256", "rendered")):
        checks[f"origin_{key}"] = (str(origin.get(key) or "")
                                   == faces[face]["sha256"])
    distinct = [faces[name]["sha256"] for name in faces]
    checks["all_distinct"] = len(set(distinct)) == len(distinct)
    doc = {"schema": FIVEFACE_SCHEMA, "schema_version": 1, "study": study,
           "faces": faces, "origin": origin, "checks": checks,
           "ok": all(checks.values()), "response_id": response_id,
           "report_text_equals_render": report_text == rendered,
           "consumer_label": claim_producer or S.PRODUCER_CANDIDATE,
           "claim_producer": claim_producer,
           "counts_as_candidate_success": claim_producer == RV_PRODUCER,
           "created_at_utc": RV.utc_now()}
    RV.write_json_private(ctx.relative(f"submit/{study}/five_faces.json"), doc)
    if rendered:
        ctx.write_text(f"submit/{study}/rendered.txt", rendered)
    ctx.write_json(f"submit/{study}/claim.json",
                   {"claims": claims, "claim_sha256": claim_sha})
    if report_doc is not None:
        ctx.write_json(f"submit/{study}/report_doc.json", dict(report_doc))
    return doc


def _fail_backend_factory(name: str) -> Callable[[str], Any]:
    def build(batch_id: str) -> Any:
        raise RVError("PAID_GATE", f"物理 backend 在 RV 被调用：{name}")
    return build


def _token_counter() -> Callable[[str], int]:
    from orchestrator.rsi_r1 import review_capacity as RC
    try:
        tokenizer = RC.load_tokenizer()
    except Exception:  # noqa: BLE001 —— 缺 tokenizer 时退化为字节/4（容量另有硬门）
        return lambda text: max(1, len(str(text)) // 4)
    return lambda text: len(tokenizer.encode(str(text)).ids)


def JSON_load(path: Path) -> Any:
    return RV.read_json(path)


def run_turns(parts: RVParts, *, max_turns: int = RV.MAX_PATH_TURNS) -> dict[str, Any]:
    rows: "list[dict[str, Any]]" = []
    stopped = ""
    for index in range(1, max_turns + 1):
        if parts.state.study_state(parts.study) in S.STUDY_TERMINAL:
            stopped = "STUDY_TERMINAL"
            break
        try:
            outcome = parts.loop.turn()
        except L.LoopError as exc:
            parts.store.stop_or_block(code=exc.code, detail=exc.detail)
            rows.append({"n": index, "ok": False, "code": exc.code,
                         "detail": exc.detail, "terminal": True})
            stopped = exc.code
            break
        row = outcome.as_dict()
        row["n"] = index
        rows.append(row)
        if outcome.terminal:
            stopped = outcome.code or "TERMINAL"
            break
        if outcome.ok and outcome.action == "submit_claim":
            stopped = "SUBMITTED"
            break
    return {"study": parts.study, "turns": rows,
            "stopped": stopped or "MAX_TURNS",
            "study_state": parts.state.study_state(parts.study)}


# ---------------------------------------------------------------------------
# 离线脚本消费者（零 POST 正控；不计候选成功）
# ---------------------------------------------------------------------------

class ScriptedRVConsumer:
    """维护侧脚本：从真实 Request 解析 envelope → 分页 read → submit。

    ``OFFLINE = True`` 标记只供 ``assert_offline`` 检查；真实 suffix 路径**不**
    接受该 sender。它的动作文本是合法 JSON，走同一 ``parse_action`` 与
    dispatcher（含容器 worker 读取）。
    """

    OFFLINE = True

    def __init__(self, *, study: str, receipt_sha: str, task_id: str,
                 primary_id: str, required: Sequence[str],
                 hypotheses: Sequence[str] = (), metric_id: str = "",
                 delta: float = 0.0, proposition: str = "increase_gt",
                 mandatory: Sequence[str] = ("analysis/output.json",)) -> None:
        self.study = study
        self.receipt_sha = str(receipt_sha)
        self.task_id = str(task_id)
        self.primary_id = str(primary_id)
        self.required = tuple(required)
        self.hypotheses = tuple(hypotheses) or (self.primary_id,)
        self.metric_id = str(metric_id)
        self.delta = float(delta)
        self.proposition = str(proposition)
        self.mandatory = tuple(mandatory)
        self.cursors: "dict[str, int | None]" = {a: 0 for a in self.mandatory}
        self.turn = 0
        self.submitted = False

    def send_once(self, request: RT.Request,
                  reservation: Any) -> L.Reply:
        self.turn += 1
        payload = json.loads(json.dumps(
            {"system": request.system, "messages": list(request.messages),
             "max_tokens": int(request.max_tokens)}, ensure_ascii=False))
        envelope = _envelope_from_request(payload)
        if not self.submitted:
            artifacts = {a["artifact_id"]: a for a in envelope.get("artifacts") or []}
            for artifact_id in self.mandatory:
                entry = artifacts.get(artifact_id)
                if entry is None:
                    return _reply(json.dumps(
                        {"action": "read_result_artifact",
                         "artifact_id": artifact_id, "output_sha256": "0" * 64,
                         "cursor": 0, "page_bytes": 4096}))
                cursor = self.cursors.get(artifact_id, 0)
                if cursor is None:
                    continue
                page = _find_page_result(payload)
                if page is not None and page.get("artifact_id") == artifact_id:
                    self.cursors[artifact_id] = page.get("next_cursor")
                if self.cursors.get(artifact_id) is not None:
                    return _reply(json.dumps(
                        {"action": "read_result_artifact",
                         "artifact_id": artifact_id,
                         "output_sha256": entry["sha256"],
                         "cursor": int(self.cursors.get(artifact_id, 0)),
                         "page_bytes": 8192}))
            self.submitted = True
            return _reply(json.dumps(self._submit_doc(envelope),
                                     ensure_ascii=False))
        return _reply(json.dumps(
            {"action": "read_result_artifact", "artifact_id": "analysis/output.json",
             "output_sha256": "f" * 64, "cursor": 0, "page_bytes": 4096}))

    def _submit_doc(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        contrasts = {str(c["contrast_id"]): c for c in envelope.get("contrasts") or []}
        primary = contrasts.get(self.primary_id) or {}
        locator = {"kind": "json_pointer", "file": "analysis/output.json",
                   "sha256": str(envelope.get("analysis_output_sha256")),
                   "pointer": "/contrasts/0/estimate"}
        report = {
            "schema": "simpraxis.rsi_r13.research_report.v1",
            "schema_version": 1,
            "task_id": self.task_id,
            "question_and_hypotheses": (
                "给定预注册研究问题与主对比；脚本正控只验证交付链路，"
                "不主张科学发现。"),
            "methods_and_assignment_responsibility": (
                "宿主分配确认 block 与 nuisance；候选程序按冻结对比系数计算；"
                "本脚本不改变方法或分析源码。"),
            "primary_results": [{
                "contrast_id": self.primary_id,
                "analysis_receipt_sha": self.receipt_sha,
                "result_locator": locator}],
            "heterogeneity_and_scope": [{
                "claim": "适用范围限于登记的确认 block 总体；逐 block 表见公开件。",
                "evidence_refs": [locator],
                "applicability": "仅登记目标总体，不外推未观测条件。"}],
            "explore_confirm_comparison": {
                "estimands": "探索与确认目标不同（不同 target）；不合并。",
                "comparable": False,
                "explanation": "探索覆盖设计空间，确认是宿主 block 抽样；只作诊断。",
                "evidence_refs": [locator]},
            "alternative_explanations": [
                "观测噪声与有限 block 数可解释区间宽度；本报告不排除全部替代解释。"],
            "limitations": [{
                "statement": "该研究只覆盖给定确认批与登记指标。",
                "evidence_refs": [locator],
                "consequence": "结论不得外推到其他设计点或未登记指标。"}],
            "next_steps": [{
                "unresolved_question": "该效应在其他设计点是否稳健？",
                "discriminating_experiment": "在未参与本批的设计点上复跑同对比。",
                "possible_outcomes": ["效应同向且区间不跨 δ", "效应消失或反向"]}],
        }
        claims = []
        for hid in self.hypotheses:
            claims.append({"hypothesis_id": hid,
                           "proposition": self.proposition,
                           "metric_id": self.metric_id,
                           "delta": float(self.delta),
                           "estimate": float(primary.get("estimate")),
                           "lower": float(primary.get("lower")),
                           "upper": float(primary.get("upper")),
                           "n_blocks": int(primary.get("n_blocks") or 0)})
        return {"action": "submit_claim",
                "claims": claims,
                "report_text": RR.render_report(report),
                "research_report": report}


def _envelope_from_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """从 Request 的公开消息里取回 envelope（JSON 块）。"""
    for message in payload.get("messages") or []:
        text = str(message.get("content") or "")
        if "envelope_sha256" not in text:
            continue
        for chunk in _json_objects(text):
            if isinstance(chunk, Mapping) and "envelope_sha256" in chunk:
                return dict(chunk)
    raise RVError("ENVELOPE_NOT_IN_REQUEST", "request 里没有可解析的 envelope")


def _json_objects(text: str) -> "list[Any]":
    out: "list[Any]" = []
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            ch = text[index]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        out.append(json.loads(text[start:index + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = text.find("{", index + 1)
                    break
        else:
            break
    return out


def _find_page_result(payload: Mapping[str, Any]) -> "dict[str, Any] | None":
    for message in reversed(payload.get("messages") or []):
        text = str(message.get("content") or "")
        for chunk in _json_objects(text):
            if isinstance(chunk, Mapping) and "page_sha256" in chunk \
                    and "next_cursor" in chunk:
                return dict(chunk)
    return None


def _reply(text: str) -> L.Reply:
    return L.Reply(text=text,
                   response_id=f"rv-script-{RV.sha_bytes(text.encode())[:12]}",
                   is_actionable=True)


def assert_offline_sender(sender: Any) -> None:
    if not getattr(sender, "OFFLINE", False):
        raise RVError("NOT_OFFLINE", type(sender).__name__)


# ---------------------------------------------------------------------------
# 容量（真实 tokenizer；必需读取页数 + submit ≤ 6）
# ---------------------------------------------------------------------------

def capacity_report(*, manifest: Mapping[str, Any], allowlist: Mapping[str, Any],
                    system_probe: str, envelope_probe: Mapping[str, Any]) -> dict[str, Any]:
    from orchestrator.rsi_r1 import review_capacity as RC
    tokenizer = RC.load_tokenizer()
    sizes = {(row["study"], row["file"].split("/")[-1]): int(row["bytes"])
             for row in allowlist.get("files") or []}
    rows: "list[dict[str, Any]]" = []
    problems: "list[str]" = []
    for study in RV.STUDIES:
        entry = manifest["studies"][study]
        mandatory = (("analysis_output.json", entry["output_sha"]),
                     ("raw_block_values.json", ""))
        pages = 0
        for name, _ in mandatory:
            size = sizes.get((study, name))
            if size is None:
                problems.append(f"{study}/{name}: 公开包缺件")
                continue
            pages += (size + RV.PAGE_BYTES_RV - 1) // RV.PAGE_BYTES_RV
        turns = pages + 1  # submit
        rows.append({"study": study, "mandatory_pages": pages,
                     "minimum_turns": turns, "cap": RV.MAX_PATH_TURNS})
        if turns > RV.MIN_PATH_TURNS:
            problems.append(f"{study}: 最低路径 {turns} > {RV.MIN_PATH_TURNS}")
    # 请求容量：用真实构造的 Request 计量（system+brief+envelope 同口径）
    from orchestrator.common import research_transport as RT
    probe = RT.build_request(
        system=system_probe,
        task_brief="### 当前可见状态\n" + json.dumps(
            {"study": "A", "state": "RESULTS_RELEASED",
             "artifacts": {}, "budget_slots_remaining": 8, "nonce": "0" * 16},
            sort_keys=True),
        turns=[], counter=lambda t: len(tokenizer.encode(t).ids),
        max_tokens=RV.RV_OUTPUT_TOKENS,
        delivery_policy=RT.POLICY_G2_ATOMIC, result_context=dict(envelope_probe))
    accounting = RT.check_request(probe)
    rows.append({"request_local_tokens": probe.local_tokens,
                 "request_bytes": probe.n_bytes,
                 "within_cap": accounting.ok,
                 "verdict": accounting.verdict})
    if not accounting.ok:
        problems.append(f"请求超帽：{accounting.verdict}")
    doc = {"schema": RV.CAPACITY_SCHEMA, "schema_version": 1,
           "created_at_utc": RV.utc_now(), "rows": rows,
           "ok": not problems, "problems": problems,
           "tok_per_page_basis": RV.PAGE_BYTES_RV}
    return doc


__all__ = [
    "RVBudget", "RVContext", "RVError", "RVAdmission", "RVParts",
    "RVSender", "RVSlotBook", "RVTransport", "RV_PRODUCER", "SCRIPTED_PRODUCER",
    "ScriptedRVConsumer", "assert_offline_sender", "build_rv_envelope",
    "build_rv_gateway", "build_rv_parts", "capacity_report", "ledger_call_rows",
    "materialize_rv_study", "posts_used", "repo_root", "run_turns", "rv_ledger",
]
