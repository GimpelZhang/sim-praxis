"""Phase 2.8.1 分析：Pilot 阶段的 go/no-go 机判（计划 §11.5 / §11.6）。

本模块只做**机械汇总**：把 6 个 Pilot Trial 的终态、出生、账本与评测判决读成
一张表，再按 §11.5 的四条**预注册规则**（先写后跑，不改）算出一个
``GO`` / ``HOLD``。它不解释结果、不挑挑拣拣、不因为「看着像 HOLD」就换个说法——
规则是外生的，代码只是它的实现。

锁内适配 A7-1（计划自相矛盾，如实登记）：§11.2 说 P281-7「仅两个薄壳，无新
逻辑」（``phase281_pilot_driver.sh`` + ``test_phase281_pilot.py``），但 §11.4
步骤 5 又要求 ``python -m orchestrator.benchmark.phase281.analysis --stage pilot``
产出 ``analysis/pilot_report.json``，而 §13.2 把 ``analysis.py`` 列在 P281-9 的
新增件里。三处不能同时成立。取最小解：本 Task 建 ``analysis.py`` 的 **pilot
段**（本文件），P281-9 在同一模块上补 confirmatory/判决段，不另起第二份汇总。

``stop_open`` 的口径（AC-P281-7-07 要求它是 §12.2 G281-4 的唯一数据源）：
这里登记的是**本模块能机判出来**的打开态 STOP——STOP-09（seq 断号）、
STOP-12（越过 60 上限）、STOP-16（容器/显存残留，仅在显式探测时）。§11.7 其余
几条（STOP-10 跨 Trial 缓存命中、STOP-14 判据被改、STOP-15 凭结果调参）不是
「读一份产物就能判」的，它们由各自的机判测试与证据包把关，本列表**不**替它们
作保——空列表只表示「这三条机判没发现问题」，不表示「全阶段无 STOP」。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 裸跑 `python -m`
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.benchmark.phase281 import constants  # noqa: E402

#: ``pilot_report.json`` 的 schema 名（AC-P281-7-07 的字段集同时钉在这）
PILOT_REPORT_SCHEMA = "simpraxis.phase281.pilot_report.v1"
#: AC-P281-7-07：字段集**恰为**这八个（多一个少一个都算契约漂移）
REPORT_FIELDS = frozenset({"schema", "generated_at", "per_trial", "verdicts",
                           "go_no_go", "reasons", "stop_open", "five_gates"})
#: Pilot 只跑一个重复（§11.1：3 任务 × 2 臂 × 1 重复 = 6）
PILOT_REP = 0
#: §11.3：三票各 20，合计 60 = 6 Trial × 10
PILOT_CALL_CAP = 60
#: §11.5：``BLOCKED_INFRA`` 达到这个数即 HOLD
HOLD_BLOCKED_INFRA = 2
#: Trial 终态集合（与 ``trial_harness.STATUSES`` 同源，这里只读不复制语义）
TERMINAL_STATUSES = ("FINISHED", "CAP_HIT", "BLOCKED_INFRA", "CRASHED")
#: 探针判据：显存上限（硬规则 5）
GPU_LIMIT_MIB = 200
#: G281-2 子进程上限（register --verify 只做本地文件哈希，正常 < 5s）
REGISTER_TIMEOUT_S = 120

TRIAL_JSON = "trial.json"


# ---------------------------------------------------------------------------
# 读盘
# ---------------------------------------------------------------------------

def trial_dir(root: Path, task_id: str, arm_id: str, rep: int) -> Path:
    """``<root>/runs/<task>/<arm>/rep<k>``（与 ``trial_harness`` 的落盘同形）。"""
    return Path(root) / "runs" / task_id / arm_id / f"rep{int(rep)}"


def evaluator_result_path(root: Path, task_id: str, arm_id: str,
                          rep: int) -> Path:
    """§11.4 步骤 4 的命名：``<task>_<arm>_rep<k>.json``。"""
    return Path(root) / "evaluator_results" / f"{task_id}_{arm_id}_rep{int(rep)}.json"


def read_json(path: Path) -> "dict[str, Any] | None":
    """读一份 JSON；缺件/坏件一律 ``None``（由调用方决定怎么记账）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _birth_status(trial: Mapping[str, Any]) -> str:
    birth = trial.get("birth")
    if isinstance(birth, Mapping):
        return str(birth.get("status") or "")
    return ""


