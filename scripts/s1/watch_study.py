#!/usr/bin/env python3
"""RSI-S1 研究运行看门狗（进程死亡 + 静默双条件）。

背景（2026-11-18 事故）：Gate1 批准后首个触点因接线缺陷崩溃，进程立即退出，
但无人监测 → 被误报为"仍在等待"。看门狗的数据面是 ``progress.jsonl``：

* **进程死亡**：pid 消失且无终态事件（study_end/study_crashed）→ PROCESS_DIED；
* **静默**：最近事件超过 ``--silence`` 秒（会话/物理都可能慢，阈值可调）→ SILENT；
* 终态事件出现即退出（0=正常终态，2=进程死亡，3=静默未恢复被外部停止）。

用法::

    python scripts/s1/watch_study.py --root <evidence_root> --pid <pid> --silence 900
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

TERMINAL = ("study_end", "study_crashed")


def _events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch(root: Path, *, pid: int | None, silence_s: float,
          interval: float, max_rounds: int | None) -> int:
    progress = Path(root) / "progress.jsonl"
    alerts = Path(root) / "watchdog_alerts.jsonl"
    rounds = 0
    silent_alerted = False
    while True:
        rounds += 1
        events = _events(progress)
        last = events[-1] if events else None
        terminal = last is not None and last.get("event") in TERMINAL
        alive = _pid_alive(pid) if pid else (not terminal if events else True)
        if terminal:
            print(json.dumps({"watchdog": "terminal", "last": last},
                             ensure_ascii=False))
            return 0
        if pid and not alive:
            row = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "alert": "PROCESS_DIED", "last_event": last}
            with open(alerts, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False))
            return 2
        # 静默判定：以文件 mtime 为事件时钟（progress 每事件写一行）
        age = time.time() - progress.stat().st_mtime if progress.is_file() else None
        if age is not None and age > silence_s and not silent_alerted:
            row = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "alert": "SILENT", "silent_s": round(age, 1),
                   "last_event": last}
            with open(alerts, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False))
            silent_alerted = True
        if max_rounds is not None and rounds >= max_rounds:
            print(json.dumps({"watchdog": "max_rounds", "last": last},
                             ensure_ascii=False))
            return 3
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 运行看门狗")
    parser.add_argument("--root", required=True)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--silence", type=float, default=900.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--max-rounds", type=int, default=0)
    args = parser.parse_args(argv)
    return watch(Path(args.root), pid=args.pid or None,
                 silence_s=args.silence, interval=args.interval,
                 max_rounds=args.max_rounds or None)


if __name__ == "__main__":
    raise SystemExit(main())
