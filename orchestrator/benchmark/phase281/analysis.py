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
#: §11.9「付费记账证明」伴生件的 schema（M2：与 8 字段契约**分开**成文）
ACCOUNTING_SCHEMA = "simpraxis.phase281.pilot_accounting.v1"
#: Pilot 只跑一个重复（§11.1：3 任务 × 2 臂 × 1 重复 = 6）
PILOT_REP = 0
#: §11.3：三票各 20，合计 60 = 6 Trial × 10
PILOT_CALL_CAP = 60
#: §11.5：``BLOCKED_INFRA`` 达到这个数即 HOLD
HOLD_BLOCKED_INFRA = 2
#: Trial 终态集合。**L6**：这是 ``trial_harness.STATUSES`` 的镜像而不是导入——
#: 导入会把 harness 的整条重依赖链（lancedb/引擎）拉进这个纯汇总模块。代价是
#: 可能漂移，故由 ``test_ac_p281_7_04_terminal_statuses_single_source`` 逐字断言
#: 两者相等（漂移即红，不再靠注释自觉）。
TERMINAL_STATUSES = ("FINISHED", "CAP_HIT", "BLOCKED_INFRA", "CRASHED")
#: 探针判据：显存上限（硬规则 5）
GPU_LIMIT_MIB = 200
#: G281-2 子进程上限（register --verify 只做本地文件哈希，正常 < 5s）
REGISTER_TIMEOUT_S = 120

TRIAL_JSON = "trial.json"


class LedgerMissingError(RuntimeError):
    """数据根里没有 ``quota/ledger.jsonl``（**H4**：绝不回落到生产账）。

    旧行为是「本根缺账 ⇒ 改读 ``paths.ledger_path()``」。后果不是崩溃而是**假合规**：
    一个空的 tmp 根实测返回 ``calls=34``（生产账），G281-3 可以是 ``true``，而报告里
    的呼数与这份根下的 Trial 毫无关系；合成腿也因此**不 hermetic**（读真钱数据）。
    与仓内 fail-closed 纪律相反，故缺件即抛。
    """


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


def _engine_fields(trial: Mapping[str, Any]) -> "tuple[str | None, str | None]":
    """读 ``trial.json.engine`` 的 ``(status, reason)``（**H3**）。

    为什么必须读：引擎的 token/round 帽走 ``agentic/wiring.py`` 的**返回字典**
    （``{"status": "failed", "reason": "[agentic:token_cap] …"}``），不进
    ``loop.py`` 的 ``CapExceeded`` 异常路径，于是 ``trial_harness`` 的「无异常 ⇒
    FINISHED」把触帽抹平成 ``FINISHED`` + ``problems=[]``。P281-7 首轮的三份
    agentic 正是这个形态（token_cap ×2、round_cap ×1），而报告里一个 engine
    字段都没有——**判决「规则 3 未命中」因此看不见真实帽因**。

    这里只做**可观测性**：把事实搬到 ``per_trial`` 上，**不**改 §11.5 的任何规则。
    「引擎级帽是否等价 ``CAP_HIT``」是改预注册的问题，须用户裁决（登记在案）。
    """
    engine = trial.get("engine")
    if not isinstance(engine, Mapping):
        return None, None
    status = engine.get("status")
    reason = engine.get("reason")
    return (str(status) if status is not None else None,
            str(reason) if reason is not None else None)


