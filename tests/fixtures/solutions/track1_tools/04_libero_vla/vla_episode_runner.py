"""Fixture：LIBERO VLA 多 Stage（合成脚本，非真实解题内容）。

内容层真件在 KB `solutions/track1_tools/04_libero_vla/`（Phase 2.7 Task 6 回收，
KB `b5de4bd`）。本文件只为引擎侧解析器/树卫生测试提供契约形状，不做任何仿真。
"""
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser(description="synthetic LIBERO VLA fixture")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--stage", default="s1_baseline")
    args = ap.parse_args()
    print(f"fixture only: stage={args.stage} run_id={args.run_id} out={args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
