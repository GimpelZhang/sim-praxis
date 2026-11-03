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


# --- Task 4b：第二次预注册（STOP-4a 用户裁决 (b)）=========================
#
# 用户 2026-11-03 裁决「同墙同帽换预注册再战一次」：新票据 + 新判据 sha +
# 新批次号，且**不得**称为本批 V3 的「重跑」。三条必须同时满足，缺一即拒：
#   1) 新目录（`walls/retest_v4/`）——V3 的 runs/verdict/LOCK 一字节不动；
#   2) 新批次号（`P26-B1b`）——账行按批隔离，V3 的 20 行不计入新信封；
#   3) 新判据文件与 LOCK——首呼前封存，sha 与 V3 不同。
#
# 唯一被允许改变的**仪器**项：出生闸的入口脚本候选名。冻结墙 A 任务书要求
# 交付 `agent_solution.py`，而 V3 的出生闸只看 `.entrypoint.py`（全墙任务书
# 无此词），故出生标记与追授尾结构上永不触发（D26-8d）。V4 把候选名作为
# campaign 参数注入，`loop.py` 的缺省仍是 `(".entrypoint.py",)`，V3 行为逐字不变。
#
# 明确**不**改变的项（预注册声明，防仪器修复滑成能力放水）：F1–F4 四开关、
# 授予串、子帽 16/9/3、信封 28、模型 ID、判据全部阈值（8 轮 / 12 项 /
# calls_to_rc0<=15 / 合并占比 0.80）。`run_expr` 的 AST 面**不放宽**：判据只要
# ≥1 次成功调用，`print(1+1)` 即可满足。
class Campaign:
    """一次预注册的命名空间（目录 / 票据 / 判据 / 入口候选名）。"""

    __slots__ = ("key", "batch_id", "subdir", "criteria_name",
                 "verdict_name", "wall_result_name", "entrypoint_names")

    def __init__(self, key: str, batch_id: str, subdir: str,
                 criteria_name: str, verdict_name: str,
                 wall_result_name: str, entrypoint_names: "tuple[str, ...]"):
        self.key = key
        self.batch_id = batch_id
        self.subdir = subdir
        self.criteria_name = criteria_name
        self.verdict_name = verdict_name
        self.wall_result_name = wall_result_name
        self.entrypoint_names = entrypoint_names

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return (f"Campaign({self.key!r}, batch={self.batch_id!r}, "
                f"subdir={self.subdir!r}, entry={self.entrypoint_names!r})")


CAMPAIGNS = {
    "v3": Campaign("v3", "P26-B1", "retest", "VERDICT_CRITERIA_V3.md",
                   "verdict_v3.json", "wall_result_v3.json",
                   (".entrypoint.py",)),
    "v4": Campaign("v4", "P26-B1b", "retest_v4", "VERDICT_CRITERIA_V4.md",
                   "verdict_v4.json", "wall_result_v4.json",
                   ("agent_solution.py",)),
}

# 进程级当前 campaign。只在 CLI 解析后由 :func:`set_campaign` 改一次；库内
# 直接调用（含既有单测）恒为 V3 ⇒ 全部路径与 V3 逐字节相同。
_CAMPAIGN = CAMPAIGNS["v3"]


def campaign() -> Campaign:
    return _CAMPAIGN


def set_campaign(key: str) -> Campaign:
    global _CAMPAIGN
    if key not in CAMPAIGNS:
        raise SystemExit(f"未知 campaign {key!r}（可选：{sorted(CAMPAIGNS)}）")
    _CAMPAIGN = CAMPAIGNS[key]
    return _CAMPAIGN


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
    """当前 campaign 的根目录。V3 恒 ``walls/retest``（与既有行为逐字节相同）。"""
    return phase26_root() / "walls" / campaign().subdir


def criteria_path() -> Path:
    return retest_root() / campaign().criteria_name


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


# --- V4 判据补丁（Task 4b；STOP-4a 裁决 (b) 授权的**唯一**增量）------------
#
# 每条 (old, new, 期望命中次数)：命中次数不符即 `--write-criteria` 熔断，
# 防止 V3 正文后续漂移使 V4 的「只差出生件名」声明失真。
# `{entry}` 由 `campaign().entrypoint_names[0]` 代入（单真值，不在此抄字面量）。
V4_CRITERIA_PATCHES = (
    ("# Phase 2.6 Task 4 三态判据 v3（首呼前冻结；与此后脚本机判逐条同义）",
     "# Phase 2.6 Task 4 三态判据 v4（首呼前冻结；与此后脚本机判逐条同义）\n\n"
     "> **v4 由来**：2026-11-03 用户经 STOP-4a 裁决「同墙同帽换预注册再战一次」。\n"
     "> 本文件是**第二次预注册**（票据 `P26-B1b`，目录 `walls/retest_v4/`），\n"
     "> **不是** v3（`P26-B1` / `walls/retest/`）的「重跑」——v3 的判据、判决件、\n"
     "> 两墙 run 目录与三签件全部原样冻结只读，一字未改。\n"
     "> 相对 v3 的**唯一**判据增量是 §1 出生件的文件名（`.entrypoint.py` →\n"
     "> `{entry}`）：冻结墙任务书要求交付 `agent_solution.py`，全墙任务书\n"
     "> 无 `.entrypoint.py` 一词，故 v3 的出生闸与追授尾**结构上永不触发**\n"
     "> （D26-8d）。其余判据（墙 A 四项 / 墙 B 12 项 / 哨兵与运动学两轴 /\n"
     "> 占比 0.80 / 三态映射 / 预算形状）与 v3 逐字相同；**未**放宽 `run_expr`\n"
     "> 的 AST 面（判据只要 >=1 次成功调用），**未**改任何阈值。", 1),
    ("- `rounds_to_first_birth <= 8`（入口脚本 `.entrypoint.py` 首次同时满足",
     "- `rounds_to_first_birth <= 8`（入口脚本 `{entry}` 首次同时满足", 1),
    ("- 票据 `P26-B1`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3",
     "- 票据 `P26-B1b`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3", 1),
)


