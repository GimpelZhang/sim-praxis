#!/usr/bin/env python3
"""Task9：生成 RSI 看板 rsi_dashboard.json（计划 L581，零付费、确定性）。

对应 kick-off 的 RSI 指标最小集与详细计划 §6.2 proxy decoupling 要求：
1. 双轴定位：RSI 等级轴（kick-off 五阶段，当前 Level 2 知识积累）×
   两条实证 track 轴；
2. 每条 run 的 Rung1–3 外部真值（物理规则，从 telemetry 重新计算）与
   Rung4 代理值（VLM 原子 rubric，读脱敏 resp 原件）并列，分歧即告警；
3. 循环次数分布（agent_run 次数、coding 补救轮数、Gate1/2 打回数、Rung4
   调用数）；
4. 打回意见主题聚类（真人 human_notes 原文 → 关键词主题，保留原文引用）；
5. phase1_learnings 新增条数与召回命中次数（生产 runner 接线前命中数=0，
   夹具召回证据单列，禁止把 tmp store 的命中记成生产命中）。

只读本仓代码与 /mnt 证据，不发任何网络/付费调用；输出落
$SIMPRAXIS_PHASE1_ROOT/task9_milestone/rsi_dashboard.json。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from orchestrator.judge.gait_rungs import evaluate_batch  # noqa: E402
from orchestrator.judge.rungs import evaluate_telemetry_file  # noqa: E402

PHASE1_ROOT = Path(os.environ.get(
    "SIMPRAXIS_PHASE1_ROOT", "/mnt/artifacts/simpraxis/phase1"))
RUNS_ROOT = Path(os.environ.get(
    "SIMPRAXIS_RUN_DIR", "/mnt/artifacts/simpraxis/runs")) / "phase1"

SCHEMA = "simpraxis.rsi_dashboard.v1"
CAP_NORMAL = 36
CAP_HARD = 48

# run 族目录 → 结案信封线程（host 侧 phase1/runs/ 只放 DONE/FAILURE）。
T7_RUNS = [
    ("t7-mujoco-20261015", "track1",
     "task7_track1/run_t7-mujoco-20261015_1",
     "task7_track1/run_t7-mujoco-20261015_2"),
    ("t7-mujoco-20261015b", "track1",
     "task7_track1/run_t7-mujoco-20261015b_1"),
    ("t7-maniskill-20261015a", "track1",
     "task7_track1/run_t7-maniskill-20261015a_1"),
]
T8_RUN = ("t8-genesis-20261016a", "track2",
          "task8_track2/q1_genesis/run_t8-genesis-20261016a_1")
T8_SPLITS = [
    ("normal_vs_ice_nominal",
     "task8_track2/evidence/"
     "rung4_t8-genesis-20261016a-r1_normal_vs_ice_nominal.resp.json"),
    ("ice_nominal_vs_ice_dr",
     "task8_track2/evidence/"
     "rung4_t8-genesis-20261016a-r1_ice_nominal_vs_ice_dr.resp.json"),
]


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def _load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _envelopes() -> dict:
    out: dict[str, dict] = {}
    for env_dir in sorted((PHASE1_ROOT / "runs").iterdir()):
        for name in ("DONE.json", "FAILURE.json"):
            f = env_dir / name
            if f.exists():
                out[env_dir.name] = _load_json(f)
    return out


def _gates_rows() -> list[dict]:
    db = PHASE1_ROOT / "state" / "pending_gates.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT thread_id,gate,status,decision,human_notes,source,"
        "card_message_id,media_message_id,created_at,decided_at "
        "FROM pending_gates ORDER BY created_at")]
    con.close()
    return rows


def _rung4_answers(resp_path: Path) -> dict:
    """解析一张 Rung4 脱敏 resp → {yes,n,low_confidence,status,answers}。

    resp 外层是网关信封 {ok,text,tokens_in, tokens_out}；text 是 rubric JSON。
    """
    if not resp_path.exists():
        # req 存在而 resp 缺席：ledger 记失败（见 paid_calls 对账），
        # 代理值不可用，由人工 Gate2 裁决兜底，不伪造分数。
        return {"status": "unavailable", "yes": None, "n": None,
                "low_confidence": None, "answers": [],
                "note": "resp 缺失（实呼失败，ledger 可对账）"}
    env = _load_json(resp_path)
    if not env.get("ok"):
        return {"status": "vlm_error", "yes": None, "n": None,
                "low_confidence": None, "answers": [],
                "note": str(env)[:200]}
    parsed = json.loads(env["text"])
    answers = parsed.get("answers", [])
    yes = sum(1 for a in answers if a.get("answer") == "yes")
    return {"status": "ok", "yes": yes, "n": len(answers),
            "low_confidence": bool(parsed.get("low_confidence")),
            "answers": answers}


def _t7_run_rows(gates: dict) -> list[dict]:
    rows = []
    for thread, track, *run_dirs in T7_RUNS:
        env = _envelopes().get(thread, {})
        agent_runs = []
        for rd in run_dirs:
            tel = RUNS_ROOT / rd / "telemetry.json"
            verdict = evaluate_telemetry_file(tel)
            agent_runs.append({
                "run_dir": rd,
                "rules_external_truth": {
                    "rung1": verdict["rung1"]["pass"],
                    "rung2": verdict["rung2"]["pass"],
                    "rung3": verdict["rung3"]["pass"],
                    "rules_pass": verdict["rules_pass"],
                },
            })
        # Rung4 证据按 run 后缀（-r1/-r2）落在 host 侧 evidence 目录。
        ev = PHASE1_ROOT / "task7_track1" / "evidence"
        proxy_runs = []
        for i in range(len(run_dirs)):
            suffix = f"r{i + 1}"
            resp = ev / f"rung4_{thread}-{suffix}.resp.json"
            proxy_runs.append({"run": i + 1, **_rung4_answers(resp)})
        g1 = gates.get((thread, 1), {})
        g2 = gates.get((thread, 2), {})
        human_final = env.get("status")
        alarms = _alarms_t7(agent_runs, proxy_runs, env)
        rows.append({
            "thread_id": thread, "track": track,
            "closeout": human_final,
            "gate1_rejects": env.get("gate1_rejects", 0),
            "gate1": {"decision": g1.get("decision"), "source": g1.get("source")},
            "gate2": {"decision": g2.get("decision"), "source": g2.get("source"),
                      "rejects": env.get("gate2_rejects", 0),
                      "human_notes": g2.get("human_notes") or ""},
            "agent_runs": agent_runs,
            "rung4_proxy_runs": proxy_runs,
            "proxy_decoupling_alarms": alarms,
        })
    return rows


def _alarms_t7(agent_runs: list[dict], proxy_runs: list[dict],
               env: dict) -> list[dict]:
    """告警判定（§6.2：分歧即告警，告警不自动改判）。"""
    alarms: list[dict] = []
    for ar, pr in zip(agent_runs, proxy_runs):
        rules_pass = ar["rules_external_truth"]["rules_pass"]
        if pr["status"] == "ok":
            proxy_pass = pr["yes"] == pr["n"] and not pr["low_confidence"]
            if rules_pass and not proxy_pass:
                alarms.append({"type": "proxy_says_defect_rules_pass",
                               "run": ar["run_dir"], "severity": "escalate_human"})
            elif proxy_pass and not rules_pass:
                alarms.append({"type": "proxy_ignores_rules_violation",
                               "run": ar["run_dir"], "severity": "escalate_human"})
        else:
            alarms.append({"type": "proxy_unavailable",
                           "run": ar["run_dir"], "severity": "human_fallback",
                           "detail": pr.get("note", pr["status"])})
    # 人类 Oracle 与两个自动化层的分歧：结案 human_failure 而代理全绿。
    if env.get("status") == "human_failure" and all(
            pr.get("status") == "ok" and pr.get("yes") == pr.get("n")
            for pr in proxy_runs):
        alarms.append({
            "type": "human_oracle_vs_proxies",
            "severity": "escalate_human",
            "detail": "Gate2 真人 2 次打回结案，而 Rung1–3 通过、Rung4 全 yes；"
                      "打回主题为卡片/视频意图说明不清（沟通类），非物理判异",
        })
    return alarms


def _t8_row() -> dict:
    thread, track, run_dir = T8_RUN
    wd = RUNS_ROOT / run_dir
    batch = evaluate_batch(wd)
    truth = {"rung1": batch["rung1"]["pass"],
             "rung2": batch["rung2"]["pass"],
             "rung3": batch["rung3"]["pass"],
             "rules_pass": batch["rules_pass"],
             "seeds": batch.get("seeds")}
    proxy_splits = []
    for split, rel in T8_SPLITS:
        proxy_splits.append({"split": split,
                             **_rung4_answers(PHASE1_ROOT / rel)})
    gates = {(r["thread_id"], r["gate"]): r for r in _gates_rows()}
    g1 = gates.get((thread, 1), {})
    g2 = gates.get((thread, 2), {})
    alarms = []
    if proxy_splits[1]["status"] == "unavailable":
        alarms.append({"type": "proxy_unavailable",
                       "split": "ice_nominal_vs_ice_dr",
                       "severity": "human_fallback",
                       "detail": "第 2 次 Rung4 实呼连接失败（ledger seq27），"
                                 "Gate2 凭原件人工裁决，代理侧缺口不补造"})
    summary = _load_json(wd / "summary.json")
    return {
        "thread_id": thread, "track": track, "closeout": "done",
        "gate1_rejects": 0,
        "gate1": {"decision": g1.get("decision"), "source": g1.get("source")},
        "gate2": {"decision": g2.get("decision"), "source": g2.get("source"),
                  "rejects": 0, "human_notes": ""},
        "rules_external_truth": truth,
        "groups": {k: {"success_rate": v.get("success_rate"),
                       "n_fell": v.get("n_fell"),
                       "slip_dist_mean_m": v.get("slip_dist_mean_m")}
                   for k, v in batch.get("groups", {}).items()},
        "dr_schema": summary.get("schema"),
        "conclusion": "contradiction_control_not_robust",
        "conclusion_basis": "ice/normal 对比率 0.824 < 预注册界 2.0；"
                            "outcome 与 mechanism 分开报告，矛盾本身是结论（L571）",
        "rung4_proxy_splits": proxy_splits,
        "proxy_decoupling_alarms": alarms,
    }


def _loop_distribution(rows: list[dict], t8: dict, ledger: list[dict]) -> dict:
    coding = Counter(r["thread_id"] for r in ledger
                     if r["stage"] == "coding_debug" and r["ok"])
    rung4_calls = Counter(r["thread_id"] for r in ledger
                          if r["stage"] == "visual_rollout" and r["ok"])
    per_thread = {}
    for row in rows + [t8]:
        tid = row["thread_id"]
        per_thread[tid] = {
            "agent_runs": 2 if row.get("closeout") == "human_failure"
            else (len(row["agent_runs"]) if "agent_runs" in row else 1),
            "coding_rounds_ok": coding.get(tid, 0),
            "gate1_rejects": row.get("gate1_rejects", 0),
            "gate2_rejects": row["gate2"].get("rejects") or 0,
            "rung4_calls_ok": rung4_calls.get(tid, 0),
        }
    # 标定批次（非 run 线程）单列，不混入 run 循环统计。
    calib = {tid: n for tid, n in rung4_calls.items() if "cal" in tid}
    return {"per_thread": per_thread,
            "calibration_threads_rung4_ok": calib,
            "coding_rounds_total": sum(coding.values()),
            "gate1_rejects_total": sum(
                v["gate1_rejects"] for v in per_thread.values()),
            "gate2_rejects_total": sum(
                v["gate2_rejects"] for v in per_thread.values())}


def _reject_clusters(gates: list[dict], envelopes: dict) -> list[dict]:
    notes = [(r["thread_id"], r["gate"], (r["human_notes"] or "").strip())
             for r in gates if r["gate"] == 2 and r["decision"]
             and r["decision"].startswith("reject")]
    captured_per_thread = Counter(t for t, _g, _n in notes)
    topics = {
        "卡片/视频意图说明不清": ["说明", "什么意思", "做什么", "解决什么"],
    }
    clusters: dict[str, list[dict]] = {}
    for thread, gate, note in notes:
        topic = next((tp for tp, kws in topics.items()
                      if any(k in note for k in kws)), "其他")
        clusters.setdefault(topic, []).append(
            {"thread_id": thread, "gate": gate, "note": note or None})
    out = [{"topic": tp, "n_text_comments": sum(1 for i in items if i["note"]),
            "items": items}
           for tp, items in sorted(clusters.items(), key=lambda kv: -len(kv[1]))]
    # 结案信封的 gate2_rejects 与 DB 幸存决策行对账：D10 多轮重发在失败线程上
    # 未走 archive（history 表为空），第二轮拒绝意见无落库件，如实单列。
    missing = []
    for tid, env in envelopes.items():
        total = env.get("gate2_rejects", 0) or 0
        gap = total - captured_per_thread.get(tid, 0)
        if gap > 0:
            missing.append({"thread_id": tid, "n_uncaptured_rejects": gap,
                            "evidence": "graph 事件序列 gate2_rejected×2 + "
                                        "runs/%s/FAILURE.json" % tid})
    if missing:
        out.append({"topic": "第二轮拒绝意见未落库（重发卡未走 archive，"
                             "无法判定是否按钮拒绝；不猜）",
                    "n_text_comments": 0, "items": [],
                    "evidence_gap": missing})
    return out


def _learnings_block() -> dict:
    from memory_flywheel.phase1.learning_store import LearningStore
    store = LearningStore()
    n_rows = store.count()
    iso = _load_json(PHASE1_ROOT / "task6_learnings" / "isolation_evidence.json")
    return {
        "table": "phase1_learnings", "rows_new": n_rows,
        "kb_chunks_baseline": iso["production_db"]["kb_chunks_rows"],
        "production_recall_hits": 0,
        "production_recall_note": "Phase 1 只写不召回：LearningStore.query 通道"
                                  "存在且在 Task6 tmp store 夹具验证可召回"
                                  "（recall_fix=t1,t2），runner 生产召回接线属"
                                  "Phase 2 backlog；禁止把夹具命中记成生产命中",
        "fixture_recall_evidence": {
            "file": "task6_learnings/isolation_evidence.json",
            "recall_fix": iso["tmp_store_smoke"]["recall_fix"],
            "recall_failure": iso["tmp_store_smoke"]["recall_failure"]},
    }


def _hitl_block(ledger: list[dict]) -> dict:
    """L580 HITL 真人点验演练：演练未完成时如实报 pending，不编结果。"""
    rp = PHASE1_ROOT / "task9_milestone" / "hitl_evidence" / "drill_result.json"
    n_calls = sum(1 for r in ledger if r.get("batch_id") == "T9-B1")
    if not rp.is_file():
        return {"status": "pending", "batch_id": "T9-B1",
                "approved": 4, "calls_recorded": n_calls,
                "note": "待真人 card_form 点验（test_hitl_drill.py，paid）"}
    r = _load_json(rp)
    agreements = [x["agreement_with_original"] for x in r["revalidations"]]
    return {
        "status": "passed" if r["human_decision"]["decision"] == "approve"
        else "rejected",
        "batch_id": "T9-B1", "calls_recorded": n_calls,
        "spotchecked_run": r["source_thread"],
        "drill_thread": r["drill_thread"],
        "rung4_revalidation_agreement": agreements,
        "rung4_reproducible": bool(agreements) and all(a == 6 for a in agreements),
        "human_decision": {"decision": r["human_decision"]["decision"],
                           "source": r["human_decision"]["source"],
                           "human_notes": r["human_decision"]["human_notes"]},
    }


def _q2_gate_block() -> dict:
    """Q2（D9 条件闸门）事后补的真人 Gate2：未点验时如实报 pending。

    Q2 不经 LangGraph 图，四任务中唯独它原无 card_form 签收；
    scripts/milestone1/q2_human_gate.py 发卡，0 付费，裁决原件落
    q2_carla/human_gate/decision.json（E8.2 递归收录）。
    """
    thread_id = "t8-q2carla-hitl-20261016"
    rp = RUNS_ROOT / "task8_track2" / "q2_carla" / "human_gate" / "decision.json"
    if not rp.is_file():
        return {"status": "pending", "thread_id": thread_id,
                "note": "D9 条件闸门原无 card_form 签收；q2_human_gate.py "
                        "发卡后待真人点验"}
    r = _load_json(rp)
    hd = r["human_decision"]
    return {
        "status": "passed" if hd["decision"] == "approve" else "rejected",
        "thread_id": r["thread_id"], "schema": r["schema"],
        "gate_path": r["gate_path"],
        "paid_llm_calls_delta": r["paid_llm_calls"]["delta"],
        "human_decision": {"decision": hd["decision"], "source": hd["source"],
                           "human_notes": hd["human_notes"],
                           "card_message_id": hd["card_message_id"],
                           "media_message_id": hd["media_message_id"]},
    }


def _paid_block(ledger: list[dict]) -> dict:
    by_stage = Counter(r["stage"] for r in ledger)
    by_task = Counter(r["task"] for r in ledger)
    return {
        "total": len(ledger), "ok": sum(1 for r in ledger if r["ok"]),
        "failed": sum(1 for r in ledger if not r["ok"]),
        "cap_normal": CAP_NORMAL, "cap_hard": CAP_HARD,
        "by_stage": dict(by_stage), "by_task": {str(k): v for k, v in by_task.items()},
        "normal_cap_used": len(ledger) <= CAP_NORMAL,
        "hard_cap_ok": len(ledger) <= CAP_HARD,
        "note": "失败行同样记账；36→48 区间每批需单独书面批准（D1 v1.1）",
    }


def main() -> int:
    gates = {(r["thread_id"], r["gate"]): r for r in _gates_rows()}
    t7_rows = _t7_run_rows(gates)
    t8 = _t8_row()
    ledger = [json.loads(l) for l in (
        PHASE1_ROOT / "quota" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()]
    dashboard = {
        "schema": SCHEMA,
        "generated_at_iso": subprocess.run(
            ["date", "+%Y-%m-%dT%H:%M:%S%z"], capture_output=True,
            text=True).stdout.strip(),
        "git_commit": _git_head(),
        "positioning": {
            "rsi_level_axis": {
                "level": 2,
                "label": "Level 2 知识/数据积累飞轮（尚未达到严格 RSI）",
                "basis": "kick-off：LangGraph 固定拓扑、system prompt、HITL 规则"
                         "仍由人类硬编码；系统积累外生数据，未修改产生数据的"
                         "机制本身（Agent_{t+1}=Agent_t(Agent_t) 不成立）",
                "next_level": "Level 3 参数级 RSI（元 Prompt 自演进，Phase 2）",
            },
            "track_axis": {
                "track1": "MuJoCo/ManiSkill 接触推块原生接触物理实证",
                "track2": "Genesis 结冰/油污 DR + Carla 固态 LiDAR 雨雪雾实证",
            },
        },
        "runs": t7_rows + [t8],
        "loop_distribution": _loop_distribution(t7_rows, t8, ledger),
        "reject_topic_clusters": _reject_clusters(
            list(gates.values()), _envelopes()),
        "learnings": _learnings_block(),
        "hitl_drill": _hitl_block(ledger),
        "q2_human_gate": _q2_gate_block(),
        "paid_calls": _paid_block(ledger),
    }
    out = PHASE1_ROOT / "task9_milestone" / "rsi_dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out),
                      "runs": len(dashboard["runs"]),
                      "alarms": sum(len(r.get("proxy_decoupling_alarms", []))
                                    for r in dashboard["runs"]),
                      "paid_total": len(ledger)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
