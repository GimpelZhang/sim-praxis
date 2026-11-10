"""RSI-R0.2 T4：十二槽 POST 同路径协议准入（真实付费，AC22 / G-D）。

两条协议 fixture（P1/P2）各一条真实链：preregister → run_experiment →
analyze → submit；每链 4 个必做槽 + 2 个自修余量槽（2026-11-10 用户裁决
「开新票据重做协议链」：首跑 4 槽零余量，坏 plan 一错就触帽）。**与 T3 控制唯一的区别是决策源**：这里用网关真实模型
（``cli.make_paid_caller``），分派/容器/观测/分析/submit/状态/journal/
槽账/计费全走同一条 ``cli.run_trial``。协议检查器（``protocol_fixture.verify_run``）
与控制跑用的是同一个，判据不因付费而放宽。

纪律（§6.1）：

* 每链最多 6 次发送（同一次 trial 内，不是旁路补发）：坏 JSON / 缺字段 /
  不合法 prereg 属**帽内可见错误**，候选可自修，各占一次 POST；但成功动作序列
  必须恰好 preregister→run_experiment→analyze→submit，多一次少一次都算 FAIL。
  链 FAIL 后剩余槽取消，G-D 失败，T5/T6 取消，不重测。
* 付费前先过最终 LOCK（``lock.verify``，AC21）与 G-P 批准件覆盖本批。
* 本地计数与完整请求字节逐 POST 记录；供应端 usage **只记录不自证**。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from orchestrator.common.runtime_paths import rsi_r02_root  # noqa: E402
from orchestrator.rsi_r02 import campaign as C  # noqa: E402
from orchestrator.rsi_r02 import cli as CLI  # noqa: E402
from orchestrator.rsi_r02 import lock as LK  # noqa: E402
from orchestrator.rsi_r02 import protocol_fixture as PF  # noqa: E402
from orchestrator.rsi_r02 import stages as SG  # noqa: E402
from orchestrator.rsi_r02 import state as ST  # noqa: E402

import t3_control as T3C  # noqa: E402  —— 槽位绑定/协议判据与 T3 同一份实现

SCHEMA_REPORT = "simpraxis.rsi_r02.t4_protocol_report.v1"
SCHEMA_CHAIN = "simpraxis.rsi_r02.t4_protocol_chain.v1"
#: 每链 4 个必做动作 + 2 个自修余量 ⇒ 两条链的发送区间 [8, 12]。槽数口径只从
#: ``protocol_fixture`` 取，本文件不写 6/12 字面量（评审 MINOR-3）。
MIN_POSTS = PF.SLOTS_PER_CHAIN * len(PF.FIXTURE_IDS) - (
    PF.REPAIR_SLOTS_PER_CHAIN * len(PF.FIXTURE_IDS))
TOTAL_POSTS = PF.TOTAL_PROTOCOL_SLOTS
# G-P 核验用的本批货币帽**不许由命令行缩小**（评审 LOW-3：调用方传小值会弱化
# 「批准件必须覆盖整批」的核验）。与 ``TOTAL_POSTS`` 同法：内部常量，非参数。
CAP_USD = 100.0


def _usage_rows(events: "list[Mapping[str, Any]]") -> "list[dict[str, Any]]":
    """逐 POST 的本地计数与供应端 reported usage（后者只记录，不当证据）。"""
    rows: "list[dict[str, Any]]" = []
    intents = {str((e.get("payload") or {}).get("call_id")): e
               for e in events
               if e.get("kind") == "intent" and e.get("action") == "post"}
    for evt in events:
        if evt.get("kind") != "result" or evt.get("action") != "post":
            continue
        pay = evt.get("payload") or {}
        cid = str(pay.get("call_id") or "")
        intent = (intents.get(cid) or {}).get("payload") or {}
        rows.append({
            "slot_id": evt.get("slot_id"),
            "call_id": cid,
            "ok": evt.get("ok"),
            "code": evt.get("code"),
            "local": {"n_bytes": intent.get("n_bytes"),
                      "local_tokens": intent.get("local_tokens"),
                      "request_sha256": intent.get("request_sha256")},
            "provider_reported": {
                "usage": pay.get("usage_normalized"),
                "usage_raw": pay.get("usage_raw"),
                "cost_status": pay.get("cost_status"),
                "settled_usd": pay.get("settled_usd"),
                "attempts_http": pay.get("attempts_http"),
                "stop_reason": pay.get("stop_reason"),
                "provider_model": pay.get("provider_model"),
                "provider_request_id": pay.get("provider_request_id"),
            },
            "reply": {"sha256": pay.get("assistant_text_sha256"),
                      "content_bytes": pay.get("content_bytes")},
        })
    return rows


def run_chain(ctx: C.CampaignContext, fixture_id: str, *, caller: Any,
              counter: Any, gpus: bool) -> "dict[str, Any]":
    """跑一条协议链（4 必做 + 2 自修余量）；返回证据记录，不做任何补发。"""
    book = ST.SlotBook(ctx)
    prefix = T3C.PROTO_SLOT_PREFIX[fixture_id]
    slot_ids = sorted(book.ids(prefix=prefix))
    if len(slot_ids) != PF.SLOTS_PER_CHAIN:
        raise SystemExit(f"{fixture_id} 期望 {PF.SLOTS_PER_CHAIN} 个协议槽"
                         f"（{len(PF.ACTION_ORDER)} 必做 + "
                         f"{PF.REPAIR_SLOTS_PER_CHAIN} 余量），"
                         f"实际 {len(slot_ids)}：{slot_ids}")
    fixture = PF.make_fixture(campaign_id=ctx.campaign_id,
                              fixture_id=fixture_id, slot_id=slot_ids[0],
                              instance_id="E-dev")
    spec = SG.TrialSpec(trial_id=f"proto_{fixture_id}", instance_id="E-dev",
                        family="E", phase="protocol", slot_prefix=prefix,
                        post_cap=len(slot_ids))
    runner = CLI.make_trial(ctx, spec, gpus=gpus)
    run_out = CLI.run_trial(ctx, spec, call_model=caller, counter=counter,
                            trial=runner, gpus=gpus,
                            public_doc=fixture.task_doc())
    if not runner.runs:
        # 恢复语义：本进程没跑物理动作时从可信 manifest 只复原定位信息。
        problems: "list[str]" = []
        runner.runs = PF.recovered_runs(runner.store.trusted_dir,
                                        spec.trial_id, problems)
    events = ST.Journal(ctx).for_trial(spec.trial_id)
    verdict = PF.verify_run(fixture, events=events, trial=runner,
                            post_budget=len(slot_ids))
    slot_actions = T3C.slot_binding(events, book)
    slot_problems = T3C.slot_problems(slot_actions)
    slot_repairs = T3C.slot_repairs(slot_actions)
    posts = _usage_rows(events)
    return {
        "schema": SCHEMA_CHAIN,
        "fixture": fixture.as_dict(),
        "trial_id": spec.trial_id,
        "outcome": run_out.get("outcome"),
        "status": run_out.get("status"),
        "posts_sent": run_out.get("posts_sent"),
        "posts_recorded": len(posts),
        "posts": posts,
        "slot_binding": slot_actions,
        "slot_problems": slot_problems,
        "slot_repairs": slot_repairs,
        "verdict": verdict,
        # 同 T3：槽账要重新载入再数（``book`` 是跑之前的内存快照，评审 MINOR-8）。
        "slots_after": ST.SlotBook(ctx).counts(),
        "chain_ok": bool(verdict["ok"] and not slot_problems
                         and run_out.get("status") == ST.TRIAL_SUBMITTED
                         and len(PF.ACTION_ORDER) <= len(posts)
                         <= len(slot_ids)),
    }


def check_batch_credential(ctx: C.CampaignContext, batch_id: str, *,
                           posts: int = TOTAL_POSTS, task: int = 0,
                           stage: str = "coding_debug") -> "dict[str, Any]":
    """实呼前的批次凭据自检（缺件/错配/批准数不足一律零 POST 拒绝）。

    G-P 自批件（``R02-B1.json``）是**花钱授权**；网关 preflight 另要一份**批次
    凭据**（``task/stage/approved/beyond_normal``）。两者不是同一个东西，也不
    互相替代：这里把后者的缺口在第一次 POST 之前暴露，而不是等 preflight 在
    槽已 claim 之后拦截（那会把 protocol slot 记成 UNKNOWN_SENT）。
    """
    from orchestrator.gateway.quota_ledger import QuotaError, load_approval

    try:
        cred = load_approval(batch_id, ctx.relative("quota/approvals"))
    except QuotaError as exc:
        return {"ok": False, "batch_id": batch_id, "credential": None,
                "problems": [f"CREDENTIAL_MISSING: {exc}"]}
    problems: "list[str]" = []
    if cred.get("task") != task:
        problems.append(f"CREDENTIAL_TASK_MISMATCH: {cred.get('task')} != {task}")
    if cred.get("stage") != stage:
        problems.append(f"CREDENTIAL_STAGE_MISMATCH: {cred.get('stage')} != {stage}")
    approved = cred.get("approved")
    if not isinstance(approved, int) or isinstance(approved, bool) or approved < posts:
        problems.append(f"CREDENTIAL_COVERAGE: approved={approved!r} < 本批 {posts} 次")
    return {"ok": not problems, "batch_id": batch_id, "problems": problems,
            "credential": cred if not problems else None}


def cancel_pending_slots(ctx, failed_fixture: str) -> "list[str]":
    """链失败后取消「失败链自身的未用槽 + 其后所有链」的槽，返回被取消的槽 id。

    §6.1 的「剩余槽如实取消」指的是**所有**未发送的协议槽：链在第 k 轮失败时，
    同一链的 k+1..4 也是剩余（评审 MINOR-2 前只取消了后继链）。已发送的槽是
    ``SUBMITTED`` 或 ``SLOT_UNKNOWN``，``cancel_unused`` 只动 ``UNUSED``，
    不会把「可能已发出去的」误记成取消。
    """
    start = PF.FIXTURE_IDS.index(failed_fixture)
    book = ST.SlotBook(ctx)
    cancelled: "list[str]" = []
    for pending in PF.FIXTURE_IDS[start:]:
        cancelled += book.cancel_unused(
            reason="T4 前链失败：协议不补考（§6.1）",
            prefix=T3C.PROTO_SLOT_PREFIX[pending])
    return cancelled


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.2 T4 协议准入（真实付费）")
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--root", default=None)
    ap.add_argument("--tokenizer", default=str(T3C.DEFAULT_TOKENIZER))
    ap.add_argument("--tokenizer-sha256", default=None)
    ap.add_argument("--batch-id", required=True,
                    help="网关批次凭据 id（billing_context 归属；本 campaign quota/approvals）")
    ap.add_argument("--gpus", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else rsi_r02_root()
    ctx = C.load_campaign(campaign_id=args.campaign_id, root=root)

    # AC21：任何付费 POST 之前先核最终 LOCK（缺件/漂移一律拒发）。
    lock_v = LK.verify(ctx)
    if not lock_v["ok"]:
        print(json.dumps({"refused": "FREEZE_INVALID",
                          "problems": lock_v["problems"]}, ensure_ascii=False))
        return 2

    # D-T4-2 的运行时守卫：协议合同必须与实机重算一致、公开文档必须自足。
    # 合同本身已由 LOCK 钉哈希，这一步查的是它与**当前代码**的关系——不一致就
    # 说明冻结面与实机脱节，零 POST 拒绝（比在付费后才解释便宜得多）。
    contract_path = ctx.relative("contract/protocol_contract.json")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"refused": "PROTOCOL_CONTRACT", "problems": [str(exc)]},
                         ensure_ascii=False))
        return 2
    contract_problems = PF.check_contract(contract, campaign_id=ctx.campaign_id)
    if contract_problems:
        print(json.dumps({"refused": "PROTOCOL_CONTRACT",
                          "problems": contract_problems}, ensure_ascii=False))
        return 2

    # G-P：批准件必须覆盖本批 12 次发送（4 必做 + 2 余量，两链）与货币帽。
    CLI.require_gp_approval(ctx, posts=TOTAL_POSTS, cap_usd=CAP_USD)
    if not args.tokenizer_sha256:
        print(json.dumps({"refused": "缺 --tokenizer-sha256"}, ensure_ascii=False))
        return 2
    # 批次凭据自检：缺件/错配/批准数不足都在**第一次 POST 之前**拒绝（零 POST）。
    cred = check_batch_credential(ctx, args.batch_id)
    if not cred["ok"]:
        print(json.dumps({"refused": "BATCH_CREDENTIAL",
                          "batch_id": args.batch_id,
                          "problems": cred["problems"]}, ensure_ascii=False))
        return 2
    caller, counter = CLI.make_paid_caller(
        tokenizer_path=args.tokenizer,
        tokenizer_sha256=args.tokenizer_sha256,
        batch_id=args.batch_id)

    chains: "list[dict[str, Any]]" = []
    for fixture_id in PF.FIXTURE_IDS:
        rec = run_chain(ctx, fixture_id, caller=caller, counter=counter,
                        gpus=args.gpus)
        C.write_json(ctx, f"t4/records/proto_{fixture_id}.json", rec)
        chains.append(rec)
        print(f"[T4] {fixture_id}: status={rec['status']} "
              f"posts={rec['posts_recorded']} ok={rec['chain_ok']} "
              f"problems={rec['verdict']['problems']}", flush=True)
        if not rec["chain_ok"]:
            # 模型错即停：不补发、不换题；剩余协议槽如实取消（含本链未用槽）。
            cancel_pending_slots(ctx, fixture_id)
            break

    local_counts = [p["local"] for ch in chains for p in ch["posts"]]
    g_d = {
        "schema": SCHEMA_REPORT,
        "campaign_id": ctx.campaign_id,
        "batch_id": args.batch_id,
        "lock_sha256": lock_v.get("lock_sha256"),
        "chains": [{k: ch[k] for k in ("trial_id", "status", "posts_recorded",
                                       "chain_ok", "verdict", "slot_binding",
                                       "slot_problems", "slot_repairs")}
                              for ch in chains],
        "posts_total": sum(ch["posts_recorded"] for ch in chains),
        "provider_models": sorted({str(p["provider_reported"]["provider_model"])
                                   for ch in chains for p in ch["posts"]}),
        "attempts_http": [p["provider_reported"]["attempts_http"]
                          for ch in chains for p in ch["posts"]],
        "local_counts": local_counts,
        "g_d_ok": (len(chains) == len(PF.FIXTURE_IDS)
                   and all(ch["chain_ok"] for ch in chains)
                   and MIN_POSTS <= sum(ch["posts_recorded"]
                                           for ch in chains) <= TOTAL_POSTS
                   and all(a == 1 for a in
                           [p["provider_reported"]["attempts_http"]
                            for ch in chains for p in ch["posts"]])),
    }
    C.write_json(ctx, "t4/g_d.json", g_d)
    print(json.dumps({"g_d_ok": g_d["g_d_ok"],
                      "posts_total": g_d["posts_total"],
                      "chains": [[c["trial_id"], c["chain_ok"]] for c in chains]},
                     ensure_ascii=False))
    return 0 if g_d["g_d_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
