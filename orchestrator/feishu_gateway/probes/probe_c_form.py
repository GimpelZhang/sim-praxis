#!/usr/bin/env python3
"""探针 C：Gate1 卡片 form 真实往返（三轮独立 WS 进程，避免单长进程操作混乱）。

每轮独立运行、独立 thread 主键：
  --round empty    卡片#1：什么都不填，直接点「打回并提出意见」
                   期望：红色 error toast「打回必须填写意见」，DB 无 DECIDED 行；
  --round filled   卡片#2：在意见框输入脚本指定文本后点「打回并提出意见」
                   期望：success toast，feedback 原样落库 human_notes；
  --round approve  卡片#3：点「批准方案 1」
                   期望：success toast，status=DECIDED，choice=0，source=card_form。
三轮证据合并写 probe_c_form.json；任一轮 exit 2。

运行：
  python -m orchestrator.feishu_gateway.probes.probe_c_form --round empty
  python -m orchestrator.feishu_gateway.probes.probe_c_form --round filled
  python -m orchestrator.feishu_gateway.probes.probe_c_form --round approve
"""
from __future__ import annotations

import argparse
import sys
import time

from ..cards import build_gate1_card, build_gate1_card_v2
from ..client import FeishuAPIError, gateway_from_env
from ._evidence import (
    confirm,
    now_iso,
    print_checklist,
    read_evidence,
    write_evidence,
)

WAIT_SECONDS = 240
PROPOSALS = [
    {"name": "探针方案 P1（form 往返）", "sim_engine": "-",
     "first_principles": "验证 form_submit/textarea/value 真实落库",
     "expected_artifacts": "probe_c_form.json"},
    {"name": "探针方案 P2（对照）", "sim_engine": "-",
     "first_principles": "验证多方案 choice 索引", "expected_artifacts": "-"},
]

ROUND_THREAD = {
    "empty": "probe_c_empty",
    "filled": "probe_c_filled",
    "approve": "probe_c_approve",
}


def _wait_action(gw, thread_id: str, predicate, timeout: int) -> "dict | None":
    """等 on_card_action 捕获满足 predicate 的回调；同时轮询 DB 兜底。"""
    deadline = time.time() + timeout
    captured: list[dict] = []
    gw._on_card_action = lambda info: captured.append(info)  # noqa: SLF001
    while time.time() < deadline:
        if captured and predicate(captured[-1]):
            return captured[-1]
        time.sleep(0.5)
    return captured[-1] if captured else None


