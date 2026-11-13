"""RSI-R1.2 T9 收尾命令面（``closeout|pack|judge``）——与冻结闭包**分离**。

锁内适配（2026-11-13，用户裁决全量重跑 + 修上一轮尾债）：这三条命令原本挂在
``cli_r12.py`` 上，而 ``cli_r12.py`` 属于冻结闭包组 ``entry``——每轮收尾加命令
都会把**下一轮**的 ``verify_lock`` 打成 ``CLOSURE_DRIFT``（上一轮实测
``21b65385… → 95f5c856…``）。收尾工具是「事后审计面」，不是被测量的仪器：
把它搬出冻结面后，收尾工具自身的演进不再污染被测闭包；被测闭包的完整性仍由
``check_rsi_r12_closure.py``（独立进程、逐组重算）与证据包逐件 sha 保证。

本模块**不** import ``cli_r12`` 的模块级命名（避免循环导入）：所有共享入口在
函数内部延迟导入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.rsi_r1 import closeout_r12 as CO12


def cmd_closeout(args: argparse.Namespace) -> int:
    """T9 结案：按账本补记族用量 → 关根 → 写 readout/失败链。

    rc：0=过程闭合（process=COMPLETE），1=记录互相矛盾（CLOSEOUT_INCOMPLETE）。
    研究结论在 claim/r1 轴，永不由本命令晋升。
    """
    from orchestrator.rsi_r1 import cli_r12 as CLI12

    root = CLI12._resolve_root(args.root, args.envelope)
    envelope = CLI12._load_envelope(root)
    ctx = CLI12._r12_ctx(root, str(envelope["envelope_id"]))
    closed = CO12.close_round(
        ctx, reason=str(args.reason or "T9 结案：按账本补记族用量并关闭本轮"))
    body = CO12.build(ctx, image_pin=args.image, repo_root=args.repo_root)
    axes = body["axes"]
    if closed.get("family_note"):
        print(f"WARN {closed['family_note']}", file=sys.stderr)
    print(f"closeout ok  envelope={body['envelope_id']}  "
          f"root_state={body['root_state']}  "
          f"family_member_closed={closed.get('family_member_closed')}")
    print(f"  axes: process={axes['process']} measurement={axes['measurement']} "
          f"study_execution={axes['study_execution']} claim={axes['claim']} "
          f"r1={axes['r1']}")
    print(f"  posts: history={body['totals']['history_posts']} "
          f"closed={body['totals']['closed_rounds_posts']} "
          f"round={body['totals']['round_calls']} "
          f"family_total={body['totals']['family_total']} "
          f"cap={body['totals']['family_cap']}")
    print(f"  failures={len(body['failure_chain'])} gaps={len(body['gaps'])}")
    for gap in body["gaps"]:
        print(f"    gap: {gap}")
    print(f"  readout={ctx.relative(CO12.READOUT_RELPATH)}")
    return 0 if axes["process"] == "COMPLETE" else 1


def cmd_pack(args: argparse.Namespace) -> int:
    """把 R1.2 盘面逐件复制进 ``evidence_pack/``（含新版独立复算器源码）。"""
    from orchestrator.rsi_r1 import cli_r12 as CLI12

    root = CLI12._resolve_root(args.root, args.envelope)
    envelope = CLI12._load_envelope(root)
    ctx = CLI12._r12_ctx(root, str(envelope["envelope_id"]))
    dest = Path(args.dest).expanduser() if args.dest else None
    manifest = CO12.build_pack(ctx, dest=dest, repo_root=args.repo_root)
    checked = CO12.verify_pack(dest or ctx.relative("evidence_pack"))
    print(f"pack ok  envelope={envelope['envelope_id']}  "
          f"n_files={manifest['n_files']}  total_bytes={manifest['total_bytes']}")
    print(f"  pack   : {dest or ctx.relative('evidence_pack')}")
    print(f"  verify : ok={checked['ok']}  problems={len(checked['problems'])}")
    for problem in checked["problems"][:10]:
        print(f"    - {problem}")
    return 0 if checked["ok"] else 1


def cmd_judge(args: argparse.Namespace) -> int:
    """外部进程跑独立复算器（第二遍由 ``-I`` 纯标准库进程执行）。"""
    from orchestrator.rsi_r1 import cli_r12 as CLI12

    root = CLI12._resolve_root(args.root, args.envelope)
    envelope = CLI12._load_envelope(root)
    ctx = CLI12._r12_ctx(root, str(envelope["envelope_id"]))
    pack = False if args.no_pack else (Path(args.verify_pack) if args.verify_pack
                                       else None)
    judged = CO12.run_judge(
        ctx, repo_root=args.repo_root, pack=pack,
        report=Path(args.report).expanduser() if args.report else None)
    print(f"judge {judged.get('verdict')}  rc={judged.get('returncode')}  "
          f"envelope={envelope['envelope_id']}")
    print(f"  problems={len(judged.get('problems') or [])}  "
          f"gaps={len(judged.get('gaps') or [])}")
    for problem in (judged.get("problems") or [])[:10]:
        print(f"    - {problem}")
    print(f"  report={judged.get('report_path')}")
    return 0 if judged.get("ok") else 1


def register(sub: argparse._SubParsersAction) -> None:
    """把三条收尾命令挂到 R1.2 子命令表（cli_r12.build_parser 调用一次）。"""
    from orchestrator.rsi_r1 import cli_r12 as CLI12  # noqa: F401  （保证同包已加载）

    def _root_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=None)
        p.add_argument("--envelope", default=None)

    pco = sub.add_parser(
        "closeout",
        help="T9：结案（五轴/失败链/族账对账；补记族用量并关根，零 POST）")
    _root_args(pco)
    pco.add_argument("--reason", default=None, help="关根理由（写进 state journal）")
    pco.add_argument("--image", default=None, help="镜像 pin（容器回收探针用）")
    pco.add_argument("--repo-root", default=None,
                     help="仓库根（冻结后漂移重算用；缺省为本包所在仓库）")
    pco.set_defaults(func=cmd_closeout)

    ppk = sub.add_parser(
        "pack", help="T9：复制进 evidence_pack/ 并写逐件 sha256 manifest（零 POST）")
    _root_args(ppk)
    ppk.add_argument("--dest", default=None,
                     help="包目录（缺省 <root>/evidence_pack）")
    ppk.add_argument("--repo-root", default=None)
    ppk.set_defaults(func=cmd_pack)

    pjd = sub.add_parser(
        "judge", help="T9：外部进程跑独立复算器（可选同时核对证据包）")
    _root_args(pjd)
    pjd.add_argument("--report", default=None)
    pjd.add_argument("--repo-root", default=None)
    pjd.add_argument("--verify-pack", default=None, help="核对指定证据包目录")
    pjd.add_argument("--no-pack", action="store_true",
                     help="明确不核对证据包（缺省：存在 <root>/evidence_pack 就核）")
    pjd.set_defaults(func=cmd_judge)
