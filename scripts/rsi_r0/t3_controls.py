#!/usr/bin/env python3
"""T3 控制矩阵：18 正控（九实例 × 2 次真实重放）、21 负控、6 项 mutation。

零模型调用。正控**全部走固定 digest 容器**：九实例的维护侧参考先真跑一遍拿到
宿主事件，再各重放两次，两次都必须 (a) ``events_sha256`` 与原始事件逐位相同、
(b) 在评测器的每一层都 PASS。负控与 mutation 是纯函数（合成基线），不启容器。

判据（评测器口径 + 容差 + 负控清单 + mutation 清单）在开跑前算成
``criteria_hash`` 并冻结进 ``protocol/t3_criteria.json``：之后任何改动都会让
本脚本直接拒绝运行——AC20「判据冻结」不是靠纪律，是靠哈希拒绝。

用法：
    python scripts/rsi_r0/t3_controls.py [--only E-v1,R-v2] [--offline]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))
sys.path.insert(0, str(_HERE.parent))

from orchestrator.common.fsutil import ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import (  # noqa: E402
    rsi_r0_protocol_dir,
    rsi_r0_campaign_root,
)
from orchestrator.rsi_r0.contracts import (  # noqa: E402
    Layer,
    LayerStatus,
    RESEARCH_CRITERIA,
    private_write,
    sha256_of,
)
from orchestrator.rsi_r0.controls import (  # noqa: E402
    MUTANTS,
    NEGATIVE_CATEGORIES,
    SCHEMA,
    criteria_definition,
    criteria_hash,
    negative_cases,
    run_mutant,
    status_is_rejection,
)
from orchestrator.rsi_r0.evaluator import (  # noqa: E402
    EVALUATOR_VERSION,
    EvaluationInput,
    diagnose_from_runs,
    evaluate,
    research_from_runs,
    series_metrics,
)
from orchestrator.rsi_r0.faults import apply_fault
from orchestrator.rsi_r0.matrix import (  # noqa: E402
    CRITERIA_SCHEMA,
    freeze_criteria,
    permission_facts,
    run_negatives,
    run_mutations,
)  # noqa: E402
from orchestrator.rsi_r0.image_pin import DIGEST, IMAGE_REF  # noqa: E402
from orchestrator.rsi_r0.instances import PLANS, assert_frozen_table  # noqa: E402
from orchestrator.rsi_r0.scenes import build_pendulum_scene, build_scene  # noqa: E402
from orchestrator.rsi_r0 import service as _service  # noqa: E402
from orchestrator.rsi_r0.trusted import (  # noqa: E402
    CANDIDATE_PARAM_BOUNDS,
    HOST_ONLY_FIELDS,
    ExperimentSession,
    container_argv_template,
)
from t2_build import public_task  # noqa: E402  （同目录脚本：任务书是唯一的公开面）

REPORT_SCHEMA = "rsi_r0.controls_report.v1"
#: 每个实例的重放次数（计划 §4.2：18 次参考重放 = 9 × 2）。
REPLAYS = 2
#: 探测时把控制器参考点放到很远（场景是无限平面），避免箱子冲过目标后反向制动。
PROBE_TARGET_X = 30.0


# ---------------------------------------------------------------------------
# 宿主侧场景（候选永远看不到这一段）
# ---------------------------------------------------------------------------
def host_scene(plan, params: dict[str, Any]) -> str:
    """按**宿主**规则构造本题场景：D 族注入故障、R 族按候选自选阻尼改写。"""
    scene_params = dict(plan.public["scene_params"])
    if plan.family.value == "R":
        scene_params["damping"] = float(params["damping"])
        return build_pendulum_scene(scene_params)
    scene = build_scene(plan.template, scene_params)
    fault = plan.private.get("fault")
    return apply_fault(scene, fault) if fault else scene


def host_fields(plan) -> dict[str, float]:
    """宿主独占注入：只有 force_scale 由宿主写进请求（候选交不出来，见 trusted）。"""
    fault = plan.private.get("fault") or {}
    if fault.get("kind") == "force_scale":
        return {"force_scale": float(fault["scale"])}
    return {}


def run_once(plan, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """真跑一次：返回 (调用记录, 服务结果)。每次调用一个新的可信会话。"""
    scene = host_scene(plan, params)
    sess = ExperimentSession(
        kind=plan.public["interface"]["kind"], image_digest=IMAGE_REF,
        scene_builder=lambda _params, _xml=scene: _xml,
        max_steps=int(plan.public["steps_max"]), host_fields=host_fields(plan))
    result = sess.run(params)
    return sess.calls[-1], result


def _replay_series(plan, result: dict[str, Any]) -> dict[str, Any]:
    return {"events_sha256": result.get("events_sha256"),
            "series": result.get("series"),
            "angle_series": result.get("angle_series"),
            "energy_initial": result.get("energy_initial"),
            "energy_final": result.get("energy_final")}


# ---------------------------------------------------------------------------
# 维护侧参考交卷（诚实自报：数字全部由复算得到，不读私有真值）
# ---------------------------------------------------------------------------
def _reference_execute(plan) -> tuple[dict, list[dict], dict[str, dict]]:
    target = list(plan.public["goal"]["target"])
    gains = dict(plan.private["reference_gains"])
    params = {"target_x": target[0], "target_y": target[1],
              "kp": gains["kp"], "kd": gains["kd"], "max_force": gains["max_force"],
              "steps": int(plan.private["reference_steps"])}
    call, result = run_once(plan, params)
    claim = series_metrics(result["series"], target)
    answer = {"family": "E", "run_id": call["run_id"],
              "claim": {"distance": claim["distance"],
                        "final_speed": claim["final_speed"]}}
    return answer, [call], {call["run_id"]: result}


def _reference_diagnose(plan) -> tuple[dict, list[dict], dict[str, dict]]:
    dt = float(plan.public["scene_params"]["timestep"])
    steps = int(plan.private["probe_steps"])
    calls: list[dict] = []
    originals: dict[str, dict] = {}
    for direction, key in ((+1, "pos"), (-1, "neg")):
        for force in plan.private["probe_forces"][key]:
            params = {"target_x": direction * PROBE_TARGET_X, "target_y": 0.0,
                      "kp": 200.0, "kd": 0.0, "max_force": abs(float(force)),
                      "steps": steps}
            call, result = run_once(plan, params)
            calls.append(call)
            originals[call["run_id"]] = result
    runs = [{**c, "series": originals[c["run_id"]]["series"]} for c in calls]
    got = diagnose_from_runs(runs, nominal=plan.private["nominal"],
                             rules=plan.private["classify"], dt=dt)
    answer = {"family": "D", "run_ids": [c["run_id"] for c in calls],
              "hypothesis": {"kind": got["kind"], "scale": got["scale"]}}
    return answer, calls, originals


def _reference_research(plan) -> tuple[dict, list[dict], dict[str, dict]]:
    priv = plan.private
    dt = float(plan.public["scene_params"]["timestep"])
    steps = int(priv["steps"])
    calls: list[dict] = []
    originals: dict[str, dict] = {}
    for level in priv["levels"]:
        for angle in priv["initial_angles"]:
            params = {"damping": float(level), "initial_angle": float(angle),
                      "steps": steps}
            call, result = run_once(plan, params)
            calls.append(call)
            originals[call["run_id"]] = result
    runs = [{**c, "angle_series": originals[c["run_id"]]["angle_series"]}
            for c in calls]
    rules = {"equivalence_bound": float(priv["equivalence_bound"]),
             "support_threshold": float(priv.get("support_threshold", 0.15))}
    got = research_from_runs(runs, dt=dt, priv={**priv, **rules})
    effects = [v for v in got["effects"].values() if v is not None]
    span = "; ".join(
        f"阻尼 {lo}→{hi}" for lo, hi in [(priv["levels"][0], priv["levels"][1])])
    answer = {
        "family": "R", "run_ids": [c["run_id"] for c in calls],
        "verdict": got["verdict"],
        "effect": float(got["mean_effect"]) if got["mean_effect"] is not None else 0.0,
        "criteria": {c: "见 run 记录与复算" for c in RESEARCH_CRITERIA},
        "scope": f"单摆（{span}）在 {steps} 步、初角 "
                 f"{priv['initial_angles']} 的范围内；不含接触与外激励",
        # 事前诊断（C8）：必须写"如果结果落在等效界内，我怎么解释"
        "diagnostics": [
            f"两水平效应 {', '.join(f'{v:+.4f}' for v in effects)}",
            f"事前等效界 {rules['equivalence_bound']}、支持阈 "
            f"{rules['support_threshold']}",
            "若效应落在等效界内则如实报告'无有意义的差异'，不抬阈翻案",
        ],
    }
    return answer, calls, originals


_REFERENCE = {"E": _reference_execute, "D": _reference_diagnose,
              "R": _reference_research}


# ---------------------------------------------------------------------------
# 正控：参考交卷 + 两次真实重放，两层都要过
# ---------------------------------------------------------------------------
def _truth(plan) -> dict[str, Any]:
    if plan.family.value == "D":
        return {"fault": plan.private["fault"], "nominal": plan.private["nominal"],
                "classify": plan.private["classify"]}
    if plan.family.value == "R":
        return {"expected": plan.private["expected"]}
    return {}


def _priv_rules(plan) -> dict[str, Any]:
    if plan.family.value == "R":
        return {"equivalence_bound": float(plan.private["equivalence_bound"]),
                "support_threshold": float(plan.private.get("support_threshold", 0.15))}
    return {"tolerance": plan.private["tolerance"]}


def positive_control(plan, replays: int, prereg_sha256: str) -> dict[str, Any]:
    """一个实例的正控：参考跑一次 → 交卷 → 连做 ``replays`` 次真实重放 → 每层判 PASS。"""
    task = public_task(plan)
    answer, calls, originals = _REFERENCE[plan.family.value](plan)

    # 最后一次重放顺带记下每个 run 的事件哈希：不许为了核对再多跑一遍容器
    seen: dict[str, str] = {}

    def _replay(call: dict[str, Any]) -> dict[str, Any]:
        _rec, result = run_once(plan, dict(call["params"]))
        seen[str(call["run_id"])] = str(result.get("events_sha256"))
        return _replay_series(plan, result)

    def _scene_for(call: dict[str, Any]) -> str:
        return sha256_of(host_scene(plan, dict(call["params"])))

    rows: list[dict[str, Any]] = []
    for index in range(replays):
        inp = EvaluationInput(
            task=task, answer=answer, calls=calls, replay=_replay,
            truth=_truth(plan), priv_rules=_priv_rules(plan),
            prereg_sha256=prereg_sha256, scene_for=_scene_for,
            slot_id=f"{plan.instance_id}#pos{index}")
        seen.clear()
        res = evaluate(inp)
        # 每轮**各自**核一次：只看最后一轮会漏掉"前一轮不一致"的情况
        round_same = all(seen.get(str(c["run_id"])) == str(c["events_sha256"])
                         for c in calls)
        applicable = [L for L in Layer if res.layers[L].status is not LayerStatus.NOT_APPLICABLE]
        ok = all(res.layers[L].status is LayerStatus.PASS for L in applicable)
        rows.append({
            "replay_index": index, "execution": res.execution.value,
            "scientific": res.scientific.value, "passed": ok,
            "same_events": round_same,
            "layers": {L.value: {"status": res.layers[L].status.value,
                                 "code": res.layers[L].reason_code}
                       for L in Layer},
        })
    same_events = bool(rows) and all(r["same_events"] for r in rows)
    # 场景身份必须**真的**是本实例的哈希，而不是恰好自洽
    scene_ok = all(_scene_for(c) == str(c["scene_sha256"]) for c in calls)
    return {
        "instance_id": plan.instance_id, "family": plan.family.value,
        "role": plan.role, "answer": answer,
        "run_ids": [c["run_id"] for c in calls],
        "replays": rows, "same_events": same_events, "scene_identity_ok": scene_ok,
        "passed": bool(rows) and all(r["passed"] for r in rows) and same_events
                  and scene_ok,
    }


# ---------------------------------------------------------------------------
# 权限面（AC18 / AC19）：候选拿不到隐藏真值、评测器、密钥与宿主 shell
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 判据冻结（AC20）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------




def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None,
                    help="逗号分隔的 instance_id 子集（调试用；不写则九实例全跑）")
    ap.add_argument("--offline", action="store_true",
                    help="只跑负控与 mutation（不启容器）；正控标 skipped")
    ap.add_argument("--replays", type=int, default=REPLAYS)
    ap.add_argument("--refreeze", action="store_true",
                    help="T3 收盘前重算判据（旧哈希进 superseded 留痕）")
    args = ap.parse_args()

    assert_frozen_table()
    protocol = ensure_private_dir(rsi_r0_protocol_dir())
    _frozen, digest = freeze_criteria(protocol, PLANS,
                                      allow_refreeze=args.refreeze)
    campaign = rsi_r0_campaign_root()

    # C1 的事前登记哈希：冻结任务集（T2 在任何模型调用之前落的锁）
    lock_path = protocol / "t2_tasks_lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    prereg = str(lock["tasks_set_sha256"])

    wanted = set(args.only.split(",")) if args.only else None
    plans = [p for p in PLANS if wanted is None or p.instance_id in wanted]

    positives: dict[str, Any] = {"total": 0, "passed": 0, "items": [],
                                 "skipped": bool(args.offline)}
    if not args.offline:
        for plan in plans:
            row = positive_control(plan, args.replays, prereg)
            positives["items"].append(row)
            positives["total"] += 1
            positives["passed"] += int(row["passed"])
            print(json.dumps({"positive": plan.instance_id, "passed": row["passed"],
                              "same_events": row["same_events"],
                              "scene_identity_ok": row["scene_identity_ok"]},
                             ensure_ascii=False), flush=True)

    negatives = run_negatives()
    mutations = run_mutations()
    permissions = permission_facts(protocol)

    ok = (permissions["ok"]
          and negatives["passed"] == negatives["total"]
          and mutations["killed"] == mutations["total"]
          and (positives["skipped"]
               or positives["passed"] == positives["total"]))
    report = {
        "schema": REPORT_SCHEMA, "controls_schema": SCHEMA,
        "evaluator_version": EVALUATOR_VERSION,
        "image_ref": IMAGE_REF, "image_digest": DIGEST,
        "campaign": campaign.name, "prereg_sha256": prereg,
        "criteria_hash": digest,
        "positives": positives, "negatives": negatives, "mutations": mutations,
        "permissions": permissions,
        "budget": {"model_calls": 0, "paid": 0},
        "passed": ok,
    }
    report["report_sha256"] = sha256_of({k: v for k, v in report.items()
                                         if k != "report_sha256"})
    out = protocol / ("t3_controls_offline.json" if args.offline
                      else "t3_controls_report.json")
    private_write(out, json.dumps(report, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n")
    print(json.dumps({"ok": ok, "report": str(out),
                      "report_sha256": report["report_sha256"],
                      "positives": f"{positives['passed']}/{positives['total']}",
                      "negatives": f"{negatives['passed']}/{negatives['total']}",
                      "mutations": f"{mutations['killed']}/{mutations['total']}",
                      "permissions_ok": permissions["ok"],
                      "missed": negatives["missed"],
                      "survivors": mutations["survivors"]}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