def collect_trials(root: Path, *, rep: int = PILOT_REP) -> "list[dict[str, Any]]":
    """把 3 任务 × 2 臂的 Trial 记录读成一张表（缺件也占一行，不静默丢）。"""
    rows: "list[dict[str, Any]]" = []
    for task_id in constants.TASK_IDS:
        for arm_id in constants.ARMS:
            path = trial_dir(root, task_id, arm_id, rep)
            trial = read_json(path / TRIAL_JSON)
            eval_path = evaluator_result_path(root, task_id, arm_id, rep)
            evaluation = read_json(eval_path)
            engine_status, engine_reason = (_engine_fields(trial) if trial
                                            else (None, None))
            rows.append({
                "task_id": task_id,
                "arm_id": arm_id,
                "rep": int(rep),
                "trial_id": constants.trial_id(task_id, arm_id, rep),
                "trial_dir": str(path),
                "present": trial is not None,
                "status": (str(trial.get("status") or "") if trial else None),
                "birth_status": (_birth_status(trial) if trial else None),
                "engine_status": engine_status,
                "engine_reason": engine_reason,
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

    # **M1（评审修）**：非终态 Trial 与「缺件」是同一个前置的两种形态——§11.1 的
    # 「6 个 Trial 都必须在场」不可能只要求文件存在、却允许它还在跑。旧实现只把
    # 非终态追加进 reasons（:363-366），判决与 G281-1 都不动 ⇒ 存在但 RUNNING 的
    # Trial 可以报 GO。这里并入前置：非终态 ⇒ HOLD。
    non_terminal = [str(r.get("trial_id")) for r in rows
                    if str(r.get("status")) not in TERMINAL_STATUSES]
    if non_terminal:
        reasons.append(f"非终态 Trial：{sorted(non_terminal)}（6 个 Trial 必须都到终态"
                       "才谈得上判决：还在跑的 Trial 没有判决可读）")
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
    # L5：旧实现按「行内含字面量 PROBLEM」收，且 rc!=0 时丢掉输出尾。两个坑：
    # (a) ``[consent] PROBLEM …`` 也是 PROBLEM，会被当成 verify 的问题；
    # (b) register 换个措辞失败 ⇒ ``problems=[]``，「红了」被记成「没发现问题」。
    # 现在只认 ``[verify] PROBLEM`` 前缀；rc!=0 却一条都没匹配上时，把 stderr 尾巴
    # 无条件当问题带出去（宁可贵一点，不许把红说成绿）。
    lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
    problems = [ln for ln in lines if ln.startswith("[verify] PROBLEM")]
    if proc.returncode != 0 and not problems:
        problems = lines[-5:] or [
            f"register --verify rc={proc.returncode} 且无 stderr 输出（原因未知）"]
    out.update({"ok": proc.returncode == 0, "rc": proc.returncode,
                "problems": problems,
                "stdout_tail": proc.stdout.strip()[-400:],
                "stderr_tail": proc.stderr.strip()[-400:]})
    return out


def five_gates(*, go_no_go: str, stop_open: Sequence[str], ledger: Mapping[str, Any],
               env: "Mapping[str, Any] | None",
               register: "Mapping[str, Any] | None" = None,
               trial_calls: "int | None" = None) -> dict:
    """G281-1..5 的快照（``ok`` 为 ``None`` 表示「本次未探测」，不冒充通过）。"""
    calls = int(ledger["summary"].get("call_rows") or 0)
    batch_window = ledger.get("batches") or None
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
    # A7-10：切窗后必须对账「窗内实呼行数 vs 各 Trial 自报观测呼数」。
    # 没有这条，「票号写错 ⇒ 窗内 0 行 ⇒ calls=0 <= 60 ⇒ G281-3 绿」就是一次
    # 干净的**假阳**：2026-11-07 重跑批正是如此（票 P281-B1b-* 零引用，三个
    # agentic Trial 一次网没发，而它们的 trial.json 却把首轮 10 呼/111529
    # tokens 报成本轮观测值，G281-3 报 ok=true）。
    #
    # 两条判据（不要求逐 Trial 精确相等——历史轮次的行可能落在别的票名下，
    # 那是既成事实，不能靠改判据去「凑平」）：
    #   ①**空虚**：有 Trial 自报花了钱，窗里却 0 行 ⇒ 票号没贯通/窗口选错；
    #   ②**超报**：窗里的行数比所有 Trial 自报之和还多 ⇒ 窗里混进了不属于
    #     本轮的行。
    vacuous = (batch_window is not None and trial_calls is not None
               and int(trial_calls) > 0 and calls == 0)
    overclaim = (batch_window is not None and trial_calls is not None
                 and calls > int(trial_calls))
    reconciled = None if (batch_window is None or trial_calls is None) \
        else (not vacuous and not overclaim)
    g281_3_ok = calls <= PILOT_CALL_CAP and contiguous and reconciled is not False
    gates: "dict[str, Any]" = {
        "g281_1": {"ok": go_no_go == "GO", "detail": f"go_no_go={go_no_go}"},
        "g281_2": g281_2,
        "g281_3": {"ok": g281_3_ok,
                   "detail": f"calls={calls}<= {PILOT_CALL_CAP}, "
                             f"seq_contiguous={contiguous} (from seq={start}), "
                             f"batches={batch_window}"
                             + ("" if reconciled is None else
                                f", trial_calls={trial_calls}, "
                                f"reconciled={reconciled}")},
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
    ledger_path = Path(root) / "quota" / "ledger.jsonl"
    if not ledger_path.is_file():
        # H4：fail-closed。不回落读生产账——那会把「本根没跑」写成「跑了 N 呼」。
        raise LedgerMissingError(
            f"数据根缺账本：{ledger_path}（拒绝回落到 {paths.ledger_path()}："
            "报告里的呼数必须与同一根下的 Trial 同源）")
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

    # 非终态已由 ``decide_go_no_go`` 的前置判掉（M1）；这里不再重复追加 reasons。
    report = {
        "schema": PILOT_REPORT_SCHEMA,
        "generated_at": now or _now_iso(),
        "per_trial": rows,
        "verdicts": {str(r["trial_id"]): r.get("verdict") for r in rows},
        "go_no_go": go_no_go,
        "reasons": reasons,
        "stop_open": stop_open,
        "five_gates": five_gates(
            go_no_go=go_no_go, stop_open=stop_open, ledger=ledger, env=env,
            register=register,
            trial_calls=sum(int(r.get("observed_calls") or 0) for r in rows)),
    }
    # L1：``assert`` 在 ``python -O`` 下被整条剥离，字段集契约会静默失效。
    if frozenset(report) != REPORT_FIELDS:
        raise RuntimeError(f"pilot_report 字段集漂移：多/少 "
                           f"{sorted(frozenset(report) ^ REPORT_FIELDS)}")
    return report


def build_accounting(root: Path, *, rep: int = PILOT_REP,
                     batches: "Sequence[str] | None" = None,
                     now: "str | None" = None) -> dict:
    """§11.9「付费记账证明」的**伴生件**（M2，2026-11-07 评审）。

    计划 §11.9 要求报告里逐字给出「实呼数、ok/fail 拆分、by_task、by_stage、
    seq 区间、每 Trial 的 seq 列表」；而 :data:`REPORT_FIELDS` 把
    ``pilot_report.json`` 钉死在 8 个字段上（§11.6 的契约，改它就是改预注册）。
    两者不冲突的做法是**另写一件**：契约文件不动，记账证明单独成文。

    口径与 :func:`ledger_snapshot` 完全一致（同一个 ``batches`` 切窗），差别只在
    粒度：这里把每一行落到它的 ``thread_id``（= ``trial_id``）上，好让读者逐 Trial
    核 seq，而不是只看总数。窗内不属于任何预注册 Trial 的行走 ``unattributed``
    **如实挂牌**——不静默丢，也不摊到某个 Trial 头上。

    顶层的 ``calls/by_task/...`` 是**窗内**视图（与 ``pilot_report`` 同口径，
    用来回答「本轮是否越帽」）；``full_ledger`` 是**整本**视图（回答 §11.9
    「Pilot 至今实呼多少」）。两者都在，不靠读者自己加。
    """
    from orchestrator.gateway.quota_ledger import ledger_summary, read_lines

    root = Path(root)
    ledger_path = root / "quota" / "ledger.jsonl"
    if not ledger_path.is_file():
        raise LedgerMissingError(
            f"数据根缺账本：{ledger_path}（记账证明必须与 Trial 同源）")
    all_rows = read_lines(ledger_path)
    rows = all_rows
    if batches is not None:
        wanted = {str(b) for b in batches}
        rows = [r for r in all_rows if str(r.get("batch_id") or "") in wanted]
    summary = ledger_summary(rows, validate=False, allow_events=("selfcheck",))
    # 全账本视图：切窗只回答「本轮花了多少」，§11.9 要的是**整本**付费证明。
    # 两栏并陈，读者不必自己把几轮加起来（加错就是把 63 读成 25）。
    full = ledger_summary(all_rows, validate=False, allow_events=("selfcheck",))
    seqs = sorted(int(r["seq"]) for r in rows
                  if isinstance(r.get("seq"), int)
                  and not isinstance(r.get("seq"), bool))
    gaps = [b for a, b in zip(seqs, seqs[1:]) if b != a + 1]

    known = {constants.trial_id(t, a, rep) for t in constants.TASK_IDS
             for a in constants.ARMS}
    per_trial: "dict[str, dict[str, Any]]" = {
        tid: {"calls": 0, "ok": 0, "fail": 0, "seqs": [],
              "tokens_in": 0, "tokens_out": 0}
        for tid in sorted(known)}
    unattributed: "list[dict[str, Any]]" = []
    for r in rows:
        if r.get("event") != "call" or not isinstance(r.get("seq"), int):
            continue
        tid = str(r.get("thread_id") or "")
        cell = per_trial.get(tid)
        if cell is None:
            unattributed.append({"seq": r.get("seq"), "thread_id": tid or None,
                                 "batch_id": r.get("batch_id")})
            continue
        cell["calls"] += 1
        cell["ok" if r.get("ok") is True else "fail"] += 1
        cell["seqs"].append(int(r["seq"]))
        cell["tokens_in"] += int(r.get("tokens_in") or 0)
        cell["tokens_out"] += int(r.get("tokens_out") or 0)
    for cell in per_trial.values():
        cell["seqs"].sort()

    return {
        "schema": ACCOUNTING_SCHEMA,
        "generated_at": now or _now_iso(),
        "ledger": str(ledger_path),
        "batches": sorted(str(b) for b in batches) if batches else None,
        "rep": int(rep),
        "calls": int(summary.get("call_rows") or 0),
        "calls_ok": summary.get("calls_ok"),
        "by_task": summary.get("calls_by_task"),
        "by_stage": summary.get("calls_by_stage"),
        "blocked_rows": summary.get("blocked_rows"),
        "selfcheck_rows": summary.get("selfcheck_rows"),
        "seq_first": summary.get("seq_first"),
        "seq_last": summary.get("seq_last"),
        "seqs": seqs,
        "gaps": gaps,
        "contiguous": not gaps,
        "per_trial": per_trial,
        "unattributed": unattributed,
        # §11.9 的整本口径：Pilot 至今全部实呼（含首轮 34 行），不受 --batches 影响
        "full_ledger": {
            "calls": int(full.get("call_rows") or 0),
            "calls_ok": full.get("calls_ok"),
            "by_task": full.get("calls_by_task"),
            "by_stage": full.get("calls_by_stage"),
            "seq_first": full.get("seq_first"),
            "seq_last": full.get("seq_last"),
            "batches": full.get("batches"),
        },
        "tokens_total": {
            "in": sum(c["tokens_in"] for c in per_trial.values())
                  + sum(int(r.get("tokens_in") or 0) for r in rows
                        if r.get("event") == "call"
                        and str(r.get("thread_id") or "") not in known),
            "out": sum(c["tokens_out"] for c in per_trial.values())
                   + sum(int(r.get("tokens_out") or 0) for r in rows
                         if r.get("event") == "call"
                         and str(r.get("thread_id") or "") not in known),
        },
    }


def write_report(report: Mapping[str, Any], out: Path) -> Path:
    """原子写 0600（0700 目录）——与仓内其余产物同一条落盘纪律。"""
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

    target = Path(out)
    ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n", mode=0o600)
    return target


# ---------------------------------------------------------------------------
# P281-9：终局分析（18 个预注册 Trial → H1–H5 三态判决）
# ---------------------------------------------------------------------------

FINAL_REPORT_SCHEMA = "simpraxis.phase281.final_report.v1"
#: 确认性阶段的重复编号（§13.3.2：rep 1、rep 2 才进判决；rep 0 是 Pilot）
CONFIRMATORY_REPS = tuple(range(constants.REPS_PILOT, constants.REPS_TOTAL))
#: 预注册 Trial 总数（3 任务 × 2 臂 × 3 重复 = 18）——**不由实际件数推出**
PRE_REGISTERED_TRIALS = (len(constants.TASK_IDS) * len(constants.ARMS)
                         * constants.REPS_TOTAL)

#: 语义车道（用户 2026-11-07 裁决：开启；复核人 BigG / BigC）
SEMANTIC_LANE = "enabled"
SEMANTIC_LANE_REVIEWERS = ("BigG", "BigC")

#: 终局报告字段集（AC-P281-9-01/02 的契约；多一个少一个都算漂移）
FINAL_REPORT_FIELDS = frozenset({
    "schema", "experiment_id", "phase", "go_no_go", "go_no_go_reasons",
    "pre_registered_trials", "trial_counts", "trials", "hypotheses",
    "semantic_lane", "semantic_lane_reviewers", "semantic_scores",
    "ledger", "tables_baseline", "limitations", "disclaimer", "sources",
})


def _parse_ts(value: Any) -> "datetime | None":
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _wallclock_s(trial: Mapping[str, Any]) -> "int | None":
    start, end = _parse_ts(trial.get("created_at")), _parse_ts(trial.get("finished_at"))
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _tokens(trial: Mapping[str, Any]) -> "tuple[int, int]":
    observed = trial.get("observed")
    if not isinstance(observed, Mapping):
        return 0, 0
    return (int(observed.get("tokens_in") or 0), int(observed.get("tokens_out") or 0))


def collect_phase_trials(root: Path) -> "list[dict[str, Any]]":
    """18 个预注册 Trial 全表（缺件的也占一行，**不静默丢**）。

    每行是 §13.3.1 的五元组＋定位字段。``birth_level`` 是**投影**：
    harness 的出生闸只记二值（``BORN`` / ``NOT_BORN``，见
    ``trial_harness`` 步骤④），所以 ``BORN`` ⇒ ``B4``（闸门要求跑通到底层），
    ``NOT_BORN`` ⇒ ``None``（具体停在哪一层由 ``birth.reason`` 另行给出，
    不在本表臆造）。§13.3.1 只用它区分「跑起来了但没达标」与「任务没跑起来」，
    这一投影足以回答，且不会把没记录的信息编出来。
    """
    rows: "list[dict[str, Any]]" = []
    for rep in range(constants.REPS_TOTAL):
        for row in collect_trials(Path(root), rep=rep):
            rows.append(_five_tuple(Path(root), row))
    return rows


def _five_tuple(root: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    from orchestrator.benchmark.phase281 import verdict as v

    trial_json = read_json(Path(str(row["trial_dir"])) / TRIAL_JSON) or {}
    birth_status = row.get("birth_status")
    birth_level = ("B4" if str(birth_status or "") == "BORN" else None)
    trial_verdict = str(row.get("verdict") or "")
    present = bool(row.get("present"))
    observed = trial_json.get("observed") if isinstance(
        trial_json.get("observed"), Mapping) else {}
    tokens_in, tokens_out = _tokens(trial_json)
    return {
        "trial_id": row["trial_id"],
        "task_id": row["task_id"],
        "arm_id": row["arm_id"],
        "rep": int(row["rep"]),
        "phase": "pilot" if int(row["rep"]) == PILOT_REP else "confirmatory",
        "present": present,
        "status": row.get("status"),
        "birth_status": birth_status,
        "birth_level": birth_level,
        "first_birth_round": v.first_birth_round(
            birth_status, observed.get("first_birth_round") if observed else None),
        "verdict": trial_verdict if present else None,
        "failure_class": (v.classify_failure(trial_verdict, birth_level)
                          if present and trial_verdict in v.TRIAL_VERDICTS else None),
        "used_calls": row.get("used_calls"),
        "observed_calls": row.get("observed_calls"),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wallclock_s": _wallclock_s(trial_json) if present else None,
        "trial_dir": row["trial_dir"],
        "evaluator_path": row["evaluator_path"],
    }


def _ledger_tokens_by_trial(root: Path) -> "dict[str, dict[str, int]]":
    """按 ``thread_id``（= ``trial_id``）汇总账本 tokens（H5 的唯一口径）。

    读整本账、只取 ``event == "call"`` 且 ``seq`` 为整数的行；``selfcheck``
    开账行不计。缺账本按 0 处理（终局分析不是记账证明，缺账由 judge 判红）。
    """
    out: "dict[str, dict[str, int]]" = {}
    path = Path(root) / "quota" / "ledger.jsonl"
    if not path.is_file():
        return out
    from orchestrator.gateway.quota_ledger import read_lines

    for row in read_lines(path):
        if row.get("event") != "call" or not isinstance(row.get("seq"), int):
            continue
        tid = str(row.get("thread_id") or "")
        cell = out.setdefault(tid, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        cell["calls"] += 1
        cell["tokens_in"] += int(row.get("tokens_in") or 0)
        cell["tokens_out"] += int(row.get("tokens_out") or 0)
    return out


def _counts(rows: "Sequence[Mapping[str, Any]]", task_id: str, arm_id: str) -> int:
    return sum(1 for r in rows
               if r["task_id"] == task_id and r["arm_id"] == arm_id
               and r.get("verdict") == "PASS")


def _rounds(rows: "Sequence[Mapping[str, Any]]", task_id: str | None,
            arm_id: str) -> "list[int | None]":
    return [r.get("first_birth_round") for r in rows
            if r["arm_id"] == arm_id and (task_id is None or r["task_id"] == task_id)]


def _token_median(rows: "Sequence[Mapping[str, Any]]", arm_id: str,
                  ledger_tokens: Mapping[str, Mapping[str, int]]) -> float:
    values = []
    for r in rows:
        if r["arm_id"] != arm_id:
            continue
        cell = ledger_tokens.get(str(r["trial_id"]))
        if cell is None:
            continue
        values.append(float(cell["tokens_in"] + cell["tokens_out"]))
    if not values:
        return 0.0
    values.sort()
    mid = (len(values) - 1) // 2
    return values[mid]


def evaluate_hypotheses(rows: "Sequence[Mapping[str, Any]]",
                        ledger_tokens: Mapping[str, Mapping[str, int]],
                        *, semantic_lane: str = SEMANTIC_LANE,
                        semantic_present: bool = False) -> "dict[str, dict]":
    """§13.3.2 的机械判决：**只吃确认性 12 个 Trial**（Pilot rep0 不算）。

    数据不全时的诚实处理（**不是**给结论找台阶）：

    * 一个确认性 Trial 都没有 ⇒ 五个假设全判 ``INCONCLUSIVE`` +
      ``reason_code="CONFIRMATORY_NOT_RUN"``——0 个样本没有任何方向，
      与「n=12 打平」必须区分开，否则读者会把「没跑」读成「跑了说不清」；
    * 有但不齐（1..11） ⇒ ``INCONCLUSIVE`` + ``CONFIRMATORY_INCOMPLETE``。

    只有 12 个全在场，才逐假设调用 :mod:`verdict` 的纯函数。
    """
    confirmatory = [r for r in rows if r.get("phase") == "confirmatory" and r["present"]]
    expected = (len(constants.TASK_IDS) * len(constants.ARMS)
                * constants.REPS_CONFIRMATORY)
    if len(confirmatory) != expected:
        code = ("CONFIRMATORY_NOT_RUN" if not confirmatory
                else "CONFIRMATORY_INCOMPLETE")
        reason = (f"确认性阶段未启动：{expected} 个预注册 Trial 中在场 "
                  f"{len(confirmatory)} 个（Pilot 判决 HOLD，§11.5 规则 2 封存）")
        return {h: verdict_absent(h, reason=reason, code=code)
                for h in ("H1", "H2", "H3", "H4", "H5")}

    from orchestrator.benchmark.phase281 import verdict as v

    short = {t["task_id"]: t["short"] for t in constants.TASK_ORDER}
    by_short = {s: tid for tid, s in short.items()}

    h1 = v.decide_h1(_counts(confirmatory, by_short["L1"], "agentic_v1"),
                     _counts(confirmatory, by_short["L1"], "legacy_v1"))
    h2 = v.decide_h2(v.median_rounds(_rounds(confirmatory, None, "agentic_v1")),
                     v.median_rounds(_rounds(confirmatory, None, "legacy_v1")))
    def _d(short_name: str) -> int:
        tid = by_short[short_name]
        return (_counts(confirmatory, tid, "agentic_v1")
                - _counts(confirmatory, tid, "legacy_v1"))
    h3 = v.decide_h3(_d("M1"), _d("L1"))
    # R1 的结构分：评测器目前只给 verdict，没有连续分；用 PASS=1/其它=0 的
    # 指示函数代替会把「结构分」偷换成「通过率」——故这里如实记**结构分缺席**
    # （structure_present=False），H4 因此一律落 INCONCLUSIVE。
    # 锁内适配：旧写法把缺分写成 0.0 vs 0.0，在 decide_h4 里会变成「不劣」从而
    # 骗出 CAPABILITY_DEMONSTRATED；缺分与 0 分不是一回事。
    h4 = v.decide_h4(structure_worse_reps=(), structure_agentic_median=0.0,
                     structure_legacy_median=0.0, structure_present=False,
                     semantic_lane=semantic_lane,
                     semantic_present=bool(semantic_present))
    h5 = v.decide_h5(_token_median(confirmatory, "agentic_v1", ledger_tokens),
                     _token_median(confirmatory, "legacy_v1", ledger_tokens))
    return {h["id"]: h for h in (h1, h2, h3, h4, h5)}


def verdict_absent(hypothesis_id: str, *, reason: str, code: str) -> dict:
    """确认性未发生时的统一判决件（唯一出口，避免两处写法分叉）。"""
    from orchestrator.benchmark.phase281 import verdict as v

    if code not in ("CONFIRMATORY_NOT_RUN", "CONFIRMATORY_INCOMPLETE"):
        raise ValueError(f"未知的确认性缺席码：{code!r}")
    doc = v.confirmatory_absent(hypothesis_id, reason=reason)
    doc["reason_code"] = code
    return doc


#: 用户 2026-11-07 裁决「一：选 A」的逐字登记（引擎帽 ⇄ ``CAP_HIT`` 缺口）
ENGINE_CAP_GAP_LIMITATION = (
    "引擎帽与 CAP_HIT 的缺口（用户 2026-11-07 裁决选 A：维持现状，不改预注册）："
    "计划 §11.5 规则 3 只认 status == \"CAP_HIT\"。本轮三个 agentic Trial 的引擎自述"
    "终态均为 FINISHED（problems 为空），真实触帽事实只在 engine.reason 里"
    "（[agentic:round_cap] ×2、[agentic:token_cap] ×1）。按用户裁决，规则 3 仍只认"
    "字面 CAP_HIT，该缺口在本报告中如实登记，不改 §11.5、不改预注册，留待"
    "P281-9 之外的后续裁决。"
)


def _limitations(rows: "Sequence[Mapping[str, Any]]", *, go_no_go: str,
                 go_reasons: "Sequence[str]", ledger: Mapping[str, Any],
                 semantic_lane: str = SEMANTIC_LANE,
                 semantic_lane_reviewers: "Sequence[str]" = SEMANTIC_LANE_REVIEWERS,
                 semantic_present: bool = False) -> "list[str]":
    present = [r for r in rows if r["present"]]
    not_run = [r["trial_id"] for r in rows if not r["present"]]
    confirmatory_present = sum(1 for r in rows
                               if r.get("phase") == "confirmatory" and r["present"])
    crashed = [r["trial_id"] for r in present if r.get("status") == "CRASHED"]
    inval = [r["trial_id"] for r in present if r.get("verdict") == "INVALID_ARTIFACT"]
    if semantic_lane == "disabled":
        # AC-P281-9-02 的 disabled 分支要求这一句**逐字**在场：
        # 「R1 仅得结构性结论」。语义车道不开，R1 的语义维就没有数据面。
        semantic_line = ("语义车道：用户裁决**不开启**（semantic_lane=disabled）。"
                         "因此 R1 仅得结构性结论，语义维度不做任何主张；"
                         "H4 在语义车道关闭时不得给强结论（§13.3.2）。")
    else:
        semantic_line = ("语义车道：按用户裁决开启，复核人 "
                         + "、".join(semantic_lane_reviewers)
                         + ("。" if semantic_present else
                            "。但确认性阶段未启动，R1 没有可供盲评的输出，"
                            "故**无任何盲评分**；本报告不产出语义维度的结论。"))
    items = [
        ENGINE_CAP_GAP_LIMITATION,
        ((f"确认性阶段（12 个预注册 Trial：rep1/rep2）**未启动**。Pilot 机判为 "
          f"{go_no_go}（理由：{'；'.join(go_reasons) or '无'}），按计划 §11.5 的预注册"
          f"规则，确认性阶段不得在 HOLD 下开跑；未跑的 Trial 原样登记为 not_run，"
          f"不补造、不外推。合计 {len(present)}/18 个 Trial 有数据。")
         if not confirmatory_present else
         (f"确认性阶段在场 {confirmatory_present}/12 个预注册 Trial；"
          f"Pilot 机判为 {go_no_go}。判决只吃确认性 12 个，Pilot rep0 不进判决。")),
        ((f"样本量：每格只有 Pilot 的 1 个重复有数据（n=1），确认性 2 个重复缺席，"
          f"因此本报告不产出任何假设的成立或推翻结论，只登记事实与限制。")
         if not confirmatory_present else
         (f"样本量：每格 n={constants.REPS_TOTAL}（Pilot 1 + 确认性 2），"
          f"不足以做统计检验；本报告只给效应量估计与可行性证据。")),
        ("引擎侧失败（供货方空回答）："
         + (f"{len(crashed)} 个 Trial 以 CRASHED 收尾（{', '.join(crashed)}），"
            "原因是 coding_debug 阶段连续收到 HTTP 200 空回答（EmptyResponseError）。"
            if crashed else "本轮无 CRASHED 收尾的 Trial。")
         + " 硬规则 1：未改模型、未换端点。"),
        (f"INVALID_ARTIFACT 的 Trial {len(inval)} 个（{', '.join(inval)}）："
         "引擎侧未出生（NOT_BORN）或自述与观测不符，评测器按预注册判为无效产物；"
         "这类结果既不能读成「能力不足」，也不能读成「能力达标」。"),
        ("max_attempts 偏差（P281-7 首轮）：计划 §11.3 钉 1，实际代码使用网关缺省 5，"
         "故账本里的呼数是**逻辑调用**数，供应侧 HTTP 次数更多。该偏差已在后续批次"
         "修复（显式 max_attempts=1），本轮已发生的部分如实登记、不追补。"),
        (f"账本合计 {int(ledger.get('calls') or 0)} 次实呼（含 Pilot 三轮尝试），"
         f"硬帽 {constants.HARD_CAP} 未触；确认性阶段的 "
         f"{constants.CONFIRMATORY_BUDGET} 呼预算未被使用。"),
        semantic_line,
        ("出生等级是投影而非实测：harness 的出生闸只记录 BORN / NOT_BORN 二值，"
         "本报告据 BORN ⇒ B4、NOT_BORN ⇒ 未达 B4 投影；具体停在哪一层由 "
         "trial.json 的 birth.reason 给出，本报告不臆造层级。"),
    ]
    return items


def build_final_report(root: Path, *,
                       semantic_lane: str = SEMANTIC_LANE,
                       semantic_lane_reviewers: "Sequence[str]" = SEMANTIC_LANE_REVIEWERS,
                       semantic_present: bool = False) -> dict:
    """产出 ``final_report.json``（18 Trial 全表 + H1–H5 + 限制）。

    **确定性**：本函数不写任何钟表时间、不读环境变量、遍历顺序全部由
    ``constants`` 与 ``sorted`` 决定 ⇒ 同一输入重跑逐字节相同（AC-P281-9-03）。
    来源以 sha256 记录（``sources``），比时间戳更能回答「这份报告读的是哪份输入」。
    """
    from orchestrator.benchmark.phase281 import verdict as v

    root = Path(root)
    rows = collect_phase_trials(root)
    ledger_tokens = _ledger_tokens_by_trial(root)
    ledger_path = root / "quota" / "ledger.jsonl"
    if not ledger_path.is_file():
        raise LedgerMissingError(f"数据根缺账本：{ledger_path}（终局分析不引外账）")
    ledger = ledger_snapshot(ledger_path)

    pilot_rows = [r for r in rows if r["phase"] == "pilot"]
    go_no_go, go_reasons = decide_go_no_go(pilot_rows)

    hypotheses = evaluate_hypotheses(rows, ledger_tokens,
                                     semantic_lane=semantic_lane,
                                     semantic_present=semantic_present)
    for hid, doc in hypotheses.items():
        v.assert_partition(hid, doc["state"])

    tables = read_json(root / "intake" / "table_counts.json")
    pilot_report = root / "analysis" / "pilot_report.json"
    sources = {
        "pilot_report_sha256": (_sha256_file(pilot_report)
                                if pilot_report.is_file() else None),
        "ledger_sha256": _sha256_file(ledger_path),
        "table_counts_sha256": (_sha256_file(root / "intake" / "table_counts.json")
                                if (root / "intake" / "table_counts.json").is_file()
                                else None),
    }

    report = {
        "schema": FINAL_REPORT_SCHEMA,
        "experiment_id": constants.EXPERIMENT_ID,
        "phase": constants.PHASE_NAME,
        "go_no_go": go_no_go,
        "go_no_go_reasons": list(go_reasons),
        "pre_registered_trials": PRE_REGISTERED_TRIALS,
        "trial_counts": {
            "pre_registered": PRE_REGISTERED_TRIALS,
            "present": sum(1 for r in rows if r["present"]),
            "not_run": sum(1 for r in rows if not r["present"]),
            "pilot_present": sum(1 for r in pilot_rows if r["present"]),
            "confirmatory_present": sum(1 for r in rows
                                        if r["phase"] == "confirmatory" and r["present"]),
        },
        "trials": rows,
        "hypotheses": hypotheses,
        "semantic_lane": semantic_lane,
        "semantic_lane_reviewers": list(semantic_lane_reviewers),
        "semantic_scores": {
            "present": bool(semantic_present),
            "reviewers": list(semantic_lane_reviewers),
            "reason": ("确认性阶段未启动，R1 无可盲评输出，故无盲评分"
                       if not semantic_present else "已采集"),
        },
        "ledger": {
            "path": str(ledger_path),
            "calls": int(ledger["summary"].get("call_rows") or 0),
            "calls_ok": ledger["summary"].get("calls_ok"),
            "by_task": ledger["summary"].get("calls_by_task"),
            "by_stage": ledger["summary"].get("calls_by_stage"),
            "seq_first": ledger["summary"].get("seq_first"),
            "seq_last": ledger["summary"].get("seq_last"),
            "batches": ledger.get("batches"),
            "gaps": ledger.get("gaps"),
            "validation_error": ledger.get("validation_error"),
        },
        "tables_baseline": (tables.get("tables") if isinstance(tables, Mapping) else None),
        "limitations": _limitations(rows, go_no_go=go_no_go, go_reasons=go_reasons,
                                    ledger={"calls": int(ledger["summary"].get("call_rows") or 0)},
                                    semantic_lane=semantic_lane,
                                    semantic_lane_reviewers=semantic_lane_reviewers,
                                    semantic_present=bool(semantic_present)),
        "disclaimer": {"text": v.SAMPLE_SIZE_DISCLAIMER,
                       "sha256": v.disclaimer_sha256()},
        "sources": sources,
    }
    if frozenset(report) != FINAL_REPORT_FIELDS:
        raise RuntimeError(f"final_report 字段集漂移：多/少 "
                           f"{sorted(frozenset(report) ^ FINAL_REPORT_FIELDS)}")
    return report


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_opt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """把 ``final_report.json`` 渲染成可读报告（**禁用措辞扫描的对象**）。

    首页是三行固定声明（§18.3 逐字），紧接着登记该声明块的 sha256；
    其余全文不得出现 §18.2 的任何字面——本函数不写解释性套话，
    免得套话自己踩词。
    """
    from orchestrator.benchmark.phase281 import verdict as v

    disc = str(report.get("disclaimer", {}).get("text") or v.SAMPLE_SIZE_DISCLAIMER)
    disc_sha = str(report.get("disclaimer", {}).get("sha256") or v.disclaimer_sha256())
    counts = report.get("trial_counts") or {}
    lines: "list[str]" = [
        "# Phase 2.8.1 最终报告（Roadmap Phase 1）",
        "",
        disc,
        "",
        f"> 首页声明块 sha256：`{disc_sha}`（AC-P281-9-07 登记；扫描时按此 sha256 整块剔除）",
        "",
        "## 1. 结论",
        "",
        f"- go/no-go 机判：**{report.get('go_no_go')}**（预注册规则 §11.5，机械产出）",
        f"- 预注册 Trial：{counts.get('pre_registered')} 个；在场 "
        f"{counts.get('present')} 个；未跑 {counts.get('not_run')} 个",
        f"- 确认性阶段：在场 {counts.get('confirmatory_present')} 个 ⇒ "
        + ("未启动" if not counts.get("confirmatory_present") else "部分在场"),
        "",
        "机判理由：",
        "",
    ]
    for reason in report.get("go_no_go_reasons") or []:
        lines.append(f"- {reason}")
    lines += ["", "## 2. 假设判决（只吃确认性 12 个 Trial）", "",
              "| 假设 | 判决 | 事由码 |", "|---|---|---|"]
    for hid in ("H1", "H2", "H3", "H4", "H5"):
        doc = (report.get("hypotheses") or {}).get(hid) or {}
        lines.append(f"| {hid} | {doc.get('state')} | `{doc.get('reason_code')}` |")
    lines += [
        "",
        "三态取值：`CAPABILITY_DEMONSTRATED` / `NOT_ESTABLISHED` / `INCONCLUSIVE`。"
        "本报告的五条判决全部由 `verdict.py` 的纯函数产出；`INCONCLUSIVE` 的事由码为 "
        "`CONFIRMATORY_NOT_RUN` 时表示**确认性阶段没有运行**，不表示「跑了但分不清」。",
        "",
        "## 3. 逐 Trial 五元组",
        "",
        "| trial | 阶段 | 在场 | 判决 | 失败类 | 呼数 | 出生 | 首出生执行序 | 墙钟(s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report.get("trials") or []:
        lines.append(
            "| {trial_id} | {phase} | {present} | {verdict} | {failure_class} | "
            "{used_calls} | {birth_level} | {first_birth_round} | {wallclock_s} |".format(
                trial_id=row.get("trial_id"), phase=row.get("phase"),
                present=_fmt_opt(row.get("present")),
                verdict=_fmt_opt(row.get("verdict")),
                failure_class=_fmt_opt(row.get("failure_class")),
                used_calls=_fmt_opt(row.get("used_calls")),
                birth_level=_fmt_opt(row.get("birth_level")),
                first_birth_round=_fmt_opt(row.get("first_birth_round")),
                wallclock_s=_fmt_opt(row.get("wallclock_s"))))
    ledger = report.get("ledger") or {}
    lines += [
        "",
        "## 4. 账本",
        "",
        f"- 实呼行数：{ledger.get('calls')}（`ok` {ledger.get('calls_ok')}）",
        f"- seq 区间：{ledger.get('seq_first')} … {ledger.get('seq_last')}；"
        f"断号：{ledger.get('gaps') or '无'}",
        f"- by_task：{ledger.get('by_task')}",
        f"- by_stage：{ledger.get('by_stage')}",
        f"- 批次：{ledger.get('batches')}",
        "",
        "## 5. 语义车道",
        "",
        f"- 状态：**{report.get('semantic_lane')}**（用户 2026-11-07 裁决）",
        f"- 复核人：{'、'.join(report.get('semantic_lane_reviewers') or []) or '无'}",
        f"- 盲评分：{_fmt_opt((report.get('semantic_scores') or {}).get('present'))}；"
        f"{(report.get('semantic_scores') or {}).get('reason')}",
        "",
        "## 6. 限制（逐条，不得删减）",
        "",
    ]
    for item in report.get("limitations") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "## 7. 来源（sha256）",
        "",
    ]
    for key in sorted(report.get("sources") or {}):
        lines.append(f"- `{key}`：{_fmt_opt((report.get('sources') or {}).get(key))}")
    lines.append("")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="phase281_analysis",
        description="Phase 2.8.1 分析（pilot：go/no-go 机判；final：18 Trial 判决；"
                    "0 付费）")
    ap.add_argument("--stage", choices=("pilot", "final"), required=True,
                    help="分析阶段：pilot（go/no-go）/ final（H1–H5 三态判决）")
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
    ap.add_argument("--accounting-out", default=None,
                    help="§11.9 记账证明的落盘路径（M2；不给则不写）。"
                         "与 --out 的 8 字段契约分开，互不覆盖")
    ap.add_argument("--report-out", default=None,
                    help="--stage final 的可读报告路径（Markdown；不给则不写）。"
                         "§18.2 禁用措辞扫描的对象就是这份文本")
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
    if args.stage == "final":
        return _main_final(args, root)

    env = env_probe() if args.probe_env else None
    register = (register_verify(lock=args.lock) if args.verify_register else None)
    batches = _parse_batches(args.batches)
    report = build_pilot_report(root, rep=int(args.rep), env=env, register=register,
                                batches=batches)
    target = write_report(report, Path(args.out))
    acct_path = None
    if args.accounting_out:
        acct = build_accounting(root, rep=int(args.rep), batches=batches)
        acct_path = str(write_report(acct, Path(args.accounting_out)))
    print(json.dumps({"out": str(target), "go_no_go": report["go_no_go"],
                      "reasons": report["reasons"],
                      "stop_open": report["stop_open"],
                      "batches": batches,
                      "accounting_out": acct_path},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["go_no_go"] in ("GO", "HOLD") else 1


def _main_final(args: argparse.Namespace, root: Path) -> int:
    """``--stage final``：写 ``final_report.json``（可选同时写可读报告）。"""
    from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

    report = build_final_report(root)
    target = write_report(report, Path(args.out))
    report_path = None
    if args.report_out:
        text = render_report_markdown(report)
        report_path = Path(args.report_out)
        ensure_private_dir(report_path.parent)
        atomic_write_text(report_path, text, mode=0o600)
    print(json.dumps({"out": str(target), "report_out":
                      (str(report_path) if report_path else None),
                      "go_no_go": report["go_no_go"],
                      "trial_counts": report["trial_counts"],
                      "hypotheses": {k: v["state"]
                                     for k, v in report["hypotheses"].items()},
                      "disclaimer_sha256": report["disclaimer"]["sha256"]},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
