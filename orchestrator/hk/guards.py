"""八守卫：一等变异面（WP-C″ 的进化素材），每条编译自真实事故（设计族 §3）。

统一形态：`g_x(ctx)` 违规即 `raise GuardViolation`，通过静默。每守卫恰一个职责，
不共享实现（S0-3 完成判据）。负控名称 = 事故出处，见 tests/milestoneS0/test_hk_guards.py。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ._physicalrsi import mismatches

__all__ = ["GuardViolation", "GuardCtx", "GUARDS",
           "g_prereg", "g_budget", "g_send_once", "g_freeze",
           "g_bytes", "g_claim", "g_unit", "g_noprog", "enforce"]


class GuardViolation(RuntimeError):
    """守卫拒绝继续（fail-closed）。`code` 是机器判据，`detail` 是人类可读证据。"""

    def __init__(self, guard: str, code: str, detail: str):
        self.guard = guard
        self.code = code
        self.detail = detail
        super().__init__(f"{guard}:{code}: {detail}")


@dataclass
class GuardCtx:
    """守卫输入面。计划骨架字段在前；`扩展`段是各守卫负控所必需的最小补集。"""

    stage: str = ""
    registered_spec_sha: str | None = None
    remaining_budget: int = 0
    sent_slots: set[str] = field(default_factory=set)
    frozen: bool = False
    request_bytes: bytes | None = None
    claim_doc: dict | None = None
    host_recomputed: dict | None = None
    unit_pairs: list[tuple] = field(default_factory=list)
    round_no_new_info: int = 0
    # —— 扩展（守卫输入所必需）——
    required_actions: int = 0                 # g_budget：本阶段还需发出/执行的逻辑动作数
    slot: str | None = None                   # g_send_once：本次拟发送的槽
    mutation: str | None = None               # g_freeze：冻结后拟施加的变更描述
    registered_request_sha256: str | None = None  # g_bytes：登记在案的请求字节哈希
    private_sentinels: tuple[bytes, ...] = ()     # g_bytes：不得外泄的私有哨兵
    noprog_rounds: int = 2                    # g_noprog：连续无新证据轮数上限


def g_prereg(ctx: GuardCtx) -> None:
    """G-PREREG（→K1）：spec 未登记/未哈希即拒——发实验前结构性不可达。"""
    sha = ctx.registered_spec_sha
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise GuardViolation("g_prereg", "SPEC_UNREGISTERED",
                             f"stage={ctx.stage!r} registered_spec_sha={sha!r} 不是已登记 sha256")


def g_budget(ctx: GuardCtx) -> None:
    """G-BUDGET（→K3）：剩余容量不足即判，不发网（R1-N 发送前检查）。"""
    if ctx.remaining_budget < ctx.required_actions:
        raise GuardViolation("g_budget", "BUDGET_UNREACHABLE",
                             f"第 {ctx.stage!r} 阶段剩余 {ctx.remaining_budget} "
                             f"< 还需 {ctx.required_actions} 个动作")


def g_send_once(ctx: GuardCtx) -> None:
    """G-SEND-ONCE（K3/K7 内）：每槽一次发送；未知发送即终止，不隐式重试。"""
    if not ctx.slot:
        raise GuardViolation("g_send_once", "UNKNOWN_SEND",
                             f"无法归属的一次性槽发送（slot={ctx.slot!r}）")
    if ctx.slot in ctx.sent_slots:
        raise GuardViolation("g_send_once", "SLOT_ALREADY_SENT",
                             f"槽 {ctx.slot!r} 已发送过；禁止二发/隐式重试")


def g_freeze(ctx: GuardCtx) -> None:
    """G-FREEZE（→K7）：ConfirmPlan 落盘后任何方法/源码变更 = 终止项（R1-N）。"""
    if ctx.frozen and ctx.mutation:
        raise GuardViolation("g_freeze", "FROZEN_MUTATION",
                             f"冻结后仍提出变更：{ctx.mutation!r}（stage={ctx.stage!r}）")


def g_bytes(ctx: GuardCtx) -> None:
    """G-BYTES（K1→K6 各边）：发送前逐字节审计——数值同源、私有哨兵不外泄。"""
    if ctx.request_bytes is None:
        raise GuardViolation("g_bytes", "BYTES_MISMATCH",
                             f"stage={ctx.stage!r} 缺请求字节，无法审计")
    if not ctx.registered_request_sha256:
        raise GuardViolation("g_bytes", "BYTES_MISMATCH",
                             f"stage={ctx.stage!r} 未登记请求字节哈希")
    actual = hashlib.sha256(ctx.request_bytes).hexdigest()
    if actual != ctx.registered_request_sha256:
        raise GuardViolation("g_bytes", "BYTES_MISMATCH",
                             f"stage={ctx.stage!r} 请求字节 {actual[:12]}… != "
                             f"登记 {ctx.registered_request_sha256[:12]}…")
    for sentinel in ctx.private_sentinels:
        if sentinel and sentinel in ctx.request_bytes:
            raise GuardViolation("g_bytes", "PRIVATE_LEAK",
                                 f"stage={ctx.stage!r} 请求字节含私有哨兵 "
                                 f"{sentinel[:16]!r}（长度 {len(sentinel)}）")


def _claim_diffs(claimed, host, path: str, out: list[str]) -> None:
    if isinstance(host, dict):
        if not isinstance(claimed, dict):
            out.append(f"{path}: 类型 {type(claimed).__name__} != dict")
            return
        for key, host_value in host.items():
            if key not in claimed:
                out.append(f"{path}.{key}: 缺字段（host={host_value!r}）")
                continue
            _claim_diffs(claimed[key], host_value, f"{path}.{key}", out)
        return
    if isinstance(host, (int, float)) and not isinstance(host, bool):
        if not isinstance(claimed, (int, float)) or isinstance(claimed, bool):
            out.append(f"{path}: {claimed!r} 不是数值（host={host!r}）")
        elif claimed != host:  # 自报必须与 host 重算逐字段相符（R1.3 转写错负控）
            out.append(f"{path}: 自报 {claimed!r} != host {host!r}")
        return
    if claimed != host:
        out.append(f"{path}: 自报 {claimed!r} != host {host!r}")


def g_claim(ctx: GuardCtx) -> None:
    """G-CLAIM（→K8）：自报与 host 重算逐字段比对；转写不符即 CLAIM_INVALID。"""
    if ctx.claim_doc is None or ctx.host_recomputed is None:
        raise GuardViolation("g_claim", "CLAIM_INVALID",
                             "缺 claim 文档或 host 重算值，不能放行主张")
    diffs: list[str] = []
    _claim_diffs(ctx.claim_doc, ctx.host_recomputed, "$", diffs)
    if diffs:
        raise GuardViolation("g_claim", "CLAIM_INVALID",
                             "自报与 host 重算不符：" + "; ".join(diffs[:5]))


def g_unit(ctx: GuardCtx) -> None:
    """G-UNIT（所有边）：Contract mismatches 结构性拦截，禁显式 adapt 洗白。"""
    for index, pair in enumerate(ctx.unit_pairs):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise GuardViolation("g_unit", "UNIT_MISMATCH",
                                 f"unit_pairs[{index}] 不是二元组：{pair!r}")
        producer, consumer = pair
        if hasattr(producer, "name") and hasattr(consumer, "name"):
            problems = mismatches(producer, consumer)
            if problems:
                raise GuardViolation("g_unit", "UNIT_MISMATCH",
                                     f"edge[{index}]: " + "; ".join(problems))
            continue
        if consumer not in ("", None) and producer != consumer:
            raise GuardViolation("g_unit", "UNIT_MISMATCH",
                                 f"edge[{index}]: producer {producer!r} != consumer {consumer!r}")


def g_noprog(ctx: GuardCtx) -> None:
    """G-NOPROG（K4↔K5 环）：连续 N 轮无新证据/无代码推进即强制转诊断或停。"""
    if ctx.round_no_new_info >= ctx.noprog_rounds:
        raise GuardViolation("g_noprog", "NO_PROGRESS",
                             f"连续 {ctx.round_no_new_info} 轮无新证据（上限 "
                             f"{ctx.noprog_rounds}）；转诊断或停")


GUARDS = {
    "G-PREREG": g_prereg,
    "G-BUDGET": g_budget,
    "G-SEND-ONCE": g_send_once,
    "G-FREEZE": g_freeze,
    "G-BYTES": g_bytes,
    "G-CLAIM": g_claim,
    "G-UNIT": g_unit,
    "G-NOPROG": g_noprog,
}


def enforce(names, ctx: GuardCtx) -> None:
    """按名批量执行守卫；任一违规即抛出（顺序失败即停，不留半放行状态）。"""
    for name in names:
        try:
            guard = GUARDS[name]
        except KeyError:
            raise GuardViolation("enforce", "UNKNOWN_GUARD", f"未登记守卫 {name!r}") from None
        guard(ctx)
