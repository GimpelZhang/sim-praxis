#!/usr/bin/env python3
"""Phase 2.7 Task 7 —— 四个交付件的单元回归（0 付费、只读、不联网）。

Task 7 评审的 MEDIUM 尾债：四个交付件（机判 / 证据包 / 看板 / AC 测试）此前
**零单测**，而 ``4248808`` 修的 ``size==0`` 误判正是「无回归用例」放过的那类
缺陷。本文件把已经踩过的坑钉成用例，逐个防回潮：

1. ``_tests_block``：pytest 摘要里 ``failed`` 可以**排在 passed 之前**
   （``9 failed, 2262 passed``），按位置解析会把 9 读成 passed；
2. ``check_pack``：``size == 0`` 的空文件是**合法登记**（`0` falsy 陷阱）；
3. ``check_pack``：manifest 条目 ``../`` / 绝对路径是**越界**（纯哈希校验
   会「对得上」，却把判据指向包外任意文件）；
4. ``check_pack``：``git_dirty`` 不是 ``False`` 的包不许绑定 HEAD；
5. ``_check_clean_target``：``--clean`` 只许删 ``task7_milestone/evidence_pack``，
   兄弟任务的证据包一律拒删；
6. ``tests_state``（看板）：摘要行**按名提取**、必须恰一条，``failed`` 不得
   恒为 0 的死判据。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_SCRIPTS = _REPO / "scripts" / "phase27"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_cold_run_json as cold_mod           # noqa: E402
import build_dashboard_phase27 as dash_mod       # noqa: E402
import build_evidence_pack_phase27 as pack_mod   # noqa: E402
import check_phase27_closure as judge            # noqa: E402


# ------------------------------------------------------------------ 工具
def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_pack(root: Path, entries: "list[tuple[str, bytes]]") -> Path:
    """造一个**完全合法**的证据包（调用方再逐项破坏以触发具体断言）。"""
    pack = root / "task7_milestone" / "evidence_pack"
    files = []
    for rel, blob in entries:
        p = pack / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        files.append({"path": rel, "origin": "copy", "origin_rel": str(p),
                      "size": p.stat().st_size, "sha256": _sha256(p),
                      "logical_ids": ["t"], "required": True})
    man = {
        "schema": judge.MANIFEST_SCHEMA,
        "milestone": "2.7",
        "git_commit": "0" * 40,
        "git_dirty": False,
        "evidence_roots": {"@runs/phase27/<thread>/stage_<id>/": {"tag": "@runs/"}},
        "files": files,
        "n_files": len(files),
        "total_bytes": sum(e["size"] for e in files),
    }
    (pack / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return pack


def _rewrite_manifest(pack: Path, mutate) -> None:
    p = pack / "manifest.json"
    man = json.loads(p.read_text(encoding="utf-8"))
    mutate(man)
    p.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ 1. _tests_block
def test_tests_block_failed_before_passed():
    """`9 failed, 2262 passed, …`：按名提取，不能把 failed 读成 passed。"""
    text = ("# cmd: /mnt/venvs/simpraxis/bin/python -m pytest tests -q\n"
            "9 failed, 2262 passed, 7 skipped, 59 deselected in 1.0s\n"
            "rc=1\n")
    blk = judge._tests_block(text)
    assert blk["passed"] == 2262
    assert blk["failed"] == 9
    assert blk["skipped"] == 7
    assert blk["deselected"] == 59
    assert blk["rc"] == 1


def test_tests_block_zero_counters_are_kept():
    """全零计数（`0 failed`）不得被 `or` 抹成缺省。"""
    text = "# cmd: pytest\n2198 passed, 0 failed in 1.0s\nrc=0\n"
    blk = judge._tests_block(text)
    assert blk["passed"] == 2198
    assert blk["failed"] == 0
    assert blk["skipped"] == 0
    assert blk["deselected"] == 0


def test_tests_block_rejects_unreviewable_log():
    """缺 `rc=` 或摘要行的日志不可审（不许当 0 处理）。"""
    with pytest.raises(ValueError):
        judge._tests_block("# cmd: pytest\n2198 passed in 1.0s\n")
    with pytest.raises(ValueError):
        judge._tests_block("# cmd: pytest\nrc=0\n")


# ------------------------------------------------------------------ 2. size==0 回归
def test_check_pack_accepts_empty_file_size_zero(tmp_path):
    """`4248808` 回归：空文件登记 size=0 是合法值，不是「登记不符」。"""
    pack = _make_pack(tmp_path, [("cold_run.raw.log", b""),
                                 ("note.txt", b"x")])
    summary, problems, _gaps = judge.check_pack(pack)
    assert problems == [], problems
    assert summary["n_files"] == 2


def test_check_pack_size_zero_regression_would_be_caught_by_old_form(tmp_path):
    """反证：空文件若被登记成非 0，仍必须报「登记大小不符」。"""
    pack = _make_pack(tmp_path, [("empty.log", b"")])
    _rewrite_manifest(pack, lambda m: m["files"][0].update(size=7))
    _summary, problems, _gaps = judge.check_pack(pack)
    assert any("登记大小不符" in p for p in problems), problems


# ------------------------------------------------------------------ 3. 路径越界
def test_check_pack_rejects_parent_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    pack = _make_pack(tmp_path, [("ok.txt", b"x")])
    _rewrite_manifest(pack, lambda m: m["files"].append({
        "path": "../outside.txt", "origin": "copy", "size": 6,
        "sha256": _sha256(outside), "logical_ids": ["t"], "required": True}))
    _summary, problems, _gaps = judge.check_pack(pack)
    assert any("路径越界" in p for p in problems), problems


def test_check_pack_rejects_absolute_path(tmp_path):
    outside = tmp_path / "abs.txt"
    outside.write_bytes(b"secret")
    pack = _make_pack(tmp_path, [("ok.txt", b"x")])
    _rewrite_manifest(pack, lambda m: m["files"].append({
        "path": str(outside), "origin": "copy", "size": 6,
        "sha256": _sha256(outside), "logical_ids": ["t"], "required": True}))
    _summary, problems, _gaps = judge.check_pack(pack)
    assert any("路径越界" in p for p in problems), problems


# ------------------------------------------------------------------ 4. git_dirty
def test_check_pack_rejects_dirty_tree(tmp_path):
    pack = _make_pack(tmp_path, [("ok.txt", b"x")])
    _rewrite_manifest(pack, lambda m: m.update(git_dirty=True))
    _summary, problems, _gaps = judge.check_pack(pack)
    assert any("git_dirty=True" in p for p in problems), problems


def test_check_pack_rejects_missing_git_dirty_key(tmp_path):
    """缺 `git_dirty` 键 ≠ 干净树：必须显式 False 才算。"""
    pack = _make_pack(tmp_path, [("ok.txt", b"x")])
    _rewrite_manifest(pack, lambda m: m.pop("git_dirty"))
    _summary, problems, _gaps = judge.check_pack(pack)
    assert any("git_dirty" in p for p in problems), problems


# ------------------------------------------------------------------ 5. --clean 守卫
def test_clean_target_accepts_own_pack(tmp_path):
    root = tmp_path / "phase27"
    target = root / "task7_milestone" / "evidence_pack"
    target.mkdir(parents=True)
    pack_mod._check_clean_target(target, root)      # 不抛即通过


def test_clean_target_rejects_sibling_task_pack(tmp_path):
    """兄弟任务的证据包一律拒删（只查「名为 evidence_pack」不够）。"""
    root = tmp_path / "phase27"
    target = root / "task5_roadmap" / "evidence_pack"
    target.mkdir(parents=True)
    with pytest.raises(pack_mod.PackError):
        pack_mod._check_clean_target(target, root)
    assert target.is_dir(), "守卫报错后目标目录必须原样保留"


def test_clean_target_rejects_pack_outside_root(tmp_path):
    root = tmp_path / "phase27"
    root.mkdir()
    target = tmp_path / "task7_milestone" / "evidence_pack"
    target.mkdir(parents=True)
    with pytest.raises(pack_mod.PackError):
        pack_mod._check_clean_target(target, root)


# ------------------------------------------------------------------ 6. 看板 tests_state
def _log(tmp_path: Path, body: str, name: str = "x.raw.log") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_dashboard_tests_state_bare_summary(tmp_path):
    """只有 `N passed` 的裸摘要也要读得出，缺省计数归 0。"""
    p = _log(tmp_path, "# cmd: pytest tests -q\n2198 passed in 1.0s\nrc=0\n")
    st = dash_mod.tests_state(p)
    assert st["passed"] == 2198
    assert st["failed"] == 0
    assert st["skipped"] == 0
    assert st["deselected"] == 0
    assert st["rc"] == 0


def test_dashboard_tests_state_failed_is_not_dead_judge(tmp_path):
    """`failed` 必须真读到——AC 的 `failed != 0` 判据不能因缺键恒假。"""
    p = _log(tmp_path, "# cmd: pytest tests -q\n"
                       "3 failed, 100 passed, 2 skipped, 50 deselected\nrc=1\n")
    st = dash_mod.tests_state(p)
    assert st["failed"] == 3
    assert st["rc"] == 1


def test_dashboard_tests_state_rejects_duplicate_summary(tmp_path):
    """两行摘要（疑似混入子进程输出）→ 拒绝，不许「取最后一条」。"""
    p = _log(tmp_path, "# cmd: pytest tests -q\n"
                       "9 failed, 100 passed\n2198 passed\nrc=1\n")
    with pytest.raises(dash_mod.DashboardError):
        dash_mod.tests_state(p)


def test_dashboard_tests_state_rejects_missing_cmd(tmp_path):
    p = _log(tmp_path, "2198 passed in 1.0s\nrc=0\n")
    with pytest.raises(dash_mod.DashboardError):
        dash_mod.tests_state(p)


# ------------------------------------------------------------------ 7. cold_run.json 生成器
def test_cold_run_json_binds_log_sha_and_counts(tmp_path):
    p = _log(tmp_path, "# cmd: /x/python -m pytest tests -q\n"
                       "2267 passed, 7 skipped, 66 deselected in 500s\nrc=0\n",
              "cold_run.raw.log")
    doc = cold_mod.build(log=p)
    assert doc["schema"] == cold_mod.SCHEMA
    assert doc["log_sha256"] == _sha256(p)
    assert doc["passed"] == 2267
    assert doc["failed"] == 0
    assert doc["rc"] == 0
    assert doc["red"] is False
    assert "日志即最终版" in doc["note"]
    assert len(doc["commit"]) == 40


def test_cold_run_json_refuses_red_log(tmp_path):
    """红日志不得充当证据——默认拒绝落盘，须显式 --allow-red。"""
    p = _log(tmp_path, "# cmd: pytest\n3 failed, 100 passed\nrc=1\n")
    with pytest.raises(cold_mod.ColdRunError):
        cold_mod.build(log=p)
    doc = cold_mod.build(log=p, allow_red=True)
    assert doc["red"] is True and doc["failed"] == 3


def test_cold_run_json_rejects_two_summary_lines(tmp_path):
    """两段拼接（如尾部又贴了子进程输出）→ 拒绝，不猜哪条是真的。"""
    p = _log(tmp_path, "# cmd: pytest\n2198 passed\n2267 passed\nrc=0\n")
    with pytest.raises(cold_mod.ColdRunError):
        cold_mod.build(log=p)