def _observed_calls(trial: Mapping[str, Any]) -> "int | None":
    observed = trial.get("observed")
    if isinstance(observed, Mapping):
        value = observed.get("calls")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def collect_trials(root: Path, *, rep: int = PILOT_REP) -> "list[dict[str, Any]]":
    """把 3 任务 × 2 臂的 Trial 记录读成一张表（缺件也占一行，不静默丢）。"""
    rows: "list[dict[str, Any]]" = []
    for task_id in constants.TASK_IDS:
        for arm_id in constants.ARMS:
            path = trial_dir(root, task_id, arm_id, rep)
            trial = read_json(path / TRIAL_JSON)
            eval_path = evaluator_result_path(root, task_id, arm_id, rep)
            evaluation = read_json(eval_path)
            rows.append({
                "task_id": task_id,
                "arm_id": arm_id,
                "rep": int(rep),
                "trial_id": constants.trial_id(task_id, arm_id, rep),
                "trial_dir": str(path),
                "present": trial is not None,
                "status": (str(trial.get("status") or "") if trial else None),
                "birth_status": (_birth_status(trial) if trial else None),
                "used_calls": (trial.get("used_calls") if trial else None),
                "observed_calls": (_observed_calls(trial) if trial else None),
                "problems": (list(trial.get("problems") or []) if trial else []),
                "evaluator_path": str(eval_path),
                "verdict": (str(evaluation.get("verdict") or "")
                            if isinstance(evaluation, Mapping) else None),
            })
    return rows


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------

def ledger_snapshot(ledger_path: Path,
                    batches: "Sequence[str] | None" = None) -> dict:
    """读 phase281 账本（``selfcheck`` 占 seq 不计实呼）。缺件 = 0 行。

    机判失败（断号/时区/混列）**不让汇总崩掉**：本函数是「报告现状」的，
    STOP-09 要能被**写进报告**才有人看得见。故先严格判一次，判红则记下
    ``validation_error`` 并用 ``validate=False`` 再汇总一遍——红的事实随报告
    一起出去，而不是把分析器打死在异常里。

    ``batches``：只统计这几个 ``batch_id`` 的行（**重跑批的口径**）。账本是
    全阶段的 append-only 单文件，首轮 34 行不会消失；重跑批的「本轮 ≤60」
    必须按批次切窗，否则 34+34 会被读成越帽（STOP-12 假阳）。给 ``batches``
    时 seq 是**全局**的（从 35 起），故连续性按「本窗内相邻差恒为 1」判，
    不按「从 1 开始」判。
    """
    from orchestrator.gateway.quota_ledger import (
        LedgerSequenceError, ledger_summary, read_lines)

    rows = read_lines(Path(ledger_path)) if Path(ledger_path).is_file() else []
    if batches is not None:
        wanted = {str(b) for b in batches}
        rows = [r for r in rows if str(r.get("batch_id") or "") in wanted]
    validation_error = None
    if batches is not None:
        # 分窗视图下全局 seq 必然不从 1 起：不跑「从 1 连续」的严格校验，
        # 用窗内连续性代替（上面的 ``gaps`` 就是那个判据）。
        summary = ledger_summary(rows, validate=False, allow_events=("selfcheck",))
    else:
        try:
            summary = ledger_summary(rows, allow_events=("selfcheck",))
        except LedgerSequenceError as exc:
            validation_error = str(exc)
            summary = ledger_summary(rows, validate=False,
                                     allow_events=("selfcheck",))
    seqs = [int(r["seq"]) for r in rows
            if isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool)]
    seqs.sort()
    gaps = [b for a, b in zip(seqs, seqs[1:]) if b != a + 1]
    return {"summary": summary, "seqs": seqs, "gaps": gaps,
            "validation_error": validation_error,
            "batches": sorted(str(b) for b in batches) if batches else None}


def _seq_contiguous(seqs: Sequence[int], *, start: int = 1) -> bool:
    """seq 从 ``start`` 连续无洞（§11.6 AC-03 / STOP-09）。

    首轮（不切窗）``start=1``；重跑批切窗后 ``start`` 取窗内最小 seq。
    """
    return list(seqs) == list(range(start, start + len(seqs)))


# ---------------------------------------------------------------------------
# §11.5 的预注册规则
# ---------------------------------------------------------------------------

