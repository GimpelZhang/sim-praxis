#!/usr/bin/env python3
"""T2 数值收敛 + 任务有效性报告（零模型调用，真实 MuJoCo）。

两件事分开取证：

1. **收敛**（AC14 的容差为什么是 20%/0.5）：把同一实例在三个步长上重跑，
   ``dt`` 减半时 ``A``/``B``（诊断族）或沉降时间（研究族）的变化必须收缩。
   只有收缩了，"冻结容差"才是测出来的而不是拍的。
2. **有效性**（AC15）：支持题（R-dev/R-v1，效应 ≥ 支持阈）与无效题
   （R-v2，效应 ≤ 等效界）必须真的可区分；D 族三种故障必须真的被辨出来，
   不允许任何一种塌成 "none"。

输出 ``protocol/t2_convergence.json``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.rsi_r0.contracts import sha256_of  # noqa: E402
from orchestrator.rsi_r0.faults import apply_fault  # noqa: E402
from orchestrator.rsi_r0.image_pin import IMAGE_REF  # noqa: E402
from orchestrator.rsi_r0.instances import plan_for  # noqa: E402
from orchestrator.rsi_r0.reference_analysis import (  # noqa: E402
    classify_fault,
    fit_slope,
    settling_time,
)
from orchestrator.rsi_r0.scenes import build_pendulum_scene, build_scene  # noqa: E402
from orchestrator.rsi_r0.trusted import ExperimentSession  # noqa: E402

SCHEMA = "rsi_r0.convergence.v1"
#: 固定观测窗：0.30 s。步长变、窗不变，否则"收敛"只是"看得更久"。
WINDOW_S = 0.30
#: 研究族的窗要盖住真实沉降（约 2.3 s），否则三个步长一律删失，"可区分"无从谈起。
RESEARCH_WINDOW_S = 3.0
STEP_SIZES = (0.002, 0.001, 0.0005)
PROBE_TARGET_X = 30.0


def _sess(scene_xml: str, *, kind: str, max_steps: int, host_fields=None) -> ExperimentSession:
    return ExperimentSession(kind=kind, image_digest=IMAGE_REF,
                             scene_builder=lambda _p, _x=scene_xml: _x,
                             max_steps=max_steps, host_fields=host_fields)


def _diagnose_at_dt(plan, dt: float) -> dict:
    params = {**plan.public["scene_params"], "timestep": dt}
    scene = apply_fault(build_scene(plan.template, params), plan.private["fault"])
    host = {}
    if plan.private["fault"]["kind"] == "force_scale":
        host["force_scale"] = float(plan.private["fault"]["scale"])
    steps = int(round(WINDOW_S / dt))
    sess = _sess(scene, kind="push_control", max_steps=max(steps, 3000),
                 host_fields=host)
    fits = {}
    for direction, forces in ((+1, plan.private["probe_forces"]["pos"]),
                              (-1, plan.private["probe_forces"]["neg"])):
        acc = []
        for f in forces:
            r = sess.run({"target_x": direction * PROBE_TARGET_X, "target_y": 0.0,
                          "kp": 200.0, "kd": 0.0, "max_force": abs(f), "steps": steps})
            acc.append(fit_slope(r["series"], dt)[0])
        (f1, a1), (f2, a2) = (abs(forces[0]), acc[0]), (abs(forces[1]), acc[1])
        slope = (a2 - a1) / (f2 - f1)
        fits[direction] = (slope, slope * f1 - a1)
    got = classify_fault(fits[+1][0], fits[+1][1], fits[-1][0], fits[-1][1],
                         plan.private["nominal"], plan.private["classify"])
    truth = plan.private["fault"]
    return {
        "dt": dt, "steps": steps,
        "A_pos": fits[+1][0], "B_pos": fits[+1][1],
        "A_neg": fits[-1][0], "B_neg": fits[-1][1],
        "kind": got["kind"], "scale": got["scale"],
        "kind_ok": got["kind"] == truth["kind"],
        "scale_rel_error": abs(got["scale"] - truth["scale"]) / abs(truth["scale"]),
    }


def _research_at_dt(plan, dt: float) -> dict:
    params = {**plan.public["scene_params"], "timestep": dt}
    steps = int(round(RESEARCH_WINDOW_S / dt))
    rows = []
    for level in plan.private["levels"]:
        scene = build_pendulum_scene({**params, "damping": float(level)})
        sess = _sess(scene, kind="free_run", max_steps=max(steps, 4000))
        for angle in plan.private["initial_angles"]:
            r = sess.run({"initial_angle": float(angle), "steps": steps})
            settle = settling_time(r, dt)
            rows.append({"damping": float(level), "angle": float(angle),
                         "settle_s": settle["settle_s"],
                         "censored": settle["censored"]})
    effects = []
    for angle in plan.private["initial_angles"]:
        lo = next(r for r in rows if r["angle"] == angle
                  and r["damping"] == plan.private["levels"][0])
        hi = next(r for r in rows if r["angle"] == angle
                  and r["damping"] == plan.private["levels"][1])
        effects.append(None if (lo["censored"] or hi["censored"])
                       else (lo["settle_s"] - hi["settle_s"]) / lo["settle_s"])
    return {"dt": dt, "steps": steps, "rows": rows, "effects": effects}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None)
    ap.add_argument("--skip-diagnosis", action="store_true",
                    help="跳过 D 族重跑（复用上次结果，只补 R 族）")
    args = ap.parse_args()

    from orchestrator.common.fsutil import ensure_private_dir
    from orchestrator.common.runtime_paths import rsi_r0_protocol_dir

    report: dict = {"schema": SCHEMA, "image_ref": IMAGE_REF,
                    "window_s": WINDOW_S, "step_sizes": list(STEP_SIZES),
                    "diagnosis": {}, "research": {}}
    prior_diagnosis: dict = {}
    out_path = Path(args.out) if args.out else rsi_r0_protocol_dir() / "t2_convergence.json"
    if args.skip_diagnosis:
        if not out_path.exists():
            raise SystemExit("--skip-diagnosis 需要上次的 t2_convergence.json")
        prior_diagnosis = json.loads(out_path.read_text(encoding="utf-8"))["diagnosis"]
    report["diagnosis"] = prior_diagnosis

    for instance_id in ("D-dev", "D-v1", "D-v2"):
        if args.skip_diagnosis:
            continue
        plan = plan_for(instance_id)
        runs = [_diagnose_at_dt(plan, dt) for dt in STEP_SIZES]
        finest = runs[-1]
        converged = all(r["kind_ok"] for r in runs) and all(
            r["scale_rel_error"] <= plan.private["tolerance"]["scale_rel"] for r in runs)
        report["diagnosis"][instance_id] = {
            "truth": plan.private["fault"], "runs": runs,
            "frozen_dt": float(plan.public["scene_params"]["timestep"]),
            "tolerance_scale_rel": plan.private["tolerance"]["scale_rel"],
            "kind_stable_across_dt": len({r["kind"] for r in runs}) == 1,
            "converged": converged, "finest": finest,
        }
        print(json.dumps({"instance": instance_id,
                          "kinds": [r["kind"] for r in runs],
                          "err": [round(r["scale_rel_error"], 4) for r in runs],
                          "converged": converged}, ensure_ascii=False), flush=True)

    for instance_id in ("R-dev", "R-v1", "R-v2"):
        plan = plan_for(instance_id)
        runs = [_research_at_dt(plan, dt) for dt in STEP_SIZES]
        report["research"][instance_id] = {
            "expected": plan.private["expected"], "runs": runs,
            "support_threshold": float(plan.private.get("support_threshold", 0.15)),
            "equivalence_bound": float(plan.private["equivalence_bound"]),
        }
        print(json.dumps({"instance": instance_id,
                          "effects": [r["effects"] for r in runs]},
                         ensure_ascii=False), flush=True)

    # --- AC15：支持题与无效题必须可区分 --------------------------------
    r_sup, r_equiv = report["research"]["R-v1"], report["research"]["R-v2"]
    sup_eff = [abs(e) for e in r_sup["runs"][-1]["effects"] if e is not None]
    eq_eff = [abs(e) for e in r_equiv["runs"][-1]["effects"] if e is not None]
    validity = {
        "supported_effect_min": min(sup_eff) if sup_eff else None,
        "equivalent_effect_max": max(eq_eff) if eq_eff else None,
        "support_threshold": r_sup["support_threshold"],
        "equivalence_bound": r_equiv["equivalence_bound"],
        "distinguishable": bool(sup_eff and eq_eff
                                and min(sup_eff) > r_sup["support_threshold"]
                                and max(eq_eff) <= r_equiv["equivalence_bound"]),
        "d_kinds": {k: v["finest"]["kind"] for k, v in report["diagnosis"].items()},
        "d_kinds_distinct": len({v["finest"]["kind"]
                                 for v in report["diagnosis"].values()}) == 3,
        "no_d_kind_collapsed_to_none": all(v["finest"]["kind"] != "none"
                                           for v in report["diagnosis"].values()),
    }
    validity["passed"] = bool(validity["distinguishable"]
                              and validity["d_kinds_distinct"]
                              and validity["no_d_kind_collapsed_to_none"]
                              and all(v["converged"] for v in report["diagnosis"].values()))
    report["task_validity"] = validity
    report["passed"] = validity["passed"]
    report["report_sha256"] = sha256_of({k: v for k, v in report.items()
                                         if k != "report_sha256"})

    protocol = ensure_private_dir(rsi_r0_protocol_dir())
    out = Path(args.out) if args.out else protocol / "t2_convergence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(json.dumps({"ok": report["passed"], "out": str(out),
                      "validity": validity}, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
