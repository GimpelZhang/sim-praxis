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

# 目录白名单含 scripts/kb：窗口快照生成器（分列戳记哈希的承重件）也必须是
# 登记件，否则它的字节可在首呼后被改动而无登记可查（GATE1-LANE-A A10-L1）。
_ROW_RE = re.compile(
    r"^\|\s*`(scripts/(?:phase25|kb)/[^`]+\.py)`\s*\|\s*`([0-9a-f]{64})`"
    r"\s*\|\s*$")


def _rows() -> "list[tuple[str, str]]":
    """§7.2 登记表的全部匹配行（**列表**，不是 dict）。

    用 dict 收集会让同一件的重复登记行后者静默覆盖前者——表里钉两个不同
    sha 时，"登记"实际只剩最后一行，中间那行永远不被比对
    （GATE1-LANE-A/spec lane D7）。故保留全部匹配行，由调用方断言无重复。
    """
    text = PREREG.read_text(encoding="utf-8")
    # 锚点只认「首呼前登记的」，件数会随修订增加（三个 → 四个），不写死数量
    head = text.find("首呼前登记的")
    assert head != -1, "预注册件缺 §7.2 登记表"
    tail = text.find("一致性机判", head)
    assert tail != -1, "§7.2 登记表后缺一致性机判说明"
    rows = []
    for line in text[head:tail].splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def _registered() -> dict:
    return dict(_rows())


def test_registration_has_no_duplicate_tool_rows():
    """同一件在表里只能登记一次（否则前一行是永远不会被比对的死条目）。"""
    rows = _rows()
    paths = [p for p, _s in rows]
    assert len(paths) == len(set(paths)), f"§7.2 表里同一件重复登记：{paths}"


def test_registration_table_lists_five_tools():
    rows = _registered()
    assert set(rows) == {
        "scripts/phase25/task6_r4_verdict.py",
        "scripts/phase25/task6_r4_reference_sim.py",
        "scripts/phase25/gen_task6_r4_window_evidence.py",
        "scripts/phase25/gen_task6_r4_vector_identity.py",
        "scripts/kb/gen_prewindow_snapshots.py",
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


def test_registration_covers_a9_repair_round():
    """第二轮修复（A9）同样必须留档：向量轴与活库尾席都在这一轮加进来。"""
    text = PREREG.read_text(encoding="utf-8")
    assert "### 13.7 A9 门 1 第二轮" in text
    assert "vector_identity.observed ∧ unchanged" in text
    assert "gen_task6_r4_vector_identity.py" in text
    # §4 清单必须提到这两件，否则"引擎批范围"与登记表脱钩
    for rel in ("gen_prewindow_snapshots.py", "gen_task6_r4_vector_identity.py"):
        assert rel in text, rel
