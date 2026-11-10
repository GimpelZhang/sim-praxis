#!/usr/bin/env python3
"""RSI-R0.2 T3：结案生产入口预演（计划 §10 / AC19，零付费、离线、无容器）。

AC19：「所有状态入口可结案 | §10 每行 fixture 经真 CLI，尤其无 env/无 LOCK/
0trial/部分验证」。本脚本为 §10 的关键行各造一个**独立 campaign fixture**
（用生产 API 写槽账/状态/journal/计费，不自造旁路格式），然后对每个 fixture
调用**真实 CLI**：

    python -m orchestrator.rsi_r02.cli closeout --campaign-id <id> --root <base>

* 子进程环境把 ``SIMPRAXIS_*`` 与 ``ANTHROPIC_*`` 全部剥掉（无 env 路径、无凭据）；
* 除 ``approval_missing``（故意冻结 LOCK 以实测 G-P 批准门）外，fixture campaign
  内不存在任何 LOCK 文件（无 LOCK 路径）；
* 缺 campaign 上下文时 CLI 必须非零退出且**不生成**任何目录/补账。

判据只读 CLI stdout 的三轴 JSON，并与本表逐案核对；任何不符即 rc=1。
产物：``<control campaign>/t3_control/closeout_rehearsal.json``。

用法::

    /mnt/venvs/simpraxis/bin/python scripts/rsi_r02/t3_closeout_rehearsal.py \\
        --control-campaign r02_t3_control_<UTC> [--root <fixture base>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "rsi_r02")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import rsi_r02_root  # noqa: E402
from orchestrator.rsi_r02 import billing as B  # noqa: E402
from orchestrator.rsi_r02 import campaign as C  # noqa: E402
from orchestrator.rsi_r02 import closeout as CO  # noqa: E402
from orchestrator.rsi_r02 import stages as SG  # noqa: E402
from orchestrator.rsi_r02 import state as ST  # noqa: E402

import t3_control as T3C  # noqa: E402  （复用来源 campaign 与 228 槽位表）

SCHEMA_REPORT = "simpraxis.rsi_r02.t3_closeout_rehearsal.v1"


# ---------------------------------------------------------------------------
# fixture 构造（只用生产 API）
# ---------------------------------------------------------------------------


def _new_campaign(base: Path, cid: str, *, source: Path, plan: Path
                  ) -> C.CampaignContext:
    ctx = C.create_campaign(
        campaign_id=cid, plan_path=plan,
        consent_path=source / "contract" / "consent.json",
        repo_root=REPO_ROOT, root=base)
    C.write_json(ctx, "quota/slots.json", T3C.T0.build_slots())
    return ctx


def _trial(ctx: C.CampaignContext, spec: SG.TrialSpec, *, status: str,
           posts: int = 1, success: bool | None = True,
           slot_ok: bool = True) -> ST.TrialState:
    book = ST.SlotBook(ctx)
    ids = SG.slot_ids_for(book, spec)
    store = ST.StateStore(ctx)
    st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                     family=spec.family, phase=spec.phase,
                     slot_ids=ids, post_cap=spec.post_cap)
    store.transition(st, status=ST.TRIAL_RUNNING)
    for i, sid in enumerate(ids[:posts]):
        call = f"{spec.trial_id}-c{i + 1}"
        book.claim(sid, call_id=call, trial_id=spec.trial_id)
        if slot_ok:
            book.settle(sid, status=ST.SLOT_OK)
        # 预留先落盘（与生产同序）；结算交给 _settle_reservations
        B.BillingBook(ctx).reserve(call_id=call, slot_id=sid)
    st.posts_sent, st.turns = posts, posts
    if status == ST.TRIAL_SUBMITTED:
        st.independent_success = success
    store.save(st)
    return store.transition(st, status=status)


def _settle_reservations(ctx: C.CampaignContext, prefix: str) -> None:
    """把 fixture 的预留按 usage 结算（释放差额），保持 process 轴干净。"""
    book = B.BillingBook(ctx)
    for rec in book.records():
        if rec.slot_id.startswith(prefix) and rec.status == B.STATUS_PENDING:
            book.settle(call_id=rec.call_id, usage=B.Usage.normalize({
                "input_tokens": 1000, "output_tokens": 200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0}))


def _event(ctx: C.CampaignContext, *, trial_id: str, code: str,
           action: str | None = None, ok: bool = False) -> None:
    ST.Journal(ctx).append(kind="event", trial_id=trial_id, action=action,
                           ok=ok, code=code, message=f"fixture:{code}")


def _cancel_rest(ctx: C.CampaignContext, *, reason: str, phase: str) -> None:
    book = ST.SlotBook(ctx)
    for sid in book.ids(phase=phase):
        if book.status(sid) == ST.SLOT_UNUSED:
            book.cancel(sid, reason=reason)


# ---------------------------------------------------------------------------
# 各案构造：每案返回 (cid, expectations)
# ---------------------------------------------------------------------------


def case_zero_trial(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    _new_campaign(base, cid, **kw)
    return {"process": "COMPLETE", "measurement": "INCOMPLETE",
            "capability": "NOT_MEASURED",
            "reasons_include": {"measurement": ["NO_TRIAL_STARTED"]}}


def case_partial_validation(base: Path, cid: str, **kw: Any
                            ) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    for spec in SG.dev_specs():
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    for spec in SG.validation_specs()[:3]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "INCONCLUSIVE_COVERAGE",
            "reasons_include": {"capability": ["3/6"]}}


def case_full_nine(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "CEILING", "reasons_include": {}}


def case_dev_blocked(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    _trial(ctx, specs[0], status=ST.TRIAL_BLOCKED, success=None)
    _settle_reservations(ctx, specs[0].slot_prefix)
    for spec in specs[1:]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]}}


def case_input_limit(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    for spec in specs:
        if spec.family == "R":
            _trial(ctx, spec, status=ST.TRIAL_BLOCKED_INPUT_LIMIT, posts=0,
                   success=None)
        else:
            _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
            _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY", "reasons_include": {}}


def case_protocol_fail(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    _event(ctx, trial_id="protocol", code=ST.EV_PROTOCOL_FAIL,
           action="analyze")
    book = ST.SlotBook(ctx)
    for sid in book.ids(phase="protocol"):
        book.cancel(sid, reason="协议失败：G-D 不补考")
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        _cancel_rest(ctx, reason="协议失败：能力槽取消（保留分母）",
                     phase=spec.phase)
    return {"process": "COMPLETE", "measurement": "INCOMPLETE",
            "capability": "NOT_MEASURED",
            "reasons_include": {"measurement": ["NO_TRIAL_STARTED"]},
            "slots_cancelled_min": 228}


def case_unknown_sent(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    st = _trial(ctx, spec, status=ST.TRIAL_UNKNOWN_SENT, posts=1,
                success=None, slot_ok=False)
    # 不结算：残留预留正是本行要覆盖的事实（缺 usage ⇒ UNKNOWN + 保留预留）
    call = f"{spec.trial_id}-c1"
    B.BillingBook(ctx).mark_unknown(call_id=call, failure="fixture:unknown_sent")
    ST.SlotBook(ctx).settle(st.slot_ids[0], status=ST.SLOT_UNKNOWN,
                            reason="发送状态不明")
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_UNKNOWN_SENT, action="post")
    return {"process": "CLOSEOUT_INCOMPLETE", "measurement": "INCOMPLETE",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"process": ["UNSETTLED_COST"],
                                "measurement": ["UNSETTLED_COST"]}}


def case_freeze_invalid(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    _trial(ctx, spec, status=ST.TRIAL_INVALID, success=None)
    _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_FREEZE_INVALID)
    return {"process": "COMPLETE", "measurement": "INVALID_INSTRUMENT",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"measurement": ["TRIAL_INVALID"]}}


def case_freeze_semantics(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10「LOCK 语义改变 ⇒ FREEZE_INVALID」：无 INVALID trial，仅事件。"""
    ctx = _new_campaign(base, cid, **kw)
    _event(ctx, trial_id="protocol", code=ST.EV_FREEZE_INVALID)
    return {"process": "COMPLETE", "measurement": "INVALID_INSTRUMENT",
            "capability": "NOT_MEASURED",
            "reasons_include": {"measurement": ["FREEZE_INVALID"]}}


