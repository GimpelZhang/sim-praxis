"""Phase 2.8.1 P281-0 封存环代换机判（AC-P281-0-14、AC-P281-0-15）。

封存 `phase27` 后，任何 `open_ledger_append(phase27_ledger_path())` 或
`QuotaLedger.phase27().record(...)` 都会抛 `SealedLedgerError`。§16.3 的
五件历史测试必须在**同一提交**内改向到 `phase281`——改向＝断言强度不降，
判据从「phase27 可写」变成「phase28.1 可写」。

本文件与 Phase 2.7 的 `test_ac_t0_8_legacy_tests_flipped` 同构，但判定对象
不同：那里用**不可变父提交**证明「旧测试被改过」，这里用**本阶段的基线提交**
`82a1427` 证明同一件事，并要求：

* removed 行里出现旧的 `not in sealed`（真的把反向断言删了）；
* added 行里出现 `in sealed`（真的翻正了，不是只删不加）；
* added 行里不得出现 `skip` / `xfail`（不许用跳过换绿）。

基线钉死为 `BASELINE_COMMIT`（不可变 commit）。**严禁改回活 HEAD**——HEAD
在每个 Task 提交后都会前进，自指必红（同 Phase 2.5/2.6/2.7 的 PRE_T0_PARENT
纪律，见 memory:snapshot-test-baseline-no-live-head）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: P281-0 的不可变基线提交（2026-11-06 推送窗口后的 origin/main）。
BASELINE_COMMIT = "82a142710127367b0a771d818192321b53e2e951"

#: §16.3 的五件历史测试（只改断言文本/期望值，不删测试）
FLIPPED_FILES = (
    "tests/milestone25/test_task0_foundation.py",
    "tests/milestone25/test_task4_forensics_cache.py",
    "tests/milestone26/test_task0_foundation.py",
    "tests/milestone27/test_task0_foundation.py",
    "tests/milestone27/test_task3_elastic_loop.py",
)


def _diff_lines(path: str) -> "tuple[list[str], list[str]]":
    """``git diff --unified=0 BASELINE -- <path>`` 的 (added, removed) 行。"""
    out = subprocess.run(
        ["git", "diff", "--unified=0", BASELINE_COMMIT, "--", path],
        cwd=REPO_ROOT, text=True, capture_output=True, check=True).stdout
    added = [ln[1:] for ln in out.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:] for ln in out.splitlines()
               if ln.startswith("-") and not ln.startswith("---")]
    return added, removed


# ---------------------------------------------------------------------------
# AC-P281-0-14：封存环代换的文本机判
# ---------------------------------------------------------------------------

def test_flip_meta():
    # (1) 五件全部确有改动（否则 §16.3 没落地）。
    for rel in FLIPPED_FILES:
        added, removed = _diff_lines(rel)
        assert added or removed, f"§16.3 文件未落地任何改动：{rel}"

    # (2) milestone25：phase27 的反向断言被删（removed）＋翻正（added）。
    a25, r25 = _diff_lines("tests/milestone25/test_task0_foundation.py")
    assert any("phase27_ledger_path()" in ln and "not in sealed" in ln
               for ln in r25), "milestone25 未删旧的 phase27 反向断言"
    assert any("phase27_ledger_path()" in ln and " in sealed" in ln
               for ln in a25), "milestone25 未把 phase27 反向断言翻正"
    assert any("phase281_ledger_path()" in ln and "not in sealed" in ln
               for ln in a25), "milestone25 未新增 phase281 的可写断言"

    # (3) milestone27：同一处 4→5 的翻正。
    #     这里的形态与 milestone25 **不同**：milestone25 用的是单条 membership
    #     断言（`in sealed`），milestone27 用的是**精确集合字面量 `==` + 长度
    #     断言**（`assert sealed == frozenset({…})` 与 `len(sealed) == 5`）。
    #     后者比 membership 更强（判据是 `==` 而非 `⊆`），因此机判不按
    #     「added 行含 `in sealed`」认，而按**结构逐字**认这两条：
    #     ① phase27 账路径作为 frozenset 字面量的一项被加入（行尾逗号、
    #     `resolve(strict=False)` 形态不变）；② 长度从 4 改到 5。
    #     旧的反向断言仍必须出现在 removed 行里——否则就是「只加不删」。
    a27, r27 = _diff_lines("tests/milestone27/test_task0_foundation.py")
    assert any("phase27_ledger_path()" in ln and "not in sealed" in ln
               for ln in r27), "milestone27 未删旧的 phase27 反向断言"
    assert any("rp.phase27_ledger_path().resolve(strict=False)," in ln
               for ln in a27), \
        "milestone27 未把 phase27 加入封存集字面量（翻正缺失）"
    assert any("len(sealed) == 5" in ln for ln in a27), \
        "milestone27 未把封存集长度 4→5（翻正缺失）"

    # (4) milestone26：封存循环扩入 phase27（S10/S15 的文本链）。
    a26, _ = _diff_lines("tests/milestone26/test_task0_foundation.py")
    assert any("phase27_ledger_path()" in ln for ln in a26), \
        "milestone26 封存循环未扩 phase27"

    # (5) 不放水：任何改动行里不得出现 skip / xfail。
    for rel in FLIPPED_FILES:
        added, _ = _diff_lines(rel)
        for line in added:
            lowered = line.lower()
            assert "skip" not in lowered and "xfail" not in lowered, \
                f"封存环代换里出现 skip/xfail（{rel}）：{line.strip()}"


# ---------------------------------------------------------------------------
# AC-P281-0-15：三套历史测试改向后全绿（子进程 pytest，0 付费）
# ---------------------------------------------------------------------------

def test_legacy_suites_green():
    suites = ["tests/milestone25", "tests/milestone26", "tests/milestone27"]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *suites, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, text=True, capture_output=True, timeout=5400,
        env={**os.environ, "SIMPRAXIS_ALLOW_PAID": "0"})
    assert proc.returncode == 0, (
        f"§16.3 五件改向后三套历史测试仍红：rc={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout[-6000:]}\nSTDERR:\n{proc.stderr[-2000:]}")