def decide_go_no_go(rows: Sequence[Mapping[str, Any]]) -> "tuple[str, list[str]]":
    """按 §11.5 的四条规则（**顺序执行，先命中先定**）返回 ``(判决, 理由)``。

    规则原文（本函数是它的逐条实现，改这里等于改预注册）：

    1. 6 个 Trial 中 ``BLOCKED_INFRA`` ≥ 2 → ``HOLD``（基础设施不可靠）；
    2. ``birth.status == BORN`` 的 Trial == 0 → ``HOLD``（无人生还）；
    3. 全部 6 个 Trial 触 ``CAP_HIT`` → ``HOLD``（帽长不足，**不得**当场抬帽）；
    4. 其余 → ``GO``。

    前两条与「Trial 缺件」的关系：缺件（``present`` 为 False）既不是 ``BLOCKED_INFRA``
    也不是 ``BORN``，但它会让第 4 条变成「没跑完就 GO」。故缺件**先**判 HOLD——
    这不是新规则，是「6 个 Trial 都必须在场」这条前置（§11.1 第 2 点）的实现。
    """
    reasons: "list[str]" = []
    missing = [str(r.get("trial_id")) for r in rows if not r.get("present")]
    if missing:
        reasons.append(f"缺 Trial 产物：{sorted(missing)}")
        return "HOLD", reasons

    blocked = [str(r.get("trial_id")) for r in rows
               if str(r.get("status")) == "BLOCKED_INFRA"]
    if len(blocked) >= HOLD_BLOCKED_INFRA:
        reasons.append(f"规则1：BLOCKED_INFRA={len(blocked)} ≥ "
                       f"{HOLD_BLOCKED_INFRA}（{sorted(blocked)}）")
        return "HOLD", reasons

    born = [str(r.get("trial_id")) for r in rows
            if str(r.get("birth_status")) == "BORN"]
    if not born:
        reasons.append("规则2：出生 BORN 的 Trial == 0（无人生还 ⇒ 测的是引擎崩溃）")
        return "HOLD", reasons

    capped = [str(r.get("trial_id")) for r in rows
              if str(r.get("status")) == "CAP_HIT"]
    if len(capped) == len(rows):
        reasons.append(f"规则3：全部 {len(rows)} 个 Trial 触 CAP_HIT（帽长不足，"
                       "不得当场抬帽，须新预注册）")
        return "HOLD", reasons

    reasons.append(f"规则4：BLOCKED_INFRA={len(blocked)}<{HOLD_BLOCKED_INFRA}、"
                   f"BORN={len(born)}>0、CAP_HIT={len(capped)}<{len(rows)} ⇒ GO")
    return "GO", reasons


# ---------------------------------------------------------------------------
# 五门（G281-1..5；§12.2 的机判门，G281-6 是用户门不在本表）
# ---------------------------------------------------------------------------

def env_probe() -> "dict[str, Any]":
    """探测容器与显存（G281-5 / STOP-16 的数据源）。

    只在 CLI 显式 ``--probe-env`` 时调用：汇总函数本身保持纯函数，单测不需要
    docker 在不在场——这正是「把流水线通不通变成 docker 在不在」那条老坑。
    """
    import subprocess

    out: "dict[str, Any]" = {}
    # 锁内适配 A7-4：compose 件在 sandbox/，仓库根直接 `docker compose ps` 只会
    # 报 "no configuration file provided" —— 空 stdout 会被读成「容器干净」。
    compose = ["docker", "compose", "-f", str(REPO_ROOT / "sandbox" / "docker-compose.yml")]
    try:
        proc = subprocess.run(compose + ["ps", "-q"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=60,
                              check=False)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        out["containers"] = len(lines)
        out["rc"] = proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        out["containers"] = None
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=60, check=False)
        values = [int(x) for x in proc.stdout.replace(",", " ").split() if x.isdigit()]
        out["gpu_mib"] = max(values) if values else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        out["gpu_mib"] = None
        out.setdefault("error", f"{type(exc).__name__}: {exc}")
    return out


