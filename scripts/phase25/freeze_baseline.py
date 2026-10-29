#!/usr/bin/env python3
"""Phase 2.5 Task0 AC-T0-6：两排除全套测试基线冻结器（0 付费）。

输入：
  1) Task0 改动【之前】实跑的 pytest 原始日志（-q 尾行汇总，权威计数）；
  2) 当前工作树的 --collect-only 集合（拿 deselected 名单）；
  3) 改动后带 ``-v -rs`` 的实跑日志（拿唯一 skipped 测试名与原因；名单与改动
     前同构——新增 milestone25 测试不引入 skip/deselect，回归测试逐名锁定）。

输出：$SIMPRAXIS_PHASE25_ROOT/estimates/t0_test_baseline.json（原子 0600），
Task 4 起任何 PR/分支的全套结果必须能与本件对账：passed 不得低于基线、
skipped/deselected 名单非经评审不得变化。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402 L6：0600 单一源

BASELINE_SCHEMA = "simpraxis.phase25.t0_test_baseline.v1"
# 两排除（与 CLAUDE.md 收尾纪律一致）：test_04（paid 真触网）与 paid/kb_integration
# 标记（裸跑自动 skip，收集口径里直接 deselect，名单逐名冻结）。
K_EXPR = "not test_04"
MARK_EXPR = "not paid and not kb_integration"
CANONICAL_CMD = (
    '/mnt/venvs/simpraxis/bin/python -m pytest tests -q '
    '-k "not test_04" -m "not paid and not kb_integration"')

# 各计数独立搜索：单一可选分组链在 "1202 passed, 1 skipped, 42 deselected"
# 这种缺 failed 前缀的真实尾行上会让可选组静默落空。
_COUNT_RE = {
    "failed": r"(\d+) failed",
    "passed": r"(\d+) passed",
    "skipped": r"(\d+) skipped",
    "deselected": r"(\d+) deselected",
}
_VERBOSE_RE = re.compile(
    r"^(?P<node>\S+::\S+)\s+(?P<outcome>PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b")
_SKIP_REASON_RE = re.compile(
    r"^SKIPPED \[\d+\]\s+(?P<loc>\S+):(?P<line>\d+):\s*(?P<reason>.*)$")


def parse_summary_tail(raw_log: Path) -> dict:
    text = raw_log.read_text(encoding="utf-8", errors="replace")
    summary_line = None
    for line in text.splitlines():
        if " passed" in line or " failed" in line:
            summary_line = line.strip()
    if summary_line is None:
        raise ValueError(f"日志中找不到 pytest 汇总行：{raw_log}")
    counts = {k: (int(m.group(1)) if (m := re.search(pat, summary_line)) else 0)
              for k, pat in _COUNT_RE.items()}
    m_exit = re.search(r"^EXIT=(?P<rc>\d+)\s*$", text, flags=re.MULTILINE)
    m_dur = re.search(r"in ([\d.]+)s", summary_line)
    m_warn = re.search(r"(\d+) warnings?", summary_line)
    return {"summary_line": summary_line, **counts,
            "exit_code": int(m_exit.group("rc")) if m_exit else None,
            "duration_s": float(m_dur.group(1)) if m_dur else None,
            "warnings": int(m_warn.group(1)) if m_warn else 0}


def parse_verbose_log(raw_log: Path) -> dict:
    nodes: dict[str, list[str]] = {
        "passed": [], "failed": [], "skipped": [], "xfail": [], "xpass": [],
        "error": []}
    skip_reasons: list[dict] = []
    in_skip_section = False
    for line in raw_log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _VERBOSE_RE.match(line)
        if m:
            key = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped",
                   "XFAIL": "xfail", "XPASS": "xpass", "ERROR": "error"}[
                m.group("outcome")]
            nodes[key].append(m.group("node"))
        if line.startswith("SKIPPED"):
            in_skip_section = True
        if in_skip_section:
            ms = _SKIP_REASON_RE.match(line.strip())
            if ms:
                skip_reasons.append({"location": f"{ms.group('loc')}:"
                                                f"{ms.group('line')}",
                                     "reason": ms.group("reason")})
            elif line.startswith("="):
                in_skip_section = False
    return {k: sorted(v) for k, v in nodes.items()} | {
        "skip_reasons": skip_reasons}


def collect_node_ids(python: str, *, use_filters: bool) -> list[str]:
    # 总集必须【不带】-k：-k "not test_04" 剔除的 13 个 test_04 项同样计入
    # 真实运行的 42 deselected（pytest 把 -k 与 -m 剔除项合并计数）；总集与
    # 全集（-k+-m）之差才是完整 deselected 名单（29 标记 + 13 test_04 = 42）。
    cmd = [python, "-m", "pytest", "tests", "--collect-only", "-q"]
    if use_filters:
        cmd += ["-k", K_EXPR, "-m", MARK_EXPR]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True,
                          capture_output=True, check=True)
    ids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        # -q 收集输出每行一个 node id（tests/...py::test_x[param]）；忽略汇总行。
        if line.startswith("tests/") and "::" in line:
            ids.append(line.split()[0])
    return sorted(set(ids))


def build_baseline(*, prechange_log: Path, verbose_log: Path | None,
                   python: str, raw_collect: bool = True) -> dict:
    tail = parse_summary_tail(prechange_log)
    if tail["failed"] != 0 or tail["exit_code"] != 0:
        raise ValueError(f"基线日志非全绿，禁止冻结：{tail}")

    verbose = (parse_verbose_log(verbose_log) if verbose_log is not None
               else {"passed": [], "failed": [], "skipped": [], "xfail": [],
                     "xpass": [], "error": [], "skip_reasons": []})

    deselected: list[str] = []
    collected_total: list[str] = []
    collected_selected: list[str] = []
    if raw_collect:
        collected_total = collect_node_ids(python, use_filters=False)
        collected_selected = collect_node_ids(python, use_filters=True)
        deselected = sorted(set(collected_total) - set(collected_selected))

    git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                              text=True, capture_output=True, check=True
                              ).stdout.strip()

    return {
        "schema": BASELINE_SCHEMA,
        "ts_iso": utc_now_iso_z(),
        "git_head_prechange": git_head,
        "interpreter": python,
        "canonical_command": CANONICAL_CMD,
        "k_expression": K_EXPR,
        "marker_expression": MARK_EXPR,
        "prechange_raw_log": str(prechange_log),
        "verbose_log": str(verbose_log) if verbose_log is not None else None,
        "counts": {
            "passed": tail["passed"],
            "skipped": tail["skipped"],
            "deselected": tail["deselected"],
            "failed": 0,
            "warnings": tail["warnings"],
            "duration_s": tail["duration_s"],
            "exit_code": 0,
        },
        "summary_line": tail["summary_line"],
        "skipped_names": verbose["skipped"],
        "skipped_reasons": verbose["skip_reasons"],
        "deselected_names": deselected,
        "collected_total": len(collected_total),
        "collected_selected": len(collected_selected),
        "regression_use": (
            "Task 4 起全套两排除结果对账：passed 不得低于 counts.passed；"
            "skipped/deselected 名单变化必须经评审并重新冻结（禁止 test.skip/"
            "放水新增跳行）。"),
    }


def _write_atomic_0600(path: Path, doc: dict) -> None:
    # L6：统一走 fsutil.atomic_write_text（mkstemp 0600+fsync+replace 的 G-7
    # 单一源），不再自造 tmp/chmod/replace。
    atomic_write_text(
        path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="冻结 Task0 全套测试基线")
    ap.add_argument("--prechange-log", required=True,
                    help="改动前实跑的 -q 原始日志（权威计数，含 EXIT=）")
    ap.add_argument("--verbose-log", default=None,
                    help="带 -v -rs 的实跑日志（skipped 名单/原因）")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-collect", action="store_true",
                    help="跳过 --collect-only（名单字段留空）")
    args = ap.parse_args()
    doc = build_baseline(prechange_log=Path(args.prechange_log),
                         verbose_log=Path(args.verbose_log)
                         if args.verbose_log else None,
                         python=args.python,
                         raw_collect=not args.no_collect)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic_0600(out, doc)
    print(json.dumps({"out": str(out),
                      "passed": doc["counts"]["passed"],
                      "skipped": doc["counts"]["skipped"],
                      "deselected": doc["counts"]["deselected"],
                      "skipped_names": doc["skipped_names"],
                      "deselected_n": len(doc["deselected_names"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
