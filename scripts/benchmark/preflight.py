#!/usr/bin/env python
"""按路径运行的薄封装：``python scripts/benchmark/preflight.py [--run-tests]``。

``scripts/`` 不是 Python 包（A-08），主入口在 ``orchestrator/benchmark/cli.py``；
这里只做 ``sys.path`` 注入与转发，不承载任何逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    # 路线图 §13.1 的命令面：`preflight.py --validate-specs` 等价于
    # `python -m orchestrator.benchmark.cli validate-specs`（C-01 的薄封装）。
    if "--validate-specs" in argv:
        rest = [a for a in argv if a != "--validate-specs"]
        raise SystemExit(main(["validate-specs", *rest]))
    raise SystemExit(main(["preflight", *argv]))
