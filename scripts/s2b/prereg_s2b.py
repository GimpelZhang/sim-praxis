#!/usr/bin/env python3
"""S2b 预注册 LOCK 构建器（计划 §3 S2b-0）。

SUT 定义（H_k v2 = S2 v1 候选 + §2 维护侧修复，逐件 sha）+
两个新机制实例 C_N/D_N + 统计参数 + 预算/停止规则 + 一轮为限条款。

用法：/mnt/venvs/simpraxis/bin/python scripts/s2b/prereg_s2b.py [--root DIR]
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2b_rules_prereg_dir
from orchestrator.hk import verdicts as V
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import task_specs as TS

REPO = Path(__file__).resolve().parent.parent.parent

#: SUT v2 = S2 v1 候选 + §2 维护侧修复的文件清单（逐件 sha256）
SUT_FILES = [
    "orchestrator/hk/skills.py",
    "orchestrator/hk/touchpoints.py",
    "orchestrator/hk/guards.py",
    "orchestrator/hk/report_contract.py",
    "orchestrator/hk/nc_registration.py",
    "orchestrator/hk/verdicts.py",
    "orchestrator/hk/driver.py",
    "orchestrator/rsi_bridge/host_recompute.py",
    "orchestrator/rsi_bridge/review_s2.py",
    "orchestrator/rsi_r1/scenes.py",
    "orchestrator/rsi_r1/physics.py",
    "orchestrator/rsi_r1/experiment_service.py",
    "orchestrator/rsi_r1/metrics.py",
    "orchestrator/rsi_r1/rubric.py",
    "orchestrator/rsi_r1/task_specs.py",
    "orchestrator/rsi_r1/experiments.py",
    "orchestrator/rsi_r1/observations.py",
    "orchestrator/rsi_r1/backend.py",
    "orchestrator/rsi_bridge/physics_service.py",
]

#: 触点预算（每研究）
TOUCHPOINT_BUDGET = 3       # K1/K4/K8
REPAIR_BUDGET = 2           # G-REPORT_NC_MISMATCH 修复触点

#: 一轮为限条款
ONE_ROUND_CLAUSE = (
    "S2b 是玩具题校准的最后一轮。双审仍 FAIL ⇒ R1 维持 NOT_ESTABLISHED，"
    "不得再开 S2c/S2d 调参轮；直接转 Track 列表战役。双审 PASS ⇒ R1 立，"
    "同路转 Track 战役。两个出口都去列表。"
)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_rules_document() -> dict:
    """规则面：SUT v2 逐件 sha + 词表 + NC 规则 + 报告合同。"""
    anchors = {p: _sha_file(REPO / p) for p in SUT_FILES}
    return {
        "schema": "simpraxis.s2b.rules.v1",
        "version": 1,
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sut_definition": {
            "description": "H_k v2 = S2 v1 候选（guards/touchpoints/nc_toolkit）+ §2 维护侧修复（F1 信息面/F2 交叉校验/F3 请求面清单）",
            "file_anchors": anchors,
        },
        "verdict_vocabulary": {
            "values": list(V.VERDICTS_V2),
            "module": "orchestrator/hk/verdicts.py",
        },
        "report_contract": {
            "schema": "simpraxis.rsi_s2.report.v2",
            "sections": ["question_hypothesis", "methods", "main_results",
                         "heterogeneity_scope", "limitations_next_steps",
                         "negative_control_handling"],
        },
        "nc_registration": {
            "criteria": ["non_degenerate", "sub_threshold", "sensitivity_calibrated"],
            "module": "orchestrator/hk/nc_registration.py",
        },
        "f1_info_surface": {
            "k7_output": "confirm_result 必须含 negative_control + 全字段 results 行",
            "k8_request": "触点请求必含 nc_evidence + results 别名（缺→SkillError）",
            "touchpoint_v3": "submit_claim 指令明示 NC 证据在场，禁写未注册",
        },
        "f2_crosscheck": {
            "function": "report_contract.crosscheck_nc_section",
            "violation_code": "G-REPORT_NC_MISMATCH",
            "repair_touchpoint": "submit_claim_repair（一次）",
        },
        "f3_request_surface": {
            "REQUIRED_INPUT_KEYS": "touchpoints.REQUIRED_INPUT_KEYS 五触点",
            "k4_slim": "run 摘要代替全量 manifest",
        },
    }


def build_campaign_document(*, rules_sha: str) -> dict:
    """战役面：两实例 + 统计参数 + 预算 + 停止规则。"""
    c = TS.build_s2b_c()
    d = TS.build_s2b_d()
    return {
        "schema": "simpraxis.s2b.campaign.v1",
        "version": 1,
        "rules_bundle_sha256": rules_sha,
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instances": {
            "C_N": {
                "task_id": c.task_id,
                "mechanism_id": c.mechanism_id,
                "study": "C",
                "conditions": [cc.to_dict() for cc in c.conditions],
                "primary": c.primary.to_dict(),
                "estimand": c.estimand,
                "negative_control_spec": c.negative_control_spec,
            },
            "D_N": {
                "task_id": d.task_id,
                "mechanism_id": d.mechanism_id,
                "study": "D",
                "conditions": [cc.to_dict() for cc in d.conditions],
                "primary": d.primary.to_dict(),
                "estimand": d.estimand,
                "negative_control_spec": d.negative_control_spec,
            },
        },
        "statistics": {
            "ci_primary": str(R.CI_PRIMARY),
            "ci_negative": str(R.CI_NEGATIVE),
            "bootstrap_resamples": R.BOOTSTRAP_RESAMPLES,
            "bootstrap_unit": R.BOOTSTRAP_UNIT,
            "propositions": list(R.PROPOSITIONS),
        },
        "budget": {
            "gateway_hard_cap": 40,
            "session_hard_cap": 24,
            "touchpoint_per_study": TOUCHPOINT_BUDGET,
            "repair_touchpoint": REPAIR_BUDGET,
            "dual_review_calls": 4,
            "calibration_calls": 2,
        },
        "gates": {
            "GATE2_MAX_REJECTS_PER_STAGE": 2,
            "gate1": "真人批准前零付费零实跑",
            "gate2": "结果卡交真人审批",
        },
        "calibration": {
            "required": True,
            "criteria": ["sensitivity", "non_degenerate", "sub_threshold",
                         "settling", "shape_192", "determinism"],
            "report": "scripts/s2b/calibrate_s2b.py --report",
        },
        "one_round_clause": ONE_ROUND_CLAUSE,
        "stopping_rules": [
            "校准门失败→改设计不改阈值",
            "修复后双审仍 FAIL→一轮为限生效",
            "审查仪器校准失败→停付修仪器",
            "触点/预算帽到→诚实收口",
            "供应商失败→三分账占分母不补考",
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S2b 预注册 LOCK")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    root = args.root or rsi_s2b_rules_prereg_dir()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    rules = build_rules_document()
    rules_bytes = json.dumps(rules, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
    rules_sha = hashlib.sha256(rules_bytes).hexdigest()

    campaign = build_campaign_document(rules_sha=rules_sha)
    campaign_bytes = json.dumps(campaign, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode()
    campaign_sha = hashlib.sha256(campaign_bytes).hexdigest()

    rules_path = root / "rules_bundle.json"
    campaign_path = root / "campaign_bundle.json"
    rules_path.write_bytes(rules_bytes)
    campaign_path.write_bytes(campaign_bytes)

    lock = {
        "schema": "simpraxis.s2b.lock.v1",
        "rules_sha256": rules_sha,
        "campaign_sha256": campaign_sha,
        "rules_path": str(rules_path),
        "campaign_path": str(campaign_path),
        "frozen_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lock_path = root / "lock.json"
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True))

    print(f"rules_sha256:   {rules_sha}")
    print(f"campaign_sha256: {campaign_sha}")
    print(f"lock: {lock_path}")

    # 两签占位（签名人填）
    for signer in ("executor", "auditor"):
        sig_path = root / f"sign_{signer}.json"
        if not sig_path.exists():
            sig_path.write_text(json.dumps({
                "signer": signer, "rules_sha256": rules_sha,
                "campaign_sha256": campaign_sha,
                "signed_at_utc": None, "note": "待签",
            }, indent=2))
            print(f"签名占位: {sig_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
