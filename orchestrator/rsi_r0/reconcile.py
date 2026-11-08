"""AC31 对账：账本 / attempts / 预留 三方逐 `request_id` 互证。

三份权威互相独立（计划 §6.3）：账本记「谁花了第几个 seq」，attempts 记「这次逻辑
调用到底有没有 POST 出去」，预留记「先占后花」。任何一方单独可删，就不能当唯一
事实源——旧版只把 attempts 当事实源，删掉 ``quota/attempts.jsonl`` 就能让
``no_unknown_attempts`` 与 supersede 守卫同时失效（评审 MAJOR-5）。

本模块只读三份文件并给出对账结论，不写任何东西。判定口径：

* 账本 ``event="call"`` 行的整数 ``seq`` 是**权威序号**；
* 每个整数 seq 必须恰有**一条** attempt 记录与之对应（``SETTLED_OK``/``SETTLED_FAIL``
  或 ``UNKNOWN_SENT``，后者保留 seq 属于未核销而不是漂移）；
* 每条带整数 seq 的 attempt 必须能在账本里找到同一 seq；
* ``event="preflight_blocked"`` 的账本行（seq=null）必须对应一条
  ``BLOCKED_PREFLIGHT`` attempt（可证实未发网），反向亦然；
* 每条 attempt 必须有同名预留文件；多余的预留文件也算漂移；
* 存在 ``UNKNOWN_SENT`` 即 ``ok=False``（有未核销的发网事实）。

``ok`` 只在**没有任何一处漂移**时为真；空账空账本（未开跑）为真。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
from orchestrator.rsi_r0.contracts import AttemptState

REPORT_SCHEMA = "rsi_r0.reconcile.v1"

_CONSUMED_STATES = frozenset({
    AttemptState.SETTLED_OK.value,
    AttemptState.SETTLED_FAIL.value,
    AttemptState.UNKNOWN_SENT.value,
})


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def reconcile(*, ledger_path: Path, journal: AttemptJournal,
              reservations: BudgetReservations | None = None) -> dict[str, Any]:
    """逐 request 对账；返回值可直接进产物（G-P 条件与收尾报告共用）。"""
    ledger = _ledger_rows(ledger_path)
    call_seqs: dict[int, dict[str, Any]] = {}
    blocked_rows: list[dict[str, Any]] = []
    for row in ledger:
        seq = row.get("seq")
        if isinstance(seq, int) and row.get("event", "call") == "call":
            call_seqs[seq] = row
        elif seq is None and row.get("event") == "preflight_blocked":
            blocked_rows.append(row)

    latest = journal.latest_by_request()
    attempt_seqs: dict[int, str] = {}
    unknown: list[str] = []
    blocked_attempts: list[str] = []
    uncounted: list[dict[str, Any]] = []
    for rid, row in sorted(latest.items()):
        state = row.get("state")
        seq = row.get("seq")
        if state == AttemptState.UNKNOWN_SENT.value:
            unknown.append(rid)
        if state == AttemptState.BLOCKED_PREFLIGHT.value:
            blocked_attempts.append(rid)
            if isinstance(seq, int):
                attempt_seqs.setdefault(seq, rid)
            continue
        if state not in _CONSUMED_STATES:
            # PREPARED/SENT 还没走完：属于进行中或崩溃未恢复，不算漂移，单独挂牌。
            uncounted.append({"request_id": rid, "state": state})
            continue
        if isinstance(seq, int):
            attempt_seqs.setdefault(seq, rid)
        else:
            # 已结算却没有 seq：只有 BLOCKED_PREFLIGHT 允许，其它一律算漂移。
            uncounted.append({"request_id": rid, "state": state, "why": "settled_without_seq"})

    missing_attempts = sorted(set(call_seqs) - set(attempt_seqs))
    missing_ledger = sorted(set(attempt_seqs) - set(call_seqs))

    reservation_ids: set[str] = set()
    if reservations is not None:
        reservation_ids = set(reservations.load_all())
    missing_reservations = sorted(set(latest) - reservation_ids)
    orphan_reservations = sorted(reservation_ids - set(latest))

    blocked_mismatch = (len(blocked_rows) != len(blocked_attempts)) if (blocked_rows or blocked_attempts) else False

    drift = {
        "ledger_seq_without_attempt": missing_attempts,
        "attempt_seq_without_ledger": missing_ledger,
        "attempt_without_reservation": missing_reservations,
        "reservation_without_attempt": orphan_reservations,
        "unknown_sent": unknown,
        "uncounted_attempts": uncounted,
        "blocked_preflight_mismatch": blocked_mismatch,
    }
    ok = not any(drift.values())
    return {
        "schema": REPORT_SCHEMA,
        "ok": ok,
        "ledger": {"calls": len(call_seqs), "preflight_blocked": len(blocked_rows),
                   "seqs": sorted(call_seqs)},
        "attempts": {"records": len(latest), "with_seq": len(attempt_seqs),
                     "blocked_preflight": len(blocked_attempts),
                     "settled": sum(1 for r in latest.values()
                                    if r.get("state") in _CONSUMED_STATES)},
        "reservations": {"present": reservations is not None,
                         "count": len(reservation_ids)},
        "drift": drift,
        "why": "" if ok else "对账存在漂移：" + ", ".join(
            k for k, v in drift.items() if v),
    }


def reconcile_campaign(ledger_path: Path, journal: AttemptJournal,
                       reservations: BudgetReservations | None = None
                       ) -> tuple[bool, dict[str, Any]]:
    """``(ok, detail)`` 便捷包装，给门事实与收尾共用。"""
    detail = reconcile(ledger_path=ledger_path, journal=journal,
                       reservations=reservations)
    return bool(detail.get("ok")), detail