def run_round(which: str) -> int:
    # 锁内适配（探针执行 2026-10-14 实测）：store.upsert_pending 对同主键
    # PENDING 行 COALESCE 不覆盖 card_message_id（生产重启重挂保护，正确）。
    # 探针三轮重跑若复用固定 thread_id，会粘住首张 1.0 废卡 id 导致新卡点击
    # 全部 no_pending。生产中每个审批请求本来就是新 thread_id，故探针每轮也用
    # 时间戳唯一 id；并在开跑前经 store.purge_thread_like 清理历史 probe_c_*
    # 合成行（仅探针自有字面前缀；生产路径无删除入口）。
    thread_id = f"{ROUND_THREAD[which]}_{int(time.time())}"
    feedback_text = f"探针C-填充意见-{int(time.time())}"
    if which == "empty":
        print_checklist("探针 C / 第 1 轮（空意见打回必须被拒）", [
            "在飞书打开刚收到的 Gate1 探针卡片",
            "不要在任何意见框输入内容",
            "直接点击方案 1 下方红色「打回并提出意见」",
            "期望：弹出红色 toast「打回必须填写意见」，卡片不消失",
            "完成后回本终端确认",
        ])
    elif which == "filled":
        print_checklist("探针 C / 第 2 轮（填意见打回，原文落库）", [
            "在新收到卡片的【方案 1】意见框里原样输入（可复制）：",
            f"    {feedback_text}",
            "然后点击该方案下方红色「打回并提出意见」",
            "期望：成功 toast；终端会比对意见是否原样落库",
        ])
    else:
        print_checklist("探针 C / 第 3 轮（批准方案 1）", [
            "在新收到卡片上直接点击方案 1 的蓝色「批准方案 1」",
            "期望：成功 toast；DB choice=0、source=card_form",
        ])

    try:
        gw = gateway_from_env()
    except RuntimeError as exc:
        print(f"[probe-c] {exc}", file=sys.stderr)
        return 2
    try:
        for _ in range(20):
            if getattr(gw._ws_client, "_conn", None) is not None:  # noqa: SLF001
                break
            time.sleep(0.5)
        try:
            sent = gw._send_interactive(  # noqa: SLF001
                gw.owner_open_id,
                build_gate1_card(f"探针C-{which}（不影响真实 run）", PROPOSALS),
                build_gate1_card_v2(f"探针C-{which}（不影响真实 run）", PROPOSALS),
                form_required=True)
        except FeishuAPIError as exc:
            print(f"[probe-c] 发卡失败：{exc}", file=sys.stderr)
            return 2
        # 清理历次探针运行遗留的合成行（LIKE 只命中 probe_c_ 前缀，绝不触生产行）。
        # H-17：禁止裸 DELETE——统一走 store.purge_thread_like，同事务清主表/
        # history/inbound_messages，避免探针残留污染 UNION 守卫与去重表。
        gw.store.purge_thread_like(r"probe_c\_%")
        gw.store.upsert_pending(
            thread_id, 1, card_message_id=sent["message_id"],
            open_chat_id=sent["chat_id"])
        print(f"[probe-c] {which} 卡片已发 schema={sent['schema']} "
              f"message_id={sent['message_id']}", flush=True)

        if which == "empty":
            action = _wait_action(
                gw, thread_id,
                lambda info: info.get("reason") == "empty_feedback", WAIT_SECONDS)
            row = gw.store.get_pending(thread_id, 1)
            checks = [
                {"item": "收到 error toast", "expected": "error",
                 "actual": (action or {}).get("toast", {}).get("type"),
                 "passed": bool(action and action["toast"]["type"] == "error")},
                {"item": "toast 文案", "expected": "打回必须填写意见",
                 "actual": (action or {}).get("toast", {}).get("content"),
                 "passed": bool(action and
                                action["toast"]["content"] == "打回必须填写意见")},
                {"item": "DB 仍为 PENDING（无 DECIDED）", "expected": "PENDING",
                 "actual": row["status"] if row else None,
                 "passed": bool(row and row["status"] == "PENDING")},
            ]
        elif which == "filled":
            action = _wait_action(
                gw, thread_id,
                lambda info: info.get("decided") is True
                and info.get("decision") == "reject_with_comments",
                WAIT_SECONDS)
            row = gw.store.get_pending(thread_id, 1)
            notes = (row or {}).get("human_notes", "")
            checks = [
                {"item": "收到 success toast", "expected": "success",
                 "actual": (action or {}).get("toast", {}).get("type"),
                 "passed": bool(action and action["toast"]["type"] == "success")},
                {"item": "status=DECIDED decision=reject_with_comments",
                 "expected": "DECIDED/reject_with_comments",
                 "actual": None if not row else
                           f"{row['status']}/{row['decision']}",
                 "passed": bool(row and row["status"] == "DECIDED"
                                and row["decision"] == "reject_with_comments")},
                {"item": "feedback 原样落库 human_notes", "expected": feedback_text,
                 "actual": notes, "passed": feedback_text in notes},
                {"item": "source=card_form", "expected": "card_form",
                 "actual": (row or {}).get("source"),
                 "passed": bool(row and row["source"] == "card_form")},
            ]
        else:
            action = _wait_action(
                gw, thread_id,
                lambda info: info.get("decided") is True
                and info.get("decision") == "approve", WAIT_SECONDS)
            row = gw.store.get_pending(thread_id, 1)
            checks = [
                {"item": "收到 success toast", "expected": "success",
                 "actual": (action or {}).get("toast", {}).get("type"),
                 "passed": bool(action and action["toast"]["type"] == "success")},
                {"item": "status=DECIDED", "expected": "DECIDED",
                 "actual": (row or {}).get("status"),
                 "passed": bool(row and row["status"] == "DECIDED")},
                {"item": "choice=0（批准方案 1）", "expected": 0,
                 "actual": (row or {}).get("choice"),
                 "passed": bool(row and row["choice"] == 0)},
                {"item": "source=card_form", "expected": "card_form",
                 "actual": (row or {}).get("source"),
                 "passed": bool(row and row["source"] == "card_form")},
            ]

        manual_confirmed = (action is not None) and confirm(
            f"请确认你在飞书完成了 {which} 轮要求的点按操作？")
        checks.append({"item": "人工确认点按操作", "expected": True,
                       "actual": manual_confirmed, "passed": manual_confirmed})
        passed = bool(action) and all(c["passed"] for c in checks)

        evidence = read_evidence("probe_c_form.json")
        evidence["probe"] = "c_card_form"
        evidence.setdefault("rounds", {})[which] = {
            "ran_at": now_iso(),
            "thread_id": thread_id,
            "card_schema": sent["schema"],
            "card_message_id": sent["message_id"],
            "prescribed_feedback": feedback_text,
            "action": action,
            "row": gw.store.get_pending(thread_id, 1),
            "checks": checks,
            "manual_confirmed": manual_confirmed,
            "passed": passed,
        }
        path = write_evidence("probe_c_form.json", evidence)
        print(f"[probe-c] {which} passed={passed}；证据 {path}", flush=True)
        return 0 if passed else 2
    finally:
        gw.close()


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="探针 C：Gate1 form（分三轮运行）")
    p.add_argument("--round", required=True, choices=("empty", "filled", "approve"))
    args = p.parse_args(argv)
    return run_round(args.round)


if __name__ == "__main__":
    sys.exit(main())
