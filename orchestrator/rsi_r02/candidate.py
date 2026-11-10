"""RSI-R0.2 候选闭环（计划 §5.2、§8-T2，AC10/AC11/AC14/AC15/AC16）。

链路只有一条，且每一步都留证：

    公开任务 → 本地输入门 → 预留+占槽 → 一次模型 POST → usage 结算
    → 文本 JSON 解析 → 单动作 dispatch（工具前后各写一条 journal 事件）
    → 真实结果回喂 → 下一请求 → submit/judge 或停止

刻意**不**做的事：

* 模型回复不由 reference 或第二模型代写；本模块没有「注入提交」入口——
  submit 只能是模型文本经 :func:`actions.parse_action` 解析后 dispatch 的结果。
* 一次逻辑调用最多 1 个 POST：:class:`GatewayCaller` 强制 ``max_attempts=1``，
  并在拿到 capture 后核对 ``attempts_http``（>1 记隐性重试计数，不重发）。
* 工具结果未落盘就崩溃时，恢复路径只认 journal 里的未闭合 intent
  （:class:`state.StateStore.resume` → ``TOOL_OUTCOME_UNKNOWN``），绝不重跑物理动作。
"""
from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import messages as M
from orchestrator.rsi_r02 import state as ST

PRODUCER_CANDIDATE = "candidate_model"
PRODUCER_MAINTAINER = "maintainer"
DEFAULT_STAGE = "coding_debug"

EV_HIDDEN_RETRY = "HIDDEN_RETRY"
EV_REFUSED_BUDGET = ST.EV_BUDGET_HIT
#: 评审 M5：§10.2 行8「越权被正确拦截 ⇒ VALID_FAIL_POLICY，停该 trial」。
#: 越权＝模型在动作里越出公开动作面（塞未授权键/越界参数），与「JSON 写坏了」
#: 不同：后者是可见错误、下一轮自修；前者是有效配置下的策略违规，必须停该
#: trial 并落终态，不能让 loop 无限重试同一越权动作。
POLICY_VIOLATION_CODES = frozenset({
    AC.ActionCode.UNAUTHORIZED_KEY, AC.ActionCode.OUT_OF_BOUNDS})


class CandidateError(RuntimeError):
    """配置/协议前提被破坏（不是模型能力结论）。"""


#: 账本熔断原因里属于「预算帽」的一类：它们同样是**发网前**拒绝（零 POST），
#: 但语义上要落 ``BLOCKED_BUDGET`` 而不是中性的 ``PREFLIGHT_BLOCKED``（§10.2
#: 「全局费用/资源帽触及 ⇒ BLOCKED_BUDGET，覆盖不足」）。
BUDGET_PAUSE_REASONS = frozenset({
    "task_hard_cap", "global_hard_cap", "batch_budget_reached"})


def presend_exception_types() -> "tuple[type[BaseException], ...]":
    """网关**发网前**拒绝的异常族（零 POST、零费用；计划 §7 单列口径）。

    这些类型全部在 ``ModelGateway.call`` 的 preflight 之前抛出——一个字节都没
    发出、供应端无响应、费用为零。把它们并进「发送状态未知」会让一次纯粹的
    本地拦截冒充 UNKNOWN_SENT（2026-11-10 T5 首启实测：task0 帽拦下第 5 次
    调用，被记成 UNKNOWN，槽/预留/全局暂停全被污染）。延迟导入是因为网关模块
    会拖入 litellm，而本模块的单测不应因此变重。
    """
    from orchestrator.gateway.router import (
        GatewayPausedError, MissingGatewayCredentialError, NonRetryableError,
        PauseStateCorruptError, StageUnresolvedError)
    return (GatewayPausedError, PauseStateCorruptError,
            MissingGatewayCredentialError, NonRetryableError,
            StageUnresolvedError)


