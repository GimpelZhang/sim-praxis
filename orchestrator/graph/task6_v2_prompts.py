#!/usr/bin/env python3
"""Task6 V2 拆块出生（chunked birth）提示词契约文本——纯文本常量模块。

硬规则 3 合规声明：本模块**只**含提示词契约文本（中文描述性骨架、常量名、
Genesis API 序列、JSON 格式说明），**不是解题脚本**：模块内没有任何可运行
的 genesis rollout 实现（不 import genesis、不构造场景、不产生 telemetry/
CSV/视频），也没有从 V1/V2 解题脚本搬运的完整实现代码。其中短 API 序列
（如 gs.init(...)、set_sol_params(np.array([...]))、IK 公式）是作为「逐字
复用」契约文本保留的结构说明；引擎层不携带 genesis_humanoid_dr_v2.py
本体，V2 文件只能在付费 workdir 内由 runner 拆块出生。

锁内适配（计划 v1.2 外，seq42-47 拆块出生，用户 2026-10-20 裁决）：
seq43/45/46/47 实证三模型在 Anthropic-Messages shim 上单次出生 600-900 行
V2 全部同形失败（唯一 thinking block 吃满 16384 output、0 text）；同款模型
小任务可收敛。故把一次大出生拆成 4 个顺序小编排调用，每 call 只产出一块，
runner 本地拼接校验后以单个 old="" 补丁出生。本模块提供每 call 都带的合同/
骨架文案与四块边界；文案以 2026-10-20 紧凑注入探针验证过的
CONTRACT/SKELETON 为准搬入（去掉探针/证据措辞与占位符，仅保留 @@SHA@@
由工厂运行时填入 staged V1 入口 sha256 前 16 位）。
2026-10-21 run1（1 轮拆块出生 seq65 + 2 轮单补丁修复 seq66/67）后人因结案
（临时 chdir+相对录制路径），用户裁决①，合同追加第 7 条通用产物路径纪律后
重挂 run2（不改 n=4/32768/20000/行预算）。
2026-10-21 run2 chunk3 守卫三振（21256/24514/22293>20000，跨 run 1/5）后用户裁决守卫回提 26000 重挂 run3，合同七条/n=4/32768/行预算不变。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# V2 升阶任务合同（七条；每块调用都注入，runner 渲染为独立 ## 段）
# ---------------------------------------------------------------------------

# 注：合同正文首行不用 ## 标题——runner 把本常量包进《## V2 升阶任务合同》
# extra 段（评审 nit，折叠于 2026-10-20），避免相邻双 H2；优先级语义保留为加粗行。
V2_CONTRACT = (
    """**V2 升阶任务合同（最高依据，先于下列失败信息阅读）**

你要出生一个**新文件** `genesis_humanoid_dr_v2.py`：Gate2 已验收 V1 的控制器/DR 升阶版。

1. **严格 JSON 分块交付，最终补丁 old 必须是空字符串**：完整 V2 源码
   600-900 行、不要截断，由 4 个顺序调用分块出生；每次调用只输出该次分配块
   的 chunk JSON（格式严格按《拆块交付》段），引擎本地按序拼接后以单个
   old="" 补丁出生文件。不要解释、不要输出 JSON 外文字。
2. **自包含**：新文件独立可运行。严禁运行时搜索/定位/open 读取/import
   任何既有 V1 文件（genesis_humanoid_dr.py）或历史 run 目录；严禁 os.execv、os.walk、
   glob/rglob 查找、shim/转发重跑。违反整轮作废。
   （batch 父进程用 subprocess 拉起**自身** __file__ 的 rollout 子进程
   是 V1 既有合法模式，允许且必须保留。）
3. **n=3 诚实**：提案里“3 臂×10 seeds”是 macro 笔误，冻结合同 **3
   seeds**；expected_artifacts 里的 v2_genesis_contact_dr.py 已被入口名
   `genesis_humanoid_dr_v2.py` 取代。禁止把 3 次标成 10 次、禁止改种子数。
