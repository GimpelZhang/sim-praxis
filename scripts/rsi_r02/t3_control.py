#!/usr/bin/env python3
"""RSI-R0.2 T3：同路径控制、协议准入预演与继承负控（零付费、真容器）。

计划 §8-T3 / AC17–AC18。三件事，全部走**未来模型同一条路**（
``cli.make_trial`` → ``TrialRunner``；``cli.run_trial`` → ``CandidateLoop``）：

1. **九实例 reference 控制**：同一候选 loop 注入确定性 ``ModelStub``，只替换
   决策源；分派/容器/观测/分析/submit/状态/journal/槽账/计费全部走生产路径。
   判决与 R0.1 参考 actor 逐项对账；R 族另跑独立 oracle（不 import 判据数值
   函数，只共享冻结常量）。
2. **两条协议 fixture**：P1/P2 各一条四动作真实链，nonce 由 campaign+fixture+
   slot 确定性派生；检查器独立重算统计并核槽位 ``required_action``。
3. **继承负控**：R0.1 ``controls.run_all`` 在当前代码状态重跑（晚登记/漏格/
   重复/错 run/伪数据/单位错/符号错/错误结论/低增益/旧反例），期望全部仍被拒。

零付费证据：所有 POST 的 ``provider_model`` 必为 ``model_stub``/``protocol_stub``，
``attempts_http=1``，且 R0.2 真实账本行数前后不变。

用法::

    /mnt/venvs/simpraxis/bin/python scripts/rsi_r02/t3_control.py \\
        --campaign-id r02_t3_control_<UTC> [--gpus]

产物：``<control campaign>/t3_control/{report.json,records/*.json}``。
重复运行幂等（已终态 trial 走恢复语义、不重跑物理动作）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "rsi_r01"),
           str(REPO_ROOT / "scripts" / "rsi_r02")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.runtime_paths import rsi_r02_root  # noqa: E402
from orchestrator.rsi_r01 import task_contract as TC  # noqa: E402
from orchestrator.rsi_r02 import campaign as C  # noqa: E402
from orchestrator.rsi_r02 import candidate as CD  # noqa: E402
from orchestrator.rsi_r02 import cli as CLI  # noqa: E402
from orchestrator.rsi_r02 import gather as G  # noqa: E402
from orchestrator.rsi_r02 import messages as M  # noqa: E402
from orchestrator.rsi_r02 import model_stub as MS  # noqa: E402
from orchestrator.rsi_r02 import protocol_fixture as PF  # noqa: E402
from orchestrator.rsi_r02 import stages as SG  # noqa: E402
from orchestrator.rsi_r02 import state as ST  # noqa: E402

import controls as CTL  # noqa: E402  （R0.1 继承负控）
import independent_oracle as ORACLE  # noqa: E402  （不同算法，只共享常量）
import reference_actor as RA  # noqa: E402  （宿主侧 E/D 阶梯来源）


def _load_t0():
    """按路径显式加载 R0.2 的 T0（sys.path 里 rsi_r01 也有同名 t0_baseline）。"""
    path = REPO_ROOT / "scripts" / "rsi_r02" / "t0_baseline.py"
    spec = importlib.util.spec_from_file_location("rsi_r02_t0_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


T0 = _load_t0()  # 224 槽位表复用，不自造

SCHEMA_REPORT = "simpraxis.rsi_r02.t3_control_report.v1"

def default_source_campaign() -> Path:
    """生产 campaign 路径由环境派生，不写死时间戳（评审 m10）。

    写死 ``r02_<时间戳>`` 会在下一次开账后悄悄指向旧 campaign，让「生产账前后
    不变」这条零付费判据对着一个不相干的目录成立。
    """
    from orchestrator.common.runtime_paths import (rsi_r02_campaign_id,
                                                   rsi_r02_root)
    return rsi_r02_root() / rsi_r02_campaign_id()
DEFAULT_TOKENIZER = Path(
    "/mnt/artifacts/simpraxis/rsi_r01/assets/tokenizers/"
    "deepseek-ai__DeepSeek-V3.1/tokenizer.json")
PROTO_SLOT_PREFIX = {"P1": "P-A-", "P2": "P-B-"}


# ---------------------------------------------------------------------------
# 控制 campaign（与生产 campaign 完全隔离：自根、自槽账、自计费账）
# ---------------------------------------------------------------------------


def prepare(ctx_root: Path, *, campaign_id: str, source: Path, plan: Path
            ) -> "tuple[C.CampaignContext, dict[str, Any]]":
    base = ctx_root
    target = base / campaign_id
    if (target / "contract" / C.CONTEXT_FILENAME).is_file():
        ctx = C.load_campaign(campaign_id=campaign_id, root=base)
    else:
        ctx = C.create_campaign(
            campaign_id=campaign_id, plan_path=plan,
            consent_path=source / "contract" / "consent.json",
            repo_root=REPO_ROOT, root=base)

    copies: "dict[str, Any]" = {}
    for instance_id in sorted(TC.BY_ID):
        rel = f"public/tasks/{instance_id}/task.json"
        data = (source / rel).read_bytes()
        dst = ctx.relative(rel)
        if not dst.is_file() or dst.read_bytes() != data:
            ctx.write_text(rel, data.decode("utf-8"))
        copies[instance_id] = {
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "identical": ctx.relative(rel).read_bytes() == data}

    if not ctx.relative("quota/slots.json").is_file():
        C.write_json(ctx, "quota/slots.json", T0.build_slots())
    return ctx, copies


def _oracle_ok(rec: Mapping[str, Any]) -> bool:
    """评审 M7：控制通过不能只看判据自述 ``judge_pass``。

    R 族另有独立 oracle（不同算法、只共享冻结常量）必须**逐项相符**；其余
    两族没有 oracle，但一旦记录了 ``agree=False`` 也一律不认。
    """
    oracle = rec.get("oracle")
    if rec.get("family") == "R":
        return bool(oracle) and oracle.get("agree") is True
    return (oracle or {}).get("agree") is not False


def _counter() -> "tuple[Any, str]":
    digest = hashlib.sha256(DEFAULT_TOKENIZER.read_bytes()).hexdigest()
    return M.LocalCounter.load(str(DEFAULT_TOKENIZER), digest), digest


# ---------------------------------------------------------------------------
# 决策源（控制仪器；私有阶梯只在宿主侧构造）
# ---------------------------------------------------------------------------


def policy_for(inst: TC.Instance) -> MS.StubPolicy:
    steps = int(inst.private.get("reference_steps", 2000))
    if inst.family == "R":
        research = inst.public["research"]
        ladder = ({"damping": float(research["damping_levels"][0]),
                   "initial_angle": float(research["initial_angles"][0])},)
    else:
        ladder = tuple(dict(x) for x in RA.gain_ladder(inst))
    return MS.StubPolicy(ladder=ladder, steps=steps)


def _judge_of(journal: ST.Journal, trial_id: str) -> "dict[str, Any] | None":
    events = [e for e in journal.for_trial(trial_id)
              if e.get("action") == "judge"]
    if not events:
        return None
    return (events[-1].get("payload") or {}).get("verdict")


def run_instance(ctx: C.CampaignContext, spec: SG.TrialSpec, *,
                 counter: Any, gpus: bool) -> "dict[str, Any]":
    inst = TC.get(spec.instance_id)
    runner = CLI.make_trial(ctx, spec, gpus=gpus)
    public_doc = CD.load_public_doc(ctx, spec.instance_id)
    stub = MS.ModelStub(public_doc, policy=policy_for(inst))
    out = CLI.run_trial(ctx, spec, call_model=stub, counter=counter,
                        trial=runner, gpus=gpus)
    verdict = _judge_of(ST.Journal(ctx), spec.trial_id)
    rec: "dict[str, Any]" = {
        "instance_id": spec.instance_id, "trial_id": spec.trial_id,
        "family": inst.family, "phase": spec.phase,
        "outcome": out.get("outcome"), "status": out.get("status"),
        "posts_sent": out.get("posts_sent"), "turns": out.get("turns"),
        "independent_success": out.get("independent_success"),
        "judge_pass": None if verdict is None else bool(verdict.get("pass")),
        "layer": None if verdict is None else verdict.get("layer"),
        "claim": None if verdict is None else
        (verdict.get("verdict") or {}).get("claim_status"),
        "stub_turns": len(stub.decisions),
    }
    if inst.family == "R":
        if not runner.runs:      # 已终态：从可信 manifest 复原（不重跑物理动作）
            problems: "list[str]" = []
            runner.runs = PF.recovered_runs(runner.store.trusted_dir,
                                            spec.trial_id, problems)
            if problems:
                rec["recovery_problems"] = problems
        observations = {}
        for run in runner.runs:
            if run.cell is None:
                continue
            observations[run.cell] = runner.store.load_trusted(
                run.run_id, run.manifest, expected_trial=spec.trial_id,
                expected_instance=spec.instance_id)
        if observations:
            oracle = ORACLE.judge_from_observations(
                observations, int(inst.public["research"]["h"]))
            diff = (ORACLE.compare(verdict["verdict"], oracle)
                    if verdict else ["NO_VERDICT"])
            rec["oracle"] = {"result": oracle, "diff": diff, "agree": not diff}
        else:
            rec["oracle"] = {"agree": None,
                             "note": "trial 已终态（恢复语义）且本进程无新 run"}
    return rec


# ---------------------------------------------------------------------------
# 协议 fixture（四动作真实链）
# ---------------------------------------------------------------------------


def slot_binding(events: "list[dict[str, Any]]", book: "ST.SlotBook"
                 ) -> "list[dict[str, Any]]":
    """逐 POST 槽位对照 ``required_action``（纯函数，便于离线单测）。

    槽要求的是**模型在这轮 POST 里请求的动作名**（preregister/run_experiment/
    analyze/submit），不是 journal 里的 ``action="post"``。post 事件只标记
    「这一槽花了一次 POST」；真正做的是什么动作，要看同一 slot_id 的分派结果
    事件。以前拿 post 事件自比，四槽全判不匹配（槽要求 preregister，事件名
    叫 post），把正确链误报成坏链。
    """
    posts = [e for e in events
             if e.get("kind") == "result" and e.get("action") == "post"]
    dispatched: "dict[str, str]" = {}
    for e in events:
        if (e.get("kind") == "result" and e.get("action") != "post"
                and e.get("slot_id")):
            dispatched.setdefault(str(e["slot_id"]), str(e["action"]))
    out: "list[dict[str, Any]]" = []
    for evt in posts:
        sid = str(evt.get("slot_id") or "")
        try:
            required = str(book.get(sid).get("required_action"))
        except ST.StateError:
            required = None
        actual = dispatched.get(sid, "")
        out.append({"slot_id": sid, "required_action": required,
                    "actual_action": actual,
                    "ok": required is None or required == actual})
    return out


def run_protocol(ctx: C.CampaignContext, *, counter: Any, gpus: bool
                 ) -> "list[dict[str, Any]]":
    book = ST.SlotBook(ctx)
    out: "list[dict[str, Any]]" = []
    for fixture_id in PF.FIXTURE_IDS:
        prefix = PROTO_SLOT_PREFIX[fixture_id]
        slot_ids = sorted(book.ids(prefix=prefix))
        fixture = PF.make_fixture(campaign_id=ctx.campaign_id,
                                  fixture_id=fixture_id,
                                  slot_id=slot_ids[0] if slot_ids else f"{prefix}1",
                                  instance_id="E-dev")
        spec = SG.TrialSpec(trial_id=f"proto_{fixture_id}", instance_id="E-dev",
                            family="E", phase="protocol", slot_prefix=prefix,
                            post_cap=len(slot_ids) or 4)
        runner = CLI.make_trial(ctx, spec, gpus=gpus)
        stub = PF.ProtocolStub(fixture=fixture)
        run_out = CLI.run_trial(ctx, spec, call_model=stub, counter=counter,
                                trial=runner, gpus=gpus)
        if not runner.runs:
            # 恢复语义（重跑幂等）：进程没跑物理动作 ⇒ runs 为空。观测原件仍在
            # 盘上，从 manifest 复原后统计重算照旧逐件验 hash，不走「自述」。
            problems: "list[str]" = []
            runner.runs = PF.recovered_runs(runner.store.trusted_dir,
                                            spec.trial_id, problems)
        # Journal 在构造时缓存事件：必须**跑完之后**重新构造才能看到本 trial 的事件
        events = ST.Journal(ctx).for_trial(spec.trial_id)
        verdict = PF.verify_run(fixture, events=events, trial=runner)
        slot_actions = slot_binding(events, book)
        # 评审 m5：required_action 只记录不对照等于没绑——槽不是随便领的，
        # 实做动作与槽要求的动作不符就必须计入 problems（不静默）。
        slot_problems = [f"SLOT_ACTION_MISMATCH: {a['slot_id']} "
                         f"required={a['required_action']} "
                         f"actual={a['actual_action']}"
                         for a in slot_actions if not a["ok"]]
        counts = book.counts()
        out.append({"fixture": fixture.as_dict(), "trial_id": spec.trial_id,
                    "outcome": run_out.get("outcome"),
                    "status": run_out.get("status"),
                    "posts_sent": run_out.get("posts_sent"),
                    "slot_binding": slot_actions,
                    "slot_problems": slot_problems,
                    "verdict": verdict,
                    "slots_after": counts})
    return out


# ---------------------------------------------------------------------------
# 零付费证据
# ---------------------------------------------------------------------------


def protocol_ok(protocol: "list[dict[str, Any]]") -> bool:
    """协议两条链的总体判据（纯函数，便于离线单测）。

    评审 LOW-2：只看 ``verdict.ok`` 不够——协议 trial 若在跑完整条链后中断
    （ABORTED）或转终态失败，恢复出来的 run 仍能让检查器判 ok，等于把
    「没跑完」洗成「跑通」。终态必须是 SUBMITTED。
    """
    return all(item["verdict"]["ok"]
               and not item.get("slot_problems")
               and item.get("status") == ST.TRIAL_SUBMITTED
               for item in protocol)


def post_producers(journal: ST.Journal) -> "dict[str, Any]":
    models: "dict[str, int]" = {}
    attempts: "dict[str, int]" = {}
    n = 0
    for evt in journal.events:
        if evt.get("kind") != "result" or evt.get("action") != "post":
            continue
        pay = evt.get("payload") or {}
        n += 1
        models[str(pay.get("provider_model"))] = \
            models.get(str(pay.get("provider_model")), 0) + 1
        attempts[str(pay.get("attempts_http"))] = \
            attempts.get(str(pay.get("attempts_http")), 0) + 1
    return {"posts": n, "by_provider_model": models, "by_attempts_http": attempts}


def ledger_snapshot(campaign_root: Path) -> "dict[str, Any]":
    """直接读账文件取证（不经 namespace 解析：控制 campaign 无 env、也不写账）。

    R0.2 的账位于 ``<campaign>/quota/ledger.jsonl``；T3 零付费的判据是
    **生产 campaign 的账文件前后逐字节相同**（当前应为「不存在」），以及
    控制 campaign 自始未产生任何账行。账不可读时如实记 error，不当作 0。
    """
    path = campaign_root / "quota" / "ledger.jsonl"
    if not path.is_file():
        return {"path": str(path), "exists": False, "rows": 0, "sha256": None}
    data = path.read_bytes()
    return {"path": str(path), "exists": True,
            "rows": sum(1 for ln in data.splitlines() if ln.strip()),
            "sha256": hashlib.sha256(data).hexdigest()}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.2 T3 同路径控制（零付费）")
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--root", default=None)
    ap.add_argument("--source-campaign", default=None,
                    help="生产 campaign 路径（缺省按 SIMPRAXIS_RSI_R02_CAMPAIGN 派生）")
    ap.add_argument("--plan",
                    default=str(Path.home() / "文档" / "RSI_R0.2_Plan_detailed.md"))
    ap.add_argument("--gpus", action="store_true")
    ap.add_argument("--skip-controls", action="store_true",
                    help="跳过 R0.1 继承负控（除非本次已单独跑过）")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else rsi_r02_root()
    try:
        source = (Path(args.source_campaign) if args.source_campaign
                  else default_source_campaign())
    except Exception as exc:  # noqa: BLE001 —— 不猜旧 campaign
        ap.error(f"无法确定生产 campaign（显式给 --source-campaign）：{exc}")
    ctx, copies = prepare(root, campaign_id=args.campaign_id, source=source,
                          plan=Path(args.plan))
    counter, counter_sha = _counter()
    ledger_before = ledger_snapshot(source)

    records = []
    for spec in (*SG.dev_specs(), *SG.validation_specs()):
        rec = run_instance(ctx, spec, counter=counter, gpus=args.gpus)
        C.write_json(ctx, f"t3_control/records/{spec.instance_id}.json", rec)
        records.append(rec)
        print(f"[control] {spec.instance_id}: {rec['outcome']} "
              f"judge={rec['judge_pass']} layer={rec['layer']}", flush=True)

    protocol = run_protocol(ctx, counter=counter, gpus=args.gpus)
    for item in protocol:
        C.write_json(ctx,
                     f"t3_control/records/proto_{item['fixture']['fixture_id']}.json",
                     item)
        print(f"[proto] {item['fixture']['fixture_id']}: "
              f"ok={item['verdict']['ok']} problems={item['verdict']['problems']}",
              flush=True)

    controls: "dict[str, Any]" = {"skipped": True}
    if args.skip_controls:
        # 重跑本脚本（例如只补 gather 段）时，继承负控可显式跳过，但**只允许**
        # 复用同一 campaign 里已落盘的 controls.json，并如实标注来源；没有
        # 就照旧记 skipped，不伪造结果。
        prev = ctx.relative("t3_control/controls.json")
        if prev.is_file():
            summary = json.loads(prev.read_text(encoding="utf-8"))
            controls = {"skipped": True,
                        "reused_from": "t3_control/controls.json",
                        "total": summary.get("total"),
                        "rejected_all": summary.get("rejected_all"),
                        "not_rejected": summary.get("not_rejected"),
                        "names": [r["name"] for r in summary.get("controls", [])]}
    else:
        summary = CTL.run_all(root=ctx.root, gpus=args.gpus)
        controls = {"skipped": False, "total": summary["total"],
                    "rejected_all": summary["rejected_all"],
                    "not_rejected": summary["not_rejected"],
                    "names": [r["name"] for r in summary["controls"]]}
        C.write_json(ctx, "t3_control/controls.json", summary)

    # AC17：gather 必须跑在**同一条路径**上（评审 H4）——不是另写一份统计，
    # 而是生产 gather 读本 campaign 的 state/journal/观测产物。产物落 gather/，
    # 与 T8 打包读的是同一份 facts/runs/claims；trial 状态与本次控制记录逐项
    # 对账，任何不一致都计入 ok=false。
    facts_doc = G.collect(ctx)
    G.write(ctx, dict(facts_doc))
    status_by_id = {t["trial_id"]: t["effective_status"]
                    for t in facts_doc["trials"]}
    status_mismatch = [
        {"instance_id": rec["instance_id"],
         "control_status": rec["status"],
         "gather_status": status_by_id.get(rec["trial_id"], G.NOT_RUN)}
        for rec in records
        if status_by_id.get(rec["trial_id"], G.NOT_RUN) != rec["status"]]
    gather = {
        "facts_path": "gather/facts.json",
        "trials": len(facts_doc["trials"]),
        "runs": len(facts_doc["runs"]),
        "claims": len(facts_doc["claims"]),
        "not_run": sum(1 for t in facts_doc["trials"]
                       if t["effective_status"] == G.NOT_RUN),
        "denominators": facts_doc["denominators"],
        "status_mismatch": status_mismatch,
        "ok": (len(facts_doc["trials"]) >= len(records)
               and len(facts_doc["runs"]) >= len(records)
               and not status_mismatch),
    }

    ledger_after = ledger_snapshot(source)
    control_ledger = ledger_snapshot(ctx.root)
    producers = post_producers(ST.Journal(ctx))
    zero_paid = {
        "posts": producers["posts"],
        "by_provider_model": producers["by_provider_model"],
        "by_attempts_http": producers["by_attempts_http"],
        "only_stub_producers": set(producers["by_provider_model"]) <= {
            "model_stub", "protocol_stub"},
        "single_attempt_each": set(producers["by_attempts_http"]) <= {"1"},
        "production_ledger_before": ledger_before,
        "production_ledger_after": ledger_after,
        "production_ledger_unchanged": ledger_before == ledger_after,
        # 评审 m8：账文件「不存在」与「存在但没变」是两种证据，必须分开写。
        # 前者证明本批一次账都没落，后者证明落了也没动；混成一句「unchanged」
        # 会让路径写错时（对着一个空目录比前后）也照样成立。因此前后都不存在
        # 时再用「快照锚在生产 campaign 内」兜底（contract 合同必须在该目录），
        # 只有锚点成立才承认 unchanged 是正式证据。
        "production_ledger_state": (
            "ABSENT_BOTH" if not ledger_before["exists"]
            and not ledger_after["exists"] else "PRESENT"),
        "production_campaign_anchored": (
            (source / "contract" / C.CONTEXT_FILENAME).is_file()),
        "production_ledger_ok": (
            ledger_before == ledger_after
            and (ledger_after["exists"]
                 or (source / "contract" / C.CONTEXT_FILENAME).is_file())),
        "production_quota_dir_exists": (source / "quota").is_dir(),
        "control_ledger": control_ledger,
        "control_never_recorded": control_ledger["rows"] == 0,
    }

    # 评审 M3：``controls.get("rejected_all") is not False`` 在「跳过且无旧件」
    # 时对 None 也成立，等于空转判绿。改为必须**显式为 True**（13/13 全拒），
    # 复用路径只要旧件是 rejected_all=true 照过，没跑过的一律不过。
    controls_ok = controls.get("rejected_all") is True

    finished = [r for r in records if r["judge_pass"] is True and _oracle_ok(r)]
    proto_all_ok = protocol_ok(protocol)
    report = {
        "schema": SCHEMA_REPORT, "recorded_at_utc": utc_now_iso_z(),
        "campaign_id": ctx.campaign_id, "campaign_root": str(ctx.root),
        "source_campaign": str(source),
        "public_copies": copies,
        "tokenizer": {"path": str(DEFAULT_TOKENIZER), "sha256": counter_sha},
        "instances": records,
        "instances_passed": len(finished),
        "instances_total": len(records),
        "protocol": protocol,
        "protocol_ok": proto_all_ok,
        "controls": controls,
        "gather": gather,
        "zero_paid": zero_paid,
        "ok": (len(finished) == len(records) == 9 and proto_all_ok
               and controls_ok
               and gather["ok"]
               and zero_paid["only_stub_producers"]
               and zero_paid["single_attempt_each"]
               and zero_paid["production_ledger_ok"]
               and zero_paid["control_never_recorded"]),
    }
    C.write_json(ctx, "t3_control/report.json", report)
    print(json.dumps({k: report[k] for k in
                      ("instances_passed", "instances_total", "protocol_ok",
                       "ok")}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
