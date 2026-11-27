"""S2c S2c-3：Q 池战役执行器（设计前端 → 登记 open task → K0–K8 驱动 → 复跑 → 记录）。

每简报：
1) run_design（actor=HeadlessActor 前端；注入 run_dir）→ G-DESIGN 五折（三物理回调）
   → compile；DESIGN_NOT_READY 即终点码（一轮为限）。
2) build_open_task（真 regime 注入）→ register_task。
3) ResearchDriver run_campaign：q_actor 把 loop 内 propose_plan 拦截为「回显冻结设计」
   （避免重提议漂移），K4/K8 走真实 HeadlessActor；harness v2（G-REPORT+NC 交叉校验）。
4) 独立复跑由 K7 confirmer(fresh_root) 内建；study_report 落盘。

用法：python scripts/s2c/run_open.py [--fake] [--only BRIEF]
--fake：本地非付费联调（假 LLM actor + harness v0），验证全链接线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.common.fsutil import ensure_private_dir
from orchestrator.common.runtime_paths import rsi_s2c_evidence_root, rsi_s2c_sessions_dir
from orchestrator.hk.design_compiler import compile_design
from orchestrator.hk.design_runner import run_design
from orchestrator.hk.driver import GateDecision, ResearchDriver
from orchestrator.hk.skills import (default_analyzer, default_batch_runner,
                                    default_confirmer, register_task)
from orchestrator.hk.touchpoints import HeadlessActor, HeadlessSessionRunner
from orchestrator.rsi_bridge.physics_service import PhysicsService
from orchestrator.rsi_bridge.session_ledger import SessionLedger
from orchestrator.rsi_r1 import rubric as R

from open_tasks import build_open_task, scene_config
from pool_open import _BRIEFS, build_briefs
from q_callbacks import make_callbacks, _nc_id

_STUDY = {"C_collision_rebound": "C_N", "D_forced_vibration": "D_N"}


def _mid_regime(knob) -> str:
    return str((float(knob["lo"]) + float(knob["hi"])) / 2)


def _good_design(brief) -> dict:
    ax = brief.factor_axes
    a_lo, a_hi = ax[0]["values"]; b_lo, b_hi = ax[1]["values"]
    return {"hypothesis": f"{ax[0]['name']}×{ax[1]['name']} 交互随 regime 改变",
            "conditions": [
                {"id": "c_hh", "factors": {ax[0]["name"]: a_hi, ax[1]["name"]: b_hi}},
                {"id": "c_hl", "factors": {ax[0]["name"]: a_hi, ax[1]["name"]: b_lo}},
                {"id": "c_lh", "factors": {ax[0]["name"]: a_lo, ax[1]["name"]: b_hi}},
                {"id": "c_ll", "factors": {ax[0]["name"]: a_lo, ax[1]["name"]: b_lo}}],
            "primary_contrast": {"id": "axb", "treatment_high": "c_hh",
                                 "treatment_low": "c_lh", "control_high": "c_hl",
                                 "control_low": "c_ll", "proposition": "decrease_gt"},
            "negative_control": {"base": "c_hh", "sham": "host_sham"},
            "metric": brief.metrics[0], "rationale": "2×2 交互可区分 regime 依赖 vs 无关"}


class _FakeActor:
    """非付费联调：propose_design 返回好设计；K4 透传；K8 合法 claim。"""
    def __init__(self, brief):
        self.brief = brief
    def __call__(self, name, contract, value):
        if name == "propose_design":
            return _good_design(self.brief)
        if name == "propose_design_repair":
            return _good_design(self.brief)
        if name == "write_analysis":
            return {"source": "# fake passthrough（default_analyzer 用 run_manifest）"}
        if name == "submit_claim":
            # 非付费联调：回放 host 重算的 contrasts（6 核字段）+ 主对比 verdict
            rows = value.get("results") or value.get("contrasts") or []
            keys = ("id", "estimate", "ci_low", "ci_high", "n_blocks", "claim")
            verdict = rows[0].get("claim") if rows else "SUPPORTED"
            return {"verdict": verdict, "statement": "校验：交互随 regime 改变",
                    "contrasts": [{k: r.get(k) for k in keys} for r in rows]}
        raise SystemExit(f"fake actor 未覆盖触点 {name}")


def _frontend_actor(headless, ev_dir):
    """前端（run_design）用：为 propose_design/repair 注入 run_dir 后调 HeadlessActor。"""
    ctr = {"n": 0}
    def a(name, contract, value):
        if name in ("propose_design", "propose_design_repair"):
            ctr["n"] += 1
            rd = ensure_private_dir(ev_dir / "design" / f"tp_{name}_{ctr['n']}", mode=0o700)
            return headless(name, contract, {**dict(value), "run_dir": str(rd)})
        raise SystemExit(f"前端未覆盖触点 {name}")
    return a


def _loop_actor(headless):
    """driver loop 用：propose_plan 拦截为回显冻结设计；K4/K8 走真实 headless。

    注意：skills.k1 把**整个 registered 包**传给 actor（`_require_mapping(value,"spec")`
    只是类型检查返回原值），故 nested 取 value["spec"]。
    """
    def a(name, contract, value):
        if name == "propose_plan":
            sp = value.get("spec", value) if isinstance(value, dict) else value
            return {"conditions": sp.get("conditions"), "n_block": sp.get("n_block") or 32,
                    "study": sp.get("study"), "task_id": sp.get("task_id"),
                    "plan_sha256": sp.get("plan_sha256")}
        return headless(name, contract, value)
    return a


def run_brief(bid: str, brief, *, fake: bool, ledger, campaign_root) -> dict:
    label_dir = ensure_private_dir(campaign_root / "trial" / bid, mode=0o700)
    knob = _BRIEFS[bid][4]
    true_regime = {knob["scene_param"]: _mid_regime(knob)}  # 真实 regime（中点，固定）
    svc = PhysicsService(observations_root=label_dir / "cb_obs",
                         evidence_root=label_dir / "cb_ev")
    nc, pe, di = make_callbacks(svc, brief, true_regime=true_regime,
                                regime_param=knob["scene_param"],
                                regime_lo=knob["lo"], regime_hi=knob["hi"], n_block=2)
    if fake:
        base = _FakeActor(brief); fe = _FakeActor(brief)
    else:
        runner = HeadlessSessionRunner(sessions_root=rsi_s2c_sessions_dir() / "ws",
                                       ledger=ledger, max_turns=90, timeout_s=1800)
        base = HeadlessActor(runner, evidence_root=label_dir, host_label=bid)
        fe = _frontend_actor(base, label_dir)
    # 1) 设计前端 + G-DESIGN
    res = run_design(brief, fe, nc_check=nc, pilot_effect=pe, discriminability=di,
                     delta=float(R.metric_delta(brief.metrics[0])))
    rec = {"brief_id": bid, "status": res.status, "repairs": res["repairs"],
           "gate_reports": res["gate_reports"]}
    if res.status != "DESIGN_READY":
        rec["code"] = "DESIGN_NOT_READY"
        (label_dir / "study_report.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
        return rec
    # 2) 设计 → 登记 open task（只取 primary 2×2 四单元 + host sham）
    nc_base = _nc_id(res["design"]["negative_control"]["base"])
    task = build_open_task(res["spec"], brief, true_regime=true_regime,
                           nc_base_id=nc_base)
    register_task(task)
    study = _STUDY[brief.scene_id]
    spec = {"schema": "simpraxis.s2c.experiment_spec", "task_id": task.task_id,
            "study": study, "plan_sha256": task.sha256, "n_block": 32,
            "conditions": [c.to_dict() for c in task.conditions]}
    # 3) K0–K8 驱动（q_actor 拦截 propose_plan）
    driver = ResearchDriver(
        gate1_hook=lambda card: GateDecision(approved=True),
        gate2_hook=lambda card, stage: GateDecision(approved=True),
        actor=_loop_actor(base), evidence_root=label_dir,
        batch_runner=default_batch_runner, analyzer=default_analyzer,
        confirmer=default_confirmer, claim_guard=True,
        harness=("v0" if fake else "v2"), touchpoint_budget=3, diagnose_budget=2,
        nc_sensitivity={"detected": True, "evidence": "G-DESIGN 判别力+NC 回调已过"})
    outcome = driver.run_campaign({"goal": bid}, {study: spec})
    stage = (outcome.get("stages") or {}).get(study, {})
    claim = stage.get("claim") or {}
    rec.update({"task_id": task.task_id, "study": study, "outcome_status": outcome.get("status"),
                "code": stage.get("code"), "detail": stage.get("detail"),
                "claim_verdict": claim.get("verdict"), "claim": claim})
    (label_dir / "study_report.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2, default=str))
    return rec


def run_open(*, fake: bool, only: str | None) -> int:
    ev = rsi_s2c_evidence_root()
    campaign_root = ensure_private_dir(ev / "pool_q", mode=0o700)
    ledger = SessionLedger(rsi_s2c_sessions_dir() / "sessions.jsonl", hard_cap_sessions=56)
    briefs = build_briefs()
    ids = [only] if only else list(briefs)
    results = {}
    for bid in ids:
        print(f"\n=== {bid} ({briefs[bid].scene_id}) ===")
        rec = run_brief(bid, briefs[bid], fake=fake, ledger=ledger, campaign_root=campaign_root)
        results[bid] = rec
        print(f"  {rec['status']} / {rec.get('outcome_status')} / "
              f"{rec.get('code')} verdict={rec.get('claim_verdict')}")
        if rec.get("detail"):
            print("  DETAIL:", rec["detail"][:600])
    summary = {"schema": "simpraxis.s2c.pool_q_summary.v1", "fake": fake,
               "instances": {k: {"status": v["status"], "outcome": v.get("outcome_status"),
                                 "code": v.get("code"), "claim_verdict": v.get("claim_verdict")}
                             for k, v in results.items()}}
    (campaign_root / "campaign_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\n汇总：{campaign_root / 'campaign_summary.json'}")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    return run_open(fake=args.fake, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
