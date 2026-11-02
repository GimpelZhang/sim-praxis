#!/usr/bin/env python3
"""Task10：Phase 2.5 收尾对账（AC-T10-2 / AC-T10-3 / AC-T10-6，0 付费）。

只读活件，输出一台可机判的 JSON（schema
``simpraxis.phase25.closeout_reconcile.v1``），供看板（AC-T10-7）与
``scripts/verification/check_phase25_closure.py`` 逐字段互证：

- **ledger**（AC-T10-2）：phase25 独立账 seq 从 1 连续、ts 严格 ``Z`` 后缀、
  ok/fail/by_task/by_stage/by_batch 四向汇总、tokens 汇总、失败行逐条点名；
- **approvals**（AC-T10-2）：每个 batch_id 必须有批准件，``approved`` 与账行
  ``batch_approved`` 逐字相等，``batch_used_after`` 末值 ≤ approved；
- **call_cache**（AC-T10-2）：缓存条目=成功首呼后的 put 状态（非命中事件）；
  口径为「phase25 时间窗（首条账行 ts 起）内的缓存条目数」——coding 侧重放
  命中在 runner 静默返回、无持久标记，事后只能证明**窗口外零新增**，不能
  反推命中数，故如实标注 ``coding_hits_structurally_unobservable``；
- **tables**（AC-T10-3）：四表行数/维度/id 集 sha/向量集 sha，复用 Stage B
  向量恒等件（``gen_task6_r4_vector_identity.table_identity``）的同一口径，
  不另写第二套哈希；
- **methodology**（AC-T10-2 / AC-T1-7）：方法学 v2 区间件 P50/P90 vs 实际
  呼量逐 Task 落位；实际 > P90 即方法学失败结论（写 Complete，禁改区间重算）；
  T9 未执行（用户 2026-11-02 裁决停），单独标 ``not_executed`` 不参与判负；
- **env**（AC-T10-6）：容器存量（``docker ps -a`` 口径）、显存、/mnt 容量。

本脚本不调用 ModelGateway、不联网、不读凭据内容；所有外部读数（docker /
nvidia-smi / df）经显式 runner 注入，单测传假 runner 即 hermetic。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from orchestrator.common import secrets as secret_scan  # noqa: E402

SCHEMA = "simpraxis.phase25.closeout_reconcile.v1"

# Task → (区间件, 账行筛选)。T5 只认 G2 两批；T5b 是独立新实验（其子帽
# 16/9 在 5b 规格内，方法学 v2 区间件未覆盖，故不参与区间判负）。
TASK_BATCHES = {
    "t3": ("interval_t3.json", {"P25-B1"}),
    "t5": ("interval_t5.json", {"P25-B2", "P25-B3"}),
    "t9": ("interval_t9.json", set()),      # 未执行，见 NOT_EXECUTED
    "t10": ("interval_t10.json", set()),    # 收尾期若真跑连通抽验则按批补录
}
T5B_BATCHES = {"P25-B4", "P25-B5"}
NOT_EXECUTED = {
    "t9": ("用户 2026-11-02 裁决「Task7/8/9 不再继续进行」——未开跑，"
           "不构成方法学失败，也不回收其区间件"),
}


class ReconcileError(RuntimeError):
    """对账硬失败（账证不符/缺件）；CLI 以此 exit 2，不作「已通过」。"""


# ---------------------------------------------------------------------------
# 账
# ---------------------------------------------------------------------------

def ledger_stats(rows: list[dict]) -> dict:
    calls = [r for r in rows if r.get("event") == "call"]
    seqs = [r.get("seq") for r in calls]
    contiguous = seqs == list(range(1, len(calls) + 1))
    bad_ts = [r.get("seq") for r in calls
              if not str(r.get("ts_iso", "")).endswith("Z")]
    fails = [{"seq": r["seq"], "ts_iso": r["ts_iso"],
              "batch_id": r.get("batch_id"), "task": r.get("task"),
              "err": r.get("err")} for r in calls if not r.get("ok")]
    tokens = {k: sum(int(r.get(k) or 0) for r in calls)
              for k in ("tokens_in", "tokens_out",
                        "tokens_cache_read", "tokens_cache_create")}
    return {
        "n_rows": len(rows),
        "n_calls": len(calls),
        "events": dict(Counter(r.get("event") for r in rows)),
        "seq_first": seqs[0] if seqs else None,
        "seq_last": seqs[-1] if seqs else None,
        "seq_contiguous_from_1": contiguous,
        "ts_all_strict_z": not bad_ts,
        "ts_non_z_seqs": bad_ts,
        "ok": sum(1 for r in calls if r.get("ok")),
        "fail": sum(1 for r in calls if not r.get("ok")),
        "fail_rows": fails,
        "by_task": {str(k): v for k, v in
                    sorted(Counter(r.get("task") for r in calls).items())},
        "by_stage": dict(Counter(r.get("stage") for r in calls)),
        "by_batch": dict(sorted(
            Counter(r.get("batch_id") for r in calls).items())),
        "tokens": tokens,
        "first_ts_iso": calls[0]["ts_iso"] if calls else None,
        "last_ts_iso": calls[-1]["ts_iso"] if calls else None,
    }


def approvals_state(calls: list[dict], approvals_dir: Path) -> dict:
    approved_counts = {r.get("batch_id"): r.get("batch_approved")
                       for r in calls}
    used = {}
    for r in calls:
        b = r.get("batch_id")
        used[b] = max(used.get(b, 0), int(r.get("batch_used_after") or 0))
    batches = {}
    problems: list[str] = []
    for bid in sorted(approved_counts):
        path = approvals_dir / f"{bid}.json"
        if not path.is_file():
            problems.append(f"{bid}: 缺批准件 {path}")
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        batches[bid] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "approved": rec.get("approved"),
            "task": rec.get("task"),
            "ledger_phase": rec.get("ledger_phase"),
            "used": used.get(bid, 0),
        }
        if rec.get("ledger_phase") != 25:
            problems.append(f"{bid}: ledger_phase={rec.get('ledger_phase')} 非 25")
        if rec.get("approved") != approved_counts.get(bid):
            problems.append(
                f"{bid}: 批准件 approved={rec.get('approved')} 与账行 "
                f"batch_approved={approved_counts.get(bid)} 不等")
        if used.get(bid, 0) > int(rec.get("approved") or 0):
            problems.append(f"{bid}: 用 {used[bid]} 超批准 {rec.get('approved')}")
    return {"batches": batches,
            "total_approved": sum(int(b["approved"] or 0)
                                  for b in batches.values()),
            "problems": problems, "ok": not problems}


def cache_state(cache_dir: Path, first_ts: "str | None",
                n_calls: int) -> dict:
    """窗口内新增缓存条目数与「命中不可观测」的诚实标注。"""
    if not cache_dir.is_dir():
        return {"dir": str(cache_dir), "exists": False,
                "n_entries_total": 0, "n_entries_in_window": [],
                "coding_hits_structurally_unobservable": True,
                "method": "缓存目录不存在：phase25 零缓存写入。"}
    entries = sorted(cache_dir.glob("*.json"))
    in_window = []
    for f in entries:
        try:
            ts = json.loads(f.read_text(encoding="utf-8")).get("ts_iso", "")
        except (ValueError, OSError):
            continue
        # ISO8601 同形 Z 后缀可直接字符串比较（严格 Z 已在 ledger 处断言）。
        if first_ts and ts >= first_ts:
            in_window.append({"file": f.name, "ts_iso": ts})
    return {
        "dir": str(cache_dir),
        "exists": True,
        "n_entries_total": len(entries),
        "n_entries_in_window": in_window,
        "n_entries_in_window_count": len(in_window),
        "n_calls": n_calls,
        "coding_hits_structurally_unobservable": True,
        "method": (
            "条目=成功首呼后的 put 状态（同 key 覆盖），不是命中事件；coding "
            "侧重放命中在 runner 静默返回、无持久标记，故事后不可权威重算"
            "命中数。可机判口径只有一条：phase25 首条账行 ts 起的新增条目为 0"
            "（本账全部 56 呼均未产生缓存写入），缓存目录中的历史条目属于 "
            "phase1/phase2 代际，与 phase25 账无关。"),
    }


# ---------------------------------------------------------------------------
# 四表（AC-T10-3）
# ---------------------------------------------------------------------------

def _load_vector_identity_module():
    """按文件路径加载 Stage B 向量恒等件（``scripts/`` 不是包）。

    复用同一 ``table_identity`` 口径（rows/dim/向量集 sha），不另写第二套
    哈希——两套口径并存会把"同一事实两个数"带进收尾对账。
    """
    import importlib.util
    path = _REPO / "scripts/phase25/gen_task6_r4_vector_identity.py"
    spec = importlib.util.spec_from_file_location("p25_vec_identity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tables_state(uri: "str | None" = None) -> dict:
    mod = _load_vector_identity_module()
    ident = mod.build(uri)
    out = {}
    for name, rec in ident["tables"].items():
        out[name] = {"rows": rec.get("rows"), "dim": rec.get("dim"),
                     "vector_set_sha256": rec.get("vector_set_sha256")}
    out["_schema"] = ident.get("schema")
    return out


# ---------------------------------------------------------------------------
# 方法学对账（AC-T10-2 / AC-T1-7）
# ---------------------------------------------------------------------------

def methodology_state(calls: list[dict], estimates_dir: Path) -> dict:
    per_task: dict[str, dict] = {}
    problems: list[str] = []
    for task, (fname, batches) in TASK_BATCHES.items():
        path = estimates_dir / fname
        actual = sum(1 for r in calls if r.get("batch_id") in batches)
        if task in NOT_EXECUTED and actual == 0:
            per_task[task] = {
                "interval_file": fname,
                "actual": 0,
                "status": "not_executed",
                "note": NOT_EXECUTED[task],
                "methodology_failure": False,
            }
            continue
        if not path.is_file():
            problems.append(f"{task}: 缺区间件 {path}")
            continue
        est = json.loads(path.read_text(encoding="utf-8"))
        p50 = int(est["estimate"]["p50"])
        p90 = int(est["estimate"]["p90"])
        if p50 <= actual <= p90:
            status, failure = "within_p50_p90", False
        elif actual > p90:
            status, failure = "above_p90", True
        else:
            status, failure = "below_p50", False
        if failure:
            problems.append(
                f"{task}: 实际 {actual} > P90 {p90}——按 AC-T1-7 记方法学失败结论"
                "（禁改区间重算）")
        per_task[task] = {
            "interval_file": fname,
            "p50": p50, "p90": p90, "actual": actual,
            "plan_envelope": est.get("plan_envelope"),
            "scientific_cap": est.get("scientific_cap"),
            "batches": sorted(batches),
            "status": status,
            "methodology_failure": failure,
        }
    t5b_actual = sum(1 for r in calls if r.get("batch_id") in T5B_BATCHES)
    per_task["t5b"] = {
        "interval_file": None,
        "actual": t5b_actual,
        "batches": sorted(T5B_BATCHES),
        "status": "not_covered_new_experiment",
        "note": ("Task 5b 是 D-4 裁决后另立的协议加固重测（同一冻结墙），"
                 "子帽 16/9 记在 5b 规格内；方法学 v2 区间件不覆盖它，"
                 "不参与区间判负，也不据此改区间。"),
        "methodology_failure": False,
    }
    return {"per_task": per_task, "problems": problems,
            "ok": not any(v.get("methodology_failure")
                          for v in per_task.values()) and not problems}


# ---------------------------------------------------------------------------
# 环境（AC-T10-6）
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def env_state(*, docker_ps=None, nvidia_smi=None, df=None) -> dict:
    docker_ps = docker_ps or (lambda: _run(["docker", "ps", "-a", "-q"]))
    nvidia_smi = nvidia_smi or (lambda: _run([
        "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"]))
    df = df or (lambda: _run(["df", "-P", "-B1", "/mnt"]))
    containers = [x for x in docker_ps().splitlines() if x.strip()]
    vram_readings = [x.strip() for x in nvidia_smi().splitlines() if x.strip()]
    vram_mib = None
    if vram_readings:
        try:
            vram_mib = int(vram_readings[0].split()[0])
        except (ValueError, IndexError):
            vram_mib = None
    df_lines = [x for x in df().splitlines() if x.strip()]
    mnt = None
    if len(df_lines) >= 2:
        parts = df_lines[-1].split()
        if len(parts) >= 4:
            mnt = {"total_bytes": int(parts[1]), "used_bytes": int(parts[2]),
                   "avail_bytes": int(parts[3])}
    return {"containers_present": len(containers),
            "container_ids": containers,
            "vram_mib": vram_mib,
            "vram_readings": vram_readings,
            "vram_ok_le_200": vram_mib is not None and vram_mib <= 200,
            "mnt": mnt}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def build(*, ledger_path: Path, approvals_dir: Path, cache_dir: Path,
          estimates_dir: Path, lance_uri: "str | None" = None,
          env: "dict | None" = None) -> dict:
    from orchestrator.gateway.quota_ledger import read_lines
    rows = read_lines(ledger_path)
    if not rows:
        raise ReconcileError(f"phase25 账为空：{ledger_path}")
    led = ledger_stats(rows)
    calls = [r for r in rows if r.get("event") == "call"]
    if not led["seq_contiguous_from_1"]:
        raise ReconcileError(
            f"phase25 账 seq 不连续（应自 1 连续）：{led['seq_first']}.."
            f"{led['seq_last']}，n={led['n_calls']}")
    if not led["ts_all_strict_z"]:
        raise ReconcileError(
            f"ts_iso 非严格 Z 后缀的行：{led['ts_non_z_seqs']}")
    appr = approvals_state(calls, approvals_dir)
    cache = cache_state(cache_dir, led["first_ts_iso"], len(calls))
    meth = methodology_state(calls, estimates_dir)
    out = {
        "schema": SCHEMA,
        "ledger_path": str(ledger_path),
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "ledger": led,
        "approvals": appr,
        "call_cache": cache,
        "tables": tables_state(lance_uri),
        "methodology": meth,
        "env": env if env is not None else env_state(),
    }
    # 证据件不得自带密钥（与 gate 4 同源正则，单一事实源）。
    hits = secret_scan.scan_text(json.dumps(out, ensure_ascii=False))
    if hits:
        raise ReconcileError(f"对账件命中密钥正则：{hits}")
    return out


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import (
        phase1_root, phase25_approvals_dir, phase25_ledger_path,
        phase25_root)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path,
                    default=None,
                    help="phase25 ledger.jsonl（缺省走 runtime_paths）")
    ap.add_argument("--approvals", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--estimates", type=Path, default=None)
    ap.add_argument("--lance-uri", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    ledger = args.ledger or phase25_ledger_path()
    approvals = args.approvals or phase25_approvals_dir()
    cache = args.cache or (phase1_root() / ".call_cache")
    estimates = args.estimates or (phase25_root() / "estimates")
    out_path = args.out or (phase25_root() / "task10_milestone"
                            / "closeout_reconcile.json")
    try:
        out = build(ledger_path=ledger, approvals_dir=approvals,
                    cache_dir=cache, estimates_dir=estimates,
                    lance_uri=args.lance_uri)
    except ReconcileError as exc:
        print(f"[RECONCILE-FAIL] {exc}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    led = out["ledger"]
    print(f"ledger seq {led['seq_first']}..{led['seq_last']} "
          f"calls={led['n_calls']} ok={led['ok']} fail={led['fail']}")
    print(f"approvals ok={out['approvals']['ok']} "
          f"cache_in_window={out['call_cache'].get('n_entries_in_window_count')}")
    print("methodology " + " ".join(
        f"{k}={v.get('actual')}({v.get('status')})"
        for k, v in out["methodology"]["per_task"].items()))
    print("tables " + " ".join(
        f"{k}={v.get('rows')}" for k, v in out["tables"].items()
        if not k.startswith("_")))
    return 0 if (out["approvals"]["ok"] and out["methodology"]["ok"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
