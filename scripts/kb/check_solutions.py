#!/usr/bin/env python3
"""Task0b 内容层分层校验：4 方案契约/sha/可解析 + data/ 不跟踪 + knowledge 哈希集。

用法：
  python scripts/kb/check_solutions.py [--kb-root $KB_ROOT]

校验项（任一失败 exit 1）：
  1. knowledge/ 32-md sha256 集 == aac53313 基线（只增不改）且当前 HEAD 是
     Task0b 迁移提交（c5712e9f，solutions/ 首次写入）的后代（memory_flywheel.kb_pins）；
  2. solutions/ 恰好 4 个迁移方案，每个 manifest 经解析器全量校验（schema/
     solution_id/C4/逐件 sha256），方案内全部 *.py 通过 py_compile；
  3. data/ 永不入库：git ls-files data 为空且 git check-ignore 命中 data/lancedb。

只读检查，不触网、0 付费。
"""
from __future__ import annotations

import argparse
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_flywheel import kb_pins  # noqa: E402
# CACHE_DIR_NAMES 单真值在 resolver（L1 定义处），L4 树卫生复用。
from orchestrator.solutions import (  # noqa: E402
    CACHE_DIR_NAMES,
    SolutionError,
    load_manifest,
)

# Task0b 从 Phase1 迁入的存量方案全集（6 脚本文件 / 4 方案目录）。
# 单真值在 kb_pins.EXPECTED_SOLUTION_IDS（ingest strict 模式共用）；
# 保留本 list 别名以最小化本文件改动面。
EXPECTED_SOLUTIONS = list(kb_pins.EXPECTED_SOLUTION_IDS)


def _git(kb_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(kb_root), *args],
                          capture_output=True, text=True)


def check_solutions(kb_root: Path) -> list[str]:
    errors: list[str] = []
    sroot = kb_root / "solutions"
    if not sroot.is_dir():
        return [f"内容层不存在：{sroot}"]

    found = sorted(
        f"{cat.name}/{sol.name}"
        for cat in sorted(sroot.iterdir()) if cat.is_dir()
        for sol in sorted(cat.iterdir())
        if sol.is_dir() and (sol / "manifest.yaml").is_file())
    if found != EXPECTED_SOLUTIONS:
        errors.append(
            f"solutions/ 方案集不符：期望 {EXPECTED_SOLUTIONS}，实际 {found}")

    for sid in EXPECTED_SOLUTIONS:
        sdir = sroot / sid
        if not sdir.is_dir():
            continue
        try:
            manifest = load_manifest(sdir)
        except SolutionError as exc:
            errors.append(f"[{sid}] manifest 校验失败：{exc}")
            continue
        # 逐件存在性已由解析器 sha 复算覆盖；这里额外保证入口位在 files 内
        if manifest.entrypoint not in manifest.file_map:
            errors.append(f"[{sid}] entrypoint {manifest.entrypoint} 不在 files[]")
        # 可解析：方案内全部 .py（含 tests/）py_compile
        pyfiles = sorted(sdir.rglob("*.py"))
        if not pyfiles:
            errors.append(f"[{sid}] 方案内没有任何 .py 文件")
        with tempfile.TemporaryDirectory(prefix="task0b_pyc_") as cache:
            for pyf in pyfiles:
                cfile = Path(cache) / (pyf.relative_to(sdir).as_posix().replace("/", "_") + "c")
                try:
                    py_compile.compile(str(pyf), cfile=str(cfile), doraise=True)
                except py_compile.PyCompileError as exc:
                    errors.append(f"[{sid}] 不可解析 {pyf.relative_to(sdir)}：{exc.msg.splitlines()[0]}")
        print(f"[solutions OK] {sid}：{len(manifest.files)} 文件，"
              f"入口 {manifest.entrypoint}，{len(pyfiles)} 个 .py 可解析")
    return errors


def check_manifest_gaps(kb_root: Path) -> list[str]:
    """L4(a)：solutions/track[12]_*/* 中存在但缺 manifest.yaml 的游离目录。

    check_solutions 的方案集统计只数含 manifest 的目录，游离目录此前被静默
    忽略；公开内容层树卫生要求显式报错。
    """
    errors: list[str] = []
    sroot = kb_root / "solutions"
    if not sroot.is_dir():
        return errors
    for cat in sorted(sroot.glob("track[12]_*")):
        if not cat.is_dir():
            continue
        for sol in sorted(cat.iterdir()):
            if sol.is_dir() and not (sol / "manifest.yaml").is_file():
                errors.append(
                    f"方案目录缺 manifest.yaml（游离目录此前被静默忽略）："
                    f"{sol.relative_to(kb_root)}")
    return errors


def check_cache_dirs(kb_root: Path) -> list[str]:
    """L4(b)：solutions/ 下出现任何缓存目录（__pycache__ 等）即 error。"""
    errors: list[str] = []
    sroot = kb_root / "solutions"
    if not sroot.is_dir():
        return errors
    for p in sorted(sroot.rglob("*")):
        if p.name in CACHE_DIR_NAMES and p.is_dir():
            errors.append(
                f"solutions/ 下出现缓存目录（公开内容层树卫生，须清除）："
                f"{p.relative_to(kb_root)}")
    return errors


def check_data_ignored(kb_root: Path) -> list[str]:
    errors: list[str] = []
    tracked = _git(kb_root, "ls-files", "data").stdout.splitlines()
    if tracked:
        errors.append(f"data/ 有被跟踪路径（永不入库）：{tracked}")
    # Task0b 彩排实证：软链存在时 check-ignore data/lancedb 会被 git 以
    # 「路径规格位于符号链接中」拒绝（fatal），故闸口打在 data 路径本身；
    # .gitignore 必须用锚定 /data（尾斜杠 data/ 不匹配软链，会留 ?? data）。
    ci = _git(kb_root, "check-ignore", "data")
    if ci.returncode != 0:
        errors.append("git check-ignore data 未命中（.gitignore 须含锚定 /data 规则）")
    # data 可以是软链（迁移后）或不存在；若是实体目录且内含库则提示应是软链
    data = kb_root / "data"
    if data.exists() and not data.is_symlink() and (data / "lancedb").exists():
        errors.append(f"{data} 是实体目录而非软链（应 ln -s /mnt/artifacts/embodied-kb/data）")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kb-root",
                    default=os.environ.get("KB_ROOT", kb_pins.DEFAULT_KB_ROOT))
    args = ap.parse_args()
    kb_root = Path(args.kb_root)

    errors: list[str] = []
    if not kb_root.is_dir():
        print(f"[check FAIL] KB 根不存在：{kb_root}")
        return 1
    pin_errors = kb_pins.check_layered_pins(kb_root)
    for e in pin_errors:
        print(f"[kb_pins FAIL] {e}")
    errors.extend(pin_errors)
    if not pin_errors:
        print(f"[kb_pins OK] HEAD={kb_pins.head(kb_root)[:12]}，"
              f"knowledge {len(kb_pins.KNOWLEDGE_BASELINE_FILES)} md 哈希集基线一致")

    errors.extend(check_data_ignored(kb_root))
    errors.extend(check_manifest_gaps(kb_root))
    errors.extend(check_cache_dirs(kb_root))
    errors.extend(check_solutions(kb_root))

    if errors:
        print(f"[check FAIL] {len(errors)} 项不合规：")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("[check_solutions OK] 分层校验全部通过"
          f"（{len(EXPECTED_SOLUTIONS)} 方案 + data/ 未跟踪 + knowledge 基线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
