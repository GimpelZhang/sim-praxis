#!/usr/bin/env python
"""Phase 2.8.1 单 Trial 评测 CLI：``--trial-dir`` / ``--anchor`` / ``--out``。

**评测在隔离子进程里跑**（计划 §5.5「评测子进程纪律」）：

- argv 固定为 ``[<python>, "-I", "-c", <bootstrap>, <engine_repo_root>, ...]``；
- env 只放行 ``PATH/HOME/LANG/TMPDIR/SIMPRAXIS_PHASE281_ROOT``，
  任何 ``ANTHROPIC_*`` 与 ``SIMPRAXIS_ALLOW_PAID`` 一律**不传**；
- ``cwd`` 是空的 0700 临时目录（``mkdtemp``），跑完删除——不是 ``trial_dir``。

为什么是 ``-c`` bootstrap 而不是 ``-m``：``-I`` 同时忽略 ``PYTHONPATH`` 与用户
site，引擎仓又没 pip 装，所以只能由父进程把**引擎仓根**当位置参数传进去、
在 bootstrap 里落地。该路径永远由本文件的 ``__file__`` 推导，**从不**来自
trial 目录（否则等于把候选目录塞进 ``sys.path``，正是 R8 要堵的那条路）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 子进程 env 白名单（顺序无关；只放行这五个键）
CHILD_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TMPDIR",
                       "SIMPRAXIS_PHASE281_ROOT")

#: 子进程 bootstrap：落地引擎仓根后转调 ``evaluate_child.run``
CHILD_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "from orchestrator.benchmark.phase281.evaluate_child import run;"
    "raise SystemExit(run(sys.argv[2:]))"
)


def child_env(base: "dict[str, str] | None" = None) -> "dict[str, str]":
    """父进程环境 → 子进程白名单环境（凭据变量一定不在其中）。"""
    source = dict(os.environ if base is None else base)
    return {key: source[key] for key in CHILD_ENV_ALLOWLIST if key in source}


def build_child_argv(*, python: str, repo_root: Path, trial_dir: Path,
                     anchor: Path, out: Path, root: "Path | None" = None
                     ) -> "list[str]":
    """拼装隔离子进程 argv（AC-P281-1-13 的断言对象）。"""
    argv = [python, "-I", "-c", CHILD_BOOTSTRAP, str(repo_root),
            "--trial-dir", str(trial_dir), "--anchor", str(anchor),
            "--out", str(out)]
    if root is not None:
        argv.extend(["--root", str(root)])
    return argv


def run_child(*, trial_dir: Path, anchor: Path, out: Path,
              root: "Path | None" = None, python: "str | None" = None,
              repo_root: "Path | None" = None,
              timeout_s: int = 900) -> dict:
    """在隔离子进程里评测一个 Trial；返回子进程的摘要（含 rc/argv/cwd）。"""
    repo = Path(repo_root) if repo_root is not None else REPO_ROOT
    exe = python or sys.executable
    argv = build_child_argv(python=exe, repo_root=repo, trial_dir=Path(trial_dir),
                            anchor=Path(anchor), out=Path(out), root=root)
    cwd = Path(tempfile.mkdtemp(prefix="p281-eval-"))
    try:
        os.chmod(cwd, 0o700)
        proc = subprocess.run(argv, cwd=str(cwd), env=child_env(),
                              capture_output=True, text=True, timeout=timeout_s,
                              check=False)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    return {"rc": proc.returncode, "argv": argv, "stdout": proc.stdout,
            "stderr": proc.stderr, "cwd_removed": not cwd.exists()}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_evaluate", description="Phase 2.8.1 单 Trial 评测（隔离子进程）")
    ap.add_argument("--trial-dir", required=True)
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=None, help="数据根（缺省取锚的上两级）")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印子进程摘要")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    anchor = Path(args.anchor)
    root = Path(args.root) if args.root else anchor.parent.parent
    summary = run_child(trial_dir=Path(args.trial_dir), anchor=anchor,
                        out=Path(args.out), root=root)
    if args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "argv"},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        sys.stdout.write(summary["stdout"])
        sys.stderr.write(summary["stderr"])
    if summary["rc"] != 0:
        print(f"[phase281-evaluate] 子进程 rc={summary['rc']} "
              f"argv={summary['argv']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
