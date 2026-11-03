#!/usr/bin/env python3
"""Phase 2.6 Task4：t5_freeze 双墙重测（AgenticConfig 四项修复全开，付费 ≤28）。

计划出处：``~/文档/Phase_2.6_Plan_detailed.md`` §T4（v1.4）。
本脚本是 Task 4 的唯一付费入口；与 Phase 2.5 的 ``run_wall.py`` **同构但
不修改它**（D4-1：Phase 2.5「不成立」判决必须能按其原代码重建）。

用法：
  # 0 付费：冻结九件闸哈希 + 镜像 digest + 票据 + 容器管道 + F2 内联探针冒烟
  python -m scripts.phase26.run_wall_retest --preflight

  # 0 付费：在 phase25 冻结 trace 上按本 Task 预注册定义复算 R_base
  python -m scripts.phase26.run_wall_retest --recompute-baseline

  # 账外付费（恰 1 次 POST、0 账行）：coding_debug 健康单发探针（P26-B0b）
  SIMPRAXIS_ALLOW_PAID=1 python -m scripts.phase26.run_wall_retest --probe-coding

  # 0 付费：落判据文本（首呼前）并封 LOCK（sha256 + mtime + mode=444）
  python -m scripts.phase26.run_wall_retest --write-criteria
  python -m scripts.phase26.run_wall_retest --seal-criteria

  # 真跑（必须 SIMPRAXIS_ALLOW_PAID=1，裸跑 exit 2；经 run_watched.sh 外包）
  SIMPRAXIS_ALLOW_PAID=1 python -m scripts.phase26.run_wall_retest \
      --wall wallA --batch P26-B1

  # 0 付费：判定 / 聚合 / 账审
  python -m scripts.phase26.run_wall_retest --judge --wall wallA
  python -m scripts.phase26.run_wall_retest --combine-v3
  python -m scripts.phase26.run_wall_retest --judge-all
  python -m scripts.phase26.run_wall_retest --audit-ledger

纪律（硬规则 7 + §T4.1 I4）：
- 墙不许动：首呼前比对 ``freeze_manifest.json`` 九件闸哈希（复用
  ``freeze_common``）；任一不符即熔断，不触网。
- 帽不许抬：A 子帽 16 / B 子帽 9 / 储备 3，合计 28；本脚本**不重算子帽**
  （``SUBCAPS`` 是 Phase 2.5 冻结值 ``compute_subcaps(60,30)``）。
- 模型不许换：全程 ``stage_models.yaml`` 的 ``coding_debug``；回显不符即
  STOP（硬规则 1）。
- 判据预注册：``VERDICT_CRITERIA_V3.md`` 必须在**首呼之前**落盘、封 LOCK、
  置 0444；跑后零改动（sha + mtime + mode 三重取证）。
- 矛盾即结论：判负不改判据、不换种子、不重跑同一墙。唯一允许的重跑是
  D4-6 定义的「基础设施故障」，且必须有对应日志行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.agentic.caps import BudgetTracker
from orchestrator.agentic.config import AgenticConfig
from orchestrator.agentic.forensics import MODEL_ROUND_TOOL, TraceStore
from orchestrator.agentic.loop import run_agentic_inner_loop
from orchestrator.agentic.wiring import (
    build_seq_provider, parse_grant_strings,
)
from orchestrator.common.runtime_paths import sealed_ledger_paths
from orchestrator.gateway.quota_ledger import (
    QuotaLedger, billing_context, call_lines, read_lines,
)
from orchestrator.gateway.router import ModelGateway

from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls.run_wall import (
    FROZEN_GRANTS, GpuSampler, OPERATIONAL_FOOTER, docker_label_residue,
)
from scripts.phase25.walls.spike_backend import (
    IMAGE_ID, IMAGE_REPODIGEST, SpikeWallBackend, score_wall_a, score_wall_b,
)
from scripts.phase25.walls.verdict import WALL_A, WALL_B

WALLS = (WALL_A, WALL_B)

# --- §T4.7 冻结预算（不得用新测 s 值重算：重算＝抬/降帽，硬规则 7） ---------
SUBCAPS = {WALL_A: 16, WALL_B: 9}      # learn from verdict.compute_subcaps(60,30)
RESERVE = 3
HARD_ENVELOPE = sum(SUBCAPS.values()) + RESERVE      # 28
TASK_ID = 4
BATCH_ID = "P26-B1"
OFFLEDGER_BATCH = "P26-B0"             # 账外 --validate-models，恒 4 次 POST
PROBE_BATCH = "P26-B0b"               # 账外 coding_debug 单发探针，恰 1 次 POST
PROBE_SCHEMA = "simpraxis.phase26.t4_coding_probe.v1"

# --- §T4.4 判据常量（与 VERDICT_CRITERIA_V3.md 逐条同义） -------------------
BIRTH_ROUND_LIMIT = 8                  # AC-T4-1
CALLS_TO_RC0_LIMIT_B = 15              # AC-T4-2 回归闸
EFFECTIVE_RATIO_FLOOR = 0.80           # AC-T4-3
TAIL_ROUNDS = 3                        # §T4.4 CFG
GPU_CLOSEOUT_MIB = 200                 # 硬规则 5

CFG = AgenticConfig(verify_repair_tail=True,
                    verify_repair_tail_rounds=TAIL_ROUNDS,
                    run_expr=True, tolerant_patch=True, scratchpad=True)

# F2/F4 追加授予（其余与 Phase 2.5 冻结件逐字相同，运行时从 GATE.md 读取）。
EXTRA_GRANTS = ("expr:numpy,math,json,os.path", "update_scratchpad")

V_ESTABLISHED = "ESTABLISHED"
V_NOT_ESTABLISHED = "NOT_ESTABLISHED"
V_INCONCLUSIVE = "INCONCLUSIVE"
V_PENDING = "未判定"

CRITERIA_NAME = "VERDICT_CRITERIA_V3.md"
CRITERIA_LOCK_SCHEMA = "simpraxis.phase26.t4_criteria_lock.v1"
VERDICT_SCHEMA = "simpraxis.phase26.verdict.v3"
WALL_RESULT_SCHEMA = "simpraxis.phase26.wall_result.v3"
PREFLIGHT_SCHEMA = "simpraxis.phase26.t4_preflight.v1"
BASELINE_SCHEMA = "simpraxis.phase26.t4_baseline_ratio.v1"
AUDIT_SCHEMA = "simpraxis.phase26.t4_ledger_audit.v1"

# 哨兵/负值两轴在冻结 checker 里的承载检查（§VERDICT_CRITERIA_V3.md §3）。
SENTINEL_CHECKS = ("S0_no_sentinel_literals", "K5_nonfinite_raises",
                   "K6_degenerate_raises")
KINEMATIC_CHECKS = ("K1_all_pass", "K2_static", "K3_too_fast",
                    "K4_penetrating")


# --------------------------------------------------------------- 基础工具

def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def phase26_root() -> Path:
    base = os.environ.get("SIMPRAXIS_PHASE26_ROOT")
    if not base:
        raise SystemExit("SIMPRAXIS_PHASE26_ROOT 未设置（先 source "
                         "scripts/00_env.sh）")
    return Path(base)


def retest_root() -> Path:
    return phase26_root() / "walls" / "retest"


def criteria_path() -> Path:
    return retest_root() / "VERDICT_CRITERIA_V3.md"


def lock_path() -> Path:
    return retest_root() / "manifests" / "LOCK.json"


def phase26_ledger() -> QuotaLedger:
    return QuotaLedger.phase26()


def nvidia_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used",
         "--format=csv,noheader,nounits"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    return int(out.stdout.decode().strip().splitlines()[0])


def coding_debug_model_id() -> str:
    """只读解析 stage_models.yaml 的 coding_debug（不写、不猜、不分支）。"""
    import yaml
    p = (Path(__file__).resolve().parents[2]
         / "orchestrator" / "gateway" / "stage_models.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entry = (data.get("stage_models") or {}).get("coding_debug") or {}
    model_id = entry.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise SystemExit(f"coding_debug.model_id 非法：{model_id!r}（硬规则 1）")
    return model_id


# --------------------------------------------------------------- 冻结面校验

def verify_freeze(root: Path) -> dict:
    """九件闸哈希 + 内文留痕 + 镜像 digest（沿用 phase25 既有实现）。"""
    manifest_p = root / "manifests" / "freeze_manifest.json"
    if not manifest_p.is_file():
        raise SystemExit(f"冻结清单不存在：{manifest_p}")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    current = fc.gate_hashes()
    if manifest["gate_hashes"] != current:
        raise SystemExit("闸件哈希与冻结清单不符，拒呼（fail-closed）")
    fc.verify_embedded_hashes()
    for section in ("wall_a", "wall_b"):
        if section not in manifest["sections"]:
            raise SystemExit(f"冻结清单缺 {section} 段")
    image = manifest.get("image") or {}
    digest = (image.get("repo_digest") or "")
    if digest and digest != IMAGE_REPODIGEST:
        raise SystemExit(
            f"冻结清单 image.repo_digest 与 spike_backend 常量不符："
            f"{digest} vs {IMAGE_REPODIGEST}")
    proc = subprocess.run(
        ["docker", "inspect", IMAGE_ID, "--format",
         "{{json .RepoDigests}}|{{json .RepoTags}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 or IMAGE_REPODIGEST.split(":", 1)[1] not in out:
        raise SystemExit("冻结镜像 digest 不符/缺失，拒呼")
    return manifest


FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)


def grants_from_gate(wall: str) -> tuple[str, ...]:
    gate_dir = fc.GATES_DIR / ("wall_a" if wall == WALL_A else "wall_b")
    text = (gate_dir / "GATE.md").read_text(encoding="utf-8")
    blocks = [b for b in FENCE_RE.findall(text) if "run:python" in b]
    if len(blocks) != 1:
        raise SystemExit(f"GATE.md 白名单围栏不唯一：{gate_dir}")
    return tuple(ln.strip() for ln in blocks[0].splitlines() if ln.strip())


def retest_grants(wall: str) -> tuple[str, ...]:
    """Phase 2.5 冻结授予串逐字 + F2/F4 两项（计划 §T4.4 差异点 2）。"""
    frozen = grants_from_gate(wall)
    if frozen != FROZEN_GRANTS:
        raise SystemExit(f"GATE.md 白名单与冻结授予串不符：\n{frozen}")
    return frozen + EXTRA_GRANTS


def task_text(wall: str) -> str:
    gate_dir = fc.GATES_DIR / ("wall_a" if wall == WALL_A else "wall_b")
    return ((gate_dir / "TASK.md").read_text(encoding="utf-8")
            + OPERATIONAL_FOOTER)


# --------------------------------------------------------------- 判据与 LOCK

CRITERIA_BODY = """\
# Phase 2.6 Task 4 三态判据 v3（首呼前冻结；与此后脚本机判逐条同义）

