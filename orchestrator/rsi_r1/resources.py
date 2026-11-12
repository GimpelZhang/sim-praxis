"""RSI-R1.2 T1：计算资源账（typed limits 单源、累计原子预留、精确公开快照）。

Plan §2.1 的落地模块。纪律：

* **单源**：所有上限从 ``profiles.CampaignProfile.resource_limits`` 读出；
  本模块不写任何资源数字（只写事件/字段名）。渲染、校验、最终请求审计共用
  同一个 typed limits 对象。
* **累计而非每次**：探索 64 是**每 study 累计**上限（不是每条件 64，也不要求
  32 block）。``reserve_explore`` 在 run_batch 前把 ``len(cells)`` 与总步数
  原子预留；第 9 个 batch 即使仍有 run 余额也拒绝（Plan §1.1 显式协议资源）。
* **预留即计费**：实际启动/未知 run 保留计算预占，不因失败回退（宁多勿少，
  错误侧不能借回余额）；确认/复跑在 freeze 时一次原子预留，之后由
  ``consume_confirm`` 从该预留里扣减（used/reserved 分离，remaining 精确）。
* **拒绝不代选**：超帽即 ``ResourceError``（code 进公开 refusal 表），宿主不
  截断 96 为 64、不代选「最佳方案」。
* **append-only + flock + prev-sha 链**：重启只从磁盘重读，不依赖进程状态；
  任何一行被改即 ``RESOURCE_LEDGER_TAMPERED``（fail-closed，不静默接受）。

事件类型（一行一事件，``event`` 字段）：

* ``reserve``（kind=``explore``）：探索矩阵预留（runs/steps 立即计费）；
* ``freeze``：确认 + 复跑的一次原子预留（runs/steps 双份，POST 余量门已过）；
* ``consume``（kind=``confirm``/``replay``）：从 freeze 预留里扣减实际执行；
* ``sandbox``：候选沙盒执行计数（被前执行拒绝的设计调用也计数）；
* ``wallclock``：活跃/容器/GPU 墙钟累计；
* ``aux``：aux 五子信封的步数占额（执行前先占）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import profiles as P

RESOURCE_EVENT_SCHEMA = "simpraxis.rsi_r12.resource_event.v1"
RESOURCE_LEDGER_NAME = "resources.jsonl"

EVENT_RESERVE = "reserve"
EVENT_FREEZE = "freeze"
EVENT_CONSUME = "consume"
EVENT_SANDBOX = "sandbox"
EVENT_WALLCLOCK = "wallclock"
EVENT_AUX = "aux"

KIND_EXPLORE = "explore"
KIND_CONFIRM = "confirm"
KIND_REPLAY = "replay"

STUDY_LANES = ("study_A", "study_B")
TOY_LANES = ("probe", "practice")


class ResourceError(C.ContractError):
    """资源账拒绝；``code`` 一律是已登记的公开 refusal 条款。"""


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _row_sha(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _sha_doc(doc: Any) -> str:
    return hashlib.sha256(_canonical_bytes(doc)).hexdigest()


@dataclass(frozen=True)
class Reservation:
    """一次成功预留/扣减的事件投影。"""

    seq: int
    study: str
    kind: str
    runs: int
    steps: int
    batch_id: "str | None"
    sha256: str

    def to_dict(self) -> "dict[str, Any]":
        return {"seq": self.seq, "study": self.study, "kind": self.kind,
                "runs": self.runs, "steps": self.steps,
                "batch_id": self.batch_id, "sha256": self.sha256}


@dataclass(frozen=True)
class FreezeReservation:
    """freeze 时确认 + 复跑的原子预留投影。"""

    seq: int
    study: str
    lane: str
    confirm_blocks: int
    n_conditions: int
    confirm_runs: int
    replay_runs: int
    confirm_steps: int
    replay_steps: int
    post_margin: int
    sha256: str

    def to_dict(self) -> "dict[str, Any]":
        return {"seq": self.seq, "study": self.study, "lane": self.lane,
                "confirm_blocks": self.confirm_blocks,
                "n_conditions": self.n_conditions,
                "confirm_runs": self.confirm_runs,
                "replay_runs": self.replay_runs,
                "confirm_steps": self.confirm_steps,
                "replay_steps": self.replay_steps,
                "post_margin": self.post_margin, "sha256": self.sha256}


class ResourceLedger:
    """每 campaign 一份的累计资源账（``<root>/state/resources.jsonl``）。"""

    def __init__(self, root: "Path | str", *,
                 profile: P.CampaignProfile = P.PROFILE_R12_DEF,
                 clock: Callable[[], str] = _utc_now_iso_z) -> None:
        if not profile.is_r12:
            raise ResourceError(
                "PROFILE_NOT_ELIGIBLE",
                f"资源账 v2 只接受 rsi-r1.2 profile（得到 {profile.profile_id}）")
        self.root = Path(root)
        self.profile = profile
        self.limits = profile.resource_limits
        self.clock = clock
        self.dir = self.root / "state"
        self.path = self.dir / RESOURCE_LEDGER_NAME
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    # -- 读 --------------------------------------------------------------

    def rows(self) -> "tuple[dict[str, Any], ...]":
        out: "list[dict[str, Any]]" = []
        prev = None
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:  # 不可读即不可用，不猜
            raise ResourceError("RESOURCE_LEDGER_CORRUPT",
                                f"资源账不可读：{exc}") from None
        for i, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError as exc:
                raise ResourceError("RESOURCE_LEDGER_CORRUPT",
                                    f"第 {i} 行非 JSON：{exc}") from None
            if not isinstance(doc, dict):
                raise ResourceError("RESOURCE_LEDGER_CORRUPT", f"第 {i} 行非对象")
            if doc.get("schema") != RESOURCE_EVENT_SCHEMA:
                raise ResourceError("RESOURCE_LEDGER_CORRUPT",
                                    f"第 {i} 行 schema={doc.get('schema')!r}")
            if doc.get("seq") != i:
                raise ResourceError("RESOURCE_LEDGER_CORRUPT",
                                    f"第 {i} 行 seq={doc.get('seq')} 不连续")
            if doc.get("prev_sha256") != prev:
                raise ResourceError("RESOURCE_LEDGER_TAMPERED",
                                    f"第 {i} 行 prev_sha256 断链")
            body = {k: v for k, v in doc.items() if k != "sha256"}
            if doc.get("sha256") != _row_sha(body):
                raise ResourceError("RESOURCE_LEDGER_TAMPERED",
                                    f"第 {i} 行 sha256 不符")
            prev = str(doc["sha256"])
            out.append(doc)
        return tuple(out)

    def verify(self) -> "dict[str, Any]":
        rows = self.rows()
        return {"rows": len(rows),
                "head_sha256": None if not rows else str(rows[-1]["sha256"])}

    # -- 写（flock 内 read-validate-append 原子）-------------------------

    def _appender(self, event: str, payload_factory: Callable[
            ["tuple[dict[str, Any], ...]"], Mapping[str, Any]]) -> "dict[str, Any]":
        """在锁内以**最新行集**重新校验后追加（校验与追加同一临界区）。"""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd_lock = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd_lock, fcntl.LOCK_EX)
            rows = self.rows()
            payload = dict(payload_factory(rows))
            prev = None if not rows else str(rows[-1]["sha256"])
            seq = len(rows) + 1
            body = {"schema": RESOURCE_EVENT_SCHEMA, "schema_version": 1,
                    "seq": seq, "ts_iso": self.clock(), "event": event,
                    **payload, "prev_sha256": prev}
            doc = {**body, "sha256": _row_sha(body)}
            line = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                              allow_nan=False) + "\n"
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(fd_lock, fcntl.LOCK_UN)
            os.close(fd_lock)
        return doc

    # -- 聚合（纯函数，锁内外共用）---------------------------------------

    @staticmethod
    def _rows_for(rows: Iterable[Mapping[str, Any]], study: str, *,
                  event: str, kind: "str | None" = None
                  ) -> "list[Mapping[str, Any]]":
        return [r for r in rows
                if r.get("study") == study and r.get("event") == event
                and (kind is None or r.get("kind") == kind)]

    def _used(self, rows: Iterable[Mapping[str, Any]], study: str) -> "dict[str, int]":
        rows = tuple(rows)
        explore = self._rows_for(rows, study, event=EVENT_RESERVE,
                                 kind=KIND_EXPLORE)
        consumes = self._rows_for(rows, study, event=EVENT_CONSUME)
        freezes = self._rows_for(rows, study, event=EVENT_FREEZE)
        sandbox = self._rows_for(rows, study, event=EVENT_SANDBOX)
        wall = self._rows_for(rows, study, event=EVENT_WALLCLOCK)
        aux = self._rows_for(rows, study, event=EVENT_AUX)
        explore_runs = sum(int(r["runs"]) for r in explore)
        explore_steps = sum(int(r["steps"]) for r in explore)
        confirm_used = sum(int(r["runs"]) for r in consumes
                           if r.get("kind") == KIND_CONFIRM)
        replay_used = sum(int(r["runs"]) for r in consumes
                          if r.get("kind") == KIND_REPLAY)
        confirm_steps = sum(int(r["steps"]) for r in consumes
                            if r.get("kind") == KIND_CONFIRM)
        replay_steps = sum(int(r["steps"]) for r in consumes
                           if r.get("kind") == KIND_REPLAY)
        frozen_confirm = sum(int(r["confirm_runs"]) for r in freezes)
        frozen_replay = sum(int(r["replay_runs"]) for r in freezes)
        frozen_confirm_steps = sum(int(r["confirm_steps"]) for r in freezes)
        frozen_replay_steps = sum(int(r["replay_steps"]) for r in freezes)
        aux_by_env: "dict[str, int]" = {}
        for r in aux:
            key = str(r.get("envelope"))
            aux_by_env[key] = aux_by_env.get(key, 0) + int(r["steps"])
        batches = {str(r["batch_id"]) for r in explore
                   if r.get("batch_id") not in (None, "")}
        return {
            "explore_runs": explore_runs,
            "explore_steps": explore_steps,
            "explore_batches": len(batches),
            "confirm_used": confirm_used,
            "replay_used": replay_used,
            "confirm_frozen": frozen_confirm,
            "replay_frozen": frozen_replay,
            "confirm_reserved": frozen_confirm - confirm_used,
            "replay_reserved": frozen_replay - replay_used,
            "confirm_steps_used": confirm_steps,
            "replay_steps_used": replay_steps,
            "confirm_steps_reserved": frozen_confirm_steps - confirm_steps,
            "replay_steps_reserved": frozen_replay_steps - replay_steps,
            "sandbox": len(sandbox),
            "active_s": sum(int(r.get("active_s") or 0) for r in wall),
            "container_s": sum(int(r.get("container_s") or 0) for r in wall),
            "gpu_s": sum(int(r.get("gpu_s") or 0) for r in wall),
            "aux_by_env": aux_by_env,
            "aux_total": sum(aux_by_env.values()),
        }

    def _global_wallclock(self, rows: Iterable[Mapping[str, Any]]) -> "dict[str, int]":
        wall = [r for r in rows if r.get("event") == EVENT_WALLCLOCK]
        return {"container_s": sum(int(r.get("container_s") or 0) for r in wall),
                "gpu_s": sum(int(r.get("gpu_s") or 0) for r in wall)}

    def _global_aux(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return sum(int(r["steps"]) for r in rows if r.get("event") == EVENT_AUX)

    def _global_runs(self, rows: Iterable[Mapping[str, Any]]) -> "dict[str, int]":
        used = 0
        reserved = 0
        for r in rows:
            if r.get("event") == EVENT_RESERVE and r.get("kind") == KIND_EXPLORE:
                used += int(r["runs"])
            elif r.get("event") == EVENT_CONSUME:
                used += int(r["runs"])
            elif r.get("event") == EVENT_FREEZE:
                reserved += int(r["confirm_runs"]) + int(r["replay_runs"])
        if reserved < 0:
            raise ResourceError("RESOURCE_LEDGER_CORRUPT", "全局预留为负")
        return {"used": used, "reserved": reserved}

    def _check_reserved_nonnegative(self, totals: Mapping[str, int], *, where: str) -> None:
        for key in ("confirm_reserved", "replay_reserved",
                    "confirm_steps_reserved", "replay_steps_reserved"):
            if int(totals[key]) < 0:
                raise ResourceError(
                    "RESOURCE_LEDGER_CORRUPT",
                    f"{where} {key}={totals[key]} 为负（扣减超过冻结预留）")

    # -- 探索预留 --------------------------------------------------------

    def reserve_explore(self, study: str, specs: Sequence[Any], *,
                        batch_id: "str | None" = None,
                        phase: str = "explore") -> Reservation:
        """run_batch 前原子预留（累计 64 run / 8 batch / 步数 / 全局 run）。"""
        runs = len(specs)
        if runs <= 0:
            raise ResourceError("RESOURCE_EXCEEDED", "空矩阵不能预留")
        per_run_steps: "list[int]" = []
        for s in specs:
            n = int(getattr(s, "nsteps"))
            if n <= 0:
                raise ResourceError("STEP_LIMIT", f"nsteps={n} 非正")
            if n > self.limits.steps_per_run:
                raise ResourceError(
                    "STEP_LIMIT",
                    f"单 run nsteps={n} > 每 run 帽 {self.limits.steps_per_run}")
            per_run_steps.append(n)
        steps = sum(per_run_steps)
        if batch_id is not None and not isinstance(batch_id, str):
            raise ResourceError("RESOURCE_EXCEEDED", "batch_id 必须是字符串或 null")
        limits = self.limits

        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            totals = self._used(rows, study)
            self._check_reserved_nonnegative(totals, where=study)
            remaining_runs = limits.explore_runs_per_study - totals["explore_runs"]
            if runs > remaining_runs:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"matrix.cells={runs} > 探索累计余量 {remaining_runs}"
                    f"（每 study 帽 {limits.explore_runs_per_study}，已用 "
                    f"{totals['explore_runs']}；不截断、不代选）")
            batches = totals["explore_batches"]
            new_batch = batch_id not in (None, "") and batch_id not in {
                str(r.get("batch_id")) for r in self._rows_for(
                    rows, study, event=EVENT_RESERVE, kind=KIND_EXPLORE)}
            if new_batch and batches + 1 > limits.explore_batches_per_study:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"探索 batch 数 {batches + 1} > 每 study 帽 "
                    f"{limits.explore_batches_per_study}（第 {batches + 1} 个 batch "
                    f"即使仍有 {remaining_runs} run 余额也拒绝）")
            used_steps = (totals["explore_steps"]
                          + totals["confirm_steps_used"] + totals["replay_steps_used"]
                          + totals["confirm_steps_reserved"]
                          + totals["replay_steps_reserved"])
            room = limits.study_steps - used_steps
            if steps > room:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"矩阵总步数 {steps} > 剩余 study 步数 {room}"
                    f"（帽 {limits.study_steps}）")
            global_runs = self._global_runs(rows)
            if global_runs["used"] + global_runs["reserved"] + runs > limits.total_runs:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"全局 run {global_runs['used']}+{global_runs['reserved']}+{runs} "
                    f"> 整轮帽 {limits.total_runs}")
            return {"study": study, "kind": KIND_EXPLORE, "runs": runs,
                    "steps": steps, "batch_id": batch_id, "phase": phase}

        doc = self._appender(EVENT_RESERVE, _payload)
        return Reservation(seq=int(doc["seq"]), study=study, kind=KIND_EXPLORE,
                           runs=runs, steps=steps, batch_id=batch_id,
                           sha256=str(doc["sha256"]))

    # -- 确认/复跑 -------------------------------------------------------

    def check_confirm_shape(self, *, blocks: int, n_conditions: int,
                            runs: "int | None" = None) -> int:
        """确认集形状：恰 32 block、1..6 条件、runs=32×条件数 ≤192。"""
        limits = self.limits
        if int(blocks) != limits.confirm_blocks:
            raise ResourceError(
                "CONFIRM_BLOCK_COUNT",
                f"确认批 block={blocks} != 固定 {limits.confirm_blocks}")
        if not (1 <= int(n_conditions) <= limits.confirm_max_conditions):
            raise ResourceError(
                "MAX_CONDITIONS",
                f"确认条件数 {n_conditions} 不在 1..{limits.confirm_max_conditions}")
        want = int(blocks) * int(n_conditions)
        if runs is not None and int(runs) != want:
            raise ResourceError(
                "RESOURCE_EXCEEDED",
                f"确认 runs={runs} != block×条件 = {want}")
        if want > limits.confirm_runs_per_study:
            raise ResourceError(
                "RESOURCE_EXCEEDED",
                f"确认 runs={want} > 每 study 帽 {limits.confirm_runs_per_study}")
        return want

    def reserve_freeze(self, study: str, *, lane: str, blocks: int,
                       n_conditions: int, replay_runs: int,
                       confirm_steps: int, replay_steps: int,
                       posts_remaining_after_charge: int,
                       purpose: str = "") -> FreezeReservation:
        """freeze_confirm 时一次原子预留确认 + 复跑算力，并过 POST 余量门。

        ``posts_remaining_after_charge`` 由调用者从**本 lane** 的 SlotBook 计数：
        扣掉当次 POST 后本 lane 剩余额度。study lane 需 ≥8、probe/practice ≥2；
        不得借其他 lane 余额（调用者传的就是本 lane 数）。
        """
        confirm_runs = self.check_confirm_shape(blocks=blocks,
                                                n_conditions=n_conditions)
        replay_runs = int(replay_runs)
        if replay_runs < 0:
            raise ResourceError("RESOURCE_EXCEEDED", "replay_runs 为负")
        limits = self.limits
        if lane not in self.profile.lane_order:
            raise ResourceError("LANE_UNKNOWN", repr(lane))
        margin = (limits.confirm_post_margin if lane in STUDY_LANES
                  else limits.protocol_post_margin)
        if int(posts_remaining_after_charge) < margin:
            raise ResourceError(
                "BUDGET_UNREACHABLE",
                f"{lane} 扣当次 POST 后余 {posts_remaining_after_charge} < 余量门 "
                f"{margin}（不借 lane 余额）")
        confirm_steps = int(confirm_steps)
        replay_steps = int(replay_steps)
        if confirm_steps < 0 or replay_steps < 0:
            raise ResourceError("RESOURCE_EXCEEDED", "步数为负")
        if confirm_runs and confirm_steps > confirm_runs * limits.steps_per_run:
            raise ResourceError(
                "STEP_LIMIT",
                f"确认总步数 {confirm_steps} > {confirm_runs}×"
                f"{limits.steps_per_run}")
        if replay_runs and replay_steps > replay_runs * limits.steps_per_run:
            raise ResourceError(
                "STEP_LIMIT",
                f"复跑总步数 {replay_steps} > {replay_runs}×"
                f"{limits.steps_per_run}")

        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            totals = self._used(rows, study)
            self._check_reserved_nonnegative(totals, where=study)
            room_confirm = (limits.confirm_runs_per_study
                            - totals["confirm_used"] - totals["confirm_reserved"])
            if confirm_runs > room_confirm:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"确认 {confirm_runs} run > 余量 {room_confirm}"
                    f"（帽 {limits.confirm_runs_per_study}）")
            room_replay = (limits.replay_runs_per_study
                           - totals["replay_used"] - totals["replay_reserved"])
            if replay_runs > room_replay:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"复跑 {replay_runs} run > 余量 {room_replay}"
                    f"（帽 {limits.replay_runs_per_study}）")
            used_steps = (totals["explore_steps"] + totals["confirm_steps_used"]
                          + totals["replay_steps_used"]
                          + totals["confirm_steps_reserved"]
                          + totals["replay_steps_reserved"])
            if used_steps + confirm_steps + replay_steps > limits.study_steps:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"freeze 后 study 步数 {used_steps + confirm_steps + replay_steps}"
                    f" > 帽 {limits.study_steps}")
            global_runs = self._global_runs(rows)
            if (global_runs["used"] + global_runs["reserved"]
                    + confirm_runs + replay_runs > limits.total_runs):
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"freeze 后全局 run 超过整轮帽 {limits.total_runs}")
            return {"study": study, "kind": "freeze", "lane": lane,
                    "confirm_blocks": int(blocks), "n_conditions": int(n_conditions),
                    "confirm_runs": confirm_runs, "replay_runs": replay_runs,
                    "confirm_steps": confirm_steps, "replay_steps": replay_steps,
                    "post_margin": margin, "purpose": purpose,
                    "posts_remaining_after_charge": int(posts_remaining_after_charge)}

        doc = self._appender(EVENT_FREEZE, _payload)
        return FreezeReservation(
            seq=int(doc["seq"]), study=study, lane=lane,
            confirm_blocks=int(blocks), n_conditions=int(n_conditions),
            confirm_runs=confirm_runs, replay_runs=replay_runs,
            confirm_steps=confirm_steps, replay_steps=replay_steps,
            post_margin=margin, sha256=str(doc["sha256"]))

    def consume_confirm(self, study: str, *, kind: str, runs: int, steps: int,
                        batch_id: "str | None" = None) -> Reservation:
        """从 freeze 预留里扣减实际确认/复跑执行（不新增 runs 计费）。"""
        if kind not in (KIND_CONFIRM, KIND_REPLAY):
            raise ResourceError("RESOURCE_EXCEEDED", f"consume kind={kind!r}")
        runs = int(runs)
        steps = int(steps)
        if runs <= 0:
            raise ResourceError("RESOURCE_EXCEEDED", "consume runs 必须为正")
        if steps > runs * self.limits.steps_per_run:
            raise ResourceError(
                "STEP_LIMIT",
                f"consume 步数 {steps} > {runs}×{self.limits.steps_per_run}")

        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            totals = self._used(rows, study)
            self._check_reserved_nonnegative(totals, where=study)
            key = "confirm_reserved" if kind == KIND_CONFIRM else "replay_reserved"
            if runs > int(totals[key]):
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"{kind} 执行 {runs} run > freeze 未消费预留 {totals[key]}"
                    "（确认/复跑只能消费 freeze 时已预留的算力）")
            skey = ("confirm_steps_reserved" if kind == KIND_CONFIRM
                    else "replay_steps_reserved")
            if steps > int(totals[skey]):
                raise ResourceError(
                    "STEP_LIMIT",
                    f"{kind} 执行步数 {steps} > freeze 未消费步数预留 {totals[skey]}")
            return {"study": study, "kind": kind, "runs": runs, "steps": steps,
                    "batch_id": batch_id}

        doc = self._appender(EVENT_CONSUME, _payload)
        return Reservation(seq=int(doc["seq"]), study=study, kind=kind,
                           runs=runs, steps=steps, batch_id=batch_id,
                           sha256=str(doc["sha256"]))

    # -- 沙盒 / 墙钟 / aux ----------------------------------------------

    def reserve_sandbox(self, study: str, *, purpose: str,
                        rejected_before_execution: bool = False) -> Reservation:
        """候选沙盒执行一次；被前执行拒绝的设计调用也计数（Plan §2.1）。"""
        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            totals = self._used(rows, study)
            if totals["sandbox"] + 1 > self.limits.sandbox_executions_per_study:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"沙盒执行 {totals['sandbox'] + 1} > 每 study 帽 "
                    f"{self.limits.sandbox_executions_per_study}")
            return {"study": study, "kind": "sandbox", "runs": 0, "steps": 0,
                    "purpose": purpose,
                    "rejected_before_execution": bool(rejected_before_execution)}

        doc = self._appender(EVENT_SANDBOX, _payload)
        return Reservation(seq=int(doc["seq"]), study=study, kind="sandbox",
                           runs=0, steps=0, batch_id=None,
                           sha256=str(doc["sha256"]))

    def record_wallclock(self, study: str, *, active_s: int = 0,
                         container_s: int = 0, gpu_s: int = 0) -> Reservation:
        """累计墙钟：活跃每 study；容器/GPU 整轮合计（Plan §2.1）。"""
        for name, value in (("active_s", active_s), ("container_s", container_s),
                            ("gpu_s", gpu_s)):
            if not isinstance(value, int) or value < 0:
                raise ResourceError("RESOURCE_EXCEEDED",
                                    f"{name}={value!r} 必须是非负整数")
        limits = self.limits

        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            totals = self._used(rows, study)
            if totals["active_s"] + active_s > limits.active_wallclock_s:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"{study} 活跃 {totals['active_s'] + active_s}s > 帽 "
                    f"{limits.active_wallclock_s}s")
            glob = self._global_wallclock(rows)
            if glob["container_s"] + container_s > limits.container_wallclock_s:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"容器墙钟 {glob['container_s'] + container_s}s > 帽 "
                    f"{limits.container_wallclock_s}s")
            if glob["gpu_s"] + gpu_s > limits.gpu_wallclock_s:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"GPU 墙钟 {glob['gpu_s'] + gpu_s}s > 帽 "
                    f"{limits.gpu_wallclock_s}s")
            return {"study": study, "kind": "wallclock",
                    "active_s": active_s, "container_s": container_s,
                    "gpu_s": gpu_s}

        doc = self._appender(EVENT_WALLCLOCK, _payload)
        return Reservation(seq=int(doc["seq"]), study=study, kind="wallclock",
                           runs=0, steps=0, batch_id=None,
                           sha256=str(doc["sha256"]))

    def reserve_aux(self, study: str, *, envelope: str, steps: int) -> Reservation:
        """aux 五子信封占额：逐 spec ≤ 各自帽、合计 ≤ 2m（离线/付费同计）。"""
        if envelope not in P.AUX_SUBENVELOPES:
            raise ResourceError("RESOURCE_EXCEEDED",
                                f"未知 aux 子信封 {envelope!r}")
        steps = int(steps)
        if steps <= 0:
            raise ResourceError("RESOURCE_EXCEEDED", "aux steps 必须为正")

        def _payload(rows: "tuple[dict[str, Any], ...]") -> Mapping[str, Any]:
            cap = int(P.AUX_SUBENVELOPES[envelope])
            used_env = sum(int(r["steps"]) for r in rows
                           if r.get("event") == EVENT_AUX
                           and r.get("envelope") == envelope)
            if used_env + steps > cap:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"aux {envelope} {used_env + steps} step > 子信封帽 {cap}")
            total = self._global_aux(rows)
            if total + steps > self.limits.aux_steps:
                raise ResourceError(
                    "RESOURCE_EXCEEDED",
                    f"aux 合计 {total + steps} step > 总帽 {self.limits.aux_steps}")
            return {"study": study, "kind": "aux", "runs": 0, "steps": steps,
                    "envelope": envelope}

        doc = self._appender(EVENT_AUX, _payload)
        return Reservation(seq=int(doc["seq"]), study=study, kind="aux",
                           runs=0, steps=steps, batch_id=None,
                           sha256=str(doc["sha256"]))

    # -- 公开快照 --------------------------------------------------------

    def snapshot(self, study: str, *, phase: "str | None" = None) -> "dict[str, Any]":
        """精确 used/reserved/remaining（可见状态与最终请求共用同一份数）。"""
        rows = self.rows()
        totals = self._used(rows, study)
        self._check_reserved_nonnegative(totals, where=study)
        limits = self.limits
        glob_runs = self._global_runs(rows)
        glob_wall = self._global_wallclock(rows)
        if phase is None:
            phase = "unknown"
            for r in reversed(rows):
                if r.get("study") == study and r.get("phase"):
                    phase = str(r["phase"])
                    break
        explore_used = totals["explore_runs"]
        confirm_used, confirm_res = totals["confirm_used"], totals["confirm_reserved"]
        replay_used, replay_res = totals["replay_used"], totals["replay_reserved"]
        runs_used = explore_used + confirm_used + replay_used
        runs_res = confirm_res + replay_res
        steps_used = (totals["explore_steps"] + totals["confirm_steps_used"]
                      + totals["replay_steps_used"])
        steps_res = (totals["confirm_steps_reserved"]
                     + totals["replay_steps_reserved"])
        aux_by_env = {name: {"max": int(cap),
                             "used": int(totals["aux_by_env"].get(name, 0)),
                             "remaining": int(cap) - int(totals["aux_by_env"].get(name, 0))}
                      for name, cap in P.AUX_SUBENVELOPES.items()}
        return {
            "study": study,
            "phase": phase,
            "explore": {
                "max_runs": limits.explore_runs_per_study,
                "used": explore_used, "reserved": explore_used,
                "remaining": limits.explore_runs_per_study - explore_used,
                "max_batches": limits.explore_batches_per_study,
                "batches_used": totals["explore_batches"],
                "batches_remaining": (limits.explore_batches_per_study
                                      - totals["explore_batches"]),
            },
            "confirm": {
                "max_runs": limits.confirm_runs_per_study,
                "used": confirm_used, "reserved": confirm_res,
                "remaining": limits.confirm_runs_per_study - confirm_used - confirm_res,
                "blocks": limits.confirm_blocks,
                "max_conditions": limits.confirm_max_conditions,
                "post_margin": limits.confirm_post_margin,
            },
            "replay": {
                "max_runs": limits.replay_runs_per_study,
                "used": replay_used, "reserved": replay_res,
                "remaining": limits.replay_runs_per_study - replay_used - replay_res,
            },
            "runs_total": {
                "max": limits.total_runs,
                "used": glob_runs["used"], "reserved": glob_runs["reserved"],
                "remaining": limits.total_runs - glob_runs["used"] - glob_runs["reserved"],
            },
            "steps": {
                "max_study": limits.study_steps,
                "max_per_run": limits.steps_per_run,
                "max_total": limits.total_steps,
                "used": steps_used, "reserved": steps_res,
                "remaining": limits.study_steps - steps_used - steps_res,
                "aux_max": limits.aux_steps,
                "aux_used": totals["aux_total"],
                "aux_remaining": limits.aux_steps - totals["aux_total"],
            },
            "sandbox": {
                "max": limits.sandbox_executions_per_study,
                "used": totals["sandbox"],
                "reserved": totals["sandbox"],
                "remaining": limits.sandbox_executions_per_study - totals["sandbox"],
            },
            "wallclock": {
                "active_max_s": limits.active_wallclock_s,
                "active_used_s": totals["active_s"],
                "active_remaining_s": limits.active_wallclock_s - totals["active_s"],
                "container_max_s": limits.container_wallclock_s,
                "container_used_s": glob_wall["container_s"],
                "container_remaining_s": (limits.container_wallclock_s
                                          - glob_wall["container_s"]),
                "gpu_max_s": limits.gpu_wallclock_s,
                "gpu_used_s": glob_wall["gpu_s"],
                "gpu_remaining_s": limits.gpu_wallclock_s - glob_wall["gpu_s"],
            },
            "aux": {"subenvelopes": aux_by_env,
                    "total": {"max": limits.aux_steps, "used": totals["aux_total"],
                              "remaining": limits.aux_steps - totals["aux_total"]}},
            "ledger": {"rows": len(rows),
                       "head_sha256": None if not rows else str(rows[-1]["sha256"])},
        }


# ---------------------------------------------------------------------------
# 矩阵校验 v2：逐 cell 仍用 experiments.validate_matrix（单一实现），
# 累计帽由资源账在发送/执行前原子预留。
# ---------------------------------------------------------------------------

def validate_matrix_v2(matrix_json: Any, plan: C.ResearchPlan, *,
                       ledger: ResourceLedger, study: str,
                       batch_id: "str | None" = None,
                       phase: str = "explore"
                       ) -> "tuple[tuple[Any, ...], Reservation]":
    """R1.2 探索矩阵闸：形状/域/重复/step 由 ``validate_matrix`` 判，
    **累计** 64 run / 8 batch / 整轮 run 与步数由资源账在 run_batch 前原子预留。

    任一环节拒绝都不产生物理 run（调用方在 reserve 成功前不得启动 backend）。
    """
    limits = ledger.limits
    proxy = types.SimpleNamespace(
        max_runs_by_lane={KIND_EXPLORE: limits.explore_runs_per_study},
        max_steps=limits.study_steps)
    specs = E.validate_matrix(matrix_json, plan, proxy)
    reservation = ledger.reserve_explore(study, specs, batch_id=batch_id,
                                         phase=phase)
    return specs, reservation


def limits_document(profile: P.CampaignProfile = P.PROFILE_R12_DEF) -> "dict[str, Any]":
    """validator 的 typed limits 投影（审计/渲染共用；不复制字面量）。"""
    limits = profile.resource_limits
    return {
        "profile_id": profile.profile_id,
        "limits": limits.to_dict(),
        "lane_post_caps": {lane: profile.lane_post_caps[lane]
                           for lane in profile.lane_order},
        "total_posts": profile.hard_cap,
        "family_cap": profile.family_cap,
        "family_history_posts": profile.family_history_posts,
        "aux_subenvelopes": dict(P.AUX_SUBENVELOPES),
        "aux_total": P.AUX_SUBENVELOPE_TOTAL,
        "confirm_post_margin": limits.confirm_post_margin,
        "protocol_post_margin": limits.protocol_post_margin,
        "limits_sha256": _sha_doc(limits.to_dict()),
    }


__all__ = [
    "RESOURCE_EVENT_SCHEMA", "RESOURCE_LEDGER_NAME", "ResourceError",
    "Reservation", "FreezeReservation", "ResourceLedger",
    "validate_matrix_v2", "limits_document",
    "EVENT_RESERVE", "EVENT_FREEZE", "EVENT_CONSUME", "EVENT_SANDBOX",
    "EVENT_WALLCLOCK", "EVENT_AUX", "KIND_EXPLORE", "KIND_CONFIRM",
    "KIND_REPLAY",
]
