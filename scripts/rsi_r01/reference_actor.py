#!/usr/bin/env python3
"""RSI-R0.1 同路径参考 actor：九实例各一条完整 reference 路径（AC16/AC17）。

三条纪律（计划 §5.2）：

1. **同路径**：动作以**公开 JSON 文本**发出，经 ``actions.parse_action`` 解析，
   再由 ``TrialRunner`` 分派到与候选完全相同的容器/文件/schema/submit/判据；
   actor 不构造 judge 输入，也不 import 私有答案（任务私有表只在宿主侧构造
   场景与故障）。
2. **真实分析执行**：``analyze`` 提交的 Python 源码由本 actor 按公开任务生成，
   在受限容器里真跑一次并产出 ``analysis.json``；submit 引用它的 analysis_id。
3. **独立复算**：R 族的判定再走 ``independent_oracle``（不同算法），差异必须在
   1e-12 相对界内（AC17）。

产物：``<campaign>/analysis/reference_actor/<instance>.json``，含动作事件、
容器证据（run_id/manifest/shape）、judge 判决、oracle 差异与汇总。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r01 import actions as AC
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC
from orchestrator.rsi_r01.trial import TrialRunner

sys.path.insert(0, str(_REPO_ROOT / "scripts" / "rsi_r01"))
import independent_oracle as ORACLE  # noqa: E402  （不同算法，只共享常量）

#: 分析源码（R）：从 /data 的公开副本复算沉降/效应/判决，写 /out/analysis.json。
_R_SOURCE = '''\
import glob, json
BAND_T, BAND_W, H = {band_t!r}, {band_w!r}, {h!r}
docs = []
for path in sorted(glob.glob("/data/run_*.json")):
    with open(path, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))

def settle(doc):
    th, om, t = doc["theta"], doc["omega"], doc["t"]
    n = len(th)
    if n == 0 or len(om) != n or len(t) != n:
        return None, "shape"
    last_out = -1
    for i in range(n - 1, -1, -1):
        if not (abs(th[i]) < BAND_T and abs(om[i]) < BAND_W):
            last_out = i
            break
    if last_out == n - 1:
        return None, "censored"
    return float(t[last_out + 1]), None

cells = {{}}
for doc in docs:
    p = doc["params"]
    key = (float(p["damping"]), float(p["initial_angle"]))
    value, why = settle(doc)
    cells[str(key)] = {{"t": value, "why": why, "run_id": doc["run_id"]}}

dampings = sorted({{k[0] for k in cells}})
angles = sorted({{k[1] for k in cells}})
effects = {{}}
for angle in angles:
    t_low = cells.get(str((dampings[0], angle)), {{}}).get("t") if dampings else None
    t_high = cells.get(str((dampings[-1], angle)), {{}}).get("t") if dampings else None
    if t_low is None or t_high is None or t_low == 0.0:
        effects[str(angle)] = None
    else:
        effects[str(angle)] = (t_low - t_high) / t_low
vals = [v for v in effects.values()]
mean = None if not vals or any(v is None for v in vals) else sum(vals) / len(vals)
z = None if mean is None else H * mean
claim = "INCONCLUSIVE"
if z is not None and z >= {delta!r}:
    claim = "SUPPORTED"
elif z is not None and z <= -{delta!r}:
    claim = "REFUTED"
out = {{"kind": "r_analysis", "cells": cells, "effects": effects,
       "mean_effect": mean, "z": z, "claim_status": claim}}
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, sort_keys=True)
print("analysis rows:", len(docs))
'''

#: 分析源码（E/D）：从公开副本复算末位置/末速度。
_PUSH_SOURCE = '''\
import glob, json
docs = []
for path in sorted(glob.glob("/data/run_*.json")):
    with open(path, "r", encoding="utf-8") as fh:
        docs.append(json.load(fh))
rows = []
for doc in docs:
    p = doc["params"]
    x, y = doc["x"][-1], doc["y"][-1]
    vx, vy = doc["vx"][-1], doc["vy"][-1]
    dx, dy = x - p["target_x"], y - p["target_y"]
    rows.append({{"run_id": doc["run_id"], "distance": (dx * dx + dy * dy) ** 0.5,
                 "final_speed": (vx * vx + vy * vy) ** 0.5}})
best = min(rows, key=lambda r: r["distance"]) if rows else None
out = {{"kind": "push_analysis", "runs": rows, "best": best}}
with open("/out/analysis.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, sort_keys=True)
print("analysis rows:", len(rows))
'''


def prereg_plan(inst: TC.Instance) -> dict[str, Any]:
    """公开登记件：问题、h、计划格、指标、公式版本、δ、删失规则、流程、停止规则。"""
    if inst.family == "R":
        h = int(inst.public["research"]["h"])
        cells = [{"damping": float(c["damping"]), "theta0": float(c["theta0"])}
                 for c in inst.r_cells]
        return {
            "question": inst.public["research"]["question"], "h": h, "cells": cells,
            "metric": "settling_time_s",
            "formula_version": "rsi_r01.effect.v1",
            "delta": S.DELTA,
            "censoring_rule": "末样本不在带内即删失；任一必要时间删失 ⇒ mean_effect=null",
            "analysis_procedure": ("读四份公开观测 → 逐份求沉降 → 两角度 e_a → 算术平均 → z=h·mean"),
            "stop_rule": "四格齐全后提交一次结论；不重复同格、不改登记",
        }
    gains = inst.private["reference_gains"]
    return {
        "question": inst.public["task"],
        "h": 1,
        "cells": [{"kp": float(gains["kp"]), "kd": float(gains["kd"]),
                   "max_force": float(gains["max_force"])}],
        "metric": "final_distance_m",
        "formula_version": "rsi_r01.push.v1",
        "delta": S.DELTA,
        "censoring_rule": "不适用（工程族以末次可信 run 判定）",
        "analysis_procedure": "读公开观测 → 末点距离/速度 → 与容差比较",
        "stop_rule": "命中容差即停；不重复跑后择优提交",
    }


def emit(runner: TrialRunner, obj: Mapping[str, Any]) -> dict[str, Any]:
    """把动作当**公开 JSON 文本**发出（与模型同一条解析路径）。"""
    return runner.dispatch(AC.parse_action(json.dumps(obj), runner.surface))


def _run_action(inst: TC.Instance, params: Mapping[str, float]) -> dict[str, Any]:
    body: dict[str, Any] = {"damping": float(params["damping"]),
                            "initial_angle": float(params["initial_angle"]),
                            "steps": 2000} if inst.family == "R" else {
        "kp": float(params["kp"]), "kd": float(params["kd"]),
        "max_force": float(params["max_force"]),
        "steps": int(inst.private["reference_steps"])}
    return {"schema": S.SCHEMA_ACTION, "action": "run_experiment", "params": body}


def gain_ladder(inst: TC.Instance) -> list[dict[str, float]]:
    """公开参数域内的确定性搜索阶梯（不是私有答案；D 族靠它修复故障）。"""
    ref = inst.private["reference_gains"]
    boundary = inst.param_bounds
    ladder: list[dict[str, float]] = [dict(ref)]
    for factor in (2.0, 4.0, 8.0, 16.0):
        ladder.append({"kp": min(boundary["kp"][1], ref["kp"] * factor),
                       "kd": min(boundary["kd"][1], ref["kd"] * (factor ** 0.5)),
                       "max_force": min(boundary["max_force"][1],
                                        ref["max_force"] * factor)})
    seen: list[dict[str, float]] = []
    for item in ladder:
        if item not in seen:
            seen.append(item)
    return seen


#: actor 自设的到达判据（严格于任何可能被私有采用的容差；只用于决定何时停止
#: 搜索并给出结论，不读取私有 tolerance/故障标签）。
SELF_TOL = {"distance": 0.01, "final_speed": 0.005}


def _self_passed(summary: Mapping[str, Any]) -> bool:
    return (float(summary["distance"]) <= SELF_TOL["distance"]
            and float(summary["final_speed"]) <= SELF_TOL["final_speed"])


def run_instance(inst: TC.Instance, *, root: Path, gpus: bool) -> dict[str, Any]:
    trial_id = f"ref_{inst.instance_id.replace('-', '_')}"
    runner = TrialRunner(campaign_root=root, instance_id=inst.instance_id,
                         trial_id=trial_id, gpus=gpus)
    record: dict[str, Any] = {"instance_id": inst.instance_id, "trial_id": trial_id,
                              "family": inst.family, "actions": []}
    plan = prereg_plan(inst)
    record["actions"].append(emit(runner, {"schema": S.SCHEMA_ACTION,
                                           "action": "preregister", "plan": plan}))
    run_ids: list[str] = []
    if inst.family == "R":
        for cell in inst.r_cells:
            ev = emit(runner, _run_action(inst, {"damping": cell["damping"],
                                                 "initial_angle": cell["theta0"]}))
            record["actions"].append(ev)
            if ev.get("ok"):
                run_ids.append(ev["run_id"])
    else:
        for gains in gain_ladder(inst):
            ev = emit(runner, _run_action(inst, gains))
            record["actions"].append(ev)
            if not ev.get("ok"):
                continue
            run_ids.append(ev["run_id"])
            if _self_passed(ev["summary"]):
                break

    if inst.family == "R":
        source = _R_SOURCE.format(band_t=S.THETA_BAND, band_w=S.OMEGA_BAND,
                                  h=int(inst.public["research"]["h"]),
                                  delta=S.DELTA)
    else:
        source = _PUSH_SOURCE
    ev = emit(runner, {"schema": S.SCHEMA_ACTION, "action": "analyze",
                       "source": source, "run_ids": run_ids})
    record["actions"].append(ev)
    analysis_ids = ([ev["analysis"]["analysis_id"]]
                    if ev.get("ok") and ev.get("analysis") else [])

    # 结论由 actor 自己的分析产物给出（不是从 judge 反推）。
    claim = S.ClaimStatus.INCONCLUSIVE
    actor_analysis: dict[str, Any] | None = None
    if analysis_ids:
        actor_analysis = runner.analyses.read_output(analysis_ids[0])
    if inst.family == "R":
        if isinstance(actor_analysis, Mapping):
            claim = str(actor_analysis.get("claim_status") or claim)
    else:
        best = (actor_analysis or {}).get("best") or {}
        if best and float(best.get("distance", 1e9)) <= SELF_TOL["distance"] and \
                float(best.get("final_speed", 1e9)) <= SELF_TOL["final_speed"]:
            claim = S.ClaimStatus.SUPPORTED
    submit = {"schema": S.SCHEMA_ACTION, "action": "submit", "claim": claim,
              "run_ids": run_ids, "analysis_ids": analysis_ids}
    record["actions"].append(emit(runner, submit))

    judge = runner.judge()
    record["judge"] = judge
    record["claim_submitted"] = claim
    record["summary"] = runner.summary()
    if inst.family == "R":
        observations = {}
        for rec in runner.runs:
            if rec.cell is None:
                continue
            observations[rec.cell] = runner.store.load_trusted(
                rec.run_id, rec.manifest, expected_trial=trial_id,
                expected_instance=inst.instance_id)
        oracle = ORACLE.judge_from_observations(
            observations, int(inst.public["research"]["h"]))
        problems = ORACLE.compare(judge["verdict"], oracle)
        record["oracle"] = {"result": oracle, "diff": problems,
                            "agree": not problems}
    record["ok"] = bool(judge.get("pass")) and all(
        a.get("ok") for a in record["actions"])
    return record


def run_all(*, root: Path, gpus: bool = False) -> dict[str, Any]:
    out_dir = Path(root) / "analysis" / "reference_actor"
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for inst in TC.INSTANCES:
        rec = run_instance(inst, root=root, gpus=gpus)
        path = out_dir / f"{inst.instance_id}.json"
        S.write_canonical_json(path, rec)
        records.append({"instance_id": inst.instance_id, "ok": rec["ok"],
                        "family": inst.family,
                        "claim": (rec["judge"].get("verdict") or {}).get("claim_status"),
                        "oracle_agree": (rec.get("oracle") or {}).get("agree")})
    summary = {"schema": "simpraxis.rsi_r01.reference_actor.v1",
               "instances": records,
               "passed": sum(1 for r in records if r["ok"]),
               "total": len(records),
               "all_ok": all(r["ok"] for r in records),
               "oracle_all_agree": all(r["oracle_agree"] is not False
                                       for r in records)}
    S.write_canonical_json(out_dir / "summary.json", summary)
    return summary


def campaign_root() -> Path:
    root = RP.rsi_r01_campaign_root()
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T3 同路径参考 actor")
    ap.add_argument("--gpus", action="store_true")
    ap.add_argument("--instance", default=None)
    args = ap.parse_args(argv)
    root = campaign_root()
    if args.instance:
        inst = TC.get(args.instance)
        rec = run_instance(inst, root=root, gpus=args.gpus)
        print(json.dumps({"instance": rec["instance_id"], "ok": rec["ok"],
                          "judge": rec["judge"].get("verdict", {}).get("status")
                          if isinstance(rec["judge"].get("verdict"), dict) else None,
                          "oracle_agree": (rec.get("oracle") or {}).get("agree")},
                         ensure_ascii=False))
        return 0 if rec["ok"] else 3
    summary = run_all(root=root, gpus=args.gpus)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["all_ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
