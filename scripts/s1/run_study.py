#!/usr/bin/env python3
"""RSI-S1 研究执行 CLI（S1-5/S1-6；A_N / B_N 共用）。

流程：预注册 LOCK 校验（付费前）→ Gate1 多 Stage 真卡（飞书，人工批准）→
driver K0→K8（受信物理 + fresh 会话触点 + host 逐点复算 + 打回循环）→
Gate2 真卡（人工结果门）→ 研究包落盘（证据根/会话账/双面成本）。

纪律：
* 批准前零付费零实跑（No Fake Approval；driver.run_campaign 同语义）；
* 不服役任何假门：``--dry-run`` 只打印卡面与计划，不发卡不跑仿真；
* 会话面（headless）与网关面（POST）分列记账；价格 UNKNOWN 照实；
* 打回循环耗尽 = human_failure 诚实结案；首卡即过 = 如实 gap 挂牌。

用法::

    python scripts/s1/run_study.py --stages A_N B_N --max-wait 7200
    python scripts/s1/run_study.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_s1_closeout_dir, rsi_s1_evidence_root, rsi_s1_prereg_dir,
    rsi_s1_sessions_dir)
from orchestrator.gateway.quota_ledger import QuotaLedger  # noqa: E402
from orchestrator.hk import study_gate1_cards as G  # noqa: E402
from orchestrator.hk.driver import (DIAGNOSE_BUDGET, GATE2_MAX_REJECTS_PER_STAGE,  # noqa: E402
                                    TOUCHPOINT_BUDGET, GateDecision, ResearchDriver)
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,  # noqa: E402
                                    default_confirmer)
from orchestrator.hk.touchpoints import HeadlessActor, HeadlessSessionRunner  # noqa: E402
from orchestrator.rsi_bridge.physics_service import mujoco_denial_probe  # noqa: E402
from orchestrator.rsi_bridge.session_ledger import SessionLedger  # noqa: E402
from orchestrator.rsi_r1 import task_specs as TS  # noqa: E402

STUDY_BUILDERS = {"A_N": TS.build_r1n_a, "B_N": TS.build_r1n_b}
STUDY_GOALS = {
    "A_N": "摩擦×质量交互 DiD + sham 有效阴性（160 确认 run）",
    "B_N": "kp×tc 开放问题（128 确认 run；SUPPORTED/REFUTED/INCONCLUSIVE 均可计闭环）",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Progress:
    """运行进度流水（watchdog 的数据面）：append-only JSONL，逐事件落盘。

    S1 事故教训（2026-11-18）：交出人工门后若不监测，instrument 崩溃会被误报为
    "仍在等待"。此后每个关键事件都写一行，``scripts/s1/watch_study.py`` 以
    "进程存活 + 事件新鲜度" 双条件报警。
    """

    def __init__(self, root: Path):
        self.path = Path(root) / "progress.jsonl"

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts_utc": utc_now_iso_z(), "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)


class ProgressActor:
    """触点进度 + 产者链（session/model）随事件落盘。"""

    def __init__(self, inner, progress: Progress):
        self.inner = inner
        self.progress = progress

    def __call__(self, name, contract, value):
        self.progress.write("touchpoint_start", touchpoint=name)
        out = self.inner(name, contract, value)
        self.progress.write("touchpoint_end", touchpoint=name)
        return out


def _wrap_runner(kind: str, fn, progress: Progress):
    def wrapped(value, context=None):
        progress.write(f"{kind}_start")
        out = fn(value, context)
        fields: dict[str, Any] = {}
        if kind == "batch":
            fields["n_runs"] = len((out or {}).get("runs") or [])
        progress.write(f"{kind}_end", **fields)
        return out

    return wrapped


def _prereg_study(study: str) -> dict[str, Any]:
    return _load_json(rsi_s1_prereg_dir() / f"study_{study}.json")


def _verify_lock() -> dict[str, Any]:
    import importlib.util

    module_path = _REPO / "scripts" / "s1" / "prereg_studies.py"
    spec = importlib.util.spec_from_file_location("s1_prereg", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_prereg_lock()  # SystemExit = 未锁定


def study_spec(study: str) -> dict[str, Any]:
    prereg = _prereg_study(study)
    task = STUDY_BUILDERS[study]()
    return {
        "schema": "simpraxis.rsi_s1.experiment_spec",
        "task_id": task.task_id,
        "study": study,
        "plan_sha256": prereg["document_sha256"],
        "conditions": [c.to_dict() for c in task.conditions],
        "n_block": prereg["n_block"],
        "prereg_bundle_task_sha256": prereg["task_sha256"],
    }


def goal_card(studies: list[str]) -> dict[str, Any]:
    stages = []
    for study in studies:
        stages.append({
            "stage_id": study,
            "goal": STUDY_GOALS[study],
            "budget": f"触点 4 + diagnose ≤2；物理 {len(STUDY_BUILDERS[study]().conditions) * 32} run；0 网关 POST",
            "depends_on": ["A_N"] if study == "B_N" else [],
            "stop_rules": "校准不过改设计；触点/会话帽到即诚实收口；矛盾即结论",
        })
    stages.append({
        "stage_id": "REVIEW",
        "goal": "独立复跑 + 双审（审查仪器 6 例先过 READY）",
        "budget": "网关 ≤8 POST（校准 4 + 正式 4）",
        "depends_on": list(studies),
        "stop_rules": "误接收即停付修仪器；无第三票",
    })
    return G.build_goal_card(
        title="RSI-S1 最小自主研究闭环（A_N/B_N 双机制 + 阴性 + 复跑 + 双审）",
        stages=stages,
        notes=[
            f"预注册 LOCK 双签冻结：bundle {_verify_lock()['bundle_sha256'][:16]}…",
            "打回必须带意见；per-Stage 打回上限 2；批准前零实跑零付费",
            "sham 阴性必须判 equivalent(±0.001 m, 90%)；DiD 等效不可注册",
        ])


# ---------------------------------------------------------------------------
# 飞书门（生产实现；无任何默认批准）
# ---------------------------------------------------------------------------

class FeishuGates:
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
        self.gate1_decision: dict[str, Any] | None = None
        self.gate2_decisions: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.gateway.stop_ws()
        except Exception:  # noqa: BLE001 - 关停失败不影响已有证据
            pass

    def gate1_hook(self, card: Mapping[str, Any]) -> GateDecision:
        payload = G.build_gate1_card_payload(card)
        self.progress.write("gate1_sent", thread_id=self.thread_id)
        decision = self.gateway.request_gate1_approval(
            self.thread_id, payload["task_title"], payload["proposals"],
            max_wait=self.max_wait)
        self.progress.write("gate1_decision", decision=str(decision.get("decision")),
                            human_notes=str(decision.get("human_notes") or ""))
        self.gate1_decision = dict(decision, thread_id=self.thread_id,
                                   at_utc=utc_now_iso_z())
        atomic_write_text(self.evidence_root / "gate1_decision.json",
                          json.dumps(self.gate1_decision, ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", mode=0o600)
        return GateDecision(
            approved=str(decision.get("decision")) == "approve",
            human_notes=str(decision.get("human_notes") or ""),
            ref=f"feishu:{self.thread_id}:gate1")

    def gate2_hook(self, card: Mapping[str, Any], stage_id: str) -> GateDecision:
        metrics: dict[str, Any] = {}  # 现有卡面：主张/证据走 explanation 三问
        claim = card.get("claim") or {}
        contrasts = claim.get("contrasts") or []
        lines = [f"{row.get('id')}: {row.get('claim')} "
                 f"(est={row.get('estimate')}, CI=[{row.get('ci_low')}, "
                 f"{row.get('ci_high')}])" for row in contrasts if isinstance(row, dict)]
        explanation = {
            "task": f"Stage {stage_id}：{STUDY_GOALS.get(stage_id, stage_id)}"
                    "（MuJoCo 3.15.0 受信 host 服务；block 配对设计）",
            "problem": "主张与 host 逐点复算值：" + ("；".join(lines) or "（无对比行）")
                       + f"；run_idx={card.get('run_idx')}；"
                       f"打回计数={card.get('rejects')}",
            "video": f"本卡无视频；证据目录：{card.get('evidence')}"
                     "（包含 raw 记录/复算报告/触点 transcript）。"
                     "请批准或带意见打回（打回会注入诊断触点后重跑）。",
        }
        self.progress.write("gate2_sent", stage_id=stage_id,
                            run_idx=card.get("run_idx"))
        decision = self.gateway.request_gate2_approval(
            self.thread_id, f"RSI-S1 · {stage_id} 结果验收", metrics,
            intranet_url=str(card.get("evidence")), explanation=explanation,
            max_wait=self.max_wait)
        self.progress.write("gate2_decision", stage_id=stage_id,
                            decision=str(decision.get("decision")))
        record = dict(decision, stage_id=stage_id, run_idx=card.get("run_idx"),
                      thread_id=self.thread_id, at_utc=utc_now_iso_z())
        self.gate2_decisions.append(record)
        target = Path(str(card.get("evidence"))) / "gate2_decision.feishu.json"
        atomic_write_text(target, json.dumps(record, ensure_ascii=False,
                                             indent=2, sort_keys=True) + "\n",
                          mode=0o600)
        return GateDecision(
            approved=str(decision.get("decision")) == "approve",
            human_notes=str(decision.get("human_notes") or ""),
            ref=f"feishu:{self.thread_id}:gate2:{stage_id}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(*, studies: list[str], max_wait: float, dry_run: bool,
        thread_id: str | None = None) -> int:
    lock_report = _verify_lock()  # SystemExit = 未锁定：付费前硬门
    calibration_path = rsi_s1_closeout_dir() / "calibration_report.json"
    if not calibration_path.is_file():
        raise SystemExit("CALIBRATION_MISSING: 先跑 scripts/s1/calibrate_studies.py")
    calibration = _load_json(calibration_path)
    if calibration.get("all_ok") is not True:
        raise SystemExit("CALIBRATION_FAILED: 校准门未全过，不发付费")

    card = goal_card(studies) if not dry_run else G.build_goal_card(
        title="RSI-S1 最小自主研究闭环（dry-run）",
        stages=[{"stage_id": s, "goal": STUDY_GOALS[s], "budget": "dry-run",
                 "depends_on": [], "stop_rules": "dry-run"} for s in studies])
    if dry_run:
        print(json.dumps({"lock": lock_report,
                          "calibration_sha256": calibration["report_sha256"],
                          "gate1_payload": G.build_gate1_card_payload(card),
                          "studies": studies}, ensure_ascii=False, indent=2))
        return 0

    stamp = utc_now_iso_z().replace(":", "").replace("-", "")
    evidence_root = ensure_private_dir(rsi_s1_evidence_root() / f"{'-'.join(studies)}-{stamp}")
    thread = thread_id or f"s1-{'-'.join(studies)}-{stamp}"
    atomic_write_text(evidence_root / "prereg_lock.json",
                      json.dumps(lock_report, ensure_ascii=False, indent=2) + "\n",
                      mode=0o600)
    atomic_write_text(evidence_root / "calibration_ref.json",
                      json.dumps({"path": str(calibration_path),
                                  "report_sha256": calibration["report_sha256"]},
                                 ensure_ascii=False, indent=2) + "\n", mode=0o600)
    denial = mujoco_denial_probe(mode="auto",
                                 workspace=evidence_root / "denial_probe")
    atomic_write_text(evidence_root / "mujoco_denial_probe.json",
                      json.dumps(denial, ensure_ascii=False, indent=2) + "\n",
                      mode=0o600)
    if denial.get("all_denied") is not True:
        raise SystemExit("ISOLATION_FAILED: 沙箱内 mujoco 可导入（物理收权不成立）")

    progress = Progress(evidence_root)
    progress.write("study_start", studies=studies, thread_id=thread,
                   pid=__import__("os").getpid())
    ledger = SessionLedger(rsi_s1_sessions_dir() / "sessions.jsonl")
    runner = HeadlessSessionRunner(sessions_root=rsi_s1_sessions_dir() / "ws",
                                   ledger=ledger)
    actor = ProgressActor(HeadlessActor(runner, evidence_root=evidence_root,
                                        host_label=thread), progress)
    gates = FeishuGates(thread_id=thread, evidence_root=evidence_root,
                        max_wait=max_wait, progress=progress)
    try:
        driver = ResearchDriver(
            gate1_hook=gates.gate1_hook, gate2_hook=gates.gate2_hook,
            actor=actor, evidence_root=evidence_root,
            batch_runner=_wrap_runner("batch", default_batch_runner, progress),
            analyzer=default_analyzer,
            confirmer=_wrap_runner("confirm", default_confirmer, progress),
            claim_guard=True,
            touchpoint_budget=TOUCHPOINT_BUDGET,
            diagnose_budget=DIAGNOSE_BUDGET,
            max_rejects_per_stage=GATE2_MAX_REJECTS_PER_STAGE)
        stages = {study: study_spec(study) for study in studies}
        outcome = driver.run_campaign(card, stages)
        for study, row in (outcome.get("stages") or {}).items():
            progress.write("stage_end", stage=study, status=row.get("status"),
                           code=row.get("code"))
    except BaseException as exc:  # noqa: BLE001 - 崩溃也要留 watchdog 终态
        progress.write("study_crashed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        gates.close()

    gw_rows = [r for r in QuotaLedger.rsi_s1()._read_rows()
               if r.get("event", "call") == "call"]
    summary = ledger.summary(gateway_rows=gw_rows)
    report = {
        "schema": "simpraxis.rsi_s1.study_report.v1",
        "thread_id": thread,
        "studies": studies,
        "outcome": outcome,
        "gate1_decision": gates.gate1_decision,
        "gate2_decisions": gates.gate2_decisions,
        "session_summary": summary,
        "prereg_bundle_sha256": lock_report["bundle_sha256"],
        "calibration_report_sha256": calibration["report_sha256"],
        "generated_at_utc": utc_now_iso_z(),
        "gaps": [],
    }
    if gates.gate2_decisions and all(str(d.get("decision")) == "approve"
                                     for d in gates.gate2_decisions):
        report["gaps"].append("gate2_reject_loop_not_exercised")
    progress.write("study_end", status=outcome.get("status"))
    atomic_write_text(evidence_root / "study_report.json",
                      json.dumps(report, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", mode=0o600)
    print(json.dumps({"status": outcome.get("status"),
                      "stages": {k: v.get("status") for k, v in
                                 (outcome.get("stages") or {}).items()},
                      "evidence": str(evidence_root),
                      "session_face": summary["session_face"],
                      "gateway_face": summary["gateway_face"],
                      "gaps": report["gaps"]}, ensure_ascii=False, indent=2))
    return 0 if outcome.get("status") == "done" else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 研究执行（A_N/B_N）")
    parser.add_argument("--stages", nargs="+", default=["A_N", "B_N"],
                        choices=sorted(STUDY_BUILDERS))
    parser.add_argument("--max-wait", type=float, default=7200.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--thread", default="")
    args = parser.parse_args(argv)
    return run(studies=list(args.stages), max_wait=args.max_wait,
               dry_run=args.dry_run, thread_id=args.thread or None)


if __name__ == "__main__":
    raise SystemExit(main())