def criteria_body() -> str:
    """当前 campaign 的判据正文。V3 恒为 :data:`CRITERIA_BODY` 原文。"""
    text = CRITERIA_BODY
    if campaign().key == "v3":
        return text
    entry = campaign().entrypoint_names[0]
    for old, new, want in V4_CRITERIA_PATCHES:
        got = text.count(old)
        if got != want:
            raise SystemExit(
                f"V4 判据补丁锚点漂移（命中 {got} 次，期望 {want}）：{old!r}")
        text = text.replace(old, new.replace("{entry}", entry))
    if text == CRITERIA_BODY:  # pragma: no cover - 防御：补丁不得空转
        raise SystemExit("V4 判据补丁未生效")
    return text


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------- 基线复算

def _obs_nonempty(row: dict) -> bool:
    """trace 不存观察原文：可见性取 obs_head/obs_tail（切片非空即非空）。"""
    return bool((row.get("obs_head") or "").strip()
                or (row.get("obs_tail") or "").strip())


def _productive_rounds(events: list[dict],
                       born_at_round: int | None = None
                       ) -> tuple[list[dict], int]:
    """按 §3 预注册定义从 trace 行复算每轮 productive 标记。

    返回 (rounds, productive_count)。模型轮行取 ``__model_round__``（轮号
    字段 ``round``，1 起）；其余 ``__`` 前缀内部控制行（``__answer__`` /
    ``__condense__``）不参与判定（既有 trace 语义：控制行不是工具调用）。
    工具行字段：``policy_decision`` / ``obs_head`` / ``obs_tail``。

    判据 §3 是 **两择一**：(a) 该轮有 allow 且观察非空的工具调用；(b) 该轮
    首次使入口脚本落盘（``born_at_round``）。两条都必须在代码里成立，否则
    实现就窄于预注册文本。``born_at_round=None``（未出生）时 (b) 不触发。
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
    # 判据 §3 条款 (b)：出生轮本身计产出（即便该轮没有成功工具调用）。
    if born_at_round is not None:
        rec = index.get(born_at_round)
        if rec is not None and not rec["productive"]:
            rec["productive"] = True
            rec["productive_reason"] = "birth_round"
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
        "definition": (f"§T4.3 预注册定义（{campaign().criteria_name} §3 逐字"
                       + ("）" if campaign().key == "v3"
                          else "；与 V3 同名条款逐字相同）")),
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
    text = criteria_body().replace("__RBASE__", f"{rbase}")
    p = criteria_path()
    fc.atomic_write_bytes(p, text.encode("utf-8"))
    print(f"[retest] 判据已落盘：{p}")
    return 0


def _ledger_task_rows() -> list[dict]:
    ledger = phase26_ledger()
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    return [r for r in rows if r.get("task") == TASK_ID]


def _ledger_campaign_rows() -> list[dict]:
    """当前 campaign 批次号下的实呼行（Task4b 批间隔离的唯一口径）。

    V3（``P26-B1``）与 V4（``P26-B1b``）同属 task=4，账行靠 ``batch`` 字段区分。
    历史行的 ``batch`` 命名不统一（``P26-B1`` / ``p26-b1`` 皆有），故按大小写
    不敏感等值匹配。用途三处：``--seal-criteria`` 的「首呼已发生」闸、
    ``--combine``/``--judge-all`` 的共享池用量（§T4.10：V4 自带 28 信封，
    V3 的 20 行不并入；对 V3 而言本批即唯一 task=4 批次，取值与
    :func:`_ledger_task_rows` 相同）。
    """
    want = campaign().batch_id.lower()
    out = []
    for r in _ledger_task_rows():
        got = r.get("batch") or r.get("batch_id") or ""
        if isinstance(got, str) and got.lower() == want:
            out.append(r)
    return out


def _criteria_lock_record() -> dict:
    p = criteria_path()
    st = p.stat()
    return {
        "schema": CRITERIA_LOCK_SCHEMA,
        "campaign": campaign().key,
        "criteria_path": str(p),
        "criteria_sha256": fc.sha256_file(p),
        "criteria_mtime": int(st.st_mtime),
        "criteria_mode": oct(st.st_mode & 0o777),
        "locked_iso": utc_iso(),
        "batch_id": campaign().batch_id,
        "entrypoint_names": list(campaign().entrypoint_names),
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
    if _ledger_campaign_rows():
        raise SystemExit(f"账上已有 {campaign().batch_id} 的 task=4 实呼行："
                         "首呼已发生，禁止再封判据")
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
    ticket_p = (phase26_root() / "quota" / "approvals"
                / f"{campaign().batch_id}.json")
    if not ticket_p.is_file():
        raise SystemExit(f"票据不存在：{ticket_p}")
    ticket = _read_json(ticket_p)
    expect = {"batch_id": campaign().batch_id, "task": TASK_ID,
              "stage": "coding_debug",
              "approved": HARD_ENVELOPE, "beyond_normal": False,
              "ledger_phase": 26}
    for key, want in expect.items():
        if ticket.get(key) != want:
            raise SystemExit(
                f"票据字段 {key} 不符：{ticket.get(key)!r} != {want!r}"
                "（第 2/4/6 道闸按此拒）")
    try:
        pf = ledger.preflight("coding_debug", task=TASK_ID,
                              batch_id=campaign().batch_id)
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

    # 防重放：本票只值 1 个 POST。标记在**发网前**落盘——即使进程在请求
    # 途中崩溃，票也已记为用尽，不允许第二次 --probe-coding 再烧一次。
    marker = retest_root() / f"probe_consumed_{batch}.json"
    if marker.is_file():
        print(f"[probe] 拒绝：{batch} 的 1 次额度已于 "
              f"{_read_json(marker).get('consumed_at_iso')} 用尽（防重放）",
              file=sys.stderr)
        return 2
    fc.write_json(marker, {
        "schema": PROBE_SCHEMA, "batch_id": batch,
        "consumed_at_iso": utc_iso(), "max_tokens": max_tokens,
        "note": "发网前落盘：崩溃也不得重放本票（approved=1）",
    })

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

# 评审 R9 残留（executor 第四轮复现）：冻结评分器在**自身进程内**
# ``importlib.exec_module`` 跑模型自著的 ``agent_solution.py``
# （wall_a_runner.py:93、wall_b_checker.py:56），``finish()`` 要到收尾才写
# ``wall_a-<ts>.json``。模型可抢先往同一个 rw 挂载的 ``/work/.score`` 写一份
# 伪造件再 ``os._exit(0)``；旧实现 ``sorted(glob("wall_*-*.json"))[-1]`` 按
# 字典序取最后一件，伪造名 ``wall_a-99999999T999999Z.json`` 必然当选。
#
# 这里用三条**廉价、可机判、fail-closed** 的必要条件收窄该信道：
#   ① 按墙别精确 glob（``wall_a-``/``wall_b-``），杂件一律不认；
#   ② **恰 1 件**——多件即「伪造件与真件并存」，说明评分目录被写脏，
#      一律判无效（不猜哪份是真的）；
#   ③ 件名内嵌时间戳必须落在 ``[评分开始-偏差, 评分结束+偏差]``——
#      直接杀掉 future-dated 的伪造名，也顺带挡住跨轮次的陈旧件。
#
# **残余（必须如实登记，不得假装闭合）**：模型若在同一窗口内写一个合法名
# 并 ``os._exit(0)``，三条仍会放行。该通道与 R9 之前**只看 ``final_rc==0``**
# 的通道同源同强度（``os._exit(0)`` 同样把退出码置 0），并非本修复引入；
# 真正的关闭点在「评分器进程外执行模型代码」，属冻结墙的重设计，须 Phase 3
# 新预注册（I4-1 禁止在 Task 4 内改墙）。
SCORE_ARTIFACT_PREFIX = {WALL_A: "wall_a", WALL_B: "wall_b"}
SCORE_TS_SKEW_S = 120.0
SCORE_TS_FMT = "%Y%m%dT%H%M%S"


def _score_ts_from_name(path: Path, wall: str) -> float | None:
    """从 ``wall_a-<YYYYmmddTHHMMSS>.json`` 解析 UTC 时间戳；不可解析即 None。"""
    prefix = SCORE_ARTIFACT_PREFIX[wall]
    name = path.name
    head, tail = f"{prefix}-", ".json"
    if not (name.startswith(head) and name.endswith(tail)):
        return None
    try:
        stem = name[len(head):-len(tail)]
        if stem.endswith("Z"):        # 冻结 runner 无 Z；容错接受显式 UTC 后缀
            stem = stem[:-1]
        dt = datetime.strptime(stem, SCORE_TS_FMT)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def select_score_file(wall: str, score_dir: Path, *,
                      window_start: float,
                      window_end: float) -> tuple[Path | None, str]:
    """在模型可写的 ``.score`` 目录里选出**唯一且有窗口证据**的评分件。

    返回 ``(path, "")`` 表示唯一候选且时间戳在窗口内；否则 ``(None, reason)``
    且 reason 非空——调用方必须把 None 当作「无评分件」，不许回退旧逻辑。
    """
    try:
        cands = sorted(score_dir.glob(f"{SCORE_ARTIFACT_PREFIX[wall]}-*.json"))
    except OSError:
        return None, "score_dir_unreadable"
    if not cands:
        return None, "score_artifact_absent"
    if len(cands) > 1:
        return None, f"score_artifact_not_unique({len(cands)})"
    ts = _score_ts_from_name(cands[0], wall)
    if ts is None:
        return None, "score_artifact_name_unparsable"
    if ts < window_start - SCORE_TS_SKEW_S or ts > window_end + SCORE_TS_SKEW_S:
        return None, "score_artifact_outside_window"
    return cands[0], ""


def _score_once(wall: str, root: Path, work: Path) -> dict:
    score_dir = work / ".score"
    if score_dir.exists():
        shutil.rmtree(score_dir)
    window_start = time.time()
    if wall == WALL_A:
        res = score_wall_a(work, fc.GATES_DIR / "wall_a", root,
                           timeout_s=240)
    else:
        res = score_wall_b(
            work, fc.GATES_DIR / "wall_b" / "wall_b_checker.py",
            root / "evidence_wall_b" / "pitcher_open_watertight.obj",
            timeout_s=180)
    window_end = time.time()
    picked, why = select_score_file(wall, score_dir,
                                    window_start=window_start,
                                    window_end=window_end)
    score = json.loads(picked.read_text(encoding="utf-8")) if picked else None
    return {"rc": res.rc, "timed_out": res.timed_out,
            "stdout": res.stdout.decode("utf-8", "replace"),
            "stderr": res.stderr.decode("utf-8", "replace"),
            "score": score,
            "score_file": picked.name if picked else None,
            "score_file_unique": picked is not None,
            "score_file_reason": why}


def _identity_rows(ledger: QuotaLedger, batch_id: str,
                   thread_id: str) -> list[dict]:
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    return [r for r in rows
            if r.get("task") == TASK_ID and r.get("batch_id") == batch_id
            and (r.get("thread_id") or "") == thread_id]


# D4-6「基础设施故障」的唯一机判：三类，各自要求**正面证据**，无证据即
# False（fail-closed）。这一格决定 INCONCLUSIVE 窄口是否打开，宁可漏判也
# 不可误判——把模型质量类失败（未出生/补丁失败/rc≠0）判成基础设施故障，
# 等于给「失败即不确定」开后门。
#
# 证据信道只有**我方产生**、且模型代码**不能伪造**的三路（评审 R2 + R7）：
#   (a) 网关/内环自写的 outcome.kind + reason（router.py 与 loop.py 构造）；
#   (b) 内核 OOM 击杀计数（cgroup v2 `memory.events::oom_kill`）的评分前后增量，
#       **且**评分器退出码为 137。只认 137 不认计数器会被模型伪造——冻结评分器
#       在容器/宿主内 `importlib.exec_module` 跑模型写的 `agent_solution.py`，
#       模型一行 `os._exit(137)` 就能把评分器进程退出码设成 137（R7，
#       2026-11-03 经 executor 独立复现：rc=137 但内核 oom_kill 增量为 0）。
#       计数器的**读点**见 `_cgroup_event_paths()`：本机 systemd cgroup 驱动下
#       容器 scope 是 `system.slice` 的子级、`docker.service` 的兄弟，读
#       `docker.service` 会假阴性（评审 R7-CGROUP-PATH，reviewer 实证）；
#   (c) /mnt 评分窗口内的最低剩余空间（harness 观测，评分前后各取一次）。
# 模型 stdout/stderr 是**不可信信道**：模型 print 一行 MemoryError/ENOSPC
# 不得把质量失败翻成基础设施故障，故本函数根本不接受这两个参数。
INFRA_HTTP_PATTERNS = (
    # router.py:1178 5xx 重试耗尽 → GatewayError("HTTP 5xx 重试 N 次仍失败：…")
    # 生产 reason 恒被 model.py:340 包成「供应侧错误（GatewayError）：HTTP …」，
    # 故**不得行首锚定**（评审 R1：^ 锚定使真实 5xx/429 永不命中，比修复前更差）。
    # 这两条短语只由我方 router 产出，子串匹配仍然很窄。
    re.compile(r"HTTP 5\d\d 重试 \d+ 次仍失败"),
    re.compile(r"HTTP 429 .*重试 \d+ 次仍失败"),
    # router.py:1155-1171 429 退避/等待预算耗尽 → GatewayPausedError(state)，
    # str() 形如 "GATEWAY PAUSED stage=… until … reason=429 退避预算耗尽，转长熔断"。
    # 配额长熔断（_is_quota_http）reason 是上游正文，不匹配这两串 → 不算 (a)。
    re.compile(r"GATEWAY PAUSED .*reason=.*429.*(退避预算耗尽|等待预算超限)"),
)
CONTAINER_OOM_EXIT_CODES = (137,)      # SIGKILL —— **必要不充分**信号（R7）
DISK_FLOOR_GIB = 1.0                   # 评分时点 /mnt 剩余低于此即视为磁盘写满
WALL_A_CHECKS_N = 12                   # 冻结 wall_a_runner.py 的检查项数（R9）


def _cgroup_event_paths() -> tuple[Path, ...]:
    """OOM 计数的读取点：docker 容器树 + 本进程所在会话 scope。

    容器评分（墙 A）的 OOM 击杀记在 `docker.service` 的 cgroup 上；宿主进程
    评分（墙 B）与本体进程被 OOM 击杀则记在本会话 scope 及其子树上。两处都
    读，任一在评分窗口内 `oom_kill` 增量 > 0 即算一次真实内核击杀。

    评审 R7-CGROUP-PATH（第四轮，reviewer 实证）：本机 docker 用 **systemd
    cgroup 驱动**（`docker info` → `CgroupDriver=systemd CgroupVersion=2`），
    容器 scope 落在 `/sys/fs/cgroup/system.slice/docker-<id>.scope`，是
    `system.slice` 的**直接子级**、`docker.service` 的**兄弟**——`docker.service`
    的 `memory.events` 不聚合它们，读它恒得 0。实测：`--memory 128m` 容器内存
    炸弹 → 容器 scope 与 `system.slice` 的 `oom_kill` 均 0→1，而
    `docker.service` 保持 0。旧读点是 D4-6(b) 的**假阴性**（真 OOM 判成质量失败），
    比误判更该修。

    故读点改为**分层聚合器 `system.slice`**（cgroup v2 的 `memory.events` 是
    层级累计，`system.slice` 覆盖 `docker.service` 与其全部 `docker-*.scope`），
    不再同时读 `docker.service`（它是 `system.slice` 的后代，并读会重复计数；
    `≥1` 阈值下重复无害，但少一路就少一处歧义）。`system.slice` 不可读时
    回退 `docker.service`（cgroupfs 驱动老路径）。再加**本进程所在 scope**
    覆盖墙 B 的宿主侧评分。
    """
    paths: list[Path] = []
    system_slice = Path("/sys/fs/cgroup/system.slice/memory.events")
    if system_slice.exists():
        paths.append(system_slice)
    else:
        paths.append(Path(
            "/sys/fs/cgroup/system.slice/docker.service/memory.events"))
    try:
        rel = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        for line in rel.splitlines():
            if line.startswith("0::"):
                sub = line[3:].strip()
                if sub and sub != "/":
                    paths.append(Path("/sys/fs/cgroup") / sub.lstrip("/")
                                 / "memory.events")
                break
    except OSError:
        pass
    return tuple(paths)


def _read_oom_kill_counts() -> dict[str, int]:
    """读各 cgroup 的 `oom_kill` 累计值；读不到的路径直接缺席（不猜 0）。"""
    out: dict[str, int] = {}
    for p in _cgroup_event_paths():
        try:
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                key, _, val = line.partition(" ")
                if key == "oom_kill":
                    out[str(p)] = int(val.strip())
                    break
        except (OSError, ValueError):
            continue
    return out


def _oom_kill_delta(before: dict[str, int] | None,
                    after: dict[str, int] | None) -> int | None:
    """评分窗口内的内核 OOM 击杀增量；任一端不可读即 None（fail-closed）。"""
    if not before or not after or set(before) != set(after):
        return None
    return sum(max(0, after[k] - before[k]) for k in before)


def classify_infra_failure(kind: str, reason: str, *,
                           scorer_rc: int | None = None,
                           oom_kill_delta: int | None = None,
                           disk_free_gib: float | None = None
                           ) -> tuple[bool, str]:
    """D4-6 (a) 网关 5xx/429 退避耗尽 / (b) 容器 OOM 被杀 / (c) 磁盘写满。

    返回 ``(是否基础设施故障, 命中类别或空串)``。判据只看**我方机器证据**，
    不看「分数低」之类的质量信号，也不看模型可控的 stdout/stderr。

    (b) 需要**双证据**：评分器退出码 137 **且**内核 `oom_kill` 计数在评分窗口
    内确有增量。单看退出码可被模型伪造（`os._exit(137)`，见 R7），单看计数
    无法归因到本次评分，二者缺一即 False（fail-closed）。
    """
    reason = reason or ""
    if kind == "supplier_error":
        for pat in INFRA_HTTP_PATTERNS:
            if pat.search(reason):
                return True, "gateway_5xx_429_exhausted"
    if (scorer_rc is not None and int(scorer_rc) in CONTAINER_OOM_EXIT_CODES
            and oom_kill_delta is not None and int(oom_kill_delta) >= 1):
        return True, "container_oom_or_killed"
    if disk_free_gib is not None and float(disk_free_gib) < DISK_FLOOR_GIB:
        return True, "disk_full"
    return False, ""


WALL_A_SCORE_SCHEMA = "simpraxis.phase25.wall_a_score.v1"


def wall_a_checks_all_ok(checks: Any) -> bool:
    """冻结 wall_a_runner.py 的 12 项检查必须**逐项** ``ok is True``。

    评审 R9 残留 2（MINOR）：只数条数会放过「12 条但全 False」的畸形件；
    判据 §1 写的是「全 12 项检查 PASS」，故逐项判定，且 12 个名字互不相同。
    """
    if not isinstance(checks, list) or len(checks) != WALL_A_CHECKS_N:
        return False
    names = []
    for item in checks:
        if not isinstance(item, dict) or item.get("ok") is not True:
            return False
        names.append(item.get("check"))
    if not all(isinstance(n, str) and n for n in names):
        return False
    return len(set(names)) == WALL_A_CHECKS_N


def wall_a_score_ok(scorer_rc: int | None, score: dict | None, *,
                    provenance_ok: bool) -> bool:
    """墙 A 的评分侧必要条件：件源可信 ∧ rc=0 ∧ schema/verdict ∧ 12 项全 PASS。

    评审 R9：判据 §1 括注「冻结 wall_a_runner.py 全 12 项检查 PASS」原先没落到
    机判，只看 ``final_rc==0``——而冻结评分器在进程内 ``importlib.exec_module``
    跑模型写的 ``agent_solution.py``，模型一行 ``os._exit(0)`` 就能让 rc 变 0 却
    一项检查都没跑。

    评审 R9 残留（executor 第四轮）：光看评分件的**内容**不够——件本身落在模型
    可写的 ``/work/.score``。故 ``provenance_ok`` 是**必填关键字参数**，由
    ``select_score_file()`` 给出（唯一候选 + 件名时间戳在评分窗口内）；不给默认
    值是为了让每个调用点都必须显式承担件源判断，杜绝「忘了传就放行」。

    残余通道（同窗口内写合法名 + ``os._exit(0)``）与 R9 之前的退出码通道同源，
    见 ``select_score_file`` 上方登记，须 Phase 3 墙重设计关闭。
    """
    if not provenance_ok:
        return False
    if scorer_rc is None or int(scorer_rc) != 0 or not isinstance(score, dict):
        return False
    return (score.get("schema") == WALL_A_SCORE_SCHEMA
            and score.get("verdict") == "pass"
            and wall_a_checks_all_ok(score.get("checks")))


def wall_a_passed(*, born_at_round: int | None, final_rc: int | None,
                  score_ok: bool, run_expr_ok_calls: int,
                  tail_rounds: int) -> bool:
    """墙 A 过闸的完整合取（判据 §1，五项缺一不可）。"""
    return bool(
        born_at_round is not None and int(born_at_round) <= BIRTH_ROUND_LIMIT
        and final_rc == 0 and bool(score_ok)
        and int(run_expr_ok_calls) >= 1 and int(tail_rounds) >= 1)


def _derive_metrics(outcome: Any, events: list[dict],
                    scored: dict) -> dict:
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
    # born 必须先算：判据 §3 条款 (b) 把它作为 productive 的第二判据。
    rounds, productive = _productive_rounds(events, born_at_round=born)
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


def _wall_attempt_records(wall: str) -> list[dict]:
    base = retest_root() / "runs" / wall
    if not base.is_dir():
        return []
    return [_read_json(p) for p in sorted(base.glob(f"*/{campaign().wall_result_name}"))]


def _unrecorded_attempts(wall: str) -> list[str]:
    """跑了但没落判决件的尝试（进程崩溃/被杀）。

    评审 R5：``{campaign().wall_result_name}`` 是唯一事实源时，一次已烧实呼却在写件前
    崩溃的运行不会计入重试额度。run 目录由 ``cmd_run`` 在守卫**之后**创建，
    故「有 run_meta.json 而无墙结果件」必然是已开跑未收口。
    """
    base = retest_root() / "runs" / wall
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if (d / "run_meta.json").is_file() and not (
                d / campaign().wall_result_name).is_file():
            out.append(str(d))
    return out


def _guard_single_run(wall: str) -> int:
    """I4-5 机判：同一墙只许一轮**非基础设施故障**的正式结果。

    D4-6 是唯一重跑口子：上一轮必须命中三类基础设施故障之一，且本墙与
    全批储备都未用尽（每墙至多 1 次重跑，全批重跑合计 ≤ RESERVE）。返回
    本次尝试序号（0 起），落进墙结果件的 ``attempt_index``。
    """
    recs = _wall_attempt_records(wall)
    unrecorded = _unrecorded_attempts(wall)
    if unrecorded:
        raise SystemExit(
            f"{wall} 存在已开跑但未落判决件的尝试：{unrecorded}。I4-5 要求先"
            "人工分诊（查该 run 的 rounds.jsonl / outcome.json 与账行 seq 区间，"
            "确认是否已消耗实呼），补记墙结果件或挂牌失败后再重跑。")
    if not recs:
        return 0
    non_infra = [r for r in recs if not r.get("infra_failure")]
    if non_infra:
        raise SystemExit(
            f"{wall} 已有非基础设施故障的正式结果：{non_infra[0].get('run_dir')}"
            f"（passed={non_infra[0].get('passed')}）。I4-5：判负不重跑同一墙；"
            "重测须新票据 + 新判据 sha + 新批次号，不得复用本批。")
    if len(recs) >= 2:
        raise SystemExit(f"{wall} 储备重跑额度已用尽（已有 {len(recs)} 次尝试）")
    retries = sum(max(0, len(_wall_attempt_records(w)) - 1) for w in WALLS)
    if retries + 1 > RESERVE:
        raise SystemExit(f"储备池 {RESERVE} 已耗尽（已重跑 {retries} 次）")
    return len(recs)


def cmd_run(wall: str, batch_id: str) -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("拒绝裸跑付费墙：需要 SIMPRAXIS_ALLOW_PAID=1（硬规则 2 双守卫）",
              file=sys.stderr)
        return 2
    if batch_id != campaign().batch_id:
        raise SystemExit(f"批次号必须是单票 {campaign().batch_id}（拆票即 "
                         "BLOCKED_BATCH_APPROVAL_MISSING）：{batch_id}")
    # 评审 R6：同批 --preflight 必须是机判前置，而不是操作员纪律。要求预检件
    # 在场、同批、且自证 0 付费（预检本身不得花过钱）。
    pf_p = retest_root() / "preflight.json"
    if not pf_p.is_file():
        raise SystemExit(
            f"缺少本批预检件 {pf_p}：先跑 --preflight（九件闸哈希 / 镜像 digest / "
            "票据 / 0 触网）再开跑（§T4.2 入口准则）。")
    pf = _read_json(pf_p)
    if str((pf.get("ticket") or {}).get("batch_id")) != batch_id:
        raise SystemExit(
            f"预检件批次不符：preflight={pf.get('ticket', {}).get('batch_id')!r} "
            f"vs 本次 {batch_id!r}")
    if pf.get("paid_calls") != 0:
        raise SystemExit(f"预检件自证 paid_calls={pf.get('paid_calls')!r} ≠ 0")
    root = fc.freeze_root()
    verify_freeze(root)
    lock = verify_criteria_lock()
    attempt_index = _guard_single_run(wall)
    grant_strings = retest_grants(wall)
    grants = parse_grant_strings(grant_strings)

    stamp = utc_stamp()
    thread_id = f"p26-{wall.lower()}-{stamp}"
    if (retest_root() / "runs" / wall / stamp).exists():
        raise SystemExit(f"运行目录已存在（时间戳碰撞）：{stamp}")
    run_dir = fc.ensure_dir(retest_root() / "runs" / wall / stamp)
    work = fc.ensure_dir(run_dir / "work")
    text = task_text(wall)
    fc.atomic_write_bytes(run_dir / "task_prompt.txt", text.encode("utf-8"))
    meta = {
        "schema": "simpraxis.phase26.t4_run_meta.v1",
        "campaign": campaign().key,
        "wall": wall, "batch_id": batch_id, "thread_id": thread_id,
        "started_iso": utc_iso(), "subcap": SUBCAPS[wall],
        "envelope": HARD_ENVELOPE, "reserve": RESERVE,
        "entrypoint_names": list(campaign().entrypoint_names),
        # F1 追授口径（§T4.10 预注册）：`round_cap` 与 V3 逐字相同（16/9），
        # 追授 ≤3 轮**会计入本墙 paid_calls 与全批 28 包络**；子帽不含追授，
        # 故两墙同时用满追授的理论上限为 31 > 28，实际由账本第 6 道闸在 28
        # 处截断（该情形的机判口径见判据 §4/§5，属预算结果非基础设施故障）。
        "tail_rounds": (TAIL_ROUNDS if CFG.verify_repair_tail else 0),
        "subcap_excludes_tail": True,
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
            agentic_config=CFG,
            entrypoint_names=campaign().entrypoint_names)

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

    # R7：OOM 判据的前置快照必须在评分前后各取一次——模型代码能改自己的
    # 退出码，改不了内核 oom_kill 计数器。
    # 磁盘同理（executor 第四轮 LOW）：评分**期间**写满才算基础设施故障，
    # 故窗口两端各取一次并取更低值，而不是只在评分结束后看一眼。
    oom_before = _read_oom_kill_counts()
    disk_free_before = shutil.disk_usage("/mnt").free
    scored = _score_once(wall, root, work)
    oom_kill_delta_at_score = _oom_kill_delta(oom_before, _read_oom_kill_counts())
    disk_free_gib_at_score = round(
        min(disk_free_before, shutil.disk_usage("/mnt").free) / (1 << 30), 2)
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
    # 磁盘写满只能由我方观测取证：模型 stdout/stderr 可自称 ENOSPC（评审 R2）。
    # 取值为评分窗口两端（`_score_once` 前后）的**更低值**，见上方快照注释。
    infra_failure, infra_class = classify_infra_failure(
        outcome.kind, str(outcome.reason or ""),
        scorer_rc=scored["rc"], oom_kill_delta=oom_kill_delta_at_score,
        disk_free_gib=disk_free_gib_at_score)
    wall_result = {
        "schema": WALL_RESULT_SCHEMA,
        "wall": wall, "batch_id": batch_id, "thread_id": thread_id,
        "ts_iso": utc_iso(), "run_dir": str(run_dir),
        "attempt_index": attempt_index,
        "subcap": SUBCAPS[wall], "envelope": HARD_ENVELOPE,
        "loop_kind": outcome.kind,
        "supplier_errors": outcome.supplier_errors,
        "empty_responses": outcome.empty_responses,
        "cache_hits": outcome.cache_hits,
        "scorer_timed_out": scored["timed_out"],
        "scorer_rc": scored["rc"],
        "oom_kill_delta_at_score": oom_kill_delta_at_score,
        "disk_free_gib_at_score": disk_free_gib_at_score,
        "score_verdict": score.get("verdict"),
        "checks_n": len(score.get("checks") or []),
        "score_file": scored["score_file"],
        "score_file_reason": scored["score_file_reason"],
        "b_cap": score.get("b_cap"),
        "infra_failure": infra_failure,
        "infra_class": infra_class,
        "effective_round_cap": (outcome.extra or {}).get("effective_round_cap"),
        "cap_exhausted": n_calls >= SUBCAPS[wall],
        "gpu_max_mib_while_running": gpu_sampler.max_mib,
        "gpu_final_mib": gpu_final,
        "gpu_closeout_ok": gpu_final <= GPU_CLOSEOUT_MIB,
        **metrics,
    }
    # 判定（本墙）：判据见 VERDICT_CRITERIA_V3.md §1/§2。
    if wall == WALL_A:
        born = wall_result["rounds_to_first_birth"]
        # 评审 R9：判据 §1 括注「冻结 wall_a_runner.py 全 12 项检查 PASS」必须
        # 落到机判——只看 final_rc==0 不够：评分器在自身进程内 exec_module 模型
        # 自著的 agent_solution.py，模型 `os._exit(0)` 即可让 rc=0 而 12 项一项
        # 未跑（同源 R2/R7 的「退出码可信」假设）。故把评分件的 verdict 与
        # checks 条数一并作为**必要条件**；T4 两墙冻结件均满足（wallA
        # score_verdict=pass / checks_n=12），判决不变。
        score_ok = wall_a_score_ok(scored["rc"], score,
                                   provenance_ok=bool(scored["score_file_unique"]))
        wall_result["score_checks_ok"] = score_ok
        wall_result["passed"] = wall_a_passed(
            born_at_round=born, final_rc=wall_result["final_rc"],
            score_ok=score_ok,
            run_expr_ok_calls=wall_result["run_expr_ok_calls"],
            tail_rounds=wall_result["tail_rounds"])
        wall_result["fail_reasons"] = [
            name for name, cond in (
                ("birth_round_absent_or_gt_limit",
                 born is None or born > BIRTH_ROUND_LIMIT),
                ("final_rc_not_zero", wall_result["final_rc"] != 0),
                ("score_verdict_not_pass_or_checks_lt_12", not score_ok),
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
    fc.write_json(run_dir / campaign().wall_result_name, wall_result)
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
    """聚合口径：取该墙**唯一**的非基础设施故障结果。

    两次以上非基础设施故障结果意味着同一墙被重跑过（I4-5 违规），此时
    必须炸而不是"取最后一次"——静默覆盖会让新结果顶替旧判决而判据 LOCK
    依旧验绿。只有整墙全是基础设施故障（D4-6 重跑也已用尽）时，才取最后
    一次尝试，让 ``infra_failure=True`` 如实流入 INCONCLUSIVE 窄口。
    """
    recs = _wall_attempt_records(wall)
    if not recs:
        return None
    non_infra = [r for r in recs if not r.get("infra_failure")]
    if len(non_infra) > 1:
        raise SystemExit(
            f"{wall} 存在 {len(non_infra)} 份非基础设施故障结果："
            f"{[r.get('run_dir') for r in non_infra]}。I4-5 禁止重跑同一墙，"
            "无法机判选哪一份——人工裁决。")
    return non_infra[0] if non_infra else recs[-1]


def judge_wall(wall: str) -> tuple[bool, str]:
    rec = _latest_wall_result(wall)
    if rec is None:
        return False, f"{wall}: 无 {campaign().wall_result_name}"
    ok = bool(rec.get("passed"))
    reasons = rec.get("fail_reasons") or []
    return ok, f"{wall}: passed={ok}" + (f" reasons={reasons}" if reasons else "")


def cmd_judge(wall: str) -> int:
    ok, msg = judge_wall(wall)
    print(msg)
    return 0 if ok else 1


def _wall_attempts_brief(wall: str) -> list[dict]:
    """判决件里的尝试台账（D4-6 重跑留痕：几次、每次是否基础设施故障）。"""
    return [{"attempt_index": r.get("attempt_index"),
             "ts_iso": r.get("ts_iso"), "run_dir": r.get("run_dir"),
             "infra_failure": r.get("infra_failure"),
             "infra_class": r.get("infra_class"),
             "paid_calls": r.get("paid_calls"),
             "passed": r.get("passed")}
            for r in _wall_attempt_records(wall)]


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
    # 共享池按 campaign 批次计（§T4.10：V4 自带 28 信封，V3 的 20 行不并入；
    # 对 V3 而言本批即唯一 task=4 批次，取值与 ``_ledger_task_rows()`` 相同）。
    rows = _ledger_campaign_rows()
    pool_used = len(rows)
    verdict, reason, ratio = compute_verdict(rec_a, rec_b, pool_used=pool_used)
    lock = verify_criteria_lock()
    baseline = (_read_json(retest_root() / "baseline_effective_ratio.json")
                if (retest_root() / "baseline_effective_ratio.json").is_file()
                else {})
    payload = {
        "schema": VERDICT_SCHEMA,
        "ts_iso": utc_iso(),
        "campaign": campaign().key,
        "batch": campaign().batch_id,
        "entrypoint_names": list(campaign().entrypoint_names),
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
            "attempts": _wall_attempts_brief(WALL_A),
            "rounds_to_first_birth": rec_a["rounds_to_first_birth"],
            "rounds_birth_to_rc0": rec_a["rounds_birth_to_rc0"],
            "final_rc": rec_a["final_rc"],
            "tail_granted": rec_a["tail_rounds"] >= 1,
            "run_expr_ok_calls": rec_a["run_expr_ok_calls"],
            "effective_step_ratio": rec_a["effective_step_ratio"],
            "passed": rec_a["passed"], "fail_reasons": rec_a["fail_reasons"],
            "cap_exhausted": rec_a["cap_exhausted"],
            "infra_failure": rec_a.get("infra_failure"),
            "infra_class": rec_a.get("infra_class"),
            "run_dir": rec_a["run_dir"],
        },
        "wallB": {
            "subcap": SUBCAPS[WALL_B], "paid_calls": rec_b["paid_calls"],
            "attempts": _wall_attempts_brief(WALL_B),
            "witness_12": rec_b["witness_12"],
            "sentinel_count": rec_b["sentinel_count"],
            "negative_physical": rec_b["negative_physical"],
            "calls_to_rc0": rec_b["paid_calls"],
            "effective_step_ratio": rec_b["effective_step_ratio"],
            "passed": rec_b["passed"], "fail_reasons": rec_b["fail_reasons"],
            "cap_exhausted": rec_b["cap_exhausted"],
            "b_cap": rec_b.get("b_cap"),
            "infra_failure": rec_b.get("infra_failure"),
            "infra_class": rec_b.get("infra_class"),
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
    out = retest_root() / campaign().verdict_name
    fc.write_json(out, payload)
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"[retest] 判决：{out}")
    return 0


def cmd_judge_all() -> int:
    rec_a = _latest_wall_result(WALL_A)
    rec_b = _latest_wall_result(WALL_B)
    pool_used = len(_ledger_campaign_rows())
    verdict, reason, ratio = compute_verdict(rec_a, rec_b,
                                             pool_used=pool_used)
    lock_state = "OK"
    try:
        lock = verify_criteria_lock()
        # AC-T4-3 后半：判决件里的 criteria_sha256 必须与 LOCK 内值相等
        # （判决件与判据绑定，换判据即换判决）。
        vp = retest_root() / campaign().verdict_name
        if vp.is_file():
            got = _read_json(vp).get("criteria_sha256")
            if got != lock["criteria_sha256"]:
                lock_state = (f"FAIL({campaign().verdict_name}.criteria_sha256={got} != "
                              f"LOCK {lock['criteria_sha256']})")
    except SystemExit as exc:
        lock_state = f"FAIL({exc})"
    print(f"verdict={verdict} ratio={ratio} criteria_lock={lock_state} "
          f"reason={reason}")
    return 0 if lock_state == "OK" else 1


# 账外批次实耗（账行看不见的部分，人工对账的唯一真值）。
# P26-B0 批准 4 次但实发 5 次：``--validate-models`` 走 ``_post_messages``，
# 对 HTTP 200 空内容最多重试 5 次，每次重试都是真付费 POST（D26-8b 登记项）。
# ``evidence`` 一律写**绝对路径**：P26-B0 的件落网关默认根
# ``/mnt/artifacts/simpraxis/runs/model_validate``，**不在** ``phase26_root()``
# 下——写成相对路径会让读者以为在 ``$ROOT/runs/``（评审 MINOR 指出）。
OFF_LEDGER_ACTUALS = {
    "P26-B0": {"approved": 4, "posts_used": 5, "kind": "validate-models",
               "outcome": "FAIL:EmptyResponseError（macro_ideation 连续 5 次 "
                          "HTTP200 空回答；max_tokens=8 被思维链吃光）",
               "evidence": "/mnt/artifacts/simpraxis/runs/model_validate/"
                           "20261103-094723/FAILURE.json"},
    "P26-B0b": {"approved": 1, "posts_used": 1, "kind": "coding_probe",
                "outcome": "FAIL:empty_text（200/type=message/回显一致，"
                           "output_tokens=8 恰等于上限）",
                "evidence_rel": "walls/retest/probe_coding_debug_"
                                "20261103T015937Z.json"},
    "P26-B0c": {"approved": 1, "posts_used": 1, "kind": "coding_probe",
                "outcome": "OK:pong（content_types=[thinking,text]，"
                           "output_tokens=32）",
                "evidence_rel": "walls/retest/probe_coding_debug_"
                                "20261103T020156Z.json"},
}


def offledger_actuals() -> dict:
    """逐批回填「批准件/证据件是否在场」并原样带出实耗。

    ``evidence_present`` 是**机判**而非自述：审计件不得只写一个看起来像
    路径的字符串（P17 教训）。
    """
    aprovals = phase26_root() / "quota" / "approvals"
    out = {}
    for bid, rec in OFF_LEDGER_ACTUALS.items():
        rec = dict(rec)
        rel = rec.pop("evidence_rel", None)
        ev = Path(rec["evidence"]) if rec.get("evidence") else (
            phase26_root() / rel if rel else None)
        if ev is not None:
            rec["evidence"] = str(ev)
        out[bid] = {**rec,
                    "evidence_present": bool(ev is not None and ev.is_file()),
                    "approval_present": (aprovals / f"{bid}.json").is_file()}
    return out


def _sealed_ledger_status(snap_p: Path) -> tuple[bool | None, dict]:
    """封存账快照对账（fail-closed）。

    快照由 ``--preflight`` 落盘；缺席或空表 ⇒ 封存账「不可证明未改」，
    返回 ``False`` 让 ``--audit-ledger`` 判负。原实现的 ok 判据是
    ``sealed_untouched is not False``，快照缺席时恒为 True 而放行——等于
    「审计件自述未改」就算数（P17 教训）。
    """
    if not snap_p.is_file():
        return False, {"__error__": {"error": "快照缺席", "path": str(snap_p)}}
    snap = _read_json(snap_p).get("paths") or {}
    if not snap:
        return False, {"__error__": {"error": "快照为空表，无对账对象",
                                     "path": str(snap_p)}}
    untouched = True
    detail: dict[str, dict] = {}
    for path, rec in snap.items():
        p = Path(path)
        now_exists = p.is_file()
        now_mtime = int(p.stat().st_mtime) if now_exists else None
        changed = (now_exists != rec["exists"] or now_mtime != rec["mtime"])
        detail[path] = {"before": rec,
                        "after": {"exists": now_exists, "mtime": now_mtime},
                        "changed": changed}
        if changed:
            untouched = False
    return untouched, detail


def cmd_audit_ledger() -> int:
    ledger = phase26_ledger()
    rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
    # 信封按 campaign 批次计（§T4.10）：V3 的 20 行不得把 V4 的 28 呼信封挤爆。
    task_rows = _ledger_campaign_rows()
    all_task4_rows = [r for r in rows if r.get("task") == TASK_ID]
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
    sealed_untouched, sealed_detail = _sealed_ledger_status(snap_p)
    ticket = phase26_root() / "quota" / "approvals" / f"{OFFLEDGER_BATCH}.json"
    payload = {
        "schema": AUDIT_SCHEMA,
        "ts_iso": utc_iso(),
        "campaign": campaign().key,
        "batch_id": campaign().batch_id,
        "rows_total": len(rows),
        "rows_task4": len(task_rows),
        "rows_task4_all_batches": len(all_task4_rows),
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
          and sealed_untouched is True)
    return 0 if ok else 1


# --------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", choices=sorted(CAMPAIGNS), default="v3",
                    help="预注册命名空间：v3=首轮（缺省，逐字节同既有行为），"
                         "v4=STOP-4a 裁决 (b) 的第二次预注册")
    ap.add_argument("--wall", choices=list(WALLS))
    ap.add_argument("--batch", default=None,
                    help="缺省取当前 campaign 的批次号")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--probe-coding", action="store_true")
    ap.add_argument("--probe-batch", default=PROBE_BATCH)
    ap.add_argument("--probe-max-tokens", type=int, default=8)
    ap.add_argument("--recompute-baseline", action="store_true")
    ap.add_argument("--write-criteria", action="store_true")
    ap.add_argument("--seal-criteria", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--judge-all", action="store_true")
    ap.add_argument("--combine", action="store_true",
                    help="聚合两墙成当前 campaign 的判决件")
    ap.add_argument("--combine-v3", action="store_true",
                    help="兼容别名：等价于 --combine（V3 缺省即 V3 判决件）")
    ap.add_argument("--audit-ledger", action="store_true")
    args = ap.parse_args(argv)

    # 任何命令都在**路径解析之前**切 campaign：retest_root()/criteria_path()/
    # lock_path()/票据/判决件名全部随之改变（V3 缺省 ⇒ 与既有行为逐字节相同）。
    set_campaign(args.campaign)
    if args.batch is None:
        args.batch = campaign().batch_id

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
    if args.combine or args.combine_v3:
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
