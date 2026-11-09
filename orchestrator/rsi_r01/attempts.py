"""RSI-R0.1 调用事实层：槽位硬门、预留/结算、UNKNOWN 恢复、资源帽。

计划 §6.3 与 §9.3 的落地。三份权威仍分开（复用 R0 已验证的纯组件，不重写）：

* ``quota/ledger.jsonl``        —— 调用结算（谁花了第几个 seq，由 ``QuotaLedger`` 管）
* ``quota/attempts.jsonl``      —— 发网事实（``AttemptJournal``，路径显式传 R0.1）
* ``quota/reservations/``       —— 先占后花、崩溃不释放（``BudgetReservations``）

本模块只加两样 R0.1 专属的东西：

1. **槽位账（:class:`SlotBudget`）**——协议 12 / 每族 72 / 每 trial 24 / 全局 228，
   每 trial 未用额度**不得转移**（AC24：任一越界拒绝，不挪未用额度）；
2. **资源账（:class:`TrialResources`）**——每 trial ≤45 min、≤12 次候选实验、
   单次 ≤100000 步、每 trial ≤1000000 步、全阶段 ≤10000000 步。

``preflight_blocked`` 与真正 POST 严格分开：发网前被拒的是一份独立事件，不占
seq、不算实呼，但**占槽**（该 slot 已消耗，不补考）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.rsi_r0.attempts import (  # 复用通过验证的纯组件
    BudgetReservations,
    AttemptJournal,
    ConcurrentCampaignError,
    UnknownAttemptError,
    campaign_lock,
    new_request_id,
)
from orchestrator.rsi_r0.contracts import ResourceCapExceeded, ResourceCaps
from orchestrator.rsi_r01 import schemas as S

__all__ = [
    "BudgetReservations", "AttemptJournal", "ConcurrentCampaignError",
    "UnknownAttemptError", "campaign_lock", "new_request_id",
    "RSI_R01_CAPS", "SlotBudget", "SlotExhausted", "TrialResources",
    "ResourceExceeded", "quota_paths",
]

#: R0.1 的累计帽（计划 §6.3 表；输出帽与 R0 不同，不能直接沿用 R0 的默认值）。
RSI_R01_CAPS = ResourceCaps(
    per_request_input_tokens=S.MAX_INPUT_TOKENS,
    per_request_output_tokens=S.MAX_OUTPUT_TOKENS,
    cumulative_input_tokens=4_560_000,
    cumulative_output_tokens=1_867_776,
    total_usd_equivalent=100.0,
    trial_wall_s=S.TRIAL_WALLCLOCK_MAX_S,
    trial_sim_steps=S.MAX_SIM_STEPS_PER_TRIAL,
    stage_sim_steps=S.MAX_SIM_STEPS_TOTAL,
)

#: 族级帽（plan §6.3：E/D/R 各 72 = 开发 24 + 两个验证 48）。
FAMILY_CAP = S.DEV_POSTS_PER_FAMILY + 2 * S.VAL_POSTS_PER_FAMILY   # 72
TRIAL_CAP = S.DEV_POSTS_PER_FAMILY                                  # 24
PROTOCOL_CAP = S.PROTOCOL_POSTS                                     # 12
GLOBAL_CAP = S.TOTAL_NORMAL_POSTS                                   # 228


class SlotExhausted(RuntimeError):
    """槽位帽先到：拒绝再发（不挪额度、不补考）。"""


class ResourceExceeded(RuntimeError):
    """资源帽先到：结构化拒绝（BLOCKED_RESOURCE）。"""


def quota_paths(campaign_root: Path) -> dict[str, Path]:
    """campaign 的 quota 路径族（全部 0700/0600，由调用方按需建）。"""
    q = Path(campaign_root) / "quota"
    return {"quota": q, "ledger": q / "ledger.jsonl",
            "counters": q / "counters.json",
            "attempts": q / "attempts.jsonl",
            "reservations": q / "reservations",
            "approvals": q / "approvals",
            "pause": q}


# ---------------------------------------------------------------------------
# 槽位账
# ---------------------------------------------------------------------------


@dataclass
class SlotBudget:
    """按 (scope, slot_id) 记账；每次占用必须**先登记**再发网。"""

    path: Path
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        ensure_private_dir(self.path.parent)
        if self.path.is_file():
            self.slots = json.loads(self.path.read_text(encoding="utf-8"))

    # -- 只读视图 ---------------------------------------------------------
    def used_global(self) -> int:
        return len(self.slots)

    def used_trial(self, trial_id: str) -> int:
        return sum(1 for v in self.slots.values() if v["trial_id"] == trial_id)

    def used_family(self, family: str) -> int:
        return sum(1 for v in self.slots.values() if v["family"] == family)

    def used_protocol(self) -> int:
        return sum(1 for v in self.slots.values() if v["scope"] == "protocol")

    def used_scope(self, scope: str) -> int:
        return sum(1 for v in self.slots.values() if v["scope"] == scope)

    # -- 占用 -------------------------------------------------------------
    def claim(self, *, slot_id: str, scope: str, family: str | None,
              trial_id: str | None, post_cap: int | None = None) -> dict[str, Any]:
        """登记一个 slot；任一帽越界即拒（槽仍消耗，不补考）。"""
        if slot_id in self.slots:
            raise SlotExhausted(f"slot {slot_id} 已占用（不重复使用）")
        cap = int(post_cap if post_cap is not None else TRIAL_CAP)
        if scope == "protocol":
            if self.used_protocol() >= PROTOCOL_CAP:
                raise SlotExhausted(f"协议槽已满 {PROTOCOL_CAP}")
        else:
            if family is not None and self.used_family(family) >= FAMILY_CAP:
                raise SlotExhausted(f"{family} 族槽已满 {FAMILY_CAP}（未用额度不转移）")
            if trial_id is not None and self.used_trial(trial_id) >= cap:
                raise SlotExhausted(f"trial {trial_id} 槽已满 {cap}")
        if self.used_global() >= GLOBAL_CAP:
            raise SlotExhausted(f"全局槽已满 {GLOBAL_CAP}")
        rec = {"slot_id": slot_id, "scope": scope, "family": family,
               "trial_id": trial_id, "state": "CLAIMED",
               "claimed_at_utc": _utc(), "posts": 0,
               "post_cap": cap if scope != "protocol" else PROTOCOL_CAP}
        self.slots[slot_id] = rec
        self._flush()
        return rec

    def record_post(self, slot_id: str, *, state: str) -> dict[str, Any]:
        if slot_id not in self.slots:
            raise SlotExhausted(f"未知 slot {slot_id}")
        rec = self.slots[slot_id]
        rec["state"] = state
        rec["posts"] = int(rec.get("posts", 0)) + 1
        if rec["posts"] > int(rec.get("post_cap", TRIAL_CAP)):
            raise SlotExhausted(f"slot {slot_id} 超过 posts 帽 {rec['post_cap']}")
        self._flush()
        return rec

    def mark_preflight_blocked(self, slot_id: str, *, reason: str) -> dict[str, Any]:
        if slot_id not in self.slots:
            raise SlotExhausted(f"未知 slot {slot_id}")
        rec = self.slots[slot_id]
        rec["state"] = "PREFLIGHT_BLOCKED"
        rec["preflight_reason"] = str(reason)[:300]
        # 占槽但零 POST：posts 不增，也不释放（不补考）
        self._flush()
        return rec

    def as_matrix(self) -> dict[str, Any]:
        by_scope: dict[str, int] = {}
        by_family: dict[str, int] = {}
        by_trial: dict[str, int] = {}
        for rec in self.slots.values():
            by_scope[rec["scope"]] = by_scope.get(rec["scope"], 0) + 1
            if rec.get("family"):
                by_family[rec["family"]] = by_family.get(rec["family"], 0) + 1
            if rec.get("trial_id"):
                by_trial[rec["trial_id"]] = by_trial.get(rec["trial_id"], 0) + 1
        return {"schema": S.SCHEMA_SLOT_MATRIX, "global_used": len(self.slots),
                "global_cap": GLOBAL_CAP, "by_scope": by_scope,
                "by_family": by_family, "by_trial": by_trial,
                "family_cap": FAMILY_CAP, "trial_cap": TRIAL_CAP,
                "protocol_cap": PROTOCOL_CAP,
                "slots": {k: dict(v) for k, v in sorted(self.slots.items())}}

    def _flush(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.slots, sort_keys=True, indent=1) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# 每 trial 资源账
# ---------------------------------------------------------------------------


@dataclass
class TrialResources:
    """一个 trial 的时间/实验次数/仿真步数（全部来自可信侧计数）。"""

    trial_id: str
    started_monotonic: float = field(default_factory=time.monotonic)
    experiments: int = 0
    sim_steps: int = 0
    analyses: int = 0

    def charge_experiment(self, steps: int) -> None:
        steps = int(steps)
        if steps <= 0:
            raise ValueError("步骤数必须为正")
        if steps > S.MAX_SIM_STEPS_PER_CALL:
            raise ResourceExceeded(
                f"单次实验 {steps} 步 > {S.MAX_SIM_STEPS_PER_CALL}")
        if self.experiments + 1 > S.CANDIDATE_EXPERIMENTS_PER_TRIAL:
            raise ResourceExceeded(
                f"本 trial 实验次数将达 {self.experiments + 1} > "
                f"{S.CANDIDATE_EXPERIMENTS_PER_TRIAL}")
        if self.sim_steps + steps > S.MAX_SIM_STEPS_PER_TRIAL:
            raise ResourceExceeded(
                f"本 trial 步数将达 {self.sim_steps + steps} > "
                f"{S.MAX_SIM_STEPS_PER_TRIAL}")
        self.experiments += 1
        self.sim_steps += steps

    def charge_analysis(self) -> None:
        if self.analyses + 1 > S.ANALYSIS_MAX_PER_TRIAL:
            raise ResourceExceeded(
                f"本 trial 分析次数将达 {self.analyses + 1} > {S.ANALYSIS_MAX_PER_TRIAL}")
        self.analyses += 1

    def check_wallclock(self) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        if elapsed > S.TRIAL_WALLCLOCK_MAX_S:
            raise ResourceExceeded(
                f"trial 墙钟 {elapsed:.0f}s > {S.TRIAL_WALLCLOCK_MAX_S}s")

    def as_dict(self) -> dict[str, Any]:
        return {"trial_id": self.trial_id, "experiments": self.experiments,
                "sim_steps": self.sim_steps, "analyses": self.analyses,
                "elapsed_s": round(time.monotonic() - self.started_monotonic, 3)}


# ---------------------------------------------------------------------------
# 全局资源（全阶段仿真步）
# ---------------------------------------------------------------------------


class StageResources:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.doc = {"sim_steps": 0, "gpu_seconds": 0.0}
        if self.path.is_file():
            self.doc.update(json.loads(self.path.read_text(encoding="utf-8")))

    def charge_sim(self, steps: int) -> None:
        steps = int(steps)
        if self.doc["sim_steps"] + steps > S.MAX_SIM_STEPS_TOTAL:
            raise ResourceExceeded(
                f"全阶段步数将达 {self.doc['sim_steps'] + steps} > {S.MAX_SIM_STEPS_TOTAL}")
        self.doc["sim_steps"] += steps
        self._flush()

    def charge_gpu(self, seconds: float) -> None:
        if self.doc["gpu_seconds"] + float(seconds) > 2 * 3600:
            raise ResourceExceeded("全阶段 GPU 墙钟超过 2h")
        self.doc["gpu_seconds"] += float(seconds)
        self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.doc, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def _utc() -> str:
    return S.utc_now()
