#!/usr/bin/env python3
"""Phase 2.7 证据包构建（0 付费、只读源、`--clean` 重建）。

落 ``$SIMPRAXIS_PHASE27_ROOT/task7_milestone/evidence_pack/``。纪律沿
Phase 2.5 Task 10（D7-2：**从零重建**，不增量打补丁——增量会残留上一轮孤儿件）：

- 逐件 ``sha256`` + 字节数登记 ``manifest.json``；
- 单件 > 10 MiB → ``PackError``（FAULT，不出半包）；
- 符号链接一律 FATAL（``is_file()`` 会跟随链接把 ``~/access`` 打进包）；
- 目录 0700 / 文件 0600；
- 不入包：``cache/``（19M 引擎缓存）、``*.mp4``（D6-6 视频超大且非判据）、
  旧 ``evidence_pack/`` 自身、空的运行时目录；
- ``manifest.git_commit`` 是 AC-T7-6 三签的绑定锚。

用法：
  python scripts/phase27/build_evidence_pack_phase27.py            # 增量校验并重建
  python scripts/phase27/build_evidence_pack_phase27.py --clean    # 从零
  python scripts/phase27/build_evidence_pack_phase27.py --root P --pack DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common import secrets as secret_scan  # noqa: E402

SCHEMA = "simpraxis.evidence_pack.v1"
MILESTONE = "2.7"

MAX_FILE_BYTES = 10 * 1024 * 1024

# 不入包（相对 P27 根的顶层名）
SKIP_TOP_DIRS = {"cache", "watchdog", "runs", "agentic_traces", "zoo",
                 "evidence_pack"}
SKIP_SUFFIXES = (".mp4", ".avi", ".mov", ".mkv")
SKIP_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store", "node_modules"}
# 判决件在包外（避免自指 sha 悖论）：包内副本无法自证，且会让 manifest
# 依赖自身内容。Phase 2.7 口径：closure_judge.json 与 evidence_pack/ 同级。
SKIP_FILENAMES = {"closure_judge.json"}

# 证据根登记区（AC-T7 口径）：逻辑根 → 解析锚
EVIDENCE_ROOTS = {
    "@runs/phase27/<thread>/stage_<id>/": {
        "tag": "@runs/",
        "root_env": "SIMPRAXIS_RUN_DIR",
        "subdir": "phase27",
        "note": "Phase 2.7 战役运行件（含 Stage 帧/评分/trace）",
    },
    "@phase27/": {
        "tag": "@phase27/",
        "root_env": "SIMPRAXIS_PHASE27_ROOT",
        "subdir": "",
        "note": "Phase 2.7 根（任务产物、账本、批准件）",
    },
    "@repo/": {
        "tag": "@repo/",
        "root_env": None,
        "root": str(_REPO),
        "subdir": "",
        "note": "引擎仓（提交/测试/机器判据）",
    },
}


class PackError(RuntimeError):
    """缺必需证据 / 越界 / 超限；CLI exit 2，绝不产出「半包」充当完整。"""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO,
                          capture_output=True, text=True).stdout.strip()


def _git_status() -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=_REPO,
                          capture_output=True, text=True).stdout.strip()


def _walk(src_base: Path) -> "list[Path]":
    """安全 walk：不跟随链接、跳过缓存/视频、单件超限即 FAULT。"""
    files: "list[Path]" = []
    for dirpath, dirnames, filenames in os.walk(src_base, followlinks=False):
        dpath = Path(dirpath)
        kept = []
        for d in sorted(dirnames):
            p = dpath / d
            if d in SKIP_NAMES or d in {"evidence_pack"}:
                continue
            if p.is_symlink():
                raise PackError(f"目录符号链接不入包（实体不打包）：{p}")
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            p = dpath / name
            if name in SKIP_NAMES or name in SKIP_FILENAMES \
                    or name.endswith(SKIP_SUFFIXES):
                continue
            if p.is_symlink():
                raise PackError(f"文件符号链接不入包（会跟随出界）：{p}")
            st = p.stat()
            if not stat.S_ISREG(st.st_mode):
                raise PackError(f"非普通文件不入包：{p}")
            if st.st_size > MAX_FILE_BYTES:
                raise PackError(
                    f"单件 {st.st_size} B 超 10 MiB 上限：{p}")
            files.append(p)
    return files


def _copy_one(src: Path, dst: Path, rel: str,
              manifest: "list[dict]", logical: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    manifest.append({
        "path": rel,
        "origin": "copy",
        "origin_rel": str(src),
        "size": dst.stat().st_size,
        "sha256": _sha256(dst),
        "logical_ids": [logical],
        "required": True,
    })


def _write_derived(dst: Path, rel: str, obj, manifest: "list[dict]",
                   logical: str, origin: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(obj, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(dst, 0o600)
    manifest.append({
        "path": rel,
        "origin": "derived",
        "origin_rel": str(origin),
        "size": dst.stat().st_size,
        "sha256": _sha256(dst),
        "logical_ids": [logical],
        "required": True,
    })


def _scan_secrets(pack: Path) -> "list[str]":
    hits: "list[str]" = []
    for p in sorted(pack.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for name, matched in secret_scan.scan_text(text):
            hits.append(f"{p.relative_to(pack).as_posix()}::{name}::"
                        f"{str(matched)[:24]}")
    return hits


def _normalize_modes(pack: Path) -> dict:
    dirs = files = 0
    for dirpath, dirnames, filenames in os.walk(pack):
        d = Path(dirpath)
        os.chmod(d, 0o700)
        dirs += 1
        for name in filenames:
            p = d / name
            if p.is_symlink():
                continue
            os.chmod(p, 0o600)
            files += 1
    return {"dirs_0700": dirs, "files_0600": files}


def _check_clean_target(pack: Path, root: Path) -> None:
    """`--clean` 的删除守卫：目标必须是 Phase 2.7 根下的 evidence_pack 字面名。"""
    rp = pack.resolve()
    rr = root.resolve()
    if rp.name != "evidence_pack":
        raise PackError(f"--clean 只允许删名为 evidence_pack 的目录：{rp}")
    if rr not in rp.parents:
        raise PackError(f"--clean 目标越出 Phase 2.7 根：{rp} 不在 {rr} 之下")


def build(*, root: Path, pack: Path, clean: bool,
          docs: "list[Path] | None" = None) -> dict:
    if clean:
        _check_clean_target(pack, root)
        if pack.exists():
            shutil.rmtree(pack)
    pack.mkdir(parents=True, exist_ok=True)
    os.chmod(pack, 0o700)

    manifest: "list[dict]" = []

    # 1) Phase 2.7 根下各任务产物（顶层目录逐个展开，跳过缓存/视频/自指）
    for child in sorted(root.iterdir()):
        if child.name in SKIP_TOP_DIRS:
            continue
        if child.is_symlink():
            raise PackError(f"Phase 2.7 根下有符号链接顶层项：{child}")
        if child.is_file():
            if child.name.endswith(SKIP_SUFFIXES):
                continue
            if child.stat().st_size > MAX_FILE_BYTES:
                raise PackError(f"根文件超限：{child}")
            _copy_one(child, pack / child.name, child.name, manifest,
                      f"P27.root/{child.name}")
            continue
        if not child.is_dir():
            continue
        for f in _walk(child):
            rel = f.relative_to(root).as_posix()
            # 收尾件落在包根（与 evidence_pack/ 同级），便于机判按固定路径读
            if rel.startswith("task7_milestone/"):
                rel = rel[len("task7_milestone/"):]
            _copy_one(f, pack / rel, rel, manifest, f"P27.{child.name}")

    # 2) 文档（仓外）：计划与执行台账
    for doc in (docs or []):
        if not doc.is_file():
            raise PackError(f"缺文档：{doc}")
        _copy_one(doc, pack / "docs" / doc.name, f"docs/{doc.name}",
                  manifest, "docs")

    # 3) 派生件：提交身份 / 账本摘要 / 四表行数
    _write_derived(pack / "derived" / "git_head.json",
                   "derived/git_head.json",
                   {"commit": _git_head(), "dirty": bool(_git_status()),
                    "repo": str(_REPO),
                    "captured_at": datetime.now(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")},
                   manifest, "P27.T7.commit", _REPO / ".git/HEAD")

    # 4) manifest 自证（最后写，登记自身除外）
    n_files = len(manifest)
    total_bytes = sum(int(r["size"]) for r in manifest)
    manifest_doc = {
        "schema": SCHEMA,
        "milestone": MILESTONE,
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": _git_head(),
        "git_dirty": bool(_git_status()),
        "root": str(root),
        "pack": str(pack),
        "evidence_roots": EVIDENCE_ROOTS,
        "policy": {
            "max_file_bytes": MAX_FILE_BYTES,
            "no_symlinks": True,
            "excluded_top_dirs": sorted(SKIP_TOP_DIRS),
            "excluded_suffixes": list(SKIP_SUFFIXES),
            "modes": {"dirs": "0700", "files": "0600"},
        },
        "files": sorted(manifest, key=lambda r: r["path"]),
        "n_files": n_files,
        "total_bytes": total_bytes,
    }
    (pack / "manifest.json").write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=1,
                   sort_keys=True) + "\n", encoding="utf-8")

    # 5) 四查：超限 / 符号链接 / 密钥 / 权限
    big = [r["path"] for r in manifest_doc["files"]
           if int(r["size"]) > MAX_FILE_BYTES]
    if big:
        raise PackError(f"包内出现 >10 MiB 件：{big}")
    links = [p.relative_to(pack).as_posix()
             for p in pack.rglob("*") if p.is_symlink()]
    if links:
        raise PackError(f"包内出现符号链接：{links}")
    hits = _scan_secrets(pack)
    if hits:
        raise PackError(f"包内命中密钥正则 {len(hits)} 处：{hits[:3]}")
    modes = _normalize_modes(pack)
    manifest_doc["policy"]["modes"] = {"dirs": "0700", "files": "0600",
                                       **modes}
    (pack / "manifest.json").write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=1,
                   sort_keys=True) + "\n", encoding="utf-8")
    return manifest_doc


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import phase27_root

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--pack", type=Path, default=None)
    ap.add_argument("--clean", action="store_true",
                    help="先删后建（D7-2：不增量打补丁）")
    ap.add_argument("--docs", nargs="*", type=Path, default=None)
    args = ap.parse_args(argv)

    root = args.root or phase27_root()
    pack = args.pack or (root / "task7_milestone" / "evidence_pack")
    docs = args.docs
    if docs is None:
        docs = [Path.home() / "文档/Phase_2.7_Plan_detailed.md",
                Path.home() / "文档/Phase_2.7_Plan_Complete.md"]
        docs = [d for d in docs if d.is_file()]
    try:
        man = build(root=root, pack=pack, clean=args.clean, docs=docs)
    except PackError as exc:
        print(f"[PACK] {exc}", file=sys.stderr)
        return 2
    print(f"pack {pack} files={man['n_files']} "
          f"bytes={man['total_bytes']} commit={man['git_commit'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
