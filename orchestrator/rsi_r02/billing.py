"""RSI-R0.2 计费账：原子预留、结算与未知费用语义（计划 §4.2/§4.3，AC13/AC14）。

合同形态（用户 2026-11-09 逐字豁免 G-S 后确立）：

* 单请求按 ``RESERVATION_USD`` 预留；**发送前**在任何 POST 之前把预留原子落盘；
* 任一时刻必须满足 ``settled + pending + reservation ≤ cap``，否则 ``BLOCKED_BUDGET``；
* 结算只在拿到供应侧 usage 后释放差额；**usage 缺失/不可解析 ⇒ UNKNOWN**，
  预留不释放、后续全局暂停（不把未知当 0，也不凭估计释放）；
* 成本三态：EXACT / BOUNDED_UPPER / UNKNOWN。缓存字段的单价无供应侧依据，
  一律按**最高已知单价**（output）上界计，故含缓存 token 的成本只到 BOUNDED_UPPER。

本地账不是供应端账单；这里证明的是「实现遵守自己声明的上界」，不是「供应方保证」。
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r02 import campaign as C

USD_CAP = 100.0
RESERVATION_USD = 1.5
PRICE_IN_PER_1M = 1.0
PRICE_OUT_PER_1M = 4.0

COST_EXACT = "EXACT"
COST_BOUNDED_UPPER = "BOUNDED_UPPER"
COST_UNKNOWN = "UNKNOWN"

STATUS_PENDING = "PENDING"
STATUS_SETTLED = "SETTLED"
STATUS_UNKNOWN = "UNKNOWN"

LOCK_NAME = ".billing.lock"
SUMMARY_SCHEMA = "simpraxis.rsi_r02.billing.v1"


class BillingError(RuntimeError):
    """账务合同被违反（重复预留、未知 call_id、负金额等）。"""


class BudgetBlocked(BillingError):
    """触帽：``settled + pending + 预留 > cap``，拒绝发送。"""


class BillingPaused(BillingError):
    """存在 UNKNOWN 费用/发送：在 resolution 之前全局暂停，不发送任何新请求。"""


@dataclass(frozen=True)
class BillingPolicy:
    cap_usd: float = USD_CAP
    reservation_usd: float = RESERVATION_USD
    price_in_per_1m: float = PRICE_IN_PER_1M
    price_out_per_1m: float = PRICE_OUT_PER_1M
    price_basis: str = "rsi_r0/protocol/pricing_contract.json#conservative_ceiling"

    def as_dict(self) -> dict[str, Any]:
        return {"cap_usd": self.cap_usd, "reservation_usd": self.reservation_usd,
                "price_in_per_1m": self.price_in_per_1m,
                "price_out_per_1m": self.price_out_per_1m,
                "price_basis": self.price_basis,
                "claimed_as_supplier_guarantee": False}


# ---------------------------------------------------------------------------
# usage 归一化与成本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """原始字段 + 规范化字段分开存；缺字段是 None，不是 0。"""

    raw: Mapping[str, Any] | None
    in_tokens: int | None
    out_tokens: int | None
    cache_read: int | None
    cache_create: int | None
    prompt_total: int | None
    prompt_total_basis: str | None
    unknown_fields: tuple[str, ...]

    @classmethod
    def normalize(cls, raw: Mapping[str, Any] | None) -> "Usage":
        if not isinstance(raw, Mapping):
            return cls(raw=None, in_tokens=None, out_tokens=None, cache_read=None,
                       cache_create=None, prompt_total=None,
                       prompt_total_basis=None,
                       unknown_fields=("usage",))
        unknown: list[str] = []
        def num(*names: str) -> int | None:
            for name in names:
                if name in raw and raw[name] is not None:
                    val = raw[name]
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                        unknown.append(name)
                        return None
                    return int(val)
            unknown.append(names[0])
            return None
        in_tok = num("input_tokens", "in", "prompt_tokens")
        out_tok = num("output_tokens", "out", "completion_tokens")
        cache_read = num("cache_read_input_tokens", "cache_read")
        cache_create = num("cache_creation_input_tokens", "cache_create")
        # 只有供应语义证明互斥（输入不含缓存）才可加和；本阶段无该证明 ⇒ 恒 None。
        prompt_total = None
        basis = None
        if in_tok is not None and cache_read == 0 and cache_create == 0:
            prompt_total = in_tok
            basis = "exclusive_proven_by_zero_cache"
        return cls(raw=dict(raw), in_tokens=in_tok, out_tokens=out_tok,
                   cache_read=cache_read, cache_create=cache_create,
                   prompt_total=prompt_total, prompt_total_basis=basis,
                   unknown_fields=tuple(sorted(set(unknown))))

    def as_dict(self) -> dict[str, Any]:
        return {"raw": dict(self.raw) if self.raw is not None else None,
                "normalized": {"input_tokens": self.in_tokens,
                               "output_tokens": self.out_tokens,
                               "cache_read": self.cache_read,
                               "cache_create": self.cache_create,
                               "prompt_total": self.prompt_total,
                               "prompt_total_basis": self.prompt_total_basis},
                "unknown_fields": list(self.unknown_fields),
                "note": "缺字段未知不当 0；prompt_total 仅在缓存字段为 0 时给出"}


@dataclass(frozen=True)
class Cost:
    usd: float | None           # 已知/上界金额（UNKNOWN 时为 None）
    upper_usd: float | None     # 保守上界（UNKNOWN 时也给不出 ⇒ None）
    status: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"usd": self.usd, "upper_usd": self.upper_usd,
                "status": self.status, "detail": dict(self.detail)}


def cost_of(usage: Usage, policy: BillingPolicy = BillingPolicy()) -> Cost:
    """按已声明单价算成本；缓存 token 无供应单价 ⇒ 按最高单价上界计。

    * in/out 任一缺失 ⇒ UNKNOWN（不给 0，也不猜）；
    * 缓存两字段都给出且为 0 ⇒ EXACT；
    * 缓存字段缺失 ⇒ BOUNDED_UPPER：按「全部输入都可能是缓存 token 且按最高
      单价计」取保守上界（缓存 token 是 prompt 的子集，故上界成立）；
    * 缓存字段给出且 >0 ⇒ BOUNDED_UPPER：按缓存 token 数 × 最高单价。
    """
    if usage.in_tokens is None or usage.out_tokens is None:
        return Cost(usd=None, upper_usd=None, status=COST_UNKNOWN,
                    detail={"reason": "missing_in_or_out",
                            "unknown_fields": list(usage.unknown_fields)})
    known = (usage.in_tokens * policy.price_in_per_1m
             + usage.out_tokens * policy.price_out_per_1m) / 1e6
    if usage.cache_read is None or usage.cache_create is None:
        upper = known + usage.in_tokens * policy.price_out_per_1m / 1e6
        return Cost(usd=upper, upper_usd=upper, status=COST_BOUNDED_UPPER,
                    detail={"reason": "cache_fields_absent_bounded_by_input",
                            "assumed_cache_tokens": usage.in_tokens})
    cache_tokens = usage.cache_read + usage.cache_create
    if cache_tokens:
        upper = known + cache_tokens * policy.price_out_per_1m / 1e6
        return Cost(usd=upper, upper_usd=upper, status=COST_BOUNDED_UPPER,
                    detail={"reason": "cache_priced_at_max_unit",
                            "cache_tokens": cache_tokens})
    return Cost(usd=known, upper_usd=known, status=COST_EXACT, detail={})


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    call_id: str
    slot_id: str
    reserved_usd: float
    created_at_utc: str
    schema: str = "simpraxis.rsi_r02.reservation.v1"

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "Reservation":
        return cls(call_id=str(doc["call_id"]), slot_id=str(doc["slot_id"]),
                   reserved_usd=float(doc["reserved_usd"]),
                   created_at_utc=str(doc["created_at_utc"]))


@dataclass(frozen=True)
class Record:
    call_id: str
    slot_id: str
    reserved_usd: float
    status: str
    created_at_utc: str
    settled_at_utc: str | None = None
    settled_usd: float | None = None
    cost_status: str | None = None
    usage: Usage | None = None
    failure: str | None = None

    @property
    def outstanding_usd(self) -> float:
        return self.reserved_usd if self.status in (STATUS_PENDING, STATUS_UNKNOWN) else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"schema": "simpraxis.rsi_r02.reservation.v1",
                "call_id": self.call_id, "slot_id": self.slot_id,
                "reserved_usd": self.reserved_usd, "status": self.status,
                "created_at_utc": self.created_at_utc,
                "settled_at_utc": self.settled_at_utc,
                "settled_usd": self.settled_usd,
                "cost_status": self.cost_status,
                "usage": self.usage.as_dict() if self.usage is not None else None,
                "failure": self.failure}


class BillingBook:
    """campaign 根内的一本账：预留目录 + 暂停件；全部写走 campaign 写守卫。"""

    def __init__(self, ctx: C.CampaignContext, *,
                 policy: BillingPolicy = BillingPolicy(),
                 now: Callable[[], str] = utc_now_iso_z) -> None:
        self.ctx = ctx
        self.policy = policy
        self._now = now
        self.dir = ctx.subdir("quota") / "reservations"
        self.pause_path = ctx.subdir("quota") / "pause.json"

    # -- 锁与读写 ---------------------------------------------------------
    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.ctx.subdir("quota") / LOCK_NAME
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _path(self, call_id: str) -> Path:
        if not call_id or "/" in call_id or call_id.startswith("."):
            raise BillingError(f"非法 call_id：{call_id!r}")
        return self.dir / f"{call_id}.json"

    def record(self, call_id: str) -> Record:
        path = self._path(call_id)
        if not path.is_file():
            raise BillingError(f"没有预留记录：{call_id}")
        return self._to_record(C.read_json(path))

    def records(self) -> list[Record]:
        if not self.dir.is_dir():
            return []
        out = [self._to_record(C.read_json(p)) for p in sorted(self.dir.glob("*.json"))]
        return sorted(out, key=lambda r: (r.created_at_utc, r.call_id))

    @staticmethod
    def _to_record(doc: Mapping[str, Any]) -> Record:
        raw_usage = doc.get("usage") or {}
        usage = None
        if doc.get("usage") is not None:
            usage = Usage(raw=raw_usage.get("raw"),
                          in_tokens=(raw_usage.get("normalized") or {}).get("input_tokens"),
                          out_tokens=(raw_usage.get("normalized") or {}).get("output_tokens"),
                          cache_read=(raw_usage.get("normalized") or {}).get("cache_read"),
                          cache_create=(raw_usage.get("normalized") or {}).get("cache_create"),
                          prompt_total=(raw_usage.get("normalized") or {}).get("prompt_total"),
                          prompt_total_basis=(raw_usage.get("normalized") or {}).get("prompt_total_basis"),
                          unknown_fields=tuple(raw_usage.get("unknown_fields") or ()))
        return Record(call_id=str(doc["call_id"]), slot_id=str(doc["slot_id"]),
                      reserved_usd=float(doc["reserved_usd"]),
                      status=str(doc["status"]),
                      created_at_utc=str(doc["created_at_utc"]),
                      settled_at_utc=doc.get("settled_at_utc"),
                      settled_usd=doc.get("settled_usd"),
                      cost_status=doc.get("cost_status"), usage=usage,
                      failure=doc.get("failure"))

    def _write(self, rec: Record) -> None:
        C.write_json(self.ctx, f"quota/reservations/{rec.call_id}.json", rec.as_dict())

    # -- 汇总与闸 ---------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        recs = self.records()
        settled = sum(float(r.settled_usd or 0.0) for r in recs if r.status == STATUS_SETTLED)
        pending = sum(r.outstanding_usd for r in recs)
        unknown = [r.call_id for r in recs if r.status == STATUS_UNKNOWN
                   or (r.status == STATUS_SETTLED and r.cost_status == COST_UNKNOWN)]
        return {"schema": SUMMARY_SCHEMA, "policy": self.policy.as_dict(),
                "n_records": len(recs), "settled_usd": round(settled, 6),
                "pending_usd": round(pending, 6),
                "headroom_usd": round(self.policy.cap_usd
                                      - settled - pending - self.policy.reservation_usd, 6),
                "unknown_call_ids": unknown,
                "paused": self.pause_reason() is not None,
                "settled_plus_pending_usd": round(settled + pending, 6)}

    def _assert_can_reserve(self, settled: float, pending: float) -> None:
        need = settled + pending + self.policy.reservation_usd
        if need > self.policy.cap_usd + 1e-9:
            raise BudgetBlocked(
                f"触帽：settled={settled:.6f} + pending={pending:.6f} + "
                f"预留={self.policy.reservation_usd:.2f} = {need:.6f} > "
                f"cap={self.policy.cap_usd:.2f}（BLOCKED_BUDGET，不降低样本、不抬帽）")

    # -- 预留 -------------------------------------------------------------
    def reserve(self, *, call_id: str, slot_id: str) -> Record:
        """POST 之前调用：原子落盘预留；已有 UNKNOWN/暂停则拒绝发送。"""
        with self._lock():
            reason = self._pause_reason_unlocked()
            if reason is not None:
                raise BillingPaused(f"全局暂停中（{reason}）：resolution 前不发送")
            path = self._path(call_id)
            if path.exists():
                raise BillingError(f"重复预留（同一逻辑调用只许一次 POST）：{call_id}")
            recs = self.records()
            if any(r.call_id == call_id for r in recs):
                raise BillingError(f"重复预留：{call_id}")
            if any(r.slot_id == slot_id and r.status in (STATUS_PENDING, STATUS_UNKNOWN)
                   for r in recs):
                raise BillingError(f"槽位已有未结算预留：{slot_id}")
            settled = sum(float(r.settled_usd or 0.0) for r in recs
                          if r.status == STATUS_SETTLED)
            pending = sum(r.outstanding_usd for r in recs)
            self._assert_can_reserve(settled, pending)
            rec = Record(call_id=call_id, slot_id=slot_id,
                         reserved_usd=self.policy.reservation_usd,
                         status=STATUS_PENDING, created_at_utc=self._now())
            self._write(rec)
            return rec

    # -- 结算 / 未知 -------------------------------------------------------
    def settle(self, *, call_id: str, usage: Usage) -> Record:
        """拿到供应侧 usage 后结算：EXACT/BOUNDED_UPPER 释放差额；UNKNOWN 保持预留。"""
        cost = cost_of(usage, self.policy)
        with self._lock():
            rec = self.record(call_id)
            if rec.status != STATUS_PENDING:
                raise BillingError(f"状态 {rec.status} 不可结算：{call_id}")
            if cost.status == COST_UNKNOWN:
                new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                             reserved_usd=rec.reserved_usd, status=STATUS_UNKNOWN,
                             created_at_utc=rec.created_at_utc,
                             settled_at_utc=self._now(), settled_usd=None,
                             cost_status=COST_UNKNOWN, usage=usage,
                             failure="usage_unparseable_or_missing")
                self._write(new)
                self._pause_unlocked(f"费用未知 call_id={call_id}", call_id)
                return new
            amount = float(cost.usd or 0.0)
            if amount > rec.reserved_usd + 1e-9:
                # 上界被突破：不得静默补记；保留预留并暂停，等人工 resolution。
                new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                             reserved_usd=rec.reserved_usd, status=STATUS_UNKNOWN,
                             created_at_utc=rec.created_at_utc,
                             settled_at_utc=self._now(), settled_usd=None,
                             cost_status=COST_BOUNDED_UPPER, usage=usage,
                             failure=f"cost_exceeds_reservation:{amount:.6f}")
                self._write(new)
                self._pause_unlocked(
                    f"单请求成本 {amount:.6f} 超出预留 {rec.reserved_usd:.2f} "
                    f"call_id={call_id}", call_id)
                return new
            rec_docs = self.records()
            settled = sum(float(r.settled_usd or 0.0) for r in rec_docs
                          if r.status == STATUS_SETTLED)
            if settled + amount > self.policy.cap_usd + 1e-9:
                new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                             reserved_usd=rec.reserved_usd, status=STATUS_UNKNOWN,
                             created_at_utc=rec.created_at_utc,
                             settled_at_utc=self._now(), settled_usd=None,
                             cost_status=cost.status, usage=usage,
                             failure=f"settlement_exceeds_cap:{amount:.6f}")
                self._write(new)
                self._pause_unlocked(f"结算触帽 call_id={call_id}", call_id)
                return new
            new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                         reserved_usd=rec.reserved_usd, status=STATUS_SETTLED,
                         created_at_utc=rec.created_at_utc,
                         settled_at_utc=self._now(), settled_usd=amount,
                         cost_status=cost.status, usage=usage)
            self._write(new)
            return new

    def release(self, *, call_id: str, reason: str) -> Record:
        """发网前阻断（零 POST / 零费用）：释放预留并按 $0 结清，绝不记 UNKNOWN。

        计划 §7「发网前阻断单列零POST」：网关在 preflight 就拒绝时本次调用
        一个字节都没发出——响应不存在、费用为零。把它记成 UNKNOWN 会同时污染
        费用三态（预留长期挂着）与全局暂停（迫使后续全部 trial 停机）。故单列
        本入口，只允许 ``PENDING → SETTLED(0.0/EXACT)``，原因逐字保留在
        ``failure`` 供结案机判重算。
        """
        if not str(reason or "").strip():
            raise BillingError("释放预留必须写明原因（发网前阻断的 reason_code）")
        with self._lock():
            rec = self.record(call_id)
            if rec.status != STATUS_PENDING:
                raise BillingError(f"状态 {rec.status} 不可按零 POST 释放：{call_id}")
            new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                         reserved_usd=rec.reserved_usd, status=STATUS_SETTLED,
                         created_at_utc=rec.created_at_utc,
                         settled_at_utc=self._now(), settled_usd=0.0,
                         cost_status=COST_EXACT, usage=None,
                         failure=f"PREFLIGHT_BLOCKED: {str(reason)[:800]}")
            self._write(new)
            return new

    def mark_unknown(self, *, call_id: str, failure: str) -> Record:
        """发送状态不明：占槽、保留预留、全局暂停（不重发）。"""
        with self._lock():
            rec = self.record(call_id)
            if rec.status != STATUS_PENDING:
                raise BillingError(f"状态 {rec.status} 不可标记未知：{call_id}")
            new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                         reserved_usd=rec.reserved_usd, status=STATUS_UNKNOWN,
                         created_at_utc=rec.created_at_utc,
                         settled_at_utc=self._now(), settled_usd=None,
                         cost_status=COST_UNKNOWN, usage=None, failure=failure)
            self._write(new)
            self._pause_unlocked(f"UNKNOWN_SENT call_id={call_id}: {failure}", call_id)
            return new

    def resolve(self, *, call_id: str, resolution: Mapping[str, Any],
                settled_usd: float | None, cost_status: str) -> Record:
        """外部可核验证据的 resolution：只改写该记录，解除暂停需人工/证据双证。"""
        with self._lock():
            rec = self.record(call_id)
            if rec.status != STATUS_UNKNOWN:
                raise BillingError(f"只有 UNKNOWN 记录可 resolution：{call_id}")
            new = Record(call_id=rec.call_id, slot_id=rec.slot_id,
                         reserved_usd=rec.reserved_usd, status=STATUS_SETTLED,
                         created_at_utc=rec.created_at_utc,
                         settled_at_utc=self._now(), settled_usd=settled_usd,
                         cost_status=cost_status, usage=rec.usage,
                         failure=json.dumps(dict(resolution), ensure_ascii=False,
                                            sort_keys=True)[:1000])
            self._write(new)
            return new

    # -- 暂停 -------------------------------------------------------------
    def pause_reason(self) -> str | None:
        if not self.pause_path.is_file():
            return None
        try:
            doc = C.read_json(self.pause_path)
        except (OSError, ValueError):
            return "pause_state_corrupt"
        if doc.get("active"):
            return str(doc.get("reason") or "unknown")
        return None

    def _pause_reason_unlocked(self) -> str | None:
        return self.pause_reason()

    def _pause_unlocked(self, reason: str, call_id: str) -> None:
        C.write_json(self.ctx, "quota/pause.json",
                     {"schema": "simpraxis.rsi_r02.pause.v1", "active": True,
                      "reason": reason, "call_id": call_id,
                      "since_utc": self._now()})

    def clear_pause(self, *, resolution: str) -> None:
        """解除全局暂停：必须给证据说明（写进 pause 历史而不是删掉）。"""
        if not resolution.strip():
            raise BillingError("解除暂停必须给出可核验 resolution 说明")
        with self._lock():
            C.write_json(self.ctx, "quota/pause.json",
                         {"schema": "simpraxis.rsi_r02.pause.v1", "active": False,
                          "reason": "cleared", "resolution": resolution,
                          "cleared_at_utc": self._now()})
