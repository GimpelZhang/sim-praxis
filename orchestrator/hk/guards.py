"""九守卫：一等变异面（WP-C″ 的进化素材），每条编译自真实事故（设计族 §3）。

统一形态：`g_x(ctx)` 违规即 `raise GuardViolation`，通过静默。每守卫恰一个职责，
不共享实现（S0-3 完成判据）。负控名称 = 事故出处，见 tests/milestoneS0/test_hk_guards.py。

S2b 维护侧合并：G-REPORT（S2 候选守卫）+ NC 交叉校验（F2）入维护面。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ._physicalrsi import mismatches

__all__ = ["GuardViolation", "GuardCtx", "GUARDS",
           "g_prereg", "g_budget", "g_send_once", "g_freeze",
           "g_bytes", "g_claim", "g_unit", "g_noprog", "g_report", "g_lock_rev",
           "register_guard", "enforce"]


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
    report_doc: dict | None = None            # g_report：K8 六段报告（v2 合同）
    nc_evidence: dict | None = None           # g_report F2：NC 证据（交叉校验用）
    # —— S2c G-LOCK-REV：LOCK 冻结后触及设计面的改动必须带递增 rev + 重签 ——
    lock_rev: int = 0                         # 当前已登记的 LOCK 修订号
    design_change: str | None = None          # 冻结后拟施加的设计面变更描述
    proposed_lock_rev: int = 0                # 该变更对应的新修订号（须 > lock_rev）
    lock_resigned: bool = False               # 该变更是否已重签


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


#: negative_control_handling 段必须触及 NC 有效性判据的内容锚点。
_NC_ANCHORS = ("非退化", "亚阈", "灵敏度", "non-degenerate", "sub-threshold",
               "sensitivity", "有效性", "validity")
#: limitations_next_steps 段必须给出方向性下一步。
_NEXT_ANCHORS = ("下一步", "后续", "未来", "next", "follow", "future", "计划", "拟")


def _mentions(text: str, anchors: "tuple[str, ...]") -> bool:
    lowered = text.lower()
    return any(anchor.lower() in lowered for anchor in anchors)


def g_report(ctx: GuardCtx) -> None:
    """G-REPORT（K8 出口）：六段报告合同 v2 的结构自检 + NC 交叉校验（S2b F2）。

    三层，都是结构面、不做内容评分：

    1. 六段存在且非空（委托冻结的 ``report_contract``，缺段即 ``REPORT_INCOMPLETE``）；
    2. 两个 S1 实际缺段的**内容锚点**：``negative_control_handling`` 必须触及
       NC 有效性判据（非退化/亚阈/灵敏度校准），``limitations_next_steps`` 必须
       给出方向性下一步。只写段名或空话不能过关。
    3. **NC 交叉校验**（S2b F2）：``negative_control_handling`` 段内容与
       ``nc_evidence`` 对账——证据在场而报告写"未注册/未验证" ⇒
       ``G-REPORT_NC_MISMATCH``（触发一次修复触点；二次仍不符 ⇒ 结构终止）。

    违规码直接交给 driver 的修复触点（``submit_claim_repair``）：缺段清单来自
    ``report_contract.missing_sections``，锚点/交叉校验问题由 ``detail`` 说明。
    """
    from orchestrator.hk import report_contract as RC

    try:
        sections = RC.validate_report(ctx.report_doc)
    except RC.ReportContractError as exc:
        raise GuardViolation("g_report", exc.code, exc.detail) from None
    if not _mentions(sections["negative_control_handling"], _NC_ANCHORS):
        raise GuardViolation(
            "g_report", "NC_HANDLING_UNANCHORED",
            "negative_control_handling 未处理有效性（非退化/亚阈/灵敏度校准），"
            "不能只说“无异常”")
    if not _mentions(sections["limitations_next_steps"], _NEXT_ANCHORS):
        raise GuardViolation(
            "g_report", "NEXT_STEPS_UNANCHORED",
            "limitations_next_steps 未给出方向性下一步（六段合同 v2）")
    # S2b F2：NC 交叉校验——报告段与 nc_evidence 对账
    nc_evidence = getattr(ctx, "nc_evidence", None)
    nc_problems = RC.crosscheck_nc_section(ctx.report_doc, nc_evidence)
    if nc_problems:
        raise GuardViolation(
            "g_report", "G-REPORT_NC_MISMATCH",
            "; ".join(nc_problems))


def g_lock_rev(ctx: GuardCtx) -> None:
    """G-LOCK-REV（S2c）：LOCK 冻结后触及**设计面**的改动必须带递增 rev + 重签。

    编译自 S2b 真实事故：LOCK（S2b-S0）冻结 19 个 SUT 文件后，实跑段 8 次仪器
    适配改了其中 8 个，LOCK 的 SUT hash 实际 stale 而无人重签——判件基于跑过的
    代码而非冻结点。本守卫把「冻结后改设计」形态化：
    * 无 design_change（本次运行不改设计面）→ 放行；
    * 有 design_change 但 proposed_lock_rev <= lock_rev → 拒绝（未递增）；
    * 有 design_change 且递增但未重签 → 拒绝（未重签）；
    * 有 design_change + rev 递增 + 已重签 → 放行（合法锁内适配）。
    """
    if ctx.design_change is None:
        return
    if ctx.proposed_lock_rev <= ctx.lock_rev:
        raise GuardViolation(
            "g_lock_rev", "FROZEN_DESIGN_CHANGE_NO_REV",
            f"冻结后改设计（{ctx.design_change!r}）但 lock_rev 未递增"
            f"（proposed={ctx.proposed_lock_rev} <= current={ctx.lock_rev}）")
    if not ctx.lock_resigned:
        raise GuardViolation(
            "g_lock_rev", "FROZEN_DESIGN_CHANGE_UNSIGNED",
            f"冻结后改设计（{ctx.design_change!r}）rev={ctx.proposed_lock_rev}"
            f" 递增但未重签（须重签两签后方可运行）")


GUARDS = {
    "G-PREREG": g_prereg,
    "G-BUDGET": g_budget,
    "G-SEND-ONCE": g_send_once,
    "G-FREEZE": g_freeze,
    "G-BYTES": g_bytes,
    "G-CLAIM": g_claim,
    "G-UNIT": g_unit,
    "G-NOPROG": g_noprog,
    "G-REPORT": g_report,   # S2b 维护侧合并（S2 候选 + F2 交叉校验）
    "G-LOCK-REV": g_lock_rev,   # S2c：冻结后改设计须递增 rev + 重签
}


def register_guard(name: str, fn) -> None:
    """S2 运行时注册：把**候选包**提供的守卫装进注册表（如 G-REPORT）。

    S1 的八守卫是 as-built 冻结面；S2 变异面新增的守卫由候选包实现，
    由战役在开跑前注册（v0 臂不注册 → 同名守卫不可用，fail-closed）。
    """
    if not name or not callable(fn):
        raise GuardViolation("register_guard", "UNKNOWN_GUARD",
                             f"非法注册 {name!r}: {fn!r}")
    GUARDS[name] = fn


def enforce(names, ctx: GuardCtx) -> None:
    """按名批量执行守卫；任一违规即抛出（顺序失败即停，不留半放行状态）。"""
    for name in names:
        try:
            guard = GUARDS[name]
        except KeyError:
            raise GuardViolation("enforce", "UNKNOWN_GUARD", f"未登记守卫 {name!r}") from None
        guard(ctx)
