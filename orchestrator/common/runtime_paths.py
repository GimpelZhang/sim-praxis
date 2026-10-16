"""Phase 2 运行时路径解析的唯一入口（Task0 工程基座）。

Phase 1 的台账/状态路径散落在多个模块各自读 ``SIMPRAXIS_PHASE1_ROOT``；
Phase 2 起台账类路径（ledger/counters/approvals）统一由本模块解析到
**独立新账** ``$SIMPRAXIS_PHASE2_ROOT/quota/``，Phase 1 封存账保持只读、
永不被新代码以写方式打开（D-1：独立新账，seq 从 0 起）。

目录布局（全部 /mnt，仓外，0700；见 scripts/00_env.sh）：
  $SIMPRAXIS_PHASE1_ROOT/quota/ledger.jsonl      # Phase 1 封存账（29 行，只读）
  $SIMPRAXIS_PHASE2_ROOT/quota/ledger.jsonl      # Phase 2 新账（本模块入口）
  $SIMPRAXIS_PHASE2_ROOT/runs/                   # Phase 2 实跑产物根
  $SIMPRAXIS_GATEWAY_STATE/quota_pause.json      # 熔断态（沿用，不随账分根）

未导出 SIMPRAXIS_PHASE2_ROOT 时回退到 phase1 根的同级 ``phase2/``：
生产默认布局即 /mnt/artifacts/simpraxis/{phase1,phase2}；测试若只把
SIMPRAXIS_PHASE1_ROOT 指向临时根，新账自动落在临时根兄弟下，绝不误写真实
/mnt（锁内适配（Task0）：计划只写「读 SIMPRAXIS_PHASE2_ROOT」，未规定缺省值）。
"""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.common.fsutil import ensure_private_dir

DEFAULT_DATA_ROOT = "/mnt/artifacts/simpraxis"
_GATEWAY_STATE_ENV = "SIMPRAXIS_GATEWAY_STATE"
_PHASE1_ENV = "SIMPRAXIS_PHASE1_ROOT"
_PHASE2_ENV = "SIMPRAXIS_PHASE2_ROOT"


def phase1_root() -> Path:
    return Path(os.environ.get(_PHASE1_ENV, f"{DEFAULT_DATA_ROOT}/phase1"))


def phase2_root() -> Path:
    """Phase 2 根：显式 env 优先；否则取 phase1 根同级 ``phase2/``（不建目录）。"""
    root = os.environ.get(_PHASE2_ENV)
    if root:
        return Path(root)
    return phase1_root().parent / "phase2"


def phase2_runs_root() -> Path:
    return phase2_root() / "runs"


# ---- 台账类路径（Phase 2 独立新账；0700 目录在 quota_dir() 内收敛） ----

def phase2_quota_dir() -> Path:
    return ensure_private_dir(phase2_root() / "quota")


def phase2_ledger_path() -> Path:
    return phase2_quota_dir() / "ledger.jsonl"


def phase2_counters_path() -> Path:
    return phase2_quota_dir() / "counters.json"


def phase2_approvals_dir() -> Path:
    return ensure_private_dir(phase2_quota_dir() / "approvals")


# ---- 熔断态（沿用独立 env/默认根，Task0 只统一入口、不改 env 契约） ----

def gateway_state_dir() -> Path:
    return Path(os.environ.get(_GATEWAY_STATE_ENV,
                               f"{DEFAULT_DATA_ROOT}/gateway_state"))


def gateway_state_path() -> Path:
    return gateway_state_dir() / "quota_pause.json"
