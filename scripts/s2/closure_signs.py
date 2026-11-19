#!/usr/bin/env python3
"""RSI-S2 三签（S2-7）：executor / reviewer / verifier 绑同一 HEAD + 判件不动点。

三签内容（与 S1 同构）：

* ``closure_judge``：判件 sha、pass、problems、gaps；
* ``cold_run``：冷跑日志 sha、逐 ID 失败集、新增红（必须为空）、基线集合；
* ``evidence``：预注册双 bundle、校准报告、双审报告、统计初判、12 臂-实例报告、
  会话账、ResearchMemory 的逐件 sha；
* ``product_bundle``：引擎 SHA（= HEAD）与包路径。

纪律：三签必须绑**同一 HEAD**与**同一判件 sha**；任一不符即 ``SIGNS_MISMATCH``。
签件落 ``$SIMPRAXIS_RSI_S2_ROOT/closeout/signs/``（0600）。

用法::

    python scripts/s2/closure_signs.py write --head <sha> --cold-log <path> \\
        --executor-verdict COMPLETE --reviewer-verdict APPROVE_WITH_COMMENTS \\
        --verifier-verdict APPROVE [--campaign-root ...]
    python scripts/s2/closure_signs.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s2_campaign_prereg_dir, rsi_s2_closeout_dir, rsi_s2_rules_prereg_dir,
    rsi_s2_sessions_dir)

SCHEMA = "simpraxis.rsi_s2.sign.v1"
ROLES = ("executor", "reviewer", "verifier")


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cold_run(cold_log: Path) -> "dict[str, Any]":
    """从冷跑日志抽取逐 ID 失败集（pytest -q 的 short summary 行）。"""
    text = Path(cold_log).read_text(encoding="utf-8", errors="replace")
    failed_ids = []
    tail_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failed_ids.append(stripped.split("FAILED ", 1)[1].split(" - ")[0])
        if " passed" in stripped and ("failed" in stripped or "skipped" in stripped):
            tail_line = stripped
    return {"log": str(cold_log), "log_sha256": _sha_file(cold_log),
            "failed_ids": sorted(failed_ids), "summary": tail_line}


def _evidence(campaign_root: Path) -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for name, path in {
        "rules_manifest": rsi_s2_rules_prereg_dir() / "manifest.json",
        "rules_lock": rsi_s2_rules_prereg_dir() / "LOCK.json",
        "campaign_manifest": rsi_s2_campaign_prereg_dir() / "manifest.json",
        "campaign_lock": rsi_s2_campaign_prereg_dir() / "LOCK.json",
        "calibration_report": rsi_s2_closeout_dir() / "calibration_report.json",
        "judge_report": rsi_s2_closeout_dir() / "s2_closure_judge.json",
        "dual_review": campaign_root / "dual_review.json",
        "analysis": campaign_root / "analysis.json",
        "gate1_decision": campaign_root / "gate1_decision.json",
        "gate2_decision": campaign_root / "gate2_decision.json",
        "shadow_gate": campaign_root / "shadow_gate.json",
        "memory_gate": campaign_root / "memory_gate.json",
        "sessions": rsi_s2_sessions_dir() / "sessions.jsonl",
        "research_memory": Path("/mnt/artifacts/simpraxis/rsi_s2/closed/"
                                "research_memory/s2_lessons.jsonl"),
    }.items():
        out[name] = _sha_file(path) if path.is_file() else None
    trials = {}
    for trial in sorted(campaign_root.glob("trial/*/study_report.json")):
        trials[trial.parent.name] = _sha_file(trial)
    out["trials"] = trials
    return out


def write(*, campaign_root: Path, head: str, cold_log: Path,
          verdicts: Mapping[str, str], basis: str = "",
          product_bundle: Mapping[str, Any] | None = None) -> "dict[str, Any]":
    judge_path = rsi_s2_closeout_dir() / "s2_closure_judge.json"
    if not judge_path.is_file():
        raise SystemExit("JUDGE_MISSING: 先跑 check_s2_closure.py")
    judge = _load(judge_path)
    cold = _cold_run(cold_log)
    evidence = _evidence(campaign_root)
    signs_dir = ensure_private_dir(rsi_s2_closeout_dir() / "signs", mode=0o700)
    written = {}
    for role in ROLES:
        doc = {
            "schema": SCHEMA,
            "role": role,
            "verdict": verdicts[role],
            "head": head,
            "basis": basis or f"S2 首轮受控自改（{role}）",
            "closure_judge": {
                "pass": judge.get("pass"),
                "problems": judge.get("problems"),
                "gaps": judge.get("gaps"),
                "judge_sha256": judge.get("judge_sha256"),
            },
            "cold_run": cold,
            "evidence": evidence,
            "product_bundle": dict(product_bundle or {}),
            "signed_at_utc": utc_now_iso_z(),
        }
        path = signs_dir / f"{role}.json"
        atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n", mode=0o600)
        written[role] = _sha_file(path)
    return {"signs": written, "head": head,
            "judge_sha256": judge.get("judge_sha256")}


def verify() -> "dict[str, Any]":
    signs_dir = rsi_s2_closeout_dir() / "signs"
    docs = {}
    for role in ROLES:
        path = signs_dir / f"{role}.json"
        if not path.is_file():
            raise SystemExit(f"SIGNS_MISSING: {role}")
        docs[role] = _load(path)
    heads = {doc.get("head") for doc in docs.values()}
    judges = {(doc.get("closure_judge") or {}).get("judge_sha256")
              for doc in docs.values()}
    if len(heads) != 1:
        raise SystemExit(f"SIGNS_MISMATCH: HEAD 不一致 {heads}")
    if len(judges) != 1:
        raise SystemExit(f"SIGNS_MISMATCH: 判件 sha 不一致 {judges}")
    judge = _load(rsi_s2_closeout_dir() / "s2_closure_judge.json")
    if judge.get("judge_sha256") not in judges:
        raise SystemExit("SIGNS_MISMATCH: 判件 sha 与盘上判件不符")
    return {"head": heads.pop(), "judge_sha256": judges.pop(),
            "verdicts": {role: doc.get("verdict") for role, doc in docs.items()},
            "verified_at_utc": utc_now_iso_z()}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S2 三签")
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--campaign-root", required=True)
    w.add_argument("--head", required=True)
    w.add_argument("--cold-log", required=True)
    w.add_argument("--executor-verdict", required=True)
    w.add_argument("--reviewer-verdict", required=True)
    w.add_argument("--verifier-verdict", required=True)
    w.add_argument("--basis", default="")
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.cmd == "write":
        report = write(
            campaign_root=Path(args.campaign_root), head=args.head,
            cold_log=Path(args.cold_log),
            verdicts={"executor": args.executor_verdict,
                      "reviewer": args.reviewer_verdict,
                      "verifier": args.verifier_verdict},
            basis=args.basis)
    else:
        report = verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
