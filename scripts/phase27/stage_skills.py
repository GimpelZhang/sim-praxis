#!/usr/bin/env python3
"""把 KB 内容层的 ``skills/`` 暂存为只读副本（Phase 2.7 Task 2 / D2-5）。

为什么要有 staged 副本，而不是直接挂 ``$KB_ROOT/skills``：KB 是活仓（可能被
切分支、改文件），直挂会让「本轮用的技能」不可复现。本脚本把清单登记的
每一件复制到 ``$SIMPRAXIS_PHASE27_ROOT/skills_stage/<manifest_sha12>/``，
逐件复算 sha256，目录名就是清单内容的指纹——事后可逐字节复现这一轮的技能面。

目录形态（容器内以 ``/skills:ro`` 挂载这个 ``<manifest_sha12>`` 目录）：

    skills_stage/<manifest_sha12>/
        simpraxis_skills/__init__.py
        simpraxis_skills/<domain>/__init__.py
        simpraxis_skills/<domain>/<module>.py

即内容层 ``skills/`` 被**重根**成包名 ``simpraxis_skills``（Plan §D4-4 的
``from simpraxis_skills.wall_b import ...`` 依赖同一名字）；文件内容逐字节
不变，故 sha256 与清单相等。注意不能把 ``skills/`` 原样挂到 ``/skills``：
那样容器里 ``mujoco`` / ``libero`` 会变成 ``sys.path[0]`` 上的同名顶层包，
把真实 pip 包遮蔽掉。

纪律：0 联网 / 0 付费；不推送；目录 0700、文件 0600、无符号链接；已存在的
目标目录只复算不覆盖（重入幂等），任何 sha 不符即 ``SkillsError``。

用法：

    /mnt/venvs/simpraxis/bin/python -m scripts.phase27.stage_skills --stage \\
        --evidence-dir "$SIMPRAXIS_PHASE27_ROOT/task2_skills"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.common.fsutil import atomic_write_bytes, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import phase27_root  # noqa: E402
from orchestrator.skills.registry import (  # noqa: E402
    MANIFEST_NAME,
    PACKAGE_NAME,
    SkillsError,
    load_skills_manifest,
    manifest_sha12,
    skills_root,
    staged_path_for,
)

SKILLS_STAGE_DIRNAME = "skills_stage"
DIR_MODE = 0o700
FILE_MODE = 0o600


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk_nosymlink(root: Path) -> list:
    """遍历 staged 树：任何符号链接都是错误（fail-closed）。"""
    out = []
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise SkillsError(f"staged 树含符号链接：{entry}")
        out.append(entry)
    return out


def stage(evidence_dir: "Path | None" = None) -> dict:
    """执行暂存并返回报告（同时写 stage_report.json）。"""
    root = skills_root()
    manifest = load_skills_manifest(root)
    sha12 = manifest_sha12(root)
    stage_root = phase27_root() / SKILLS_STAGE_DIRNAME / sha12
    pkg_root = ensure_private_dir(stage_root, mode=DIR_MODE)

    planned: list[tuple[str, Path, str]] = []
    for item in manifest["skills"]:
        planned.append((item["relpath"], staged_path_for(item["relpath"], stage_root),
                        item["sha256"]))
    for item in manifest["packages"]:
        planned.append((item["relpath"], staged_path_for(item["relpath"], stage_root),
                        item["sha256"]))

    kb_root = root.parent
    copied: list[dict] = []
    for relpath, dst, want in planned:
        src = kb_root / relpath
        if not src.is_file():
            raise SkillsError(f"技能源文件不存在：{src}")
        if not dst.exists():
            ensure_private_dir(dst.parent, mode=DIR_MODE)
            atomic_write_bytes(dst, src.read_bytes(), mode=FILE_MODE)
        else:
            dst.chmod(FILE_MODE)
        actual = _sha256_file(dst)
        if actual != want:
            raise SkillsError(
                f"staged 副本 sha256 不符：{dst} manifest={want[:12]}… "
                f"实际={actual[:12]}…（先删 {stage_root} 再重跑）")
        copied.append({"relpath": relpath,
                       "staged": str(dst.relative_to(stage_root)),
                       "sha256": actual,
                       "mode": oct(dst.stat().st_mode & 0o777)})

    # 目录/文件权限与符号链接复核（AC-T2-3）
    for path in [stage_root] + _walk_nosymlink(stage_root):
        if path.is_dir():
            path.chmod(DIR_MODE)
        elif path.is_file():
            path.chmod(FILE_MODE)
    for path in _walk_nosymlink(stage_root):
        mode = path.stat().st_mode & 0o777
        if path.is_dir():
            if mode != DIR_MODE:
                raise SkillsError(f"staged 目录权限不是 {oct(DIR_MODE)}：{path}")
        elif mode != FILE_MODE:
            raise SkillsError(f"staged 文件权限不是 {oct(FILE_MODE)}：{path}")

    report = {
        "schema": "simpraxis.skills_stage_report.v1",
        "manifest_sha12": sha12,
        "manifest_sha256": hashlib.sha256(
            (root / MANIFEST_NAME).read_bytes()).hexdigest(),
        "skills_root": str(root),
        "stage_root": str(stage_root),
        "package_name": PACKAGE_NAME,
        "files": copied,
    }
    if evidence_dir is not None:
        ev = ensure_private_dir(Path(evidence_dir), mode=DIR_MODE)
        atomic_write_bytes(ev / "stage_report.json",
                           (json.dumps(report, ensure_ascii=False, indent=2,
                                       sort_keys=False) + "\n").encode("utf-8"),
                           mode=FILE_MODE)
    return report


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", action="store_true",
                    help="执行暂存（唯一动作；不传即打印清单摘要）")
    ap.add_argument("--evidence-dir", default=None,
                    help="stage_report.json 落盘目录（缺省不写）")
    args = ap.parse_args(argv)

    if not args.stage:
        root = skills_root()
        manifest = load_skills_manifest(root)
        print(f"skills_root={root}")
        print(f"manifest_sha12={manifest_sha12(root)}")
        for item in manifest["skills"]:
            print(f"  {item['name']:<22} {item['sha256'][:12]}… "
                  f"entry={list(item['entry'])}")
        return 0

    report = stage(Path(args.evidence_dir) if args.evidence_dir else None)
    print(json.dumps({"stage_root": report["stage_root"],
                      "manifest_sha12": report["manifest_sha12"],
                      "files": len(report["files"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":                                              # pragma: no cover
    raise SystemExit(main())
