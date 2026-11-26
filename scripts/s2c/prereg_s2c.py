#!/usr/bin/env python3
"""S2c 预注册 LOCK（S2c-0）：P 池锁定 + 两判词阈值 + 预算 + 一轮为限。

LOCK 分两 bundle：rules（测量纪律/冻结面/阈值）与 campaign（P 池 6 实例 + 预算）。
Q 池简报首版冻结于 S2c-2（design_space.py 落地后 lock_rev=2 重签，G-LOCK-REV 守）。

用法：python scripts/s2c/prereg_s2c.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2c_rules_prereg_dir

REPO = Path(__file__).resolve().parent.parent.parent

#: 测量纪律冻结面（改动须 lock_rev++ + 重签，G-LOCK-REV 守）
FROZEN_DISCIPLINE = [
    "P 池运行期间不改 v2（调查是测量，不是修复）；改 v2 留到下一轮",
    "不动模型、δ/α、判据算法、physics_service、host_recompute、verdicts、report_contract",
    "Q 池放开实验设计权，但设计经 G-DESIGN 结构门（界内/形状/NC/试点可检出/判别力）",
    "终点码（CLAIM_INVALID/NC_*/G-REPORT_*/DESIGN_NOT_READY）计为结果不重跑；仪器失败留档重跑单列账",
    "判别力与复跑由宿主算，不靠模型自述；世界变体数值只进审查者通道",
]

#: 两个判词阈值（预注册）
VERDICT_THRESHOLDS = {
    "R1_DESIGN": {
        "established_if": "6 开放实例中 ≥4 个完成「设计过门→K0→K8→复跑一致→双审 PASS」且判别力成立",
        "else": "NOT_ESTABLISHED 或 INCONCLUSIVE_COVERAGE（如实）",
    },
    "V_HEADROOM": {
        "locate_by": "全过=天花板；全败=地板；其余=有余量",
        "headroom_metric": "通过率∈(0,1) 或 六维分/成本有分布 → 该维度进 R2 第二轮主指标",
    },
}

#: 余量→R2 决策表（预注册分支）
R2_DECISION_TABLE = [
    {"cond": "P 通过率∈(0,1) 或分数/成本有分布", "then": "R2 第二轮主指标取该维度，用 P 型实例"},
    {"cond": "P 全过（含零效应与陷阱）", "then": "预置题无余量，R2 改用 Q 型实例，主指标取设计一次过率/判别力/成本"},
    {"cond": "P 大面积失败", "then": "R1 成立不稳，如实重判「成立于 2/N 样本」，先修再 R2"},
]

ONE_ROUND = ("S2c 是测量轮且一轮为限：R1-DESIGN 不过则不开调参轮，如实记 NOT_ESTABLISHED；"
             "P 池期间不修 v2。")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(doc) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def build_rules() -> dict:
    modules = ["orchestrator/hk/guards.py", "orchestrator/hk/skills.py",
               "orchestrator/hk/touchpoints.py", "orchestrator/hk/driver.py",
               "orchestrator/rsi_bridge/host_recompute.py",
               "orchestrator/rsi_r1/rubric.py", "orchestrator/rsi_r1/scenes.py",
               "orchestrator/rsi_r1/physics.py",
               "orchestrator/rsi_r1/task_specs.py", "orchestrator/rsi_r1/metrics.py",
               # Q 池仪器面（rev2 纳入冻结：设计三模块 + 简报/可行性脚本）
               "orchestrator/hk/design_space.py", "orchestrator/hk/design_gate.py",
               "orchestrator/hk/design_compiler.py", "scripts/s2c/pool_open.py"]
    return {
        "schema": "simpraxis.s2c.rules.v1", "version": 2, "lock_rev": 2,
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_discipline": FROZEN_DISCIPLINE,
        "frozen_disciple_module_anchors": {m: _sha_file(REPO / m) for m in modules},
        "verdict_thresholds": VERDICT_THRESHOLDS,
        "r2_decision_table": R2_DECISION_TABLE,
        "one_round": ONE_ROUND,
        "budget": {"gateway_hard_cap": 44, "session_hard_cap": 56,
                   "task_caps": {"1": 4, "2": 24, "3": 8, "4": 8}},
        "touchpoint_caps": {"P_per_instance": 3, "Q_per_instance": 5,
                            "Q_design_repair": 2},
    }


def build_campaign(*, rules_sha: str) -> dict:
    # 导入 P 池实例（脚本路径）
    sys.path.insert(0, str(REPO / "scripts" / "s2c"))
    from pool_preset import build_instances  # noqa: E402
    from pool_open import build_briefs, world_variants  # noqa: E402
    insts = build_instances()
    preset = {}
    for name, (label, task) in insts.items():
        preset[name] = {
            "label": label,
            "task_id": task.task_id,
            "mechanism_id": task.mechanism_id,
            "sha256": task.sha256,
            "conditions": [c.to_dict() for c in task.conditions],
            "primary": task.primary.to_dict(),
        }
    briefs = build_briefs()
    q_briefs = {bid: b.public_document() for bid, b in briefs.items()}
    return {
        "schema": "simpraxis.s2c.campaign.v1", "version": 2, "lock_rev": 2,
        "rules_bundle_sha256": rules_sha,
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "p_pool": preset,
        "p_pool_labels": {k: v["label"] for k, v in preset.items()},
        "q_pool": {
            "status": "frozen_rev2_separable_verified（AC-04 实测 6/6 可分且非退化）",
            "domains": ["rebound", "forced_vibration"],
            "n_briefs": len(briefs),
            "briefs": q_briefs,
            # 私有世界变体（只进审查者通道；actor 永远只见 public_document）
            "world_variants": world_variants(),
            "feasibility_probe": "python scripts/s2c/pool_open.py --verify",
            "domain_deviation": ("原计划 contact_sliding/rebound/forced_vibration 3 域×2；"
                                 "物理实测 scene A 的位移/止动时仅 (v0,μ) 纯函数、mass 被消去，"
                                 "无自由旋钮可调制交互（DiD 恒 0）→ 换域为 rebound 4 + forced_vib 2"),
        },
        "one_round": ONE_ROUND,
    }


def main() -> int:
    root = rsi_s2c_rules_prereg_dir()
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    rules = build_rules()
    rb = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    rules_sha = hashlib.sha256(rb).hexdigest()
    campaign = build_campaign(rules_sha=rules_sha)
    cb = json.dumps(campaign, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    campaign_sha = hashlib.sha256(cb).hexdigest()
    (root / "rules_bundle.json").write_bytes(rb)
    (root / "campaign_bundle.json").write_bytes(cb)
    lock = {"schema": "simpraxis.s2c.lock.v1", "lock_rev": 2,
            "rules_sha256": rules_sha, "campaign_sha256": campaign_sha,
            "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lock_rev_note": "rev2：并入 Q 池 6 简报+私有世界变体（AC-04 改域重设计），"
                             "G-LOCK-REV 授权设计变更后升 rev 重签"}
    (root / "lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True))
    print(f"rules_sha256:    {rules_sha}")
    print(f"campaign_sha256: {campaign_sha}")
    print(f"lock: {root / 'lock.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
