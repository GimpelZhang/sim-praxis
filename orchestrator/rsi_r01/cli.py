"""RSI-R0.1 CLI（T0 起按任务逐步补齐；计划 §10 只登记运行类入口）。

T0 只实现 ``init``：创建/幂等加载 campaign 根与不可变上下文。初始化不触网、
不要求 env 注入成功（计划 §10：不能因 env 失败把阶段停在无法结案状态）。

后续任务的子命令（check-instrument/lock/probe/develop/validate/closeout）
在各自 Task 中实现；**不提供**跳过 G-I/G-D/G-V 的 force 旗标。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from orchestrator.rsi_r01 import campaign as camp
from orchestrator.rsi_r01 import lock as L


def _cmd_init(args: "argparse.Namespace") -> int:
    ctx = camp.create_campaign(
        campaign_id=args.campaign_id,
        plan_path=args.plan,
        consent_path=args.consent,
        repo_root=args.repo_root,
        root=args.root,
    )
    print(json.dumps({
        "campaign_id": ctx.campaign_id,
        "root": str(ctx.root),
        "namespace": ctx.namespace,
        "plan_path": ctx.plan_path,
        "plan_sha256": ctx.plan_sha256,
        "consent_sha256": ctx.consent_sha256,
        "code_commit": ctx.code_commit,
        "created_at_utc": ctx.created_at_utc,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_lock(args: "argparse.Namespace") -> int:
    ctx = camp.load_campaign(campaign_id=args.campaign_id, root=args.root)
    try:
        if args.candidate:
            out = L.build_candidate(ctx, repo_root=args.repo_root)
        elif args.finalize:
            out = L.finalize(ctx, repo_root=args.repo_root)
        else:
            out = L.verify_lock(ctx, repo_root=args.repo_root)
    except L.LockError as exc:
        print(json.dumps({"ok": False, "code": exc.code,
                          "problems": exc.problems}, ensure_ascii=False,
                         indent=2))
        return 4
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("verdict", "PASS") == "PASS" else 4


def _cmd_sign(args: "argparse.Namespace") -> int:
    ctx = camp.load_campaign(campaign_id=args.campaign_id, root=args.root)
    cand_path = ctx.root / "contract" / L.CANDIDATE_FILENAME
    if not cand_path.is_file():
        print(json.dumps({"ok": False, "code": "BLOCKED_APPROVAL",
                          "problems": ["缺 lock_candidate（先 lock --candidate）"]},
                         ensure_ascii=False, indent=2))
        return 4
    candidate = json.loads(cand_path.read_text(encoding="utf-8"))
    expected = L.core_sha256(candidate)
    if args.binding_sha != expected or candidate.get("lock_sha256") != expected:
        print(json.dumps({"ok": False, "code": "LOCK_HASH_MISMATCH",
                          "problems": [f"给定 binding-sha 与候选不一致（候选 {expected}）"]},
                         ensure_ascii=False, indent=2))
        return 4
    sig = L.signature_document(
        role=args.role, verdict=args.verdict, binding_sha256=expected,
        signed_by=args.signed_by,
        evidence_sha256=candidate["facts"]["g_i"]["evidence"],
        notes=args.note or [])
    sig["implementation_sha256"] = candidate["facts"]["code"]["implementation_sha256"]
    # 旧签名不得被静默顶掉（reviewer LOW-1）：先归档到 signatures/history/，
    # 再写新签名。修订（rev2…）因此保留完整的签字沿革。
    target = ctx.root / L.SIG_DIR / f"{args.role}.json"
    if target.is_file():
        try:
            prev = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        stamp = str(prev.get("binding_sha256") or "unknown")[:12]
        hist = ctx.root / L.SIG_DIR / "history"
        hist.mkdir(parents=True, exist_ok=True)
        os.chmod(hist, 0o700)
        dest = hist / f"{args.role}-{stamp}.json"
        os.replace(target, dest)
    path = camp.write_json(ctx, f"{L.SIG_DIR}/{args.role}.json", sig)
    print(json.dumps({"ok": True, "path": str(path), "role": args.role,
                      "binding_sha256": expected}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> "argparse.ArgumentParser":
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.rsi_r01.cli",
        description="RSI-R0.1 入口（T0：init；运行类入口逐 Task 添加）")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser(
        "init", help="创建/幂等加载 campaign 根与不可变上下文（不触网）")
    p_init.add_argument("--campaign-id", required=True)
    p_init.add_argument("--plan", required=True,
                        help="已批准计划的路径（G0 证据；内容 hash 进上下文）")
    p_init.add_argument("--consent", required=True,
                        help="用户实施授权的逐字文本文件（AC01；缺失即拒绝）")
    p_init.add_argument("--repo-root", default=str(Path.cwd()),
                        help="代码仓根（默认当前目录；用于绑定 code_commit）")
    p_init.add_argument("--root", default=None,
                        help="家族根覆盖（测试/演练用；生产缺省 $DATA_ROOT/rsi_r01）")

    p_lock = sub.add_parser(
        "lock", help="T5 最终 LOCK：--candidate 生成候选 / --finalize 汇总双签 / 缺省执行复核")
    p_lock.add_argument("--campaign-id", required=True)
    p_lock.add_argument("--root", default=None)
    p_lock.add_argument("--repo-root", default=str(L.REPO_ROOT))
    p_lock.add_argument("--candidate", action="store_true",
                        help="重算事实并写 contract/lock_candidate.json")
    p_lock.add_argument("--finalize", action="store_true",
                        help="双签齐备后写 contract/final_lock.json")
    p_lock.add_argument("--verify", action="store_true",
                        help="重算事实并校验自哈希/双签（默认动作）")

    p_sign = sub.add_parser(
        "sign", help="G-I 独立签字（role=reviewer/verifier，绑定同一 lock_sha256）")
    p_sign.add_argument("--campaign-id", required=True)
    p_sign.add_argument("--root", default=None)
    p_sign.add_argument("--role", required=True, choices=L.SIG_ROLES)
    p_sign.add_argument("--verdict", required=True, choices=("APPROVE", "REJECT"))
    p_sign.add_argument("--binding-sha", required=True,
                        help="候选的 lock_sha256（签字对象）")
    p_sign.add_argument("--signed-by", required=True,
                        help="签字人/独立审查遍标识（reviewer 与 verifier 必须不同）")
    p_sign.add_argument("--note", action="append", default=[],
                        help="结论依据（可重复）")
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "lock":
        return _cmd_lock(args)
    if args.cmd == "sign":
        return _cmd_sign(args)
    raise SystemExit(f"未知子命令：{args.cmd}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
