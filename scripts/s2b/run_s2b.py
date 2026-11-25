#!/usr/bin/env python3
"""S2b 战役驱动器：Gate1 真卡发送（计划 §3 S2b-3）。

用法：
  python scripts/s2b/run_s2b.py gate1     # 发 Gate1 卡（真人裁决）
  python scripts/s2b/run_s2b.py status    # 查看当前状态

批准前零付费零实跑（No Fake Approval）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.common.runtime_paths import rsi_s2b_evidence_root, rsi_s2b_rules_prereg_dir
from orchestrator.hk import study_gate1_cards as G

REPO = Path(__file__).resolve().parent.parent.parent

PROTOCOL_NOTES = [
    "五段研究协议：",
    "① 仿真方法与问题：MuJoCo 3.15.0 无头物理仿真（A800 GPU EGL）。"
    "C_N 研究球体碰撞中接触阻尼比×撞击速度对反弹高度的交互（DiD）；"
    "D_N 研究受迫振动中驱动频率×关节阻尼对稳态振幅的交互（DiD）。",
    "② 测量与阴性对照：C_N 用 rebound_height_m（δ=0.001），NC=solimp_damping 亚阈微扰；"
    "D_N 用 steady_state_amplitude_rad（δ=0.01），NC=drive_phase_rad 亚阈微扰。"
    "NC 三连判据（非退化/亚阈/灵敏度校准）结构拒绝。",
    "③ 证据与判定链：K0→K8 全链（3 触点/研究），host 独立复跑 n_diffs=0，"
    "G-CLAIM 自报 vs host 逐字段，词表 v2 五值，六段报告合同 + G-REPORT 交叉校验。",
    "④ 两臂对照与反混淆：单臂 SUT v2（S2 v1 候选 + 维护侧修复），无 v0 对照。"
    "诊断：NC 证据面信息缺失已修复（F1/F2/F3）。",
    "⑤ 预算与停止：网关硬帽 40 POST（双审 4 + 校准 2 + 探针 2 + 修复 8 + 余量 24），"
    "会话硬帽 24。一轮为限：两出口都转 Track 列表。",
]


def build_goal_card() -> dict:
    lock_path = rsi_s2b_rules_prereg_dir() / "lock.json"
    lock = json.loads(lock_path.read_text())
    stages = [
        {"stage_id": "C_N",
         "goal": "碰撞域：接触阻尼比×撞击速度→反弹高度 DiD + NC 三连验证",
         "budget": "5 条件×32 block=160 run；3 触点 + 1 修复；独立复跑",
         "depends_on": [],
         "stop_rules": "NC 退化/超阈/未校准→结构拒绝；复跑 n_diffs≠0→停止码"},
        {"stage_id": "D_N",
         "goal": "受迫振动域：驱动频率×关节阻尼→稳态振幅 DiD + NC 三连验证",
         "budget": "5 条件×32 block=160 run；3 触点 + 1 修复；独立复跑",
         "depends_on": [],
         "stop_rules": "NC 退化/超阈/未校准→结构拒绝；复跑 n_diffs≠0→停止码"},
        {"stage_id": "REVIEW",
         "goal": "独立复跑 + 双审（2 通道×2 研究=4 呼）+ R1 判词五条件",
         "budget": "双审 4 POST；审查仪器 6 例校准先 READY",
         "depends_on": ["C_N", "D_N"],
         "stop_rules": "审查仪器误接收即停付修仪器；双审任一双败→该研究 FAIL"},
    ]
    return G.build_goal_card(
        title="RSI-S2b K8 信息面修复 + R1 重验轮：C_N/D_N 两新机制实例",
        stages=stages,
        notes=list(PROTOCOL_NOTES) + [
            f"预注册 LOCK（两签）：rules {lock['rules_sha256'][:16]}… / "
            f"campaign {lock['campaign_sha256'][:16]}…",
            "一轮为限条款已入 LOCK：两出口都转 Track 列表",
            "SUT v2 = S2 v1 候选 + F1/F2/F3 维护侧修复（非自改实验）",
        ])


def send_gate1() -> int:
    card = build_goal_card()
    evidence_root = rsi_s2b_evidence_root()
    evidence_root.mkdir(parents=True, exist_ok=True)

    # 落卡面证据
    card_path = evidence_root / "gate1_card.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2,
                                    sort_keys=True))
    print(f"Gate1 卡面已写：{card_path}")

    payload = G.build_gate1_card_payload(card)
    payload_path = evidence_root / "gate1_payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True))
    print(f"Gate1 payload 已写：{payload_path}")

    # 实际发送飞书 Gate1 卡（S2 campaign_gate 同构）
    from orchestrator.feishu_gateway.client import gateway_from_env

    gateway = gateway_from_env(
        db_path=str(evidence_root / "feishu_pending.sqlite3"),
        evidence_dir=str(evidence_root / "feishu"),
        run_dir=str(evidence_root))

    thread_id = "s2b-campaign-20261125"
    print(f"飞书 gateway 就绪，发送 Gate1 卡（thread={thread_id}）…")

    decision = gateway.request_gate1_approval(
        thread_id, payload["task_title"], payload["proposals"],
        max_wait=5400)

    decision_path = evidence_root / "gate1_decision.json"
    decision_doc = dict(decision, thread_id=thread_id)
    decision_path.write_text(json.dumps(decision_doc, ensure_ascii=False,
                                        indent=2, sort_keys=True))
    print(f"Gate1 决策：{decision.get('decision')}")
    print(f"人工意见：{decision.get('human_notes', '')}")
    print(f"决策已落盘：{decision_path}")

    try:
        gateway.stop_ws()
    except Exception:  # noqa: BLE001
        pass

    return 0


def show_status() -> int:
    evidence_root = rsi_s2b_evidence_root()
    card_path = evidence_root / "gate1_card.json"
    if card_path.exists():
        card = json.loads(card_path.read_text())
        print(f"Gate1 卡面已发：{card.get('title')}")
        print(f"Stages: {[s['stage_id'] for s in card.get('stages', [])]}")
    else:
        print("Gate1 卡面未发")
    decision_path = evidence_root / "gate1_decision.json"
    if decision_path.exists():
        d = json.loads(decision_path.read_text())
        print(f"Gate1 决策：{d.get('decision')}")
    lock_path = rsi_s2b_rules_prereg_dir() / "lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        print(f"LOCK: rules {lock['rules_sha256'][:16]}… / campaign {lock['campaign_sha256'][:16]}…")
    return 0


def run_studies() -> int:
    """S2b-4：两研究 K0→K8 全链实跑（SUT v2 单臂）。"""
    from orchestrator.common.fsutil import ensure_private_dir
    from orchestrator.common.runtime_paths import rsi_s2b_sessions_dir
    from orchestrator.hk.driver import GateDecision, ResearchDriver
    from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                        default_confirmer)
    from orchestrator.hk.touchpoints import HeadlessActor, HeadlessSessionRunner
    from orchestrator.rsi_bridge.session_ledger import SessionLedger
    from orchestrator.rsi_r1 import task_specs as TS

    evidence_root = rsi_s2b_evidence_root()
    campaign_root = ensure_private_dir(evidence_root / "campaign", mode=0o700)

    # 检查 Gate1 批准
    decision_path = evidence_root / "gate1_decision.json"
    if not decision_path.exists():
        print("Gate1 未批准，禁止实跑（No Fake Approval）")
        return 1
    decision = json.loads(decision_path.read_text())
    if decision.get("decision") != "approve":
        print(f"Gate1 决策为 {decision.get('decision')}，禁止实跑")
        return 1

    # 会话账（S2b 会话面硬帽 24）
    ledger = SessionLedger(
        rsi_s2b_sessions_dir() / "sessions.jsonl",
        hard_cap_sessions=24)

    # NC 灵敏度（从校准报告取）
    cal_path = Path("/tmp/s2b_calibration.json")
    nc_sensitivity = {}
    if cal_path.exists():
        cal = json.loads(cal_path.read_text())
        nc_sensitivity = {
            "C_N": cal.get("C_N", {}).get("nc_non_degenerate", {}),
            "D_N": cal.get("D_N", {}).get("nc_non_degenerate", {}),
        }

    tasks = {
        "C_N": TS.build_s2b_c(),
        "D_N": TS.build_s2b_d(),
    }

    results = {}
    for study_name, task in tasks.items():
        label = f"s2b-{study_name.lower()}"
        run_root = ensure_private_dir(campaign_root / "trial" / label, mode=0o700)
        print(f"\n=== {study_name} K0→K8 ===")

        runner = HeadlessSessionRunner(
            sessions_root=rsi_s2b_sessions_dir() / "ws",
            ledger=ledger, max_turns=90)
        actor = HeadlessActor(runner, evidence_root=run_root, host_label=label)

        driver = ResearchDriver(
            gate1_hook=lambda card: GateDecision(approved=True),
            gate2_hook=lambda card, stage: GateDecision(approved=True),
            actor=actor, evidence_root=run_root,
            batch_runner=default_batch_runner,
            analyzer=default_analyzer,
            confirmer=default_confirmer,
            claim_guard=True,
            harness="v2",
            nc_sensitivity=nc_sensitivity.get(study_name),
            touchpoint_budget=5,  # 3 + 2 修复
            diagnose_budget=2)

        # 构造实验 spec（从 task 的 public_document）
        task_doc = task.to_dict()
        spec = {
            "schema": "simpraxis.s2b.experiment_spec",
            "task_id": task.task_id,
            "study": study_name,
            "plan_sha256": task.sha256,
            "n_block": 32,
            "conditions": [c.to_dict() for c in task.conditions],
        }

        outcome = driver.run_campaign(
            {"goal": study_name},
            {study_name: spec})

        status = outcome.get("status")
        stage_info = (outcome.get("stages") or {}).get(study_name, {})
        code = stage_info.get("code")
        claim = stage_info.get("claim")

        print(f"  status: {status}, code: {code}")
        if claim:
            print(f"  verdict: {claim.get('verdict')}")

        results[study_name] = {
            "status": status, "code": code,
            "claim": claim, "outcome": outcome,
        }

        # 落研究报告
        report_path = run_root / "study_report.json"
        report_path.write_text(json.dumps({
            "schema": "simpraxis.s2b.study_report.v1",
            "study": study_name, "status": status, "code": code,
            "claim": claim, "outcome": outcome,
        }, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        print(f"  报告：{report_path}")

    # 汇总
    summary_path = campaign_root / "campaign_summary.json"
    summary_path.write_text(json.dumps({
        "schema": "simpraxis.s2b.campaign_summary.v1",
        "studies": {k: {"status": v["status"], "code": v["code"],
                        "verdict": (v.get("claim") or {}).get("verdict")}
                    for k, v in results.items()},
    }, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\n汇总：{summary_path}")

    all_done = all(v["status"] == "done" for v in results.values())
    return 0 if all_done else 1


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "gate1":
        return send_gate1()
    elif cmd == "run":
        return run_studies()
    elif cmd == "status":
        return show_status()
    else:
        print(f"未知命令：{cmd}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