def presend_code(exc: BaseException) -> "tuple[str, str]":
    """发网前拒绝 → ``(journal code, reason)``；预算帽与中性拦截分开。"""
    state = getattr(exc, "state", None)
    reason = ""
    if isinstance(state, Mapping):
        reason = str(state.get("reason") or "")
    if reason in BUDGET_PAUSE_REASONS:
        return ST.EV_BUDGET_HIT, reason
    return ST.EV_PREFLIGHT_BLOCKED, reason or type(exc).__name__


# ---------------------------------------------------------------------------
# 模型调用（唯一入口：ModelGateway；测试注入替身）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    text: str
    capture: Mapping[str, Any] = field(default_factory=dict)

    @property
    def attempts_http(self) -> int:
        try:
            return int(self.capture.get("attempts_http") or 1)
        except (TypeError, ValueError):
            return 1

    @property
    def usage(self) -> B.Usage:
        return B.Usage.normalize(self.capture.get("usage"))


class GatewayCaller:
    """生产路径：把 :class:`messages.Request` 送进 ModelGateway 的一次调用。

    ``max_attempts=1`` 是硬前提（SDK/HTTP 层重试会破坏「每逻辑调用 ≤1 POST」）；
    构造时拒绝其它值，运行后再核对 ``attempts_http``。

    记账归属（``billing``）同样是构造期硬前提：网关挂了账本（``_billing`` 非空）
    时没有 ``{task, batch_id, thread_id}`` 的 caller 一构造就拒绝，把「缺
    billing_context 的付费调用」挡在第一次 POST 之前，而不是等网关 fail-closed。
    T4 首跑（2026-11-10）正是漏了这一层：槽已 claim、那次 POST 记为 UNKNOWN_SENT，
    整条协议链作废——此处的构造期守卫与 ``__call__`` 的上下文包裹是它的修复。
    """

    def __init__(self, *, gateway: Any, stage: str = DEFAULT_STAGE,
                 temperature: float = 0.2, timeout: int = 120,
                 billing: "Mapping[str, Any] | None" = None):
        if int(getattr(gateway, "max_attempts", 0) or 0) != 1:
            raise CandidateError(
                "ModelGateway 必须以 max_attempts=1 构造（关闭内部退避重试）；"
                f"当前 max_attempts={getattr(gateway, 'max_attempts', None)!r}")
        self.billing = None if billing is None else {
            "task": int(billing["task"]),
            "batch_id": str(billing["batch_id"]),
            "thread_id": str(billing.get("thread_id") or ""),
        }
        if (self.billing is None
                and getattr(gateway, "_billing", None) is not None):
            raise CandidateError(
                "网关挂了账本但 GatewayCaller 未给 billing 归属（task/batch_id/"
                "thread_id）：付费实呼必须落在 billing_context 内，构造即拒绝")
        if self.billing is not None and not self.billing["batch_id"].strip():
            raise CandidateError("billing.batch_id 不得为空（批次凭据按它取）")
        self.gateway = gateway
        self.stage = stage
        self.temperature = float(temperature)
        self.timeout = int(timeout)

    def __call__(self, request: M.Request) -> Reply:
        capture: "dict[str, Any]" = {}
        messages = [{"role": "system", "content": request.system},
                    *request.as_messages()]
        with self._billing_context():
            text, _tokens = self.gateway.call(
                self.stage, messages, max_tokens=request.max_tokens,
                temperature=self.temperature, timeout=self.timeout,
                capture=capture)
        return Reply(text=str(text or ""), capture=dict(capture))

    def _billing_context(self) -> Any:
        """每次 POST 一个独立上下文（不在进程级常驻，跨 trial 不串账）。"""
        if self.billing is None:
            return contextlib.nullcontext()
        from orchestrator.gateway.quota_ledger import billing_context

        return billing_context(task=self.billing["task"],
                               batch_id=self.billing["batch_id"],
                               thread_id=self.billing["thread_id"])


