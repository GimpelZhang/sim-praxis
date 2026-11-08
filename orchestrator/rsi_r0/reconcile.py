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
* ``event="preflight_blocked"`` 的账本行（seq=null）必须与一条 ``BLOCKED_PREFLIGHT``
  attempt 逐 ``(stage, task, batch_id)`` 配对（只比个数会把"A 批被拦、B 批记账"
  当成对得上）；
* 同一整数 seq 出现两条 attempt 即漂移（``duplicate_seq``；旧版 ``setdefault``
  静默丢弃第二条，seq 复用检不出来）；
* 每条 attempt 必须有同名预留文件；多余的预留文件也算漂移；
* 存在 ``UNKNOWN_SENT`` 即 ``ok=False``（有未核销的发网事实）；
* 报表里记过的整数 seq（``report_seqs``：协议/开发/验证三份报告各自写下
  ``requests.ledger_seqs``）必须能在账本里找到——这条是**防抹账**：旧版把账本、
  attempts、预留三份一起删掉时对账返回真（"未开跑"与"抹掉证据"不可区分），
  而报告里的 seq 是第四份独立痕迹。

``ok`` 只在**没有任何一处漂移**时为真；空账空账本（从未开跑、也没有任何报告）
为真——这一点如实写在这里，不假装三份全删也能检出。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
from orchestrator.rsi_r0.contracts import AttemptState

REPORT_SCHEMA = "rsi_r0.reconcile.v1"

_CONSUMED_STATES = frozenset({
    AttemptState.SETTLED_OK.value,
    AttemptState.SETTLED_FAIL.value,
    AttemptState.UNKNOWN_SENT.value,
})