def register_verify(*, lock: "str | Path | None" = None) -> "dict[str, Any]":
    """跑 ``phase281_register.py --verify``（G281-2 的数据源；0 付费、只读）。

    只在 CLI 显式 ``--verify-register`` 时调用——与 :func:`env_probe` 同一条纪律：
    汇总函数保持纯函数，单测不必依赖仓内 LOCK 的当前状态。

    逐条纪律（§12.2：「G281-1..5 是机判门……可复算、不留给判断空间」）：

    * 闸判据是**子进程 rc == 0**（register CLI 的既有裁判：有问题 rc=2 且 stderr
      逐条打印 ``[verify] PROBLEM …``），不看 stdout 自述；
    * 跑不起来（解释器缺失/超时/脚本不在场）一律 ``ok=None`` + ``error``，
      **不**当通过——「没看见」不是「没问题」（同 A7-4 的 compose rc 口径）；
    * ``--lock`` 缺省交给 register 自己（与用户批准 LOCK ``f36695ec`` 同一根）。
    """
    import subprocess

    cmd = [sys.executable, "-I",
           str(REPO_ROOT / "scripts" / "benchmark" / "phase281_register.py"),
           "--verify"]
    if lock is not None:
        cmd += ["--lock", str(lock)]
    out: "dict[str, Any]" = {"cmd": cmd}
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=REGISTER_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        out.update({"ok": None, "rc": None, "problems": [],
                    "error": f"{type(exc).__name__}: {exc}"})
        return out
    problems = [ln.strip() for ln in proc.stderr.splitlines() if "PROBLEM" in ln]
    out.update({"ok": proc.returncode == 0, "rc": proc.returncode,
                "problems": problems,
                "stdout_tail": proc.stdout.strip()[-400:]})
    return out


def five_gates(*, go_no_go: str, stop_open: Sequence[str], ledger: Mapping[str, Any],
               env: "Mapping[str, Any] | None",
               register: "Mapping[str, Any] | None" = None) -> dict:
    """G281-1..5 的快照（``ok`` 为 ``None`` 表示「本次未探测」，不冒充通过）。"""
    calls = int(ledger["summary"].get("call_rows") or 0)
    if register is None:
        g281_2 = {"ok": None, "detail": "未探测（CLI 加 --verify-register 才有值）"}
    elif register.get("ok") is None:
        g281_2 = {"ok": None, "rc": register.get("rc"),
                  "detail": f"跑不起来：{register.get('error')}"}
    else:
        g281_2 = {"ok": register.get("ok") is True,
                  "rc": register.get("rc"),
                  "problems": list(register.get("problems") or []),
                  "detail": f"register --verify rc={register.get('rc')}"}
    # 首轮不切窗时 seq 从 1 起；重跑批切窗（``--batches``）后从窗内首号起。
    seqs = list(ledger["seqs"])
    start = seqs[0] if seqs else 1
    contiguous = _seq_contiguous(seqs, start=start)
    gates: "dict[str, Any]" = {
        "g281_1": {"ok": go_no_go == "GO", "detail": f"go_no_go={go_no_go}"},
        "g281_2": g281_2,
        "g281_3": {"ok": calls <= PILOT_CALL_CAP and contiguous,
                   "detail": f"calls={calls}<= {PILOT_CALL_CAP}, "
                             f"seq_contiguous={contiguous} (from seq={start}), "
                             f"batches={ledger.get('batches')}"},
        "g281_4": {"ok": list(stop_open) == [], "detail": f"stop_open={list(stop_open)}"},
    }
    if env is None:
        gates["g281_5"] = {"ok": None, "detail": "未探测（CLI 加 --probe-env 才有值）"}
    else:
        containers = env.get("containers")
        gpu = env.get("gpu_mib")
        # rc != 0（compose 件缺失/daemon 不在）时「0 个容器」不是干净，是没看见。
        clean = env.get("rc") == 0
        ok = (clean and containers == 0 and isinstance(gpu, int)
              and gpu <= GPU_LIMIT_MIB)
        gates["g281_5"] = {"ok": ok,
                           "detail": f"containers={containers}, gpu_mib={gpu}, "
                                     f"compose_rc={env.get('rc')}"}
    return gates


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pilot_report(root: Path, *, rep: int = PILOT_REP,
                       env: "Mapping[str, Any] | None" = None,
                       register: "Mapping[str, Any] | None" = None,
                       now: "str | None" = None,
                       batches: "Sequence[str] | None" = None) -> dict:
    """产出 ``pilot_report.json`` 的完整文档（字段集**恰为** :data:`REPORT_FIELDS`）。

    ``batches``：把账本口径切到本轮批次（重跑批用）。缺省 ``None`` = 全账本
    （首轮口径，行为与 P281-7 逐位相同）。
    """
    from orchestrator.benchmark.phase281 import paths

    root = Path(root)
    rows = collect_trials(root, rep=rep)
    ledger_path = (Path(root) / "quota" / "ledger.jsonl")
    if not ledger_path.is_file():
        ledger_path = paths.ledger_path()
    ledger = ledger_snapshot(ledger_path, batches=batches)
    go_no_go, reasons = decide_go_no_go(rows)

    stop_open: "list[str]" = []
    scope = (f"（口径：批次 {ledger['batches']}）" if ledger.get("batches") else "")
    if ledger["gaps"] or ledger.get("validation_error"):
        stop_open.append("STOP-09")
        reasons.append(f"seq 断号：gaps={ledger['gaps']}{scope}"
                       + (f"；机判：{ledger['validation_error']}"
                          if ledger.get("validation_error") else ""))
    calls = int(ledger["summary"].get("call_rows") or 0)
    if calls > PILOT_CALL_CAP:
        stop_open.append("STOP-12")
        reasons.append(f"实呼 {calls} 越过 Pilot 上限 {PILOT_CALL_CAP}（STOP-12）{scope}")
    if env is not None:
        containers = env.get("containers")
        gpu = env.get("gpu_mib")
        if (isinstance(containers, int) and containers > 0) or \
                (isinstance(gpu, int) and gpu > GPU_LIMIT_MIB):
            stop_open.append("STOP-16")
            reasons.append(f"收尾不干净：containers={containers}, gpu_mib={gpu}")

    non_terminal = [str(r["trial_id"]) for r in rows
                    if r.get("present") and str(r.get("status")) not in TERMINAL_STATUSES]
    if non_terminal:
        reasons.append(f"非终态 Trial：{sorted(non_terminal)}")

    report = {
        "schema": PILOT_REPORT_SCHEMA,
        "generated_at": now or _now_iso(),
        "per_trial": rows,
        "verdicts": {str(r["trial_id"]): r.get("verdict") for r in rows},
        "go_no_go": go_no_go,
        "reasons": reasons,
        "stop_open": stop_open,
        "five_gates": five_gates(go_no_go=go_no_go, stop_open=stop_open,
                                 ledger=ledger, env=env, register=register),
    }
    assert frozenset(report) == REPORT_FIELDS, sorted(report)
    return report