# ---------------------------------------------------------------------------
# 配置与公共文档
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopConfig:
    post_cap: int = 24
    max_tokens: int = M.MAX_OUTPUT_TOKENS
    task_brief: str = ("按动作协议回复你的下一个动作（单条 JSON）。"
                       "所有数值必须来自你自己在本 trial 的真实实验/分析。")


def load_public_doc(ctx: C.CampaignContext, instance_id: str) -> "dict[str, Any]":
    """候选唯一可见的任务文档（``public/tasks/<id>/task.json``）。"""
    rel = f"public/tasks/{instance_id}/task.json"
    path = ctx.relative(rel)
    if not path.is_file():
        raise CandidateError(f"公开任务缺件：{path}")
    import json
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 候选循环
# ---------------------------------------------------------------------------


class CandidateLoop:
    """一个 trial 的完整候选闭环；可重入（``run`` 幂等于终态）。"""

    def __init__(self, *, ctx: C.CampaignContext, trial: Any,
                 public_doc: Mapping[str, Any], store: ST.StateStore,
                 state: ST.TrialState, journal: ST.Journal, slots: ST.SlotBook,
                 billing: B.BillingBook, counter: Any,
                 call_model: Callable[[M.Request], Reply],
                 slot_ids: Sequence[str], campaign: "ST.CampaignState | None" = None,
                 config: LoopConfig = LoopConfig()):
        self.ctx = ctx
        self.trial = trial
        self.public_doc = dict(public_doc)
        self.store = store
        self.state = state
        self.journal = journal
        self.slots = slots
        self.billing = billing
        self.counter = counter
        self.call_model = call_model
        self.slot_ids = tuple(slot_ids)
        self.campaign = campaign
        self.config = config
        self._count = M.require_counter(counter)
        self.system = M.build_system(self.public_doc, trial.surface)
        self._turns: "list[M.Turn]" = []

    # -- 运行 -------------------------------------------------------------

    def run(self) -> "dict[str, Any]":
        st = self.state
        if st.status in ST.TRIAL_TERMINAL:
            return self._summary("already_terminal")
        st = self.store.transition(st, status=ST.TRIAL_RUNNING)

        while True:
            stop = self._stop_reason(st)
            if stop is not None:
                return stop

            request = M.build_request(
                system=self.system, task_brief=self.config.task_brief,
                turns=self._turns, counter=self._count,
                max_tokens=self.config.max_tokens)
            acc = M.check_request(request)
            self.journal.append(
                kind="decision", trial_id=st.trial_id, action="check_input",
                ok=acc.ok, code=None if acc.ok else acc.verdict,
                message=acc.as_dict()["note"],
                payload={"request_sha256": request.sha256,
                         "n_bytes": acc.n_bytes,
                         "local_tokens": acc.local_tokens,
                         "verdict": acc.verdict})
            if not acc.ok:
                self.store.transition(st, status=ST.TRIAL_BLOCKED_INPUT_LIMIT,
                                      code=ST.EV_INPUT_LIMIT,
                                      reason=f"{acc.verdict}（n_bytes={acc.n_bytes} "
                                             f"tokens={acc.local_tokens}）；不抬帽")
                return self._summary("input_limit")

            slot_id = self._next_slot()
            if slot_id is None:
                self.store.transition(st, status=ST.TRIAL_BLOCKED, code="NO_SLOT",
                                      reason="本 trial 槽已用尽/被取消")
                return self._summary("no_slot")

            call_id = f"{st.trial_id}::{slot_id}"
            try:
                self.billing.reserve(call_id=call_id, slot_id=slot_id)
            except B.BillingPaused as exc:
                self.store.transition(st, status=ST.TRIAL_BLOCKED,
                                      code="BILLING_PAUSED", reason=str(exc))
                return self._summary("billing_paused")
            except B.BudgetBlocked as exc:
                self._pause_campaign(ST.EV_BUDGET_HIT, str(exc))
                self.slots.cancel_unused(reason=f"BLOCKED_BUDGET: {exc}",
                                         prefix=self._slot_prefix())
                self.store.transition(st, status=ST.TRIAL_BLOCKED,
                                      code=ST.EV_BUDGET_HIT, reason=str(exc))
                return self._summary("budget")

            self.slots.claim(slot_id, call_id=call_id, trial_id=st.trial_id)
            self.journal.append(
                kind="intent", trial_id=st.trial_id, action="post",
                slot_id=slot_id,
                payload={"request_sha256": request.sha256,
                         "n_bytes": request.n_bytes,
                         "local_tokens": request.local_tokens,
                         "producer": PRODUCER_CANDIDATE,
                         "call_id": call_id})

            try:
                reply = self.call_model(request)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, presend_exception_types()):
                    # 发网前拒绝：零 POST、零费用。单列，绝不冒充 UNKNOWN_SENT。
                    return self._presend_blocked(st, slot_id, call_id, exc)
                failure = f"{type(exc).__name__}: {exc}"[:400]
                self.billing.mark_unknown(call_id=call_id, failure=failure)
                self.slots.settle(slot_id, status=ST.SLOT_UNKNOWN,
                                  reason=f"UNKNOWN_SENT: {failure}")
                self.journal.append(
                    kind="result", trial_id=st.trial_id, action="post",
                    slot_id=slot_id, ok=None, code=ST.EV_UNKNOWN_SENT,
                    message=failure,
                    payload={"sent_state": "UNKNOWN", "call_id": call_id})
                self._pause_campaign(ST.EV_UNKNOWN_SENT,
                                     f"{st.trial_id} slot={slot_id} {failure}")
                self.store.transition(st, status=ST.TRIAL_UNKNOWN_SENT,
                                      code=ST.EV_UNKNOWN_SENT, reason=failure)
                return self._summary("unknown_sent")

            usage = reply.usage
            try:
                settled = self.billing.settle(call_id=call_id, usage=usage)
            except B.BillingError as exc:  # 结算异常按未知处理，不伪称已结算
                self.billing.mark_unknown(call_id=call_id, failure=str(exc))
                self.slots.settle(slot_id, status=ST.SLOT_UNKNOWN,
                                  reason=f"COST_UNKNOWN: {exc}")
                self.journal.append(kind="result", trial_id=st.trial_id,
                                    action="post", slot_id=slot_id, ok=None,
                                    code=ST.EV_COST_UNKNOWN, message=str(exc),
                                    payload={"call_id": call_id})
                self._pause_campaign(ST.EV_COST_UNKNOWN, str(exc))
                self.store.transition(st, status=ST.TRIAL_UNKNOWN_SENT,
                                      code=ST.EV_COST_UNKNOWN, reason=str(exc))
                return self._summary("cost_unknown")

            st.posts_sent += 1
            hidden = reply.attempts_http > 1
            if hidden:
                st.posts_hidden_retry += 1
                self.journal.append(
                    kind="decision", trial_id=st.trial_id, action="hidden_retry",
                    ok=False, code=EV_HIDDEN_RETRY,
                    message=f"attempts_http={reply.attempts_http}（应恒为 1）",
                    payload={"slot_id": slot_id})
            self.journal.append(
                kind="result", trial_id=st.trial_id, action="post",
                slot_id=slot_id, ok=True,
                payload={"call_id": call_id,
                         "cost_status": settled.cost_status,
                         "settled_usd": settled.settled_usd,
                         "usage_raw": dict(usage.raw),
                         "usage_normalized": usage.as_dict(),
                         "attempts_http": reply.attempts_http,
                         "stop_reason": reply.capture.get("stop_reason"),
                         "provider_model": reply.capture.get("provider_model"),
                         "provider_request_id": reply.capture.get("provider_request_id"),
                         "body_sha256": reply.capture.get("body_sha256"),
                         "content_bytes": reply.capture.get("content_bytes"),
                         # 原始回复逐字入证据（T4 的非ce绑定校验与 T5「保存原始
                         # 响应」都读这里；不落盘就无法证明动作来自本轮真实结果）。
                         "assistant_text": str(reply.text or ""),
                         "assistant_text_sha256":
                             hashlib.sha256(str(reply.text or "").encode("utf-8")
                                            ).hexdigest(),
                         "producer": PRODUCER_CANDIDATE})
            self.store.save(st)

            if settled.status == B.STATUS_UNKNOWN or settled.cost_status == B.COST_UNKNOWN:
                detail = (settled.failure
                          or f"unknown_fields={list(usage.unknown_fields)}")
                self.slots.settle(slot_id, status=ST.SLOT_UNKNOWN,
                                  reason=f"COST_UNKNOWN: {detail}"[:400])
                self._pause_campaign(ST.EV_COST_UNKNOWN,
                                     f"usage 缺失/不可归并：{detail}")
                self.store.transition(st, status=ST.TRIAL_UNKNOWN_SENT,
                                      code=ST.EV_COST_UNKNOWN, reason=detail)
                return self._summary("cost_unknown")

            text = (reply.text or "").strip()
            if not text:
                # 明确已响应的空答：按 §10.2「明确已响应的调用失败」停该 trial
                self.slots.settle(slot_id, status=ST.SLOT_FAIL,
                                  reason="EMPTY_RESPONSE")
                self.journal.append(kind="decision", trial_id=st.trial_id,
                                    action="classify_reply", ok=False,
                                    code="EMPTY_RESPONSE",
                                    message="HTTP 已响应但正文为空")
                self.store.transition(st, status=ST.TRIAL_BLOCKED,
                                      code="EMPTY_RESPONSE",
                                      reason="空答按已响应失败处理，不隐式重试")
                return self._summary("empty_response")
            self.slots.settle(slot_id, status=ST.SLOT_OK)

            action = AC.parse_action(text, self.trial.surface)
            st.turns += 1
            if isinstance(action, AC.ActionFailure):
                result = action.as_dict()
                if action.code in POLICY_VIOLATION_CODES:
                    # 越权被正确拦截 ⇒ 该 trial VALID_FAIL_POLICY 并停；不计成功，
                    # 也不当作泄密（未越权成功），其余 trial 照常继续。
                    self.journal.append(
                        kind="decision", trial_id=st.trial_id,
                        action="parse", ok=False,
                        code=ST.EV_POLICY_VIOLATION,
                        message=f"{action.code}: {action.message}"[:500],
                        payload={"slot_id": slot_id,
                                 "surface_kind": self.trial.surface.kind})
                    self.store.transition(
                        st, status=ST.TRIAL_VALID_FAIL_POLICY,
                        code=ST.EV_POLICY_VIOLATION,
                        reason=f"越权动作被拦截（{action.code}）；停该 trial")
                    return self._summary("policy_violation")
                self.journal.append(
                    kind="decision", trial_id=st.trial_id,
                    action="parse", ok=False, code=action.code,
                    message=action.message,
                    payload={"slot_id": slot_id})
                self._turns.append(M.Turn(model_text=text, result=result))
                self.store.save(st)
                continue

            outcome = self._dispatch(st, action, slot_id)
            self._turns.append(M.Turn(model_text=text, result=outcome["result"]))
            self.store.save(st)
            if outcome.get("fatal"):
                return self._summary(outcome["fatal"])
            if isinstance(action, AC.Submit) and outcome["result"].get("ok"):
                judge = self.trial.judge()
                st.independent_success = bool(judge.get("pass")) and not st.assisted
                self.journal.append(
                    kind="decision", trial_id=st.trial_id, action="judge",
                    ok=bool(judge.get("pass")),
                    payload={"independent_success": st.independent_success,
                             "verdict": judge, "producer": PRODUCER_CANDIDATE})
                self.store.transition(st, status=ST.TRIAL_SUBMITTED,
                                      code="SUBMITTED")
                return self._summary("submitted")

    # -- 单动作分派（工具前后各写 journal；异常不留假结果）----------------

    def _dispatch(self, st: ST.TrialState, action: AC.Action,
                  slot_id: str) -> "dict[str, Any]":
        name = action_name(action)
        payload = self._action_payload(action)
        self.journal.append(kind="intent", trial_id=st.trial_id, action=name,
                            slot_id=slot_id,
                            payload={**payload, "producer": PRODUCER_CANDIDATE})
        try:
            result = self.trial.dispatch(action)
        except Exception as exc:  # noqa: BLE001 —— 工具可能已执行：不得补结果
            self.journal.append(kind="decision", trial_id=st.trial_id,
                                action=name, ok=False,
                                code=ST.EV_TOOL_OUTCOME_UNKNOWN,
                                message=f"{type(exc).__name__}: {exc}"[:400],
                                payload={"slot_id": slot_id})
            if self.campaign is not None:
                self.campaign.pause(code=ST.EV_TOOL_OUTCOME_UNKNOWN,
                                    reason=f"{st.trial_id} {name} 抛异常，结果未落盘")
            self.store.transition(st, status=ST.TRIAL_TOOL_OUTCOME_UNKNOWN,
                                  code=ST.EV_TOOL_OUTCOME_UNKNOWN,
                                  reason=f"{name} 结果不确定；不重跑原实验")
            return {"result": {"ok": False, "code": ST.EV_TOOL_OUTCOME_UNKNOWN,
                               "message": str(exc)[:400]},
                    "fatal": "tool_outcome_unknown"}
        payload = {k: v for k, v in result.items()
                   if k in ("run_id", "analysis_id", "path", "sha256",
                            "status", "claim")}
        analysis = result.get("analysis")
        if isinstance(analysis, Mapping):
            # 分析回包（含 STDOUT 预览）逐字入证据：协议检查器要独立核验候选
            # 是否真的读到了本轮产物（T4 的 nonce/stat 绑定），不能靠自述。
            payload["analysis_id"] = analysis.get("analysis_id")
            payload["stdout_preview"] = analysis.get("stdout_preview")
        self.journal.append(kind="result", trial_id=st.trial_id, action=name,
                            slot_id=slot_id, ok=bool(result.get("ok")),
                            code=result.get("code"),
                            message=result.get("message"),
                            payload=payload)
        return {"result": dict(result)}

    @staticmethod
    def _action_payload(action: AC.Action) -> "dict[str, Any]":
        if isinstance(action, AC.RunExperiment):
            return {"params": dict(action.params)}
        if isinstance(action, AC.Analyze):
            return {"source_sha256": _sha(action.source),
                    "source_bytes": len(action.source.encode("utf-8")),
                    "run_ids": list(action.run_ids)}
        if isinstance(action, AC.Preregister):
            return {"plan_keys": sorted(action.plan.keys())}
        if isinstance(action, AC.Submit):
            return {"claim": action.claim, "run_ids": list(action.run_ids),
                    "analysis_ids": list(action.analysis_ids)}
        return {}

    # -- 停止条件 ---------------------------------------------------------

    def _stop_reason(self, st: ST.TrialState) -> "dict[str, Any] | None":
        if self.campaign is not None and self.campaign.paused:
            reason = self.campaign.pause_reason() or ""
            self.store.transition(st, status=ST.TRIAL_BLOCKED,
                                  code="CAMPAIGN_PAUSED", reason=reason)
            return self._summary("campaign_paused")
        cap = min(int(st.post_cap), int(self.config.post_cap))
        if st.posts_sent >= cap:
            self.journal.append(kind="decision", trial_id=st.trial_id,
                                action="cap", ok=False,
                                code=ST.EV_VALID_FAIL_CAP,
                                message=f"posts_sent={st.posts_sent} cap={cap}")
            self.store.transition(st, status=ST.TRIAL_VALID_FAIL_CAP,
                                  code=ST.EV_VALID_FAIL_CAP,
                                  reason=f"有效响应耗尽 {cap} 次 POST 未解题")
            return self._summary("valid_fail_cap")
        return None

    def _next_slot(self) -> "str | None":
        for sid in self.slot_ids:
            if self.slots.status(sid) == ST.SLOT_UNUSED:
                return sid
        return None

    def _presend_blocked(self, st: ST.TrialState, slot_id: str, call_id: str,
                         exc: BaseException) -> "dict[str, Any]":
        """发网前拒绝的收口：预留按 $0 释放、槽按 CANCELLED 收、campaign 停全部。

        计划 §7「发网前阻断单列零POST」+ §10.2「全局费用/资源帽触及 ⇒
        BLOCKED_BUDGET，取消剩余槽；覆盖不足，不能提高本 campaign 帽」。
        事实链：ledger 已有一行 ``preflight_blocked``（``seq=null``，占批不占
        实呼）；这里只补 trial 侧的对应事实，不写 UNKNOWN、不占实呼。
        """
        code, reason = presend_code(exc)
        detail = f"{type(exc).__name__}: {exc}"[:400]
        self.billing.release(call_id=call_id,
                             reason=f"{code} {reason} {detail}"[:900])
        self.slots.release_claimed(
            slot_id, reason=f"PREFLIGHT_BLOCKED {code} {detail}"[:400])
        self.journal.append(
            kind="result", trial_id=st.trial_id, action="post", slot_id=slot_id,
            ok=False, code=code, message=detail,
            payload={"sent_state": "NOT_SENT", "call_id": call_id,
                     "posts_sent": st.posts_sent, "pause_reason": reason})
        self._pause_campaign(code, f"{st.trial_id} slot={slot_id} "
                                   f"发网前阻断（零 POST）：{detail}")
        if code == ST.EV_BUDGET_HIT:
            # §10.2 只有「全局费用/资源帽触及」才取消剩余槽；中性拦截
            # （凭据缺失/暂停件/配置错误）只停全部，不预先烧掉本 trial 的槽。
            self.slots.cancel_unused(
                reason=f"{code}: 发网前阻断后不再发送（§10.2 取消剩余槽）",
                prefix=self._slot_prefix())
        self.store.transition(st, status=ST.TRIAL_BLOCKED, code=code,
                              reason=f"发网前阻断（零 POST）：{detail}")
        return self._summary("preflight_blocked")

    def _slot_prefix(self) -> str:
        if not self.slot_ids:
            return ""
        first = self.slot_ids[0]
        return first.rsplit("-", 1)[0] + "-"

    def _pause_campaign(self, code: str, reason: str) -> None:
        if self.campaign is not None:
            self.campaign.pause(code=code, reason=reason)

    # -- 救援与总结 -------------------------------------------------------

    def assist(self, reason: str) -> ST.TrialState:
        """人工实质救援：立即记 ASSISTED 且 independent_success=False。"""
        self.state.assisted = True
        self.state.independent_success = False
        return ST.mark_assisted(self.store, self.journal, self.state,
                                reason=reason)

    def _summary(self, outcome: str) -> "dict[str, Any]":
        return {
            "trial_id": self.state.trial_id,
            "instance_id": self.state.instance_id,
            "family": self.state.family,
            "outcome": outcome,
            "status": self.state.status,
            "reason_code": self.state.reason_code,
            "posts_sent": self.state.posts_sent,
            "posts_hidden_retry": self.state.posts_hidden_retry,
            "turns": self.state.turns,
            "assisted": self.state.assisted,
            "independent_success": self.state.independent_success,
            "producer": PRODUCER_CANDIDATE,
            "ended_at_utc": utc_now_iso_z(),
        }


def action_name(action: Any) -> str:
    """协议动作名（与模型可见的 JSON action 字段一致）。"""
    if isinstance(action, AC.Preregister):
        return "preregister"
    if isinstance(action, AC.RunExperiment):
        return "run_experiment"
    if isinstance(action, AC.Analyze):
        return "analyze"
    if isinstance(action, AC.Submit):
        return "submit"
    return type(action).__name__


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
