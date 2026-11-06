"""Phase 2.8.1 T1：评测子进程入口（隔离、无网络凭据、cwd 不在 trial 内）。

调用形态（**由 ``scripts/benchmark/phase281_evaluate.py`` 统一拼装**）::

    <python> -I -c "<bootstrap>" <engine_repo_root> \
        --trial-dir <trial_dir> --anchor <anchor.json> --out <result.json>

``-I`` 让子进程忽略 ``PYTHONPATH``/用户 site，因此**引擎仓根由父进程作为第一个
位置参数传入**，导入路径由父进程那段 bootstrap 字符串落地——本模块自身不含任何
``sys.path`` 操作（AC-P281-1-14 的静态扫描面之一，扫描的是源码文本，故这里连
那个调用形态的字面量都不写）。

本模块只做四件事：解析参数 → 载入仓外锚 → 调 ``CandidateEvaluator.evaluate`` →
原子写 0600 结果件。**不执行候选代码、不 import 候选模块、不联网。**
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_evaluator as ce
from orchestrator.common.fsutil import atomic_write_text

__all__ = ("REFUSED_ENV_PREFIXES", "main", "run")

#: 子进程若见到这些前缀的环境变量即拒绝开工（凭据不得进评测进程）
REFUSED_ENV_PREFIXES = ("ANTHROPIC_", "SIMPRAXIS_ALLOW_PAID")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281-evaluate-child",
        description="Phase 2.8.1 候选评测子进程（隔离）")
    ap.add_argument("--trial-dir", required=True)
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=None,
                    help="数据根（缺省取锚所在 private/ 的上级）")
    return ap


def _refused_env() -> "list[str]":
    return sorted(key for key in os.environ
                  if key.startswith(REFUSED_ENV_PREFIXES))


def run(argv: "list[str] | None" = None) -> int:
    """执行一次评测；返回进程退出码（0 = 结果件已写出，与 verdict 无关）。"""
    args = _parser().parse_args(argv)
    refused = _refused_env()
    if refused:
        print(f"[phase281-evaluate] 拒绝开工：评测子进程环境含凭据变量 {refused}",
              file=sys.stderr)
        return 2

    trial_dir = Path(args.trial_dir)
    anchor_path = Path(args.anchor)
    out = Path(args.out)
    root = Path(args.root) if args.root else anchor_path.parent.parent

    anchor = None
    anchor_error: "str | None" = None
    try:
        anchor = anchor_mod.load_anchor(anchor_path)
    except anchor_mod.AnchorError as exc:
        anchor_error = str(exc)

    evaluator = ce.CandidateEvaluator(anchor=anchor, root=root,
                                      anchor_error=anchor_error)
    doc = evaluator.evaluate(trial_dir)
    atomic_write_text(out, json.dumps(doc, ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n", mode=0o600)
    verdict = doc.get("verdict")
    passed = sum(1 for c in doc.get("criteria") or [] if c.get("passed"))
    total = len(doc.get("criteria") or [])
    print(f"[phase281-evaluate] {doc.get('trial_id')} verdict={verdict} "
          f"criteria={passed}/{total} -> {out}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    return run(argv)


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
