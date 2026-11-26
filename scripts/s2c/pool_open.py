#!/usr/bin/env python3
"""S2c Q 池 6 份领域简报（3 域 × 2）+ 宿主私有世界变体 + 可行性预检（AC-04）。

简报只含公开面（问题/竞争解释公开描述/因子轴/可选指标/约束/试点配额）；
世界变体数值只落宿主私有 files（不进 DomainBrief.public_document）。

用法：python scripts/s2c/pool_open.py [--verify]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.hk.design_space import DomainBrief

# 域 → (scene_id, metric, factor_axes)
_DOMAINS = {
    "contact_sliding_A": ("A_contact_friction", "displacement_x_m",
                          [{"name": "mu", "values": ["0.2", "0.6"], "scene_param": "sliding_friction"},
                           {"name": "mass", "values": ["0.3", "1.0"], "scene_param": "mass_kg"}]),
    "contact_sliding_B": ("A_contact_friction", "stop_time_s",
                          [{"name": "v0", "values": ["0.2", "0.5"], "scene_param": "v0_mps"},
                           {"name": "mu", "values": ["0.2", "0.6"], "scene_param": "sliding_friction"}]),
    "rebound_A": ("C_collision_rebound", "rebound_height_m",
                  [{"name": "damp", "values": ["0.3", "0.8"], "scene_param": "solref_damping_ratio"},
                   {"name": "v0", "values": ["1.5", "4.5"], "scene_param": "v0_mps"}]),
    "rebound_B": ("C_collision_rebound", "rebound_height_m",
                  [{"name": "mass", "values": ["0.46", "0.55"], "scene_param": "ball_mass_kg"},
                   {"name": "damp", "values": ["0.3", "0.8"], "scene_param": "solref_damping_ratio"}]),
    "forced_vib_A": ("D_forced_vibration", "steady_state_amplitude_rad",
                     [{"name": "freq", "values": ["1.0", "2.5"], "scene_param": "drive_freq_hz"},
                      {"name": "damp", "values": ["0.05", "0.4"], "scene_param": "joint_damping"}]),
    "forced_vib_B": ("D_forced_vibration", "steady_state_amplitude_rad",
                     [{"name": "freq", "values": ["0.6", "2.0"], "scene_param": "drive_freq_hz"},
                      {"name": "amp", "values": ["0.9", "1.1"], "scene_param": "drive_amplitude_nm"}]),
}


def build_briefs() -> dict:
    briefs = {}
    for bid, (scene, metric, axes) in _DOMAINS.items():
        # ≥2 竞争解释（公开描述，id 对应宿主私有变体）
        expl = ({"id": "e1", "desc": f"{axes[0]['name']} 主导效应"},
                {"id": "e2", "desc": f"{axes[1]['name']} 主导效应"})
        briefs[bid] = DomainBrief(
            brief_id=bid, question=f"{scene}：{axes[0]['name']}×{axes[1]['name']} 的交互及其主导机制？",
            competing_explanations=expl, factor_axes=tuple(axes),
            metrics=(metric,), scene_id=scene,
            constraints={"max_conditions": 6},
            pilot_quota={"max_iterations": 3, "runs_per_iteration": 16})
    return briefs


def world_variants() -> dict:
    """宿主私有世界变体（只进审查者通道）：每个简报 2 个"真实机制"设定。

    变体 = 让某因子成为真实主导者的物理设定；判别力检验 = 同一参考设计在两变体下
    指标模式是否可分（min 两两差 > δ）。
    """
    return {
        # contact_sliding：变体A μ主导、变体B mass 主导（通过 torsional/v0 nuisance 区分）
        "contact_sliding_A": {"v_mu_dominant": {"torsional_friction_m": "0.01"},
                             "v_mass_dominant": {"torsional_friction_m": "0.05"}},
        "contact_sliding_B": {"v_v0_dominant": {"torsional_friction_m": "0.01"},
                             "v_mu_dominant": {"torsional_friction_m": "0.05"}},
        "rebound_A": {"v_damp_dominant": {"solimp_damping": "0.9"},
                     "v_v0_dominant": {"solimp_damping": "0.95"}},
        "rebound_B": {"v_mass_dominant": {"solimp_damping": "0.9"},
                     "v_damp_dominant": {"solimp_damping": "0.95"}},
        "forced_vib_A": {"v_freq_dominant": {"drive_phase_rad": "0.0"},
                        "v_damp_dominant": {"drive_phase_rad": "0.03"}},
        "forced_vib_B": {"v_freq_dominant": {"drive_phase_rad": "0.0"},
                        "v_amp_dominant": {"drive_phase_rad": "0.03"}},
    }


def reference_design(brief: DomainBrief) -> dict:
    """宿主参考设计：因子轴 2×2 全因子（A1/A2 × B1/B2）。"""
    ax = brief.factor_axes
    a_lo, a_hi = ax[0]["values"]
    b_lo, b_hi = ax[1]["values"]
    conds = [
        {"id": "c_hi_hi", "factors": {ax[0]["name"]: a_hi, ax[1]["name"]: b_hi}},
        {"id": "c_hi_lo", "factors": {ax[0]["name"]: a_hi, ax[1]["name"]: b_lo}},
        {"id": "c_lo_hi", "factors": {ax[0]["name"]: a_lo, ax[1]["name"]: b_hi}},
        {"id": "c_lo_lo", "factors": {ax[0]["name"]: a_lo, ax[1]["name"]: b_lo}},
    ]
    return {"conditions": conds, "metric": brief.metrics[0],
            "primary_contrast": {"id": "a_x_b", "treatment_high": "c_hi_hi",
                                 "treatment_low": "c_lo_hi", "control_high": "c_hi_lo",
                                 "control_low": "c_lo_lo", "proposition": "increase_gt"}}


def verify_feasibility() -> dict:
    """AC-04：每份简报的参考设计在各私有世界变体上两两可分（DiD 差 > δ）。"""
    from scripts.s2b.calibrate_s2b import _run_one, _make_spec
    from orchestrator.rsi_bridge.physics_service import PhysicsService
    from orchestrator.rsi_r1 import scenes as S
    import tempfile, json as _json

    briefs = build_briefs()
    variants = world_variants()
    tmp = Path(tempfile.mkdtemp(prefix="s2c-open-"))
    svc = PhysicsService(observations_root=tmp / "obs", evidence_root=tmp / "ev")
    report = {}
    for bid, brief in briefs.items():
        scene = brief.scene_id
        study = {"A_contact_friction": "A", "C_collision_rebound": "C",
                 "D_forced_vibration": "D"}[scene]
        ref = reference_design(brief)
        # 每变体跑参考设计 4 条件 × 3 block → 主对比 DiD
        dids = {}
        for vname, vparams in variants[bid].items():
            ax = brief.factor_axes
            vals = {}
            for c in ref["conditions"]:
                params = {}
                for axis in ax:
                    params[axis["scene_param"]] = c["factors"][axis["name"]]
                # 变体注入（覆盖某些固定/额外参数）
                params.update(vparams)
                # 场景感知补默认（各场景边界内取值）
                from orchestrator.rsi_r1.scenes import scene_doc
                valid = {p.id for p in scene_doc(scene).params}
                _sc_defaults = {
                    "A_contact_friction": {"v0_mps": "0.4", "sliding_friction": "0.4",
                                           "mass_kg": "0.6", "torsional_friction_m": "0.01"},
                    "C_collision_rebound": {"v0_mps": "3.0", "solref_damping_ratio": "0.5",
                                            "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                            "solref_time_s": "0.02"},
                    "D_forced_vibration": {"drive_freq_hz": "1.5", "joint_damping": "0.2",
                                           "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"},
                }[scene]
                for k, v in _sc_defaults.items():
                    if k in valid:
                        params.setdefault(k, v)
                for b in range(3):
                    spec = _make_spec(study_id=study, condition_id=c["id"],
                                      block_id=f"b{b}", scene_id=scene, params=params)
                    r = _run_one(svc, spec, f"f-{bid}-{vname}-{c['id']}")
                    vals.setdefault((vname, c["id"]), []).append(r["metric_value"])
            th, tl = "c_hi_hi", "c_lo_hi"
            ch, cl = "c_hi_lo", "c_lo_lo"
            mean = lambda v, cid: sum(vals[(v, cid)]) / len(vals[(v, cid)])
            dids[vname] = ((mean(vname, th) - mean(vname, tl))
                           - (mean(vname, ch) - mean(vname, cl)))
        # 两两可分：变体间 DiD 差 > δ
        from orchestrator.rsi_r1 import rubric as R
        delta = float(R.metric_delta(brief.metrics[0]))
        vs = list(dids.values())
        min_pair = min((abs(vs[i] - vs[j]) for i in range(len(vs))
                        for j in range(i + 1, len(vs))), default=0.0)
        report[bid] = {"dids": dids, "min_pair_diff": min_pair, "delta": delta,
                       "separable": min_pair > delta}
    return report


def main() -> int:
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    briefs = build_briefs()
    print(f"Q 池 {len(briefs)} 简报：")
    for bid, b in briefs.items():
        print(f"  {bid}: {b.scene_id} metric={b.metrics} axes={[a['name'] for a in b.factor_axes]}")
    if args.verify:
        print("=== 可行性预检（参考设计跨世界变体两两可分）===")
        rep = verify_feasibility()
        for bid, r in rep.items():
            print(f"  {bid}: min_pair_diff={r['min_pair_diff']:.4g} δ={r['delta']} "
                  f"可分={r['separable']} dids={ {k: round(v,6) for k,v in r['dids'].items()} }")
        out = Path("/mnt/artifacts/simpraxis/s2c_open_feasibility.json")
        out.write_text(json.dumps(rep, indent=2))
        print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