def case_budget_hit(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    ctx = _new_campaign(base, cid, **kw)
    for spec in SG.dev_specs():
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id="campaign", code=ST.EV_BUDGET_HIT)
    for phase in ("validation", "protocol"):
        _cancel_rest(ctx, reason="BLOCKED_BUDGET：触帽取消剩余槽（保留分母）",
                     phase=phase)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "INCONCLUSIVE_COVERAGE",
            "reasons_include": {},
            "slots_cancelled_min": 152, "budget_hit_recorded": True}


def case_paused(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """非污染原因的暂停：进程轴停住，测量轴仍是「没跑完」而不是仪器坏。

    评审 m7：``pause`` 自 M4 起会先写 journal 决策事件，暂停码直接进
    ``journal.codes``。原来这里借 ``EV_CONTAMINATION`` 当普通暂停码，会被
    测量轴正确判成 CONTAMINATED（新行为），故改用 ``EV_UNKNOWN_SENT`` 表达
    「发送状态不明而暂停」——这才是普通暂停的真实语义。
    """
    ctx = _new_campaign(base, cid, **kw)
    ST.CampaignState(ctx).pause(code=ST.EV_UNKNOWN_SENT,
                                reason="fixture:paused")
    return {"process": "CLOSEOUT_INCOMPLETE", "measurement": "INCOMPLETE",
            "capability": "NOT_MEASURED",
            "reasons_include": {"process": ["CAMPAIGN_PAUSED"]}}


def case_contamination_pause(base: Path, cid: str, **kw: Any
                             ) -> "dict[str, Any]":
    """污染触发的暂停：M4 之后暂停码进 codes，测量轴必须判 CONTAMINATED。

    这是 M4 真正要覆盖的生产路径——没有这条事件，「运行时因污染暂停」的
    campaign 结案时会落 INCOMPLETE 而非 CONTAMINATED（仪器/安全结论被吞）。
    """
    ctx = _new_campaign(base, cid, **kw)
    ST.CampaignState(ctx).pause(code=ST.EV_CONTAMINATION,
                                reason="fixture:污染触发暂停")
    return {"process": "CLOSEOUT_INCOMPLETE", "measurement": "CONTAMINATED",
            "capability": "NOT_MEASURED",
            "reasons_include": {"process": ["CAMPAIGN_PAUSED"],
                                "measurement": ["CONTAMINATION"]}}


# ---------------------------------------------------------------------------
# §10.2 余下各行（评审 H3：AC19 要求「每行 fixture 经真 CLI」）
# ---------------------------------------------------------------------------


def case_approval_missing(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行1「无 G0/安全/价格证据 ⇒ NOT_STARTED，早停，无 POST」。

    真入口是 ``cli run --execute-paid``。评审 N1：若 fixture 干脆没有 LOCK，
    ``run`` 会先在 FREEZE_INVALID 门就拒，**G-P 批准门一次都没被实测过**。
    所以这里把生产 campaign 的 7 件阶段证据 + spend 合同复制进 fixture，
    真签真冻结一份 LOCK（两签为不同 fixture 签署人），让拒绝**发生在
    G-P 批准门**并记录逐字拒绝原因。

    先有鸡还是先有蛋：``closeout_rehearsal.json`` 本身就是本预演的产物，
    首次运行时生产 campaign 里还没有它。缺的件用**显式占位件**补齐（只存在
    于该 fixture，逐条记进 ``evidence_seeded``，绝不冒充生产证据），否则
    ``LK.build_candidate`` 会因缺件拒签、G-P 门一次都走不到。
    """
    ctx = _new_campaign(base, cid, **kw)
    from orchestrator.rsi_r02 import lock as LK

    src = Path(kw["source"])
    rels = list(LK.EVIDENCE_FILES.values()) + ["contract/spend_contract.json"]
    seeded: "list[str]" = []
    for rel in rels:
        dst = ctx.relative(rel)
        ensure_private_dir(dst.parent)
        srcp = src / rel
        if srcp.is_file():
            data = srcp.read_bytes()
        else:
            seeded.append(rel)
            data = (json.dumps(
                {"schema": "simpraxis.rsi_r02.fixture_placeholder.v1",
                 "rel": rel,
                 "note": "结案预演 fixture 占位：只为 LOCK 可组装，非生产证据"},
                ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        dst.write_bytes(data)
        os.chmod(dst, 0o600)
    LK.write_candidate(ctx)
    LK.sign(ctx, role="reviewer", signer="t3-rehearsal-reviewer",
            statement="fixture：只为把 run 推进到批准门")
    LK.sign(ctx, role="verifier", signer="t3-rehearsal-verifier",
            statement="fixture：只为把 run 推进到批准门")
    LK.freeze(ctx)
    return {"process": "COMPLETE", "measurement": "INCOMPLETE",
            "capability": "NOT_MEASURED",
            "reasons_include": {"measurement": ["NO_TRIAL_STARTED"]},
            "expect_run_refused": True,
            "expect_lock_frozen": True,
            "expect_refusal_mentions": "G-P",
            "evidence_seeded": seeded}


def case_asset_mismatch(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行2「旧资产 hash 失配/参考失败 ⇒ INVALID，停全部」→ 仪器无效。"""
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    _trial(ctx, spec, status=ST.TRIAL_INVALID, success=None)
    _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_ASSET_MISMATCH)
    return {"process": "COMPLETE", "measurement": "INVALID_INSTRUMENT",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"measurement": ["TRIAL_INVALID"]}}


