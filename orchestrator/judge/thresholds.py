"""Rung1–3 阈值常量与溯源（详细计划 L521-523；不允许裸数字）。

能力矩阵（memory_flywheel/capability_matrix.yaml，35 条带证据事实）与
RSI_survey.pdf 中均无穿模容差/jerk 阈值的现成出处，故按计划要求在此给
推导，完整版落证据 threshold_provenance.md（task5_judge/）。

穿模 0.005 m —— 两条独立推导收敛：
  D1 任务尺度：桌面操作典型块体边长 L≈0.05 m，容差取 0.1·L = 5 mm。
  D2 引擎接触松弛：MuJoCo 默认 solref=(0.002,1.0)（KB [CODE]
      genie_sim_v3/background_knowledge §2.2.3，接触时间常数 τ=2 ms）；
      pick-place 末段接近速度 v≈0.5 m/s，软接触正常弹性陷入
      d_soft≈v·τ=1e-3 m，取 5×d_soft = 5 mm。
  残余风险：PhysX GPU 路径 contact_offset/rest_offset 在 lw_benchhub
  与 genie_sim_v3 均沿用 Isaac 默认（matrix L58 标「未覆盖」），Task7
  实测后回填校准；在此之前阈值以 MuJoCo 参考实现为准，Rung4/人工 Gate2
  兜底（规则/VLM 分歧升级人工，D8）。

jerk 500 m/s³（60 Hz 帧序列基准值）：
  梯形速度 profile：v_p≈0.5 m/s，最短加加速度建立时间 t_r=0.1 s
  （60 fps 下 6 帧）→ a_max≈Δv/t_r=5 m/s²，一帧内建立 j≈300 m/s³，
  取 1.67× 余量 = 500 m/s³。
  等价的帧间最小可检尖峰 δ*=500·(1/60)³≈2.3e-3 m；交替 ±1 mm 抖动
  三阶中心差分为 8δ/dt³≈1.7e3 m/s³，必然拦截。
  阈值随帧 dt 缩放：thr(dt)=500·(dt60/dt)³，dt60=1/60 s（平滑运动的
  数值三阶差分对 dt 不敏感，噪声项按 dt^-3 缩放，故按 δ* 恒定换算）。
  dt 仅在 [1/120, 1/30] 锚定带内有效，带外 Rung3 保守判负。
"""
from __future__ import annotations

MAX_PENETRATION_M = 0.005            # Rung1：逐物理子步穿模深度上限（米）
JERK_THRESHOLD_60HZ = 500.0          # Rung3：60Hz 基准 jerk 上限（m/s³）
DT60 = 1.0 / 60.0
DT_BAND = (1.0 / 120.0, 1.0 / 30.0)  # Rung3 阈值锚定带（秒）

GOAL_WINDOW_S = 0.5                  # Rung2：末窗口全部在公差盒内（秒）
CONTACT_DURATION_S = 1.0             # Rung2：接触力连续 True 最短时长（秒）
CONTACT_FORCE_CV_MAX = 0.3           # Rung2：窗口内 std/|mean| 上限

# 任务尺度（推导入证据，勿在判定代码外散落裸值）
TASK_OBJECT_SCALE_M = 0.05
MUJOCO_SOLREF_TAU_S = 0.002          # KB matrix：MuJoCo 默认 solref timeconst
APPROACH_SPEED_M_S = 0.5


def jerk_threshold(dt: float) -> float:
    """按帧 dt 把 60 Hz 基准阈值换算到当前采样率（保持 δ* 恒定）。"""
    return JERK_THRESHOLD_60HZ * (DT60 / dt) ** 3
