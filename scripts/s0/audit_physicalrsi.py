#!/usr/bin/env python3
"""S0-1 依赖准入质量审计 CLI（fail-closed；实现单一真值在
`orchestrator/rsi_bridge/audit.py`）。

用法（外环引擎解释器）：
    /mnt/venvs/physicalrsi/bin/python scripts/s0/audit_physicalrsi.py \
        --repo /mnt/assets/simpraxis/PhysicalRSI/PhysicalRSI \
        --report runs/inventory/s0_baseline/physicalrsi_audit.json

五项检查：① 版本 0.3.0 + 18 关键类可解析；② Contract/skill 冒烟；③ 克隆仓自带
pytest（唯一豁免簇 `tests/test_infrastructure_faults.py`——5ms 预算 vs 30ms sleep
的宿主时序敏感断言，须证明抖动而非确定性失败）；④ compileall；⑤ Apache-2.0。

锁内适配：计划原文 `uv pip install 'physicalrsi==0.3.0'` 假设 PyPI 有包，2026-11-18
实测 404；改为自已核对本地克隆安装同版本源码，pin 不松（pyproject version 由本审计取证）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.rsi_bridge.audit import EXPECTED_VERSION, run_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path, help="PhysicalRSI 克隆根（只读）")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--skip-upstream-tests", action="store_true",
                        help="仅供负控构造（报告标记 skipped_for_negative_control）")
    args = parser.parse_args()

    ok, checks = run_audit(args.repo, skip_upstream_tests=args.skip_upstream_tests)
    report = {
        "schema": "simpraxis.s0.physicalrsi_audit.v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "repo": str(args.repo),
        "expected_version": EXPECTED_VERSION,
        "checks": checks,
        "pass": bool(ok),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"pass": report["pass"],
                      "failed_checks": [c["id"] for c in checks if not c["pass"]]},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
