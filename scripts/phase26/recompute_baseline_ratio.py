#!/usr/bin/env python3
"""Phase 2.6 Task4 §T4.4-D：在 T5/T5b 冻结 trace 上复算 R_base（0 付费）。

计划要求单独成件（`$ROOT/walls/retest/baseline_effective_ratio.json`），
但**不得有第二套定义**：本脚本只是 ``run_wall_retest.cmd_recompute_baseline``
的薄壳，全部口径（productive 轮定义、分母、分墙汇总）单真值在
``scripts/phase26/run_wall_retest.py``（VERDICT_CRITERIA_V3.md §3 逐字）。

D26-8a：提纲的「36%」在 Phase 2.5 无计算式、不可复现；本件按预注册定义
复算，若不在 [0.34, 0.38] 带内，报告同时给「记载 36%」与「复算 R_base」，
以复算值为对照基准，**不得篡改成 36%**。

用法：
  python -m scripts.phase26.recompute_baseline_ratio
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.phase26.run_wall_retest import main as _retest_main


def main(argv: list[str] | None = None) -> int:
    return _retest_main(["--recompute-baseline", *(argv or [])])


if __name__ == "__main__":
    raise SystemExit(main())