def write_report(report: Mapping[str, Any], out: Path) -> Path:
    """原子写 0600（0700 目录）——与仓内其余产物同一条落盘纪律。"""
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

    target = Path(out)
    ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n", mode=0o600)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_analysis",
        description="Phase 2.8.1 分析（pilot：go/no-go 机判；0 付费）")
    ap.add_argument("--stage", choices=("pilot",), required=True,
                    help="分析阶段（本 Task 只有 pilot）")
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--root", default=None, help="数据根（缺省 paths.phase281_root()）")
    ap.add_argument("--rep", type=int, default=PILOT_REP)
    ap.add_argument("--probe-env", action="store_true",
                    help="同时探测容器/显存（G281-5 / STOP-16 的数据源）")
    ap.add_argument("--verify-register", action="store_true",
                    help="同时跑 phase281_register.py --verify（G281-2 的数据源）")
    ap.add_argument("--lock", default=None,
                    help="--verify-register 的 LOCK 路径（缺省交给 register 自己）")
    ap.add_argument("--batches", default=None,
                    help="逗号分隔的 batch_id 白名单：把账本口径切到本轮批次"
                         "（重跑批用，如 P281-B1b-1,P281-B1b-2,P281-B1b-3）。"
                         "缺省 = 全账本（首轮口径）")
    return ap


def _parse_batches(value: "str | None") -> "list[str] | None":
    """``--batches`` 解析；空串/None ⇒ ``None``（全账本口径，不是空窗）。"""
    if not value:
        return None
    items = [x.strip() for x in str(value).split(",")]
    wanted = [x for x in items if x]
    return wanted or None


def main(argv: "Iterable[str] | None" = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    from orchestrator.benchmark.phase281 import paths

    root = Path(args.root) if args.root else paths.phase281_root()
    env = env_probe() if args.probe_env else None
    register = (register_verify(lock=args.lock) if args.verify_register else None)
    batches = _parse_batches(args.batches)
    report = build_pilot_report(root, rep=int(args.rep), env=env, register=register,
                                batches=batches)
    target = write_report(report, Path(args.out))
    print(json.dumps({"out": str(target), "go_no_go": report["go_no_go"],
                      "reasons": report["reasons"],
                      "stop_open": report["stop_open"],
                      "batches": batches},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["go_no_go"] in ("GO", "HOLD") else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
