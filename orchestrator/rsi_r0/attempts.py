"""RSI-R0 发网事实层：串行锁、资源预留/结算、unknown 恢复、硬帽。

计划 §6.3 把三份权威分开：

* ``quota/ledger.jsonl``             —— **调用结算**（谁花了第几个 seq）
* ``quota/attempts.jsonl``           —— **发网事实**（这次逻辑调用到底有没有 POST 出去）
* ``quota/resource_reservations/``   —— **预留与不确定开销**（先占后花，崩溃不释放）

三者靠 ``request_id`` 关联，任何一方都不得自行重算另一个的总数（AC31 对账时逐
``request_id`` 比对）。本模块只拥有后两份；ledger 仍由 ``QuotaLedger`` 负责。

关键失败语义（AC09）：

* 发网与结算之间崩溃 ⇒ 恢复时置 ``UNKNOWN_SENT``：**耗位、预留不释放、不重发**；
* 只有可证实发网前中止才可结算为 ``BLOCKED_PREFLIGHT``，且该 slot 仍算已消耗；
* 缓存只服务零付费重放调试，正式试次禁用响应缓存。
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import (
    rsi_r0_attempts_path,
    rsi_r0_reservations_dir,
)
from orchestrator.rsi_r0.contracts import (
    AttemptRecord,
    AttemptState,
    ResourceCapExceeded,
    ResourceCaps,
    canonical_json,
    sha256_of,
)


class ConcurrentCampaignError(RuntimeError):
    """同一 campaign 已有另一个执行器持锁（AC08：并发第二执行器被拒绝）。"""


class UnknownAttemptError(RuntimeError):
    """对未预留的 request_id 做结算/发网标记。"""


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


@contextmanager
def campaign_lock(quota_dir: Path) -> Iterator[Path]:
    """campaign 级串行锁（``flock`` 非阻塞）。

    R0 全部串行（计划 §6.3「禁止并发 paid worker」）。锁文件放在 quota/ 下，
    进程退出自动释放；拿到不到即抛 :class:`ConcurrentCampaignError`——
    绝不排队等待，也绝不「乐观继续」。
    """
    ensure_private_dir(quota_dir)
    lock_path = quota_dir / ".r0.lock"
    fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 —— 生命周期由 with 管
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConcurrentCampaignError(
                f"campaign 已有执行器持锁：{lock_path}") from exc
        os.chmod(lock_path, 0o600)
        yield lock_path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    ensure_private_dir(path.parent)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class BudgetReservations:
    """资源预留账：先占后花；崩溃后不释放（计划 §6.3）。"""

    def __init__(self, caps: ResourceCaps, reservations_dir: Path | None = None) -> None:
        self.caps = caps
        self.dir = ensure_private_dir(reservations_dir or rsi_r0_reservations_dir())

    def path_for(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"

    def load_all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in sorted(self.dir.glob("req_*.json")):
            try:
                out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                out[p.stem] = {"request_id": p.stem, "state": "CORRUPT"}
        return out

    def totals(self) -> dict[str, float]:
        """已占用（含 UNKNOWN 未释放）的累计量。"""
        acc = {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "requests": 0}
        for row in self.load_all().values():
            acc["requests"] += 1
            acc["tokens_in"] += int(row.get("reserved_tokens_in") or 0)
            acc["tokens_out"] += int(row.get("reserved_tokens_out") or 0)
            acc["usd"] += float(row.get("reserved_usd") or 0.0)
        return acc

    def reserve(self, *, request_id: str, slot_id: str, batch_id: str, task_id: int,
                stage: str, tokens_in: int, tokens_out: int, usd: float) -> dict[str, Any]:
        """发网**之前**原子占位；任一门先到即拒（AC08/AC10）。"""
        if tokens_in > self.caps.per_request_input_tokens:
            raise ResourceCapExceeded(
                f"单请求输入 {tokens_in} > {self.caps.per_request_input_tokens}")
        if tokens_out > self.caps.per_request_output_tokens:
            raise ResourceCapExceeded(
                f"单请求输出 {tokens_out} > {self.caps.per_request_output_tokens}")
        cur = self.totals()
        if cur["tokens_in"] + tokens_in > self.caps.cumulative_input_tokens:
            raise ResourceCapExceeded(
                f"累计输入帽：{cur['tokens_in']}+{tokens_in} > "
                f"{self.caps.cumulative_input_tokens}")
        if cur["tokens_out"] + tokens_out > self.caps.cumulative_output_tokens:
            raise ResourceCapExceeded(
                f"累计输出帽：{cur['tokens_out']}+{tokens_out} > "
                f"{self.caps.cumulative_output_tokens}")
        if cur["usd"] + usd > self.caps.total_usd_equivalent:
            raise ResourceCapExceeded(
                f"费用帽：{cur['usd']:.4f}+{usd:.4f} > {self.caps.total_usd_equivalent}")
        doc = {
            "request_id": request_id,
            "slot_id": slot_id,
            "batch_id": batch_id,
            "task_id": task_id,
            "stage": stage,
            "state": "RESERVED",
            "reserved_tokens_in": int(tokens_in),
            "reserved_tokens_out": int(tokens_out),
            "reserved_usd": float(usd),
            "reserved_at_utc": _utc_now(),
        }
        target = self.path_for(request_id)
        if target.exists():
            raise RuntimeError(f"request_id 重复预留：{request_id}")
        _atomic_write_json(target, doc)
        return doc

    def settle(self, request_id: str, *, usage_in: int | None, usage_out: int | None,
               usd: float | None) -> dict[str, Any]:
        """结算：状态置 SETTLED，**实际用量**单列，预留字段原样保留（对账用）。"""
        p = self.path_for(request_id)
        if not p.exists():
            raise UnknownAttemptError(f"未预留的 request_id：{request_id}")
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc.update({
            "state": "SETTLED",
            "usage_in": usage_in,
            "usage_out": usage_out,
            "actual_usd": usd,
            "settled_at_utc": _utc_now(),
        })
        _atomic_write_json(p, doc)
        return doc

    def mark_unknown(self, request_id: str, reason: str) -> dict[str, Any]:
        """发网与结算之间失联：保留占用、标记 UNKNOWN（AC09：不释放、不重发）。"""
        p = self.path_for(request_id)
        if not p.exists():
            raise UnknownAttemptError(f"未预留的 request_id：{request_id}")
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc.update({"state": "UNKNOWN_SENT", "unknown_reason": reason,
                    "unknown_at_utc": _utc_now()})
        _atomic_write_json(p, doc)
        return doc


class AttemptJournal:
    """``attempts.jsonl``：每次逻辑调用的 pre/post 事件，append-only。"""

    def __init__(self, path: Path | None = None,
                 reservations: BudgetReservations | None = None) -> None:
        self.path = path or rsi_r0_attempts_path()
        self.reservations = reservations

    # ---- 写侧 ----

    def reserve(self, *, slot_id: str, batch_id: str, task_id: int, stage: str,
                tokens_in: int, tokens_out: int, usd: float,
                request_id: str | None = None) -> AttemptRecord:
        """原子占位 + 落一条 ``PREPARED`` 事件（顺序：预留先于发网，AC08）。"""
        rid = request_id or new_request_id()
        if self.reservations is not None:
            self.reservations.reserve(
                request_id=rid, slot_id=slot_id, batch_id=batch_id, task_id=task_id,
                stage=stage, tokens_in=tokens_in, tokens_out=tokens_out, usd=usd)
        rec = AttemptRecord(
            request_id=rid, slot_id=slot_id, batch_id=batch_id, task_id=task_id,
            stage=stage, state=AttemptState.PREPARED,
            reserved_usd=usd, reserved_tokens_in=tokens_in,
            reserved_tokens_out=tokens_out,
            created_at_utc=_utc_now(), updated_at_utc=_utc_now(),
        )
        _append_jsonl(self.path, rec.as_row())
        return rec

    def mark_sent(self, rec: AttemptRecord, *, seq: int | None) -> AttemptRecord:
        rec.state = AttemptState.SENT
        rec.seq = seq
        rec.attempts_http = 1
        rec.updated_at_utc = _utc_now()
        _append_jsonl(self.path, rec.as_row())
        return rec

    def settle_ok(self, rec: AttemptRecord, *, usage_in: int | None, usage_out: int | None,
                  stop_reason: str | None, block_types: tuple[str, ...],
                  provider_request_id: str | None, body_sha256: str | None,
                  usd: float | None) -> AttemptRecord:
        rec.state = AttemptState.SETTLED_OK
        rec.usage_in, rec.usage_out = usage_in, usage_out
        rec.stop_reason = stop_reason
        rec.content_block_types = tuple(block_types)
        rec.provider_request_id = provider_request_id
        rec.body_sha256 = body_sha256
        rec.updated_at_utc = _utc_now()
        _append_jsonl(self.path, rec.as_row())
        if self.reservations is not None:
            self.reservations.settle(rec.request_id, usage_in=usage_in,
                                     usage_out=usage_out, usd=usd)
        return rec

    def settle_fail(self, rec: AttemptRecord, *, error_class: str,
                    http_status: int | None = None,
                    body_sha256: str | None = None,
                    block_types: tuple[str, ...] = ()) -> AttemptRecord:
        rec.state = AttemptState.SETTLED_FAIL
        rec.error_class = error_class
        rec.http_status = http_status
        rec.body_sha256 = body_sha256
        rec.content_block_types = tuple(block_types)
        rec.updated_at_utc = _utc_now()
        _append_jsonl(self.path, rec.as_row())
        if self.reservations is not None:
            # 失败照样结算：预留转为实际占用（费用不可知时保留预留值，不伪造 0）。
            self.reservations.settle(rec.request_id, usage_in=None, usage_out=None,
                                     usd=None)
        return rec

    def block_preflight(self, rec: AttemptRecord, *, reason: str) -> AttemptRecord:
        """可证实发网前中止：HTTP 尝试计数保持 0，但 slot 仍算已消耗。"""
        rec.state = AttemptState.BLOCKED_PREFLIGHT
        rec.error_class = f"preflight_blocked:{reason}"[:300]
        rec.attempts_http = 0
        rec.updated_at_utc = _utc_now()
        _append_jsonl(self.path, rec.as_row())
        if self.reservations is not None:
            self.reservations.settle(rec.request_id, usage_in=0, usage_out=0, usd=0.0)
        return rec

    # ---- 读侧 ----

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def latest_by_request(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.events():
            latest[row["request_id"]] = row
        return latest

    def requests_for_slot(self, slot_id: str) -> list[dict[str, Any]]:
        return [r for r in self.latest_by_request().values() if r.get("slot_id") == slot_id]

    def recover(self) -> dict[str, Any]:
        """启动时恢复（AC09）：只读现场、只写**终态事件**，绝不重发。

        * 最后一条是 ``SENT`` ⇒ 已发网但无结算 ⇒ ``UNKNOWN_SENT``：占位、预留
          **不释放**、序号保留，且阻塞新付费批次（``assert_no_unknown``）。
        * 最后一条是 ``PREPARED`` ⇒ 崩溃发生在 ``mark_sent`` 之前。发网顺序是
          **先落 SENT（fsync）再调 transport**（:meth:`mark_sent` 的调用约定，
          runner 侧保证），所以 PREPARED 可证实「从未发出 HTTP」⇒ 按
          ``BLOCKED_PREFLIGHT`` 结算（HTTP 尝试 0、预留以 0 用结清），该 slot
          仍算已消耗。
        * 其它终态不再处理（幂等：重复调用不会二次记账）。
        """
        recovered: list[dict[str, Any]] = []
        latest = self.latest_by_request()
        for rid, row in sorted(latest.items()):
            state = row.get("state")
            if state == AttemptState.SENT.value:
                if self.reservations is not None:
                    self.reservations.mark_unknown(rid, "crash_between_send_and_settle")
                rec = AttemptRecord(
                    request_id=rid, slot_id=row.get("slot_id", ""),
                    batch_id=row.get("batch_id", ""), task_id=int(row.get("task_id") or 0),
                    stage=row.get("stage", ""), state=AttemptState.UNKNOWN_SENT,
                    seq=row.get("seq"), attempts_http=int(row.get("attempts_http") or 1),
                    reserved_usd=float(row.get("reserved_usd") or 0.0),
                    reserved_tokens_in=int(row.get("reserved_tokens_in") or 0),
                    reserved_tokens_out=int(row.get("reserved_tokens_out") or 0),
                    error_class="UNKNOWN_SENT:crash_between_send_and_settle",
                    created_at_utc=row.get("created_at_utc", ""),
                    updated_at_utc=_utc_now(),
                )
                _append_jsonl(self.path, rec.as_row())
                recovered.append({"request_id": rid, "from": state,
                                  "to": AttemptState.UNKNOWN_SENT.value,
                                  "reservation": "retained"})
            elif state == AttemptState.PREPARED.value:
                rec = AttemptRecord(
                    request_id=rid, slot_id=row.get("slot_id", ""),
                    batch_id=row.get("batch_id", ""), task_id=int(row.get("task_id") or 0),
                    stage=row.get("stage", ""), state=AttemptState.PREPARED,
                    seq=None, attempts_http=0,
                    reserved_usd=float(row.get("reserved_usd") or 0.0),
                    reserved_tokens_in=int(row.get("reserved_tokens_in") or 0),
                    reserved_tokens_out=int(row.get("reserved_tokens_out") or 0),
                    created_at_utc=row.get("created_at_utc", ""),
                    updated_at_utc=_utc_now(),
                )
                self.block_preflight(rec, reason="crashed_before_send")
                recovered.append({"request_id": rid, "from": state,
                                  "to": AttemptState.BLOCKED_PREFLIGHT.value,
                                  "reservation": "settled_zero"})
        return {"recovered": recovered, "count": len(recovered)}

    def assert_no_unknown(self) -> None:
        """付费批次开跑前的健康检查：存在 UNKNOWN 即拒绝（先人工核对）。"""
        latest = self.latest_by_request()
        unknown = [rid for rid, row in latest.items()
                   if row.get("state") == AttemptState.UNKNOWN_SENT.value]
        if unknown:
            raise RuntimeError(
                f"存在 {len(unknown)} 条 UNKNOWN_SENT 未核销，拒绝新付费批次：{unknown[:5]}")


def fingerprint_attempt_set(rows: list[dict[str, Any]]) -> str:
    """收尾对账用：按 request_id 排序的规范哈希（AC31）。"""
    ordered = sorted(rows, key=lambda r: r.get("request_id", ""))
    return sha256_of([{k: r.get(k) for k in (
        "request_id", "slot_id", "batch_id", "seq", "state", "attempts_http")}
        for r in ordered])


def canonical_attempt_line(row: Mapping[str, Any]) -> str:
    return canonical_json(row)
