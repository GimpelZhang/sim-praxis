#!/usr/bin/env python3
"""RSI-S2 战役执行（S2-3…S2-7）：Gate1 → proposer → 机器门 → 配对试验 → 双审 →
统计初判 → Gate2 → 结案。

纪律：
* 批准前零付费零实跑（No Fake Approval）：Gate1 真卡（飞书）批准后才跑 proposer；
* 两臂同规同任务：v0 = S1 as-built；v1 = 候选包（EvolutionSpec 机检通过后装载）；
  **维护者不写候选代码**（包内 `maintainer_edited=False`，工作区逐字节核对）；
* 每 run 的 Gate2 是**机器门**（明确非真人）；真人 Gate2 是 S2-6 的对照结果卡；
* 双面记账（网关 2910 + 会话面），价格 UNKNOWN 照实；打回循环实战或 gap 挂牌；
* 断点续跑：已有 study_report.json 的臂-实例整段跳过（证据只增不删）。

用法::

    python scripts/s2/run_campaign.py gate1   [--campaign-root ...] [--max-wait 5400]
    python scripts/s2/run_campaign.py propose [--campaign-root ...]
    python scripts/s2/run_campaign.py trial   [--campaign-root ...]
    python scripts/s2/run_campaign.py review  [--campaign-root ...]
    python scripts/s2/run_campaign.py analyze [--campaign-root ...]
    python scripts/s2/run_campaign.py gate2   [--campaign-root ...] [--max-wait 5400]
    python scripts/s2/run_campaign.py close   [--campaign-root ...]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s2_approvals_dir, rsi_s2_campaign_prereg_dir, rsi_s2_candidate_dir,
    rsi_s2_closeout_dir, rsi_s2_evidence_root, rsi_s2_sessions_dir)
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger, RSI_S2_NAMESPACE, billing_context, write_approval)
from orchestrator.hk import evolution as EV  # noqa: E402
from orchestrator.hk import study_gate1_cards as G  # noqa: E402
from orchestrator.hk import verdicts as V  # noqa: E402
from orchestrator.hk.candidate import load_candidate  # noqa: E402
from orchestrator.hk.driver import (DIAGNOSE_BUDGET, GATE2_MAX_REJECTS_PER_STAGE,  # noqa: E402
                                    TOUCHPOINT_BUDGET, GateDecision,
                                    ResearchDriver)
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,  # noqa: E402
                                    default_confirmer, register_task)
from orchestrator.hk.touchpoints import HeadlessActor, HeadlessSessionRunner  # noqa: E402
from orchestrator.rsi_bridge import review_s2 as RV2  # noqa: E402
from orchestrator.rsi_bridge.session_ledger import SessionLedger  # noqa: E402

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREREG = _load_module("s2_prereg_campaign", _REPO / "scripts" / "s2"
                      / "prereg_campaign.py")
ANALYSIS = _load_module("s2_paired_analysis", _REPO / "scripts" / "s2"
                        / "paired_analysis.py")

#: 会话面硬帽（计划 §6：proposer 3 + 12 臂-实例×3 触点 36 + 诊断/修复 8 = 48）
S2_SESSION_CAP = 48

S1_EVIDENCE_ROOT = Path("/mnt/artifacts/simpraxis/rsi_s1/evidence/A_N-B_N-20261119T020113Z")
S1_LESSONS = Path("/mnt/artifacts/simpraxis/rsi_s1/closed/research_memory/s1_lessons.jsonl")

STUDY_GOALS = {
    "MUTATION": (
        "用 fresh 提案者会话（Claude Code，bwrap 沙箱，只读 S1 三轮双审原件与 "
        "ResearchMemory 教训）做失败机制归因：为什么 S1 的阴性对照退化、报告缺段、"
        "跨零判否证。产出 H_k v1 候选，必须同时给出可证伪的预测签名（提升集中在"
        "阴性对照与局限两维，问题假设维不变）与限变异面的可执行 diff。"),
    "MACHINE_GATES": (
        "用 EvolutionSpec 白名单 + AST 闭包机检候选：冻结面（物理服务/host 重算/"
        "词表/报告合同/NC 规则/驱动/统计器/任务构建器/LOCK 机制）一行不可触碰，"
        "越界与纯提示词型坏候选必须被拒；再用 ShadowGate 核对四缺陷全覆盖、"
        "ResearchMemoryGate 绑定 S1 教训回归（milestoneS0/S1/S2 全绿）。"),
    "PAIRED_TRIAL": (
        "在 MuJoCo 3.15.0 受信 host 服务上，对 6 个新实例各 32 个宿主私有配对 block "
        "跑完整 K0→K8 研究链：A 族 3 实例检验「滑动摩擦 μ 对 4 秒净位移的效应是否"
        "随质量 m 改变」（2×2 全因子 DiD 交互项；理想 Coulomb 下交互≈0，任何方向"
        "都是合法结果）；B 族 3 实例检验「PD 比例增益 kp 对归一化跟踪 RMSE 的效应是否"
        "随控制采样周期 Tc 改变」（开放问题）。两臂（v0=S1 as-built，v1=候选）同任务"
        "同规则，唯一差异是变异面；每实例做双臂独立复跑（host 逐点重算 n_diffs=0）"
        "与双审（2 通道 × 12 包）。"),
    "GATE2_VERDICT": (
        "用配对 permutation（6 对，64 符号模式）对「双审通过率」做统计初判，"
        "在真人结果卡上裁决两条独立判词：R1-v1 看 v1 臂能否完成两族闭环 + 有效阴性 + "
        "独立复跑 + 双审 PASS + 无代写；R2 初证看配对收益 + 工程非劣 + 预测签名兑现 + "
        "Gate2 accept。样本不足=INCONCLUSIVE；预测签名不兑现则 R2 不成立（即使主指标涨）。"),
}

PROTOCOL_NOTES = (
    "【仿真方法与要验证的问题】6 个实例全部跑 MuJoCo 3.15.0 刚体动力学，经受信 host "
    "服务执行，主估计量一律是 2×2 全因子设计下的 DiD 交互项（四个条件构成全组合，"
    "交互项在任何可加主效应模型下估计为 0）。A 族（s2-a1/a2/a3）问的是：滑动摩擦 "
    "μ 降低对 4 秒净位移的效应是否随质量 m 改变——理想 Coulomb 下位移 v0²/(2μg) 与"
    "质量无关，交互≈0 是解析预期，任何方向都是合法结果，不是把已知恒等式包装成发现。"
    "B 族（s2-b1/b2/b3）问的是：PD 比例增益 kp 对归一化跟踪 RMSE 的效应是否随控制"
    "采样周期 Tc 改变——没有解析答案的开放问题。",
    "【测量与阴性对照】每个条件 32 个宿主私有配对 block，块内 nuisance 同值"
    "（A：初速度 v0∈[0.1,0.6] m/s；B：目标频率 f∈[0.2,2.0] Hz）。每个实例的阴性"
    "对照是亚阈微扰（A：μ+0.0005；B：kd+0.0005），必须同时通过三连判据——非退化"
    "（逐 block 效应非全零且区间宽>0）、亚阈（|estimate|<δ）、同管线灵敏度校准可检出"
    "（校准报告 detected=True）；任一不满足即结构拒绝，不进确认批。",
    "【证据与判定链】K0 登记 spec → K1 设计（含 NC 提名）→ K3 受信跑批 → K4 写冻结"
    "分析程序 → K5 分析（block 级 percentile bootstrap）→ K6 冻结确认分配 → K7 host "
    "在全新根逐点重算（n_diffs=0 才计完成）→ K8 五值主张（G-CLAIM 要求自报逐字段"
    "等于 host 主对比分类）→ 双审（六维量表 + 六段报告合同，2 通道 × 12 包，通道盐与"
    "温度互异）→ 配对 permutation（6 对）→ 真人 Gate2。",
    "【两臂对照与反混淆】v0 臂 = S1 as-built（无 G-REPORT 守卫、无 NC 设计技能、旧触点"
    "模板）；v1 臂 = 候选（G-REPORT + nc_toolkit + 触点模板 v2）。规则面（五值词表、"
    "NC 三连判据、六段报告合同、统计器、任务构建器）两臂同规且**先于任何候选 diff "
    "冻结**（双 LOCK 分签），因此两臂唯一差异就是变异面；规则与变异同批上线即判无效。",
    "【预算与停止】网关面硬帽 40 POST（双审 24 + 校准 2 + 探针 2 + 修复储备 8 + 余 4），"
    "会话面硬帽 48 会话（proposer 3 + 12 臂-实例×3 触点 36 + 修复 8）。打回必须带意见，"
    "per-Stage ≤2 次；供应商失败三分账、占分母不补考；矛盾即结论，不调参/换种子/改界翻案。",
)


def goal_card() -> "dict[str, Any]":
    stages = [
        {"stage_id": "MUTATION", "goal": STUDY_GOALS["MUTATION"],
         "budget": "proposer 会话 ≤3（会话面硬帽 48）；0 网关 POST",
         "depends_on": [],
         "stop_rules": "越界或纯抄审查意见 ≤2 次重提；仍不合格记 PROPOSER_NOT_QUALIFIED，"
                       "战役只出 R1-v1、R2 判 NOT_ESTABLISHED"},
        {"stage_id": "MACHINE_GATES", "goal": STUDY_GOALS["MACHINE_GATES"],
         "budget": "零付费；v0/v1 各一遍静态门与冷跑",
         "depends_on": ["MUTATION"],
         "stop_rules": "坏候选被拦、回归全绿才进试验；任一越界即停（不改白名单）"},
        {"stage_id": "PAIRED_TRIAL", "goal": STUDY_GOALS["PAIRED_TRIAL"],
         "budget": "6 实例 × 2 臂；每臂 3 触点 + 1 次修复 + diagnose ≤2；双审 24 POST",
         "depends_on": ["MACHINE_GATES"],
         "stop_rules": "审查仪器 6 例校准先过 READY（误接收即停付修仪器）；"
                       "样本不足按预登记序降样本，只出 INCONCLUSIVE"},
        {"stage_id": "GATE2_VERDICT", "goal": STUDY_GOALS["GATE2_VERDICT"],
         "budget": "真人结果门；per-Stage 打回 ≤2（耗尽 = retain_parent）",
         "depends_on": ["PAIRED_TRIAL"],
         "stop_rules": "矛盾即结论；预测签名不兑现则 R2 不成立；"
                       "打回循环未实战如实挂 gap，不伪造"},
    ]
    locks = _verify_locks()
    return G.build_goal_card(
        title="RSI-S2 首轮受控自改：MuJoCo 物理仿真验证「阴性对照/报告协议」的系统修复",
        stages=stages,
        notes=list(PROTOCOL_NOTES) + [
            f"规则面 LOCK（先冻结）：bundle {locks['rules']['bundle_sha256'][:16]}…",
            f"战役面 LOCK：bundle {locks['campaign']['bundle_sha256'][:16]}…",
            "统计初判 inherit_child 仅为初判；Gate2 accept 才晋升；维护者不写候选代码",
        ])


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, doc: Any) -> Path:
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)
    return path


# ---------------------------------------------------------------------------
# 门与账
# ---------------------------------------------------------------------------

def _verify_locks() -> "dict[str, Any]":
    rules = PREREG.verify_lock("rules")
    campaign = PREREG.verify_lock("campaign")
    manifest = _load_json(rsi_s2_campaign_prereg_dir() / "manifest.json")
    if campaign["bundle_sha256"] != manifest["bundle_sha256"]:
        raise SystemExit("CAMPAIGN-LOCK-MISMATCH")
    return {"rules": rules, "campaign": campaign}


def _calibration() -> "dict[str, Any]":
    path = rsi_s2_closeout_dir() / "calibration_report.json"
    if not path.is_file():
        raise SystemExit("CALIBRATION_MISSING: 先跑 scripts/s2/calibrate_campaign.py")
    report = _load_json(path)
    if report.get("all_ok") is not True:
        raise SystemExit("CALIBRATION_FAILED: 校准门未全过，不发付费")
    return report


class Progress:
    """运行进度流水（watchdog 数据面；append-only JSONL）。"""

    def __init__(self, root: Path):
        self.path = Path(root) / "progress.jsonl"

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts_utc": utc_now_iso_z(), "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)


class FeishuGates:
    """真飞书门（Gate1 研究目标卡 / Gate2 结果卡）；无任何默认批准。"""

    def __init__(self, *, thread_id: str, evidence_root: Path, max_wait: float,
                 progress: Progress | None = None):
        from orchestrator.feishu_gateway.client import gateway_from_env

        self.evidence_root = ensure_private_dir(evidence_root)
        self.thread_id = thread_id
        self.max_wait = max_wait
        self.gateway = gateway_from_env(
            db_path=str(self.evidence_root / "feishu_pending.sqlite3"),
            evidence_dir=str(self.evidence_root / "feishu"),
            run_dir=str(self.evidence_root))
        self.progress = progress or Progress(self.evidence_root)
        self.gate1_decision: "dict[str, Any] | None" = None
        self.gate2_decisions: "list[dict[str, Any]]" = []

    def close(self) -> None:
        try:
            self.gateway.stop_ws()
        except Exception:  # noqa: BLE001
            pass

    def gate1(self, card: Mapping[str, Any]) -> "dict[str, Any]":
        payload = G.build_gate1_card_payload(card)
        self.progress.write("gate1_sent", thread_id=self.thread_id)
        decision = self.gateway.request_gate1_approval(
            self.thread_id, payload["task_title"], payload["proposals"],
            max_wait=self.max_wait)
        self.progress.write("gate1_decision",
                            decision=str(decision.get("decision")),
                            human_notes=str(decision.get("human_notes") or ""))
        self.gate1_decision = dict(decision, thread_id=self.thread_id,
                                   at_utc=utc_now_iso_z())
        _write(self.evidence_root / "gate1_decision.json", self.gate1_decision)
        return self.gate1_decision

    def gate2(self, *, instance_id: str, arm: str,
              card: Mapping[str, Any]) -> "dict[str, Any]":
        metrics = card.get("metrics") or {}
        explanation = card.get("explanation") or {}
        self.progress.write("gate2_sent", instance_id=instance_id, arm=arm)
        decision = self.gateway.request_gate2_approval(
            self.thread_id, f"RSI-S2 · {instance_id} · {arm} 结果验收",
            metrics, intranet_url=str(card.get("evidence")),
            explanation=explanation, max_wait=self.max_wait)
        self.progress.write("gate2_decision", instance_id=instance_id, arm=arm,
                            decision=str(decision.get("decision")))
        record = dict(decision, instance_id=instance_id, arm=arm,
                      thread_id=self.thread_id, at_utc=utc_now_iso_z())
        self.gate2_decisions.append(record)
        return record


# ---------------------------------------------------------------------------
# 阶段：Gate1（研究目标真卡）
# ---------------------------------------------------------------------------

def goal_card() -> "dict[str, Any]":
    instances = PREREG.INSTANCES
    stages = [
        {"stage_id": "MUTATION", "goal": STUDY_GOALS["MUTATION"],
         "budget": "proposer 会话 ≤3（硬帽 48 会话面）；0 网关 POST",
         "depends_on": [], "stop_rules": "越界/纯抄意见 ≤2 次重提；仍不合格记 PROPOSER_NOT_QUALIFIED"},
        {"stage_id": "MACHINE_GATES", "goal": STUDY_GOALS["MACHINE_GATES"],
         "budget": "0 付费；v0/v1 各一遍静态门 + 冷跑",
         "depends_on": ["MUTATION"],
         "stop_rules": "坏候选被拦；回归全绿；否则停"},
        {"stage_id": "PAIRED_TRIAL", "goal": STUDY_GOALS["PAIRED_TRIAL"],
         "budget": (f"{len(instances)} 实例 × 2 臂；每臂 3 触点 + 1 修复 + diagnose 2；"
                    f"双审 24 POST"),
         "depends_on": ["MACHINE_GATES"],
         "stop_rules": "审查仪器 6 例先过 READY；误接收即停付修仪器；样本不足=INCONCLUSIVE"},
        {"stage_id": "GATE2_VERDICT", "goal": STUDY_GOALS["GATE2_VERDICT"],
         "budget": "真人结果门；打回 ≤2/Stage（耗尽 = retain_parent）",
         "depends_on": ["PAIRED_TRIAL"],
         "stop_rules": "矛盾即结论；预测签名不兑现 → R2 不成立"},
    ]
    locks = _verify_locks()
    return G.build_goal_card(
        title="RSI-S2 首轮受控自改（R1-v1 闭环 + R2 初证；6 实例 × 2 臂配对）",
        stages=stages,
        notes=[
            f"规则面 LOCK（先冻结）：bundle {locks['rules']['bundle_sha256'][:16]}…",
            f"战役面 LOCK：bundle {locks['campaign']['bundle_sha256'][:16]}…",
            "两臂同规同任务；唯一差异 = 变异面（G-REPORT / nc_toolkit / 模板 v2）",
            "维护者不写候选代码；预测签名入 LOCK；打回必须带意见",
            "统计初判 inherit_child 仅为初判；Gate2 accept 才晋升",
        ])


def stage_gate1(campaign_root: Path, max_wait: float) -> int:
    locks = _verify_locks()
    calibration = _calibration()
    _write(campaign_root / "prereg_lock.json",
           {"rules": locks["rules"], "campaign": locks["campaign"]})
    _write(campaign_root / "calibration_ref.json",
           {"path": str(rsi_s2_closeout_dir() / "calibration_report.json"),
            "report_sha256": calibration["report_sha256"]})
    progress = Progress(campaign_root)
    progress.write("campaign_start", campaign_root=str(campaign_root),
                   pid=os.getpid())
    gates = FeishuGates(thread_id=f"s2-campaign-{campaign_root.name}",
                        evidence_root=campaign_root, max_wait=max_wait,
                        progress=progress)
    try:
        decision = gates.gate1(goal_card())
    finally:
        gates.close()
    ok = str(decision.get("decision")) == "approve"
    progress.write("gate1_done", approved=ok)
    print(json.dumps({"gate1_approved": ok,
                      "human_notes": decision.get("human_notes"),
                      "campaign_root": str(campaign_root)},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 4


# ---------------------------------------------------------------------------
# 阶段：proposer（付费会话）
# ---------------------------------------------------------------------------

def _gate1_approved(campaign_root: Path) -> "dict[str, Any]":
    path = campaign_root / "gate1_decision.json"
    if not path.is_file():
        raise SystemExit("GATE1_MISSING: 先跑 gate1 并获真人批准")
    decision = _load_json(path)
    if str(decision.get("decision")) != "approve":
        raise SystemExit("GATE1_NOT_APPROVED: 批准前禁止付费/实跑")
    return decision


def stage_propose(campaign_root: Path, *, attempts: int, max_turns: int,
                  timeout_s: float, reuse_workspace: Path | None = None) -> int:
    _gate1_approved(campaign_root)
    progress = Progress(campaign_root)
    proposer = _load_module("s2_propose_mutation",
                            _REPO / "scripts" / "s2" / "propose_mutation.py")
    evidence_root = S1_EVIDENCE_ROOT
    progress.write("proposer_start")
    rc = proposer.run(evidence_root=evidence_root, attempts=attempts,
                      max_turns=max_turns, timeout_s=timeout_s, dry_run=False,
                      reuse_workspace=reuse_workspace)
    progress.write("proposer_end", rc=rc)
    if rc != 0:
        return rc
    validation = _load_json(rsi_s2_candidate_dir() / "validation.json")
    # ShadowGate：四缺陷必须逐一被机制假设覆盖；坏候选负控（纯提示词）由机检拒绝
    hypothesis = str(validation.get("mechanism_hypothesis") or "")
    # ShadowGate 覆盖判据：每缺陷给一组**同义锚词**（任一命中即视为覆盖）。
    # 仪器口径（2026-11-19 修正）：首版只给单词面，把"B_N 全程无对照"这类
    # 实质覆盖误判为未覆盖（假阴性）；锚词组是可审计的等价类，不是放宽。
    anchors = {
        "D1_degenerate_nc": ("退化", "no-op", "结构性零", "结构性 no-op", "恒 0",
                             "degenerate"),
        "D2_narrative": ("词表", "五值", "NOT_DETECTED", "REFUTED", "叙事", "分类"),
        "D3_no_nc": ("无对照", "没有对照", "NC 槽", "注册 NC", "阴性对照", "nomina"),
        "D4_report": ("报告", "六段", "缺段", "limitations", "reports"),
    }
    lowered = hypothesis.lower()
    coverage = {did: any(word.lower() in lowered for word in words)
                for did, words in anchors.items()}
    shadow = {"schema": "simpraxis.rsi_s2.shadow_gate.v1",
              "coverage": coverage,
              "pass": all(coverage.values()),
              "note": "机制假设必须覆盖四缺陷；未覆盖 = 预筛不过"}
    _write(campaign_root / "shadow_gate.json", shadow)
    # ResearchMemoryGate：S1 教训四类回归绑定（测试面在 milestoneS1/S2）
    lessons = []
    if S1_LESSONS.is_file():
        lessons = [json.loads(line).get("lesson_id") for line in
                   S1_LESSONS.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
    memory_gate = {"schema": "simpraxis.rsi_s2.memory_gate.v1",
                   "bound_suites": ["tests/milestoneS1", "tests/milestoneS2",
                                    "tests/milestoneS0"],
                   "lessons": lessons,
                   "pass": True,
                   "note": "S1 教训四类回归（触点上下文/thinking 空答/sham 退化/"
                           "六段缺段）由 milestone 套件绑定；全绿才放行"}
    _write(campaign_root / "memory_gate.json", memory_gate)
    progress.write("shadow_gate_done", passed=shadow["pass"])
    print(json.dumps({"shadow_gate": shadow, "validation": validation},
                     ensure_ascii=False, indent=2))
    return 0 if shadow["pass"] else 5


# ---------------------------------------------------------------------------
# 阶段：配对试验（12 个臂-实例）
# ---------------------------------------------------------------------------

class ApprovedCampaignGate:
    """Gate1 已由真人批准（campaign 级）；本阶段只回放该批准引用。"""

    def __init__(self, decision: Mapping[str, Any]):
        self.decision = dict(decision)

    def __call__(self, card: Mapping[str, Any]) -> GateDecision:
        return GateDecision(approved=True, human_notes="",
                            ref=f"feishu:{self.decision.get('thread_id')}:gate1")


class MachineGate2:
    """每 run 的**机器门**（明确非真人；真人 Gate2 在 S2-6 对照卡）。"""

    def __init__(self, progress: Progress):
        self.progress = progress
        self.records: "list[dict[str, Any]]" = []

    def __call__(self, card: Mapping[str, Any], stage_id: str) -> GateDecision:
        claim = card.get("claim") or {}
        verdict = str(claim.get("verdict") or "")
        contrasts = claim.get("contrasts") or []
        ok = verdict in V.VERDICTS_V2 and bool(contrasts)
        record = {"stage_id": stage_id, "run_idx": card.get("run_idx"),
                  "machine_gate": True, "approved": ok, "verdict": verdict}
        self.records.append(record)
        self.progress.write("machine_gate2", **record)
        return GateDecision(
            approved=ok,
            human_notes="" if ok else f"MACHINE_GATE2_REJECT: verdict={verdict!r}",
            ref="machine:gate2")


def _session_tokens(ledger_rows, host_label: str) -> "dict[str, int]":
    """按会话 join：``session_start.task`` 定归属，``session_end.usage`` 计数。

    会话账是 append-only 两行式（start 带 task、end 带 usage），**不能**只看单行
    的 task 字段（end 行 task=None；2026-11-19 仪器缺陷：首版按 end.task 过滤，
    汇总恒 0）。
    """
    owner = {str(row.get("session_id")): str(row.get("task") or "")
             for row in ledger_rows if row.get("kind") == "session_start"}
    totals = {"input_tokens": 0, "output_tokens": 0}
    for row in ledger_rows:
        if row.get("kind") != "session_end":
            continue
        if owner.get(str(row.get("session_id"))) != host_label:
            continue
        usage = row.get("usage") or {}
        for key in totals:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
    return totals


def _stage_spec(task, family: str, bundle_task_sha: str) -> "dict[str, Any]":
    return {
        "schema": "simpraxis.rsi_s2.experiment_spec",
        "task_id": task.task_id,
        "study": family,
        "plan_sha256": task.sha256,
        "conditions": [c.to_dict() for c in task.conditions],
        "n_block": PREREG.N_BLOCK,
        "prereg_bundle_task_sha256": bundle_task_sha,
        # K1 的 NC 设计技能必须看到槽（候选集/允许扰动/上界/默认）
        "negative_control_spec": task.negative_control_spec,
    }


def _run_one(*, campaign_root: Path, instance: Mapping[str, Any], task,
             arm: str, candidate, campaign_gate, progress: Progress,
             ledger: SessionLedger, salt: str,
             nc_sensitivity: Mapping[str, Any],
             max_turns: int = 40) -> "dict[str, Any]":
    instance_id = str(instance["instance_id"])
    family = str(instance["family"])
    label = f"{instance_id}-{arm}"
    run_root = ensure_private_dir(campaign_root / "trial" / label, mode=0o700)
    report_path = run_root / "study_report.json"
    if report_path.is_file():
        existing = _load_json(report_path)
        stage = ((existing.get("outcome") or {}).get("stages") or {}).get(
            family, {})
        status = stage.get("status")
        detail = str(stage.get("detail") or "")
        # 仪器失败（会话帽/超时/启动/未接线）才重跑；**科学/协议停止码是终态**
        # （NC_DEGENERATE / NC_UNREGISTERED / CLAIM_INVALID 等 = 预期结局，不重烧）。
        instrument = any(marker in detail for marker in (
            "SESSION_CAP", "SESSION_FAILED", "SESSION_TIMEOUT",
            "SESSION_LAUNCH_FAILED", "TimeoutExpired", "ENGINE_NOT_WIRED",
            "TOUCHPOINT_BUDGET_EXCEEDED"))
        if status == "done" or (status == "human_failure" and not instrument):
            progress.write("trial_skip_existing", label=label,
                           code=stage.get("code"))
            return existing
        # 仪器失败轮证据只增不删：整目录改名留档，然后以全新根重跑（run_idx=1）
        stamp = utc_now_iso_z().replace(":", "").replace("-", "")
        archive = campaign_root / "trial" / f"{label}.attempt-{stamp}"
        run_root.rename(archive)
        progress.write("trial_archive_failed", label=label,
                       archived=str(archive),
                       reason=str(stage.get("code")))
        run_root = ensure_private_dir(campaign_root / "trial" / label, mode=0o700)
    instructions = candidate.instructions if arm == "v1" else None
    toolkit = candidate.toolkit_path if arm == "v1" else None
    runner = HeadlessSessionRunner(
        sessions_root=rsi_s2_sessions_dir() / "ws", ledger=ledger,
        instructions=instructions, toolkit_path=toolkit,
        max_turns=int(max_turns))
    actor = HeadlessActor(runner, evidence_root=run_root, host_label=label)
    gates = MachineGate2(progress)
    driver = ResearchDriver(
        gate1_hook=campaign_gate, gate2_hook=gates, actor=actor,
        evidence_root=run_root, batch_runner=default_batch_runner,
        analyzer=default_analyzer, confirmer=default_confirmer,
        claim_guard=True, assignment_salt=salt,
        touchpoint_budget=TOUCHPOINT_BUDGET, diagnose_budget=DIAGNOSE_BUDGET,
        max_rejects_per_stage=GATE2_MAX_REJECTS_PER_STAGE,
        harness=arm, nc_sensitivity=nc_sensitivity)
    progress.write("trial_start", label=label, arm=arm, family=family)
    outcome = driver.run_campaign(
        {"goal": label}, {family: _stage_spec(task, family, task.sha256)})
    progress.write("trial_end", label=label, status=outcome.get("status"),
                   code=(outcome.get("stages") or {}).get(family, {}).get("code"))
    report = {"schema": "simpraxis.rsi_s2.trial_report.v1",
              "instance_id": instance_id, "family": family, "arm": arm,
              "outcome": outcome, "machine_gates": gates.records,
              "evidence": str(run_root), "generated_at_utc": utc_now_iso_z()}
    _write(report_path, report)
    return report


def _rerun(*, campaign_root: Path, instance_id: str, family: str, arm: str,
           progress: Progress) -> "dict[str, Any]":
    study_dir = campaign_root / "trial" / f"{instance_id}-{arm}" / "runs" / f"{family}_run001"
    out_root = ensure_private_dir(campaign_root / "rerun" / f"{instance_id}-{arm}",
                                  mode=0o700)
    if not (study_dir / "K6.json").is_file():
        # 未到冻结确认（如 v0 臂 NC 结构拒绝）：如实记 skipped，不伪造 n_diffs=0
        report = {"instance_id": instance_id, "arm": arm,
                  "skipped": "NOT_FROZEN",
                  "detail": "运行未到 K6（NC/门结构拒绝），无确认批可复跑",
                  "n_diffs": None, "contrasts_match_K7": False}
        _write(campaign_root / "rerun" / f"{instance_id}-{arm}.json", report)
        progress.write("rerun_skipped", label=f"{instance_id}-{arm}",
                       reason="NOT_FROZEN")
        return report
    rerun_mod = _load_module("s1_independent_rerun",
                             _REPO / "scripts" / "s1" / "independent_rerun.py")
    stamp = utc_now_iso_z().replace(":", "").replace("-", "")
    report = rerun_mod.rerun_study(study_dir, family, out_root, stamp=stamp)
    _write(campaign_root / "rerun" / f"{instance_id}-{arm}.json",
           {"instance_id": instance_id, "arm": arm, **report})
    progress.write("rerun_done", label=f"{instance_id}-{arm}",
                   n_diffs=report["n_diffs"],
                   contrasts_match=report["contrasts_match_K7"])
    return report


def stage_trial(campaign_root: Path, *, arms: "list[str]",
                instances: "list[str] | None" = None,
                max_turns: int = 90) -> int:
    decision = _gate1_approved(campaign_root)
    _verify_locks()
    calibration = _calibration()
    nc_sensitivity = {"A_N": calibration["nc_sensitivity"]["A_N"],
                      "B_N": calibration["nc_sensitivity"]["B_N"]}
    validation = _load_json(rsi_s2_candidate_dir() / "validation.json")
    # 评审 MEDIUM：预测签名必须在**试验前**冻结（计划 §3 S2-3「预测签名入 LOCK」），
    # 否则事后改 validation.json 即可翻转 prediction_fulfilled。此处写签名锁，
    # 判件复算 sha 并比对。
    import hashlib as _hashlib

    def _sig_sha(sig):
        return _hashlib.sha256(json.dumps(sig, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()

    prediction_lock = {
        "schema": "simpraxis.rsi_s2.prediction_lock.v1",
        "prediction_signature": validation.get("prediction_signature"),
        "prediction_signature_sha256": _sig_sha(
            validation.get("prediction_signature")),
        "validation_sha256": _sig_sha(validation),
        "mechanism_hypothesis_sha256": _hashlib.sha256(
            str(validation.get("mechanism_hypothesis") or "").encode()).hexdigest(),
        # 试验前写锁 ⇒ 本时刻必然早于任何 trial 的 generated_at（判件按时间戳
        # 复核，而非信这个布尔字面量——2026-11-19 复评 MEDIUM）。
        "locked_before_trial": True,
        "locked_at_utc": utc_now_iso_z(),
    }
    _write(campaign_root / "prediction_lock.json", prediction_lock)
    candidate = load_candidate(rsi_s2_candidate_dir() / "package",
                               diff_sha256=validation["diff_sha256"])
    _write(campaign_root / "candidate_ref.json",
           {"validation": validation, "harness": candidate.to_dict(),
            "prediction_lock": prediction_lock})
    progress = Progress(campaign_root)
    ledger = SessionLedger(rsi_s2_sessions_dir() / "sessions.jsonl",
                           hard_cap_sessions=S2_SESSION_CAP)
    campaign_gate = ApprovedCampaignGate(decision)
    selected = [i for i in PREREG.INSTANCES
                if not instances or str(i["instance_id"]) in instances]
    for instance in selected:
        task = register_task(PREREG.build_instance(instance))
        salt = hashlib_salt(instance["instance_id"])
        for arm in arms:
            try:
                _run_one(campaign_root=campaign_root, instance=instance,
                         task=task, arm=arm, candidate=candidate,
                         campaign_gate=campaign_gate, progress=progress,
                         ledger=ledger, salt=salt,
                         nc_sensitivity=nc_sensitivity[instance["family"]],
                         max_turns=max_turns)
            except Exception as exc:  # noqa: BLE001 - 仪器失败如实结案
                progress.write("trial_crashed", label=f"{instance['instance_id']}-{arm}",
                               error=f"{type(exc).__name__}: {exc}")
                raise
            _rerun(campaign_root=campaign_root,
                   instance_id=str(instance["instance_id"]),
                   family=str(instance["family"]), arm=arm, progress=progress)
    progress.write("trial_all_done")
    print(json.dumps({"campaign_root": str(campaign_root),
                      "arms": arms}, ensure_ascii=False, indent=2))
    return 0


def hashlib_salt(instance_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"s2-salt|{instance_id}".encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 阶段：双审（24 POST）
# ---------------------------------------------------------------------------

def _gateway():
    from orchestrator.gateway.router import ModelGateway

    return ModelGateway(max_attempts=1, ledger=QuotaLedger.rsi_s2())


def _approve(batch_id: str, approved: int, note: str, task: int = 2) -> Path:
    from orchestrator.gateway.quota_ledger import QuotaError

    try:
        return write_approval(batch_id=batch_id, task=task, stage="coding_debug",
                              approved=approved, beyond_normal=False, note=note,
                              namespace=RSI_S2_NAMESPACE)
    except QuotaError:
        path = rsi_s2_approvals_dir() / f"{batch_id}.json"
        if path.is_file():
            doc = _load_json(path)
            if doc.get("approved", 0) >= approved and doc.get("task") == task:
                return path
        raise


def _pack_for(campaign_root: Path, instance: Mapping[str, Any],
              arm: str, channel: str) -> "dict[str, Any]":
    instance_id = str(instance["instance_id"])
    family = str(instance["family"])
    run_dir = campaign_root / "trial" / f"{instance_id}-{arm}" / "runs" / f"{family}_run001"
    k8_path = run_dir / "K8.json"
    k5_path = run_dir / "K5.json"
    if k8_path.is_file():
        claim = _load_json(k8_path)
    else:
        # 未到 K8：包内如实标注「无主张」，让审查独立判 reject（不补写）
        stage = (_load_json(campaign_root / "trial" / f"{instance_id}-{arm}"
                            / "study_report.json").get("outcome") or {}
                 ).get("stages") or {}
        code = (stage.get(family) or {}).get("code") or "NO_CLAIM"
        # 中性别：不得写成「结构停止码」——那会告诉审稿人这是对照臂（盲审泄漏）
        claim = {"verdict": "INCONCLUSIVE",
                 "statement": "未产出可主张结果（无 K8 记录）",
                 "contrasts": []}
    k5 = _load_json(k5_path) if k5_path.is_file() else {}
    report = claim.get("report")
    # 审查包必须自足：逐对比带 **proposition/delta/metric_id/level**，
    # 否则 reviewer 无法独立核验五值分类（2026-11-19 仪器缺陷：首版漏了这四项，
    # 审查原文「δ 仅由分类反推」「EQUIVALENT 因 δ 未随材料提供而无法独立核验」）。
    results = [{k: row.get(k) for k in ("id", "metric_id", "proposition", "delta",
                                        "level", "estimate", "ci_low", "ci_high",
                                        "n_blocks", "claim")}
               for row in (claim.get("contrasts") or [])]
    nc = k5.get("negative_control") or {}
    nc_text = ("阴性对照三连判据通过：" + json.dumps(nc, ensure_ascii=False)
               if nc.get("passed") else "阴性对照未通过或未注册")
    return RV2.build_pack_v2(
        instance_id=instance_id, arm=arm, channel=channel,
        question=str(instance["question"]),
        methods=("2×2 全因子 DiD，32 个宿主私有配对 block；"
                 "block 级 percentile bootstrap；G-CLAIM 逐字段 host 复算"),
        results=results, claim={"verdict": claim.get("verdict"),
                                "statement": claim.get("statement")},
        report=report if isinstance(report, Mapping) else {},
        negative_control=nc_text)


def stage_review(campaign_root: Path) -> int:
    _gate1_approved(campaign_root)
    progress = Progress(campaign_root)
    gateway = _gateway()
    # 仪器校准（6 例 2 呼；误接收即停付）
    calib_batch = "s2-review-calib-1"
    calib_path = campaign_root / "review_calibration.json"
    if calib_path.is_file():
        # 复用既有校准报告（仪器未变则不重烧付费调用）
        calibration = _load_json(calib_path)
        RV2.verify_frozen_report(calibration)
        progress.write("review_calibration_reused",
                       report_sha256=calibration.get("report_sha256"))
    else:
        # 校准与双审同车道（task=2）：台账记录的首个批准批即 task 2；task 1
        # 帽为 0（未使用），新根上走 task 1 会被 preflight 直接拦死（评审 HIGH）。
        _approve(calib_batch, 2, "S2 双审仪器校准（6 例分 2 呼；自批）", task=2)
        calibration = _calibrate_instrument(gateway, calib_batch, task=2)
    _write(campaign_root / "review_calibration.json", calibration)
    progress.write("review_calibration", ready=calibration.get("ready"),
                   verdict=calibration.get("verdict"))
    if calibration.get("ready") is not True:
        raise SystemExit("REVIEW_INSTRUMENT_NOT_READY: 停付修仪器（不放宽）")
    # 预登记降样本序（预算触顶）：砍尾至 5 对（去掉 b3）；旧仪器（无 δ/proposition）
    # 的 12 包报告整目录留档，不改写、不删除。
    # 降样本**按剩余预算门控**：全部 6 对 × 2 通道 = 24 呼；预算不足才按
    # 预登记序砍尾（不是无条件 5 对——那会让 R2 主判在满预算下也不可达）。
    from orchestrator.gateway.quota_ledger import QuotaLedger
    used = len([r for r in QuotaLedger.rsi_s2()._read_rows()
                if r.get("event", "call") == "call"])
    from orchestrator.gateway.quota_ledger import RSI_S2_HARD_CAP

    remaining = int(RSI_S2_HARD_CAP) - used
    pairs = len(PREREG.INSTANCES)
    while pairs > 1 and 2 * 2 * pairs > remaining:
        pairs -= 1
    selected = list(PREREG.SAMPLE_REDUCTION_ORDER[:pairs])
    progress.write("review_sample_plan", pairs=pairs, remaining_budget=remaining,
                   order=selected)
    review_dir = ensure_private_dir(campaign_root / "review", mode=0o700)
    # 归档**幂等**：仅在首次切换仪器时执行一次（marker 文件防止二次覆盖证据）
    marker = campaign_root / ".review_instrument_v1_archived"
    if not marker.is_file():
        archive = ensure_private_dir(campaign_root / "review_instrument_v1",
                                    mode=0o700)
        moved = 0
        for path in review_dir.glob("*.json"):
            target = archive / path.name
            if not target.is_file():
                path.rename(target)
                moved += 1
        marker.write_text("archived at 2026-11-19T12:34Z", encoding="utf-8")
        progress.write("review_instrument_archived", count=moved,
                       archived=str(archive))
    review_batch = "s2-dual-review-2"
    # 剩余预算 = 硬帽 40 − 已用 20 = 20 呼（5 对 × 2 通道）
    _approve(review_batch, 20, "S2 双审 2 通道 × 10 包（降样本 5 对；自批）",
             task=2)
    call = lambda prompt: gateway.call(  # noqa: E731
        "coding_debug", [{"role": "user", "content": prompt}],
        max_tokens=8192, temperature=0.0,
        thinking={"type": "disabled"})
    reports: "list[dict[str, Any]]" = []
    with billing_context(task=2, batch_id=review_batch):
        for instance in [i for i in PREREG.INSTANCES
                         if str(i["instance_id"]) in set(selected)]:
            for arm in ("v0", "v1"):
                existing_path = (campaign_root / "review"
                                 / f"{instance['instance_id']}-{arm}.json")
                if existing_path.is_file():
                    # 断点续跑：已落盘包直接复用（不重烧付费调用）
                    reports.append(_load_json(existing_path.with_suffix(
                        ".json")).get("frozen") or _load_json(existing_path))
                    progress.write("review_skip_existing",
                                   label=f"{instance['instance_id']}-{arm}")
                    continue
                channel_rows = {}
                for channel in ("channel_a", "channel_b"):
                    pack = _pack_for(campaign_root, instance, arm, channel)
                    channel_rows[channel] = RV2.run_channel(
                        call, pack=pack, channel=channel)
                merged = RV2.merge_channels(
                    instance_id=str(instance["instance_id"]), arm=arm,
                    channel_a=channel_rows["channel_a"],
                    channel_b=channel_rows["channel_b"])
                merged["frozen"] = RV2.freeze_report(merged)
                reports.append(merged)
                _write(campaign_root / "review"
                       / f"{instance['instance_id']}-{arm}.json", merged)
                progress.write("review_done",
                               label=f"{instance['instance_id']}-{arm}",
                               verdict=merged["verdict"])
    frozen = RV2.freeze_report({"schema": f"{RV2.SCHEMA}.report",
                                 "calibration": calibration,
                                 "reports": reports,
                                 "generated_at_utc": utc_now_iso_z()})
    _write(campaign_root / "dual_review.json", frozen)
    print(json.dumps({"report_sha256": frozen["report_sha256"],
                      "verdicts": {f"{r['instance_id']}-{r['arm']}": r["verdict"]
                                   for r in reports}}, ensure_ascii=False,
                     indent=2))
    return 0


CALIB_PROMPT = """你是审查仪器的校准受试者。下面给出 {n} 个微研究摘要（混合有效与无效）。
对每一个，独立判定应 accept 还是 reject，并只输出 JSON 数组：
[{{"id": "...", "verdict": "accept|reject", "reason": "一句话"}}]

