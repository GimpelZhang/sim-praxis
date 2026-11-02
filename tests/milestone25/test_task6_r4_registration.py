#!/usr/bin/env python3
"""Phase 2.5 Task6 Stage B（r4）— 登记件 sha 一致性（hermetic）。

预注册 §7.2 的登记表把三件判据工具的 sha256 写进**文本**；§7.1 黑名单又是
按「件」判的。两者脱钩就会出最坏的情况：表里钉的是上一版 sha，而实跑的是
修过的一版——登记失效而无人发现（GATE1 规格 lane M3）。

本文件逐字解析 §7.2 表格并与实际字节比对，任一侧漂移即红。首呼之后若要
动这三件（含"只补注释"），先改表再改件，且按 §7.1 那是黑名单行为——
测试会逼着人先面对这件事。

全 hermetic：只读仓库内文本，不连库、不联网、0 付费。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PREREG = REPO_ROOT / "scripts/phase25/task6_r4_prereg.md"

_ROW_RE = re.compile(
    r"^\|\s*`(scripts/phase25/[^`]+\.py)`\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$")


def _registered() -> dict:
    text = PREREG.read_text(encoding="utf-8")
    head = text.find("**首呼前登记的三个件 sha**")
    assert head != -1, "预注册件缺 §7.2 登记表"
    tail = text.find("一致性机判", head)
    assert tail != -1, "§7.2 登记表后缺一致性机判说明"
    rows = {}
    for line in text[head:tail].splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def test_registration_table_lists_three_tools():
    rows = _registered()
    assert set(rows) == {
        "scripts/phase25/task6_r4_verdict.py",
        "scripts/phase25/task6_r4_reference_sim.py",
        "scripts/phase25/gen_task6_r4_window_evidence.py",
    }, sorted(rows)


def test_registered_shas_match_working_tree_bytes():
    """§7.2 表 = 实际件字节。漂移即黑名单命中（首呼后）或登记过期（首呼前）。"""
    rows = _registered()
    drift = {}
    for rel, want in rows.items():
        got = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        if got != want:
            drift[rel] = {"registered": want, "actual": got}
    assert not drift, f"登记 sha 与落地件不符：{drift}"


def test_registration_covers_a8_repair_round():
    """门 1 修复轮（A8）必须留在预注册件里，否则口径无出处。"""
    text = PREREG.read_text(encoding="utf-8")
    assert "### 13.6 A8 门 1 修复轮" in text
    assert "gate_restamp_other_rows" in text      # C3(b) 硬条件
    assert "docker ps -a" in text                 # C2 存量口径
    assert "declared_by_executor" in text         # 残余「执行方声明」如实留痕
