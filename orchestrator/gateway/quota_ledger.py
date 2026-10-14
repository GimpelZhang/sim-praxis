#!/usr/bin/env python3
"""Phase 1 批次配额账本 + 双层计数闸门（详细计划 §0.2/§6，D1/D3 签收）。

本模块【不含任何网络代码】：只做批次凭据校验、ledger 记账与预算闸门；
HTTP 分类/退避仍在 router.py，m9/m12 逻辑零改动（§6.3）。

路径（全部 /mnt，仓外）：
  $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl     # 每行一次实呼或一次 preflight 拦截
  $SIMPRAXIS_PHASE1_ROOT/quota/counters.json    # 连续配额错误计数
  $SIMPRAXIS_PHASE1_ROOT/quota/approvals/<batch_id>.json   # 每批书面批准凭据
  $SIMPRAXIS_PHASE1_ROOT/quota/resume_audit.jsonl          # 人工恢复流水
暂停态文件不在这里：沿用 router.py 的 $SIMPRAXIS_GATEWAY_STATE/quota_pause.json，
is_paused() 直接复用 router 实现（不复制其逻辑）。预算类暂停 resume_at_epoch=null
（无票不自动恢复），在既有 7 键上追加 schema_version=2 的超集键（§6.1）。

接线状态（Task3 已收口，§6.3「倾向并入 Task3」）：Task2 阶段账本先行，
direction_setter 显式 preflight/record；Task3 起同一对门只在
ModelGateway.call/call_visual 内部各执行一次（BillingGate），setter 的显式
记账已移除，杜绝双计数。一次逻辑调用 = 一次 preflight + 至多一行整数 seq
（出口成功/失败都记），call 内部 HTTP 退避重试不重复记账。

口径签收（2026-10-14 代码评审 L1/L4）：
- resume_ticket：§6.1 设想双方把持（人工写票、操作者持票解封）；Phase 1 单机
  单人操作，当前 resume CLI 只校验票非空并记入 resume_audit.jsonl（票仅留痕、
  不与暂停文件中的值比对）。多机/多人运行前必须升级为双向校验。
- 并发：preflight 与 record 分持文件锁（TOCTOU 窗口）。Phase 1 形态是单机
  顺序 CLI；Task3 收口后唯一触网线程是 LangGraph invoke 主线程（飞书 WS 回调
  线程只写 pending_gates + set Event，不调模型），保持单调用方语义，接受该窗口；
  出现并发 runner 前必须在 record() 内复检上限或把 preflight+record 收进同一把锁。
- tokens_in/out：Task3 收口后由 call/call_visual 从 usage 回填
  （litellm input/output_tokens，直 POST body.usage.input_tokens/output_tokens）；
  Task2 的 9 行历史记录维持 null 不回改。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.gateway.router import (
    GatewayPausedError,
    _state_dir as router_state_dir,  # 锁内适配：复用同一暂停文件目录
    _state_path as router_state_path,
    is_paused,
    redact,
)

NORMAL_BUDGET = 36       # 常态批准总量（各任务「批准预算」列之和）
HARD_CAP = 48            # 全局先到先停硬门
# 各任务局部硬上限（§0.2；只表示局部可申请边界，全局 48 才是总闸）
# 锁内适配（计划 §0.2 D1 偏离，用户书面签收 2026-10-14）：Task2 硬上限 10→14。
# 原因：T2-B1 的 8 次中 6 次耗于首发环境缺陷（litellm/py3.10 shim、thinking 块
# 吃光输出预算、expected_artifacts 数组形态，均取证修复并回归）；track2 Q1 与
# Gate1 打回余量在 10 以内无法完成。全局 36/48 双门不变；4/5/7/8/9 各列不动。
TASK_HARD_CAPS = {2: 14, 4: 8, 5: 6, 7: 14, 8: 14, 9: 4}
CONSECUTIVE_QUOTA_LIMIT = 3
LOCK_NAME = ".ledger.lock"


class QuotaError(RuntimeError):
    """账本/凭据自身错误（区别于 GatewayPausedError 的预算熔断）。"""


def phase1_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                               "/mnt/artifacts/simpraxis/phase1"))


def quota_dir() -> Path:
    d = phase1_root() / "quota"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path() -> Path:
    return quota_dir() / "ledger.jsonl"


def counters_path() -> Path:
    return quota_dir() / "counters.json"


def approvals_dir() -> Path:
    d = quota_dir() / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


class _FileLock:
    """ledger 跨线程/跨进程互斥（fcntl 建议锁；单机单盘 /mnt）。"""

    def __init__(self) -> None:
        self._fh = open(quota_dir() / LOCK_NAME, "a+")  # noqa: SIM115

    def __enter__(self) -> "_FileLock":
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def read_lines() -> list[dict[str, Any]]:
    p = ledger_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # 账本只追加、逐行 json；坏行必须显式暴露，不许静默跳过
            raise QuotaError(f"ledger 存在不可解析行：{line[:120]}")
    return rows


def call_lines(rows: "list[dict[str, Any]] | None" = None) -> list[dict[str, Any]]:
    """真正触网的实呼行（seq 为整数）；preflight 拦截行 seq=null 不计数。"""
    return [r for r in (rows if rows is not None else read_lines())
            if isinstance(r.get("seq"), int)]


def load_approval(batch_id: str) -> dict[str, Any]:
    p = approvals_dir() / f"{batch_id}.json"
    if not p.exists():
        raise QuotaError(f"批次批准凭据不存在：{p}（实呼前必须有书面批次批准）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 评审 L3：凭据损坏按"凭据缺失"fail-closed，且让 preflight 的审计行不致断链
        raise QuotaError(f"批次批准凭据损坏不可读：{p}（{type(exc).__name__}）") from exc
    if not isinstance(data, dict):
        raise QuotaError(f"批次凭据 {batch_id} 顶层不是 JSON 对象")
    for k in ("batch_id", "task", "stage", "approved", "beyond_normal"):
        if k not in data:
            raise QuotaError(f"批次凭据 {batch_id} 缺字段 {k}")
    if data["batch_id"] != batch_id:
        raise QuotaError(f"批次凭据 batch_id 不一致：{data['batch_id']} != {batch_id}")
    if not isinstance(data["approved"], int) or data["approved"] <= 0:
        raise QuotaError(f"批次凭据 {batch_id} approved 必须为正整数")
    return data


def _write_budget_pause(reason: str, trigger: dict[str, Any]) -> dict[str, Any]:
    """写 v2 超集暂停态：前 7 键名/原义与 router._write_pause_state 完全一致，
    resume_at_epoch=null 表示不自动恢复（§6.1）。"""
    now = time.time()
    state = {
        "schema_version": 2,
        "status": "PAUSED_WAITING_QUOTA",
        "stage": "*",
        "reason": reason,
        "paused_at_epoch": now,
        "paused_at_iso": _now_iso(),
        "resume_at_epoch": None,
        "resume_at_iso": None,
        "scope": "global",
        "trigger": trigger,
        "resume_ticket": None,
        "affected_stages": ["*"],
    }
    d = router_state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = router_state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(router_state_path())
    os.chmod(router_state_path(), 0o600)
    return state


def _append_line(row: dict[str, Any]) -> None:
    p = ledger_path()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(p, 0o600)


def _read_counters() -> dict[str, Any]:
    p = counters_path()
    if not p.exists():
        return {"consecutive_quota_errors": 0}
    return json.loads(p.read_text(encoding="utf-8"))


def _write_counters(c: dict[str, Any]) -> None:
    c["updated_at_iso"] = _now_iso()
    tmp = counters_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(counters_path())
    os.chmod(counters_path(), 0o600)


def _is_quota_err(err: "str | None") -> bool:
    if not err:
        return False
    text = err.lower()
    return any(k in text for k in ("quota", "balance", "billing", "欠费", "余额",
                                  "insufficient", "resource exhausted"))


class QuotaLedger:
    """批次闸门 + 记账（方向 setter/runner 在每次实呼前后各调一次）。"""

    def __init__(self) -> None:
        self.quota_dir = quota_dir()

    # ---------- 调用前：不发网拦截（命中另写一行 ok=false err=preflight_blocked） ----------

    def preflight(self, stage: str, *, task: int, batch_id: str) -> dict[str, Any]:
        with _FileLock():
            # 1) router 既有暂停态（m9 长熔断/人工暂停）——复用 is_paused，不复制逻辑
            paused = is_paused(stage)
            if paused:
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: paused:{paused.get('reason')}")
                raise GatewayPausedError(paused)

            # 2) 批次书面批准凭据必须存在。
            # 锁内适配（计划 §6.2）：凭据缺失/不匹配不属于 §6.2 列举的持久熔断原因，
            # 只做当次不发网拦截 + 审计行（内存态、不落 quota_pause.json）；
            # 凭据补齐后下一次 preflight 自然放行，无需 resume ticket。
            try:
                approval = load_approval(batch_id)
            except QuotaError as exc:
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: {redact(str(exc))[:200]}")
                raise GatewayPausedError({
                    "status": "BLOCKED_BATCH_APPROVAL_MISSING",
                    "persisted": False, "stage": stage, "reason": "batch_approval_missing",
                    "trigger": {"task": task, "batch_id": batch_id},
                }) from exc
            if approval["task"] != task or approval["stage"] != stage:
                self._record_blocked(stage, task, batch_id,
                                     "preflight_blocked: batch credential task/stage mismatch")
                raise GatewayPausedError({
                    "status": "BLOCKED_BATCH_APPROVAL_MISMATCH",
                    "persisted": False, "stage": stage,
                    "reason": "batch_approval_mismatch",
                    "trigger": {"task": task, "batch_id": batch_id,
                                "approval_task": approval["task"],
                                "approval_stage": approval["stage"]},
                })

            rows = call_lines()
            global_used = max((r["global_used_after"] for r in rows), default=0)
            batch_used = sum(1 for r in rows if r.get("batch_id") == batch_id)
            task_used = sum(1 for r in rows if r.get("task") == task)

            trigger = {"stage": stage, "task": task, "batch_id": batch_id,
                       "batch_approved": approval["approved"], "batch_used": batch_used,
                       "global_used": global_used, "global_cap": HARD_CAP}

            # 3) 全局硬门 48（先到先停，与单任务列无关）
            if global_used >= HARD_CAP:
                state = _write_budget_pause("global_hard_cap", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: global_hard_cap {global_used}")
                raise GatewayPausedError(state)

            # 4) 单任务局部硬上限
            cap = TASK_HARD_CAPS.get(task)
            if cap is not None and task_used >= cap:
                state = _write_budget_pause("global_hard_cap",
                                            {**trigger, "task_used": task_used,
                                             "task_hard_cap": cap})
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: task{task}_hard_cap {task_used}>={cap}")
                raise GatewayPausedError(state)

            # 5) 常态 36：越过区间必须持单独书面批准的 beyond_normal 凭据
            if global_used >= NORMAL_BUDGET and not approval.get("beyond_normal"):
                state = _write_budget_pause("batch_budget_reached", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: normal budget {global_used}"
                                     " without beyond_normal approval")
                raise GatewayPausedError(state)

            # 6) 本批次计数
            if batch_used >= approval["approved"]:
                state = _write_budget_pause("batch_budget_reached", trigger)
                self._record_blocked(stage, task, batch_id,
                                     f"preflight_blocked: batch {batch_id} used "
                                     f"{batch_used}>={approval['approved']}")
                raise GatewayPausedError(state)

            return {"global_used": global_used, "batch_used": batch_used,
                    "task_used": task_used, "batch_approved": approval["approved"]}

    # ---------- 调用出口：成功与配额类失败都记账 ----------

    def record(self, stage: str, *, task: int, batch_id: str, ok: bool,
               model: "str | None" = None, thread_id: str = "",
               tokens_in: "int | None" = None, tokens_out: "int | None" = None,
               err: "str | None" = None, evidence: "str | None" = None) -> dict[str, Any]:
        with _FileLock():
            rows = call_lines()
            seq = max((int(r["seq"]) for r in rows), default=0) + 1
            global_after = max((r["global_used_after"] for r in rows), default=0) + 1
            try:
                approval = load_approval(batch_id)
                batch_approved = approval["approved"]
                approval_ref = f"quota/approvals/{batch_id}.json"
            except QuotaError:
                # 正常不会发生（preflight 已拦）；记账不允许因凭据缺失而丢失
                batch_approved, approval_ref = None, None
            batch_after = sum(1 for r in rows if r.get("batch_id") == batch_id) + 1
            row = {
                "ts_iso": _now_iso(), "event": "call",
                "stage": stage, "model": model, "thread_id": thread_id,
                "task": task, "batch_id": batch_id, "seq": seq,
                "global_used_after": global_after,
                "batch_approved": batch_approved,
                "batch_used_after": batch_after,
                "tokens_in": tokens_in, "tokens_out": tokens_out,
                "ok": bool(ok), "err": redact(err)[:500] if err else None,
                "approval_ref": approval_ref, "evidence": evidence,
            }
            _append_line(row)

            # 连续配额错误计数（成功归零；连续 3 次 → 长熔断，§6 阈值）
            counters = _read_counters()
            if ok:
                counters["consecutive_quota_errors"] = 0
            elif _is_quota_err(err):
                counters["consecutive_quota_errors"] = \
                    int(counters.get("consecutive_quota_errors", 0)) + 1
            _write_counters(counters)
            if counters.get("consecutive_quota_errors", 0) >= CONSECUTIVE_QUOTA_LIMIT \
                    and is_paused(stage) is None:
                _write_budget_pause("consecutive_quota_errors",
                                    {"consecutive_errors":
                                     counters["consecutive_quota_errors"],
                                     "stage": stage})
            return row

    def _record_blocked(self, stage: str, task: int, batch_id: str,
                        err: str) -> None:
        """preflight 拦截审计行：不触网、不递增 seq/全局计数（§6.3）。"""
        rows = call_lines()
        global_used = max((r["global_used_after"] for r in rows), default=0)
        batch_used = sum(1 for r in rows if r.get("batch_id") == batch_id)
        _append_line({
            "ts_iso": _now_iso(), "event": "preflight_blocked",
            "stage": stage, "model": None, "thread_id": "",
            "task": task, "batch_id": batch_id, "seq": None,
            "global_used_after": global_used,
            "batch_approved": None, "batch_used_after": batch_used,
            "tokens_in": None, "tokens_out": None,
            "ok": False, "err": err, "approval_ref": None, "evidence": None,
        })


# ---------- Task3：调用方记账上下文 + 网关统一记账门 ----------

@dataclass
class BillingContext:
    """一次业务运行（CLI 进程 / graph 线程）的记账归属。

    task/batch_id 是 preflight 校验批次凭据的输入；thread_id/evidence 写入每行
    ledger。evidence 是每次实呼粒度，由调用方在 call() 前通过
    set_current_evidence() 刷新（如 direction 的 *.req.json 落盘后）。
    """

    task: int
    batch_id: str
    thread_id: str = ""
    evidence: "str | None" = None


_BILLING_CTX: ContextVar["BillingContext | None"] = ContextVar(
    "simpraxis_billing_ctx", default=None)


@contextmanager
def billing_context(*, task: int, batch_id: str, thread_id: str = ""):
    """生产入口必须用本上下文包住实呼；无上下文的挂账本网关 fail-closed。"""
    ctx = BillingContext(task=task, batch_id=batch_id, thread_id=thread_id)
    token = _BILLING_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _BILLING_CTX.reset(token)


def current_billing() -> "BillingContext | None":
    return _BILLING_CTX.get()


def set_current_evidence(ref: "str | None") -> None:
    """把「本次实呼」的证据引用挂到当前上下文（无上下文时静默 no-op，
    供 monkeypatch/未挂账本的测试路径使用）。"""
    ctx = _BILLING_CTX.get()
    if ctx is not None:
        ctx.evidence = ref


class BillingGate:
    """ModelGateway.call/call_visual 入口/出口的唯一记账门（§1.5/§6.3）。

    入口：复用 router.is_paused → ledger.preflight（含凭据/全局/任务/批次六段）；
    出口：成功与失败都 ledger.record 一行整数 seq。preflight 拦截自身的
    seq=null 审计行由 QuotaLedger 负责，出口不重复记账。
    BillingGate 自身不含任何网络代码。
    """

    def __init__(self, ledger: "QuotaLedger | None" = None) -> None:
        self.ledger = ledger or QuotaLedger()

    @staticmethod
    def _ctx() -> BillingContext:
        ctx = _BILLING_CTX.get()
        if ctx is None:
            raise QuotaError(
                "网关挂了账本但缺少 billing_context（task/batch_id/thread_id）："
                "生产入口必须用 quota_ledger.billing_context(...) 包住实呼，"
                "无记账上下文的付费调用 fail-closed（禁止裸跑烧预算）")
        return ctx

    def preflight(self, stage: str) -> dict[str, Any]:
        ctx = self._ctx()
        return self.ledger.preflight(stage, task=ctx.task, batch_id=ctx.batch_id)

    def record(self, stage: str, *, ok: bool,
               model: "str | None" = None,
               tokens_in: "int | None" = None, tokens_out: "int | None" = None,
               err: "str | None" = None) -> dict[str, Any]:
        ctx = self._ctx()
        return self.ledger.record(
            stage, task=ctx.task, batch_id=ctx.batch_id, ok=ok, model=model,
            thread_id=ctx.thread_id, tokens_in=tokens_in, tokens_out=tokens_out,
            err=err, evidence=ctx.evidence)


# ---------- 批次批准凭据 / 恢复（只接受显式参数的 CLI） ----------

def write_approval(*, batch_id: str, task: int, stage: str, approved: int,
                   beyond_normal: bool, note: str) -> Path:
    if not batch_id or "/" in batch_id or ".." in batch_id:
        raise QuotaError("batch_id 非法")
    if approved <= 0:
        raise QuotaError("approved 必须为正整数")
    if beyond_normal:
        rows = call_lines()
        used = max((r["global_used_after"] for r in rows), default=0)
        if used < NORMAL_BUDGET:
            raise QuotaError(
                f"beyond_normal 凭据只允许在全局已用 >= {NORMAL_BUDGET} 时开具"
                f"（当前 {used}）；36→48 每批单独书面批准（D1）")
        if used + approved > HARD_CAP:
            raise QuotaError(
                f"本批 {approved} 次将越过全局硬顶 {HARD_CAP}（当前已用 {used}）")
    p = approvals_dir() / f"{batch_id}.json"
    if p.exists():
        raise QuotaError(f"批次凭据已存在，禁止覆盖：{p}")
    doc = {"batch_id": batch_id, "task": task, "stage": stage,
           "approved": int(approved), "beyond_normal": bool(beyond_normal),
           "created_at_iso": _now_iso(), "note": redact(note)[:1000]}
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def resume(*, ticket: str, batch_id: "str | None" = None) -> Path:
    """唯一恢复路径：显式 uuid 票 + 预算暂停时须带新批次凭据（§6.2）。"""
    if not ticket or len(ticket) < 8:
        raise QuotaError("--ticket 必须是非空 uuid（>=8 字符）")
    pause = router_state_path()
    if not pause.exists():
        raise QuotaError("无暂停态文件，无需 resume")
    state = json.loads(pause.read_text(encoding="utf-8"))
    reason = state.get("reason", "")
    rows = call_lines()
    used = max((r["global_used_after"] for r in rows), default=0)
    if reason in ("batch_budget_reached", "global_hard_cap") or used >= NORMAL_BUDGET:
        if not batch_id:
            raise QuotaError("预算类恢复必须同时给 --batch-id 指向新批次批准凭据")
        approval = load_approval(batch_id)
        if used >= NORMAL_BUDGET and not approval.get("beyond_normal"):
            raise QuotaError("36→48 区间恢复：新批次凭据必须 --beyond-normal 开具")
    audit = quota_dir() / "resume_audit.jsonl"
    with open(audit, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts_iso": _now_iso(), "ticket": ticket,
                             "reason": reason, "batch_id": batch_id,
                             "global_used": used}, ensure_ascii=False) + "\n")
    os.chmod(audit, 0o600)
    pause.unlink()
    return audit


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="Phase 1 配额账本（批准凭据/恢复/计数）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("approve", help="写入一批书面批准凭据（显式参数，无默认值）")
    pa.add_argument("--batch-id", required=True)
    pa.add_argument("--task", type=int, required=True, choices=sorted(TASK_HARD_CAPS))
    pa.add_argument("--stage", required=True,
                    choices=("macro_ideation", "coding_debug", "visual_rollout"))
    pa.add_argument("--approved", type=int, required=True)
    pa.add_argument("--beyond-normal", action="store_true",
                    help="36→48 缓冲区间批次（全局已用>=36 才允许开具）")
    pa.add_argument("--note", required=True, help="书面批准出处/要点（必填）")

    pr = sub.add_parser("resume", help="凭显式 uuid 票解除暂停态")
    pr.add_argument("--ticket", required=True)
    pr.add_argument("--batch-id", default=None)

    st = sub.add_parser("status", help="打印当前计数与暂停态，不触网不写账")

    args = p.parse_args(argv)
    if args.cmd == "approve":
        path = write_approval(batch_id=args.batch_id, task=args.task,
                              stage=args.stage, approved=args.approved,
                              beyond_normal=args.beyond_normal, note=args.note)
        print(f"[quota] 批次凭据已写（0600）：{path}")
        return 0
    if args.cmd == "resume":
        path = resume(ticket=args.ticket, batch_id=args.batch_id)
        print(f"[quota] 暂停已解除，恢复流水：{path}")
        return 0
    rows = read_lines()
    calls = call_lines(rows)
    used = max((r["global_used_after"] for r in calls), default=0)
    blocked = sum(1 for r in rows if r.get("event") == "preflight_blocked")
    print(json.dumps({"global_used": used, "normal_budget": NORMAL_BUDGET,
                      "hard_cap": HARD_CAP, "blocked_lines": blocked,
                      "paused": is_paused()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
