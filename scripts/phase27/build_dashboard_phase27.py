#!/usr/bin/env python3
"""Phase 2.7 AC 达成度看板（0 付费、只读）。

产出 ``$SIMPRAXIS_PHASE27_ROOT/task7_milestone/dashboard_phase27.json``：
git 状态、四段测试读数（冷跑 / kbint / 容器场 / 验收套）、四表行数、账本汇总、
逐 Task 结果、环境出清、AC 计数。

**契约**（与 ``check_phase27_closure.py`` 互证，但实现独立——D7-1）：
每段测试日志必须恰好含**一条** ``# cmd:`` 行与**一条** ``rc=`` 行；缺一即
``DashboardError``（CLI exit 2），绝不产「半张绿」看板。

用法：
  python scripts/phase27/build_dashboard_phase27.py \
      --cold-run  .../cold_run.raw.log \
      --kbint     .../kbint.raw.log \
      --container .../container_run.raw.log \
      --out       .../dashboard_phase27.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

PLAN = Path.home() / "文档/Phase_2.7_Plan_detailed.md"

SCHEMA = "simpraxis.rsi_dashboard_phase27.v1"

DECLARED_AC_TOTAL = 67
DECLARED_AC_PER_TASK = {"T0": 9, "T1": 10, "T2": 6, "T3": 13, "T4": 9,
                        "T5": 5, "T6": 8, "T7": 7}

TABLE_NAMES = ("kb_chunks", "solution_chunks", "phase1_learnings",
               "phase2_learnings")

TASK_RESULTS = [
    {"task": "T0", "status": "closed", "paid": 0,
     "result": "开工基座：呼量 v2 / 看门狗 / 自检行 / 00_env 根登记（BR-8）。"},
    {"task": "T1", "status": "closed", "paid": 0,
     "result": "Stage0 四段探针；宿主不 import 物理引擎，容器探针出证。"},
    {"task": "T2", "status": "closed", "paid": 0,
     "result": "可执行技能库 + skills_manifest.v1（BR-4 依赖与 import 探针）。"},
    {"task": "T3", "status": "closed", "paid": 0,
     "result": "弹性内环 / ENV_SPEC / 微探针 / agentic-v4 缓存纪元。"},
    {"task": "T4", "status": "closed_not_established", "paid": 52,
     "result": "双墙两模式均判 NOT_ESTABLISHED（模式 R 17 呼 / 模式 N 35 呼）。"},
    {"task": "T5", "status": "closed", "paid": 0,
     "result": "多 Stage 路线图状态机 + 飞书富卡真人演练。"},
    {"task": "T6", "status": "closed", "paid": 15,
     "result": "LIBERO VLA 多 Stage 战役：主策略 0/10，对照臂不可达，"
               "内容层回收 12 块。"},
    {"task": "T7", "status": "closed", "paid": 0,
     "result": "里程碑收尾：冷跑/kbint/容器场 + 证据包 + 机判 + 三签。"},
]


class DashboardError(RuntimeError):
    """日志不可审或 AC 计数与声明不符；CLI exit 2。"""


def _run(cmd: "list[str]") -> str:
    return subprocess.run(cmd, cwd=_REPO, capture_output=True,
                          text=True).stdout.strip()


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_state() -> dict:
    return {
        "commit": _run(["git", "rev-parse", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "ahead_origin_main": int(_run(
            ["git", "rev-list", "--count", "origin/main..HEAD"]) or 0),
        "dirty": bool(_run(["git", "status", "--porcelain"])),
        "push_policy": "本地攒批未推送；推送仅经用户显式窗口指示（人工 STOP 门）",
    }


_SUMMARY_RE = re.compile(r"\b\d+ passed\b")


def tests_state(result_path: "Path | None") -> dict:
    """吃一套实跑日志：唯一 pytest 摘要行 + 唯一 rc 行 + `# cmd:` 头。

    各计数字段**按名提取**（pytest 的打印次序是 `failed, passed, skipped,
    deselected`，不是固定次序），摘要行必须**恰一条**——多行摘要往往意味着
    日志里混进了子进程的 pytest 输出，此时「取最后一条」很可能读错。
    """
    if result_path is None or not Path(result_path).is_file():
        raise DashboardError(f"缺测试日志：{result_path}")
    text = Path(result_path).read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if _SUMMARY_RE.search(ln)]
    if not lines:
        raise DashboardError(f"测试日志里找不到 pytest 摘要行：{result_path}")
    if len(lines) > 1:
        raise DashboardError(
            f"测试日志里 pytest 摘要行应恰 1 条，实得 {len(lines)} 条"
            f"（疑似混入子进程输出）：{result_path}")
    line = lines[0]
    rcs = re.findall(r"^rc=(\d+)$", text, re.M)
    if len(rcs) != 1:
        raise DashboardError(
            f"测试日志里 rc= 行应恰 1 条，实得 {len(rcs)} 条：{result_path}")
    cmds = re.findall(r"^# cmd: (.+)$", text, re.M)
    if len(cmds) != 1:
        raise DashboardError(
            f"测试日志里 `# cmd:` 行应恰 1 条，实得 {len(cmds)} 条"
            f"（命令口径不可审）：{result_path}")

    def _n(field: str) -> int:
        m = re.search(rf"\b(\d+) {field}\b", line)
        return int(m.group(1)) if m else 0

    return {
        "source": str(result_path),
        "summary": line,
        "passed": _n("passed"),
        "failed": _n("failed"),
        "skipped": _n("skipped"),
        "deselected": _n("deselected"),
        "rc": int(rcs[0]),
        "command": cmds[0].strip(),
    }


def ac_counts(plan_path: Path = PLAN) -> dict:
    text = plan_path.read_text(encoding="utf-8")
    ids = sorted({(int(a), int(b)) for a, b in
                  re.findall(r"AC-T(\d+)-(\d+)", text)})
    per = {f"T{i}": 0 for i in range(8)}
    for t, _k in ids:
        per[f"T{t}"] = per.get(f"T{t}", 0) + 1
    return {
        "plan": str(plan_path),
        "total": len(ids),
        "per_task": per,
        "declared_total": DECLARED_AC_TOTAL,
        "declared_per_task": DECLARED_AC_PER_TASK,
        "ids_sha256": __import__("hashlib").sha256(
            ",".join(f"AC-T{t}-{k}" for t, k in ids).encode()).hexdigest(),
        "matches_declared": (len(ids) == DECLARED_AC_TOTAL
                             and per == DECLARED_AC_PER_TASK),
    }


def table_rows() -> dict:
    import lancedb
    from memory_flywheel import kb_pins

    db = lancedb.connect(kb_pins.default_lancedb_uri())
    out = {}
    for name in TABLE_NAMES:
        try:
            out[name] = db.open_table(name).count_rows()
        except Exception as exc:                       # noqa: BLE001
            out[name] = f"<missing: {type(exc).__name__}>"
    return out


def ledger_block(ledger: Path) -> dict:
    rows = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    calls = [r for r in rows if r.get("event") == "call"]
    by_task: "dict[str, int]" = {}
    by_stage: "dict[str, int]" = {}
    by_batch: "dict[str, int]" = {}
    for r in calls:
        by_task[str(r.get("task"))] = by_task.get(str(r.get("task")), 0) + 1
        by_stage[str(r.get("stage"))] = by_stage.get(str(r.get("stage")), 0) + 1
        by_batch[str(r.get("batch_id"))] = by_batch.get(
            str(r.get("batch_id")), 0) + 1
    return {
        "path": str(ledger),
        "total_rows": len(rows),
        "n_calls": len(calls),
        "n_selfcheck": sum(1 for r in rows if r.get("event") == "selfcheck"),
        "n_blocked": sum(1 for r in rows
                         if r.get("event") == "preflight_blocked"),
        "ok": sum(1 for r in calls if r.get("ok")),
        "fail": sum(1 for r in calls if not r.get("ok")),
        "by_task": by_task,
        "by_stage": by_stage,
        "by_batch": by_batch,
    }


def env_block() -> dict:
    ps = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                        capture_output=True, text=True).stdout.split()
    containers = [c for c in ps if "simpraxis" in c.lower()
                  or "sim_praxis" in c.lower()]
    vram = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True).stdout.split()
        vram = max(int(x) for x in out) if out else None
    except (OSError, ValueError):
        vram = None
    df = subprocess.run(["df", "-h", "/mnt"], capture_output=True,
                        text=True).stdout.strip().splitlines()
    return {
        "containers_present": containers,
        "vram_mib": vram,
        "vram_ok_le_200": (vram is not None and vram <= 200),
        "mnt": df[-1] if df else None,
    }


def build(*, cold_run: Path, kbint: Path, container_run: Path,
          ledger: Path, plan_path: Path = PLAN) -> dict:
    counts = ac_counts(plan_path)
    if not counts["matches_declared"]:
        raise DashboardError(
            f"AC 计数与声明不符：total={counts['total']}"
            f"（声明 {DECLARED_AC_TOTAL}），per_task={counts['per_task']}")
    tests = {
        "cold_run": tests_state(cold_run),
        "kbint": tests_state(kbint),
        "container_run": tests_state(container_run),
    }
    # 「半张绿」守卫：任一段红即拒绝出板。冷跑是主判据，活库/容器套同样是
    # 锁内适配 2 纳入收尾的判据——只守冷跑会让 kbint 红着也能出绿板。
    #
    # 锁内适配 3：验收套（AC-T7-1..7）**不进看板**。它读的正是本看板与本
    # 证据包，若在此处要求它绿，就形成「看板要绿→验收套要跑→验收套要读
    # 看板」的死循环；它作为独立车道留日志，由 AC-T7-7 与 §8 对账。
    for key in ("cold_run", "kbint", "container_run"):
        blk = tests[key]
        if blk["rc"] != 0 or blk["failed"] != 0:
            raise DashboardError(
                f"{key} rc={blk['rc']} failed={blk['failed']}：看板不产")
    return {
        "schema": SCHEMA,
        "version": "1.0",
        "generated_at": utc_now_iso_z(),
        "git": git_state(),
        "tests": tests,
        "ac_counts": counts,
        "lance_tables": table_rows(),
        "ledger": ledger_block(ledger),
        "tasks": TASK_RESULTS,
        "closeout": env_block(),
    }


def main(argv: "list[str] | None" = None) -> int:
    from orchestrator.common.runtime_paths import (
        phase27_ledger_path, phase27_root)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root = phase27_root() / "task7_milestone"
    ap.add_argument("--cold-run", type=Path, default=root / "cold_run.raw.log")
    ap.add_argument("--kbint", type=Path, default=root / "kbint.raw.log")
    ap.add_argument("--container", type=Path,
                    default=root / "container_run.raw.log")
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--plan", type=Path, default=PLAN)
    ap.add_argument("--out", type=Path, default=root / "dashboard_phase27.json")
    args = ap.parse_args(argv)

    try:
        dash = build(cold_run=args.cold_run, kbint=args.kbint,
                     container_run=args.container,
                     ledger=args.ledger or phase27_ledger_path(),
                     plan_path=args.plan)
    except DashboardError as exc:
        print(f"[DASHBOARD] {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dash, ensure_ascii=False,
                                   indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    t = dash["tests"]
    print(f"[DASHBOARD] {args.out}\n"
          f"  cold:      passed={t['cold_run']['passed']} rc={t['cold_run']['rc']}\n"
          f"  kbint:     passed={t['kbint']['passed']} rc={t['kbint']['rc']}\n"
          f"  container: passed={t['container_run']['passed']} "
          f"rc={t['container_run']['rc']}\n"
          f"  ac_total={dash['ac_counts']['total']} "
          f"tables={dash['lance_tables']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
