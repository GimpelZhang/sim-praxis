#!/usr/bin/env python3
"""S2c P 池 6 预置实例（余量调查；不改 v2）+ 宿主参考设计物理定标签。

标签意图（计划 §3 S2c-1）：
  强效应 ×2（C/D 新参数格大交互，天花板校验）；零效应 ×2（已知交互≈0）；
  近 δ ×1（DiD≈1.2–1.8δ）；NC 陷阱 ×1（5 候选仅 1 有效）。

用法：python scripts/s2c/pool_preset.py [--verify]
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S
from orchestrator.rsi_r1 import task_specs as TS
from orchestrator.rsi_r1 import contrasts as X

_cond = TS._condition  # noqa: SLF001
_allowed = TS._allowed_space  # noqa: SLF001
_fixed = TS._fixed_of  # noqa: SLF001
_metric_sha = TS._metric_method_sha  # noqa: SLF001
SPLIT = TS.SPLIT_HOLDOUT


def _make_task(*, task_id, scene_id, conditions, primary, nc_slot,
               nuisance, question) -> TS.TaskSpecV13:
    spec = TS.TaskSpecV13(
        task_id=task_id, mechanism_id=scene_id, split=SPLIT,
        scene_version=S.scene_doc(scene_id).version,
        public_question=question,
        allowed_design_space=_allowed(scene_id),
        estimand={"metric_id": primary.metric,
                  "contrast_ids": [primary.id, "nc"],
                  "target_population": "32 个宿主分配的确认 block",
                  "aggregation": "paired_block_linear_contrast",
                  "weighting": "equal_block_weight"},
        nuisance_schema=nuisance,
        negative_control_spec=nc_slot,
        private_instance_commitment="0" * 64,
        metric_method_sha=_metric_sha(),
        resource_spec_sha="0" * 64,
        conditions=conditions, primary=primary,
        fixed={**_fixed(scene_id), "confirm_blocks": str(32)},
    )
    return spec.validate()


def _c_conds(a, b, va, vb, extra_a, extra_b, sham_damp=None):
    """C_N 场景的 2×2 + 可选 sham（按 ball_mass/solref_time nuisance 默认）。"""
    base = {"solimp_damping": "0.9", "ball_mass_kg": "0.5", "solref_time_s": "0.02"}
    keys_a = {**base, **extra_a}  # extra like {"solref_damping_ratio": a}
    return [
        _cond("c_hi_hi", S.SCENE_C, {**base, "solref_damping_ratio": a, "v0_mps": va}),
        _cond("c_hi_lo", S.SCENE_C, {**base, "solref_damping_ratio": a, "v0_mps": vb}),
        _cond("c_lo_hi", S.SCENE_C, {**base, "solref_damping_ratio": b, "v0_mps": va}),
        _cond("c_lo_lo", S.SCENE_C, {**base, "solref_damping_ratio": b, "v0_mps": vb}),
    ]


def _did(task_id_contrast, metric, high_a, low_a, high_b, low_b):
    return X.ContrastV13(id=task_id_contrast, kind=X.KIND_DID, metric=metric,
              proposition="decrease_gt", delta=R.metric_delta(metric),
              terms=X.did_terms(treatment_high=high_a, treatment_low=low_a,
                                 control_high=high_b, control_low=low_b))


def _nc_slot(metric_eq, allowed, cands, default, delta):
    return {"schema": "simpraxis.rsi_s2.nc_slot.v1",
            "allowed_diff": allowed,
            "max_perturbation": {k: "0.01" for k in allowed},
            "delta": delta, "ci_level": "0.90",
            "equivalence_metric": metric_eq,
            "candidates": cands, "default": default}


def build_instances() -> dict:
    """6 个 P 池实例 → {label_group: TaskSpec}。"""
    insts = {}

    # ---- 强效应 C：damping_ratio {0.2,0.85}×v0 {1.0,5.0} 大交互 ----
    conds = [
        _cond("c_hi_hi", S.SCENE_C, {"solref_damping_ratio": "0.85", "v0_mps": "5.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("c_hi_lo", S.SCENE_C, {"solref_damping_ratio": "0.85", "v0_mps": "1.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("c_lo_hi", S.SCENE_C, {"solref_damping_ratio": "0.2", "v0_mps": "5.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("c_lo_lo", S.SCENE_C, {"solref_damping_ratio": "0.2", "v0_mps": "1.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("c_sham", S.SCENE_C, {"solref_damping_ratio": "0.2", "v0_mps": "5.0",
                                    "solimp_damping": "0.905", "ball_mass_kg": "0.5",
                                    "solref_time_s": "0.02"}),
    ]
    insts["strong_C"] = ("强效应", _make_task(
        task_id="s2c-strong-C", scene_id=S.SCENE_C, conditions=tuple(conds),
        primary=_did("damp_x_vel", "rebound_height_m", "c_hi_hi", "c_lo_hi", "c_hi_lo", "c_lo_lo"),
        nc_slot=_nc_slot("rebound_height_m_negative_equivalence", ["solimp_damping"],
                         ["c_lo_hi", "c_sham", "c_hi_hi", "c_hi_lo"],
                         {"base": "c_lo_hi", "sham": "c_sham"}, "0.001"),
        nuisance={"solref_time_s": {"mode": "varying", "support": ["0.015", "0.025"],
                                    "assignment_owner": "host", "note": "接触时间常数变异"},
                  },
        question="球体碰撞：阻尼比(0.85→0.2)对反弹高度效应是否随撞击速度(5→1)改变？"))

    # ---- 强效应 D：freq {0.5,3.0}×damping {0.02,0.45} 大交互 ----
    conds_d = [
        _cond("d_hi_hi", S.SCENE_D, {"drive_freq_hz": "3.0", "joint_damping": "0.45",
                                     "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d_hi_lo", S.SCENE_D, {"drive_freq_hz": "3.0", "joint_damping": "0.02",
                                     "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d_lo_hi", S.SCENE_D, {"drive_freq_hz": "0.5", "joint_damping": "0.45",
                                     "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d_lo_lo", S.SCENE_D, {"drive_freq_hz": "0.5", "joint_damping": "0.02",
                                     "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d_sham", S.SCENE_D, {"drive_freq_hz": "3.0", "joint_damping": "0.45",
                                    "drive_phase_rad": "0.01", "drive_amplitude_nm": "1.0"}),
    ]
    insts["strong_D"] = ("强效应", _make_task(
        task_id="s2c-strong-D", scene_id=S.SCENE_D, conditions=tuple(conds_d),
        primary=_did("freq_x_damp", "steady_state_amplitude_rad", "d_hi_hi", "d_lo_hi", "d_hi_lo", "d_lo_lo"),
        nc_slot=_nc_slot("steady_state_amplitude_rad_negative_equivalence", ["drive_phase_rad"],
                         ["d_hi_hi", "d_sham", "d_hi_lo", "d_lo_hi"],
                         {"base": "d_hi_hi", "sham": "d_sham"}, "0.01"),
        nuisance={"drive_amplitude_nm": {"mode": "varying", "support": ["0.9", "1.1"],
                                         "assignment_owner": "host", "note": "驱动力幅值变异"},
                  },
        question="受迫振动：驱动频率(3→0.5Hz)对稳态振幅效应是否随关节阻尼(0.45→0.02)改变？"))

    # ---- 零效应 A2→ 改 D 系列窄格（A 场景 torsional NC 结构退化不可用）----
    conds_d3 = [
        _cond("d3_hi_hi", S.SCENE_D, {"drive_freq_hz": "2.0", "joint_damping": "0.20",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d3_hi_lo", S.SCENE_D, {"drive_freq_hz": "2.0", "joint_damping": "0.195",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d3_lo_hi", S.SCENE_D, {"drive_freq_hz": "1.95", "joint_damping": "0.20",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d3_lo_lo", S.SCENE_D, {"drive_freq_hz": "1.95", "joint_damping": "0.195",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("d3_sham", S.SCENE_D, {"drive_freq_hz": "2.0", "joint_damping": "0.20",
                                     "drive_phase_rad": "0.01", "drive_amplitude_nm": "1.0"}),
    ]
    insts["zero_D3"] = ("零效应", _make_task(
        task_id="s2c-zero-D3", scene_id=S.SCENE_D, conditions=tuple(conds_d3),
        primary=_did("freq_x_damp", "steady_state_amplitude_rad", "d3_hi_hi", "d3_lo_hi", "d3_hi_lo", "d3_lo_lo"),
        nc_slot=_nc_slot("steady_state_amplitude_rad_negative_equivalence", ["drive_phase_rad"],
                         ["d3_hi_hi", "d3_sham", "d3_hi_lo", "d3_lo_hi"],
                         {"base": "d3_hi_hi", "sham": "d3_sham"}, "0.01"),
        nuisance={"drive_amplitude_nm": {"mode": "varying", "support": ["0.95", "1.05"],
                                         "assignment_owner": "host", "note": "幅值窄变异"}},
        question="受迫振动窄格：频率(2.0→1.8Hz)对振幅效应是否随阻尼(0.20→0.18)改变？"))

    # 零效应 2 复用另一 cell 的零交互（D_N 低频段弱交互）
    conds_d2 = [
        _cond("dz_hi_hi", S.SCENE_D, {"drive_freq_hz": "0.6", "joint_damping": "0.4",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("dz_hi_lo", S.SCENE_D, {"drive_freq_hz": "0.6", "joint_damping": "0.35",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("dz_lo_hi", S.SCENE_D, {"drive_freq_hz": "0.5", "joint_damping": "0.4",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("dz_lo_lo", S.SCENE_D, {"drive_freq_hz": "0.5", "joint_damping": "0.35",
                                      "drive_phase_rad": "0.0", "drive_amplitude_nm": "1.0"}),
        _cond("dz_sham", S.SCENE_D, {"drive_freq_hz": "0.6", "joint_damping": "0.4",
                                     "drive_phase_rad": "0.01", "drive_amplitude_nm": "1.0"}),
    ]
    insts["zero_D2"] = ("零效应", _make_task(
        task_id="s2c-zero-D2", scene_id=S.SCENE_D, conditions=tuple(conds_d2),
        primary=_did("freq_x_damp", "steady_state_amplitude_rad", "dz_hi_hi", "dz_lo_hi", "dz_hi_lo", "dz_lo_lo"),
        nc_slot=_nc_slot("steady_state_amplitude_rad_negative_equivalence", ["drive_phase_rad"],
                         ["dz_hi_hi", "dz_sham", "dz_hi_lo", "dz_lo_hi"],
                         {"base": "dz_hi_hi", "sham": "dz_sham"}, "0.01"),
        nuisance={"drive_amplitude_nm": {"mode": "varying", "support": ["0.95", "1.05"],
                                         "assignment_owner": "host", "note": "幅值窄变异"}},
        question="受迫振动低频段：频率(0.6→0.5Hz)对振幅效应是否随阻尼(0.4→0.35)改变？"))

    # ---- 近 δ C：damping {0.55,0.65} × v0 {3.6,4.0}，DiD 调至 ≈1.5δ（δ=0.001）----
    conds_n = [
        _cond("n_hi_hi", S.SCENE_C, {"solref_damping_ratio": "0.60", "v0_mps": "4.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("n_hi_lo", S.SCENE_C, {"solref_damping_ratio": "0.60", "v0_mps": "3.8",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("n_lo_hi", S.SCENE_C, {"solref_damping_ratio": "0.55", "v0_mps": "4.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("n_lo_lo", S.SCENE_C, {"solref_damping_ratio": "0.55", "v0_mps": "3.8",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("n_sham", S.SCENE_C, {"solref_damping_ratio": "0.55", "v0_mps": "4.0",
                                    "solimp_damping": "0.905", "ball_mass_kg": "0.5",
                                    "solref_time_s": "0.02"}),
    ]
    insts["near_delta_C"] = ("近δ", _make_task(
        task_id="s2c-near-C", scene_id=S.SCENE_C, conditions=tuple(conds_n),
        primary=_did("damp_x_vel", "rebound_height_m", "n_hi_hi", "n_lo_hi", "n_hi_lo", "n_lo_lo"),
        nc_slot=_nc_slot("rebound_height_m_negative_equivalence", ["solimp_damping"],
                         ["n_lo_hi", "n_sham", "n_hi_hi", "n_hi_lo"],
                         {"base": "n_lo_hi", "sham": "n_sham"}, "0.001"),
        nuisance={"solref_time_s": {"mode": "varying", "support": ["0.015", "0.025"],
                                    "assignment_owner": "host", "note": "接触时间常数变异"}},
        question="球体碰撞：阻尼比(0.65→0.55)对反弹高度效应是否随撞击速度(4→3.6)改变？"))

    # ---- NC 陷阱 C：主 DID 用 damping×v0 全因子；NC 候选 5 个，4 诱饵（因子格）
    #      与 base 只差 damping/v0（不在 allowed_diff=solimp）→ 结构不可达；仅 sham 有效。
    trap_conds = [
        _cond("t_hi_hi", S.SCENE_C, {"solref_damping_ratio": "0.7", "v0_mps": "4.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("t_hi_lo", S.SCENE_C, {"solref_damping_ratio": "0.7", "v0_mps": "2.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("t_lo_hi", S.SCENE_C, {"solref_damping_ratio": "0.4", "v0_mps": "4.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        _cond("t_lo_lo", S.SCENE_C, {"solref_damping_ratio": "0.4", "v0_mps": "2.0",
                                     "solimp_damping": "0.9", "ball_mass_kg": "0.5",
                                     "solref_time_s": "0.02"}),
        # sham：只改 solimp（默认 base=t_lo_lo, sham=t_sham → diff 仅 solimp ✓）
        _cond("t_sham", S.SCENE_C, {"solref_damping_ratio": "0.4", "v0_mps": "2.0",
                                    "solimp_damping": "0.905", "ball_mass_kg": "0.5",
                                    "solref_time_s": "0.02"}),
    ]
    insts["nc_trap_C"] = ("NC陷阱", _make_task(
        task_id="s2c-trap-C", scene_id=S.SCENE_C, conditions=tuple(trap_conds),
        primary=_did("damp_x_vel", "rebound_height_m", "t_hi_hi", "t_lo_hi", "t_hi_lo", "t_lo_lo"),
        nc_slot=_nc_slot("rebound_height_m_negative_equivalence", ["solimp_damping"],
                         # 5 候选：t_lo_lo + t_sham(唯一有效) + 3 个因子格诱饵（不可达）
                         ["t_lo_lo", "t_sham", "t_hi_hi", "t_hi_lo", "t_lo_hi"],
                         {"base": "t_lo_lo", "sham": "t_sham"}, "0.001"),
        nuisance={"solref_time_s": {"mode": "varying", "support": ["0.015", "0.025"],
                                    "assignment_owner": "host", "note": "接触时间常数变异"}},
        question="球体碰撞：阻尼比(0.7→0.4)对反弹高度效应是否随撞击速度(4→2)改变？NC 5 候选仅 1 有效。"))

    return insts


def verify_labels(insts: dict) -> dict:
    """宿主参考设计物理跑 → 每实例 DiD，按预注册判据定标签（AC-05）。"""
    from scripts.s2b.calibrate_s2b import _run_one, _make_spec
    from orchestrator.rsi_bridge.physics_service import PhysicsService
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="s2c-preset-"))
    svc = PhysicsService(observations_root=tmp / "obs", evidence_root=tmp / "ev")

    report = {}
    for name, (label, task) in insts.items():
        scene = task.mechanism_id
        study = {"C_collision_rebound": "C", "D_forced_vibration": "D",
                 "A_contact_friction": "A"}[scene]
        # 每条件 3 block 取均值
        vals = {}
        for c in task.conditions:
            for b in range(3):
                spec = _make_spec(study_id=study, condition_id=c.id,
                                  block_id=f"b{b}", scene_id=scene,
                                  params=dict(c.params))
                r = _run_one(svc, spec, f"v-{name}-{c.id}")
                vals.setdefault(c.id, []).append(r["metric_value"])
        # DiD from primary terms
        def mean(cid):
            return sum(vals[cid]) / len(vals[cid])
        terms = task.primary.terms
        # DID terms: (th,+1)(tl,-1)(ch,+1)(cl,-1) → (thctl - tlctl) - ... 线性
        did = 0.0
        for cid, w in terms:
            did += float(w) * mean(cid)
        delta = float(task.primary.delta)
        report[name] = {"label": label, "did": did, "delta": delta,
                        "ratio": abs(did) / delta,
                        "conditions": [c.id for c in task.conditions]}
    return report


def main() -> int:
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    insts = build_instances()
    print(f"P 池 {len(insts)} 实例：")
    for name, (label, task) in insts.items():
        print(f"  {name} [{label}] {task.task_id} conds={len(task.conditions)}")
    if args.verify:
        print("=== 物理定标签 ===")
        rep = verify_labels(insts)
        for name, r in rep.items():
            print(f"  {name} [{r['label']}] DiD={r['did']:.4f} δ={r['delta']} "
                  f"|DiD|/δ={r['ratio']:.1f}")
        out = Path("/mnt/artifacts/simpraxis/s2c_preset_report.json")
        out.write_text(json.dumps(rep, indent=2))
        print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
