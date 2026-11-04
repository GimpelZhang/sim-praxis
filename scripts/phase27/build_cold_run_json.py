#!/usr/bin/env python3
"""Phase 2.7 Task 7 —— 冷跑证据件 ``cold_run.json`` 生成器（0 付费、只读）。

上一轮该件是**手写**的：数与日志对不对、commit 是不是被签的那个，全靠人眼。
本脚本把它变成可复现产物：从 raw 日志解析出 ``# cmd:`` / 摘要行 / ``rc=``，
按名提取计数，绑定 ``git rev-parse HEAD`` 与日志 sha256，并在落盘前自校验。

纪律：
- 日志必须**恰一条**摘要行、恰一条 ``rc=``、恰一条 ``# cmd:``（多段拼接即
  拒绝，避免把子进程摘要读进来）；
- ``rc != 0`` 或 ``failed != 0`` 默认拒绝落盘（红日志不是证据）；确需留证时
  显式 ``--allow-red``，并把 ``red: true`` 写进 json；
- ``note`` 逐字声明「日志即最终版」，与机判/AC 的读法一致（verifier 缺口 2）。

用法：
  python scripts/phase27/build_cold_run_json.py                     # 默认路径
  python scripts/phase27/build_cold_run_json.py --log L --out J
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SCHEMA = "simpraxis.phase27.cold_run.v1"


class ColdRunError(RuntimeError):
    """日志不可审 / 非绿；CLI exit 2，绝不产出与日志不符的 json。"""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def parse_log(text: str) -> dict:
    """``# cmd:`` / 摘要行 / ``rc=`` 三元；各计数**按名**提取，不依赖次序。"""
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    lines = [ln.strip() for ln in text.splitlines()
             if re.search(r"\b\d+ passed\b", ln)]
    if len(cmds) != 1 or len(rcs) != 1:
        raise ColdRunError(
            f"日志不可审：`# cmd:` {len(cmds)} 条、`rc=` {len(rcs)} 条（各应恰 1）")
    if len(lines) != 1:
        raise ColdRunError(
            f"日志里 pytest 摘要行应恰 1 条，实得 {len(lines)} 条"
            "（多段拼接会把子进程的摘要读进来）")

    def _n(field: str) -> int:
        m = re.search(rf"\b(\d+) {field}\b", lines[0])
        return int(m.group(1)) if m else 0

    return {"command": cmds[0].strip(), "summary": lines[0],
            "passed": _n("passed"), "failed": _n("failed"),
            "skipped": _n("skipped"), "deselected": _n("deselected"),
            "rc": int(rcs[0])}


def build(*, log: Path, repo: Path = _REPO, allow_red: bool = False,
          note: str | None = None) -> dict:
    if not log.is_file():
        raise ColdRunError(f"缺冷跑日志：{log}")
    parsed = parse_log(log.read_text(encoding="utf-8", errors="replace"))
    if not allow_red and (parsed["rc"] != 0 or parsed["failed"] != 0):
        raise ColdRunError(
            f"冷跑非绿（rc={parsed['rc']}, failed={parsed['failed']}）："
            "红日志不得充当证据（确需留证加 --allow-red）")
    return {
        "schema": SCHEMA,
        "command": parsed["command"],
        "commit": _git_head(repo),
        "log": str(log),
        "log_sha256": _sha256(log),
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "skipped": parsed["skipped"],
        "deselected": parsed["deselected"],
        "rc": parsed["rc"],
        "summary": parsed["summary"],
        "red": bool(parsed["rc"] != 0 or parsed["failed"] != 0),
        "generated_at": datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": note or (
            "日志即最终版（权威轮，逐字 CLAUDE.md 两排除命令、无 --ignore）；"
            "验收套按 milestone_closeout marker 在收集期 deselect，"
            "其独立车道日志见 milestone_closeout.raw.log"),
    }


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import phase27_root

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root = phase27_root() / "task7_milestone"
    ap.add_argument("--log", type=Path, default=root / "cold_run.raw.log")
    ap.add_argument("--out", type=Path, default=root / "cold_run.json")
    ap.add_argument("--allow-red", action="store_true")
    args = ap.parse_args(argv)
    try:
        doc = build(log=args.log, allow_red=args.allow_red)
    except ColdRunError as exc:
        print(f"[COLD_RUN] {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                                   sort_keys=True) + "\n", encoding="utf-8")
    print(f"[COLD_RUN] {args.out}\n"
          f"  passed={doc['passed']} failed={doc['failed']} "
          f"rc={doc['rc']} commit={doc['commit'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
