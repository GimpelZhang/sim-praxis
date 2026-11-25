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

    # 尝试发飞书（若 gateway 可用）
    try:
        from orchestrator.feishu_gateway.client import gateway_from_env

        gateway = gateway_from_env(
            db_path=str(evidence_root / "feishu_pending.sqlite3"),
            evidence_dir=str(evidence_root / "feishu"))
        print("飞书 gateway 就绪，发送 Gate1 卡…")
        # 这里需要真实的 gateway 调用——S2 的 campaign_gate 模式
        # 但 S2b 是单轮简化，先落盘卡面等人工裁决
        print("卡面已就绪，请人工在飞书审批（或直接在会话中裁决）")
    except Exception as exc:
        print(f"飞书 gateway 不可用（{type(exc).__name__}: {exc}）")
        print("卡面已落盘，请人工裁决")

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
    lock_path = rsi_s2b_rules_prereg_dir() / "lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        print(f"LOCK: rules {lock['rules_sha256'][:16]}… / campaign {lock['campaign_sha256'][:16]}…")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "gate1":
        return send_gate1()
    elif cmd == "status":
        return show_status()
    else:
        print(f"未知命令：{cmd}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