4. **整体设计约束（关键）**：下方骨架中标【逐字复用】的常量/公式/API 序列/
   IK/控制器/采集/产物壳**直接采用，不要重新推导或改写它们**；
   你唯一的创造性工作是骨架末尾【V2 替换】的 a)-g) 新参数化与能量审计。
   不要逐行重推 Genesis API 与控制器数学。
5. **退化诚实**：Genesis 1.3.3 只暴露线性 sol 7 元组与标量摩擦时，
   Hunt-Crossley/Stribeck/关节 armature 等无法逐子步表达的项必须退化为
   等效实现并在源码注释与 innovation.json 显式记录口径，禁止假装实现。
6. **禁硬编码结果**：summary/CSV/telemetry 全部真实运行机产；判定器从
   series 独立重算成败/滑移/峰速，伪造必被抓。
"""
    # 锁内适配（2026-10-21 用户裁决①，seq65-67 run1 human_failure 后 run2 重挂）：
    # 模型 V2 临时 chdir 后裸调 start_recording 相对路径，编码器惰性开文件撞只读 cwd；
    # 条款只立通用产物路径纪律，不点名 Genesis API、不提供修法。
    """7. **产物路径纪律**：脚本可以为解析资产临时 chdir（例如进入 genesis/assets 解析 MJCF），
   但所有写出文件——telemetry/.json/.csv/.mp4/日志/临时件——一律以输出目录下的
   **绝对路径**创建与打开；禁止把任何临时 chdir 之后的 cwd 当作产物写入位置前提。
   第三方库若在内部线程中**惰性**创建文件（录制器在首个仿真步才打开文件即属此类），
   传入的文件名同样必须是绝对路径；任何临时 chdir 必须在开始步进仿真前恢复原 cwd。
