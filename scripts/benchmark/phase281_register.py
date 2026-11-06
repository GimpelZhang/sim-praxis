#!/usr/bin/env python3
"""Phase 2.8.1 预注册 CLI 的仓内入口（计划 §9.5）。

**唯一实现**在 ``orchestrator/benchmark/phase281/registration.py::main``：
计划 §9.5 逐条写的是 ``python -m orchestrator.benchmark.phase281.registration``，
本脚本只是同一入口的 ``scripts/`` 壳（仓内约定），不复制任何逻辑。

    python scripts/benchmark/phase281_register.py --derive-requirements
    python scripts/benchmark/phase281_register.py --write-order
    python scripts/benchmark/phase281_register.py --write-lock
    python scripts/benchmark/phase281_register.py --verify
    python scripts/benchmark/phase281_register.py --verify-consent --consent PATH
    python scripts/benchmark/phase281_register.py --write-consent --consent PATH \\
        --user-statement "…"      # 只在用户明确批准 LOCK 之后

**0 付费**：全程只读本地文件与账本 JSONL，不构造网关、不发网。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 允许 `python scripts/...` 裸跑
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281.registration import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
