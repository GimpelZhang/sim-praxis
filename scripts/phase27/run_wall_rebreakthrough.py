#!/usr/bin/env python3
"""Phase 2.7 Task4：双墙破局重测（模式 R 回归闸 / 模式 N 新标尺）。

计划出处：``~/文档/Phase_2.7_Plan_detailed.md`` §T4（v1.4d）。本脚本是 Task 4
的**唯一付费入口**；``scripts/phase26/run_wall_retest.py`` **只读复用**
（D4-1：不得给 phase26 脚本加 ``v5`` campaign——``tests/milestone26/
test_task4b_preregistration.py::test_set_campaign_rejects_unknown_key`` 锁死）。

用法（顺序即计划 §T4.5 的顺序）：

  # 1) 0 付费：九件闸哈希 / 镜像 digest / 预算干跑（D4-7）/ 容器管道冒烟
  python -m scripts.phase27.run_wall_rebreakthrough --preflight --mode R

  # 2) 0 付费：开票（P27-B1 = 模式 R 28；P27-B1b = 模式 N 35）
  python -m scripts.phase27.run_wall_rebreakthrough --open-ticket --mode R

  # 3) 0 付费：带票复跑预检（票据字段 + 账本 preflight，必须 0 写账）
  python -m scripts.phase27.run_wall_rebreakthrough --preflight --mode R

  # 4) 0 付费：判据落盘 + 封 LOCK（sha256 + mtime + 0444）
  python -m scripts.phase27.run_wall_rebreakthrough --write-criteria --mode R
  python -m scripts.phase27.run_wall_rebreakthrough --seal-criteria --mode R

  # 5) 真跑（必须 SIMPRAXIS_ALLOW_PAID=1，裸跑 exit 2）
  SIMPRAXIS_ALLOW_PAID=1 python -m scripts.phase27.run_wall_rebreakthrough \
      --run --wall wallA --mode R

  # 6) 0 付费：三态判定 + 报告 + 解冻建议
  python -m scripts.phase27.run_wall_rebreakthrough --judge --mode R

纪律（硬规则 7 + §T4.1 不变量）：
- 墙不许动：开跑前比对 ``freeze_manifest.json`` 九件闸哈希（复用
  ``freeze_common``）；任一不符即熔断，不触网。
- 帽不许抬：模式 R 子帽 16/9 + 储备 3 = 28；模式 N 子帽 15/20 + 储备 0 = 35；
  任务帽 63 = 28 + 35（**按信封授权相加**）；全局硬帽 178。
- 模型不许换：全程 ``stage_models.yaml`` 的 ``coding_debug``（硬规则 1）。
- 判据预注册：``VERDICT_CRITERIA_PHASE27_<mode>.md`` 必须在**首呼之前**落盘、
  封 LOCK、置 0444；跑后零改动（sha + mtime + mode 三重取证）。
- 矛盾即结论：判负不改判据、不换种子、不重跑同一墙；唯一重跑口子是
  基础设施故障（三路我方证据，见 ``classify_infra_failure``）。
- **本脚本不解冻** ``RunSpec.agentic``（BR-2 用户裁决）：只出证据与建议。

锁内适配（计划 §T4 与仓内现实的最小差异，全部登记在
``Phase_2.7_Plan_Complete.md`` §5）：
1. ``--write-criteria/--seal-criteria`` 不复用 phase26 的同名函数——它们硬绑
   ``$SIMPRAXIS_PHASE26_ROOT/walls/<campaign.subdir>/``，在 phase27 上调用会
   写进**封存目录**。本脚本只复用其**判据正文**（``CRITERIA_BODY`` +
   ``V4_CRITERIA_PATCHES``，防正文漂移），路径/票号/LOCK 自建。
2. ``CFG_N`` 补 ``run_mode="N"``：``AgenticConfig.run_mode`` 缺省是 ``"R"``，
   照抄计划 §T4.4 的 ``CFG_N`` 片段会让模式 N 的环境卡**自称 R**（配置说谎）。
   该字段只切换标签与真值段门控，不是能力杠杆。
3. 运行目录用 ``<session>/<wall>/<stamp>/``（计划 §T4.7 的树是草图；墙目录下
   必须有 stamp 才能容纳基础设施故障重跑，且与 phase26 既有结构同构）。
4. 模式 R 用 ``agent_solution.py`` 作出生闸候选名（= V4 判据）：V3 的
   ``.entrypoint.py`` 在冻结任务书里**从不出现**（D26-8d），用它等于让出生闸
   结构上永不触发，是**假测试**。R 与 V3 的差异恰为 V4 已登记的那一条。
5. ``--preflight`` 分两段：计划 §T4.5 第 2 步在**开票之前**，而 phase26 的
   preflight 需要票据在场。故无票时只跑「冻结面 + 预算干跑 + 冒烟」并记
   ``ticket_present=false``；有票时补跑票据字段 + ``ledger.preflight``。
   ``--run`` 只认带票预检件（``ticket_preflight_ok=true``）。
6. 预算口径按账本自身语义：``global_used = max(global_used_after)``、
   ``pool_used`` 数**占批行**（phase27 ``uses_batch_occupancy`` ＝含
   ``seq=null`` 的 blocked 行），仅 ``plan_check`` 的 task 计量与
   ``preflight`` 同口径（整数 seq 行）。
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.agentic.caps import (
    KIND_BIRTH_ABSENT,
    BudgetTracker,
    CapExceeded,
)
from orchestrator.agentic.config import AgenticConfig
from orchestrator.agentic.forensics import MODEL_ROUND_TOOL, TraceStore
from orchestrator.agentic.loop import _born_entrypoint, run_agentic_inner_loop
from orchestrator.agentic.wiring import build_seq_provider, parse_grant_strings
from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import phase27_root, sealed_ledger_paths
from orchestrator.gateway.quota_ledger import (
    PHASE27_HARD_CAP,
    PHASE27_TASK_CAPS,
    QuotaLedger,
    billing_context,
    call_lines,
    read_lines,
    write_approval,
)
from orchestrator.gateway.router import ModelGateway

import scripts.phase26.run_wall_retest as t26
from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls.run_wall import GpuSampler, docker_label_residue
from scripts.phase25.walls.spike_backend import (
    IMAGE_ID,
    IMAGE_REPODIGEST,
    SpikeWallBackend,
)
from scripts.phase25.walls.verdict import WALL_A, WALL_B

WALLS = (WALL_A, WALL_B)
TASK_ID = 4
TASK_CAP = int(PHASE27_TASK_CAPS[TASK_ID])
GPU_CLOSEOUT_MIB = 200
DISK_FLOOR_GIB = 40

# --- §T4.4 预算形状（字面量单一源；不得在步骤里单点覆盖） -------------------
MODE_R = {"wallA": 16, "wallB": 9, "reserve": 3, "total": 28}
MODE_N = {"wallA": 15, "wallB": 20, "reserve": 0, "total": 35}
MODES = {"R": MODE_R, "N": MODE_N}
BATCH_BY_MODE = {"R": "P27-B1", "N": "P27-B1b"}

# --- §T4.4 判据常量 ---------------------------------------------------------
BIRTH_ROUND_LIMIT_R = 8      # 与 V3/V4 同标尺
BIRTH_ROUND_LIMIT_N = 5      # 模式 N 新标尺
CALLS_TO_RC0_LIMIT_B = 15
EFFECTIVE_RATIO_FLOOR = 0.80
TAIL_ROUNDS = 3

# 模式开关：R 必须与 V3/V4 逐字同条件（回归闸），新杠杆**只**在 N 打开。
CFG_R = AgenticConfig(verify_repair_tail=True, verify_repair_tail_rounds=TAIL_ROUNDS,
                      run_expr=True, tolerant_patch=True, scratchpad=True,
                      env_spec=False, render_preview=False, run_mode="R")
CFG_N = AgenticConfig(verify_repair_tail=True, verify_repair_tail_rounds=TAIL_ROUNDS,
                      run_expr=True, tolerant_patch=True, scratchpad=True,
                      env_spec=True, render_preview=True, run_mode="N")
CFG = {"R": CFG_R, "N": CFG_N}          # 唯一入口；禁止在步骤里单点覆盖

ENTRYPOINT_NAMES = ("agent_solution.py",)   # D26-8d：冻结任务书要求交付的文件名

CRITERIA_LOCK_SCHEMA = "simpraxis.phase27.t4_criteria_lock.v1"
PREFLIGHT_SCHEMA = "simpraxis.phase27.t4_preflight.v1"
WALL_RESULT_SCHEMA = "simpraxis.phase27.wall_result.v1"
VERDICT_SCHEMA = "simpraxis.phase27.verdict.v1"
PLAN_CHECK_SCHEMA = "simpraxis.phase27.t4_plan_check.v1"

V_ESTABLISHED = t26.V_ESTABLISHED
V_NOT_ESTABLISHED = t26.V_NOT_ESTABLISHED
V_INCONCLUSIVE = t26.V_INCONCLUSIVE

EARLY_STOP_ROUND = 8
EARLY_STOP_REASON = "birth_absent_at_round_8"


# ------------------------------------------------------------------ 基础工具

def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rebreak_root() -> Path:
    return phase27_root() / "walls" / "rebreakthrough"


def current_pointer() -> Path:
    return rebreak_root() / "CURRENT"


def session_root(*, create: bool = False) -> Path:
    """本次战役的根目录（``CURRENT`` 指针 = 唯一真值）。

    计划 §T4.7 的树是一份 ``<ts>/`` 目录；本脚本把「哪一份 <ts>」记在
    ``CURRENT``（单行相对名）里，所有子命令共享同一目录，避免各命令各建
    一份互不相认的证据树。
    """
    ptr = current_pointer()
    if ptr.is_file():
        name = ptr.read_text(encoding="utf-8").strip()
        if not name:
            raise SystemExit(f"CURRENT 指针为空：{ptr}")
        p = rebreak_root() / name
        if not p.is_dir():
            raise SystemExit(f"CURRENT 指向的目录不存在：{p}")
        return p
    if not create:
        raise SystemExit(f"本次战役尚未初始化（缺 {ptr}）：先跑 --preflight")
    base = rebreak_root()
    stamp = utc_stamp()
    p = base / stamp
    if p.exists():
        raise SystemExit(f"会话目录已存在（时间戳碰撞）：{p}")
    fc.ensure_dir(p)
    fc.ensure_dir(p / "manifests")
    fc.atomic_write_bytes(ptr, (stamp + "\n").encode("utf-8"))
    return p


def mode_table(mode: str) -> dict:
    if mode not in MODES:
        raise SystemExit(f"未知模式 {mode!r}（R / N）")
    return MODES[mode]


def batch_of(mode: str) -> str:
    return BATCH_BY_MODE[mode]


def birth_limit(mode: str) -> int:
    return BIRTH_ROUND_LIMIT_R if mode == "R" else BIRTH_ROUND_LIMIT_N


def criteria_path(mode: str) -> Path:
    return session_root() / f"VERDICT_CRITERIA_PHASE27_{mode}.md"


def lock_path(mode: str) -> Path:
    """预注册 LOCK 的落点：两模式各一份，互不覆盖。

    锁内适配（2026-11-04）：计划假设「一次战役一份 LOCK」，但模式 R 已封的
    ``LOCK.json`` 必须逐字节不动（AC-T4-1 取证），而模式 N 是**另一份**预注册
    （判据正文不同、票据不同）。故 R 沿用 ``LOCK.json``，N 用 ``LOCK_N.json``；
    封存语义（sha256 + mtime + 0444 三重校验）逐字不变。
    """
    name = "LOCK.json" if mode == "R" else f"LOCK_{mode}.json"
    return session_root() / "manifests" / name


def phase27_ledger() -> QuotaLedger:
    return QuotaLedger.phase27()


def nvidia_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used",
         "--format=csv,noheader,nounits"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    return int(out.stdout.decode().strip().splitlines()[0])


def _rows() -> list[dict]:
    return read_lines(phase27_ledger().ns.ledger_path_fn())


def _batch_rows(mode: str) -> list[dict]:
    """占批行（phase27 口径含 ``seq=null`` 的 blocked 行）。"""
    want = batch_of(mode)
    return [r for r in _rows() if r.get("batch_id") == want]


def _batch_spent(mode: str) -> int:
    """本批次已花的**实呼**行数（``event="call"``；blocked 行 seq=null 不占实呼）。"""
    return sum(1 for r in _batch_rows(mode) if r.get("event") == "call")


# ------------------------------------------------------------------ 冻结面校验

def verify_freeze(root: Path) -> dict:
    """九件闸哈希 + 内文留痕 + 镜像 digest（沿用 phase25 既有实现）。"""
    manifest_p = root / "manifests" / "freeze_manifest.json"
    if not manifest_p.is_file():
        raise SystemExit(f"冻结清单不存在：{manifest_p}")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    if manifest["gate_hashes"] != fc.gate_hashes():
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


def grants_for(wall: str) -> tuple[str, ...]:
    return t26.retest_grants(wall)


def task_text(wall: str, mode: str, skills_sha12: "str | None" = None) -> str:
    """冻结任务书 + 操作页脚；模式 N 追加 D4-5 事实行（**只加信息面**）。

    ``skills_sha12`` 非空时再追加技能库事实行（模式 N 的墙 B 才挂技能库；
    模式 R 一行都不加，保持与 V4 逐字同条件）。
    """
    text = t26.task_text(wall)
    if mode == "N":
        # D4-5：D26-8e 的教训——任务书 API 2 未声明入参类型，模型自写
        # isinstance(str) 守卫即失分。这里只加一行**事实声明**，不改评分器
        # （改评分器 = 改标尺）。模式 R 不出现本行（与 V4 逐字同条件）。
        text += ("\n\n[环境卡事实] `out_path` 参数可能是 `pathlib.Path` 或 `str`，"
                 "请统一用 `str(out_path)` 落盘。\n")
    if skills_sha12:
        text += (
            "\n\n[技能库事实] 工作目录下的 `simpraxis_skills/` 是一份可导入的"
            f"技能库（清单指纹 `{skills_sha12}`），其中 "
            "`simpraxis_skills.wall_b.instruments` 已实现本题要求的四个 API："
            "`make_vessel_mesh(out_path)`、`edge_manifold_counts(faces)`、"
            "`kinematic_witness(t, th, p, cl, *, flow_start, flow_end)`、"
            "`evaluate_precheck(w)`；其合同阈值与违例名按任务书冻结表。"
            "是否使用、如何改参数由你决定；若选择转发，注意评分器以自己的目录"
            "为 `sys.path[0]` 执行 `agent_solution.py`，可在文件头写 "
            "`import os, sys; sys.path.insert(0, os.path.dirname("
            "os.path.abspath(__file__)))` 后再 `from simpraxis_skills.wall_b"
            ".instruments import ...`。\n")
    return text


# --------------------------------------------------- 技能库落地（模式 N / D4-4b）

SKILLS_PACKAGE_DIRNAME = "simpraxis_skills"


def skills_stage_root() -> Path:
    """当前清单指纹对应的 staged 技能根（D2-5：目录名即清单 sha12）。"""
    from orchestrator.skills.registry import manifest_sha12, skills_root
    return phase27_root() / "skills_stage" / manifest_sha12(skills_root())


def _stage_skills_into(work: Path) -> dict:
    """把 staged 技能库整树落进 workdir（D4-4b 方案 b），返回落盘记录。

    为什么落在 workdir 而不是装进主 venv（方案 a）：workdir 是**唯一**同时
    被模型容器（``-w /work``，rw 挂载）与评分进程（cwd=workdir）看见的路径，
    所以同一份文件在两侧都可导入；装 venv 会改动宿主环境且不可按轮复现。
    """
    from orchestrator.skills.registry import (PACKAGE_NAME, load_skills_manifest,
                                              manifest_sha12, skills_root)
    root = skills_root()
    manifest = load_skills_manifest(root)      # 逐件复算 sha256，fail-closed
    sha12 = manifest_sha12(root)
    src = phase27_root() / "skills_stage" / sha12 / PACKAGE_NAME
    if not src.is_dir():
        raise SystemExit(
            f"技能库未暂存：{src}（先跑 scripts.phase27.stage_skills --stage）")
    dst_pkg = work / PACKAGE_NAME
    if dst_pkg.exists():
        raise SystemExit(f"workdir 已存在 {PACKAGE_NAME}：{dst_pkg}")
    wanted = [(e["relpath"], e["sha256"]) for e in manifest["skills"]]
    wanted += [(p["relpath"], p["sha256"]) for p in manifest["packages"]]
    files = []
    for rel, sha in wanted:
        sub = rel.split("/", 1)[1]             # "skills/<sub>" → "<sub>"
        s = src / sub
        if s.is_symlink() or not s.is_file():
            raise SystemExit(f"staged 缺件/软链：{s}")
        data = s.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise SystemExit(f"staged 文件 sha256 与清单不符：{rel}")
        d = dst_pkg / sub
        ensure_private_dir(d.parent, mode=0o700)
        fc.atomic_write_bytes(d, data, mode=0o600)
        files.append({"relpath": rel, "sha256": sha})
    return {"manifest_sha12": sha12, "stage_root": str(src),
            "workdir_package": str(dst_pkg), "files": files}


def skills_import_probe(work: Path) -> dict:
    """以**评分器同款解释器与 cwd** 跑 import 探针（D4-4b 入口条件，0 付费）。"""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import simpraxis_skills.wall_b.instruments as m;"
         "assert callable(m.make_vessel_mesh) and callable(m.evaluate_precheck);"
         "print('SKILLS_IMPORT_OK')"],
        cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=180)
    return {"python": sys.executable, "cwd": str(work), "rc": proc.returncode,
            "stdout": (proc.stdout or "").strip()[-400:],
            "stderr": (proc.stderr or "").strip()[-800:]}


# ------------------------------------------------------------------ 判据正文

def _phase26_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_PHASE26_ROOT",
                               str(phase27_root().parent / "phase26")))


def v3_criteria_path() -> Path:
    """AC-T4-1 的判据源：phase26 **冻结** V3 件（不是仓内同名模板）。"""
    return _phase26_root() / "walls" / "retest" / "VERDICT_CRITERIA_V3.md"


def v3_criteria_text() -> str:
    p = v3_criteria_path()
    if not p.is_file():
        raise SystemExit(f"冻结 V3 判据不存在：{p}（AC-T4-1 判据源）")
    return p.read_text(encoding="utf-8")


# 模式 R：**只**施加三处已登记替换（可机判条目新增/删除各 0 条）。
_P27_R_SUBS = (
    # ① D26-8d（V4 已登记的**唯一**判据增量）：出生件名
    (".entrypoint.py", ENTRYPOINT_NAMES[0], 1),
    # ② 票据号：Phase 2.7 独立账
    ("- 票据 `P26-B1`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3",
     "- 票据 `P27-B1`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3", 1),
    # ③ 账本命名空间 + 封存账本数（Phase 2.6 起 phase26 也封存）
    ("- 全部实呼落 `QuotaLedger.phase26()`；phase1/2/25 三本封存账零写入；",
     "- 全部实呼落 `QuotaLedger.phase27()`；Phase 1/2/2.5/2.6 四本封存账零写入；", 1),
)

# 模式 N：**新标尺**（出生帽 5、票 P27-B1b、子额度 15/20/0）。
_P27_N_SUBS = (
    ("- `rounds_to_first_birth <= 8`（入口脚本 `agent_solution.py` 首次同时满足",
     "- `rounds_to_first_birth <= 5`（**模式 N 新标尺**；V3/V4 的 8 轮不适用）\n"
     "  （入口脚本 `agent_solution.py` 首次同时满足", 1),
    ("- 票据 `P27-B1`：单票覆盖两墙，`approved=28`，票内子额度 16/9/3",
     "- 票据 `P27-B1b`：单票覆盖两墙，`approved=35`，票内子额度 15/20/0", 1),
    # N 的信封是 35，V3 §4(iii)/§5 里写死的「28」若不跟着换会自相矛盾
    # （审阅 GAP「Mode N stale pool size」）。
    ("(iii) 共享池已用到硬包络 28", "(iii) 共享池已用到硬包络 35", 1),
    ("若已用 28 仍 `INCONCLUSIVE`", "若已用 35 仍 `INCONCLUSIVE`", 1),
)

_HEADER_BEGIN = "<!-- P27-HEADER-BEGIN -->"
_HEADER_END = "<!-- P27-HEADER-END -->"
_EARLY_BEGIN = "<!-- P27-EARLYSTOP-BEGIN -->"
_EARLY_END = "<!-- P27-EARLYSTOP-END -->"

# D4-8 原文（AC-T4-9①：必须在首呼前 LOCK 的预注册件里**逐字**出现）。
EARLY_STOP_RULE_TEXT = (
    "模式 R 跑完**第 8 轮**时，若 `rounds_to_first_birth is None` 且 workdir 内"
    "无任何入口脚本落盘（即 `birth_absent` 已成定局），脚本**允许**提前中止 R；"
    "R 的判决**仍为负**（`NOT_ESTABLISHED`），**不得**因早停改判。未用额度"
    "**在任务帽 63 之内**转给模式 N（N 自身信封仍 35，不抬帽；63 不因 R 早停而"
    "冻结余额），**绝不**抬高 `PHASE27_TASK_CAPS[4]` 或 178。")


def _header_block(mode: str, v3_sha: str, ts_iso: str) -> str:
    sub = mode_table(mode)
    head = [
        _HEADER_BEGIN,
        "",
        f"生成时刻 ts_iso = `{ts_iso}`；模型面 `coding_debug = "
        f"{t26.coding_debug_model_id()}`（硬规则 1：`stage_models.yaml` 是唯一"
        "模型配置点，本 Task **不变更**任何 stage 的 model_id）。",
        "",
        f"# Phase 2.7 Task 4 三态判据 模式 {mode}"
        "（首呼前冻结；'R' = 与 V3/V4 逐字同条件，'N' = 新标尺不可比）",
        "",
        "> **文件由来**：本文件由 `scripts/phase27/run_wall_rebreakthrough.py"
        f"::criteria_body(\"{mode}\")` 在**首呼之前**生成并封 LOCK。正文来源 = 冻结 "
        f"`$SIMPRAXIS_PHASE26_ROOT/walls/retest/VERDICT_CRITERIA_V3.md`"
        f"（sha256 `{v3_sha}`）**逐字**，仅施加下列**已登记**替换：",
        "> ① 出生件名 `.entrypoint.py` → `agent_solution.py`（D26-8d，V4 已登记的"
        "**唯一**判据增量）；",
        "> ② 票据号 `P26-B1` → `P27-B1`（Phase 2.7 独立账）；",
        "> ③ 账本命名空间 `phase26()` → `phase27()` 与四本封存账（Phase 2.6 起"
        "phase26 亦封存只读）。",
    ]
    if mode == "R":
        head += [
            "> ",
            "> **可机判条目新增 0 条、删除 0 条、阈值 0 处改动**（AC-T4-1：剥掉本"
            "标记块与下面早停块、施加上述三处替换、再删去 V3 首行标题**及其后的"
            "空行**后，与冻结 V3 **逐字节相等**——被替换掉的那行标题由本块承载）。",
            "> ",
            "> **可比性声明**：模式 R 与 V3/V4 **可比**——同墙（`t5_freeze`）、"
            "同帽（wallA 16 / wallB 9 / 储备 3）、同判据、同模型面。",
            "> **与模式 N 的关系**：N 是**新标尺**（ENV_SPEC / 预览探针 / 资产驱动 / "
            "技能库全开，出生帽 5、票据 `P27-B1b` 35 呼），与 V3/V4 **不可比**；"
            "N 的判据单独落盘。R 失败不阻塞 N；N 结果不回写 R 的判决。",
            f"> **账**：票据 `{batch_of(mode)}` {sub['total']} = {sub['wallA']} + "
            f"{sub['wallB']} + {sub['reserve']}（子帽只由脚本 `MODE_{mode}` 强制，"
            "不进票据）；两票相加 28 + 35 = 63 = `PHASE27_TASK_CAPS[4]`（按**信封"
            "授权**相加，不按实花相加）。",
        ]
    else:
        head += [
            "> ④ 出生帽 `<= 8` → `<= 5`（模式 N 新标尺）；⑤ 票据 → `P27-B1b` / "
            "`approved=35` / 子额度 15/20/0；⑥ 共享池硬包络 `28` → `35`（§4(iii) "
            "与 §5 到帽条，信封变则这两处的「28」必须跟着变，否则件内自相矛盾）；"
            "⑦ 附加杠杆（非判据条目）：N 另开 ENV_SPEC 与预览探针，模式 R 一律不给。",
            "> ",
            "> **不可比声明**：模式 N 是**新标尺**（信息面大幅增强），任何结果"
            "**不得**与 V3/V4 做同尺比较，不得写作「优于 V4」「打破历史记录」"
            "（AC-T4-6 正则黑名单）。N 只回答一个问题：在信息面增强的标尺下，双墙"
            "是否**首次**可过。R 失败不阻塞 N；N 结果不回写 R 的判决。",
            f"> **账**：票据 `{batch_of(mode)}` {sub['total']} = {sub['wallA']} + "
            f"{sub['wallB']} + {sub['reserve']}（子帽只由脚本 `MODE_{mode}` 强制，"
            "不进票据）。",
        ]
    head += [_HEADER_END]
    return "\n".join(head) + "\n"


def _early_stop_block() -> str:
    return "\n".join([
        _EARLY_BEGIN,
        "",
        "## 7. 模式 R 早停规则（D4-8 / D4-8b；预注册钉死，非新增判据条目）",
        "",
        EARLY_STOP_RULE_TEXT,
        "",
        "判决件必须新增 `early_stop` 块：`{occurred, rounds_used, "
        "early_stop_reason, transferred_to, transfer_amount}`，其中：",
        "- `rounds_used == 8`（第 8 轮跑完后熔断，第 9 轮不再发起）；",
        "- `early_stop_reason == \"birth_absent_at_round_8\"`；",
        "- `transferred_to == \"N\"`，`transfer_amount == 28 - rounds_used`"
        "（`rounds_used == 8` 时即 20）；",
        "- 报告须写明「`comparable_rounds=8`，第 9–16 轮未执行」，**禁止**写成"
        "「完整重打 16 轮」；",
        "- 账上批准件仍**恰为两张**（`P27-B1` / `P27-B1b`），**不得**为转移另开"
        "第三张票（D4-8b）。",
        _EARLY_END,
        "",
    ])


def _apply_subs(text: str, subs, *, tag: str) -> str:
    for old, new, want in subs:
        got = text.count(old)
        if got != want:
            raise SystemExit(
                f"{tag} 锚点漂移（命中 {got} 次，期望 {want}）：{old[:48]!r}")
        text = text.replace(old, new)
    return text


def criteria_body(mode: str) -> str:
    """预注册正文：冻结 V3 逐字 + 已登记替换 + 标记块（R 另含早停规则）。

    R 件剥掉两个标记块并施加 :data:`_P27_R_SUBS` 后与冻结 V3 **逐字节相等**
    ——这是 AC-T4-1「新增可机判条目 0 条」的可机判形式。
    """
    v3 = v3_criteria_text()
    v3_sha = hashlib.sha256(v3.encode("utf-8")).hexdigest()
    subs = _P27_R_SUBS if mode == "R" else _P27_R_SUBS + _P27_N_SUBS
    body = _apply_subs(v3, subs, tag=f"模式 {mode} 判据替换")
    title, _, rest = body.partition("\n")
    out = _header_block(mode, v3_sha, utc_iso()) + rest.lstrip("\n")
    if mode == "R":
        out = out.rstrip("\n") + "\n" + _early_stop_block()
        if EARLY_STOP_RULE_TEXT not in out:      # pragma: no cover - 防御
            raise SystemExit("早停规则未进入预注册件（AC-T4-9①）")
    elif "不可比" not in out:                    # pragma: no cover - 防御
        raise SystemExit("模式 N 判据缺「不可比」声明")
    return out




def _rbase() -> float:
    """R_base 只作参考基线（判据是绝对阈值 0.80）：取 phase26 已冻结复算值。"""
    p = (Path(os.environ.get(
        "SIMPRAXIS_PHASE26_ROOT",
        str(phase27_root().parent / "phase26"))) / "walls" / "retest"
        / "baseline_effective_ratio.json")
    if not p.is_file():
        raise SystemExit(f"缺 R_base 基线件：{p}（phase26 冻结产物，只读）")
    return float(json.loads(p.read_text(encoding="utf-8"))["recomputed"])


def cmd_write_criteria(mode: str) -> int:
    session_root(create=True)
    if lock_path(mode).is_file():
        raise SystemExit("LOCK 已存在：判据已封，禁止重写")
    p = criteria_path(mode)
    if p.exists():
        raise SystemExit(f"判据已存在：{p}（删旧件须人工裁决）")
    text = criteria_body(mode).replace("__RBASE__", f"{_rbase()}")
    fc.atomic_write_bytes(p, text.encode("utf-8"))
    print(f"[p27t4] 判据已落盘：{p}（{len(text.encode('utf-8'))} B）")
    return 0


def _criteria_lock_record(mode: str) -> dict:
    p = criteria_path(mode)
    st = p.stat()
    return {
        "schema": CRITERIA_LOCK_SCHEMA,
        "mode": mode,
        "batch_id": batch_of(mode),
        "criteria_path": str(p),
        "criteria_sha256": fc.sha256_file(p),
        "criteria_mtime": int(st.st_mtime),
        "criteria_mode": oct(st.st_mode & 0o777),
        "locked_iso": utc_iso(),
        "entrypoint_names": list(ENTRYPOINT_NAMES),
        "subcaps": dict(mode_table(mode)),
        "note": ("首呼锁：判据文本在首呼之前封存；跑后任何改动都使本批次作废"
                 "（sha + mtime + mode 三重取证）。"),
    }


def verify_criteria_lock(mode: str) -> dict:
    if not lock_path(mode).is_file():
        raise SystemExit("LOCK 不存在：判据未预注册，拒绝开跑（fail-closed）")
    lock = json.loads(lock_path(mode).read_text(encoding="utf-8"))
    if lock.get("mode") != mode:
        raise SystemExit(
            f"LOCK 属于模式 {lock.get('mode')!r}，本次是 {mode!r}")
    p = criteria_path(mode)
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
        raise SystemExit("判据 LOCK 校验失败（AC-T4-1）：" + "；".join(problems))
    return lock


def cmd_seal_criteria(mode: str) -> int:
    if lock_path(mode).is_file():
        raise SystemExit("LOCK 已存在：判据已封，拒绝重封")
    if _batch_rows(mode):
        raise SystemExit(f"账上已有 {batch_of(mode)} 的占批行：首呼已发生，"
                         "禁止再封判据")
    p = criteria_path(mode)
    if not p.is_file():
        raise SystemExit(f"判据文件缺失：{p}（先 --write-criteria）")
    os.chmod(p, 0o444)
    rec = _criteria_lock_record(mode)
    fc.write_json(lock_path(mode), rec)
    print(json.dumps(rec, indent=1, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------ 预算 / 开票

def plan_check() -> dict:
    """D4-7：开票前预算干跑（逐票 + 任务累计 + 全局）。

    ``write_approval`` **只校验全局**，会接受一张必然在半场爆掉的票；任务级
    PAUSE 不可恢复（``quota_ledger.py`` 的 task_hard_cap 分支），故这里把
    28 + 35 一次算进 cap。

    锁内适配 #13（2026-11-04，登记于 Phase_2.7_Plan_Complete.md §5.0b）：
    干跑问的是「这一批**还要**花多少」，不是「票面总额多少」。原实现对每张票都
    用整额 ``approved`` 再加一次 ``task_used``，于是已花掉的实呼被**重复计入**：
    模式 N 已花 18 后，`task_used(35) + approved(35) = 70 > 63` 会把一次本来
    合规的墙 B 开跑挡在门外（实测 11:12 被 D4-7 拒绝）。改为按**票额剩余**
    （``approved − 本批已花``）计：票未开时 remaining == approved，历史判决
    全不受影响；批次信封本身的硬约束仍由 ``cmd_run`` 的剩余钳制（#12）执行。
    """
    rows = _rows()
    global_used = max((r.get("global_used_after") or 0 for r in rows), default=0)
    task_used = sum(1 for r in call_lines(rows) if r.get("task") == TASK_ID)
    checks = []
    ok_all = True
    for mode, sub in (("R", MODE_R), ("N", MODE_N)):
        approved = sub["total"]
        spent = _batch_spent(mode)
        remaining_batch = max(0, approved - spent)
        task_ok = task_used + remaining_batch <= TASK_CAP
        global_ok = global_used + remaining_batch <= PHASE27_HARD_CAP
        ok_all = ok_all and task_ok and global_ok
        checks.append({
            "mode": mode, "batch_id": batch_of(mode), "approved": approved,
            "batch_spent": spent, "batch_remaining": remaining_batch,
            "task_used": task_used, "task_cap": TASK_CAP, "task_ok": task_ok,
            "global_used": global_used, "global_cap": PHASE27_HARD_CAP,
            "global_ok": global_ok,
        })
    envelope_ok = (MODE_R["total"] + MODE_N["total"]) == TASK_CAP
    return {
        "schema": PLAN_CHECK_SCHEMA, "ts_iso": utc_iso(),
        "task_cap": TASK_CAP, "hard_cap": PHASE27_HARD_CAP,
        "envelope_sum": MODE_R["total"] + MODE_N["total"],
        "envelope_matches_task_cap": envelope_ok,
        "checks": checks, "ok": ok_all and envelope_ok,
    }


def _persist_plan_check(written_by: str) -> dict:
    """把预算干跑结果落 ``<session>/plan_check.json``（0 付费、无网络）。

    ``written_by`` 标明这次落盘是谁触发的（``run:wallB:N`` / ``plan-check:N``），
    使 AC-T4-5 读到的件能自证「哪一次真开跑前的预算现状」，而不是一张陈件。
    """
    plan = plan_check()
    plan["written_by"] = written_by
    fc.write_json(session_root(create=True) / "plan_check.json", plan)
    return plan


def cmd_open_ticket(mode: str, batch: str) -> int:
    if batch != batch_of(mode):
        raise SystemExit(f"模式 {mode} 的票号必须是 {batch_of(mode)}：{batch!r}")
    sub = mode_table(mode)
    plan = plan_check()
    root = session_root(create=True)
    fc.write_json(root / "plan_check.json", plan)
    if not plan["ok"]:
        raise SystemExit(f"预算干跑失败，拒绝开票（D4-7）：{plan}")
    note = (f"Phase 2.7 Task4 模式 {mode} 双墙重测信封："
            f"wallA {sub['wallA']} / wallB {sub['wallB']} / "
            f"reserve {sub['reserve']} = {sub['total']}。"
            f"子帽只由脚本强制，不进票（拆票会 BLOCKED_BATCH_APPROVAL_MISMATCH）。")
    p = write_approval(batch_id=batch, task=TASK_ID, stage="coding_debug",
                       approved=sub["total"], beyond_normal=False,
                       note=note, namespace=phase27_ledger().ns)
    print(f"[p27t4] 票已开：{p}")
    return 0


# ------------------------------------------------------------------ 预检

def _smoke_backend(wall: str, root: Path) -> dict:
    backend = SpikeWallBackend(wall, freeze_dir=root)
    if wall == WALL_A:
        code = ("import numpy, mujoco, robosuite; "
                "from libero.libero.benchmark import get_benchmark; "
                "from libero.libero.envs.env_wrapper import ControlEnv; "
                "print('SMOKE_OK', ControlEnv.__name__)")
    else:
        code = ("import numpy, trimesh; "
                "print('SMOKE_OK', trimesh.__version__)")
    with tempfile.TemporaryDirectory(prefix="p27t4preflight-") as td:
        res = backend.run(["/opt/venv/bin/python", "-c", code],
                          cwd=Path(td), timeout_s=180)
        expr = backend.run(
            ["python", "-I", "-c",
             "import numpy; print('EXPR_OK', numpy.__version__)"],
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


def cmd_preflight(mode: str) -> int:
    root_dir = fc.freeze_root()
    manifest = verify_freeze(root_dir)
    sroot = session_root(create=True)
    ledger = phase27_ledger()
    ledger_p = ledger.ns.ledger_path_fn()
    before = len(call_lines(read_lines(ledger_p)))

    plan = plan_check()
    fc.write_json(sroot / "plan_check.json", plan)
    if not plan["ok"]:
        raise SystemExit(f"预算干跑失败（D4-7），拒绝开票/开跑：{plan}")

    ticket_p = ledger.ns.approvals_dir_fn() / f"{batch_of(mode)}.json"
    ticket = None
    pf_usage = None
    if ticket_p.is_file():
        ticket = json.loads(ticket_p.read_text(encoding="utf-8"))
        sub = mode_table(mode)
        expect = {"batch_id": batch_of(mode), "task": TASK_ID,
                  "stage": "coding_debug", "approved": sub["total"],
                  "beyond_normal": False, "ledger_phase": 27}
        for key, want in expect.items():
            if ticket.get(key) != want:
                raise SystemExit(
                    f"票据字段 {key} 不符：{ticket.get(key)!r} != {want!r}")
        try:
            pf_usage = ledger.preflight("coding_debug", task=TASK_ID,
                                        batch_id=batch_of(mode))
        except Exception as exc:  # noqa: BLE001 - 任何拦截都是预检失败
            raise SystemExit(f"票据 preflight 被拦：{type(exc).__name__}: {exc}")
        if len(call_lines(read_lines(ledger_p))) != before:
            raise SystemExit("preflight 意外写账（票据可能被判 blocked）")

    grants = {}
    smokes = {}
    for w in WALLS:
        g = grants_for(w)
        parse_grant_strings(g)
        grants[w] = list(g)
        smokes[w] = _smoke_backend(w, root_dir)
        if smokes[w]["smoke_rc"] != 0 or "SMOKE_OK" not in smokes[w]["smoke_stdout"]:
            raise SystemExit(f"{w} 管道冒烟失败：{smokes[w]}")
        if smokes[w]["expr_rc"] != 0 or "EXPR_OK" not in smokes[w]["expr_stdout"]:
            raise SystemExit(f"{w} F2 内联探针冒烟失败：{smokes[w]}")
    # D4-4b：模式 N 的技能库必须**在评分时刻**可导入（评分跑宿主主 venv、
    # cwd=workdir、sys.path[0]=评分器目录），故这里用同一解释器 + 同款 cwd
    # 落一份 workdir 形态并真跑一次 import；不绿不许开票。
    skills_probe = None
    if mode == "N":
        probe_work = fc.ensure_dir(
            phase27_root() / "task4_skills" / f"import_probe_{utc_stamp()}" / "work")
        probe_rec = _stage_skills_into(probe_work)
        skills_probe = dict(skills_import_probe(probe_work),
                            manifest_sha12=probe_rec["manifest_sha12"],
                            files=len(probe_rec["files"]))
        fc.write_json(sroot / "skills_import_probe.json", skills_probe)
        if skills_probe["rc"] != 0 or "SKILLS_IMPORT_OK" not in skills_probe["stdout"]:
            raise SystemExit(f"技能库 import 探针失败（D4-4b）：{skills_probe}")

    residue = docker_label_residue()
    if residue:
        raise SystemExit(f"冒烟后容器残留：{residue}（硬规则 5）")

    free_gib = shutil.disk_usage("/mnt").free / (1 << 30)
    if free_gib < DISK_FLOOR_GIB:
        raise SystemExit(f"/mnt 空闲 {free_gib:.1f}G < {DISK_FLOOR_GIB}G（§T4.2）")
    gpu_mib = nvidia_used_mib()
    if gpu_mib > GPU_CLOSEOUT_MIB:
        raise SystemExit(f"入口显存 {gpu_mib} MiB > {GPU_CLOSEOUT_MIB}（§T4.2）")

    after = len(call_lines(read_lines(ledger_p)))
    payload = {
        "schema": PREFLIGHT_SCHEMA,
        "ts_iso": utc_iso(),
        "mode": mode,
        "model": {"coding_debug": t26.coding_debug_model_id()},
        "manifest_sections": list(manifest["sections"]),
        "gate_hashes": fc.gate_hashes(),
        "image": {"id": IMAGE_ID, "repo_digest": IMAGE_REPODIGEST},
        "grants": grants,
        "ticket_present": ticket is not None,
        "ticket": ticket,
        "ticket_preflight_ok": pf_usage is not None,
        "preflight_usage": pf_usage,
        "plan_check": plan,
        "ledger_call_rows_before_after": [before, after],
        "smoke": smokes,
        "container_residue": [],
        "free_gib_mnt": round(free_gib, 1),
        "gpu_mib_entry": gpu_mib,
        "skills_import_probe": skills_probe,
        "paid_calls": 0,
    }
    fc.write_json(sroot / "preflight.json", payload)
    fc.write_json(sroot / "preflight_ledger_delta.json", {
        "schema": "simpraxis.phase27.t4_preflight_ledger_delta.v1",
        "ts_iso": utc_iso(), "before": before, "after": after,
        "delta": after - before, "paid_calls": 0})
    fc.write_json(sroot / "manifests" / "sealed_ledger_snapshot.json",
                  {"ts_iso": utc_iso(), "paths": _sealed_snapshot()})
    log = [
        f"phase27 task4 preflight  mode={mode}  ts={payload['ts_iso']}",
        f"gate_hashes: {json.dumps(fc.gate_hashes(), ensure_ascii=False)}",
        f"image: {IMAGE_ID} {IMAGE_REPODIGEST}",
        f"model coding_debug: {payload['model']['coding_debug']}",
        f"plan_check ok={plan['ok']} task_used={plan['checks'][0]['task_used']}"
        f" task_cap={TASK_CAP} global_used={plan['checks'][0]['global_used']}"
        f" global_cap={PHASE27_HARD_CAP} envelope_sum={plan['envelope_sum']}",
        f"ticket_present={ticket is not None}"
        f" ticket_preflight_ok={pf_usage is not None}",
        f"ledger call rows before/after: {before}/{after}"
        f" (delta {after - before})",
    ]
    for w in WALLS:
        log.append(f"{w}: smoke={smokes[w]['smoke_stdout'][:120]!r} "
                   f"expr={smokes[w]['expr_stdout'][:120]!r}")
    if skills_probe is not None:
        log.append(f"skills_import_probe: rc={skills_probe['rc']} "
                   f"sha12={skills_probe['manifest_sha12']} "
                   f"files={skills_probe['files']} "
                   f"stdout={skills_probe['stdout'][:60]!r}")
    log.append(f"residue=[] free_gib_mnt={free_gib:.1f} gpu_mib_entry={gpu_mib}")
    log.append("paid_calls=0")
    fc.atomic_write_bytes(sroot / "preflight.log",
                          ("\n".join(log) + "\n").encode("utf-8"))
    print("\n".join(log))
    return 0


# ------------------------------------------------------------------ 早停预算

class EarlyStopBudget(BudgetTracker):
    """模式 R 的第 8 轮熔断（D4-8，预注册钉死；不触碰引擎）。

    规则：**跑完第 8 轮**时，若 ``rounds_to_first_birth is None`` 且 workdir
    内没有任何入口脚本落盘（``birth_absent`` 已成定局），中止本次 run。
    判决**仍为负**（``NOT_ESTABLISHED``），不得因早停改判；未用额度在任务帽
    63 之内转为可用余额（不新开票、不动 178）。

    实现：子类化 ``BudgetTracker``，只在 ``add_round`` 的**开轮前**判定一次，
    不改 ``CapExceeded`` 语义（kind 仍是 ``birth_absent``）。
    """

    def __init__(self, *, stop_after: int, workdir: Path,
                 entry_names: tuple[str, ...], **kw: Any) -> None:
        super().__init__(**kw)
        self._stop_after = int(stop_after)
        self._workdir = Path(workdir)
        self._entry_names = tuple(entry_names)
        self.early_stopped = False

    def add_round(self) -> None:
        if (self.rounds_used >= self._stop_after
                and self.born_at_round is None
                and _born_entrypoint(self._workdir, self._entry_names) is None):
            self.early_stopped = True
            raise CapExceeded(
                KIND_BIRTH_ABSENT,
                f"模式 R 第 {self._stop_after} 轮早停：出生缺席已成定局"
                f"（rounds_used={self.rounds_used}）")
        super().add_round()


# ------------------------------------------------------------------ 真跑

def _run_base(wall: str) -> Path:
    return session_root() / wall


def _dir_mode(d: Path) -> "str | None":
    """读一次尝试目录的标尺：run_meta.json 优先，其次 wall_result.json。"""
    for name in ("run_meta.json", "wall_result.json"):
        p = d / name
        if p.is_file():
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            return (rec or {}).get("mode")
    return None


def _attempt_dirs(wall: str, mode: "str | None" = None) -> list[Path]:
    """尝试目录；给 mode 时只留该标尺的目录（两模式互不遮蔽）。

    锁内适配（2026-11-04）：原实现按**墙**枚举，模式 R 的正式结果会把模式 N
    的同一堵墙挡在 I4-5 单跑闸之外。计划 v1.4d 已把 N 预注册为**独立标尺**
    （另票 `P27-B1b`、另判据锁、15/20/0 子帽），且 N 判据明文「R 失败不阻塞
    N；N 结果不回写 R 的判决」。故枚举、单跑闸、判官三处一律按 (墙, 模式)
    作用域——同一标尺内 I4-5 语义（判负不重跑同一墙）原样不变。
    """
    base = _run_base(wall)
    if not base.is_dir():
        return []
    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    if mode is None:
        return dirs
    return [p for p in dirs if _dir_mode(p) == mode]


def _attempt_records(wall: str, mode: "str | None" = None) -> list[dict]:
    out = []
    for d in _attempt_dirs(wall, mode):
        p = d / "wall_result.json"
        if p.is_file():
            out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def _unrecorded_attempts(wall: str, mode: "str | None" = None) -> list[str]:
    """已开跑但既无判决件、也**未做人工分诊**的尝试目录。

    分诊件即计划 §T4.7 证据树里的 ``FAILURE.json``：一次尝试若在首呼前就崩
    （如配置校验失败、容器起不来），由人写一份 ``FAILURE.json`` 说明根因与
    实呼为 0，该目录即视为已分诊、不再挡后续开跑；没有分诊件的残目录仍然挡。
    """
    out = []
    for d in _attempt_dirs(wall, mode):
        if (d / "run_meta.json").is_file() \
                and not (d / "wall_result.json").is_file() \
                and not (d / "FAILURE.json").is_file():
            out.append(str(d))
    return out


def _guard_single_run(wall: str, mode: str) -> int:
    """I4-5：同一墙、**同一标尺**下只许一轮非基础设施故障的正式结果。"""
    unrecorded = _unrecorded_attempts(wall, mode)
    if unrecorded:
        raise SystemExit(
            f"{wall} 存在已开跑但未落判决件的尝试：{unrecorded}。先人工分诊"
            "（查 rounds.jsonl / outcome.json 与账行 seq 区间）再决定。")
    recs = _attempt_records(wall, mode)
    if not recs:
        return 0
    non_infra = [r for r in recs if not r.get("infra_failure")]
    if non_infra:
        raise SystemExit(
            f"{wall}（模式 {mode}）已有非基础设施故障的正式结果："
            f"{non_infra[0].get('run_dir')}"
            f"（passed={non_infra[0].get('passed')}）。判负不重跑同一墙。")
    if len(recs) >= 2:
        raise SystemExit(f"{wall}（模式 {mode}）储备重跑额度已用尽"
                         f"（已有 {len(recs)} 次尝试）")
    retries = sum(max(0, len(_attempt_records(w, mode)) - 1) for w in WALLS)
    reserve = mode_table(mode)["reserve"]
    if retries + 1 > reserve:
        raise SystemExit(f"储备池 {reserve} 已耗尽（已重跑 {retries} 次）")
    return len(recs)


def _subcap(mode: str, wall: str) -> int:
    return int(mode_table(mode)[wall])


def _last_round_tools(events: list[dict]) -> list[str]:
    rows = [e for e in events if e.get("tool") == MODEL_ROUND_TOOL]
    if not rows:
        return []
    args = rows[-1].get("args") or {}
    tools = args.get("tools") or args.get("tool_calls") or []
    out = []
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name") or t.get("tool")
                if isinstance(name, str):
                    out.append(name)
            elif isinstance(t, str):
                out.append(t)
    return out


def cmd_run(wall: str, mode: str, batch: str) -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("拒绝裸跑付费墙：需要 SIMPRAXIS_ALLOW_PAID=1（硬规则 2 双守卫）",
              file=sys.stderr)
        return 2
    if wall not in WALLS:
        raise SystemExit(f"未知墙 {wall!r}")
    if batch != batch_of(mode):
        raise SystemExit(f"批次号必须是单票 {batch_of(mode)}（拆票即 "
                         "BLOCKED_BATCH_APPROVAL_MISSING）：{batch}")
    sroot = session_root()
    pf_p = sroot / "preflight.json"
    if not pf_p.is_file():
        raise SystemExit(f"缺少本批预检件 {pf_p}：先跑 --preflight --mode {mode}")
    pf = json.loads(pf_p.read_text(encoding="utf-8"))
    if pf.get("mode") != mode:
        raise SystemExit(f"预检件模式不符：{pf.get('mode')!r} vs {mode!r}")
    if str((pf.get("ticket") or {}).get("batch_id")) != batch:
        raise SystemExit(
            f"预检件批次不符：{pf.get('ticket', {}).get('batch_id')!r} vs {batch!r}")
    if pf.get("ticket_preflight_ok") is not True:
        raise SystemExit("预检件未带票跑过账本 preflight：开票后必须重跑 --preflight")
    if pf.get("paid_calls") != 0:
        raise SystemExit(f"预检件自证 paid_calls={pf.get('paid_calls')!r} ≠ 0")
    if not pf.get("plan_check", {}).get("ok"):
        raise SystemExit("预检件的预算干跑未通过，拒绝开跑（D4-7）")
    if plan_check()["ok"] is not True:   # 开跑前再算一次（票据已占用）
        raise SystemExit("开跑前预算干跑失败（D4-7），拒绝开跑")
    # 开跑前的预算干跑**随跑落盘**：先前的写法只在 --preflight/--write-criteria
    # 落 plan_check.json，开跑时不刷新，导致「最后一次真开跑」的预算现状（尤其
    # 是票已花掉多少）在件里是旧的。这里带 written_by 落一次，事后可审计。
    _persist_plan_check(f"run:{wall}:{mode}")

    root = fc.freeze_root()
    verify_freeze(root)
    lock = verify_criteria_lock(mode)
    attempt_index = _guard_single_run(wall, mode)
    grant_strings = grants_for(wall)
    grants = parse_grant_strings(grant_strings)
    cfg = CFG[mode]
    subcap = _subcap(mode, wall)
    envelope = int(mode_table(mode)["total"])
    spent = _batch_spent(mode)
    remaining = envelope - spent
    if remaining <= 0:
        raise SystemExit(
            f"批次 {batch} 信封 {envelope} 已用尽（已实呼 {spent}）：到帽即停、"
            "不追加；确需增呼须报用户另开票据（D4-8b：N 的实际实呼上限仍为信封）。")
    round_cap = min(subcap, remaining)
    tail_rounds = TAIL_ROUNDS if cfg.verify_repair_tail else 0
    tail_withheld = None
    if remaining < subcap + tail_rounds:
        # 锁内适配 #12（2026-11-04，登记于 Phase_2.7_Plan_Complete.md §5.0b）：
        # 计划的 MODE_N 信封 35 = wallA 15 + wallB 20 + reserve 0，恰好**不留**尾轮
        # 余量，而 CFG_N 又要求 verify_repair_tail_rounds=3；两者只在「wallA 未触发
        # 追授」时相容。模式 N 的 wallA 首次真的追授了 3 轮（18 呼），此时若仍按
        # 名义子帽 20 + 尾轮 3 跑 wallB，批次实呼会到 38–41，超出票面 approved=35
        # —— D4-8b 明文「N 的实际实呼上限仍为 35」，§6.2 也禁止「先跑超了再报」。
        # 故按**票额剩余**钳制本轮 round_cap，并在剩余不足以容纳尾轮时扣留尾轮
        # （tail_withheld_reason 落 run_meta 与 wall_result）。这是**收紧**而非放宽：
        # 钳制只会让本轮可用的轮数变少，永远不会让判决更容易通过。
        tail_withheld = (f"ticket_envelope_remaining={remaining} < "
                         f"subcap({subcap})+tail({tail_rounds})")
        tail_rounds = 0
        # 内环有硬校验「cfg.verify_repair_tail=True ⇒ budget 追授轮数 > 0」，
        # 故扣留尾轮时必须同时把 cfg 侧的追授关掉（而非只把 budget 置 0）；
        # 这是同一次「收紧」的两个面，meta 里两处取值保持一致、可机判。
        cfg = dataclasses.replace(cfg, verify_repair_tail=False,
                                  verify_repair_tail_rounds=0)

    stamp = utc_stamp()
    thread_id = f"p27-{mode.lower()}-{wall.lower()}-{stamp}"
    base = fc.ensure_dir(_run_base(wall))
    if (base / stamp).exists():
        raise SystemExit(f"运行目录已存在（时间戳冲突）：{stamp}")
    run_dir = fc.ensure_dir(base / stamp)
    work = fc.ensure_dir(run_dir / "work")
    # 模式 N 的墙 B 才挂技能库（R 与 V4 逐字同条件；新杠杆不得漏到 R）。
    skills_rec = _stage_skills_into(work) if (mode == "N" and wall == WALL_B) else None
    text = task_text(wall, mode,
                     skills_rec["manifest_sha12"] if skills_rec else None)
    fc.atomic_write_bytes(run_dir / "task_prompt.txt", text.encode("utf-8"))
    meta = {
        "schema": "simpraxis.phase27.t4_run_meta.v1",
        "mode": mode, "wall": wall, "batch_id": batch, "thread_id": thread_id,
        "started_iso": utc_iso(), "subcap": subcap,
        "envelope": mode_table(mode)["total"],
        "reserve": mode_table(mode)["reserve"],
        "ticket_spent_before": spent,
        "ticket_remaining_before": remaining,
        "round_cap_applied": round_cap,
        "entrypoint_names": list(ENTRYPOINT_NAMES),
        "tail_rounds": tail_rounds,
        "tail_withheld_reason": tail_withheld,
        "subcap_excludes_tail": True,
        "model": {"coding_debug": t26.coding_debug_model_id()},
        "agentic_config": {
            "verify_repair_tail": cfg.verify_repair_tail,
            "verify_repair_tail_rounds": cfg.verify_repair_tail_rounds,
            "run_expr": cfg.run_expr,
            "tolerant_patch": cfg.tolerant_patch,
            "scratchpad": cfg.scratchpad,
            "env_spec": cfg.env_spec,
            "render_preview": cfg.render_preview,
            "run_mode": cfg.run_mode,
        },
        "grants": list(grant_strings),
        "task_prompt_sha256": fc.sha256_file(run_dir / "task_prompt.txt"),
        "task_prompt_bytes": len(text.encode("utf-8")),
        "criteria_sha256": lock["criteria_sha256"],
        "image": {"id": IMAGE_ID, "repo_digest": IMAGE_REPODIGEST},
        "gate_hashes": fc.gate_hashes(),
        "callcache_enabled": False,
        "early_stop_rule": (EARLY_STOP_REASON
                            if (mode == "R" and wall == WALL_A) else None),
        "skills_stage": skills_rec,
    }
    fc.write_json(run_dir / "run_meta.json", meta)
    print(f"[p27t4] {wall} 开跑 mode={mode} batch={batch} thread={thread_id} "
          f"subcap={subcap} round_cap={round_cap} tail={tail_rounds} "
          f"env_remaining={remaining} run_dir={run_dir}", flush=True)

    ledger = phase27_ledger()
    gateway = ModelGateway(ledger=ledger)
    common_budget = dict(
        step_cap=0, token_cap=0, round_cap=round_cap,
        verify_repair_tail_rounds=tail_rounds)
    if mode == "R" and wall == WALL_A:
        # D4-8：只有模式 R 的墙 A 有第 8 轮熔断（预注册钉死的那一条）。
        budget: BudgetTracker = EarlyStopBudget(
            stop_after=EARLY_STOP_ROUND, workdir=work,
            entry_names=ENTRYPOINT_NAMES, **common_budget)
    else:
        budget = BudgetTracker(**common_budget)
    tracer = TraceStore(run_dir / "traces", thread_id)
    backend = SpikeWallBackend(wall, freeze_dir=root)

    with GpuSampler(run_dir / "gpu_mib.csv") as gpu_sampler, \
            billing_context(task=TASK_ID, batch_id=batch, thread_id=thread_id):
        outcome = run_agentic_inner_loop(
            workdir=work, task=text, grants=grants,
            gateway=gateway, budget=budget, tracer=tracer,
            thread_id=thread_id, run_backend=backend,
            probe_registry=None, recall_port=None,
            call_cache=None, nonprod=False,
            seq_provider=build_seq_provider(),
            agentic_config=cfg,
            entrypoint_names=ENTRYPOINT_NAMES)

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
        "early_stopped": bool(getattr(budget, "early_stopped", False)),
        "extra": dict(outcome.extra or {}),
    }
    fc.write_json(run_dir / "outcome.json", outcome_rec)
    print(f"[p27t4] loop 终局 kind={outcome.kind} rounds={outcome.rounds_used} "
          f"steps={outcome.steps_used}", flush=True)

    oom_before = t26._read_oom_kill_counts()
    disk_free_before = shutil.disk_usage("/mnt").free
    scored = t26._score_once(wall, root, work)
    oom_kill_delta_at_score = t26._oom_kill_delta(oom_before,
                                                  t26._read_oom_kill_counts())
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

    events = t26._load_trace_events(Path(outcome.trace_path))
    metrics = t26._derive_metrics(outcome, events, scored)
    with open(run_dir / "rounds.jsonl", "w", encoding="utf-8") as fh:
        for rec in metrics.pop("round_detail"):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    seq_rows = t26._identity_rows(ledger, batch, thread_id)
    n_calls = len(seq_rows)
    metrics["paid_calls"] = n_calls
    metrics["ledger_seq"] = [r.get("seq") for r in seq_rows]
    score = scored.get("score") or {}
    infra_failure, infra_class = t26.classify_infra_failure(
        outcome.kind, str(outcome.reason or ""),
        scorer_rc=scored["rc"], oom_kill_delta=oom_kill_delta_at_score,
        disk_free_gib=disk_free_gib_at_score)
    wall_result = {
        "schema": WALL_RESULT_SCHEMA,
        "mode": mode, "wall": wall, "batch_id": batch, "thread_id": thread_id,
        "ts_iso": utc_iso(), "run_dir": str(run_dir),
        "attempt_index": attempt_index,
        "subcap": subcap, "envelope": mode_table(mode)["total"],
        "round_cap_applied": round_cap,
        "ticket_spent_before": spent,
        "ticket_remaining_before": remaining,
        "tail_withheld_reason": tail_withheld,
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
        "cap_exhausted": n_calls >= round_cap,
        "gpu_max_mib_while_running": gpu_sampler.max_mib,
        "gpu_final_mib": gpu_final,
        "gpu_closeout_ok": gpu_final <= GPU_CLOSEOUT_MIB,
        "early_stop_occurred": bool(getattr(budget, "early_stopped", False)),
        # AC-T4-3（模式 N 额外）：工具层的拒绝计数如实登记（deny>0 不判失败，
        # 但必须在报告里给出原因）。口径与判据 §3 的 policy deny 一致。
        "policy_denies": sum(
            1 for e in events
            if isinstance(e.get("policy"), dict)
            and e["policy"].get("decision") not in (None, "allow")),
        **metrics,
    }
    born = wall_result["rounds_to_first_birth"]
    if wall == WALL_A:
        score_ok = t26.wall_a_score_ok(
            scored["rc"], score, provenance_ok=bool(scored["score_file_unique"]))
        wall_result["score_checks_ok"] = score_ok
        limit = birth_limit(mode)
        wall_result["birth_round_limit"] = limit
        wall_result["passed"] = bool(
            born is not None and int(born) <= limit
            and wall_result["final_rc"] == 0 and score_ok
            and int(wall_result["run_expr_ok_calls"]) >= 1
            and int(wall_result["tail_rounds"]) >= 1)
        wall_result["fail_reasons"] = [
            name for name, cond in (
                ("birth_round_absent_or_gt_limit",
                 born is None or born > limit),
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
    # §T4.9 触帽解读口径（v1.3）：必须同时报 rounds_used / 末轮工具动作类型 /
    # FAEA 锚点。「触帽」只说明预算用尽，不说明模型做不到。
    wall_result["amortization_note"] = {
        "rounds_used": wall_result["rounds"],
        "cap_exhausted": wall_result["cap_exhausted"],
        "last_round_tools": _last_round_tools(events),
        "faea_anchor": ("LIBERO-Spatial 37.3 ReAct turn/task（口径 = 一次模型调用"
                        "一个 turn，与 caps.add_round 同）；Wall A 帽 16(R)/15(N) "
                        "均低于该均值。"),
        "reading": ("触帽（rounds_used == subcap）只说明预算用尽，不说明模型做不到；"
                    "未触帽但失败是更强的负证据。"),
    }
    fc.write_json(run_dir / "wall_result.json", wall_result)
    print(json.dumps(wall_result, indent=1, ensure_ascii=False))
    print(f"[p27t4] {wall} 完成：passed={wall_result['passed']} "
          f"calls={n_calls}/{round_cap} gpu_final={gpu_final}MiB", flush=True)
    if gpu_final > GPU_CLOSEOUT_MIB:
        raise SystemExit(f"收尾显存 {gpu_final} > {GPU_CLOSEOUT_MIB} MiB"
                         "（硬规则 5）；不擅杀他方进程，STOP 人工核")
    return 0


# ------------------------------------------------------------------ 判定

def cmd_judge(mode: str) -> int:
    sroot = session_root()
    lock = verify_criteria_lock(mode)
    recs = {w: _attempt_records(w, mode) for w in WALLS}
    missing = [w for w in WALLS if not recs[w]]
    if missing:
        raise SystemExit(f"墙结果件缺失：{missing}（未跑完不得判定）")
    latest = {w: recs[w][-1] for w in WALLS}
    both_passed = all(latest[w].get("passed") is True for w in WALLS)
    pool_used = len(_batch_rows(mode))
    envelope = mode_table(mode)["total"]
    infra = {w: bool(latest[w].get("infra_failure")) for w in WALLS}

    total_rounds = sum(int(latest[w].get("rounds") or 0) for w in WALLS)
    productive = sum(int(latest[w].get("productive_rounds") or 0)
                     for w in WALLS)
    ratio = round(productive / total_rounds, 6) if total_rounds else None

    if both_passed and ratio is not None and ratio >= EFFECTIVE_RATIO_FLOOR:
        verdict = V_ESTABLISHED
    elif (not both_passed) and any(infra.values()) and pool_used >= envelope:
        verdict = V_INCONCLUSIVE
    else:
        verdict = V_NOT_ESTABLISHED

    early = bool(latest[WALL_A].get("early_stop_occurred")) if mode == "R" else False
    rounds_used_a = int(latest[WALL_A].get("rounds") or 0)
    early_block = {
        "occurred": early,
        "rounds_used": rounds_used_a,
        "early_stop_reason": EARLY_STOP_REASON if early else None,
        "transferred_to": "N" if early else None,
        # AC-T4-9③：转移额 = 本票信封（28）− 已用轮数（D4-8b：不新开票）
        "transfer_amount": (int(mode_table(mode)["total"]) - rounds_used_a
                            if early else 0),
        "comparable_rounds": (EARLY_STOP_ROUND if early else
                              int(mode_table(mode)[WALL_A])),
    }
    if early and verdict != V_NOT_ESTABLISHED:
        raise SystemExit("早停不得改判：判决必须仍为 NOT_ESTABLISHED（D4-8）")

    payload = {
        "schema": VERDICT_SCHEMA,
        "mode": mode,
        "batch_id": batch_of(mode),
        "ts_iso": utc_iso(),
        "criteria_sha256": lock["criteria_sha256"],
        "entrypoint_names": list(ENTRYPOINT_NAMES),
        "subcaps": dict(mode_table(mode)),
        "verdict": verdict,
        "pool_used": pool_used,
        "envelope": envelope,
        "reserve_left": max(0, envelope - pool_used),
        "effective_step_ratio": ratio,
        "ratio_floor": EFFECTIVE_RATIO_FLOOR,
        "walls": {w: {
            "passed": latest[w].get("passed"),
            "run_dir": latest[w].get("run_dir"),
            "fail_reasons": latest[w].get("fail_reasons"),
            "infra_failure": latest[w].get("infra_failure"),
            "paid_calls": latest[w].get("paid_calls"),
            "rounds": latest[w].get("rounds"),
            "productive_rounds": latest[w].get("productive_rounds"),
            "rounds_to_first_birth": latest[w].get("rounds_to_first_birth"),
            "final_rc": latest[w].get("final_rc"),
            "checks_n": latest[w].get("checks_n"),
            "early_stop_occurred": latest[w].get("early_stop_occurred"),
            "amortization_note": latest[w].get("amortization_note"),
        } for w in WALLS},
        "early_stop": early_block,
        "comparable_to_v3_v4": {
            "comparable": mode == "R",
            "basis": ("模式 R = V4 判据逐字（V3 + D26-8d 已登记的出生件名）；"
                      "相对 V3 新增可机判条目 0 条。"
                      if mode == "R" else
                      "模式 N 是新标尺（ENV_SPEC/预览/资产/技能库全开），"
                      "与 V3/V4 **不可比**。"),
            "comparable_rounds": early_block["comparable_rounds"],
        },
        "unfreeze": {
            "recommended": False,
            "note": ("本 Task 不解冻 RunSpec.agentic（BR-2 用户裁决）；"
                     "解冻三件套（新预注册 + 门 1 双 lane + T5/T5b 重跑）未齐备。"),
        },
    }
    out = sroot / f"verdict_phase27_{mode}.json"
    fc.write_json(out, payload)
    _write_report(sroot, mode, payload)
    _write_unfreeze_recommendation(sroot, mode, payload)
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"[p27t4] 判决 = {verdict} → {out}")
    return 0


def _write_report(sroot: Path, mode: str, verdict: dict) -> Path:
    comparable = verdict["comparable_to_v3_v4"]
    lines = [
        f"# Phase 2.7 Task 4 破局重测报告（模式 {mode}）",
        "",
        f"- 判决：**{verdict['verdict']}**",
        f"- 票据：`{verdict['batch_id']}`（信封 {verdict['envelope']}，"
        f"实呼 {verdict['pool_used']}，余 {verdict['reserve_left']}）",
        f"- 判据 sha256：`{verdict['criteria_sha256']}`（首呼前 LOCK）",
        "",
        "## 可比性声明",
        "",
        ("**模式 R 与 V3/V4 可比**" if comparable["comparable"]
         else "**模式 N 与 V3/V4 不可比**") + "——" + comparable["basis"],
        "",
        ("另一模式（`P27-B1b`：ENV_SPEC + 预览 + 资产 + 技能库全开）是**不可比**的"
         "新标尺，其数字**不得**与本报告并列或折算比较（AC-T4-6）。"
         if mode == "R" else
         "另一模式（`P27-B1`：与 V3/V4 逐字同条件）与本报告**不可比**——"
         "本报告是新标尺结果，不得与 R/V3/V4 并列或折算比较（AC-T4-6）。"),
        "",
    ]
    if comparable["comparable"] and verdict["early_stop"]["occurred"]:
        lines += [
            f"**早停诚实标注**：`comparable_rounds={verdict['early_stop']['comparable_rounds']}`，"
            f"第 {verdict['early_stop']['comparable_rounds'] + 1}–"
            f"{verdict['subcaps']['wallA']} 轮未执行（D4-8 第 8 轮熔断）；"
            "本报告只覆盖前 8 轮（第 9–16 轮未执行），**不得**当作整批结果引用。",
            "",
            "> 注：`transfer_amount` 是**早停时刻**（仅 Wall A 已跑）的记账口径 "
            "= 28 − 8 = 20；R 的**实际**总实呼见上方票据行的 `pool_used`"
            "（Wall A + Wall B），两者不可相互折算。",
            "",
        ]
    lines += ["## 两墙结果", "",
              "| 墙 | passed | 付费调用 | 轮 | 有效轮 | 首出生轮 | final_rc | 检查数 | 失败项 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for w in WALLS:
        d = verdict["walls"][w]
        lines.append(
            f"| {w} | {d['passed']} | {d['paid_calls']} | {d['rounds']} | "
            f"{d['productive_rounds']} | {d['rounds_to_first_birth']} | "
            f"{d['final_rc']} | {d['checks_n']} | "
            f"{', '.join(d['fail_reasons'] or []) or '—'} |")
    lines += [
        "",
        f"合并有效步数占比：**{verdict['effective_step_ratio']}** "
        f"（判据 ≥ {verdict['ratio_floor']}）",
        "",
    ]
    # 关键读数：判件在场、12 项见证全过、final_rc=0，却因**轮/呼预算**未过墙时，
    # 必须逐字写明「墙是搭成了的，败在预注册的调用数上限」——否则读者会把
    # 「passed=False」误读成「模型做不到」（§T4.9 诚实标注口径，硬规则 7：
    # 判据不改，但读法不许歪）。
    for w in WALLS:
        d = verdict["walls"][w]
        if (int(d.get("checks_n") or 0) == 12 and d.get("final_rc") == 0
                and not d.get("passed")):
            lines += [
                f"> **{w} 关键读数**：评分件在场、12 项见证全过、`final_rc=0`，"
                f"该墙的构造性目标**已达成**；`passed=False` 只因预注册的调用数"
                f"上限未守住（`{'/'.join(d.get('fail_reasons') or [])}`："
                f"{d.get('paid_calls')} 呼 > 15）。判据不改（硬规则 7），"
                "但不得据此表述为「模型搭不出该墙」。",
                "",
            ]
    lines += [
        "## 触帽解读口径（§T4.9 / v1.3）",
        "",
    ]
    for w in WALLS:
        note = verdict["walls"][w].get("amortization_note") or {}
        lines.append(f"- **{w}**：rounds_used={note.get('rounds_used')}、"
                     f"触帽={note.get('cap_exhausted')}、末轮工具="
                     f"{note.get('last_round_tools')}；{note.get('faea_anchor')}")
    lines += [
        "",
        "## 解冻建议",
        "",
        "**不建议解冻**：本 Task 只交付证据，解冻是用户裁决（BR-2）。"
        "解冻三件套（新预注册 + 门 1 双 lane 同 sha + T5/T5b 重跑）未齐备，"
        "`RunSpec.agentic` 默认值**未被本 Task 修改**。",
        "",
        "## 锁内适配",
        "",
        "详见 `Phase_2.7_Plan_Complete.md` §5；要点：判据正文复用 phase26 单一源、"
        "路径/票号/LOCK 自建（phase26 函数硬绑封存根）、`CFG_N` 补 `run_mode=\"N\"`、"
        "运行目录 `wall/<stamp>/`、模式 R 出生件名 = V4 的 `agent_solution.py`、"
        "预检分两段（开票前后各一次）。",
        "",
    ]
    p = sroot / f"rebreakthrough_report_{mode}.md"
    fc.atomic_write_bytes(p, ("\n".join(lines) + "\n").encode("utf-8"))
    return p


def _write_unfreeze_recommendation(sroot: Path, mode: str,
                                   verdict: dict) -> Path:
    lines = [
        f"# 解冻建议（Phase 2.7 Task 4，模式 {mode}）",
        "",
        f"- 判决：**{verdict['verdict']}**",
        "- 建议：**不解冻**。`RunSpec.agentic` 维持 opt-in / 冻结。",
        "",
        "依据（Phase 2.5 签收时写死的三条件，BR-2 用户裁决要求全部满足才可解冻）：",
        "",
        "1. 新预注册件（本 Task 只做了**双墙重测**的预注册，不是解冻预注册）；",
        "2. 门 1 双 lane 同 sha 的独立签署 —— 未做；",
        "3. T5 + T5b 重跑（≥ 50 呼，另开信封）与 T4 契约重跑 —— 未做。",
        "",
        "本件不构成解冻许可，也不改写任何默认值；重启任一前置须用户另行裁决。",
        "",
    ]
    p = sroot / f"unfreeze_recommendation_{mode}.md"
    fc.atomic_write_bytes(p, ("\n".join(lines) + "\n").encode("utf-8"))
    return p


# ------------------------------------------------------------------ CLI

def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--open-ticket", action="store_true")
    ap.add_argument("--write-criteria", action="store_true")
    ap.add_argument("--seal-criteria", action="store_true")
    ap.add_argument("--verify-lock", action="store_true")
    ap.add_argument("--plan-check", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--mode", choices=sorted(MODES), default=None)
    ap.add_argument("--wall", choices=list(WALLS), default=None)
    ap.add_argument("--batch", default=None)
    args = ap.parse_args(argv)

    actions = [n for n in ("preflight", "open_ticket", "write_criteria",
                           "seal_criteria", "verify_lock", "plan_check", "run",
                           "judge")
               if getattr(args, n)]
    if len(actions) != 1:
        ap.error("必须且只能指定一个动作：" + ", ".join(actions or ["（无）"]))
    action = actions[0]
    if action in ("preflight", "open_ticket", "write_criteria", "seal_criteria",
                  "verify_lock", "plan_check", "judge") and not args.mode:
        ap.error(f"--{action.replace('_', '-')} 需要 --mode {{R,N}}")

    if action == "preflight":
        return cmd_preflight(args.mode)
    if action == "open_ticket":
        mode = args.mode
        return cmd_open_ticket(mode, args.batch or batch_of(mode))
    if action == "write_criteria":
        return cmd_write_criteria(args.mode)
    if action == "seal_criteria":
        return cmd_seal_criteria(args.mode)
    if action == "verify_lock":
        print(json.dumps(verify_criteria_lock(args.mode), indent=1,
                         ensure_ascii=False))
        return 0
    if action == "run":
        if not args.wall:
            ap.error("--run 需要 --wall {wallA,wallB}")
        mode = args.mode or "R"
        return cmd_run(args.wall, mode, args.batch or batch_of(mode))
    if action == "plan_check":
        plan = _persist_plan_check(f"plan-check:{args.mode}")
        print(json.dumps(plan, indent=1, ensure_ascii=False))
        return 0 if plan["ok"] else 1
    return cmd_judge(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