def _ledger_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读账本；坏行**不抛**，作为 ``corrupt_records`` 漂移上报（第三份评审 LOW-3）。

    对账的契约是「永远返回一份报告」：坏行若以 ``JSONDecodeError`` 冒到
    ``gate_facts``，调用方拿到的是 traceback 而不是可读的门失败理由。
    """
    if not path.exists():
        return [], []
    out: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            corrupt.append({"source": "ledger", "line": index, "why": str(exc)})
    return out, corrupt


def reconcile(*, ledger_path: Path, journal: AttemptJournal,
              reservations: BudgetReservations | None = None,
              report_seqs: Sequence[tuple[str, Sequence[int]]] = ()) -> dict[str, Any]:
    """逐 request 对账；返回值可直接进产物（G-P 条件与收尾报告共用）。

    ``report_seqs`` 是「第四份痕迹」：三份权威文件都可能被删，报告里的
    ``requests.ledger_seqs`` 不会跟着消失（它已经写进 protocol/ 下的成品件）。
    """
    ledger, corrupt = _ledger_rows(ledger_path)
    call_seqs: dict[int, dict[str, Any]] = {}
    blocked_rows: list[dict[str, Any]] = []
    for row in ledger:
        seq = row.get("seq")
        if isinstance(seq, int) and row.get("event", "call") == "call":
            call_seqs[seq] = row
        elif seq is None and row.get("event") == "preflight_blocked":
            blocked_rows.append(row)

    try:
        latest = journal.latest_by_request()
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        # attempts 侧坏行同理：记成漂移，别让对账以异常形式失败。
        corrupt.append({"source": "attempts", "line": None, "why": str(exc)})
        latest = {}
    attempt_seqs: dict[int, str] = {}
    claimed_rows: dict[int, Mapping[str, Any]] = {}
    duplicate_seqs: list[dict[str, Any]] = []
    unknown: list[str] = []
    blocked_attempts: list[dict[str, Any]] = []
    uncounted: list[dict[str, Any]] = []

    def _claim(seq: int, rid: str, row: Mapping[str, Any] | None = None) -> None:
        if seq in attempt_seqs:
            duplicate_seqs.append({"seq": seq, "request_ids": [attempt_seqs[seq], rid]})
            return
        attempt_seqs[seq] = rid
        if row is not None:
            claimed_rows[seq] = row

    for rid, row in sorted(latest.items()):
        state = row.get("state")
        seq = row.get("seq")
        if state == AttemptState.UNKNOWN_SENT.value:
            unknown.append(rid)
        if state == AttemptState.BLOCKED_PREFLIGHT.value:
            # attempts.jsonl 的列名是 ``task_id``，账本里叫 ``task``（评审 N-2）：
            # 不归一化会让"同一个 (stage, task, batch)"永远配不上，稳定误报漂移。
            task = row.get("task", row.get("task_id"))
            blocked_attempts.append({"request_id": rid, "stage": row.get("stage"),
                                     "task": task,
                                     "batch_id": row.get("batch_id")})
            if isinstance(seq, int):
                _claim(seq, rid, row)
            continue
        if state not in _CONSUMED_STATES:
            # PREPARED/SENT 还没走完：属于进行中或崩溃未恢复，不算漂移，单独挂牌。
            uncounted.append({"request_id": rid, "state": state})
            continue
        if isinstance(seq, int):
            _claim(seq, rid, row)
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

    # blocked 行逐 (stage, task, batch_id) 配对：只比个数会漏掉"A 批被拦、B 批记账"。
    def _blocked_key(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return (row.get("stage"), row.get("task"), row.get("batch_id"))

    want = sorted((str(k) for k in map(_blocked_key, blocked_rows)))
    got = sorted((str(k) for k in map(_blocked_key, blocked_attempts)))
    blocked_mismatch: Any = False
    if want != got:
        blocked_mismatch = {"ledger_only": _multiset_minus(want, got),
                            "attempts_only": _multiset_minus(got, want)}

    # call 行只按 seq 与 attempt 对上还不够：账行的 (stage, task, batch) 与
    # attempt 的身份不符，说明「A 批的账被算到 B 批头上」——只比 seq 会让它过。
    # （第三份独立评审 2026-11-08 指出的未挂牌缺口。）
    def _identity(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        task = row.get("task", row.get("task_id"))
        return (row.get("stage"), task, row.get("batch_id"))

    identity_bad: list[dict[str, Any]] = []
    identity_unverified: list[int] = []
    for seq in sorted(set(call_seqs) & set(claimed_rows)):
        ledger_key, attempt_key = _identity(call_seqs[seq]), _identity(claimed_rows[seq])
        # 逐字段比：只比**双方都有值**的字段。账行缺 stage/task/batch（合成夹具或
        # 早期行）不能让对账变成假漂移；但整条都没值就单独挂牌——"核不了"与
        # "核过且一致"不是一回事。
        pairs = [(str(l), str(a)) for l, a in zip(ledger_key, attempt_key)
                 if l is not None and a is not None]
        if any(l != a for l, a in pairs):
            identity_bad.append({"seq": seq,
                                 "ledger": [str(x) for x in ledger_key],
                                 "attempts": [str(x) for x in attempt_key]})
        elif not pairs:
            identity_unverified.append(seq)

    reported: dict[int, str] = {}
    missing_reported: list[dict[str, Any]] = []
    for source, seqs in report_seqs:
        for seq in seqs:
            if not isinstance(seq, int):
                continue
            if seq not in call_seqs:
                missing_reported.append({"source": source, "seq": seq})
            reported.setdefault(seq, source)

    drift = {
        "ledger_seq_without_attempt": missing_attempts,
        "attempt_seq_without_ledger": missing_ledger,
        "duplicate_seq": duplicate_seqs,
        "call_identity_mismatch": identity_bad,
        "corrupt_records": corrupt,
        "attempt_without_reservation": missing_reservations,
        "reservation_without_attempt": orphan_reservations,
        "unknown_sent": unknown,
        "uncounted_attempts": uncounted,
        "blocked_preflight_mismatch": blocked_mismatch,
        "report_seq_without_ledger": missing_reported,
    }
    ok = not any(drift.values())
    return {
        "schema": REPORT_SCHEMA,
        "ok": ok,
        "files_present": {"ledger": ledger_path.exists(),
                          "attempts": journal.path.exists() if journal.path else None},
        "identity_unverified_seqs": identity_unverified,
        "ledger": {"calls": len(call_seqs), "preflight_blocked": len(blocked_rows),
                   "seqs": sorted(call_seqs)},
        "attempts": {"records": len(latest), "with_seq": len(attempt_seqs),
                     "blocked_preflight": len(blocked_attempts),
                     "settled": sum(1 for r in latest.values()
                                    if r.get("state") in _CONSUMED_STATES)},
        "reservations": {"present": reservations is not None,
                         "count": len(reservation_ids)},
        "reports": {"sources": [s for s, _ in report_seqs],
                    "seqs": sorted(reported)},
        "drift": drift,
        "why": "" if ok else "对账存在漂移：" + ", ".join(
            k for k, v in drift.items() if v),
    }


def _multiset_minus(left: Sequence[str], right: Sequence[str]) -> list[str]:
    """``left - right`` 的多重集差（元素是 repr 过的键，便于直接进 JSON）。"""
    rest = list(right)
    out: list[str] = []
    for item in left:
        if item in rest:
            rest.remove(item)
        else:
            out.append(item)
    return out


def reconcile_campaign(ledger_path: Path, journal: AttemptJournal,
                       reservations: BudgetReservations | None = None,
                       report_seqs: Sequence[tuple[str, Sequence[int]]] = ()
                       ) -> tuple[bool, dict[str, Any]]:
    """``(ok, detail)`` 便捷包装，给门事实与收尾共用。"""
    detail = reconcile(ledger_path=ledger_path, journal=journal,
                       reservations=reservations, report_seqs=report_seqs)
    return bool(detail.get("ok")), detail