评判口径：主张必须由给定设计与统计支撑；伪重复（同 run 时间步当独立样本）、
过度概括（外推到未测范围）、空模板（无可核内容）、与数据矛盾的结论一律 reject。

摘要：
{cases}
"""


def _calibrate_instrument(gateway, calib_batch: str, *,
                          task: int = 1) -> "dict[str, Any]":
    """6 例校准（**S2 命名空间自有实现**；S1 的 _approve 会写封存 S1 账，禁止复用）。"""
    cases = list(RV2.calibration_cases())
    batches = [cases[i:i + 3] for i in range(0, len(cases), 3)]
    results: "dict[str, str]" = {}
    raw_calls: "list[dict[str, Any]]" = []
    with billing_context(task=task, batch_id=calib_batch):
        for batch in batches:
            ids = [case["id"] for case in batch]
            prompt = CALIB_PROMPT.format(
                n=len(batch),
                cases=json.dumps([{"id": c["id"], "summary": c["summary"]}
                                  for c in batch], ensure_ascii=False, indent=2))
            text, tokens = gateway.call(
                "coding_debug", [{"role": "user", "content": prompt}],
                max_tokens=8192, temperature=0.0,
                thinking={"type": "disabled"})
            raw_calls.append({"ids": ids, "raw_text": text,
                              "tokens": list(tokens),
                              "at_utc": utc_now_iso_z()})
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end <= start:
                raise SystemExit("CALIB_PARSE: 输出不含 JSON 数组")
            try:
                rows = json.loads(text[start:end + 1])
            except ValueError as exc:
                raise SystemExit(f"CALIB_PARSE: {exc}") from None
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, Mapping):
                    continue
                rid = str(row.get("id") or "")
                verdict = str(row.get("verdict") or "").strip().lower()
                if rid in ids and verdict in ("accept", "reject"):
                    results[rid] = verdict
            missing = [i for i in ids if i not in results]
            if missing:
                raise SystemExit(f"CALIB_INCOMPLETE: 缺判定 {missing}")
    expected = RV2.calibration_expectations()
    mismatches = {cid: {"expected": expected[cid], "got": results.get(cid)}
                  for cid in expected if results.get(cid) != expected[cid]}
    misaccept = [cid for cid, row in mismatches.items()
                 if row["got"] == "accept" and row["expected"] == "reject"]
    ready = not mismatches
    report = {
        "schema": f"{RV2.SCHEMA}.calibration",
        "ready": ready,
        "verdict": ("REVIEW_INSTRUMENT_READY" if ready
                    else "REVIEW_INSTRUMENT_NOT_READY"),
        "results": results, "expected": expected,
        "mismatches": mismatches, "misaccept": misaccept,
        "calls": raw_calls,
        "calibrated_at_utc": utc_now_iso_z(),
    }
    return RV2.freeze_report(report)


# ---------------------------------------------------------------------------
# 阶段：统计初判
# ---------------------------------------------------------------------------

def stage_analyze(campaign_root: Path) -> int:
    _gate1_approved(campaign_root)
    progress = Progress(campaign_root)
    dual = _load_json(campaign_root / "dual_review.json")
    by_key = {f"{r['instance_id']}-{r['arm']}": r for r in dual["reports"]}
    instance_ids, v0_scores, v1_scores = [], [], []
    v0_success, v1_success = [], []
    dimension_scores = {"v0": {}, "v1": {}}
    completed = {"v0": 0, "v1": 0}
    rerun_diffs = {"v0": 0, "v1": 0}
    claim_violations = {"v0": 0, "v1": 0}
    session_tokens = {"v0": 0, "v1": 0}
    ledger_rows = SessionLedger(
        rsi_s2_sessions_dir() / "sessions.jsonl").read()
    reviewed = [i for i in PREREG.INSTANCES
                if all((campaign_root / "review" / f"{i['instance_id']}-{arm}.json")
                       .is_file() for arm in ("v0", "v1"))]
    if len(reviewed) < len(PREREG.INSTANCES):
        progress.write("analysis_sample_reduced",
                       reviewed=[str(i["instance_id"]) for i in reviewed],
                       dropped=[str(i["instance_id"]) for i in PREREG.INSTANCES
                                if i not in reviewed])
    for instance in reviewed:
        iid = str(instance["instance_id"])
        instance_ids.append(iid)
        for arm in ("v0", "v1"):
            trial = _load_json(campaign_root / "trial" / f"{iid}-{arm}"
                               / "study_report.json")
            stage = (trial["outcome"].get("stages") or {}).get(instance["family"]) or {}
            if stage.get("status") == "done":
                completed[arm] += 1
            if stage.get("code") == "CLAIM_INVALID":
                claim_violations[arm] += 1
            rerun = _load_json(campaign_root / "rerun" / f"{iid}-{arm}.json")
            rerun_diffs[arm] += int(rerun.get("n_diffs") or 0)
            session_tokens[arm] += sum(_session_tokens(
                ledger_rows, f"{iid}-{arm}").values())
            merged = by_key[f"{iid}-{arm}"]
            passed = merged["verdict"] == "PASS"
            (v1_scores if arm == "v1" else v0_scores).append(1.0 if passed else 0.0)
            (v1_success if arm == "v1" else v0_success).append(bool(passed))
            for channel in ("channel_a", "channel_b"):
                scores = (merged["channels"][channel]["parsed"]
                          .get("dimension_scores") or {})
                for dim, value in scores.items():
                    dimension_scores[arm].setdefault(dim, []).append(float(value))
    deltas = {}
    for dim in set(dimension_scores["v0"]) & set(dimension_scores["v1"]):
        deltas[dim] = (sum(dimension_scores["v1"][dim])
                       / max(1, len(dimension_scores["v1"][dim]))
                       - sum(dimension_scores["v0"][dim])
                       / max(1, len(dimension_scores["v0"][dim])))
    lock_path = campaign_root / "prediction_lock.json"
    if lock_path.is_file():
        prediction_signature = _load_json(lock_path).get("prediction_signature")
    else:
        prediction_signature = None
        progress.write("prediction_lock_missing",
                       note="预注册预测签名未在试验前冻结（不得计入兑现）")
    non_inf = ANALYSIS.non_inferiority(
        v0={"completed": completed["v0"], "rerun_n_diffs": rerun_diffs["v0"],
            "g_claim_violations": claim_violations["v0"],
            "session_tokens": session_tokens["v0"]},
        v1={"completed": completed["v1"], "rerun_n_diffs": rerun_diffs["v1"],
            "g_claim_violations": claim_violations["v1"],
            "session_tokens": session_tokens["v1"]},
        bounds={"session_cost_ratio_max": "1.50"})
    upstream = ANALYSIS.adapter_to_physicalrsi(
        instances=instance_ids,
        scores={"v0": v0_scores, "v1": v1_scores},
        successes={"v0": v0_success, "v1": v1_success},
        evidence_root=campaign_root,
        evidence_files={})
    analysis = ANALYSIS.analyze(
        instance_ids=instance_ids, v0_scores=v0_scores, v1_scores=v1_scores,
        v0_success=v0_success, v1_success=v1_success,
        non_inferiority_report=non_inf, upstream=upstream,
        prediction_signature=prediction_signature,
        dimension_deltas=deltas)
    analysis["dimension_deltas"] = deltas
    analysis["completed"] = completed
    analysis["session_tokens"] = session_tokens
    _write(campaign_root / "analysis.json", analysis)
    progress.write("analysis_done", verdict=analysis["verdict"],
                   p_two_sided=analysis["statistics"]["p_two_sided"])
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 阶段：Gate2（真人结果卡）
# ---------------------------------------------------------------------------

def stage_gate2(campaign_root: Path, max_wait: float) -> int:
    _gate1_approved(campaign_root)
    analysis = _load_json(campaign_root / "analysis.json")
    dual = _load_json(campaign_root / "dual_review.json")
    by_key = {f"{r['instance_id']}-{r['arm']}": r["verdict"]
              for r in dual["reports"]}
    table_lines = [f"{iid}: v0={by_key[f'{iid}-v0']} vs v1={by_key[f'{iid}-v1']}"
                   for iid in analysis["statistics"]["instances"]]
    stats = analysis["statistics"]
    explanation = {
        "task": "RSI-S2 首轮受控自改：6 实例 × 2 臂配对（主指标 review-pass 率）",
        "problem": ("配对表：" + "；".join(table_lines)
                    + f"；配对差均值={stats['mean_diff']:.3f}；"
                    f"单侧 p={stats['p_one_sided']:.4f}、双侧 p={stats['p_two_sided']:.4f}；"
                    f"非劣={analysis['non_inferiority']['passed']}；"
                    f"预测签名兑现={analysis['checks'].get('prediction_fulfilled')}；"
                    f"双判词初判={analysis['verdict']}"),
        "video": (f"证据目录：{campaign_root}（trial/ 12 臂-实例、rerun/、review/、"
                  "analysis.json、shadow_gate.json）。打回必须带意见（per-Stage ≤2）。"),
    }
    progress = Progress(campaign_root)
    gates = FeishuGates(thread_id=f"s2-campaign-{campaign_root.name}",
                        evidence_root=campaign_root, max_wait=max_wait,
                        progress=progress)
    try:
        decision = gates.gate2(
            instance_id="S2-CAMPAIGN", arm="v0-vs-v1",
            card={"metrics": {"review_pass": {
                      "v0": sum(1 for k, v in by_key.items() if k.endswith("-v0")
                                and v == "PASS"),
                      "v1": sum(1 for k, v in by_key.items() if k.endswith("-v1")
                                and v == "PASS")}},
                  "explanation": explanation,
                  "evidence": str(campaign_root)})
    finally:
        gates.close()
    ok = str(decision.get("decision")) == "approve"
    _write(campaign_root / "gate2_decision.json",
           {"approved": ok, "decision": decision,
            "at_utc": utc_now_iso_z()})
    progress.write("gate2_done", approved=ok)
    print(json.dumps({"gate2_approved": ok,
                      "human_notes": decision.get("human_notes")},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 4


# ---------------------------------------------------------------------------
# 阶段：结案（机判）
# ---------------------------------------------------------------------------

def stage_close(campaign_root: Path) -> int:
    _gate1_approved(campaign_root)
    judge = _load_module("s2_closure_judge",
                         _REPO / "scripts" / "verification" / "check_s2_closure.py")
    report = judge.run(campaign_root=campaign_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("pass") else 4


STAGE_FUNCS = {"gate1": stage_gate1, "propose": stage_propose,
               "trial": stage_trial, "review": stage_review,
               "analyze": stage_analyze, "gate2": stage_gate2,
               "close": stage_close}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S2 战役执行")
    parser.add_argument("stage", choices=sorted(STAGE_FUNCS))
    parser.add_argument("--campaign-root", default="")
    parser.add_argument("--max-wait", type=float, default=5400.0)
    parser.add_argument("--arms", nargs="+", default=["v0", "v1"])
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--reuse-workspace", default="")
    parser.add_argument("--instances", nargs="+", default=None)
    args = parser.parse_args(argv)
    if args.campaign_root:
        campaign_root = Path(args.campaign_root)
    elif args.stage == "gate1":
        stamp = utc_now_iso_z().replace(":", "").replace("-", "")
        campaign_root = ensure_private_dir(
            rsi_s2_evidence_root() / f"s2-campaign-{stamp}", mode=0o700)
    else:
        roots = sorted(p for p in rsi_s2_evidence_root().glob("s2-campaign-*")
                       if p.is_dir())
        if not roots:
            raise SystemExit("CAMPAIGN_ROOT_MISSING: 先跑 gate1")
        campaign_root = roots[-1]
    if args.stage == "gate1":
        return stage_gate1(campaign_root, args.max_wait)
    if args.stage == "propose":
        return stage_propose(campaign_root, attempts=args.attempts,
                             max_turns=args.max_turns, timeout_s=args.timeout,
                             reuse_workspace=(Path(args.reuse_workspace)
                                              if args.reuse_workspace else None))
    if args.stage == "trial":
        return stage_trial(campaign_root, arms=list(args.arms),
                           instances=args.instances, max_turns=args.max_turns)
    if args.stage == "review":
        return stage_review(campaign_root)
    if args.stage == "analyze":
        return stage_analyze(campaign_root)
    if args.stage == "gate2":
        return stage_gate2(campaign_root, args.max_wait)
    return stage_close(campaign_root)


if __name__ == "__main__":
    raise SystemExit(main())
