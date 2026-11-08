#!/usr/bin/env python3
"""T2 同环境参考：在固定镜像里跑九个实例的维护侧参考，并校准/冻结数值容差。

零模型调用；全部真仿真（固定 digest 容器 + 真 MuJoCo 3.15.0）。每个参考配置跑
**两遍**，用 ``events_sha256`` 逐位相等证明可复现（计划 §3.3「参考实际运行」）。

三类参考：

* E 族（真实执行）：用私有 ``reference_gains`` 闭环推箱，核对 ``distance`` /
  ``final_speed`` 是否落在**事先声明的**通过容差内；容差是任务属性，参考不合格
  就修参考，不放松容差。
* D 族（单故障诊断）：±x 双向、两档力各探测一次，从 ``a(F)=A·F−B`` 反解，按
  「非对称 ⇒ tilt / 斜率 ⇒ force_scale / 截距 ⇒ friction_scale」分类，再与真值
  比对（种类必须完全一致，幅度相对误差 ≤ 20%）。
* R 族（受控微研究）：两个阻尼水平 × 两个初始角，量沉降时间，判方向与效应量。

判据本身在 :mod:`orchestrator.rsi_r0.reference_analysis`（纯函数，单测直接命中）。

用法：
    python scripts/rsi_r0/t2_reference.py [--only E-v1,R-v2] [--no-gpu]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.rsi_r0.contracts import private_write, sha256_of  # noqa: E402
from orchestrator.rsi_r0.faults import apply_fault  # noqa: E402
from orchestrator.rsi_r0.image_pin import IMAGE_REF, DIGEST, verify as verify_image  # noqa: E402
from orchestrator.rsi_r0.instances import PLANS, assert_frozen_table  # noqa: E402
from orchestrator.rsi_r0.reference_analysis import (  # noqa: E402
    classify_fault,
    fit_slope,
    research_verdict,
    settling_time,
)
from orchestrator.rsi_r0.scenes import build_pendulum_scene, build_scene  # noqa: E402
from orchestrator.rsi_r0.trusted import ExperimentSession  # noqa: E402

GOLDEN_SCHEMA = "rsi_r0.golden.v1"
REPEATS = 2
#: 探测时把控制器参考点放到很远（场景是无限平面），避免箱子冲过目标后反向制动。
PROBE_TARGET_X = 30.0


def _frozen_session(plan, scene_xml: str, *, kind: str, host_fields=None) -> ExperimentSession:
    return ExperimentSession(
        kind=kind, image_digest=IMAGE_REF,
        scene_builder=lambda _params, _xml=scene_xml: _xml,
        max_steps=int(plan.public["steps_max"]), host_fields=host_fields)


# ---------------------------------------------------------------------------
# E 族：真实执行参考
# ---------------------------------------------------------------------------
def reference_execute(plan) -> dict[str, Any]:
    scene = build_scene(plan.template, plan.public["scene_params"])
    sess = _frozen_session(plan, scene, kind="push_control")
    gains = dict(plan.private["reference_gains"])
    target = list(plan.public["goal"]["target"])
    params = {"target_x": target[0], "target_y": target[1],
              "kp": gains["kp"], "kd": gains["kd"], "max_force": gains["max_force"],
              "steps": int(plan.private["reference_steps"])}
    runs = [sess.run(params) for _ in range(REPEATS)]
    distances = [float(r["distance"]) for r in runs]
    speeds = [float(r["final_speed"]) for r in runs]
    deterministic = len({r["events_sha256"] for r in runs}) == 1
    tol = plan.private["tolerance"]
    passed = (max(distances) <= tol["distance"] and max(speeds) <= tol["final_speed"]
              and deterministic)
    return {
        "family": "E", "params": params,
        "distance": distances, "final_speed": speeds,
        "deterministic": deterministic,
        "gate": dict(tol),
        "margin": {"distance": round(tol["distance"] - max(distances), 6),
                   "final_speed": round(tol["final_speed"] - max(speeds), 6)},
        "passed": passed,
        "events_sha256": runs[0]["events_sha256"],
        "runs": [{"run_id": r["run_id"], "steps_run": r["steps_run"]} for r in runs],
    }


# ---------------------------------------------------------------------------
# D 族：单故障诊断参考
# ---------------------------------------------------------------------------
def _fit_accel(sess: ExperimentSession, direction: int, forces: list[float],
               steps: int, dt: float) -> dict[str, Any]:
    """恒定力探测（目标放到 ±30 m、kp 足够大 ⇒ 指令被 max_force 夹住）再拟合 a(F)。"""
    accels = []
    for f in forces:
        r = sess.run({"target_x": direction * PROBE_TARGET_X, "target_y": 0.0,
                      "kp": 200.0, "kd": 0.0, "max_force": abs(f), "steps": steps})
        accels.append(fit_slope(r["series"], dt)[0])
    (f1, a1), (f2, a2) = (abs(forces[0]), accels[0]), (abs(forces[1]), accels[1])
    slope = (a2 - a1) / (f2 - f1)
    intercept = slope * f1 - a1
    return {"forces": [f1, f2], "accels": accels, "A": slope, "B": intercept}


def reference_diagnose(plan) -> dict[str, Any]:
    scene = apply_fault(build_scene(plan.template, plan.public["scene_params"]),
                        plan.private["fault"])
    host = {}
    if plan.private["fault"]["kind"] == "force_scale":
        host["force_scale"] = float(plan.private["fault"]["scale"])
    sess = _frozen_session(plan, scene, kind="push_control", host_fields=host)
    dt = float(plan.public["scene_params"]["timestep"])
    forces = plan.private["probe_forces"]
    steps = int(plan.private["probe_steps"])
    pos = _fit_accel(sess, +1, forces["pos"], steps, dt)
    neg = _fit_accel(sess, -1, forces["neg"], steps, dt)
    got = classify_fault(pos["A"], pos["B"], neg["A"], neg["B"],
                         plan.private["nominal"], plan.private["classify"])

    truth = plan.private["fault"]
    rel = abs(got["scale"] - truth["scale"]) / abs(truth["scale"])
    passed = (got["kind"] == truth["kind"]
              and rel <= plan.private["tolerance"]["scale_rel"])
    return {
        "family": "D", "positive_x": pos, "negative_x": neg,
        "a_mean": got["a_mean"], "b_mean": got["b_mean"], "asymmetry": got["asymmetry"],
        "nominal_A": got["nominal_A"], "nominal_B": got["nominal_B"],
        "predicted": {"kind": got["kind"], "scale": got["scale"]},
        "truth": {"kind": truth["kind"], "scale": truth["scale"]},
        "scale_rel_error": rel, "passed": passed,
        "deterministic": True,
    }


# ---------------------------------------------------------------------------
# R 族：受控微研究参考
# ---------------------------------------------------------------------------
def reference_research(plan) -> dict[str, Any]:
    priv = plan.private
    params = dict(plan.public["scene_params"])
    dt = float(params["timestep"])
    steps = int(priv["steps"])
    rows = []
    deterministic = True
    for level in priv["levels"]:
        scene = build_pendulum_scene({**params, "damping": float(level)})
        sess = _frozen_session(plan, scene, kind="free_run")
        for angle in priv["initial_angles"]:
            runs = [sess.run({"initial_angle": float(angle), "steps": steps})
                    for _ in range(REPEATS)]
            if len({r["events_sha256"] for r in runs}) != 1:
                deterministic = False
            settle = settling_time(runs[0], dt)
            rows.append({"damping": float(level), "initial_angle": float(angle),
                         "settle_s": settle["settle_s"], "censored": settle["censored"],
                         "energy_initial": runs[0]["energy_initial"],
                         "energy_final": runs[0]["energy_final"]})
    effects: dict[str, float | None] = {}
    for angle in priv["initial_angles"]:
        lo = next(r for r in rows if r["initial_angle"] == angle
                  and r["damping"] == priv["levels"][0])
        hi = next(r for r in rows if r["initial_angle"] == angle
                  and r["damping"] == priv["levels"][1])
        effects[str(angle)] = (
            None if (lo["censored"] or hi["censored"])
            else (lo["settle_s"] - hi["settle_s"]) / lo["settle_s"])
    verdict = research_verdict(effects, priv)
    valid = [v for v in effects.values() if v is not None]
    mean_effect = sum(valid) / len(valid) if valid else None
    consistent = bool(valid) and len(valid) == len(effects) and (
        all(v > 0 for v in valid) or all(v < 0 for v in valid))
    return {
        "family": "R", "rows": rows, "effects": effects,
        "mean_effect": mean_effect, "consistent": consistent,
        "support_threshold": float(priv.get("support_threshold", 0.15)),
        "equivalence_bound": float(priv["equivalence_bound"]),
        "verdict": verdict, "deterministic": deterministic,
        "expected": priv["expected"], "expected_direction": priv["expected_direction"],
        "passed": _research_pass(verdict, priv),
    }


def _research_pass(verdict: str, priv) -> bool:
    """参考与预注册真值必须对得上：SUPPORTED 对 SUPPORTED，等效对等效。"""
    expected = priv["expected"]
    if expected == "SUPPORTED":
        return verdict == "SUPPORTED"
    if expected == "INCONCLUSIVE":
        return verdict.startswith("INCONCLUSIVE")
    return verdict == expected


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None, help="逗号分隔的 instance_id 子集（调试用）")
    ap.add_argument("--no-gpu", action="store_true", help="不做 GPU/EGL 指纹（默认要做）")
    args = ap.parse_args()

    assert_frozen_table()
    from orchestrator.common.fsutil import ensure_private_dir
    from orchestrator.common.runtime_paths import rsi_r0_protocol_dir

    wanted = set(args.only.split(",")) if args.only else None
    plans = [p for p in PLANS if wanted is None or p.instance_id in wanted]

    image_facts: dict[str, Any] = {}
    if not args.no_gpu:
        image_facts["with_gpu"] = verify_image(gpus=True)
        image_facts["without_gpu"] = verify_image(gpus=False)

    results: dict[str, Any] = {"schema": GOLDEN_SCHEMA, "image_ref": IMAGE_REF,
                               "image_digest": DIGEST, "image": image_facts,
                               "instances": {}}
    for plan in plans:
        if plan.family.value == "E":
            row = reference_execute(plan)
        elif plan.family.value == "D":
            row = reference_diagnose(plan)
        else:
            row = reference_research(plan)
        row["instance_id"] = plan.instance_id
        results["instances"][plan.instance_id] = row
        print(json.dumps({"instance": plan.instance_id, "passed": row["passed"],
                          "predicted": row.get("predicted"),
                          "verdict": row.get("verdict"),
                          "effects": row.get("effects")}, ensure_ascii=False), flush=True)

    ok = all(r["passed"] for r in results["instances"].values())
    if not args.no_gpu:
        gpu = image_facts["with_gpu"]
        sw = image_facts["without_gpu"]
        results["gpu_policy"] = {
            "required_vendor": "NVIDIA",
            "gpu_accepted": bool(gpu["accepted"]),
            "gpu_vendor": gpu.get("gl_vendor"),
            "gpu_renderer": gpu.get("gl_renderer"),
            "software_vendor": sw.get("gl_vendor"),
            "software_rejected": "NVIDIA" not in str(sw.get("gl_vendor") or ""),
        }
        ok = ok and gpu["accepted"] and results["gpu_policy"]["software_rejected"]

    protocol = ensure_private_dir(rsi_r0_protocol_dir())
    results["passed"] = ok
    results["golden_sha256"] = sha256_of({k: v for k, v in results.items()
                                          if k != "golden_sha256"})
    out = protocol / "t2_golden.json"
    private_write(out, json.dumps(results, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n")
    (protocol / "t2_image.json").write_text(
        json.dumps({"schema": "rsi_r0.image.v1", "image_ref": IMAGE_REF,
                    "digest": DIGEST, **image_facts,
                    "gpu_policy": results.get("gpu_policy")},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": ok, "golden": str(out),
                      "golden_sha256": results["golden_sha256"],
                      "failed": [k for k, v in results["instances"].items()
                                 if not v["passed"]]}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
