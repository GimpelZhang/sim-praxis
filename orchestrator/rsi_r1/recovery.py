"""RSI-R1.2 T2：有界自修的判别面与里程碑游标（Plan §3.1；AC17–24/54）。

两条纪律（逐字来自 Plan §3.1）：

1. **判别输入是证据，不是错误码**：同一个 ``RESOURCE_EXCEEDED`` 既可能是矩阵
   前执行校验（无副作用，可自修），也可能是已经部分执行后的资源超额（不可
   自修）。判别必须看 ``phase`` / 是否有未闭 intent / 已确认效果状态 / 冻结
   状态 / 原始异常类别；**缺证据默认 terminal**，可恢复结果必须带
   ``side_effect_status=NONE`` 或可验证的 ``KNOWN_COMPLETE``。
2. **完成判据是里程碑游标**：八个规范位置各自记录所消费的 source/plan/run-set
   版本；改 design/plan/analysis 会让下游里程碑失效（旧 raw 仍计预算并归档，
   但不复用为新计划证据），确认冻结后这些修改一律终止
   （``METHOD_CHANGE_AFTER_CONFIRM``）。

白名单只在 T0 注册（``profiles.RECOVERABLE_ERROR_CODES`` / ``TERMINAL_ERROR_CODES``），
运行中不可扩展：本模块只读取 ``ProtocolPolicy`` 的两个 tuple，不提供任何写入口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator.rsi_r1 import profiles as P

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

RECOVERY_RECOVERABLE = "recoverable"
RECOVERY_TERMINAL = "terminal"
RECOVERY_REPUBLISH_ONLY = "republish_only"

EFFECT_NONE = "NONE"
EFFECT_KNOWN_COMPLETE = "KNOWN_COMPLETE"
EFFECT_UNKNOWN = "UNKNOWN"

CODE_TRANSPORT = "TRANSPORT_FAILURE"
CODE_UNCLASSIFIED = "UNCLASSIFIED"
CODE_UNKNOWN_EFFECT = "TOOL_OUTCOME_UNKNOWN"
CODE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
CODE_BUDGET_UNREACHABLE = "BUDGET_UNREACHABLE"
CODE_METHOD_CHANGE = "METHOD_CHANGE_AFTER_CONFIRM"
CODE_REPUBLISH_ONLY = "RECOVERY_REPUBLISH_ONLY"

QUALIFICATION_SCHEMA = "simpraxis.rsi_r1.qualification.v1"

#: 网关传输类（空回答/超时/连接错误）：立即终止本轮新付费，禁止隐式重发。
TRANSPORT_CODES: "tuple[str, ...]" = (
    "EmptyResponseError", "GatewayError", "TimeoutError", "ConnectError",
    "ReadTimeout", "WriteTimeout", "TransportError", "RemoteProtocolError",
)

#: 只允许在**确认冻结前**可恢复的错误码（Plan §3.1「确认冻结前」括号）。
#: 冻结之后再出现这些码，说明候选想改方法/来源重做确认——按不可恢复处理。
PRE_FREEZE_ONLY_CODES: "frozenset[str]" = frozenset({
    "CANDIDATE_PROCESS_EXIT", "ARTIFACT_MALFORMED",
    "DESIGN_FAILED", "ANALYSIS_FAILED",
    "PLAN_MISMATCH", "SOURCE_SHA_INVALID",
    "HYPOTHESIS_INSUFFICIENT", "DISTRIBUTION",
    "DISTRIBUTION_DEGENERATE", "DESIGN_NOT_SEPARABLE",
    "CONDITION_UNKNOWN", "SPEC_TAMPERED",
    "CONFIRM_BLOCK_COUNT", "MAX_CONDITIONS",
})

#: 八位置 → 规范动作名（重复的 write_artifact 用 position kind 区分）。
POSITION_ACTIONS: "tuple[str, ...]" = (
    "write_artifact", "propose_plan", "run_batch", "write_artifact",
    "analyze", "freeze_confirm", "run_confirm", "submit_claim",
)

#: 八位置 → 产物 kind（仅 write_artifact 位置非空）。
POSITION_KINDS: "tuple[str, ...]" = (
    "design", "", "", "analysis", "", "", "", "",
)

#: 位置内容变更后，从哪个位置起下游失效（Plan §3.1：
#: 改 design ⇒ propose 及其下游；改 plan ⇒ 探索/分析/freeze 下游；
#: 改 analysis ⇒ 分析成功及其下游）。
INVALIDATE_FROM: "Mapping[int, int]" = {0: 1, 1: 2, 3: 4}

assert len(POSITION_ACTIONS) == len(P.CANONICAL_POSITIONS) == 8
assert len(POSITION_KINDS) == len(POSITION_ACTIONS)


# ---------------------------------------------------------------------------
# 失败判别
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureEvidence:
    """一次失败回合的**宿主证据**（不是候选自述）。"""

    phase: str
    frozen: bool
    open_intent: bool
    effect_status: str
    code: str
    error_class: str = ""
    transport: bool = False
    budget_exhausted: bool = False
    method_change: bool = False

    def as_dict(self) -> "dict[str, Any]":
        return {"phase": self.phase, "frozen": bool(self.frozen),
                "open_intent": bool(self.open_intent),
                "effect_status": self.effect_status, "code": self.code,
                "error_class": self.error_class,
                "transport": bool(self.transport),
                "budget_exhausted": bool(self.budget_exhausted),
                "method_change": bool(self.method_change)}


@dataclass(frozen=True)
class RecoveryDecision:
    kind: str
    code: str
    reason: str
    side_effect_status: str

    @property
    def recoverable(self) -> bool:
        return self.kind == RECOVERY_RECOVERABLE

    def as_dict(self) -> "dict[str, Any]":
        return {"kind": self.kind, "code": self.code, "reason": self.reason,
                "side_effect_status": self.side_effect_status,
                "recoverable": self.recoverable}


def classify_failure(evidence: FailureEvidence, *,
                     policy: "P.ProtocolPolicy" = P.R12_POLICY
                     ) -> RecoveryDecision:
    """按 Plan §3.1 分类一次失败。缺证据一律 terminal（fail-closed）。"""
    code = str(evidence.code or "").strip() or CODE_UNCLASSIFIED
    transport = bool(evidence.transport) or evidence.error_class in TRANSPORT_CODES \
        or code in TRANSPORT_CODES
    if transport:
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_TRANSPORT,
            "网关空回答/超时/连接失败：终止本轮新付费，不重发原请求、不换 request_id",
            evidence.effect_status)
    if evidence.budget_exhausted or code == "BUDGET_REFUSED":
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_BUDGET_EXHAUSTED,
            "预算耗尽：不可恢复（付费后无本轮修复入口）", evidence.effect_status)
    if evidence.method_change or code == CODE_METHOD_CHANGE:
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_METHOD_CHANGE,
            "确认冻结后改方法/来源：禁止重发布当重实验", evidence.effect_status)
    if code in policy.terminal_codes:
        return RecoveryDecision(
            RECOVERY_TERMINAL, code,
            "错误码在 T0 注册的终止类（越权/未知发送/未知执行/LOCK 漂移/仪器失败）",
            evidence.effect_status)
    if evidence.effect_status not in (EFFECT_NONE, EFFECT_KNOWN_COMPLETE):
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_UNKNOWN_EFFECT,
            f"副作用状态 {evidence.effect_status!r} 不可归因；缺证据默认终止",
            evidence.effect_status)
    if evidence.open_intent:
        if evidence.effect_status == EFFECT_KNOWN_COMPLETE:
            return RecoveryDecision(
                RECOVERY_REPUBLISH_ONLY, CODE_REPUBLISH_ONLY,
                "存在未闭 intent 但已有可信完整结果：只允许 ResearchState.recover "
                "幂等重发布，不重跑物理调用", evidence.effect_status)
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_UNKNOWN_EFFECT,
            "存在未闭 intent 且无可信结果：TOOL_OUTCOME_UNKNOWN，禁止自动重跑",
            evidence.effect_status)
    if code not in policy.recoverable_codes:
        return RecoveryDecision(
            RECOVERY_TERMINAL, CODE_UNCLASSIFIED,
            "错误码不在 T0 注册的可恢复白名单内（运行中不可扩展白名单）",
            evidence.effect_status)
    if evidence.frozen and code in PRE_FREEZE_ONLY_CODES:
        return RecoveryDecision(
            RECOVERY_TERMINAL, code,
            "确认冻结后才出现的改方法/来源类错误：不允许改写源码重确认",
            evidence.effect_status)
    return RecoveryDecision(
        RECOVERY_RECOVERABLE, code,
        "前执行/沙盒内可安全归因：候选可在同一 attempt 自修，额外 POST 与沙盒照计",
        evidence.effect_status)


# ---------------------------------------------------------------------------
# 里程碑游标
# ---------------------------------------------------------------------------

@dataclass
class MilestoneRecord:
    position: int
    name: str
    action_index: int
    consumed: "dict[str, Any]" = field(default_factory=dict)

    def as_dict(self) -> "dict[str, Any]":
        return {"position": self.position, "name": self.name,
                "action_index": self.action_index,
                "consumed": dict(self.consumed)}


def _sha_doc(obj: Any) -> str:
    import hashlib
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _versions_for(position: int, result: Mapping[str, Any]) -> "dict[str, Any]":
    """从宿主回包里取该位置所消费的版本（只读回包，不信候选自述）。"""
    if position == 0:
        return {"design_source_sha256": str(result.get("source_sha256") or "")}
    if position == 1:
        return {"plan_sha256": str(result.get("plan_sha256") or ""),
                "revision": result.get("revision")}
    if position == 2:
        runs = [str(r) for r in (result.get("run_ids") or [])]
        return {"run_set_sha256": _sha_doc(sorted(runs)), "n_runs": len(runs)}
    if position == 3:
        return {"analysis_source_sha256": str(result.get("source_sha256") or "")}
    if position == 4:
        return {"analysis_output_sha256": _sha_doc(result.get("analysis")),
                "analysis_relpath": result.get("analysis_relpath")}
    if position == 5:
        return {"confirm_token": result.get("confirm_token"),
                "confirm_manifest_sha256": str(result.get("manifest_sha256") or "")}
    if position == 6:
        return {"confirm_result_sha256": str(result.get("result_sha256") or ""),
                "n_runs": result.get("n_runs")}
    return {"judge_verdict": result.get("judge_verdict"),
            "verdict": result.get("verdict")}


class MilestoneState:
    """八位置里程碑 + 失效规则 + 首次尝试记录（Plan §3.1 第三段）。"""

    def __init__(self, *, policy: "P.ProtocolPolicy" = P.R12_POLICY) -> None:
        self.policy = policy
        self.records: "dict[int, MilestoneRecord]" = {}
        self.first_attempt: "dict[int, dict[str, Any]]" = {}
        self.recoverable_failures: "list[dict[str, Any]]" = []
        self.first_error: "dict[str, Any] | None" = None
        self.invalidations: "list[dict[str, Any]]" = []
        self.frozen = False
        self.stop_note: "str | None" = None

    # -- 读数 ------------------------------------------------------------
    @property
    def cursor(self) -> int:
        pos = 0
        while pos in self.records:
            pos += 1
        return pos

    @property
    def completed(self) -> bool:
        return self.cursor >= len(POSITION_ACTIONS)

    def min_remaining_actions(self) -> int:
        return max(0, len(POSITION_ACTIONS) - self.cursor)

    # -- 位置识别 --------------------------------------------------------
    def position_for(self, action: "str | None",
                     result: Mapping[str, Any]) -> "int | None":
        name = str(action or "")
        kind = result.get("kind")
        if name == "write_artifact":
            candidates = [i for i, k in enumerate(POSITION_KINDS)
                          if POSITION_ACTIONS[i] == name and k and k == kind]
            if candidates:
                return candidates[0]
            if kind is None:
                # 无 kind（解析失败/回包缺字段）：若游标本身就是写位置则按游标
                if POSITION_ACTIONS[self.cursor] == name \
                        and POSITION_KINDS[self.cursor]:
                    return self.cursor
            return None
        for i, act in enumerate(POSITION_ACTIONS):
            if act == name:
                return i
        return None

    # -- 成功 ------------------------------------------------------------
    def record_success(self, *, action: "str | None", result: Mapping[str, Any],
                       action_index: int) -> "dict[str, Any]":
        pos = self.position_for(action, result)
        if pos is None:
            return {"advanced": None, "extra": True}
        if self.frozen and pos in INVALIDATE_FROM:
            return {"advanced": None, "extra": True, "terminal": CODE_METHOD_CHANGE,
                    "detail": "确认冻结后禁止改写 design/plan/analysis"}
        if pos == self.cursor:
            consumed = _versions_for(pos, result)
            self.records[pos] = MilestoneRecord(
                position=pos, name=P.CANONICAL_POSITIONS[pos],
                action_index=action_index, consumed=consumed)
            self.first_attempt.setdefault(
                pos, {"ok": True, "code": "", "action_index": action_index})
            if pos == 5:
                self.frozen = True
            return {"advanced": pos, "extra": False, "consumed": consumed}
        # 额外动作：只有**内容真的变了**才让下游失效；同字节重发不洗掉里程碑。
        changed = False
        if pos in INVALIDATE_FROM and pos in self.records:
            try:
                changed = _versions_for(pos, result) != self.records[pos].consumed
            except Exception:  # pragma: no cover - 防御：版本不可比按变更处理
                changed = True
            if changed:
                start = INVALIDATE_FROM[pos]
                dropped = self._invalidate_from(start)
                self.invalidations.append(
                    {"position": pos, "name": P.CANONICAL_POSITIONS[pos],
                     "from": start, "dropped": dropped,
                     "action_index": action_index})
                return {"advanced": None, "extra": True, "invalidated": dropped}
        return {"advanced": None, "extra": True}

    def _invalidate_from(self, start: int) -> "list[int]":
        dropped = sorted(p for p in self.records if p >= start)
        for p in dropped:
            del self.records[p]
        if 5 in dropped:
            self.frozen = False
        return dropped

    # -- 失败 ------------------------------------------------------------
    def note_failure(self, *, action: "str | None",
                     result: Mapping[str, Any], decision: RecoveryDecision,
                     action_index: int) -> None:
        pos = self.position_for(action, result)
        if pos is None:
            pos = self.cursor
        first = self.first_attempt.get(pos)
        if first is None and pos == self.cursor:
            self.first_attempt[pos] = {"ok": False, "code": decision.code,
                                       "action_index": action_index}
        if self.first_error is None:
            self.first_error = {"position": pos,
                                "name": P.CANONICAL_POSITIONS[pos]
                                if 0 <= pos < len(P.CANONICAL_POSITIONS) else "",
                                "code": decision.code, "action_index": action_index}
        if decision.kind == RECOVERY_RECOVERABLE:
            self.recoverable_failures.append(
                {"position": pos, "code": decision.code,
                 "action_index": action_index,
                 "side_effect_status": decision.side_effect_status})

    def note_budget_stop(self, *, need: int, remaining: int) -> None:
        self.stop_note = (f"剩余 {remaining} 呼 < 闭合依赖所需 {need} 呼："
                          f"不发明知无法闭合的调用")

    # -- 报告 ------------------------------------------------------------
    def qualification(self, *, total_actions: int,
                      posts_cap: int) -> "dict[str, Any]":
        rows: "list[dict[str, Any]]" = []
        for i, name in enumerate(P.CANONICAL_POSITIONS):
            rec = self.first_attempt.get(i)
            rows.append({
                "position": i, "name": name,
                "first_attempt_ok": None if rec is None else bool(rec["ok"]),
                "first_code": "" if rec is None else str(rec.get("code") or ""),
                "first_action_index": None if rec is None
                else rec.get("action_index"),
            })
        attempted = [r for r in rows if r["first_attempt_ok"] is not None]
        ok_count = sum(1 for r in attempted if r["first_attempt_ok"])
        return {
            "schema": QUALIFICATION_SCHEMA,
            "positions": rows,
            "first_attempt_ok_count": ok_count,
            "attempted_positions": len(attempted),
            "first_attempt_ok_all": bool(attempted) and ok_count == len(attempted),
            "budgeted_completion": bool(self.completed and total_actions <= posts_cap),
            "total_posts": int(total_actions),
            "posts_cap": int(posts_cap),
            "recoverable_failures": list(self.recoverable_failures),
            "first_error": None if self.first_error is None
            else dict(self.first_error),
            "invalidations": list(self.invalidations),
            "milestones": [self.records[p].as_dict() for p in sorted(self.records)],
            "stop_note": self.stop_note,
            "note": ("小样本资格检查：只报首次尝试与最终完成两面，"
                     "不包装成稳定成功率，也不进入任何科学判据。"),
        }


__all__ = [
    "CODE_BUDGET_EXHAUSTED", "CODE_BUDGET_UNREACHABLE", "CODE_METHOD_CHANGE",
    "CODE_REPUBLISH_ONLY", "CODE_TRANSPORT", "CODE_UNCLASSIFIED",
    "CODE_UNKNOWN_EFFECT", "EFFECT_KNOWN_COMPLETE", "EFFECT_NONE",
    "EFFECT_UNKNOWN", "FailureEvidence", "INVALIDATE_FROM", "MilestoneRecord",
    "MilestoneState", "POSITION_ACTIONS", "POSITION_KINDS",
    "PRE_FREEZE_ONLY_CODES", "QUALIFICATION_SCHEMA", "RECOVERY_RECOVERABLE",
    "RECOVERY_REPUBLISH_ONLY", "RECOVERY_TERMINAL", "RecoveryDecision",
    "TRANSPORT_CODES", "classify_failure",
]
