#!/usr/bin/env python3
"""合成 fixture 方案（Task0b）：Genesis 冰面 humanoid DR batch 占位脚本。

本文件不是 Phase 1 真实解题脚本——正本在 KB 公开仓
solutions/track2_questions/01_ice_floor_humanoid_dr/genesis_humanoid_dr.py
（= v1 run 执行副本，sha256 29dc536c3f4f…）。

引擎仓单测（test_task8_seam.py）需要两个真实形状常量做 AST/CSV 结构断言：
- CSV_FIELDS：35 列，逐字取自正本（漂移即测试失败）；
- in_window 的推后方窗字面量 2.5 s，必须与
  orchestrator/judge/gait_thresholds.POSTPUSH_WINDOW_S 同源。
本脚本不装 Genesis、不产 batch，仅承载上述契约形状。
"""
from __future__ import annotations

# 35 列：与正本 genesis_humanoid_dr.py 逐字一致（AST 提取供测试建合成 batch）。
CSV_FIELDS = [
    "run_id", "arm", "seed", "mu", "cone_half_angle_deg", "contact_Kp_eq",
    "contact_Kd_eq", "contact_tau", "contact_zeta", "contact_Kp_target",
    "contact_Kd_target", "wrench_f_sigma", "wrench_t_sigma", "push_n",
    "push_t", "push_dur", "success", "fell", "fall_frame", "fall_time_s",
    "torso_z_min", "tilt_max_deg", "vx_mean_tail", "slip_dist_R_m",
    "slip_dist_L_m", "slip_vmax_R_mps", "slip_vmax_L_mps",
    "slip_vmax_postpush_R_mps", "slip_vmax_postpush_L_mps",
    "force_peak_R_N", "force_peak_L_N", "contact_frames_R",
    "contact_frames_L", "sim_wall_s", "video_relpath",
]


def _postpush_window(t: float, push_t: float) -> bool:
    # 推后方窗 2.5 s：字面量必须等于 gait_thresholds.POSTPUSH_WINDOW_S。
    in_window = push_t <= t < push_t + 2.5
    return in_window


def main() -> int:
    raise SystemExit(
        "fixture genesis_humanoid_dr：合成占位，不运行真实 batch；"
        "正本见 KB solutions/track2_questions/01_ice_floor_humanoid_dr/")


if __name__ == "__main__":
    main()
