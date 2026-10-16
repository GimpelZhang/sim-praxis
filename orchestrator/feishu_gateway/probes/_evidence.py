"""探针共用：人工操作清单醒目打印 + 证据 JSON 落盘（原子写，0600）。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.common.fsutil import atomic_write_text


def evidence_dir() -> Path:
    root = os.environ.get("SIMPRAXIS_PHASE1_ROOT",
                          "/mnt/artifacts/simpraxis/phase1")
    d = Path(root) / "task1_feishu"
    d.mkdir(parents=True, exist_ok=True)
    return d


def print_checklist(title: str, steps: list[str]) -> None:
    bar = "=" * 72
    print("\n" + bar, flush=True)
    print(f"  {title}", flush=True)
    print(bar, flush=True)
    for i, s in enumerate(steps, 1):
        print(f"  [{i}] {s}", flush=True)
    print(bar + "\n", flush=True)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_evidence(name: str, payload: dict[str, Any]) -> Path:
    """tmp+replace 原子写，权限 0600；同文件可多次合并（probe C 三轮写同一文件）。"""
    path = evidence_dir() / name
    payload = {"updated_at": now_iso(), **payload}
    # Task0（arch 合并）：第二套 mkstemp 实现删除，统一走
    # orchestrator.common.fsutil.atomic_write_text（mkstemp→fchmod→fsync→replace）。
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def read_evidence(name: str) -> dict[str, Any]:
    path = evidence_dir() / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def confirm(prompt: str) -> bool:
    """终端人工 y 确认（探针 D 可播放确认、B/C 本人操作确认）。"""
    try:
        return input(f"\n{prompt}（确认请输入 y，其余视为未确认）：").strip().lower() == "y"
    except EOFError:
        return False
