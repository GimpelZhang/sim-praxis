#!/usr/bin/env python3
"""Phase 2.7 Task0 AC-T0-4：两排除全套基线冻结器（0 付费、0 联网）。

职责：跑一次 CLAUDE.md 收尾命令 → 原样落 raw log → 解析汇总行 → 生成
``$SIMPRAXIS_PHASE27_ROOT/estimates/t0_baseline_phase27.json``。

纪律：json 里每个数字都由 raw log 复算得出；**任何手写数字一律视为伪造**
（AC-T0-4 的机判只认 raw log）。同时落 T0.7 要求的两份辅助件：
``t0_git_head.txt``（本仓 + KB 仓 HEAD）与 ``sealed_ledger_sha256_before.txt``
（四本封存账开工前 sha256，证明只读）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.common import runtime_paths as rp  # noqa: E402
from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text  # noqa: E402

SCHEMA = "simpraxis.phase27.t0_baseline.v1"
# 逐字等于 AC-T0-4 的期望值（单引号版本，与 Phase 2.6 同款）。
CMD = ("pytest tests -q -k 'not test_04' "
       "-m 'not paid and not kb_integration'")
ARGV = ["-m", "pytest", "tests", "-q", "-k", "not test_04",
        "-m", "not paid and not kb_integration"]
COUNT_RE = {
    "passed": r"(\d+) passed",
    "skipped": r"(\d+) skipped",
    "deselected": r"(\d+) deselected",
    "failed": r"(\d+) failed",
    "warnings": r"(\d+) warnings?",
}
# Phase 2.6 收官基线（用于自动生成 note；不同即解释来源，不臆测原因）。
PREV_PHASE26_BASELINE = (2198, 7, 51)
# 锁内适配 L-7（bootstrap）：AC-T0-4 自己要求基线件存在，而基线件只能由本脚本
# 全绿跑出——形成循环。首个冻结轮允许「唯一失败项恰是 AC-T0-4」并落
# bootstrap raw；随后立刻再跑一次规范命令（此时 AC-T0-4 已能通过），由该**全绿**
# 轮覆盖基线件。bootstrap 轮不是放水：除 AC-T0-4 外任何失败/任何多失败一律拒冻。
BOOTSTRAP_FAILURE = "test_ac_t0_4_baseline_json_frozen"


def parse_summary(raw_text: str) -> dict:
    summary_line = None
    for line in raw_text.splitlines():
        if " passed" in line or " failed" in line:
            summary_line = line.strip()
    if summary_line is None:
        raise ValueError("raw log 里找不到 pytest 汇总行（禁止手写基线）")
    counts = {k: (int(m.group(1)) if (m := re.search(pat, summary_line)) else 0)
              for k, pat in COUNT_RE.items()}
    m_dur = re.search(r"in ([\d.]+)s", summary_line)
    if m_dur is None:
        raise ValueError(f"汇总行缺少耗时：{summary_line}")
    m_exit = re.search(r"^EXIT=(\d+)\s*$", raw_text, flags=re.MULTILINE)
    return {"summary_line": summary_line, **counts,
            "duration_s": float(m_dur.group(1)),
            "exit_code": int(m_exit.group(1)) if m_exit else None}


def _run_suite(python: str) -> tuple[str, int]:
    proc = subprocess.run([python, *ARGV], cwd=REPO_ROOT, text=True,
                          capture_output=True)
    return (proc.stdout + proc.stderr), proc.returncode


def _git_head(repo: Path) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                              capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _ledger_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _failing_tests(raw_text: str) -> list[str]:
    return re.findall(r"^FAILED (\S+)", raw_text, flags=re.MULTILINE)


def build(*, python: str, raw_path: Path, bootstrap: bool = False) -> dict:
    raw_text, rc = _run_suite(python)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw_text + f"\nEXIT={rc}\n", encoding="utf-8")
    tail = parse_summary(raw_text + f"\nEXIT={rc}\n")
    bootstrap_note = None
    if rc != 0 or tail["failed"] != 0:
        failing = _failing_tests(raw_text)
        if (bootstrap and tail["failed"] == 1 and len(failing) == 1
                and failing[0].endswith(BOOTSTRAP_FAILURE)):
            bootstrap_note = (
                f"bootstrap 轮：唯一失败项为 {failing[0]}（基线件尚未存在，"
                "循环依赖）；紧随其后的全绿轮会覆盖本件。")
        else:
            raise SystemExit(
                f"[freeze] 全套非全绿，禁止冻结：rc={rc} "
                f"failed={tail['failed']} failing={failing}\n"
                f"{tail['summary_line']}")

    ledgers = {
        "phase1": rp.phase1_ledger_path(),
        "phase2": rp.phase2_ledger_path(),
        "phase25": rp.phase25_ledger_path(),
        "phase26": rp.phase26_ledger_path(),
        "phase27": rp.phase27_ledger_path(),
    }
    sealed_sha = {name: _sha256(path) for name, path in ledgers.items()
                  if name != "phase27"}
    head = _git_head(REPO_ROOT)
    kb_root = Path.home() / "embodied-ai-sim-knowledge-base"
    kb_head = _git_head(kb_root) if kb_root.is_dir() else None

    est = rp.phase27_estimates_dir()
    (est / "t0_git_head.txt").write_text(
        f"simpraxis {head}\nkb {kb_head}\n", encoding="utf-8")
    (est / "sealed_ledger_sha256_before.txt").write_text(
        "".join(f"{name} {sha} {ledgers[name]}\n"
                for name, sha in sealed_sha.items()), encoding="utf-8")

    triple = (tail["passed"], tail["skipped"], tail["deselected"])
    doc = {
        "schema": SCHEMA,
        "ts_iso": utc_now_iso_z(),
        "git_head": head,
        "kb_head": kb_head,
        "interpreter": python,
        "cmd": CMD,
        "passed": tail["passed"],
        "skipped": tail["skipped"],
        "deselected": tail["deselected"],
        "failed": tail["failed"],
        "duration_s": tail["duration_s"],
        "exit_code": rc,
        "summary_line": tail["summary_line"],
        "raw_log_relpath": str(raw_path.relative_to(rp.phase27_root())),
        "ledger_rows": {name: _ledger_rows(path)
                        for name, path in ledgers.items()},
        "sealed_ledger_sha256": sealed_sha,
    }
    if bootstrap_note:
        doc["bootstrap_note"] = bootstrap_note
    if triple != PREV_PHASE26_BASELINE:
        doc["note"] = (
            f"与 Phase 2.6 收官基线 {PREV_PHASE26_BASELINE} 不同：本件是 Phase 2.7 "
            f"Task0 改动【前】的实跑（raw log 为准），差异来源见 "
            f"{doc['raw_log_relpath']} 与 M2 之后的提交累积。")
    return doc


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="冻结 Phase 2.7 Task0 基线")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--raw", default=None, help="raw log 路径（缺省 estimates/）")
    ap.add_argument("--out", default=None, help="json 路径（缺省 estimates/）")
    ap.add_argument("--bootstrap", action="store_true",
                    help="首轮引导：仅允许唯一失败项为 AC-T0-4（随后必须再跑全绿轮）")
    args = ap.parse_args(argv)

    est = rp.phase27_estimates_dir()
    raw_path = Path(args.raw) if args.raw else est / "t0_baseline_pytest.raw.log"
    out = Path(args.out) if args.out else est / "t0_baseline_phase27.json"
    doc = build(python=args.python, raw_path=raw_path,
                bootstrap=args.bootstrap)
    atomic_write_text(out, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "json": str(out), "raw": str(raw_path),
        "passed": doc["passed"], "skipped": doc["skipped"],
        "deselected": doc["deselected"], "duration_s": doc["duration_s"],
        "git_head": doc["git_head"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