"""
)

# ---------------------------------------------------------------------------
# V1 冻结实现结构骨架（改写蓝本；@@SHA@@ 由工厂填 staged V1 sha256 前 16 位）
# ---------------------------------------------------------------------------

V2_SKELETON = """### V1 冻结实现结构骨架（改写蓝本；V1 全文 sha256=@@SHA@@，不随本请求提供）
```
常量【逐字复用】:
  L1=0.40, L2=0.39, DT=0.005(substep), CTRL_HZ=100, TELEM_HZ=50,
  dur=8.0s → 恰好 400 telemetry 帧（每 10 子步采 1 帧）;
  SCHEMA_VERSION="simpraxis.task8.track2.telemetry.v1";
  ARMS=("normal","ice_nominal","ice_dr"); PUSH 35N×0.2s,
  PUSH_T_BY_SEED=[3.0,2.7,3.3];
  NOMINAL_SOL={tau:.01,zeta:1,dmin:.9,dmax:.95,width:1e-3,mid:.5,power:2};
  μ: normal=0.7, ice_nominal=0.05（名义两臂不下发 sol）。

Genesis sol 7 元组 ↔ 等效 (Kp,Kd)【公式逐字复用】:
  Kd=2/(dmax·tau); Kp=1/(dmax²·tau²·zeta²);
  反解 tau=2/(dmax·Kd)（硬下限 2·DT+1e-4）, zeta=1/(dmax·tau·√Kp)。

场景 API 序列【逐字复用】:
  os.environ.setdefault("GS_ENABLE_ZEROCOPY","0")
  gs.init(backend=gs.gpu, seed=seed, use_deterministic_algorithms=True)
  gs.Scene(sim_options=gs.options.SimOptions(dt=DT, substeps=1),
           show_viewer=False, renderer=gs.options.renderers.Rasterizer())
  scene.add_entity(gs.morphs.Plane(), material=gs.materials.Rigid(friction=mu))
  human=scene.add_entity(gs.morphs.MJCF(file="xml/humanoid.xml"))
  （rollout 子进程 chdir 到 os.path.dirname(gs.__file__)+"/assets" 仅为解析 MJCF；
  步进前必须 restore cwd，任何产物/录制文件名只用绝对路径，见合同 7）
  cam=scene.add_camera(model="pinhole",res=(640,480),
      pos=(1.8,-2.2,1.25),lookat=(0.6,0,0.85),fov=42,GUI=False)
  scene.build()
  关节 dof 名: hip_y/knee/ankle_y/hip_x/hip_z_{right,left}, abdomen_x/y/z,
  shoulder1/shoulder2/elbow_{right,left}（get_joint(name).dofs_idx_local）;
  link 名: torso, thigh_{right,left}, foot_{right,left}。
  PD gains【逐字复用】: leg 1500/60, abdomen 600/30, upper 300/20
  (set_dofs_kp/set_dofs_kv)；q0=初始 dofs 位姿。
  接触: 三臂都对 foot_right/foot_left 调 set_friction(mu)（双 geom max
  组合，必须双足同步）；override_sol 臂对 ground.geoms 与双足 geoms 同时
  set_sol_params(np.array([tau,zeta,dmin,dmax,width,mid,power],float64))。
  先 settle 0.5s：control_dofs_position(q0) 后 scene.step()。

两杆矢状面 IK + capture-point/Raibert 反应踏步【逐字复用，V2 不改策略】:
  leg_ik(x,z): r=min(hypot(x,z),L1+L2-1e-4);
    kap=acos(clamp((L1²+L2²-r²)/(2·L1·L2))); beta=π-kap;
    delta=atan2(x,z); gam=asin(clamp(L2·sin(beta)/r)); return(delta+gam,beta)
  参数: cadence_hz=1, step_len=.22, step_h=.07, vdes=.2, t_ds=.8,
    theta_des=.05, k_v=.18, k_th=.12, k_w=.02, lean_lat=.14,
    contact_n=25N；相位 p=(2π·cadence·max(0,t-t_ds))+(R:0,L:π) mod 2π，
    p≥π 想要 swing；stance→swing 时 t_sw=.5/cadence，
    x_plant=torso_x+vx·t_sw+0.5·step_len+k_v(vx-vdes)+k_th(θ-θdes)+
            k_w·θ̇（θ̇ 用 0.7/0.3 低通）；
    swing 落腿: u=(t-t0)/t_sw，u≥.55 且足底接触力>25N 或 u≥1；空中轨迹
    smoothstep s=3u²-2u³、抬腿 step_h·sin(πu)；
    dof target: hip=q0-α, knee=q0-β, ankle=q0+(θ+α-β)，
    α=gamma-θ；单腿支撑期 abdomen_x 加 lean_lat 侧倾。
  控制每 ctrl_every=20 子步更新一次，human.control_dofs_position(target)。

外力【逐字复用；每子步 scene.step() 前重发，外力每子步清零】:
  推力窗 push_t≤t<push_t+0.2:
    human._solver.apply_links_external_force(
      force=np.array([[35.0,0,0]]), links_idx=np.array([torso_idx]))
  ice_dr 油污 wrench（独立 RNG: default_rng(9901+seed*131+arm_index*7717)）:
    双足水平 N(0,2.0)N apply_links_external_force + 偏航
    N(0,0.2)Nm apply_links_external_torque。

采集与 telemetry【键名逐字复用】:
  series 9 键等长 400: t, torso_z, torso_x,
    tilt_deg=degrees(acos(clamp(Rm[2,2]))), vx(torso get_vel x),
    contact_force_R/L (human.get_contacts(with_entity=ground)，按 foot link
    idx 过滤 force_a 行求范数和), slip_v_R/L (foot get_vel 的 xy 范数)。
  跌倒: z<0.85 或 tilt>50°。
  outcomes: success(=not fell), fell, fall_frame(或 null),
    fall_time_s, torso_z_min, torso_z_last, torso_x_last, tilt_max_deg,
    vx_mean_tail(末 1/4 段均值),
    slip_dist_m{R,L}=Σ|vxy|·0.02 仅接触帧(fmag>2N),
    slip_v_max_mps{R,L}, slip_v_max_postpush_mps{R,L}
    (窗 [push_t, push_t+2.5)), contact_force_peak_N{R,L},
    contact_frames{R,L}, sim_wall_s(真实 time.time 墙钟,正有限)。
  telemetry 顶层: schema, label, engine="genesis", engine_version="1.3.3",
    params(含 arm/seed/mu/contact/push/wrench/gains), sim{dt,ctrl_hz,
    telem_hz,dur_s,frames,gs_enable_zerocopy:false,
    use_deterministic_algorithms:true}, series, outcomes, video_relpath。

batch 与产物【壳逐字复用】:
  父进程不 import genesis；9 rollout 顺序子进程
  cmd=[sys.executable, abspath(__file__),"rollout","--params-json",p,
       "--telemetry",t,"--video",v,"--label",run_id,"--dur-s","8.0"]，
  env GS_ENABLE_ZEROCOPY=0；run_id=f"{arm}_s{seed}"。
  argparse: {rollout|batch}；rollout --params-json [--telemetry --video
  --label --dur-s]；batch --out-dir --seeds（默认 3）[--dur-s]
  --calibration-json（拷入 out_dir，samefile 守卫）。
  telemetry/{run_id}.json 与 .params.json；videos/{run_id}.mp4
  （均以 out_dir 下绝对路径传入；--video 给录制器的必须是绝对路径）。
  dr_controls_vs_experimental.csv 恰好 9 数据行（# 注释行允许），35 列
  顺序: run_id,arm,seed,mu,cone_half_angle_deg,contact_Kp_eq,
  contact_Kd_eq,contact_tau,contact_zeta,contact_Kp_target,
  contact_Kd_target,wrench_f_sigma,wrench_t_sigma,push_n,push_t,
  push_dur,success,fell,fall_frame,fall_time_s,torso_z_min,tilt_max_deg,
  vx_mean_tail,slip_dist_R_m,slip_dist_L_m,slip_vmax_R_mps,
  slip_vmax_L_mps,slip_vmax_postpush_R_mps,slip_vmax_postpush_L_mps,
  force_peak_R_N,force_peak_L_N,contact_frames_R,contact_frames_L,
  sim_wall_s,video_relpath。
  summary.json: {schema,seeds:3,disturbance:{push_n,push_dur_s,
  push_t_by_seed},dr_envelope:{V2 真实分布描述},groups:{arm:{n,
  success_rate,n_fell,fall_frames,mu_per_seed,slip_dist_m_mean:[R,L],
  slip_vmax_postpush_mps_max:[单值列表照 V1 形状],tilt_max_deg_max}}}。
  另产: plots/contact_forces.png, plots/slip_comparison.png,
  collage.png（3 臂×6 帧 PIL 标注）,
  split_normal_vs_ice_nominal_s0.mp4, split_ice_nominal_vs_ice_dr_s0.mp4
  (ffmpeg hstack libx264 crf28 yuv420p), cover.png, SUMMARY.md,
  manifest.json, innovation.json, push_window_calibration.json。

【V2 替换】只重写参数化/物理段（draw_params 等），a)-g) 为全部新工作:
  a) ζ 条件化: ζ~logU[0.3,0.9] 先采, Kp~logU[7.69e3,1.3e6],
     Kd=2ζ√(Kp·m_eff) clip[15,2000]（m_eff=60kg，在注释写明口径），
     再映射 sol 7 元组；逐 run 记录 ζ 与「对 V1 独立 Kd~logU[15,200]
     箱的修复」判定（ζ<0.3 等价计数）。
  b) Stribeck: μs~logU[.035,.60], μk=μs·U[.4,.9], vs~U[.01,.05]；
     标量 set_friction 无法表达 μ(v)=μk+(μs−μk)exp(−(v/vs)²)，按
     episode 等效 μ 实现并注释/innovation 记录退化，禁止谎称速度相关。
  c) Hunt-Crossley Fn=Kδ^1.5(1+c δ̇)：引擎仅线性 sol，退化为 Kp/ζ
     等效，n=1.5、恢复系数 e~U[0,.1] 的设计意图写进注释与 innovation。
  d) 5 级材质梯（干/湿/油/釉冰/混合斑；μ 锚点约 .6/.3/.1/.03，
     混合斑 patch 面积比 U[.1,.4]、0.5m 网格 tile）；3 seeds 铺不满梯级
     用确定性规则 forced_for_ladder_coverage 选最低 μk seed 启用 tile，
     并产 μ 扫描 0.03/.06/.12/.30 单调证据（可入 innovation/dr 分布，
     禁止虚构未跑的 episode 行）。
  e) 关节 damping~logU[1,20] N·m·s/rad、armature~logU[.01,.1]、
     frictionloss~U[.1,.5]；Genesis 无对应 dof API 时如实记录未施加项。
  f) 安全界 contact_force≤8000N（DR≤16000）、slip_v≤40m/s；每 episode
     能量审计：外部功=35·Σvx·dt（推力窗躯干位移近似口径，注释写明），
     报告 ΔE/E（阈值 5%），值进 telemetry 自定义块 energy_audit 与
     innovation.json，超限 episode 如实标记不删。
  g) innovation.json 机产: {"control_law": 非空字符串,
     "dr_distribution": 非空对象(本批真实 ζ/Kp/Kd/μs/μk/材质级采样与
     ζ 修复率统计), "eliminated_assumptions": 字符串数组(可选)}。
```
"""

# ---------------------------------------------------------------------------
# 四块固定划分（n 固定 4，禁止运行时由模型自报块数——防模型逃单块）
# ---------------------------------------------------------------------------

V2_CHUNK_TITLES: tuple[str, ...] = (
    "头部/常量/V2 参数化与 CLI",
    "rollout 场景与物理注入",
    "IK/控制器/主循环/遥测/能量审计",
    "batch 编排与五产物",
)

# 每块边界文案（行数预算 + 只允许写的章节）；runner 在 chunk 清单与本块
# 边界两处原样渲染。键顺序与 V2_CHUNK_TITLES 一一对应。
_CHUNK_BOUNDARIES: tuple[str, ...] = (
    "行数预算：约 180 行。只允许写的章节：模块 docstring、imports、全部常量；"
    "V2 a)-f) 的 draw_params/RNG 采样（ζ 条件化 Kd 反解 clip[15,2000]、"
    "μs/μk/vs、材质梯 forced_for_ladder_coverage、关节 DR、能量审计字段）；"
    "argparse（rollout|batch；batch 的 --out-dir/--seeds 默认 3/"
    "--calibration-json）；calibration 拷贝（samefile 守卫）。"
    "不得写场景初始化、控制器主循环、batch 编排。",
    "行数预算：约 220 行。只允许写的章节：gs.init/Scene/Plane/MJCF/相机、"
    "关节与 link 索引、set_friction 双足、set_sol_params(ground+feet)、"
    "settle、35N×0.2s 每子步外力、ice_dr wrench、contacts helper。"
    "不得重复块 0 的 import/常量，不得写 IK/步态/控制器主循环、batch 编排。",
    "行数预算：约 260 行。只允许写的章节：leg_ik、capture-point 步态全部"
    "参数、PD 更新、400 帧采集 9 键、跌倒判定、outcomes（slip/contact "
    "全字段）、energy_audit 块。不得重复块 0/1 的常量、场景 API 与物理注入，"
    "不得写 argparse 与 batch 编排。",
    "行数预算：约 200 行。只允许写的章节：batch 编排与五产物——父进程不 "
    "import genesis、[sys.executable, abspath(__file__), ...] 子进程、"
    "CSV 35 列 9 行、summary groups 四指标、plots/collage/splits/cover、"
    "manifest、innovation.json{control_law,dr_distribution}、SUMMARY.md。"
    "不得重复块 0-2 的采样/场景/控制器代码。",
)


def build_chunk_boundary(i: int) -> str:
    """返回第 i 块（0 起）的边界与行数预算文案；越界 fail-loud。"""
    if not isinstance(i, int) or isinstance(i, bool) \
            or not 0 <= i < len(_CHUNK_BOUNDARIES):
        raise ValueError(f"chunk 边界下标非法：{i!r}（共 {len(_CHUNK_BOUNDARIES)} 块）")
    return _CHUNK_BOUNDARIES[i]