def case_malformed_action(base: Path, cid: str, **kw: Any
                          ) -> "dict[str, Any]":
    """§10.2 行4「候选 JSON 错而还在帽内 ⇒ 可见错误，下轮自修，不停」。

    结构化错误计一次 POST，trial 继续并最终提交；结案不得因此判仪器无效。
    """
    ctx = _new_campaign(base, cid, **kw)
    for spec in SG.dev_specs():
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=SG.dev_specs()[0].trial_id,
           code=ST.EV_MALFORMED_ACTION, action="parse")
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "INCONCLUSIVE_COVERAGE",
            "reasons_include": {"capability": ["0/6"]}}


def case_call_failed(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行5「明确已响应的调用失败 ⇒ trial BLOCKED，其余照跑」。"""
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    _trial(ctx, specs[0], status=ST.TRIAL_BLOCKED, success=None)
    _settle_reservations(ctx, specs[0].slot_prefix)
    _event(ctx, trial_id=specs[0].trial_id, code=ST.EV_CALL_FAILED, action="post")
    for spec in specs[1:]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]}}


def case_valid_fail_cap(base: Path, cid: str, **kw: Any
                        ) -> "dict[str, Any]":
    """§10.2 行7「有效响应耗尽 24/工具帽 ⇒ VALID_FAIL_CAP，不混作网络故障」。"""
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    _trial(ctx, specs[0], status=ST.TRIAL_VALID_FAIL_CAP, success=False)
    _settle_reservations(ctx, specs[0].slot_prefix)
    _event(ctx, trial_id=specs[0].trial_id, code=ST.EV_VALID_FAIL_CAP)
    for spec in specs[1:]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]}}


def case_policy_violation(base: Path, cid: str, **kw: Any
                          ) -> "dict[str, Any]":
    """§10.2 行8「越权被正确拦截 ⇒ VALID_FAIL_POLICY，停该 trial，不叫泄密」。"""
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    _trial(ctx, specs[0], status=ST.TRIAL_VALID_FAIL_POLICY, success=False)
    _settle_reservations(ctx, specs[0].slot_prefix)
    _event(ctx, trial_id=specs[0].trial_id, code=ST.EV_POLICY_VIOLATION,
           action="run_experiment")
    for spec in specs[1:]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]}}


def case_contamination(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行9「实际泄密/答案污染 ⇒ INVALID，停全部，不以其他题补齐」。"""
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    _trial(ctx, spec, status=ST.TRIAL_INVALID, success=None)
    _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_CONTAMINATION,
           action="analyze")
    _cancel_rest(ctx, reason="污染：停全部（§10.2 STOP_ALL）", phase="validation")
    return {"process": "COMPLETE", "measurement": "CONTAMINATED",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"measurement": ["CONTAMINATION"]}}


