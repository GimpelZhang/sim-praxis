"""飞书审批网关命令行入口（详细计划 §2 Task 1，L405/L426）。

子命令：
  decide      仅在 --cli 时可用：stdin 输入 y/n + 意见，mark_decided(source="cli")，
              不触网、不需要飞书凭据（直接写 pending_gates.db，供重启/断网人工裁决）；
  send-probe  向 owner 发一张标注「探针」的 Gate1 卡片（需 FEISHU_* 环境变量）；
  await       阻塞等待某 thread/gate 的决策并打印 JSON（需 FEISHU_*，启动 WS）。

无 --cli 且 FEISHU 凭据缺失：非零退出报错，禁止默认批准。
"""
from __future__ import annotations

import argparse
import json
import sys

from .client import FeishuAPIError, gateway_from_env
from .cards import build_gate1_card, build_gate1_card_v2
from .store import PendingStore, default_db_path


def _ask_decision(gate: int, choice_default: int) -> dict:
    """stdin 交互：y/n；reject 意见非空否则重问。返回落库参数。"""
    while True:
        ans = input("请输入决策（y=批准，n=打回并提出意见）[y/n]：").strip().lower()
        if ans in ("y", "n"):
            break
        print("只能输入 y 或 n，请重试。")
    if ans == "y":
        choice = choice_default if gate == 1 else None
        return {"decision": "approve", "choice": choice, "human_notes": ""}
    while True:
        note = input("请输入打回意见（必填，会原样带回重做，空输入将重问）：\n> ").strip()
        if note:
            return {"decision": "reject_with_comments", "choice": None,
                    "human_notes": note}
        print("打回必须填写意见。")


def _cmd_decide(args: argparse.Namespace) -> int:
    store = PendingStore(args.db)
    row = store.get_pending(args.thread, args.gate)
    if row is None:
        print(f"未找到 PENDING 行：thread={args.thread} gate={args.gate}", file=sys.stderr)
        return 2
    if row["status"] != "PENDING":
        print(f"该行状态为 {row['status']}，不可再决策", file=sys.stderr)
        return 2
    d = _ask_decision(args.gate, args.choice)
    ok = store.mark_decided(
        args.thread, args.gate,
        decision=d["decision"], source="cli",
        choice=d["choice"], human_notes=d["human_notes"])
    if not ok:
        print("落库失败（并发：可能已被卡片/其他 CLI 先决策）", file=sys.stderr)
        return 2
    result = store.get_pending(args.thread, args.gate)
    print(json.dumps({k: result[k] for k in
                      ("status", "decision", "choice", "human_notes", "source")},
                     ensure_ascii=False, indent=2))
    store.close()
    return 0


def _cmd_send_probe(args: argparse.Namespace) -> int:
    try:
        gw = gateway_from_env(start_ws=False)
    except RuntimeError as exc:
        print(f"[send-probe] {exc}", file=sys.stderr)
        return 2
    proposals = [
        {"name": "探针方案 P1（卡片往返验证）", "sim_engine": "-",
         "first_principles": "验证 form/textarea/form_submit 真实往返",
         "expected_artifacts": "toast/回调证据"},
        {"name": "探针方案 P2（对照项）", "sim_engine": "-",
         "first_principles": "多方案选择按钮", "expected_artifacts": "-"},
    ]
    try:
        sent = gw._send_interactive(  # noqa: SLF001 — CLI 复用与生产同一条发卡路径
            gw.owner_open_id,
            build_gate1_card("探针任务（不影响任何真实 run）", proposals),
            build_gate1_card_v2("探针任务（不影响任何真实 run）", proposals),
        )
    except FeishuAPIError as exc:
        print(f"[send-probe] 发卡失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(sent, ensure_ascii=False))
    gw.close()
    return 0


def _cmd_await(args: argparse.Namespace) -> int:
    try:
        gw = gateway_from_env()
    except RuntimeError as exc:
        print(f"[await] {exc}", file=sys.stderr)
        return 2
    try:
        result = gw._await_decision(  # noqa: SLF001
            args.thread, args.gate, max_wait=args.max_wait)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        gw.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator.feishu_gateway",
                                description="SimPraxis 飞书真实审批网关")
    p.add_argument("--cli", action="store_true",
                   help="启用 stdin 人工裁决旁路（decide 子命令必须带）")
    p.add_argument("--db", default=str(default_db_path()),
                   help="pending_gates.db 路径（默认 $SIMPRAXIS_PHASE1_ROOT/state/）")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("decide", help="stdin 人工裁决（source=cli，不触网）")
    d.add_argument("--thread", required=True)
    d.add_argument("--gate", type=int, required=True, choices=(1, 2))
    d.add_argument("--choice", type=int, default=0,
                   help="Gate1 批准时的方案索引（0 起，默认 0）")
    d.set_defaults(func=_cmd_decide, cli_only=True)

    s = sub.add_parser("send-probe", help="发一张探针 Gate1 卡片给 owner")
    s.set_defaults(func=_cmd_send_probe, cli_only=False)

    a = sub.add_parser("await", help="阻塞等待决策并打印 JSON")
    a.add_argument("--thread", required=True)
    a.add_argument("--gate", type=int, required=True, choices=(1, 2))
    a.add_argument("--max-wait", type=float, default=None)
    a.set_defaults(func=_cmd_await, cli_only=False)
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "cli_only", False) and not args.cli:
        print("decide 仅允许在显式 --cli 下使用（防止自动化路径误触人工旁路）",
              file=sys.stderr)
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
