#!/usr/bin/env python3
"""S2c Q 池 6 份领域简报（2 域 × 3，均经物理实测可分）+ 宿主私有世界变体 + 可行性预检。

设计要点（S2c-0 预检迭代后，AC-04）：
- 旧版用「与识别无交互的微扰」当世界变体（torsional/solimp/phase 微抖），DiD 两两≈相同，
  全 6 不可分——那是「漂移不是机制」（S2b 教训「凭直觉猜因子轴」重演）。
- 现版只用**物理实测**证明「2×2 DiD 非退化且被一个宽区间自由旋钮调制」的轴对，
  世界变体 = 该旋钮的 弱/强 两档（弱交互/强交互两套机制），参考设计端点可复现。
- 竞争解释重定义为**交互强度**：e1=弱（近可加），e2=强（显著协同）；actor 须设计实验判定。
- 简报只含公开面；旋钮数值（世界变体）只落宿主私有 files，不进 DomainBrief.public_document。

用法：python scripts/s2c/pool_open.py [--verify]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.hk.design_space import DomainBrief

# brief → (scene_id, metric, axis0{scene_param,lo,hi}, axis1{...}, regime_knob{...})
# 6 brief：C 3（强参数 trio {damp,v0,drop} 轮换轴/regime）+ D 3（{freq,damping,spring} 轮换）。
# 关键：regime 旋钮与 NC 链**不同参数**——C NC=solref_time(nuisance)/solimp(sham)、
# D NC=drive_amplitude(nuisance)/drive_phase(sham)，S2b 已验证非退化。故「世界判别力」
# 与「非退化 NC」不再抢同一个强调制剂（R2 前置债已解）。
_BRIEFS = {
    # 域 C 回弹：regime=drop_height_m（撞击速度 sqrt(v0^2+2gh) 调制交互，独立于 NC 链）
    "rebound_damp_v0": ("C_collision_rebound", "rebound_height_m",
                        {"name": "damp", "scene_param": "solref_damping_ratio", "lo": "0.3", "hi": "0.8"},
                        {"name": "v0", "scene_param": "v0_mps", "lo": "1.5", "hi": "4.5"},
                        {"name": "drop", "scene_param": "drop_height_m", "lo": "0.3", "hi": "2.0"}),
    "rebound_damp_drop": ("C_collision_rebound", "rebound_height_m",
                          {"name": "damp", "scene_param": "solref_damping_ratio", "lo": "0.3", "hi": "0.8"},
                          {"name": "drop", "scene_param": "drop_height_m", "lo": "0.3", "hi": "2.0"},
                          {"name": "v0", "scene_param": "v0_mps", "lo": "1.5", "hi": "4.5"}),
    "rebound_v0_drop": ("C_collision_rebound", "rebound_height_m",
                        {"name": "v0", "scene_param": "v0_mps", "lo": "1.5", "hi": "4.5"},
                        {"name": "drop", "scene_param": "drop_height_m", "lo": "0.3", "hi": "2.0"},
                        {"name": "damp", "scene_param": "solref_damping_ratio", "lo": "0.3", "hi": "0.8"}),
    # 域 D 受迫振动：regime=spring_stiffness_nm_rad（共振位置 sqrt(k/I)，独立于 NC 链）
    "forced_freq_damping": ("D_forced_vibration", "steady_state_amplitude_rad",
                            {"name": "freq", "scene_param": "drive_freq_hz", "lo": "0.5", "hi": "3.0"},
                            {"name": "damping", "scene_param": "joint_damping", "lo": "0.01", "hi": "0.5"},
                            {"name": "spring", "scene_param": "spring_stiffness_nm_rad", "lo": "5.0", "hi": "20.0"}),
    "forced_freq_spring": ("D_forced_vibration", "steady_state_amplitude_rad",
                           {"name": "freq", "scene_param": "drive_freq_hz", "lo": "0.5", "hi": "3.0"},
                           {"name": "spring", "scene_param": "spring_stiffness_nm_rad", "lo": "5.0", "hi": "20.0"},
                           {"name": "damping", "scene_param": "joint_damping", "lo": "0.01", "hi": "0.5"}),
    "forced_damping_spring": ("D_forced_vibration", "steady_state_amplitude_rad",
                              {"name": "damping", "scene_param": "joint_damping", "lo": "0.01", "hi": "0.5"},
                              {"name": "spring", "scene_param": "spring_stiffness_nm_rad", "lo": "5.0", "hi": "20.0"},
                              {"name": "freq", "scene_param": "drive_freq_hz", "lo": "0.5", "hi": "3.0"}),
}

# 场景默认（其余参数保持默认；含新 regime 参数的默认，与既有物理恒等）
_DEFAULTS = {
    "C_collision_rebound": {"solref_damping_ratio": "0.5", "v0_mps": "3.0",
                            "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                            "solref_time_s": "0.02", "drop_height_m": "1.0"},
    "D_forced_vibration": {"drive_freq_hz": "1.5", "joint_damping": "0.2",
                           "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0",
                           "spring_stiffness_nm_rad": "10.0"},
}


def build_briefs() -> dict:
    briefs = {}
    for bid, (scene, metric, ax0, ax1, knob) in _BRIEFS.items():
        expl = ({"id": "e_regime_invariant",
                 "desc": f"{ax0['name']} 与 {ax1['name']} 的交互强度与 regime（{knob['name']}）无关（近似不变）"},
                {"id": "e_regime_dependent",
                 "desc": f"{ax0['name']} 与 {ax1['name']} 的交互强度随 regime（{knob['name']}）显著改变"})
        briefs[bid] = DomainBrief(
            brief_id=bid,
            question=(f"{scene}：{ax0['name']} 与 {ax1['name']} 的交互强度是否随外部 regime 改变？"
                      f"设计实验判定（给出能区分『交互 regime 无关』与『交互随 regime 改变』的主对比与阴性对照）。"),
            competing_explanations=expl,
            factor_axes=tuple([
                {"name": ax0["name"], "values": [ax0["lo"], ax0["hi"]], "scene_param": ax0["scene_param"]},
                {"name": ax1["name"], "values": [ax1["lo"], ax1["hi"]], "scene_param": ax1["scene_param"]},
            ]),
            metrics=(metric,), scene_id=scene,
            constraints={"max_conditions": 6},
            pilot_quota={"max_iterations": 3, "runs_per_iteration": 16})
    return briefs


def world_variants() -> dict:
    """宿主私有世界变体（只进审查者通道）：每简报 2 套 regime = 旋钮 低/高 两档。

    v_regime_lo → regime 取下界；v_regime_hi → regime 取上界。
    判别力 = 同一参考设计在 2 变体下 DiD 差 > δ。regime 与 NC 链是不同参数。
    """
    wv = {}
    for bid, (_scene, _metric, ax0, ax1, knob) in _BRIEFS.items():
        wv[bid] = {
            "v_regime_lo": {knob["scene_param"]: knob["lo"]},
            "v_regime_hi": {knob["scene_param"]: knob["hi"]},
        }
    return wv


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


def _did_under(svc, scene, study, brief, ref, world_params, tag, n_block=3):
    """在给定世界变体（额外参数）下跑参考设计 4 条件 × n_block，返回 DiD。"""
    ax = brief.factor_axes
    vals = {}
    for c in ref["conditions"]:
        params = dict(_DEFAULTS[scene])
        for axis in ax:
            params[axis["scene_param"]] = c["factors"][axis["name"]]
        params.update(world_params)  # 旋钮覆盖（世界变体）
        cid = c["id"]
        for blk in range(n_block):
            from scripts.s2b.calibrate_s2b import _run_one, _make_spec
            spec = _make_spec(study_id=study, condition_id=cid, block_id=f"b{blk}",
                              scene_id=scene, params=params)
            r = _run_one(svc, spec, f"v-{brief.brief_id}-{tag}-{cid}-{blk}")
            vals.setdefault(cid, []).append(r["metric_value"])
    m = lambda c: sum(vals[c]) / len(vals[c])
    return (m("c_hi_hi") - m("c_lo_hi")) - (m("c_hi_lo") - m("c_lo_lo"))


def verify_feasibility(n_block: int = 3) -> dict:
    """AC-04：每份简报的参考设计在各私有世界变体上 DiD 差 > δ（可分）。

    同时报告参考 DiD 是否非退化（|DiD|>δ），防「漂移不是机制」。
    """
    from scripts.s2b.calibrate_s2b import _run_one, _make_spec  # noqa: F401
    from orchestrator.rsi_bridge.physics_service import PhysicsService
    from orchestrator.rsi_r1 import rubric as R
    import tempfile

    briefs = build_briefs()
    variants = world_variants()
    tmp = Path(tempfile.mkdtemp(prefix="s2c-open-"))
    svc = PhysicsService(observations_root=tmp / "obs", evidence_root=tmp / "ev")
    report = {}
    for bid, brief in briefs.items():
        scene = brief.scene_id
        study = {"C_collision_rebound": "C", "D_forced_vibration": "D"}[scene]
        ref = reference_design(brief)
        delta = float(R.metric_delta(brief.metrics[0]))
        dids = {}
        for vname, vparams in variants[bid].items():
            dids[vname] = _did_under(svc, scene, study, brief, ref, vparams, vname, n_block)
        vs = list(dids.values())
        min_pair = min((abs(vs[i] - vs[j]) for i in range(len(vs))
                        for j in range(i + 1, len(vs))), default=0.0)
        # 非退化：任一变体下参考 DiD 显著偏离 0
        max_abs_did = max(abs(v) for v in vs)
        report[bid] = {"dids": dids, "min_pair_diff": min_pair, "max_abs_did": max_abs_did,
                       "delta": delta, "separable": min_pair > delta,
                       "nondegenerate": max_abs_did > delta}
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
        print("=== 可行性预检（参考设计跨世界变体可分 & 非退化）===")
        rep = verify_feasibility()
        ok = True
        for bid, r in rep.items():
            sep, nd = r["separable"], r["nondegenerate"]
            ok = ok and sep and nd
            print(f"  {bid}: min_pair_diff={r['min_pair_diff']:.4g} δ={r['delta']} "
                  f"可分={sep} | max|DiD|={r['max_abs_did']:.4g} 非退化={nd} "
                  f"dids={ {k: round(v, 6) for k, v in r['dids'].items()} }")
        out = Path("/mnt/artifacts/simpraxis/s2c_open_feasibility.json")
        out.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(f"报告：{out}")
        print(f"全 6 可分且非退化：{ok}（{sum(1 for r in rep.values() if r['separable'] and r['nondegenerate'])}/{len(rep)}）")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