本文件是 Task 4「agentic 工具层 F1–F4 是否解冻历史墙」的唯一判据文本。
**首呼之前**落盘、封 LOCK（sha256 + mtime + mode=444）；首呼后一字不得改
（硬规则 7：矛盾即结论，禁止调参/换种子/改界翻案）。任何引擎脚手架改动
只能发生在首呼之前；首呼后发现歧义按「疑罪从有」处理，不改判据。

## 1. 墙 A（信息墙：出生难）判据 — AC-T4-1

在**同一份绝对冻结的** `t5_freeze` 墙 A 上，用四项修复全开的内层循环：

- `rounds_to_first_birth <= 8`（入口脚本 `.entrypoint.py` 首次同时满足
  「存在 + >=200 字节 + `compile()` 通过」的模型轮号；轮号从 1 起）；
- **且** 评分器 `final_rc == 0`（冻结 `wall_a_runner.py` 全 12 项检查 PASS）；
- **且** trace 含 >=1 次成功的 `run_expr` 调用（F2 真的进了工具面）；
- **且** trace 含 >=1 个 `phase == "verify_repair"` 的模型轮（F1 追授生效）。

四条全成立才算墙 A 过；任一条不成立即墙 A 不过。

## 2. 墙 B（合同墙：验收难）判据 — AC-T4-2

在同一份冻结墙 B 上：

- 冻结 `wall_b_checker.py` 的 **12 项检查**逐项 PASS（`checks` 恰 12 条，
  `verdict == "pass"`，`rc == 0`）；
- **且** `sentinel_count == 0`：三条哨兵类检查逐条 PASS
  （`S0_no_sentinel_literals` / `K5_nonfinite_raises` / `K6_degenerate_raises`）；
- **且** `negative_physical == 0`：四条运动学检查逐条 PASS
  （`K1_all_pass` / `K2_static` / `K3_too_fast` / `K4_penetrating`；冻结
  checker 在这四条内合取「witness 全有限 且 speed_max>=0 且 drift_max>=0」，
  不可再分解，故以四条检查的失败数作机判代理，不另造第二真值）；
- **且** `calls_to_rc0 <= 15`（AC-T4-2 的**回归闸**；对当前模型是松的，
  单独满足不具证伪力，见 §T4.3 锁内适配 2）。

## 3. 有效执行步数占比（预注册定义，逐字）

```
单位：模型轮（一次 gateway 调用 = 一轮）。
productive_round(r) := 该轮至少满足其一：
  (a) 存在一次工具调用，其观察非空 且 不是 policy deny、不是 schema reject、
      不是 PatchApplyFailed、不是 CapExceeded；
  (b) 该轮首次使入口脚本出现在磁盘（birth_round）。
effective_step_ratio := count(productive_round) / count(all rounds)
判据：ratio >= 0.80
基线：在 T5/T5b 的冻结 trace 上按同一定义复算，得 R_base。
```

机判口径补充（不改变上面定义，只说明本脚本怎么算）：
`policy deny` 取 trace 行 `policy.decision != "allow"`；`schema reject` 与
`PatchApplyFailed` 在工具层都是 deny 观察（`ToolResult.denied_result`），已
被上一条覆盖；`CapExceeded` 是 BaseException，**不产生工具行**，故不参与
分母之外的任何计数。`birth_round` 由内环的 `budget.born_at_round` 直接给出。

## 4. 三态映射（判定顺序，短路）

1. 两墙全过 **且** 两墙合并 `effective_step_ratio >= 0.80` → `ESTABLISHED`；
2. 否则，**仅当下列三条同时成立** → `INCONCLUSIVE`：
   (i) 两墙未全过；(ii) 存在**基础设施故障**（D4-6 三类：网关 5xx/429
   三次退避仍失败；容器 OOM/被杀；磁盘写满）；(iii) 共享池已用到硬包络 28
   （储备耗尽，本批次无法再跑）；
3. 其余一律 → `NOT_ESTABLISHED`。

