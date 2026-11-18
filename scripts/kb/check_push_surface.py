#!/usr/bin/env python3
"""S0-8 防泄机判：闭源目录 0 件入 push 面（bundle manifest 与 staging diff 对账）。

用法：
  python scripts/kb/check_push_surface.py --staging <待推送目录> --bundle <product 包>

判据（任一命中即 rc=1，硬错误 rc=2，干净 rc=0）：
  1. staging 中任一文件路径含闭源 tier 名（research_memory/archives/promoted_params）；
  2. staging 中任一文件字节 sha256 与 product 包闭源 tier 清单撞库；
  3. staging 为 git 仓时，已跟踪文件集合与 manifest 闭源清单求交（防"已提交"泄漏）。
只读、0 付费、不触网。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

PRODUCT_CLOSED_TIERS = ("research_memory", "archives", "promoted_params")


class PushSurfaceError(RuntimeError):
    """用法/缺件硬错（rc=2）。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def closed_index(bundle: Path) -> dict[str, str]:
    manifest_path = bundle / "bundle_manifest.json"
    if not manifest_path.is_file():
        raise PushSurfaceError(f"缺 bundle_manifest.json：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    closed = manifest.get("closed_tiers")
    if not isinstance(closed, dict):
        raise PushSurfaceError(f"{bundle} 不是 product 包（缺 closed_tiers）")
    index: dict[str, str] = {}
    for tier, meta in closed.items():
        files = meta.get("files", []) if isinstance(meta, dict) else None
        if not isinstance(files, list):
            raise PushSurfaceError(f"closed_tiers[{tier}].files 必须是列表")
        for entry in files:
            if not isinstance(entry, dict) or "relpath" not in entry or "sha256" not in entry:
                raise PushSurfaceError(f"closed_tiers[{tier}] 文件项缺 relpath/sha256")
            index[f"closed/{tier}/{entry['relpath']}"] = entry["sha256"]
    return index


def scan(staging: Path, index: dict[str, str]) -> list[dict]:
    if not staging.is_dir():
        raise PushSurfaceError(f"staging 目录不存在：{staging}")
    shas = set(index.values())
    leaked: list[dict] = []
    for path in sorted(staging.rglob("*")):
        if ".git" in path.parts or path.is_dir():
            continue
        rel = path.relative_to(staging).as_posix()
        reasons = []
        if any(part in PRODUCT_CLOSED_TIERS for part in Path(rel).parts):
            reasons.append("closed-tier-path")
        if path.is_file() and _sha256(path) in shas:
            reasons.append("closed-bytes-sha256")
        if reasons:
            leaked.append({"path": rel, "reasons": reasons})
    # git 已跟踪集：路径级再核一次（防大小写/软链绕行）
    if (staging / ".git").exists():
        tracked = subprocess.run(["git", "-C", str(staging), "ls-files"],
                                 capture_output=True, text=True)
        if tracked.returncode == 0:
            for rel in tracked.stdout.splitlines():
                if any(part in PRODUCT_CLOSED_TIERS for part in Path(rel).parts):
                    if not any(item["path"] == rel for item in leaked):
                        leaked.append({"path": rel, "reasons": ["git-tracked-closed-path"]})
    return leaked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    try:
        index = closed_index(args.bundle)
        leaked = scan(args.staging, index)
    except PushSurfaceError as exc:
        print(f"[PUSH-SCAN HARD ERROR] {exc}", file=sys.stderr)
        return 2
    report = {"closed_files": len(index), "staging": str(args.staging),
              "leaked": leaked, "clean": not leaked}
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    if leaked:
        print(f"[PUSH-SCAN FAIL] push 面出现 {len(leaked)} 件闭源文件：")
        for item in leaked:
            print(f"  - {item['path']} ({','.join(item['reasons'])})")
        return 1
    print(f"[PUSH-SCAN OK] push 面 0 件闭源（对照 {len(index)} 件闭源清单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
