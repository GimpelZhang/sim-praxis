"""RSI-R0.2 运行入口：dev/validation 固定顺序调度、恢复与结案（计划 §6.2/§6.3）。

三条纪律落在这里：

* **顺序不可跳**：``stages.specs_for`` 给定顺序，``next_trial`` 只挑未开跑的；
  任何已有终态的 trial 不会被再次选中。
* **零 POST 的入口**：``resume`` / ``status`` / ``closeout`` 不需要模型与凭据；
  ``run`` 必须显式 ``--execute-paid`` 且带 G-P 自批件与钉死分词器，否则拒绝。
* **不留无人后台**：每个 trial 的 POST 状态与工具进展都落 journal/state，
  进程死亡后 ``resume`` 只能把未闭合动作判成 ``TOOL_OUTCOME_UNKNOWN``。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from orchestrator.rsi_r02 import billing as B
from orchestrator.rsi_r02 import campaign as C
from orchestrator.rsi_r02 import candidate as CD
from orchestrator.rsi_r02 import closeout as CO
from orchestrator.rsi_r02 import messages as M
from orchestrator.rsi_r02 import stages as SG
from orchestrator.rsi_r02 import state as ST

EXIT_OK = 0
EXIT_REFUSED = 2

#: 这些终态之后仍可继续跑同阶段其他 trial（§10.2 CONTINUE_OTHERS）。
CONTINUE_STATUSES = frozenset({
    ST.TRIAL_SUBMITTED, ST.TRIAL_VALID_FAIL_CAP, ST.TRIAL_VALID_FAIL_POLICY,
    ST.TRIAL_BLOCKED, ST.TRIAL_BLOCKED_INPUT_LIMIT, ST.TRIAL_ASSISTED,
})
#: 这些终态/停因必须立刻停止本阶段（不自动重启、不补考）。
STOP_STATUSES = frozenset({
    ST.TRIAL_UNKNOWN_SENT, ST.TRIAL_TOOL_OUTCOME_UNKNOWN, ST.TRIAL_ABORTED,
    ST.TRIAL_INVALID,
})


class CliError(RuntimeError):
    """入口拒绝（配置/授权/状态不满足）；拒绝即零 POST。"""


# ---------------------------------------------------------------------------
# 真实资源构造（惰性导入；测试一律注入替身）
# ---------------------------------------------------------------------------


def make_trial(ctx: C.CampaignContext, spec: SG.TrialSpec, *,
               gpus: bool = False) -> Any:
    """同一条 TrialRunner 通路（T3 才证明它真能跑；这里只接线）。"""
    from orchestrator.rsi_r01.trial import TrialRunner

    return TrialRunner(campaign_root=ctx.root, instance_id=spec.instance_id,
                       trial_id=spec.trial_id, gpus=gpus)


def make_paid_caller(*, tokenizer_path: str, tokenizer_sha256: str
                     ) -> "tuple[CD.GatewayCaller, M.LocalCounter]":
    """唯一的付费入口：ModelGateway 单试、namespace 2902、钉死分词器。"""
    from orchestrator.common.runtime_paths import rsi_r02_gateway_state_path
    from orchestrator.gateway.quota_ledger import QuotaLedger
    from orchestrator.gateway.router import ModelGateway

    counter = M.LocalCounter.load(tokenizer_path, tokenizer_sha256)
    gateway = ModelGateway(ledger=QuotaLedger.rsi_r02(), max_attempts=1,
                           pause_state_path=rsi_r02_gateway_state_path())
    return CD.GatewayCaller(gateway=gateway), counter


# ---------------------------------------------------------------------------
# 授权与状态前置
# ---------------------------------------------------------------------------


def require_campaign_runnable(camp: ST.CampaignState) -> None:
    if camp.status == ST.CAMP_CLOSED:
        raise CliError("campaign 已结案：不得再跑（新增只能另立 campaign）")
    if camp.paused:
        raise CliError(f"campaign 处于全局暂停：{camp.pause_reason()}；"
                       "须外部可核验 resolution 才能解除")


def gp_approvals(ctx: C.CampaignContext) -> "list[dict[str, Any]]":
    """读 ``quota/approvals/*.json`` 里的 G-P 自批件（不生成、不猜）。"""
    root = ctx.root / "quota" / "approvals"
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CliError(f"G-P 批准件损坏：{path.name}: {exc}") from exc
        if doc.get("gate") == "G-P":
            doc["_path"] = str(path)
            out.append(doc)
    return out


def require_gp_approval(ctx: C.CampaignContext, *, posts: int,
                        cap_usd: float = B.USD_CAP) -> "dict[str, Any]":
    """G-P 是预算自批件：授权 POST 数 ≥ 本批、货币帽与账本一致。"""
    for doc in gp_approvals(ctx):
        authorized = doc.get("authorized_posts")
        if not isinstance(authorized, int):
            continue
        if authorized < posts:
            continue
        if float(doc.get("cap_usd", -1)) + 1e-9 < cap_usd:
            continue
        return doc
    raise CliError(
        f"没有覆盖 {posts} POST / {cap_usd:.2f} USD 的 G-P 自批件"
        "（quota/approvals/*.json，gate=G-P）；拒绝付费运行")


def require_gv(ctx: C.CampaignContext) -> "dict[str, Any]":
    """§6.3：仅 G-V（三族开发有效达标）通过后才可进入 validation。"""
    from orchestrator.rsi_r02 import billing as B2

    store = ST.StateStore(ctx)
    by_id = {}
    for spec in SG.dev_specs():
        try:
            by_id[spec.instance_id] = store.load(spec.trial_id)
        except ST.StateError:
            by_id[spec.instance_id] = None
    passed = all(
        st is not None and st.status == ST.TRIAL_SUBMITTED
        and st.independent_success is True and not st.assisted
        for st in by_id.values())
    detail = {sid: (None if st is None else st.status)
              for sid, st in by_id.items()}
    if not passed:
        raise CliError(f"G-V 未通过（{detail}）：不得启动 validation")
    summary = B2.BillingBook(ctx).summary()
    return {"gate": "G-V", "passed": True, "dev": detail,
            "settled_usd": summary["settled_usd"],
            "pending_usd": summary["pending_usd"]}


# ---------------------------------------------------------------------------
# 单 trial
# ---------------------------------------------------------------------------


def _summary(st: ST.TrialState, outcome: str) -> "dict[str, Any]":
    return {"trial_id": st.trial_id, "instance_id": st.instance_id,
            "family": st.family, "phase": st.phase, "outcome": outcome,
            "status": st.status, "reason_code": st.reason_code,
            "posts_sent": st.posts_sent,
            "posts_hidden_retry": st.posts_hidden_retry, "turns": st.turns,
            "assisted": st.assisted,
            "independent_success": st.independent_success}


def run_trial(ctx: C.CampaignContext, spec: SG.TrialSpec, *,
              call_model: Callable[[M.Request], CD.Reply],
              counter: Any, trial: Any = None, gpus: bool = False,
              config: "CD.LoopConfig | None" = None,
              campaign: "ST.CampaignState | None" = None,
              trial_factory: Callable[..., Any] = make_trial) -> "dict[str, Any]":
    """跑一个 trial；已有状态时严格走恢复语义，绝不重跑物理动作。"""
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    slots = ST.SlotBook(ctx)
    camp = campaign or ST.CampaignState(ctx)

    existing = None
    try:
        existing = store.load(spec.trial_id)
    except ST.StateError:
        pass

    if existing is not None:
        st = store.resume(existing, journal)
        if st.status in ST.TRIAL_TERMINAL:
            journal.append(kind="decision", trial_id=spec.trial_id,
                           action="resume", ok=True, code="ALREADY_TERMINAL",
                           message=f"已有终态 {st.status}；不重跑")
            return _summary(st, "already_terminal")
        if st.posts_sent == 0 and not journal.for_trial(spec.trial_id):
            # 零 POST 且零事件：没有未落盘的动作，重入同一 trial 是诚实的
            journal.append(kind="decision", trial_id=spec.trial_id,
                           action="resume", ok=True, code="RESUME_ZERO_POST",
                           message="进程中断但零 POST/零事件：从同一 trial 记录重入")
        else:
            store.transition(st, status=ST.TRIAL_ABORTED, code=ST.EV_ABORTED,
                             reason=(f"进程中断且已有 {st.posts_sent} 次发送/事件；"
                                     "轨迹不可重建，不重跑（分母保留）"))
            slots.cancel_unused(reason="ABORTED: 不可恢复中断（§10.2 CANCEL_REST）")
            journal.append(kind="decision", trial_id=spec.trial_id,
                           action="resume", ok=False, code=ST.EV_ABORTED,
                           message="不可恢复中断：取消剩余槽")
            return _summary(st, "aborted_unrecoverable")

    require_campaign_runnable(camp)
    slot_ids = SG.slot_ids_for(slots, spec)

    if existing is None:
        st = store.start(trial_id=spec.trial_id, instance_id=spec.instance_id,
                         family=spec.family, phase=spec.phase,
                         slot_ids=slot_ids, post_cap=spec.post_cap)
    else:
        st = existing
    if camp.status == ST.CAMP_NOT_STARTED:
        camp.start()

    if trial is None:
        trial = trial_factory(ctx, spec, gpus=gpus)
    public_doc = CD.load_public_doc(ctx, spec.instance_id)
    loop = CD.CandidateLoop(
        ctx=ctx, trial=trial, public_doc=public_doc, store=store, state=st,
        journal=journal, slots=slots, billing=B.BillingBook(ctx),
        counter=counter, call_model=call_model, slot_ids=slot_ids,
        campaign=camp,
        config=config or CD.LoopConfig(post_cap=spec.post_cap))
    return loop.run()


def _stop_of(out: Mapping[str, Any]) -> "str | None":
    status = out.get("status")
    if status in STOP_STATUSES:
        return f"trial {out.get('trial_id')} → {status}"
    return None


def run_phase(ctx: C.CampaignContext, phase: str, *,
              call_model: Callable[[M.Request], CD.Reply],
              counter: Any, trial_factory: Callable[..., Any] = make_trial,
              trial: Any = None, gpus: bool = False,
              config: "CD.LoopConfig | None" = None,
              gate: bool = True) -> "dict[str, Any]":
    """固定顺序跑完（或停住）一个阶段；返回逐 trial 结果与停因。"""
    specs = SG.specs_for(phase)
    if phase == "validation" and gate:
        require_gv(ctx)
    store = ST.StateStore(ctx)
    slots = ST.SlotBook(ctx)
    # 先整阶段预检：任何槽账不符都在零 POST 前拒绝
    for spec in SG.pending_trials(specs, store):
        SG.slot_ids_for(slots, spec)

    results: "list[dict[str, Any]]" = []
    stop: "str | None" = None
    for spec in specs:
        if SG.trial_status(store, spec) is not None:
            continue                      # 已有状态：终态跳过、非终态走恢复
        camp = ST.CampaignState(ctx)
        if camp.paused:
            stop = f"campaign_paused: {camp.pause_reason()}"
            break
        out = run_trial(ctx, spec, call_model=call_model, counter=counter,
                        trial=trial, gpus=gpus, config=config,
                        campaign=camp, trial_factory=trial_factory)
        results.append(out)
        stop = _stop_of(out)
        if stop is not None:
            break
        camp = ST.CampaignState(ctx)
        if camp.paused:
            stop = f"campaign_paused: {camp.pause_reason()}"
            break
    return {"phase": phase, "results": results, "stop_reason": stop,
            "completed": stop is None}


# ---------------------------------------------------------------------------
# 零 POST 命令
# ---------------------------------------------------------------------------


def resume_all(ctx: C.CampaignContext) -> "dict[str, Any]":
    """只做恢复判定：有未闭合 intent ⇒ TOOL_OUTCOME_UNKNOWN；零 POST。"""
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    out: "list[dict[str, Any]]" = []
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        try:
            st = store.load(spec.trial_id)
        except ST.StateError:
            continue
        before = st.status
        st = store.resume(st, journal)
        out.append({"trial_id": spec.trial_id, "before": before,
                    "after": st.status,
                    "reason": st.reason})
    return {"resumed": out, "pending_intents": len(journal.pending_intents())}


def assist_trial(ctx: C.CampaignContext, trial_id: str, reason: str
                 ) -> "dict[str, Any]":
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    st = store.load(trial_id)
    st = ST.mark_assisted(store, journal, st, reason=reason)
    return {"trial_id": trial_id, "status": st.status,
            "assisted": st.assisted,
            "independent_success": st.independent_success}


def clear_pause(ctx: C.CampaignContext, resolution: str) -> "dict[str, Any]":
    camp = ST.CampaignState(ctx)
    if not camp.paused:
        return {"cleared": False, "reason": "campaign 未处于暂停"}
    camp.clear_pause(resolution=resolution)
    billing = B.BillingBook(ctx)
    if billing.pause_reason() is not None:
        billing.clear_pause(resolution=resolution)
    return {"cleared": True, "resolution": resolution}


def status(ctx: C.CampaignContext) -> "dict[str, Any]":
    store = ST.StateStore(ctx)
    journal = ST.Journal(ctx)
    camp = ST.CampaignState(ctx)
    trials = []
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        st = SG.trial_status(store, spec)
        trials.append({"trial_id": spec.trial_id, "phase": spec.phase,
                       "family": spec.family, "status": st})
    try:
        slots: Any = ST.SlotBook(ctx).counts()
    except ST.StateError as exc:
        slots = {"error": str(exc)}
    return {"campaign_id": ctx.campaign_id,
            "campaign_status": camp.status,
            "pause": camp.pause_reason(),
            "slots": slots, "trials": trials,
            "journal_events": len(journal.events),
            "journal_integrity": journal.integrity(),
            "pending_intents": len(journal.pending_intents()),
            "billing": B.BillingBook(ctx).summary()}


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------


def _ctx(args: argparse.Namespace) -> C.CampaignContext:
    from orchestrator.common.runtime_paths import rsi_r02_campaign_id, rsi_r02_root

    cid = args.campaign_id or rsi_r02_campaign_id()
    base = Path(args.root) if args.root else rsi_r02_root()
    return C.load_campaign(campaign_id=cid, root=base)


def _emit(obj: Any, *, as_json: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
          if as_json else obj)


def _cmd_status(args: argparse.Namespace) -> int:
    _emit(status(_ctx(args)), as_json=True)
    return EXIT_OK


def _cmd_resume(args: argparse.Namespace) -> int:
    _emit(resume_all(_ctx(args)), as_json=True)
    return EXIT_OK


def _cmd_closeout(args: argparse.Namespace) -> int:
    out = CO.write_facts(_ctx(args))
    _emit(out["judgement"], as_json=True)
    return EXIT_OK


def _cmd_assist(args: argparse.Namespace) -> int:
    _emit(assist_trial(_ctx(args), args.trial_id, args.reason), as_json=True)
    return EXIT_OK


def _cmd_clear_pause(args: argparse.Namespace) -> int:
    _emit(clear_pause(_ctx(args), args.resolution), as_json=True)
    return EXIT_OK


def _record_freeze_invalid(ctx: C.CampaignContext,
                           problems: Sequence[str]) -> str:
    """LOCK 漂移要留下 ``EV_FREEZE_INVALID`` 证据（评审 M6）。

    否则「跑到一半实现被改」时 CLI 只抛错、结案看不到任何冻结事件，测量轴
    会落 INCOMPLETE 而不是 INVALID_INSTRUMENT——仪器失效被记成「没跑完」。
    只在**已经开过 trial** 时记：还没开跑就被拒不是样本仪器失效，写进去反而
    把「未启动」伪装成「仪器坏」，两轴都失真。
    """
    trials_dir = ctx.relative("trials")
    started = trials_dir.is_dir() and any(trials_dir.glob("*/state.json"))
    if not started:
        return ""
    try:
        ST.Journal(ctx).append(
            kind="decision", trial_id="campaign", action="freeze", ok=False,
            code=ST.EV_FREEZE_INVALID,
            message=f"FREEZE_INVALID: {list(problems)[:3]}")
    except (OSError, ValueError, ST.StateError) as exc:
        return f"（冻结失效事件落盘失败：{exc}）"
    return "（已记 EV_FREEZE_INVALID 事件）"


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    if not args.execute_paid:
        raise CliError("run 会真实付费：必须显式 --execute-paid（并满足 G-P/分词器）")
    # AC21：先冻结再模型请求——最终 LOCK 缺件/漂移（任一实现或共享依赖改动）
    # 一律拒绝付费 POST，不给「自觉遵守」留口子。
    from orchestrator.rsi_r02 import lock as LK

    verdict = LK.verify(ctx)
    if not verdict["ok"]:
        note = _record_freeze_invalid(ctx, verdict["problems"])
        raise CliError("FREEZE_INVALID：最终 LOCK 未冻结或实现/证据已漂移 "
                       f"{verdict['problems'][:3]}；拒绝付费 POST{note}")
    specs = SG.specs_for(args.phase)
    store = ST.StateStore(ctx)
    pending = SG.pending_trials(specs, store)
    if not pending:
        _emit({"phase": args.phase, "results": [], "completed": True,
               "stop_reason": "nothing_pending"}, as_json=True)
        return EXIT_OK
    require_gp_approval(ctx, posts=sum(s.post_cap for s in pending),
                        cap_usd=args.cap_usd)
    if not (args.tokenizer and args.tokenizer_sha256):
        raise CliError("付费运行需要 --tokenizer 与 --tokenizer-sha256")
    caller, counter = make_paid_caller(tokenizer_path=args.tokenizer,
                                       tokenizer_sha256=args.tokenizer_sha256)
    out = run_phase(ctx, args.phase, call_model=caller, counter=counter,
                    gpus=args.gpus)
    _emit(out, as_json=True)
    return EXIT_OK if out["completed"] else EXIT_REFUSED


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="RSI-R0.2 运行入口（默认离线）")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="只读状态（零 POST）")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("resume", help="恢复判定（零 POST，不重跑动作）")
    p.set_defaults(func=_cmd_resume)

    p = sub.add_parser("closeout", help="结案事实与三轴（零 POST）")
    p.set_defaults(func=_cmd_closeout)

    p = sub.add_parser("assist", help="标记人工救援（independent_success=false）")
    p.add_argument("--trial-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_assist)

    p = sub.add_parser("clear-pause", help="用外部可核验证据解除暂停")
    p.add_argument("--resolution", required=True)
    p.set_defaults(func=_cmd_clear_pause)

    p = sub.add_parser("run", help="按固定顺序跑 dev/validation（需 --execute-paid）")
    p.add_argument("--phase", required=True, choices=("dev", "validation"))
    p.add_argument("--execute-paid", action="store_true")
    p.add_argument("--tokenizer", default=None)
    p.add_argument("--tokenizer-sha256", default=None)
    p.add_argument("--cap-usd", type=float, default=B.USD_CAP)
    p.add_argument("--gpus", action="store_true")
    p.set_defaults(func=_cmd_run)
    return ap


def main(argv: "Sequence[str] | None" = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (CliError, C.CampaignError, ST.StateError, SG.StageError,
            B.BillingError, CD.CandidateError) as exc:
        _emit({"refused": str(exc), "type": type(exc).__name__}, as_json=True)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
