#!/usr/bin/env python3
"""Track 输入解析：Task0 扩编固化的 inputs/tracks/track_{1,2}_samples.txt。

文件是评审输入的冻结原件（Phase 2 Task0 起逐字复制自 ~/文档，各 6 条，
sha256 见 Task0 commit body），解析器只做结构化，不改写一字：
  track_1: 序号 + 工具名 + 若干 URL（repo/主页/论文，按出现顺序保留）
  track_2: 序号 + 题目原文（可能跨多行，直到下一个序号/文件结束）

# 锁内适配（Task0）：计划 v1.2 写「7/6 条」，实测 ~/文档 原件两份均为 6 条
# （track_1 sha256 8eb02971…、track_2 0f25cf69…）；冻结原则是逐字保真，
# 不自造第 7 条，按 6/6 固化并在 Task0 报告登记偏差。解析逻辑本就支持 N 条，
# 序号 1..N 连续自检自动覆盖扩编。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACK1_FILE = REPO_ROOT / "inputs" / "tracks" / "track_1_samples.txt"
TRACK2_FILE = REPO_ROOT / "inputs" / "tracks" / "track_2_samples.txt"

_URL_RE = re.compile(r"https?://\S+")
_HEAD_RE = re.compile(r"^(\d+)\.\s*(.+?)\s*$")


def parse_track1(path: "str | Path" = TRACK1_FILE) -> list[dict]:
    """→ [{"index":1,"name":"ManiSkill","urls":[...]}]。"""
    text = Path(path).read_text(encoding="utf-8")
    entries: list[dict] = []
    cur: "dict | None" = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEAD_RE.match(line)
        if m and not line.lower().startswith("http") and ":" in m.group(2):
            cur = {"index": int(m.group(1)),
                   "name": m.group(2).rstrip(":").strip(),
                   "urls": []}
            entries.append(cur)
            continue
        if cur is not None:
            cur["urls"].extend(_URL_RE.findall(line))
    if not entries:
        raise ValueError(f"track1 输入解析为空：{path}")
    # 解析自检（评审 L6）：序号必须 1..N 连续不跳号，防冻结文件格式漂移被静默误切
    if [e["index"] for e in entries] != list(range(1, len(entries) + 1)):
        raise ValueError(f"track1 序号不连续：{[e['index'] for e in entries]}（{path}）")
    return entries


def parse_track2(path: "str | Path" = TRACK2_FILE) -> list[dict]:
    """→ [{"index":1,"question":"...（原文，含换行折叠为空格）"}]。"""
    text = Path(path).read_text(encoding="utf-8")
    entries: list[dict] = []
    cur: "dict | None" = None
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^(\d+)\.\s*(.+)$", line)
        if m:
            if cur is not None:
                cur["question"] = " ".join(buf).strip()
            cur = {"index": int(m.group(1)), "question": ""}
            entries.append(cur)
            buf = [m.group(2).strip()]
        elif cur is not None and line:
            buf.append(line)
    if cur is not None:
        cur["question"] = " ".join(buf).strip()
    for e in entries:
        if not e["question"]:
            raise ValueError(f"track2 第 {e['index']} 题正文为空：{path}")
    if not entries:
        raise ValueError(f"track2 输入解析为空：{path}")
    if [e["index"] for e in entries] != list(range(1, len(entries) + 1)):
        raise ValueError(f"track2 序号不连续：{[e['index'] for e in entries]}（{path}）")
    return entries


def get_input(track: str, index: int) -> dict:
    """按 track 名与序号取冻结输入；track1 返回工具条目，track2 返回题目条目。"""
    if track == "track1":
        items = parse_track1()
    elif track == "track2":
        items = parse_track2()
    else:
        raise ValueError(f"未知 track={track!r}，合法值 track1|track2")
    for it in items:
        if it["index"] == index:
            return {"track": track, **it}
    raise ValueError(f"{track} 无序号 {index}（可选 {[i['index'] for i in items]}）")
