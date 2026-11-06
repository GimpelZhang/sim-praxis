#!/usr/bin/env python
"""Phase 2.8.1 仓外信任锚 CLI：``--write`` / ``--verify``。

用法::

    $PY scripts/benchmark/phase281_anchor.py --root $ROOT --write
    $PY scripts/benchmark/phase281_anchor.py --root $ROOT --verify    # rc=0

``--write`` 把三任务的判据/运行时快照与四份源码摘要写进
``<root>/private/``（0700 目录、0600 文件），并写入自锚 ``manifest_sha256``。
``--verify`` 重算全部摘要，逐条打印问题并以 rc=1 结束（**STOP-05：无有效锚禁付费**）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # 引擎仓根：由本文件位置推导的**固定**路径，从不来自 trial 目录（AC-P281-1-14）
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281 import anchor as anchor_mod  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_anchor", description="Phase 2.8.1 仓外信任锚（写/验）")
    ap.add_argument("--root", required=True, help="phase281 数据根")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="写锚（幂等）")
    mode.add_argument("--verify", action="store_true", help="重算并校验锚")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    return ap


def _emit_json(payload: dict) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root)
    target = anchor_mod.anchor_path(root)

    if args.write:
        try:
            manifest = anchor_mod.write_anchor(root, written_by="phase281_anchor_cli")
        except anchor_mod.AnchorError as exc:
            print(f"[phase281-anchor] 写锚失败：{exc}", file=sys.stderr)
            return 1
        print(f"[phase281-anchor] 已写 {target} "
              f"manifest_sha256={manifest.manifest_sha256}")
        if args.json:
            _emit_json(manifest.to_json())
        return 0

    problems = anchor_mod.verify_anchor(target)
    if args.json:
        _emit_json({"anchor": str(target), "problems": problems,
                    "ok": not problems})
    if problems:
        print(f"[phase281-anchor] 校验失败（{len(problems)} 条）：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"[phase281-anchor] 校验通过：{target}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