def case_assisted(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行10「人工代写关键部分 ⇒ ASSISTED，停止独立成功计数」。"""
    ctx = _new_campaign(base, cid, **kw)
    specs = SG.dev_specs()
    st = _trial(ctx, specs[0], status=ST.TRIAL_SUBMITTED)
    _settle_reservations(ctx, specs[0].slot_prefix)
    ST.mark_assisted(ST.StateStore(ctx), ST.Journal(ctx), st,
                     reason="fixture：维护者代写了关键分析段落")
    for spec in specs[1:]:
        _trial(ctx, spec, status=ST.TRIAL_SUBMITTED)
        _settle_reservations(ctx, spec.slot_prefix)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]},
            "assisted_recorded": True}


def case_tool_outcome_unknown(base: Path, cid: str, **kw: Any
                              ) -> "dict[str, Any]":
    """§10.2 行12「工具结果不确定 ⇒ trial 停、暂停相关资源」。"""
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    _trial(ctx, spec, status=ST.TRIAL_TOOL_OUTCOME_UNKNOWN, success=None)
    _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_TOOL_OUTCOME_UNKNOWN,
           action="run_experiment")
    ST.CampaignState(ctx).pause(code=ST.EV_TOOL_OUTCOME_UNKNOWN,
                                reason="fixture:tool_outcome_unknown")
    return {"process": "CLOSEOUT_INCOMPLETE", "measurement": "INCOMPLETE",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"process": ["CAMPAIGN_PAUSED"],
                                "measurement": ["TOOL_OUTCOME_UNKNOWN"]}}


def case_aborted(base: Path, cid: str, **kw: Any) -> "dict[str, Any]":
    """§10.2 行15「人工终止/不可恢复中断 ⇒ ABORTED，取消剩余槽，不补考」。"""
    ctx = _new_campaign(base, cid, **kw)
    spec = SG.dev_specs()[0]
    _trial(ctx, spec, status=ST.TRIAL_ABORTED, success=None)
    _settle_reservations(ctx, spec.slot_prefix)
    _event(ctx, trial_id=spec.trial_id, code=ST.EV_ABORTED, action="resume")
    for phase in ("validation", "protocol"):
        _cancel_rest(ctx, reason="ABORTED：取消剩余槽（§10.2 CANCEL_REST）",
                     phase=phase)
    return {"process": "COMPLETE", "measurement": "VALID",
            "capability": "DEVELOPMENT_NOT_READY",
            "reasons_include": {"capability": ["G-V 未通过"]}}


CASES = {
    "zero_trial": case_zero_trial,
    "partial_validation": case_partial_validation,
    "full_nine": case_full_nine,
    "dev_blocked": case_dev_blocked,
    "input_limit": case_input_limit,
    "protocol_fail": case_protocol_fail,
    "unknown_sent": case_unknown_sent,
    "freeze_invalid": case_freeze_invalid,
    "freeze_semantics": case_freeze_semantics,
    "budget_hit": case_budget_hit,
    "paused": case_paused,
    "contamination_pause": case_contamination_pause,
    # §10.2 余下各行（H3）
    "approval_missing": case_approval_missing,
    "asset_mismatch": case_asset_mismatch,
    "malformed_action": case_malformed_action,
    "call_failed": case_call_failed,
    "valid_fail_cap": case_valid_fail_cap,
    "policy_violation": case_policy_violation,
    "contamination": case_contamination,
    "assisted": case_assisted,
    "tool_outcome_unknown": case_tool_outcome_unknown,
    "aborted": case_aborted,
}


# ---------------------------------------------------------------------------
# 真 CLI
# ---------------------------------------------------------------------------


def _run_cli_raw(campaign_id: str, base: Path, *argv: str
                 ) -> "subprocess.CompletedProcess[str]":
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SIMPRAXIS_", "ANTHROPIC_"))}
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.rsi_r02.cli",
         "--campaign-id", campaign_id, "--root", str(base), *argv],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=300)


def run_cli(campaign_id: str, base: Path) -> "dict[str, Any]":
    proc = _run_cli_raw(campaign_id, base, "closeout")
    verdict = None
    if proc.returncode == 0:
        verdict = json.loads(proc.stdout)
    return {"returncode": proc.returncode,
            "env_keys_stripped": sorted(k for k in os.environ
                                        if k.startswith(("SIMPRAXIS_",
                                                         "ANTHROPIC_"))),
            "stdout_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(),
            "stderr_tail": proc.stderr[-800:], "judgement": verdict}


def _flatten(verdict: Mapping[str, Any]) -> "dict[str, str]":
    return {"process": verdict["process"]["status"],
            "measurement": verdict["measurement"]["status"],
            "capability": verdict["capability"]["status"]}


def _reasons(verdict: Mapping[str, Any]) -> "dict[str, list[str]]":
    out = {"process": list(verdict["process"]["reasons"]),
           "measurement": list(verdict["measurement"]["reasons"])}
    cap = verdict.get("capability") or {}
    if cap.get("reason"):
        out["capability"] = [str(cap["reason"])]
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.2 T3 结案预演（AC19）")
    ap.add_argument("--control-campaign", required=True)
    ap.add_argument("--root", default=None,
                    help="fixture 用 campaign 基目录（默认 <rsi_r02_root>/t3_rehearsal_<UTC>）")
    ap.add_argument("--source-campaign", default=None,
                    help="生产 campaign（默认按 runtime_paths 解析，不猜旧 id）")
    ap.add_argument("--plan",
                    default=str(Path.home() / "文档" / "RSI_R0.2_Plan_detailed.md"))
    args = ap.parse_args(argv)

    stamp = utc_now_iso_z().replace("-", "").replace(":", "")
    base = (Path(args.root) if args.root
            else rsi_r02_root() / f"t3_rehearsal_{stamp}")
    base.mkdir(parents=True, exist_ok=True)
    source = (Path(args.source_campaign) if args.source_campaign
              else T3C.default_source_campaign())
    plan = Path(args.plan)
    ctrl = C.load_campaign(campaign_id=args.control_campaign,
                           root=rsi_r02_root())

    results: "dict[str, Any]" = {}
    failures: "list[str]" = []
    for name, build in CASES.items():
        cid = f"rb_{name}_{stamp}"
        exp = build(base, cid, source=source, plan=plan)
        camp_root = base / cid
        # 评审 m6：原来只认 ``LOCK*`` 前缀，而真正的冻结件叫
        # ``contract/final_lock.json``——守卫对真 LOCK 视而不见。改为判定
        # **LOCK 件所在目录**（contract/）里文件名含 ``lock`` 的件；
        # approval_missing 一案**故意**冻结，单独放行。
        #
        # 不能对全树按名字含 lock 判：状态机自己的 flock 文件叫
        # ``quota/.slots.lock`` / ``trials/.journal.lock``，那是运行期互斥锁，
        # 不是 LOCK 件——按名字全树判会把每个 fixture 都误报成「含 LOCK」。
        lock_files = [str(p.relative_to(camp_root))
                      for p in camp_root.rglob("*")
                      if p.is_file() and "lock" in p.name.lower()
                      and str(p.relative_to(camp_root)).startswith("contract/")]
        run = run_cli(cid, base)
        rec: "dict[str, Any]" = {"campaign_id": cid, "root": str(camp_root),
                                 "lock_files": lock_files, "expect": exp,
                                 "run": run, "problems": []}
        if exp.get("expect_run_refused"):
            # §10.2 行1 的真入口：无批准件/LOCK 的 ``run --execute-paid`` 必须
            # 拒绝（rc=2）且**一个 trial 都不开跑**（零 POST 不靠自觉）。
            refusal = _run_cli_raw(cid, base, "run", "--phase", "dev",
                                   "--execute-paid")
            trials_dir = camp_root / "trials"
            started = (sorted(str(p.relative_to(camp_root)) for p in
                              trials_dir.glob("*/state.json"))
                       if trials_dir.is_dir() else [])
            rec["run_refusal"] = {"returncode": refusal.returncode,
                                  "stdout_tail": refusal.stdout[-600:],
                                  "stderr_tail": refusal.stderr[-400:],
                                  "trials_started": started}
            if refusal.returncode != 2:
                rec["problems"].append(
                    f"无批准却 run rc={refusal.returncode}（应拒绝 2）")
            if started:
                rec["problems"].append(f"无批准却开跑 trial：{started}")
            # 评审 N1：拒绝必须发生在**批准门**——理由里要出现 G-P，且不能是
            # FREEZE_INVALID（那说明 LOCK 没冻上，批准门根本没被走到）。
            frozen = exp.get("expect_lock_frozen")
            if frozen and not lock_files:
                rec["problems"].append("期望已冻结 LOCK，实际无 LOCK 件")
            needle = str(exp.get("expect_refusal_mentions") or "")
            if needle and needle not in refusal.stdout:
                rec["problems"].append(
                    f"拒绝理由未提到 {needle!r}：{refusal.stdout[-300:]}")
            if frozen and "FREEZE_INVALID" in refusal.stdout:
                rec["problems"].append("拒绝发生在 FREEZE_INVALID 门，未走到批准门")
        if run["returncode"] != 0 or run["judgement"] is None:
            rec["problems"].append(f"CLI rc={run['returncode']}")
        else:
            got = _flatten(run["judgement"])
            reasons = _reasons(run["judgement"])
            for axis, want in (("process", exp["process"]),
                               ("measurement", exp["measurement"]),
                               ("capability", exp["capability"])):
                if got[axis] != want:
                    rec["problems"].append(f"{axis}: {got[axis]} != {want}")
            for axis, needles in (exp.get("reasons_include") or {}).items():
                for needle in needles:
                    if not any(needle in r for r in reasons.get(axis, [])):
                        rec["problems"].append(f"{axis} 缺理由 {needle!r}")
            if lock_files and not exp.get("expect_lock_frozen"):
                rec["problems"].append(f"fixture 含 LOCK 文件：{lock_files}")
            facts = json.loads(
                (camp_root / "closeout" / "facts.json").read_text("utf-8"))
            rec["facts"] = {
                "codes": facts["journal"]["codes"],
                "slots_cancelled": facts["slots_cancelled"]["count"],
                "billing": {k: facts["billing"].get(k) for k in
                            ("settled_usd", "pending_usd", "unknown_call_ids")},
                "capability": run["judgement"].get("capability"),
            }
            if "slots_cancelled_min" in exp:
                if facts["slots_cancelled"]["count"] < exp["slots_cancelled_min"]:
                    rec["problems"].append(
                        "取消槽数不足："
                        f"{facts['slots_cancelled']['count']}"
                        f" < {exp['slots_cancelled_min']}")
            if exp.get("budget_hit_recorded"):
                if not facts["journal"]["codes"].get("budget_hit"):
                    rec["problems"].append("BLOCKED_BUDGET 事件未入 codes")
        results[name] = rec
        if rec["problems"]:
            failures.extend(f"{name}: {p}" for p in rec["problems"])
        print(f"[rehearsal] {name}: rc={run['returncode']} "
              f"problems={len(rec['problems'])}", flush=True)

    # 缺件路径：无 campaign 上下文 ⇒ 非零退出且**不生成**目录（不补账、不建根）
    missing_id = f"rb_missing_{stamp}"
    missing_run = run_cli(missing_id, base)
    missing_problems = []
    if missing_run["returncode"] == 0:
        missing_problems.append("缺上下文却 rc=0")
    if (base / missing_id).exists():
        missing_problems.append("CLI 自行创建了 campaign 目录")
    results["missing_context"] = {"campaign_id": missing_id,
                                  "run": missing_run,
                                  "problems": missing_problems}
    failures.extend(f"missing_context: {p}" for p in missing_problems)
    print(f"[rehearsal] missing_context: rc={missing_run['returncode']} "
          f"problems={len(missing_problems)}", flush=True)

    report = {
        "schema": SCHEMA_REPORT, "recorded_at_utc": utc_now_iso_z(),
        "control_campaign": args.control_campaign,
        "fixture_base": str(base),
        "cases": results, "failures": failures, "ok": not failures,
        "note": ("全部 fixture 经真 CLI（closeout 子命令），环境已剥离 "
                 "SIMPRAXIS_*；除 approval_missing（故意冻结 LOCK 以实测 G-P "
                 "批准门）外 fixture 内无 LOCK；缺上下文案不生成任何目录。"
                 "approval_missing 的 evidence_seeded 列出以占位件补齐的"
                 "证据（首次运行时尚无本预演报告），占位件只存在于该 fixture。"),
    }
    C.write_json(ctrl, "t3_control/closeout_rehearsal.json", report)
    print(json.dumps({"cases": len(CASES) + 1, "failures": failures,
                      "ok": report["ok"]}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
