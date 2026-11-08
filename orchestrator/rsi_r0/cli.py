"""RSI-R0 唯一入口（AC21：全部路径显式 R0，禁止转旧阶段执行入口）。

子命令按计划 §10：

    preflight  → 零付费前置体检（路径/账套/封存/判据/发网事实账）
    controls   → 21 负控 + 6 mutation + 判据冻结核对（零容器、零模型）
    dry-run    → mock 模型零付费端到端（POST 恒 0，可中断可恢复）
    lock       → 写一次即锁的完整快照；--verify 只校验不回填
    protocol   → G-P 门后的六次协议探针（T5 起接线；现在只到门）
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
from orchestrator.rsi_r0.attempts import AttemptJournal
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
    _emit({"command": cmd, "gate": verdict, "note": "T5 起接线（T4 只交付门与拒绝路径）"})
    return EXIT_REFUSED


def cmd_protocol(args: argparse.Namespace) -> int:
    return _paid("protocol", args, "G-P")


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
        "attempts": {"requests": len(latest), "http_posts": posts},
        "evidence_scope": "zero_paid_only",
        "not_run": ["protocol(six)", "development(three)", "validation(six)"],
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
    p.set_defaults(func=cmd_lock)

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
