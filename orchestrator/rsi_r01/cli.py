"""RSI-R0.1 CLI（T0 起按任务逐步补齐；计划 §10 只登记运行类入口）。

T0 只实现 ``init``：创建/幂等加载 campaign 根与不可变上下文。初始化不触网、
不要求 env 注入成功（计划 §10：不能因 env 失败把阶段停在无法结案状态）。

后续任务的子命令（check-instrument/lock/probe/develop/validate/closeout）
在各自 Task 中实现；**不提供**跳过 G-I/G-D/G-V 的 force 旗标。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.rsi_r01 import campaign as camp


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
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        return _cmd_init(args)
    raise SystemExit(f"未知子命令：{args.cmd}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
