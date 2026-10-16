#!/usr/bin/env python3
"""合成 fixture 方案（Task0b）：mujoco_contact_push 占位脚本。

本文件不是 Phase 1 真实解题脚本——正本在 KB 公开仓
solutions/track1_tools/02_mujoco_contact_push/mujoco_contact_push.py。
此处仅为引擎仓单测提供 resolver/stage 接缝所需的最小可执行形状
（--out-dir/--run-id 两参 + manifest.json 落盘），禁止用于任何真实 rollout。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="fixture mujoco contact push")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({"fixture": "mujoco_contact_push", "run_id": args.run_id}),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
