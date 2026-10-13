"""支持 python -m orchestrator.feishu_gateway 调 CLI。"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
