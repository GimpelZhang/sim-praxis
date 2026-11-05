#!/usr/bin/env python
"""按路径运行的薄封装：``python scripts/benchmark/capture_runtime.py --task X``。

``scripts/`` 不是 Python 包（TD-2），主入口在 ``orchestrator/benchmark/cli.py``；
这里只做 ``sys.path`` 注入与转发，不承载任何逻辑（Plan T2.4）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["capture-runtime", *sys.argv[1:]]))
