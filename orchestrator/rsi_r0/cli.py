"""RSI-R0 唯一入口（AC21：全部路径显式 R0，禁止转旧阶段执行入口）。

子命令按计划 §10：

    preflight  → 零付费前置体检（路径/账套/封存/判据/发网事实账）
    controls   → 21 负控 + 6 mutation + 判据冻结核对（零容器、零模型）
    dry-run    → mock 模型零付费端到端（POST 恒 0，可中断可恢复）
    lock       → 写一次即锁的完整快照；--verify 只校验不回填
    pricing    → 资源与报价合同（最坏成本上界，零付费）
    approve    → 批次批准（账本票 + 门侧批准件，写一次即锁）
    model-approval → 模型 ID 批准登记（硬规则 1 产物化）
    protocol   → G-P 门后的六次协议探针（T5，真实发网）
    develop    → G-P 门后的三族开发轨迹（T6）
    validate   → G-V 门后的六条验证轨迹（T7）
    closeout   → 对账与结案件（零付费）

规矩：每个子命令自己查前置门（不靠调用者按顺序点），失败一律非零退出并打印
结构化原因；命令参数里不含任何凭据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from orchestrator.common import runtime_paths as rp
from orchestrator.rsi_r0 import gates
from orchestrator.rsi_r0.attempts import AttemptJournal, _utc_now
from orchestrator.rsi_r0.contracts import (
    CalibrationVerdict,
    pick_primary_verdict,
    sha256_of,
)
from orchestrator.rsi_r0.runner import PaidPathNotArmed, R0Runner, RunnerError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _runner(args: argparse.Namespace) -> R0Runner:
    return R0Runner(Path(args.campaign).expanduser())


# ---------------------------------------------------------------- 子命令
def cmd_preflight(args: argparse.Namespace) -> int:
    runner = _runner(args)
    if args.prepare:
        facts_prepare = runner.ensure_campaign()
    else:
        facts_prepare = None
    facts = runner.preflight()
    _emit({"command": "preflight", "prepare": facts_prepare, **facts})
    return EXIT_OK if facts["ok"] else EXIT_REFUSED


def cmd_controls(args: argparse.Namespace) -> int:
    from orchestrator.rsi_r0.instances import PLANS
    from orchestrator.rsi_r0.matrix import freeze_criteria, run_mutations, run_negatives

    runner = _runner(args)
    runner.ensure_campaign()
    protocol = rp.rsi_r0_protocol_dir()
    frozen, digest = freeze_criteria(protocol, PLANS, allow_refreeze=False)
    negatives = run_negatives()
    mutations = run_mutations()
    report = {"command": "controls", "campaign": str(runner.root),
              "criteria_hash": digest,
              "criteria_matches_frozen": frozen.get("criteria_hash") == digest,
              "negatives": {"passed": negatives["passed"], "total": negatives["total"],
                            "missed": negatives["missed"]},
              "mutations": {"killed": mutations["killed"], "total": mutations["total"],
                            "survivors": mutations["survivors"]}}
    report["report_sha256"] = sha256_of(report)
    targets = [protocol / "t4_controls_latest.json"]
    for p in targets:
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                     + "\n", encoding="utf-8")
    _emit(report)
    ok = (negatives["passed"] == negatives["total"] == 21
          and mutations["killed"] == mutations["total"] == 6
          and report["criteria_matches_frozen"])
    return EXIT_OK if ok else EXIT_REFUSED


def cmd_dry_run(args: argparse.Namespace) -> int:
    runner = _runner(args)
    runner.ensure_campaign()
    report = runner.dry_run(resume=not args.no_resume, fresh=args.fresh)
    _emit({"command": "dry-run", "report": report, "posts": report["posts"],
           "slots": len(report["slots"]),
           "completed": sum(1 for s in report["slots"]
                            if s.get("state") == "COMPLETED")})
    return EXIT_OK if report["posts"] == 0 else EXIT_REFUSED


def cmd_lock(args: argparse.Namespace) -> int:
    runner = _runner(args)
    runner.ensure_campaign()
    if args.verify:
        verdict = runner.verify_lock()
        _emit({"command": "lock", "verify": verdict})
        return EXIT_OK if verdict["ok"] else EXIT_REFUSED
    if args.supersede_reason:
        out = runner.supersede_lock(args.supersede_reason)
        _emit({"command": "lock", "superseded": out,
               "verify": runner.verify_lock()})
        return EXIT_OK
    snap = runner.write_lock()
    _emit({"command": "lock", "lock_sha256": snap["lock_sha256"],
           "path": str(runner.lock_path()),
           "criteria_hash": snap["criteria_hash"],
           "code_set_sha256": snap["code_set_sha256"]})
    return EXIT_OK


def _paid(cmd: str, args: argparse.Namespace, gate: str) -> int:
    """付费入口的统一拒绝路径：没有 ALLOW_PAID / 门不成立 ⇒ 非零退出。"""
    runner = _runner(args)
    if not os.environ.get("SIMPRAXIS_ALLOW_PAID"):
        _emit({"command": cmd, "refused": "SIMPRAXIS_ALLOW_PAID 未设置",
               "gate": gate})
        return EXIT_REFUSED
    try:
        verdict = runner.require_gate(gate, batch_id=args.batch)
    except (gates.GateRefused, RunnerError) as exc:
        _emit({"command": cmd, "refused": str(exc), "gate": gate})
        return EXIT_REFUSED
    _emit({"command": cmd, "gate": verdict, "note": "已过门；由具体子命令继续执行"})
    return EXIT_OK


def cmd_pricing(args: argparse.Namespace) -> int:
    """资源与报价合同（零付费、纯算术）：--write 落盘，否则只报现状。"""
    from orchestrator.rsi_r0 import budget_contract

    runner = _runner(args)
    runner.ensure_campaign()
    path = rp.rsi_r0_protocol_dir() / "pricing_contract.json"
    if args.write:
        doc = budget_contract.write_contract(path)
        written = True
    else:
        doc = budget_contract.read_contract(path)
        written = False
    ok, why = budget_contract.contract_ok(doc)
    _emit({"command": "pricing", "written": written, "ok": ok, "why": why,
           "path": str(path), "contract": doc})
    return EXIT_OK if ok else EXIT_REFUSED


def cmd_approve(args: argparse.Namespace) -> int:
    """批次批准：账本侧批次票 + 门侧写一次即锁批准件（两处必须一致）。"""
    from orchestrator.gateway import quota_ledger as ql

    runner = _runner(args)
    runner.ensure_campaign()
    batch = args.batch
    task = args.task
    stage = args.stage
    if args.requests > ql.RSI_R0_TASK_CAPS.get(task, 0):
        _emit({"command": "approve",
               "error": f"task {task} 的任务帽是 {ql.RSI_R0_TASK_CAPS.get(task, 0)}，"
                        f"不能批 {args.requests} 次"})
        return EXIT_REFUSED
    approvals = rp.rsi_r0_approvals_dir()
    ticket_path = approvals / f"{batch}.json"
    if ticket_path.exists():
        try:
            existing = ql.load_approval(batch, approvals)
        except Exception as exc:  # noqa: BLE001 —— 旧票坏了就是拒
            _emit({"command": "approve", "error": f"已存在的批次票不可用：{exc}"})
            return EXIT_REFUSED
        same = (int(existing.get("approved") or 0) == args.requests
                and existing.get("task") == task
                and existing.get("stage") == stage)
        if not same:
            _emit({"command": "approve",
                   "error": f"批次票已存在且不同，拒绝覆盖：{ticket_path}"})
            return EXIT_REFUSED
        ticket_created = False
    else:
        ql.write_approval(batch_id=batch, task=task, stage=stage,
                          approved=args.requests, beyond_normal=False,
                          note=args.note or f"RSI-R0 {batch} 批次批准",
                          namespace=ql.RSI_R0_NAMESPACE)
        ticket_created = True

    body = gates.approval_body(
        batch_id=batch, task_cap=args.requests, hard_cap=ql.RSI_R0_HARD_CAP,
        currency="USD", usd_ceiling=args.usd_ceiling,
        approved_by=args.approved_by,
        approved_at_utc=(args.approved_at or _utc_now()),
        scope=args.scope)
    try:
        written = gates.write_approval(runner.gate_approval_path(batch), body)
    except gates.ApprovalError as exc:
        _emit({"command": "approve", "error": str(exc)})
        return EXIT_REFUSED
    _emit({"command": "approve", "batch": batch, "task": task, "stage": stage,
           "requests": args.requests, "usd_ceiling": args.usd_ceiling,
           "ticket_created": ticket_created, "artifact": written,
           "path": str(runner.gate_approval_path(batch))})
    return EXIT_OK


def cmd_model_approval(args: argparse.Namespace) -> int:
    """模型 ID 批准登记（硬规则 1 的产物化；只记录既有用户裁决）。"""
    runner = _runner(args)
    runner.ensure_campaign()
    try:
        doc = runner.write_model_approval(decided_at_utc=args.decided_at,
                                          basis=args.basis,
                                          approved_by=args.approved_by)
    except RunnerError as exc:
        _emit({"command": "model-approval", "error": str(exc)})
        return EXIT_REFUSED
    ok, detail = runner.model_ids_approved()
    _emit({"command": "model-approval", "ok": ok, "detail": detail,
           "stage_models": doc.get("stage_models"),
           "stage_models_sha256": doc.get("stage_models_sha256")})
    return EXIT_OK if ok else EXIT_REFUSED


def cmd_protocol(args: argparse.Namespace) -> int:
    """六次协议探针（AC25）：G-P 门后真实发网，至多 6 次、无重试、逐请求记账。"""
    from orchestrator.rsi_r0 import protocol_probe as pp

    runner = _runner(args)
    runner.ensure_campaign()
    if not os.environ.get("SIMPRAXIS_ALLOW_PAID"):
        _emit({"command": "protocol", "refused": "SIMPRAXIS_ALLOW_PAID 未设置",
               "gate": "G-P"})
        return EXIT_REFUSED
    try:
        gate = runner.require_gate("G-P", batch_id=args.batch)
    except (gates.GateRefused, RunnerError) as exc:
        _emit({"command": "protocol", "refused": str(exc), "gate": "G-P"})
        return EXIT_REFUSED

    from orchestrator.gateway.quota_ledger import QuotaLedger
    from orchestrator.gateway.router import ModelGateway

    gateway = ModelGateway(ledger=QuotaLedger.rsi_r0(), max_attempts=1,
                           pause_state_path=rp.rsi_r0_gateway_state_path())
    try:
        report = pp.ProtocolProbe(runner, gateway).run()
    except pp.ProbeError as exc:
        _emit({"command": "protocol", "refused": str(exc), "gate": "G-P"})
        return EXIT_REFUSED
    out = pp.write_report(report)
    _emit({"command": "protocol", "gate": gate["gate"], "verdict": report["verdict"],
           "blocked_reason": report["blocked_reason"],
           "requests": report["requests"], "model": report["model"],
           "report": str(out), "report_sha256": report["report_sha256"]})
    return EXIT_OK if report["verdict"] == "PROTOCOL_OK" else EXIT_REFUSED


def cmd_develop(args: argparse.Namespace) -> int:
    return _paid("develop", args, "G-P")


def cmd_validate(args: argparse.Namespace) -> int:
    return _paid("validate", args, "G-V")


def cmd_closeout(args: argparse.Namespace) -> int:
    """零付费结案件：只汇总**已存在**的事实，缺件如实记 NOT_RUN。"""
    runner = _runner(args)
    runner.ensure_campaign()
    protocol = rp.rsi_r0_protocol_dir()
    closeout = rp.rsi_r0_closeout_dir()

    def _json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    criteria = _json(protocol / "t3_criteria.json")
    lock = _json(protocol / "t4_lock.json")
    controls = _json(protocol / "t4_controls_latest.json")
    dryrun = _json(closeout / "dryrun_report.json")
    probe = _json(protocol / "protocol_probe_report.json")
    journal = AttemptJournal(rp.rsi_r0_attempts_path())
    latest = journal.latest_by_request()
    posts = sum(int(r.get("attempts_http") or 0) for r in latest.values())

    missing: list[str] = []
    candidates: list[CalibrationVerdict] = []
    if criteria is None:
        missing.append("t3_criteria.json")
    if lock is None:
        missing.append("t4_lock.json")
    if controls is None:
        missing.append("t4_controls_latest.json")
    elif controls["negatives"]["passed"] != controls["negatives"]["total"]:
        candidates.append(CalibrationVerdict.INVALID_INSTRUMENT)
    if dryrun is None or dryrun.get("posts") not in (0,):
        missing.append("dryrun_report.json(posts=0)")
    if posts:
        candidates.append(CalibrationVerdict.INVALID_INSTRUMENT)
    if probe is None:
        missing.append("protocol_probe_report.json")
    elif probe.get("verdict") != "PROTOCOL_OK":
        candidates.append(CalibrationVerdict.PROTOCOL_BLOCKED)
    if missing:
        candidates.append(CalibrationVerdict.ABORTED_INCOMPLETE)
    if not candidates:
        candidates.append(CalibrationVerdict.ABORTED_INCOMPLETE)   # R0 未跑完，不冒充完成
    primary, others = pick_primary_verdict(candidates)

    report = {
        "schema": "rsi_r0.closeout.v1",
        "campaign": str(runner.root),
        "primary_verdict": primary.value,
        "reason_codes": [v.value for v in others],
        "missing_artifacts": missing,
        "criteria_hash": (criteria or {}).get("criteria_hash"),
        "lock_sha256": (lock or {}).get("lock_sha256"),
        "controls": ({k: controls[k] for k in ("negatives", "mutations")}
                     if controls else None),
        "dryrun_posts": (dryrun or {}).get("posts"),
        "protocol": ({"verdict": probe.get("verdict"),
                      "requests_used": (probe.get("requests") or {}).get("used"),
                      "report_sha256": probe.get("report_sha256")}
                     if probe else None),
        "attempts": {"requests": len(latest), "http_posts": posts},
        "evidence_scope": "zero_paid_only" if not posts else "paid",
        "not_run": (["development(three)", "validation(six)"]
                    if probe and probe.get("verdict") == "PROTOCOL_OK"
                    else ["protocol(six)", "development(three)", "validation(six)"]),
    }
    report["report_sha256"] = sha256_of(report)
    out = closeout / "closeout_zero_paid.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    _emit({"command": "closeout", "report": report, "path": str(out)})
    return EXIT_OK


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m orchestrator.rsi_r0.cli",
        description="RSI-R0 唯一入口（所有子命令都 fail-closed 自查前置门）")
    sub = ap.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--campaign", required=True,
                       help="campaign 绝对路径（不存在时由 --prepare/ensure 创建）")

    p = sub.add_parser("preflight", help="零付费前置体检")
    _common(p)
    p.add_argument("--prepare", action="store_true", help="缺失时先建六族骨架")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("controls", help="21 负控 + 6 mutation + 判据冻结（零容器）")
    _common(p)
    p.set_defaults(func=cmd_controls)

    p = sub.add_parser("dry-run", help="mock 模型零付费端到端（可中断可恢复）")
    _common(p)
    p.add_argument("--fresh", action="store_true", help="清掉 dry_* 进度重跑")
    p.add_argument("--no-resume", action="store_true", help="不沿用已有进度")
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("lock", help="写一次即锁的快照；--verify 只校验")
    _common(p)
    p.add_argument("--verify", action="store_true")
    p.add_argument("--supersede-reason", default=None,
                   help="显式取代旧 LOCK（≥20 字理由；旧件归档不删；有发网 attempt 即拒）")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("pricing", help="资源与报价合同（零付费；--write 落盘）")
    _common(p)
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_pricing)

    p = sub.add_parser("approve", help="批次批准（账本票 + 门侧写一次即锁批准件）")
    _common(p)
    p.add_argument("--batch", required=True)
    p.add_argument("--task", type=int, required=True)
    p.add_argument("--stage", default="coding_debug")
    p.add_argument("--requests", type=int, required=True)
    p.add_argument("--usd-ceiling", type=float, default=100.0)
    p.add_argument("--approved-by", default="user")
    p.add_argument("--approved-at", default=None,
                   help="裁决时间（缺省=现在，UTC Z）")
    p.add_argument("--scope", default="protocol")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("model-approval", help="模型 ID 批准登记（硬规则 1 产物化）")
    _common(p)
    p.add_argument("--decided-at", required=True, help="用户裁决时间（UTC Z）")
    p.add_argument("--basis", required=True, help="用户逐字裁决出处（≥20 字）")
    p.add_argument("--approved-by", default="user")
    p.set_defaults(func=cmd_model_approval)

    for name, fn, helptext in (
        ("protocol", cmd_protocol, "六次协议探针（G-P 后；SIMPRAXIS_ALLOW_PAID=1）"),
        ("develop", cmd_develop, "三族开发轨迹（G-P 后）"),
        ("validate", cmd_validate, "六条验证轨迹（G-V 后）"),
    ):
        p = sub.add_parser(name, help=helptext)
        _common(p)
        p.add_argument("--batch", default="R0-P" if name == "protocol" else
                       ("R0-D-E" if name == "develop" else "R0-V-E1"))
        p.set_defaults(func=fn)

    p = sub.add_parser("closeout", help="零付费结案件（缺件如实记 NOT_RUN）")
    _common(p)
    p.set_defaults(func=cmd_closeout)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PaidPathNotArmed as exc:
        _emit({"command": args.command, "refused": str(exc)})
        return EXIT_REFUSED
    except (RunnerError, gates.GateRefused, gates.ApprovalError) as exc:
        _emit({"command": args.command, "error": str(exc)})
        return EXIT_REFUSED
    except rp.RsiR0CampaignUnsetError as exc:
        _emit({"command": args.command, "error": str(exc)})
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