> `INCONCLUSIVE` 是**窄口**，三种「看起来像不确定」的情形都不是它：
> - 「墙 A 出生了但 rc≠0」「墙 B 12 项有一项掉」→ `NOT_ESTABLISHED`
>   （跑完了，只是没过）；
> - 「两墙全过但 ratio < 0.80」→ `NOT_ESTABLISHED`（跑完了，效率不达标）；
> - 「基础设施故障但池未用满」→ `NOT_ESTABLISHED`（还有储备却没跑完，
>   按判据就是没过；不得当作"不确定"软化）。

## 5. 预算与账（§T4.7）

- 票据 `P26-B1`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3
  （子额度只由脚本 `SUBCAPS` 强制，不进票据）；
- 全部实呼落 `QuotaLedger.phase26()`；phase1/2/25 三本封存账零写入；
- 到帽即停，不追加；若已用 28 仍 `INCONCLUSIVE` → 报用户要新票据。

## 6. 硬规则 7 声明

本判据在首呼前预注册；跑完不修改。与预注册方向相反的结果（含负结论）
矛盾即结论，不调参、不换种子、不改界、不重跑同一墙翻案。

---

预注册 sha256 占位行（`--seal-criteria` 前由脚本写入实际值）：
`R_base = __RBASE__`
"""


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------- 基线复算

def _obs_nonempty(row: dict) -> bool:
    """trace 不存观察原文：可见性取 obs_head/obs_tail（切片非空即非空）。"""
    return bool((row.get("obs_head") or "").strip()
                or (row.get("obs_tail") or "").strip())


def _productive_rounds(events: list[dict]) -> tuple[list[dict], int]:
    """按 §3 预注册定义从 trace 行复算每轮 productive 标记。

    返回 (rounds, productive_count)。模型轮行取 ``__model_round__``（轮号
    字段 ``round``，1 起）；其余 ``__`` 前缀内部控制行（``__answer__`` /
    ``__condense__``）不参与判定（既有 trace 语义：控制行不是工具调用）。
    工具行字段：``policy_decision`` / ``obs_head`` / ``obs_tail``。
    """
    rounds: list[dict] = []
    by_round: dict[Any, list[dict]] = {}
    for e in events:
        tool = str(e.get("tool") or "")
        if tool == MODEL_ROUND_TOOL:
            rounds.append({"round_idx": e.get("round"),
                           "cache_hit": bool((e.get("args") or {}).get(
                               "cache_hit")),
                           "productive": False,
                           "productive_reason": ""})
        elif tool.startswith("__"):
            continue
        else:
            by_round.setdefault(e.get("round"), []).append(e)
    index = {r["round_idx"]: r for r in rounds}
    productive = 0
    for ridx, rows in by_round.items():
        rec = index.get(ridx)
        if rec is None:      # 无对应模型轮行的工具行（异常残行）不计数
            continue
        for row in rows:
            decision = (row.get("policy_decision") or {}).get("decision")
            if decision == "allow" and _obs_nonempty(row):
                rec["productive"] = True
                rec["productive_reason"] = f"tool:{row.get('tool')}"
                break
    for rec in rounds:
        if rec["productive"]:
            productive += 1
    return rounds, productive


def _load_trace_events(trace_jsonl: Path) -> list[dict]:
    out = []
    for line in trace_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def baseline_ratio_sources() -> list[Path]:
    """Phase 2.5 冻结 trace 全集（T5 g2 + T5b 两批）。"""
    root = fc.freeze_root()
    return sorted((root / "runs").glob("*/*/traces/*/trace.jsonl"))


def cmd_recompute_baseline() -> int:
    traces = baseline_ratio_sources()
    if not traces:
        raise SystemExit(f"冻结 trace 不存在：{fc.freeze_root()}/runs/*/*/traces")
    per_run = []
    total_rounds = 0
    total_productive = 0
    for p in traces:
        events = _load_trace_events(p)
        rounds, productive = _productive_rounds(events)
        if not rounds:
            continue
        wall = "wallA" if "/wallA/" in str(p) else (
            "wallB" if "/wallB/" in str(p) else "unknown")
        rec = {"wall": wall, "trace": str(p), "rounds": rounds,
               "n_rounds": len(rounds), "n_productive": productive,
               "ratio": round(productive / len(rounds), 6)}
        per_run.append(rec)
        total_rounds += len(rounds)
        total_productive += productive
    if total_rounds == 0:
        raise SystemExit("冻结 trace 里没有模型轮行，无法复算 R_base")
    pooled = round(total_productive / total_rounds, 6)
    by_wall = {}
    for wall in ("wallA", "wallB"):
        rs = [r for r in per_run if r["wall"] == wall]
        n = sum(r["n_rounds"] for r in rs)
        k = sum(r["n_productive"] for r in rs)
        by_wall[wall] = {"runs": len(rs), "rounds": n, "productive": k,
                         "ratio": round(k / n, 6) if n else None}
    payload = {
        "schema": BASELINE_SCHEMA,
        "ts_iso": utc_iso(),
        "definition": "§T4.3 预注册定义（VERDICT_CRITERIA_V3.md §3 逐字）",
        "phase25_recorded": 0.36,
        "recomputed": pooled,
        "by_wall": by_wall,
        "in_36pct_band": 0.34 <= pooled <= 0.38,
        "runs": per_run,
    }
    out = retest_root() / "baseline_effective_ratio.json"
    fc.write_json(out, payload)
    print(json.dumps({k: v for k, v in payload.items() if k != "runs"},
                     indent=1, ensure_ascii=False))
    print(f"[retest] R_base={pooled} → {out}")
    return 0


# --------------------------------------------------------------- 判据写/封

def cmd_write_criteria() -> int:
    base_p = retest_root() / "baseline_effective_ratio.json"
    if not base_p.is_file():
        raise SystemExit(f"先跑 --recompute-baseline（缺 {base_p}）")
    if lock_path().is_file():
        raise SystemExit("LOCK 已存在：判据已封，禁止重写")
    rbase = _read_json(base_p)["recomputed"]
    text = CRITERIA_BODY.replace("__RBASE__", f"{rbase}")
    p = criteria_path()
    fc.atomic_write_bytes(p, text.encode("utf-8"))
    print(f"[retest] 判据已落盘：{p}")
    return 0


def _ledger_task_rows() -> list[dict]:
    ledger = phase26_ledger()
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    return [r for r in rows if r.get("task") == TASK_ID]


def _criteria_lock_record() -> dict:
    p = criteria_path()
    st = p.stat()
    return {
        "schema": CRITERIA_LOCK_SCHEMA,
        "criteria_path": str(p),
        "criteria_sha256": fc.sha256_file(p),
        "criteria_mtime": int(st.st_mtime),
        "criteria_mode": oct(st.st_mode & 0o777),
        "locked_iso": utc_iso(),
        "batch_id": BATCH_ID,
        "note": ("首呼锁：判据文本在首呼之前封存；跑后任何改动都使本批次作废"
                 "（sha + mtime + mode 三重取证）。"),
    }


def verify_criteria_lock() -> dict:
    """AC-T4-4 三重取证：sha256 相等 + mode=444 + mtime 相等。"""
    if not lock_path().is_file():
        raise SystemExit("LOCK 不存在：判据未预注册，拒绝开跑（fail-closed）")
    lock = _read_json(lock_path())
    p = criteria_path()
    if not p.is_file():
        raise SystemExit(f"判据文件缺失：{p}")
    st = p.stat()
    problems = []
    if fc.sha256_file(p) != lock["criteria_sha256"]:
        problems.append("sha256 不符")
    if int(st.st_mtime) != int(lock["criteria_mtime"]):
        problems.append(f"mtime 不符（{int(st.st_mtime)} vs "
                        f"{lock['criteria_mtime']}）")
    if oct(st.st_mode & 0o777) != lock["criteria_mode"]:
        problems.append(f"mode 不符（{oct(st.st_mode & 0o777)} vs "
                        f"{lock['criteria_mode']}）")
    if problems:
        raise SystemExit("判据 LOCK 校验失败（AC-T4-4）：" + "；".join(problems))
    return lock


def cmd_seal_criteria() -> int:
    if lock_path().is_file():
        raise SystemExit("LOCK 已存在：判据已封，拒绝重封")
    if _ledger_task_rows():
        raise SystemExit("账上已有 task=4 实呼行：首呼已发生，禁止再封判据")
    p = criteria_path()
    if not p.is_file():
        raise SystemExit(f"判据文件缺失：{p}（先 --write-criteria）")
    os.chmod(p, 0o444)
    rec = _criteria_lock_record()
    fc.write_json(lock_path(), rec)
    print(json.dumps(rec, indent=1, ensure_ascii=False))
    return 0


# --------------------------------------------------------------- 预检

def _smoke_backend(wall: str, root: Path) -> dict:
    """容器管道冒烟 + F2 内联探针冒烟（run_expr 的 `python -I -c` 路径）。"""
    backend = SpikeWallBackend(wall, freeze_dir=root)
    smoke = "/opt/venv/bin/python"
    if wall == WALL_A:
        code = ("import numpy, mujoco, robosuite; "
                "from libero.libero.benchmark import get_benchmark; "
                "from libero.libero.envs.env_wrapper import ControlEnv; "
                "print('SMOKE_OK', ControlEnv.__name__)")
    else:
        code = ("import numpy, trimesh; "
                "print('SMOKE_OK', trimesh.__version__)")
    with tempfile.TemporaryDirectory(prefix="p26t4preflight-") as td:
        res = backend.run([smoke, "-c", code], cwd=Path(td), timeout_s=180)
        # F2：run_expr 的执行形态逐字（`python -I -c src`），只验管道通。
        expr = backend.run(
            ["python", "-I", "-c", "import numpy; print('EXPR_OK', numpy.__version__)"],
            cwd=Path(td), timeout_s=180)
    return {
        "smoke_rc": res.rc,
        "smoke_stdout": res.stdout.decode("utf-8", "replace").strip()[-400:],
        "smoke_stderr": res.stderr.decode("utf-8", "replace").strip()[-400:],
        "expr_rc": expr.rc,
        "expr_stdout": expr.stdout.decode("utf-8", "replace").strip()[-400:],
        "expr_stderr": expr.stderr.decode("utf-8", "replace").strip()[-400:],
    }


def _sealed_snapshot() -> dict:
    out = {}
    for p in sorted(sealed_ledger_paths()):
        out[str(p)] = {"exists": p.is_file(),
                       "mtime": int(p.stat().st_mtime) if p.is_file() else None}
    return out


def cmd_preflight(wall: str | None = None) -> int:
    root = fc.freeze_root()
    manifest = verify_freeze(root)
    walls = (wall,) if wall else WALLS
    ledger = phase26_ledger()
    ledger_p = ledger.ns.ledger_path_fn()
    before = len(call_lines(read_lines(ledger_p)))
    ticket_p = phase26_root() / "quota" / "approvals" / f"{BATCH_ID}.json"
    if not ticket_p.is_file():
        raise SystemExit(f"票据不存在：{ticket_p}（先落 P26-B1.json）")
    ticket = _read_json(ticket_p)
    expect = {"batch_id": BATCH_ID, "task": TASK_ID, "stage": "coding_debug",
              "approved": HARD_ENVELOPE, "beyond_normal": False,
              "ledger_phase": 26}
    for key, want in expect.items():
        if ticket.get(key) != want:
            raise SystemExit(
                f"票据字段 {key} 不符：{ticket.get(key)!r} != {want!r}"
                "（第 2/4/6 道闸按此拒）")
    try:
        pf = ledger.preflight("coding_debug", task=TASK_ID, batch_id=BATCH_ID)
    except Exception as exc:  # noqa: BLE001 - 任何拦截都是预检失败
        raise SystemExit(f"票据 preflight 被拦：{type(exc).__name__}: {exc}")
    after = len(call_lines(read_lines(ledger_p)))
    if after != before:
        raise SystemExit("preflight 意外写账（票据可能被判 blocked）")

    grants = {}
    smokes = {}
    for w in walls:
        g = retest_grants(w)
        parse_grant_strings(g)          # 形态闸：偷渡授予串直接抛
        grants[w] = list(g)
        smokes[w] = _smoke_backend(w, root)
        if smokes[w]["smoke_rc"] != 0 or "SMOKE_OK" not in smokes[w][
                "smoke_stdout"]:
            raise SystemExit(f"{w} 管道冒烟失败：{smokes[w]}")
        if smokes[w]["expr_rc"] != 0 or "EXPR_OK" not in smokes[w][
                "expr_stdout"]:
            raise SystemExit(f"{w} F2 内联探针冒烟失败：{smokes[w]}")
    residue = docker_label_residue()
    if residue:
        raise SystemExit(f"冒烟后容器残留：{residue}（硬规则 5）")

    free_gib = shutil.disk_usage("/mnt").free / (1 << 30)
    if free_gib < 40:
        raise SystemExit(f"/mnt 空闲 {free_gib:.1f}G < 40G（§T4.2 入口准则）")

    payload = {
        "schema": PREFLIGHT_SCHEMA,
        "ts_iso": utc_iso(),
        "model": {"coding_debug": coding_debug_model_id()},
        "wall": wall or "all",
        "manifest_sections": list(manifest["sections"]),
        "gate_hashes": fc.gate_hashes(),
        "image": {"id": IMAGE_ID, "repo_digest": IMAGE_REPODIGEST},
        "grants": grants,
        "ticket": ticket,
        "preflight_usage": pf,
        "ledger_call_rows_before_after": [before, after],
        "smoke": smokes,
        "container_residue": [],
        "free_gib_mnt": round(free_gib, 1),
        "paid_calls": 0,
    }
    out = retest_root() / "preflight.json"
    fc.write_json(out, payload)
    delta = {"schema": "simpraxis.phase26.t4_preflight_ledger_delta.v1",
             "ts_iso": utc_iso(), "before": before, "after": after,
             "delta": after - before, "paid_calls": 0}
    fc.write_json(retest_root() / "preflight_ledger_delta.json", delta)
    fc.write_json(retest_root() / "manifests" / "sealed_ledger_snapshot.json",
                  {"ts_iso": utc_iso(), "paths": _sealed_snapshot()})
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    return 0


# --------------------------------------------------------------- 账外单发探针

def cmd_probe_coding(batch: str = PROBE_BATCH,
                     max_tokens: int = 8) -> int:
    """账外 coding_debug 单发探针（P26-B0b，恰 1 次付费 POST，0 账行）。

    计划出处：§T4.5 步骤 2 的 STOP-1 分支（用户 2026-11-03 裁决「先补 1 次
    coding 探针」）。``--validate-models`` 在 macro_ideation 上撞 5 次 HTTP
    200 空回答后失败，需要单独确认 **墙 A/B 真正要用的 coding_debug** 今天
    是否健康。

    与 ``ping_text`` 的刻意差异：``ping_text`` 走 ``_post_messages``，对
    HTTP 200 空内容会重试至 ``max_attempts=5``（每次重试都是真付费 POST）。
    本探针用 ``_post_once`` 一次性直 POST，**任何情况下都只有 1 个请求**，
    这正是「approved=1」的兑现方式（硬规则 2：超发即违票）。

    纪律：
    - 票据 ``P26-B0b.json`` 必须在场且 approved==1（缺失/超额即 exit 2，不发网）；
    - 账行前后必须相等（本探针 0 账行，与 ``--validate-models`` 同口径）；
    - 回显模型必须逐字等于 ``stage_models.yaml`` 的 coding_debug（硬规则 1：
      不符即 STOP，不改配置、不猜 ID）。
    """
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("[probe] 拒绝：需 SIMPRAXIS_ALLOW_PAID=1（付费探针守卫）",
              file=sys.stderr)
        return 2
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
            or max_tokens <= 0:
        print(f"[probe] 拒绝：max_tokens 非法 {max_tokens!r}", file=sys.stderr)
        return 2
    ticket_p = phase26_root() / "quota" / "approvals" / f"{batch}.json"
    if not ticket_p.is_file():
        print(f"[probe] 拒绝：票据不存在 {ticket_p}", file=sys.stderr)
        return 2
    ticket = _read_json(ticket_p)
    for key, want in (("batch_id", batch), ("task", TASK_ID),
                      ("stage", "coding_debug"), ("approved", 1),
                      ("ledger_phase", 26)):
        if ticket.get(key) != want:
            print(f"[probe] 拒绝：票据 {key}={ticket.get(key)!r} != {want!r}",
                  file=sys.stderr)
            return 2

    ledger = phase26_ledger()
    ledger_p = ledger.ns.ledger_path_fn()
    rows_before = len(call_lines(read_lines(ledger_p)))
    model_id = coding_debug_model_id()

    gw = ModelGateway(ledger=None, unguarded=True)  # 账外：与 validate 同路径
    prompt = "ping，只回复 pong"
    rec: dict[str, Any] = {
        "schema": PROBE_SCHEMA, "ts_iso": utc_iso(), "batch_id": batch,
        "stage": "coding_debug", "requested_model": model_id,
        "prompt": prompt, "max_tokens": max_tokens, "paid_posts": 1,
        "off_ledger": True,
    }
    t0 = time.perf_counter()
    try:
        resp, body, _payload = gw._post_once(
            "coding_debug", model_id, [{"type": "text", "text": prompt}],
            max_tokens=max_tokens, timeout=60)
    except Exception as exc:  # noqa: BLE001 - 连接/超时：无响应即无证据
        rec.update({"latency_ms": int((time.perf_counter() - t0) * 1000),
                    "error_type": type(exc).__name__, "error": str(exc)[:500],
                    "verdict": "FAIL:no_response"})
        _write_probe(rec)
        return 1
    rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    rec["status"] = int(getattr(resp, "status_code", 0) or 0)
    rec["type"] = body.get("type")
    rec["echo_model"] = body.get("model")
    blocks = body.get("content") if isinstance(body.get("content"), list) else []
    # 块类型留证：空正文时用来区分「thinking-only」与「真空 content」
    rec["content_types"] = [b.get("type") for b in blocks if isinstance(b, dict)]
    text = "\n".join(b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text").strip()
    rec["text"] = text[:200]
    rec["usage"] = body.get("usage")
    rec["empty"] = not text
    rows_after = len(call_lines(read_lines(ledger_p)))
    rec["ledger_rows_before_after"] = [rows_before, rows_after]

    problems = []
    if rec["status"] != 200:
        problems.append(f"status={rec['status']}")
    if rec["type"] != "message":
        problems.append(f"type={rec['type']!r}")
    if rec["empty"]:
        problems.append("empty_text")
    if rec["echo_model"] != model_id:
        problems.append(f"echo_model={rec['echo_model']!r} != {model_id!r}")
    if rows_after != rows_before:
        problems.append(f"ledger_rows {rows_before}->{rows_after}（账外探针不得写账）")
    rec["verdict"] = "OK" if not problems else "FAIL:" + ";".join(problems)
    _write_probe(rec)
    print(json.dumps(rec, indent=1, ensure_ascii=False))
    return 0 if not problems else 1


def _write_probe(rec: dict) -> Path:
    out = retest_root() / f"probe_coding_debug_{utc_stamp()}.json"
    rec["evidence_path"] = str(out)      # 先落路径再写，件内自证位置
    fc.write_json(out, rec)              # 0700 目录 + 0600 原子写
    return out


# --------------------------------------------------------------- 真跑

def _score_once(wall: str, root: Path, work: Path) -> dict:
    score_dir = work / ".score"
    if score_dir.exists():
        shutil.rmtree(score_dir)
    if wall == WALL_A:
        res = score_wall_a(work, fc.GATES_DIR / "wall_a", root,
                           timeout_s=240)
    else:
        res = score_wall_b(
            work, fc.GATES_DIR / "wall_b" / "wall_b_checker.py",
            root / "evidence_wall_b" / "pitcher_open_watertight.obj",
            timeout_s=180)
    files = sorted(score_dir.glob("wall_*-*.json"))
    score = json.loads(files[-1].read_text(encoding="utf-8")) if files else None
    return {"rc": res.rc, "timed_out": res.timed_out,
            "stdout": res.stdout.decode("utf-8", "replace"),
            "stderr": res.stderr.decode("utf-8", "replace"),
            "score": score, "score_file": files[-1].name if files else None}


def _identity_rows(ledger: QuotaLedger, batch_id: str,
                   thread_id: str) -> list[dict]:
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    return [r for r in rows
            if r.get("task") == TASK_ID and r.get("batch_id") == batch_id
            and (r.get("thread_id") or "") == thread_id]


def _derive_metrics(outcome: Any, events: list[dict],
                    scored: dict) -> dict:
    rounds, productive = _productive_rounds(events)
    # birth 轮：内环直接给出（AgenticOutcome.extra 自带 born_at_round）。
    # 兜底：从 trace 的追授轮反推——tail 只在出生校验通过后才追授，故
    # 首个 phase="verify_repair" 的模型轮恰是出生轮 + 1。
    born = (outcome.extra or {}).get("born_at_round")
    tail_rows = [e for e in events
                 if e.get("tool") == MODEL_ROUND_TOOL
                 and (e.get("args") or {}).get("phase") == "verify_repair"]
    if born is None and tail_rows:
        first_tail = tail_rows[0].get("round")
        if isinstance(first_tail, int):
            born = first_tail - 1
    run_expr_ok = sum(
        1 for e in events
        if e.get("tool") == "run_expr"
        and (e.get("policy_decision") or {}).get("decision") == "allow"
        and _obs_nonempty(e))
    total_rounds = len(rounds)
    ratio = (round(productive / total_rounds, 6)
             if total_rounds else None)
    score = scored.get("score") or {}
    checks = score.get("checks") or []
    witness_12 = {c.get("check"): bool(c.get("ok")) for c in checks}
    sentinel_count = sum(1 for n in SENTINEL_CHECKS
                         if not witness_12.get(n, False))
    negative_physical = sum(1 for n in KINEMATIC_CHECKS
                            if not witness_12.get(n, False))
    return {
        "rounds": total_rounds, "productive_rounds": productive,
        "effective_step_ratio": ratio,
        "rounds_to_first_birth": born,
        "rounds_birth_to_rc0": (total_rounds - born
                                if born is not None else None),
        "tail_rounds": len(tail_rows),
        "run_expr_ok_calls": run_expr_ok,
        "final_rc": scored.get("rc"),
        "witness_12": witness_12,
        "sentinel_count": sentinel_count,
        "negative_physical": negative_physical,
        "round_detail": rounds,
    }


def cmd_run(wall: str, batch_id: str) -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("拒绝裸跑付费墙：需要 SIMPRAXIS_ALLOW_PAID=1（硬规则 2 双守卫）",
              file=sys.stderr)
        return 2
    if batch_id != BATCH_ID:
        raise SystemExit(f"批次号必须是单票 {BATCH_ID}（拆票即 "
                         "BLOCKED_BATCH_APPROVAL_MISSING）：{batch_id}")
    root = fc.freeze_root()
    verify_freeze(root)
    lock = verify_criteria_lock()
    grant_strings = retest_grants(wall)
    grants = parse_grant_strings(grant_strings)

    stamp = utc_stamp()
    thread_id = f"p26-{wall.lower()}-{stamp}"
    run_dir = fc.ensure_dir(retest_root() / "runs" / wall / stamp)
    work = fc.ensure_dir(run_dir / "work")
    text = task_text(wall)
    fc.atomic_write_bytes(run_dir / "task_prompt.txt", text.encode("utf-8"))
    meta = {
        "schema": "simpraxis.phase26.t4_run_meta.v1",
        "wall": wall, "batch_id": batch_id, "thread_id": thread_id,
        "started_iso": utc_iso(), "subcap": SUBCAPS[wall],
        "envelope": HARD_ENVELOPE, "reserve": RESERVE,
        "model": {"coding_debug": coding_debug_model_id()},
        "agentic_config": {
            "verify_repair_tail": CFG.verify_repair_tail,
            "verify_repair_tail_rounds": CFG.verify_repair_tail_rounds,
            "run_expr": CFG.run_expr,
            "tolerant_patch": CFG.tolerant_patch,
            "scratchpad": CFG.scratchpad,
        },
        "grants": list(grant_strings),
        "task_prompt_sha256": fc.sha256_file(run_dir / "task_prompt.txt"),
        "task_prompt_bytes": len(text.encode("utf-8")),
        "criteria_sha256": lock["criteria_sha256"],
        "image": {"id": IMAGE_ID, "repo_digest": IMAGE_REPODIGEST},
        "gate_hashes": fc.gate_hashes(),
        "callcache_enabled": False,
    }
    fc.write_json(run_dir / "run_meta.json", meta)
    print(f"[retest] {wall} 开跑 batch={batch_id} thread={thread_id} "
          f"subcap={SUBCAPS[wall]} run_dir={run_dir}", flush=True)

    ledger = phase26_ledger()
    gateway = ModelGateway(ledger=ledger)
    budget = BudgetTracker(
        step_cap=0, token_cap=0, round_cap=SUBCAPS[wall],
        verify_repair_tail_rounds=(TAIL_ROUNDS
                                   if CFG.verify_repair_tail else 0))
    tracer = TraceStore(run_dir / "traces", thread_id)
    backend = SpikeWallBackend(wall, freeze_dir=root)

    with GpuSampler(run_dir / "gpu_mib.csv") as gpu_sampler, \
            billing_context(task=TASK_ID, batch_id=batch_id,
                            thread_id=thread_id):
        outcome = run_agentic_inner_loop(
            workdir=work, task=text, grants=grants,
            gateway=gateway, budget=budget, tracer=tracer,
            thread_id=thread_id, run_backend=backend,
            probe_registry=None, recall_port=None,
            call_cache=None, nonprod=False,
            seq_provider=build_seq_provider(),
            agentic_config=CFG)

    residue = docker_label_residue()
    if residue:
        raise SystemExit(f"loop 终局后模型容器残留：{residue}（人工清理前不评分）")

    outcome_rec = {
        "kind": outcome.kind, "value": outcome.value, "reason": outcome.reason,
        "smolagents_state": outcome.smolagents_state,
        "rounds_used": outcome.rounds_used, "steps_used": outcome.steps_used,
        "tokens_used": outcome.tokens_used, "cache_hits": outcome.cache_hits,
        "empty_responses": outcome.empty_responses,
        "supplier_errors": outcome.supplier_errors,
        "trace_path": str(outcome.trace_path),
        "is_failure_artifact": outcome.is_failure_artifact,
        "extra": dict(outcome.extra or {}),
    }
    fc.write_json(run_dir / "outcome.json", outcome_rec)
    print(f"[retest] loop 终局 kind={outcome.kind} "
          f"rounds={outcome.rounds_used} steps={outcome.steps_used}", flush=True)

    scored = _score_once(wall, root, work)
    score_dir = fc.ensure_dir(run_dir / "score")
    fc.atomic_write_bytes(score_dir / "stdout.log",
                          scored["stdout"].encode("utf-8"))
    fc.atomic_write_bytes(score_dir / "stderr.log",
                          scored["stderr"].encode("utf-8"))
    if scored["score"] is not None:
        fc.write_json(score_dir / "score.json", scored["score"])

    residue_after = docker_label_residue()
    if residue_after:
        raise SystemExit(f"评分后容器残留：{residue_after}（硬规则 5）")
    gpu_final = nvidia_used_mib()

    events = _load_trace_events(Path(outcome.trace_path))
    metrics = _derive_metrics(outcome, events, scored)
    with open(run_dir / "rounds.jsonl", "w", encoding="utf-8") as fh:
        for rec in metrics.pop("round_detail"):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    seq_rows = _identity_rows(ledger, batch_id, thread_id)
    n_calls = len(seq_rows)
    metrics["paid_calls"] = n_calls
    metrics["ledger_seq"] = [r.get("seq") for r in seq_rows]
    score = scored.get("score") or {}
    wall_result = {
        "schema": WALL_RESULT_SCHEMA,
        "wall": wall, "batch_id": batch_id, "thread_id": thread_id,
        "ts_iso": utc_iso(), "run_dir": str(run_dir),
        "subcap": SUBCAPS[wall], "envelope": HARD_ENVELOPE,
        "loop_kind": outcome.kind,
        "supplier_errors": outcome.supplier_errors,
        "empty_responses": outcome.empty_responses,
        "cache_hits": outcome.cache_hits,
        "scorer_timed_out": scored["timed_out"],
        "score_verdict": score.get("verdict"),
        "checks_n": len(score.get("checks") or []),
        "b_cap": score.get("b_cap"),
        "infra_failure": outcome.kind == "supplier_error",
        "cap_exhausted": n_calls >= SUBCAPS[wall],
        "gpu_max_mib_while_running": gpu_sampler.max_mib,
        "gpu_final_mib": gpu_final,
        "gpu_closeout_ok": gpu_final <= GPU_CLOSEOUT_MIB,
        **metrics,
    }
    # 判定（本墙）：判据见 VERDICT_CRITERIA_V3.md §1/§2。
    if wall == WALL_A:
        born = wall_result["rounds_to_first_birth"]
        wall_result["passed"] = bool(
            born is not None and born <= BIRTH_ROUND_LIMIT
            and wall_result["final_rc"] == 0
            and wall_result["run_expr_ok_calls"] >= 1
            and wall_result["tail_rounds"] >= 1)
        wall_result["fail_reasons"] = [
            name for name, cond in (
                ("birth_round_absent_or_gt_limit",
                 born is None or born > BIRTH_ROUND_LIMIT),
                ("final_rc_not_zero", wall_result["final_rc"] != 0),
                ("no_successful_run_expr", wall_result["run_expr_ok_calls"] < 1),
                ("no_verify_repair_round", wall_result["tail_rounds"] < 1),
            ) if cond]
    else:
        w12 = wall_result["witness_12"]
        wall_result["passed"] = bool(
            scored["rc"] == 0 and score.get("verdict") == "pass"
            and len(w12) == 12 and all(w12.values())
            and wall_result["sentinel_count"] == 0
            and wall_result["negative_physical"] == 0
            and n_calls <= CALLS_TO_RC0_LIMIT_B)
        wall_result["fail_reasons"] = [
            name for name, cond in (
                ("rc_or_score_verdict", not (scored["rc"] == 0
                                             and score.get("verdict") == "pass")),
                ("witness_not_12", len(w12) != 12),
                ("witness_item_failed", not all(w12.values())),
                ("sentinel", wall_result["sentinel_count"] != 0),
                ("negative_physical", wall_result["negative_physical"] != 0),
                ("calls_to_rc0_gt_15", n_calls > CALLS_TO_RC0_LIMIT_B),
            ) if cond]
    fc.write_json(run_dir / "wall_result_v3.json", wall_result)
    print(json.dumps(wall_result, indent=1, ensure_ascii=False))
    print(f"[retest] {wall} 完成：passed={wall_result['passed']} "
          f"calls={n_calls}/{SUBCAPS[wall]} gpu_final={gpu_final}MiB",
          flush=True)
    if gpu_final > GPU_CLOSEOUT_MIB:
        raise SystemExit(f"收尾显存 {gpu_final} > {GPU_CLOSEOUT_MIB} MiB"
                         "（硬规则 5）；不擅杀他方进程，STOP 人工核")
    return 0


# --------------------------------------------------------------- 判定/聚合

def _latest_wall_result(wall: str) -> dict | None:
    base = retest_root() / "runs" / wall
    if not base.is_dir():
        return None
    cands = sorted(base.glob("*/wall_result_v3.json"))
    return _read_json(cands[-1]) if cands else None


def judge_wall(wall: str) -> tuple[bool, str]:
    rec = _latest_wall_result(wall)
    if rec is None:
        return False, f"{wall}: 无 wall_result_v3.json"
    ok = bool(rec.get("passed"))
    reasons = rec.get("fail_reasons") or []
    return ok, f"{wall}: passed={ok}" + (f" reasons={reasons}" if reasons else "")


def cmd_judge(wall: str) -> int:
    ok, msg = judge_wall(wall)
    print(msg)
    return 0 if ok else 1


def _pooled_ratio(a: dict | None, b: dict | None) -> float | None:
    rounds = sum((r or {}).get("rounds", 0) or 0 for r in (a, b))
    productive = sum((r or {}).get("productive_rounds", 0) or 0
                     for r in (a, b))
    return round(productive / rounds, 6) if rounds else None


def compute_verdict(rec_a: dict | None, rec_b: dict | None,
                    *, pool_used: int) -> tuple[str, str, float | None]:
    """§4 三态映射（短路）。INCONCLUSIVE 是窄口：基础设施故障 + 储备耗尽。"""
    ratio = _pooled_ratio(rec_a, rec_b)
    both = bool(rec_a and rec_b
                and rec_a.get("passed") and rec_b.get("passed"))
    if both and ratio is not None and ratio >= EFFECTIVE_RATIO_FLOOR:
        return V_ESTABLISHED, "两墙全过且 ratio>=0.80", ratio
    infra = any(bool((r or {}).get("infra_failure")) for r in (rec_a, rec_b))
    if not both and infra and pool_used >= HARD_ENVELOPE:
        return (V_INCONCLUSIVE,
                "基础设施故障且共享池已用满 28（储备耗尽）", ratio)
    reasons = []
    if not both:
        reasons.append("墙门未全过")
    if ratio is None or ratio < EFFECTIVE_RATIO_FLOOR:
        reasons.append(f"ratio={ratio} < {EFFECTIVE_RATIO_FLOOR}")
    return V_NOT_ESTABLISHED, "；".join(reasons) or "未过", ratio


def cmd_combine() -> int:
    rec_a = _latest_wall_result(WALL_A)
    rec_b = _latest_wall_result(WALL_B)
    missing = [w for w, r in ((WALL_A, rec_a), (WALL_B, rec_b)) if r is None]
    if missing:
        raise SystemExit(f"缺墙结果，无法聚合：{missing}")
    ledger = phase26_ledger()
    rows = _ledger_task_rows()
    pool_used = len(rows)
    verdict, reason, ratio = compute_verdict(rec_a, rec_b, pool_used=pool_used)
    lock = verify_criteria_lock()
    baseline = (_read_json(retest_root() / "baseline_effective_ratio.json")
                if (retest_root() / "baseline_effective_ratio.json").is_file()
                else {})
    payload = {
        "schema": VERDICT_SCHEMA,
        "ts_iso": utc_iso(),
        "batch": BATCH_ID,
        "criteria_sha256": lock["criteria_sha256"],
        "model": {"coding_debug": coding_debug_model_id()},
        "agentic_config": {
            "verify_repair_tail": CFG.verify_repair_tail,
            "verify_repair_tail_rounds": CFG.verify_repair_tail_rounds,
            "run_expr": CFG.run_expr,
            "tolerant_patch": CFG.tolerant_patch,
            "scratchpad": CFG.scratchpad,
        },
        "wallA": {
            "subcap": SUBCAPS[WALL_A], "paid_calls": rec_a["paid_calls"],
            "rounds_to_first_birth": rec_a["rounds_to_first_birth"],
            "rounds_birth_to_rc0": rec_a["rounds_birth_to_rc0"],
            "final_rc": rec_a["final_rc"],
            "tail_granted": rec_a["tail_rounds"] >= 1,
            "run_expr_ok_calls": rec_a["run_expr_ok_calls"],
            "effective_step_ratio": rec_a["effective_step_ratio"],
            "passed": rec_a["passed"], "fail_reasons": rec_a["fail_reasons"],
            "cap_exhausted": rec_a["cap_exhausted"],
            "run_dir": rec_a["run_dir"],
        },
        "wallB": {
            "subcap": SUBCAPS[WALL_B], "paid_calls": rec_b["paid_calls"],
            "witness_12": rec_b["witness_12"],
            "sentinel_count": rec_b["sentinel_count"],
            "negative_physical": rec_b["negative_physical"],
            "calls_to_rc0": rec_b["paid_calls"],
            "effective_step_ratio": rec_b["effective_step_ratio"],
            "passed": rec_b["passed"], "fail_reasons": rec_b["fail_reasons"],
            "cap_exhausted": rec_b["cap_exhausted"],
            "b_cap": rec_b.get("b_cap"),
            "run_dir": rec_b["run_dir"],
        },
        "pool": {"base": sum(SUBCAPS.values()), "reserve": RESERVE,
                 "used": pool_used, "envelope": HARD_ENVELOPE},
        "pooled_effective_step_ratio": ratio,
        "verdict": verdict, "verdict_reason": reason,
        "baseline_effective_ratio": {
            "phase25_recorded": 0.36,
            "recomputed": baseline.get("recomputed"),
            "deviation_registered": "D26-8a",
        },
        "hard_rule_7_attestation": "未翻案：判据预注册于首呼前，跑后未改。",
    }
    out = retest_root() / "verdict_v3.json"
    fc.write_json(out, payload)
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"[retest] 判决：{out}")
    return 0


def cmd_judge_all() -> int:
    rec_a = _latest_wall_result(WALL_A)
    rec_b = _latest_wall_result(WALL_B)
    pool_used = len(_ledger_task_rows())
    verdict, reason, ratio = compute_verdict(rec_a, rec_b,
                                             pool_used=pool_used)
    lock_state = "OK"
    try:
        lock = verify_criteria_lock()
        # AC-T4-3 后半：判决件里的 criteria_sha256 必须与 LOCK 内值相等
        # （判决件与判据绑定，换判据即换判决）。
        vp = retest_root() / "verdict_v3.json"
        if vp.is_file():
            got = _read_json(vp).get("criteria_sha256")
            if got != lock["criteria_sha256"]:
                lock_state = (f"FAIL(verdict_v3.criteria_sha256={got} != "
                              f"LOCK {lock['criteria_sha256']})")
    except SystemExit as exc:
        lock_state = f"FAIL({exc})"
    print(f"verdict={verdict} ratio={ratio} criteria_lock={lock_state} "
          f"reason={reason}")
    return 0 if lock_state == "OK" else 1


# 账外批次实耗（账行看不见的部分，人工对账的唯一真值）。
# P26-B0 批准 4 次但实发 5 次：``--validate-models`` 走 ``_post_messages``，
# 对 HTTP 200 空内容最多重试 5 次，每次重试都是真付费 POST（D26-8b 登记项）。
OFF_LEDGER_ACTUALS = {
    "P26-B0": {"approved": 4, "posts_used": 5, "kind": "validate-models",
               "outcome": "FAIL:EmptyResponseError（macro_ideation 连续 5 次 "
                          "HTTP200 空回答；max_tokens=8 被思维链吃光）",
               "evidence": "runs/model_validate/20261103-094723/FAILURE.json"},
    "P26-B0b": {"approved": 1, "posts_used": 1, "kind": "coding_probe",
                "outcome": "FAIL:empty_text（200/type=message/回显一致，"
                           "output_tokens=8 恰等于上限）",
                "evidence": "walls/retest/probe_coding_debug_"
                            "20261103T015937Z.json"},
    "P26-B0c": {"approved": 1, "posts_used": 1, "kind": "coding_probe",
                "outcome": "OK:pong（content_types=[thinking,text]，"
                           "output_tokens=32）",
                "evidence": "walls/retest/probe_coding_debug_"
                            "20261103T020156Z.json"},
}


def offledger_actuals() -> dict:
    """逐批回填「批准件是否在场」并原样带出实耗。"""
    aprovals = phase26_root() / "quota" / "approvals"
    out = {}
    for bid, rec in OFF_LEDGER_ACTUALS.items():
        out[bid] = {**rec,
                    "approval_present": (aprovals / f"{bid}.json").is_file()}
    return out


def cmd_audit_ledger() -> int:
    ledger = phase26_ledger()
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    task_rows = [r for r in rows if r.get("task") == TASK_ID]
    seqs = [int(r["seq"]) for r in rows if isinstance(r.get("seq"), int)]
    contiguous = seqs == list(range(1, len(seqs) + 1))
    by_task: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_batch: dict[str, int] = {}
    for r in task_rows:
        by_task[str(r.get("task"))] = by_task.get(str(r.get("task")), 0) + 1
        by_stage[str(r.get("stage"))] = by_stage.get(str(r.get("stage")), 0) + 1
        by_batch[str(r.get("batch_id"))] = by_batch.get(
            str(r.get("batch_id")), 0) + 1
    snap_p = retest_root() / "manifests" / "sealed_ledger_snapshot.json"
    sealed_untouched = None
    sealed_detail = {}
    if snap_p.is_file():
        snap = _read_json(snap_p)["paths"]
        sealed_untouched = True
        for path, rec in snap.items():
            p = Path(path)
            now_exists = p.is_file()
            now_mtime = int(p.stat().st_mtime) if now_exists else None
            changed = (now_exists != rec["exists"]
                       or now_mtime != rec["mtime"])
            sealed_detail[path] = {"before": rec,
                                   "after": {"exists": now_exists,
                                             "mtime": now_mtime},
                                   "changed": changed}
            if changed:
                sealed_untouched = False
    ticket = phase26_root() / "quota" / "approvals" / f"{OFFLEDGER_BATCH}.json"
    payload = {
        "schema": AUDIT_SCHEMA,
        "ts_iso": utc_iso(),
        "rows_total": len(rows),
        "rows_task4": len(task_rows),
        "envelope": HARD_ENVELOPE,
        "within_envelope": len(task_rows) <= HARD_ENVELOPE,
        "seq_contiguous": contiguous,
        "seq_min": min(seqs) if seqs else None,
        "seq_max": max(seqs) if seqs else None,
        "by_task": by_task, "by_stage": by_stage, "by_batch": by_batch,
        "sealed_untouched": sealed_untouched,
        "sealed_detail": sealed_detail,
        "offledger_validate": (OFFLEDGER_BATCH if ticket.is_file() else None),
        "offledger_batches": offledger_actuals(),
        "offledger_total_posts": sum(
            r["posts_used"] for r in offledger_actuals().values()),
        "offledger_note": ("账外批次不写账行，账口径看不见，须人工对账；"
                           "各批 approved 与 posts_used 逐批如实登记（见 "
                           "offledger_batches）"),
    }
    out = retest_root() / "ledger_audit.json"
    fc.write_json(out, payload)
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    ok = (payload["within_envelope"] and contiguous
          and sealed_untouched is not False)
    return 0 if ok else 1


# --------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wall", choices=list(WALLS))
    ap.add_argument("--batch", default=BATCH_ID)
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--probe-coding", action="store_true")
    ap.add_argument("--probe-batch", default=PROBE_BATCH)
    ap.add_argument("--probe-max-tokens", type=int, default=8)
    ap.add_argument("--recompute-baseline", action="store_true")
    ap.add_argument("--write-criteria", action="store_true")
    ap.add_argument("--seal-criteria", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--judge-all", action="store_true")
    ap.add_argument("--combine-v3", action="store_true")
    ap.add_argument("--audit-ledger", action="store_true")
    args = ap.parse_args(argv)

    if args.preflight:
        return cmd_preflight(args.wall)
    if args.probe_coding:
        return cmd_probe_coding(args.probe_batch, args.probe_max_tokens)
    if args.recompute_baseline:
        return cmd_recompute_baseline()
    if args.write_criteria:
        return cmd_write_criteria()
    if args.seal_criteria:
        return cmd_seal_criteria()
    if args.combine_v3:
        return cmd_combine()
    if args.judge_all:
        return cmd_judge_all()
    if args.audit_ledger:
        return cmd_audit_ledger()
    if args.judge:
        if not args.wall:
            ap.error("--judge 必须给 --wall")
        return cmd_judge(args.wall)
    if not args.wall:
        ap.error("非聚合模式必须给 --wall")
    return cmd_run(args.wall, args.batch)


if __name__ == "__main__":
    raise SystemExit(main())
