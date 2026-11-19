#!/usr/bin/env python3
"""RSI-S2 规则教训入库（S2-1 第 5 项）：闭源 ResearchMemory 三件 + 哈希链续写。

三条教训全部来自 S1 双审的三轮稳定复现（原件：
``/mnt/artifacts/simpraxis/rsi_s1/closeout/review/dual_review.json``），与
已有 ``s1-sham-degeneracy`` 互链（同一失败面、不同机判面）：

* ``s1-claim-vocabulary``：CI 跨零/带内 ≠ 证据否证 → 五值词表；
* ``s1-nc-validity``：退化阴性对照不验证任何东西 → 三连判据结构拒绝；
* ``s1-report-sections``：报告六段合同（缺段即结构不合格）。

**闭源层**：本文件只写 ``$S2_ROOT/closed/research_memory/``（0700/0600），
永不入公开仓、永不进 push 面（ProductBundle / check_push_surface 对账）。

用法::

    python scripts/s2/memory_lessons.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.runtime_paths import rsi_s2_root  # noqa: E402
from orchestrator.rsi_bridge.memory_store import ResearchMemory  # noqa: E402

S1_DUAL_REVIEW = "/mnt/artifacts/simpraxis/rsi_s1/closeout/review/dual_review.json"

LESSONS = (
    {
        "lesson_id": "s1-claim-vocabulary",
        "lesson": ("主对比 95% CI 跨零或整段落进 ±δ 等效带时，不得判 REFUTED："
                   "「未检出」与「否证」是两种证据。v2 五值词表把方向主张细分为 "
                   "SUPPORTED/REFUTED/NOT_DETECTED/EQUIVALENT/INCONCLUSIVE，"
                   "且 claim.verdict 必须等于 host 主对比分类（G-CLAIM 逐字段）。"),
        "evidence": [S1_DUAL_REVIEW,
                     "orchestrator/hk/verdicts.py",
                     "tests/milestoneS2/test_verdict_v2.py"],
        "counterexample": ("S1 A_N 主对比 estimate −3.46e-7、95% CI [−3.72e-6, 3.09e-6] "
                           "被旧三值判 REFUTED；两审均以「跨零 CI 判 REFUTED 与主张强度"
                           "不匹配」拒绝。"),
    },
    {
        "lesson_id": "s1-nc-validity",
        "lesson": ("阴性对照必须同时满足三连判据才有证据价值：非退化（逐 block 效应"
                   "非全零且区间宽 >0）、亚阈（|estimate| < δ）、灵敏度校准（同管线注入"
                   "已知效应可检出）。任一不满足即结构拒绝，不进确认批；每研究强制"
                   "注册 NC（B 机制无 NC 同样是缺陷）。"),
        "evidence": [S1_DUAL_REVIEW,
                     "orchestrator/hk/nc_registration.py",
                     "tests/milestoneS2/test_nc_validity.py"],
        "counterexample": ("S1 A_N 的 sham（torsional 0.01→0.02）逐 block 效应恒 0、"
                           "90% 区间 [0,0]；B_N 无注册阴性对照——两审各自独立拒绝。"),
    },
    {
        "lesson_id": "s1-report-sections",
        "lesson": ("研究报告是**六段合同**：问题与可反驳假设 / 方法 / 主结果 / 异质性与"
                   "范围 / 局限与下一步 / 阴性对照处理。缺段不是文风问题而是结构性"
                   "不合格（对应六维量表逐段计 0）；v1 变异面的 G-REPORT 守卫在 K8 前"
                   "自检并给一次修复触点。"),
        "evidence": [S1_DUAL_REVIEW,
                     "orchestrator/hk/report_contract.py",
                     "tests/milestoneS2/test_report_contract.py"],
        "counterexample": ("S1 B_N 审查原文：未报告局限/下一步、无阴性对照处理，"
                           "无法排除非特异效应 → reject。"),
    },
)


def run(*, dry_run: bool = False) -> "dict[str, object]":
    root = rsi_s2_root() / "closed" / "research_memory"
    if dry_run:
        return {"dry_run": True, "root": str(root),
                "lessons": [row["lesson_id"] for row in LESSONS]}
    memory = ResearchMemory(root, journal="s2_lessons")
    appended = []
    for row in LESSONS:
        if any(entry.get("lesson_id") == row["lesson_id"]
               for entry in memory.entries):
            continue
        entry = memory.append(**row)
        appended.append({"lesson_id": entry["lesson_id"],
                         "sha256": entry["sha256"]})
    return {"root": str(memory.path), "appended": appended,
            "chain_length": len(memory.entries),
            "tail_sha256": memory.entries[-1]["sha256"] if memory.entries else ""}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S2 规则教训入库（闭源）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = run(dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
