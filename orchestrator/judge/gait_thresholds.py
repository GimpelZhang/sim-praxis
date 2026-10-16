"""Task8 Track2 步态/滑移判定阈值与溯源（Phase_1_Plan_detailed.md L561-572）。

与 Track1（rungs.py/thresholds.py 的夹爪推块语义）相互独立：Task8 的判定对象
是双足人形在正常地面/结冰地面/DR 包络三臂 × N 种子的 **batch**，阈值只服务
两类判断——证据是否可信（finite/独立重算/物理界）与对照是否可分辨（预注册
有序性）。**不**设「实验组必须更好」的通过阈值：L571 规定矛盾本身是结论。

阈值溯源
--------
跌倒判据 FALL_Z_M=0.85 / FALL_TILT_DEG=50：直接取自生产脚本
sandbox/track2/genesis_humanoid_dr.py 的跌倒检测（控制器冻结载体口径，
站立躯干高 ≈1.27 m）；判定器从 series 重算并与 outcomes 逐 run 对质，
不允许脚本自报成败而不验。

接触积分力门 CONTACT_FORCE_N=2.0、telemetry 帧 TELEM_HZ=50、期望帧数 400
（8 s）：同脚本 telemetry 口径；滑移距离 = Σ|v_xy|·dt（仅接触帧）。

滑移速度物理界 SLIP_V_MAX_MPS=50：A800 上 8 s 片段里自由倾覆的实测峰速
sw4 最大 16.8 m/s（push_window_calibration.json v2），50 是数值爆炸
（NaN 邻帧/求解器发散）界，不是行为阈值。

对照有序性 CONTRAST_RATIO=2.0（推力后峰速组均值之比）：sw4 确定性臂九格
标定中正常存活格峰速 ≤0.474 m/s、冰面倾覆格 ≥5.6 m/s（比值 >10）；取
2.0 是两族之间宽余量的预注册界，写于正式 batch **之前**
（/mnt/caches/simpraxis/genesis_probe/push_window_calibration.json v2）。
若正式 batch 不满足，报「不可分辨/矛盾」并升级人工，绝不事后调界。

重算容差：series 力值四舍五入到 0.01 N，2.0 N 门边界帧可能翻转（单帧
贡献 vxy·0.02 s，倾覆段 vxy 可达 17 m/s ⇒ 单帧 ≤0.34 m），故滑移距离
容差取相对 5%（典型接触速度 <<1 m/s 时该界远宽于取整误差）；峰速/躯干
极值来自同一组 rounded series，要求逐位一致。
"""
from __future__ import annotations

FALL_Z_M = 0.85
FALL_TILT_DEG = 50.0
CONTACT_FORCE_N = 2.0
TELEM_HZ = 50
DUR_S = 8.0
EXPECTED_FRAMES = 400

SLIP_V_MAX_MPS = 50.0
# sw4 名义臂实测峰 6032 N（Kp≈1.1e4 倾覆冲击），取 10 kN 数值界。
CONTACT_FORCE_MAX_N = 10000.0
# DR 臂 Kp 对数包络上界 1e6（≈名义 ×90）：法向力 F~Kp·δ，网格穿深 δ 取
# 2 cm 接触容差即 F~2e4 N——按包络推导的界（20 kN），不是按实测 12.7 kN
# 反调；名义两臂仍用 10 kN。
CONTACT_FORCE_MAX_N_DR = 20000.0

# outcomes 的峰速从未取整的 vxy 取 max 后 round(3)；series 存 round(4)，
# 在 125 帧窗口内 argmax 可能换到相邻帧（实测差 0.001 m/s）。独立重算的
# 对质容差取 0.005 m/s（远小于有序/失序两族间距 0.5 vs 5.6 m/s）。
VMAX_ABS_TOL = 0.005

# tilt 两级精度舍入桶界：series 存 round(raw,3)、outcomes 存 round(raw,2)，
# 二者同源 raw 但不可逐位比对——raw 极大值 m∈[x.xx45, x.xx5) 时
# round(m,3)=x.xx5（浮点回读可偏上）再 round(_,2)=x.(xx+1)，而
# round(m,2)=x.xx（实测 ice_dr_s1：m∈[123.8045,123.805)，series 123.805
# →123.81 vs 声明 123.8）。容差取声明值末位一单位 0.01°；存活/倾覆两族
# 间距为 9.88° vs ≥104°，该容差不具区分意义。
TILT_MAX_ABS_TOL_DEG = 0.01

SLIP_DIST_REL_TOL = 0.05        # 重算 vs 自报滑移距离相对容差（力门取整翻转）
CONTRAST_RATIO = 2.0            # 预注册：冰/干 推力后峰速组均值之比下界

TELEMETRY_SCHEMA = "simpraxis.task8.track2.telemetry.v1"
ENGINE = "genesis"
ENGINE_VERSION = "1.3.3"
ARMS = ("normal", "ice_nominal", "ice_dr")
MIN_SEEDS = 3
