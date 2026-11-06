#!/usr/bin/env python3
"""Phase 2.7 独立账对账器（AC-T0-3 机判入口；0 付费、0 联网）。

口径（沿用 Phase 2.6 已签收的 selfcheck 先例）：
  * 整数 ``seq`` 从 1 起**全局连续**（``selfcheck`` 占号、``preflight_blocked``
    的 ``seq`` 为 null 不占号）；
  * ``event != "call"`` 的行单列对账，不计入实呼；
  * ``global_used_after`` 必须与实呼计数同步递增（硬帽口径不得静默失效）；
  * ``ts_iso`` 必须严格 UTC ``Z``。

用法：
  python scripts/phase27/check_ledger_phase27.py             # 查活账
  python scripts/phase27/check_ledger_phase27.py --selftest  # 自检（tmp 根）
  python scripts/phase27/check_ledger_phase27.py --ledger P --report R.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

STRICT_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
ALLOWED_EVENTS = ("call", "selfcheck", "preflight_blocked")


def _rows_from(path: Path) -> "list[dict]":
    if not path.exists():
        return []
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[phase27 ledger] 第 {lineno} 行不是合法 JSON：{exc}")
    return rows


def analyze(rows: "list[dict]") -> dict:
    """返回 {"problems": [...], "summary": {...}}；problems 空即 rc=0。"""
    problems: "list[str]" = []
    calls = [r for r in rows if r.get("event") == "call"]
    selfchecks = [r for r in rows if r.get("event") == "selfcheck"]
    blocked = [r for r in rows if r.get("event") == "preflight_blocked"]

    for idx, r in enumerate(rows, 1):
        ev = r.get("event")
        if ev not in ALLOWED_EVENTS:
            problems.append(f"第 {idx} 行 event 不在白名单：{ev!r}")
        ts = r.get("ts_iso")
        if not isinstance(ts, str) or not STRICT_Z.match(ts):
            problems.append(f"第 {idx} 行 ts_iso 非严格 UTC Z：{ts!r}")
        seq = r.get("seq")
        if seq is not None and not isinstance(seq, int):
            problems.append(f"第 {idx} 行 seq 既非 int 也非 null：{seq!r}")
        if ev == "preflight_blocked" and seq is not None:
            problems.append(f"第 {idx} 行 preflight_blocked 的 seq 必须为 null")

    int_seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    if int_seqs != list(range(1, len(int_seqs) + 1)):
        problems.append(f"整数 seq 不连续：{int_seqs}")

    # 恰 1 条：**0 条也红**——空账/自造账不得静默返回 problems=[]（本脚本
    # 自称 AC-T0-3 机判入口，独立 rc 必须真能咬）。
    if len(selfchecks) != 1:
        problems.append(f"selfcheck 行应恰有 1 条，实际 {len(selfchecks)}")
    if selfchecks and selfchecks[0].get("seq") != 1:
        problems.append(f"selfcheck 必须占 seq=1，实际 {selfchecks[0].get('seq')!r}")
    if selfchecks and selfchecks[0].get("model") is not None:
        problems.append("selfcheck 行 model 必须为 null")

    for i, r in enumerate(calls, 1):
        if r.get("global_used_after") != i:
            problems.append(
                f"第 {i} 条实呼 global_used_after={r.get('global_used_after')!r}，应为 {i}")
            break

    return {
        "problems": problems,
        "summary": {
            "total_rows": len(rows),
            "call_rows": len(calls),
            "selfcheck_rows": len(selfchecks),
            "blocked_rows": len(blocked),
            "seq_first": (min(int_seqs) if int_seqs else None),
            "seq_last": (max(int_seqs) if int_seqs else None),
            "global_used": len(calls),
        },
    }


def _selfcheck(ledger_arg: str | None) -> int:
    """自检：在 tmp 根里造正/负样本，验证 analyze 的每个判据都会触发。"""
    from orchestrator.common import runtime_paths as rp  # noqa: PLC0415
    from orchestrator.gateway import quota_ledger as ql  # noqa: PLC0415

    ts = ql._now_iso()
    ok_rows = [
        {"ts_iso": ts, "event": "selfcheck", "seq": 1, "model": None, "ok": True,
         "global_used_after": 0},
        {"ts_iso": ts, "event": "call", "seq": 2, "model": "m",
         "global_used_after": 1, "ok": True},
        {"ts_iso": ts, "event": "call", "seq": 3, "model": "m",
         "global_used_after": 2, "ok": False},
        {"ts_iso": ts, "event": "preflight_blocked", "seq": None, "model": None,
         "global_used_after": 2, "ok": False},
    ]
    res = analyze(ok_rows)
    if res["problems"]:
        print("[selftest] 正样本被判红：" + "; ".join(res["problems"]))
        return 1
    if res["summary"]["call_rows"] != 2 or res["summary"]["blocked_rows"] != 1:
        print(f"[selftest] 正样本计数不对：{res['summary']}")
        return 1

    negatives = {
        "seq 断档": [ok_rows[0], dict(ok_rows[1], seq=3)],
        "ts 非 Z": [dict(ok_rows[0], ts_iso="2026-11-03T10:00:00+08:00")],
        "selfcheck 不在 seq1": [dict(ok_rows[0], seq=2),
                                dict(ok_rows[1], seq=1)],
        "blocked 占 seq": [dict(ok_rows[3], seq=4)],
        "global_used 断档": [ok_rows[0], ok_rows[1],
                             dict(ok_rows[2], global_used_after=7)],
        "未知 event": [dict(ok_rows[0], event="bogus")],
        "缺 selfcheck": [dict(ok_rows[1], seq=1, global_used_after=1)],
        "空账": [],
    }
    for name, rows in negatives.items():
        if not analyze(rows)["problems"]:
            print(f"[selftest] 负样本「{name}」未判红（判据失效）")
            return 1

    # 端到端：真跑 QuotaLedger 落三行，再用本脚本口径复算。
    # 锁内适配（Phase 2.8.1 T0）：phase27 已封存，写改指 phase281；本段验证的是
    # analyze() 的判据在「真账形态」上成立，与期号无关（analyze 不读期号/帽集）。
    with tempfile.TemporaryDirectory() as td:
        roots = {"SIMPRAXIS_PHASE1_ROOT": f"{td}/phase1",
                 "SIMPRAXIS_PHASE2_ROOT": f"{td}/phase2",
                 "SIMPRAXIS_PHASE25_ROOT": f"{td}/phase25",
                 "SIMPRAXIS_PHASE26_ROOT": f"{td}/phase26",
                 "SIMPRAXIS_PHASE27_ROOT": f"{td}/phase27",
                 "SIMPRAXIS_PHASE281_ROOT": f"{td}/phase281",
                 "SIMPRAXIS_GATEWAY_STATE": f"{td}/gwstate"}
        old = {k: os.environ.get(k) for k in roots}
        os.environ.update(roots)
        try:
            # 开账 selfcheck 行由 Task 0 一次性写入（QuotaLedger 无公开 API），
            # 端到端样本按同一形状自行种入，再接 QuotaLedger.record——
            # 「缺 selfcheck 即红」的判据要能在真账形态上验到。
            led_path = rp.phase281_ledger_path()
            led_path.parent.mkdir(parents=True, exist_ok=True)
            led_path.write_text(json.dumps({
                "ts_iso": ql._now_iso(), "event": "selfcheck", "seq": 1,
                "model": None, "ok": True, "global_used_after": 0,
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(led_path, 0o600)
            led = ql.QuotaLedger.phase281()
            for i in (1, 2, 3):
                led.record("coding_debug", task=4, batch_id="P27-SELFTEST",
                           ok=True, model="selftest-model", thread_id=f"t{i}")
            rows = _rows_from(rp.phase281_ledger_path())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    res = analyze(rows)
    if res["problems"]:
        print("[selftest] 端到端账被判红：" + "; ".join(res["problems"]))
        return 1
    if res["summary"]["call_rows"] != 3:
        print(f"[selftest] 端到端实呼数不对：{res['summary']}")
        return 1
    print(json.dumps({"selftest": "ok", "summary": res["summary"]},
                     ensure_ascii=False))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2.7 独立账对账器")
    ap.add_argument("--ledger", default=None, help="账本路径（缺省读活账）")
    ap.add_argument("--report", default=None, help="把结果写成 JSON")
    ap.add_argument("--selftest", action="store_true", help="自检（tmp 根）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selfcheck(args.ledger)

    from orchestrator.common import runtime_paths as rp  # noqa: PLC0415
    path = Path(args.ledger) if args.ledger else rp.phase27_ledger_path()
    rows = _rows_from(path)
    res = analyze(rows)
    if not path.is_file():
        # 缺账本 ≠ 空账合格：独立机判入口必须自证「账在」，否则 `--ledger
        # 不存在的路径` 会 rc=0 骗过调用方（AC-T7-2 自身另有 is_file 兜底，
        # 但本脚本的 rc 也可能被别处单独消费）。
        res["problems"].insert(0, f"账本不存在：{path}")
    payload = {"schema": "simpraxis.phase27.ledger_check.v1",
               "ledger": str(path), "row_count": len(rows), **res}
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
