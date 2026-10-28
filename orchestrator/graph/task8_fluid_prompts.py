"""Phase 2 Task8 流体（Q5 厨房倒水）coding 出生合同文本。

单一来源家法同 ``task7_libero_prompts``/``task6_v2_prompts``：合同文本与装配
代码分文件，防 ``task8_fluid.py`` 膨胀；本模块只有字符串与纯格式化函数
（零网络/零付费/零文件系统访问——stage0_facts_text 只吃 gate1_proposal
已装载的机器事实）。

出生语义（与 task7 libero 同构、task6 V2 家法）：
``make_spec_factory`` 在方案尚未回收进 KB 内容层
（``solutions_root()/track2_questions/03_kitchen_pour`` 不存在）时，cmd 指向
出生时不存在的 ``genesis_fluid_pour.py``，首次 exec 必然「文件不存在」失败；
coding 环按合同在付费 workdir 出生脚本——引擎仓/KB 仓在 Gate2 回收前都不
持有该文件。回收后同工厂走 resolve+stage 逐件 sha256 路径，合同不再注入。

交付通道（攢批93，2026-10-27，T8-D2 账外取证后的锁内适配）：kimi-k3.1-pro
在方舟 anthropic-messages shim 上对完整出生合同单次出活结构性不可行
（probe E：thinking disabled+temp0.6 下 605.1s/126682 字规划独白走 text
通道/stop=max_tokens，补丁未开始即截断；同源 probe B 默认形态 792s/0 正文）。
故出生态固定走 runner n=6 拆块通道（``chunk_birth_contract`` +
``task8_fluid_chunks`` 六标题/六边界），与 Task6 V2 run4 家法同形；
``stage_birth_contract``（单次 JSON 补丁版）保留为契约文本与测试冻结件，
工厂不再注入。两版共享 ``_requirement_blocks``，机检要求逐字同一份。

85% 入杯语义（计划 L604-606，逐字进合同）：末 0.5s 沉降窗逐帧中位；严格
几何 r < inner_radius 且 bottom_z < z < rim_z（贴壁/贴底/贴口不入杯）；
飞溅（z ≥ rim_z）与越界（r ≥ inner_radius 侧壁外 / z ≤ bottom_z 杯底以下）
分列。数字全部由引擎从 trajectory.npz 现算（探针几何函数单点），脚本只报
最原始几何参数与轨迹，禁止自报任何衍生比率。
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# results.json 原始契约（出生脚本唯一必须自报的结构化产物之一）
#
# 引擎侧 evaluate（task8_fluid.make_evaluate_seam）读它并经
# build_wj_fluid_report 组装 simpraxis.wj_fluid_report.v1——入杯率/守恒/
# 飞溅/越界由引擎现算（数字不采信脚本自报的衍生值），脚本只报最原始的
# 几何参数与计数。
# ---------------------------------------------------------------------------

RESULTS_SCHEMA = "simpraxis.task8.fluid_results.v1"

# 目标杯冻结几何（Q5 厨房倒水场景原始常量；results 契约示例与运动学构造性
# 合同同一单点，禁止他处再写字面量）。出生脚本块 1 必须把这三个数定义为
# 自身的原始几何常量并原样自报；机判按同几何从轨迹复算。
CUP_INNER_RADIUS = 0.045
CUP_BOTTOM_Z = 0.92
CUP_RIM_Z = 1.02

# 构造性合同 v2（攢批102，2026-10-28 run_4 真人卡#4 否决后）：run_4 运动学
# 不等式全过但壶=五个独立非 fixed Box、驱动器只驱动到一块，自由壶壁被 SPH
# 打飞（真人「红薄板飞起/插入杯中」）、粒子 98% 越界。下列阈值把「单体
# 刚体壶 + 绕嘴铰链慢动力学」改成出生前纯 CPU 可验/跑完后近零成本护栏。
VESSEL_LIP_DRIFT_MAX_M = 0.010      # 倾斜段壶嘴世界位置漂移上界（绕嘴铰链）
VESSEL_LIP_SPEED_MAX_MPS = 0.20     # 倾斜段壶嘴合速度上界
VESSEL_TILT_ANG_SPEED_MAX_DPS = 80  # 俯仰角速度上界（度/秒）
VESSEL_SETTLE_MIN_S = 0.3           # 起倒前重力静置最短时长
VESSEL_TILT_MIN_S = 1.5             # θ=0→≥120° 转动历时下界
SIM_BLOWOUT_R_M = 2.5               # 爆炸护栏：粒子离台面世界原点范数上界
SIM_BLOWOUT_FRAC_MAX = 0.02         # NaN/超界粒子比例上界（与 85% 判据无关）


def _results_contract_json() -> str:
    """results.json 形态示例 + 机检规则（嵌入出生合同正文）。"""
    example = {
        "schema": RESULTS_SCHEMA,
        "solver_api": "SPH.Liquid",
        "n_particles": 12000,
        "frame_dt_s": 0.02,
        "cup": {"inner_radius": CUP_INNER_RADIUS, "bottom_z": CUP_BOTTOM_Z,
                "rim_z": CUP_RIM_Z},
    }
    return json.dumps(example, ensure_ascii=False, indent=2)


def _single_patch_intro(script_name: str, cmd_line: str) -> str:
    """单次 JSON 补丁交付导语（冻结件；工厂出生态自攢批93 起改走拆块版）。"""
    return f"""\
本工作目录将出生 `{script_name}`（Q5 厨房倒水流固耦合，Genesis 粒子流体）。
命令行已被外层固定：`{cmd_line}`。
用一次 JSON 补丁创建该文件：`{{"patches":[{{"path":"{script_name}","old":"","new":"<完整源码>"}}]}}`
（old 空串=新文件出生；path 是相对本工作目录的字面名）。"""


def _chunk_intro(script_name: str, cmd_line: str, n_chunks: int) -> str:
    """拆块交付导语（攢批93）：交付协议改 sentinel 围栏，机检要求不变。"""
    return f"""\
本工作目录将出生 `{script_name}`（Q5 厨房倒水流固耦合，Genesis 粒子流体）。
命令行已被外层固定：`{cmd_line}`。
完整源码由 {n_chunks} 个顺序调用拆块出生：你每次只输出《拆块交付》段分配给
本块的 Python 原文，严格按该段 <<<<<<< simpraxis.chunk.v1 围栏三段结构
（首行起始围栏、中间多行 Python 原文零转义、末行结束围栏；禁止 JSON、
禁止 markdown 代码围栏、围栏外禁止任何解释或思考独白——起始围栏之前出现
任何正文即本机校验失败）。引擎本地按序拼接六块后，以单个
{{"patches":[{{"path":"{script_name}","old":"","new":<拼接后的完整源码>"}}]}}
补丁（old 必须是空串）创建文件；以下机检要求针对拼接后的完整文件逐条生效，
任何一条不符整线程结案。"""


def _kinematic_contract_block() -> str:
    """倾倒运动学构造性合同（三次真人 Gate2 否决后，2026-10-28 强化）。

    三次失败：run10 全程静止（壶不倾、0 入杯）；run_2 真倾倒但壶杯穿模、
    74% 粒子漏桌（0.258 入杯）；run_3 为避穿模把壶拉开→又回到壶杯不动、
    98.7% 漏桌（0 入杯，真人「甚至不如上一轮」）。共性是模型自由发挥的
    倾倒几何不可靠。本块**不提供任何 Genesis 实现代码**（硬规则 3：引擎层
    不携带解题脚本），只把「真倒 / 不穿模 / 水入杯口」改写成出生脚本必须
    在**场景构建之前**用纯 CPU 解析自检的数值不等式，违规即结构化 FAILURE，
    给修复轮近零成本的确定性反馈，而不是烧 GPU + 真人眼才发现。
    """
    lip_r = round(CUP_INNER_RADIUS - 0.010, 3)      # 流出窗壶嘴水平半径上界 0.035
    z_lo = round(CUP_RIM_Z + 0.005, 3)             # 壶嘴最低 1.025（不入杯腔）
    z_hi = round(CUP_RIM_Z + 0.05, 3)              # 壶嘴最高 1.07（水柱短、抑飞溅）
    return f"""\
## 倾倒运动学构造性合同（三次真人否决后强化；最高优先级，仅次于人工评审意见）
目标杯冻结几何：杯口平面 z={CUP_RIM_Z}、内壁半径 R={CUP_INNER_RADIUS}、杯轴竖直、
杯底 z={CUP_BOTTOM_Z}。源容器（壶）设「倾倒俯仰角」θ=壶体轴与竖直方向夹角
（直立 θ=0°），壶嘴尖点记 P(t)。你的倾倒运动学必须让下面全部不等式**由构造
满足**，而不是寄望物理把水溅进杯子：
1. **真倾倒（禁静止）**：θ(t) 必须连续非恒常——全程 max θ−min θ ≥ 120°，
   最大俯仰角 θ_max ≥ 120°；且存在长度 ≥0.5s 的连续「流出停留窗」整段
   θ(t) ≥ 105°（壶嘴低于液面、水可持续流出）；θ 单调平滑变化（分段线性/
   平滑插值，禁止瞬时跳变/瞬移），起倒与回正对称收尾。
2. **壶嘴对准杯口（禁漏桌）**：在整个流出停留窗内，壶嘴尖点 P(t) 的水平投影
   到杯轴距离 ρ(t) 必须 ≤ {lip_r}（=R−0.010，留 1cm 贴壁余量），使水流柱
   全程落进口面；壶嘴高度必须满足 {z_lo} ≤ z_P(t) ≤ {z_hi}（始终在杯口平面
   上方 0.5–5cm：水柱短以抑飞溅，且壶嘴绝不探进杯口平面以下）。
3. **壶杯不穿模（构造性间隙）**：除壶嘴尖点 P 外，壶体任何部分在任何时刻都
   不得进入柱体「ρ<R 且 z<{CUP_RIM_Z}+0.005」（杯口平面以下的杯内腔与杯壁
   同柱）；在流出窗每个采样点，壶体包围体与杯壁圆柱面/杯口平面的解析最小
   间隙 ≥0.012m。实现时把壶与杯各表示为简单原始体（盒/圆柱），用解析式算
   有符号距离，不得依赖渲染或物理碰撞事后发现。
4. **出生前强制自检**：必须实现 `kinematic_precheck()`——**不建 gs 场景、不
   初始化 SPH、不用 GPU**，仅按你的 θ(t)/位姿解析式在整条时间轴上密集采样
   （50Hz 全程，至少每个子步一个点），逐条数值核验上述 1/2/3；任一不满足
   立即写结构化 FAILURE（沿用 simpraxis.task8.fluid_failure.v1，
   kind="kinematic_precheck"，error 写明违反第几条与见证数值，如
   min 间隙/ρ_max/θ_max/停留窗时长）并非零退出。自检在 main 中、任何
   scene.build/实体创建**之前**执行；它不产生任何产物、**禁止**把这些中间
   量写进 results.json（衍生比率仍一律由引擎现算）。物理仿真只有在自检全过
   后才允许开始。"""


def _vessel_structure_contract_block() -> str:
    """源容器结构与慢动力学构造性合同 v2（攢批102，run_4 卡#4 后强化）。

    run_4 终态脚本逐行取证：上一合同的壶嘴轨迹不等式全过（纯 CPU precheck
    真开火拦住了 3.6mm 超调），但壶被实现为五个独立非 fixed 动态 Box、
    1.3.3 无 gs.constraints 可焊，逐帧驱动器靠不存在的 .parts 属性取集合，
    实际只驱动一块，其余壶壁作为自由刚体被 SPH 打飞（真人见「红薄板飞起/
    插入杯中」）；粒子在 lift/slide 段失去容纳，98% 越界、1.1% 入杯。
    本块同样**不含任何 Genesis 实现代码**（硬规则 3），只把结构完整性与
    慢动力学改成构造要求 + 出生前纯 CPU 自检/跑完后近零成本 numpy 护栏。

    C6（攢批103，用户裁决「C6合同强化后再战」）：run_5 首战出生脚本壶
    网格非流形（is_watertight=False：边被 3 面引用），出生前自检 8 轮帽
    熔断、零 GPU 未发卡#5；rehang 后被 callcache 字节级重放（0 实呼）。
    强化只加判据不给拓扑做法：第 5/8 条新增「每无向边恰两面」边流形判据
    + 边界边/非流形边计数见证（指纹变化自然失配重放缓存）。
    """
    return f"""\
## 源容器结构与慢动力学构造性合同 v2（run_4 卡#4 后强化；与上一运动学合同同优先级）
上一合同的轨迹不等式在 run_4 全部通过，但实跑散架：壶是五个独立非固定
Box、驱动器只驱动到其中一块，自由壶壁被流体打飞、粒子在壶平移阶段就失控。
以下要求必须**由构造满足**，并纳入出生前自检：
5. **单体刚体壶（严禁散件）**：壶必须是**单一刚体实体**——用程序化
   trimesh（venv 已装 trimesh 5.1.1）在当前工作目录生成开口空心壶网格
   （壁+底一体、watertight：**每条无向边恰被两个三角形引用**、无边界边、
   无重复面、法向一致；自检不过即由生成代码自行重构网格拓扑直到满足，
   STL 或 OBJ 落本 workdir），经**一次**
   scene.add_entity(morph=gs.morphs.Mesh(file=<该网格>, fixed=True,
   decompose_nonconvex=True)) 创建（coacd 非凸分解已装，凹面盛水必须开，
   凸包化会封死壶口）；脚本里壶只允许这一个实体句柄（自检断言壶 add_entity
   恰一次）。**严禁**用多个独立 Box/Cylinder/实体拼壶，严禁一组实体只驱动
   其中几个。目标杯五壁保持 fixed=True 世界静止（run_4 杯结构正确，沿用）。
6. **运动学驱动 + 绕嘴铰链 + 慢动力学**：壶作为 fixed 运动学壁逐帧
   set_pos/set_quat 下发位姿；倾斜必须**绕壶嘴尖点铰链**——整个倾斜与
   流出停留窗内壶嘴世界位置漂移 ≤{VESSEL_LIP_DRIFT_MAX_M}m、合速度
   ≤{VESSEL_LIP_SPEED_MAX_MPS} m/s；起倒前先在直立姿态重力静置
   ≥{VESSEL_SETTLE_MIN_S}s（粒子在壶内收敛，禁止边出生边运动/先平移后
   粒子就位）；θ=0 到 θ≥120° 转动历时 ≥{VESSEL_TILT_MIN_S}s、
   smoothstep 类平滑、角速度 ≤{VESSEL_TILT_ANG_SPEED_MAX_DPS}°/s，
   禁止瞬移与逐帧大跳。仅当实测 fixed 运动学壁不与 SPH 碰撞耦合时，允许
   把这**同一个单体实体**改 fixed=False 并逐帧同变换驱动（此备选为本合同
   明文许可，禁止临场发明第三种壶结构或驱动法）。
7. **物理爆炸护栏 sim_blowout()（近零成本，与 85% 判据无关）**：仿真
   结束后用纯 numpy 读 trajectory positions，若 NaN/Inf 粒子或
   ‖position‖>{SIM_BLOWOUT_R_M}m（台面世界原点）的粒子比例
   >{SIM_BLOWOUT_FRAC_MAX:.0%}，立即写结构化 FAILURE
   （simpraxis.task8.fluid_failure.v1，kind="sim_blowout"，见证值=坏粒子
   比例与最大范数）并非零退出。此护栏**只**认物理爆炸：严禁以入杯率高低
   触发、严禁据此调任何物理/几何阈值（85% 目标冻结，矛盾即结论）。
8. **kinematic_precheck() 必须扩验第 5/6 条可静态验证部分**：用 trimesh
   加载第 5 条网格文件（不建 gs 场景、不用 GPU）确认文件存在、三角面数 >0
   且开口/壁厚几何可读；**并逐边核验网格边流形——对全部无向边统计每个边
   被多少个三角形引用，恰为 2 才通过，引用数为 1 的是边界边、≥3 的是非
   流形边，两类都计数，任一存在连同 mesh.is_watertight=False 即不通过**
   （只给判据：必须自行重构生成拓扑，禁止在自检里跳过/放宽该判据）；
   再按位姿解析式在整条时间轴数值求导，逐条核验
   壶嘴漂移 ≤{VESSEL_LIP_DRIFT_MAX_M}m、合速度 ≤{VESSEL_LIP_SPEED_MAX_MPS}
   m/s、角速度 ≤{VESSEL_TILT_ANG_SPEED_MAX_DPS}°/s、静置 ≥{VESSEL_SETTLE_MIN_S}s、
   转动历时 ≥{VESSEL_TILT_MIN_S}s；任一不过写 kind="kinematic_precheck"
   结构化 FAILURE（见证值：面数/边界边数/非流形边数/is_watertight/
   漂移 max/速度 max/角速度 max/静置/历时），
   仍在任何 gs.init/scene.build 之前、非零退出。"""


def _requirement_blocks(*, stage0_facts_text: str,
                        particle_cap: int, vram_abort_gb: float,
                        max_reductions: int, settling_window_s: float,
                        cup_entry_target: float) -> str:
    """单次/拆块两版合同共享的机检要求正文（唯一来源，禁止分叉）。"""
    return f"""\
## 运行环境（已由引擎注入，勿改）
- 解释器是共享 venv（genesis-world 1.3.3 / torch 2.4.1+cu124 已装）；
- GS_ENABLE_ZEROCOPY=0（默认 zerocopy 路径 int32 gather 抛错的必需正确性开关）、
  PYOPENGL_PLATFORM=egl：无头 EGL 渲染，禁止 glfw/窗口；
- 命令行无 --out-dir：产物一律写当前工作目录（=本 workdir）；
- 禁止联网（不下载数据集/权重）；禁止写本工作目录以外路径；禁止改
  site-packages；禁止改写外层已注入的环境变量。

## 脚本职责（stage1_pour）
- 用 Stage0 已反射证实的流体求解器 API（见 Stage0 事实块 solver_api）建
  厨房倒水场景：源容器倾斜→液体粒子流动→目标杯接收；
- 杯几何必须是脚本自报的**原始参数**（见 results 契约 cup 段）——r/z 严格
  边界，机判由引擎按同一几何从轨迹复算；
- 粒子上限 {particle_cap} 是硬天花板：任何参数组合都不得超
  （CLI 层 {particle_cap + 1} 即拒）；
- VRAM 看门狗：每个子步采样 torch.cuda.max_memory_allocated()，超过
  {vram_abort_gb} GB 立即优雅中止——写结构化 FAILURE 说明 + 非零
  退出，绝不放任 OOM 崩溃；参数下调（先砍粒子预算、再砍子步）最多
  {max_reductions} 次；
- 渲染：EGL 离屏把全过程写成 mp4（可 imageio/imageio-ffmpeg），透明/半透明
  液体调到人眼可辨液面与入杯瞬间；
- 任何一步崩/看门狗中止/产物写不出 → 非零退出 + stderr 原文，禁止伪造成功。

## 必写产物（全部相对当前工作目录；缺任何一件 rc 必须非零）
1. `results.json`——最原始自报（唯一结构化自报产物；入杯率/守恒/飞溅/越界
   全部由引擎从轨迹现算，**禁止**自报任何衍生字段——in_cup_ratio/
   entry_ratio_median/splash_ratio/out_of_bounds_ratio/conservation/
   cup_entry_rate 等出现即整线程结案）：
{_results_contract_json()}
   机检规则（引擎组装 wj_fluid_report 时 fail-closed 复验）：
   - schema 逐字匹配 "{RESULTS_SCHEMA}"；
   - solver_api 非空（与 Stage0 反射证实的 API 一致）；
   - n_particles 为 1..{particle_cap} 整数；
   - frame_dt_s 为正有限实数；
   - cup 为 {{inner_radius>0, bottom_z, rim_z>bottom_z}}（有限实数）。
2. `trajectory.npz`——`np.savez_compressed(path, positions=<float32 (F,N,3)>)`；
   N 必须 == n_particles，全部坐标有限；F 帧必须放得下沉降窗（末
   {settling_window_s}s，50Hz 下 25 帧）。
3. `video.mp4`——倾倒→流动→入杯/飞溅全过程渲染（Gate2 真人裁决媒体，必写）。
4. `cover.png`——末帧封面（必写）。
5. `manifest.json`——{{"n_frames": <int>, "frame_dt_s": <float>}}（与
   results.frame_dt_s 必须一致；引擎免解码取时长）。

## Stage0 流体能力事实（容器探针产物，非模型文案；据此选求解器 API）
{stage0_facts_text}

## 85% 入杯语义（机判口径，引擎按此从轨迹复算；Gate2 真人按此对质）
- 结果口径 = 末 {settling_window_s}s 沉降窗**逐帧入杯比例的中位数**
  （非末帧单点、非全程平均）；
- 严格几何：入杯 ⇔ r < inner_radius 且 bottom_z < z < rim_z——贴壁/贴底/
  贴口粒子不入杯；飞溅（z ≥ rim_z）与越界（侧壁外 + 杯底以下）分列单报；
- 目标：入杯率中位数 ≥ {cup_entry_target}；守恒机判（总数恒定/边界
  溢出 ≤2%/末帧入杯 ≥80%）由引擎三查；
- 视频与 results/trajectory 必须同一次运行产出（禁止复用旧片/旧轨迹）。

{_kinematic_contract_block()}

{_vessel_structure_contract_block()}"""


def stage_birth_contract(*, script_name: str, cmd_line: str,
                         stage0_facts_text: str,
                         particle_cap: int, vram_abort_gb: float,
                         max_reductions: int, settling_window_s: float,
                         cup_entry_target: float) -> str:
    """Stage1 出生合同正文·单次 JSON 补丁版（冻结件；工厂出生态走拆块版）。

    机检规则全部可被引擎侧复验（validate_wj_fluid_report / 组装器
    fail-closed）：任何一条不符 → evaluate GraphStateError → 线程
    human_failure 结案，出生脚本无第二次机会——故合同把每个必写产物与
    每个字段界都写死。数值参数由工厂从探针单点常量传入（本模块零依赖）。
    """
    return _single_patch_intro(script_name, cmd_line) + "\n\n" + \
        _requirement_blocks(
            stage0_facts_text=stage0_facts_text,
            particle_cap=particle_cap, vram_abort_gb=vram_abort_gb,
            max_reductions=max_reductions,
            settling_window_s=settling_window_s,
            cup_entry_target=cup_entry_target)


def chunk_birth_contract(*, script_name: str, cmd_line: str,
                         stage0_facts_text: str,
                         particle_cap: int, vram_abort_gb: float,
                         max_reductions: int, settling_window_s: float,
                         cup_entry_target: float,
                         n_chunks: int = 6) -> str:
    """Stage1 出生合同正文·n 拆块版（攢批93 起工厂出生态注入此版）。

    导语改 sentinel 围栏交付协议（runner._extract_chunk 解析），机检要求
    与单次版逐字共享 _requirement_blocks——任何分叉都可能让模型面与机判
    口径漂移。
    """
    return _chunk_intro(script_name, cmd_line, n_chunks) + "\n\n" + \
        _requirement_blocks(
            stage0_facts_text=stage0_facts_text,
            particle_cap=particle_cap, vram_abort_gb=vram_abort_gb,
            max_reductions=max_reductions,
            settling_window_s=settling_window_s,
            cup_entry_target=cup_entry_target)


def stage0_facts_text(proposal: dict) -> str:
    """gate1_proposal 机器事实 → 合同/卡片共用的紧凑文本（调用方负责转义
    场景——合同走 prompt 原文、卡片走 cards.md_escape）。"""
    gpu = proposal.get("gpu") or {}
    versions = proposal.get("versions") or {}
    ver_line = "、".join(f"{k}={v}" for k, v in versions.items()) or "无"
    return "\n".join([
        f"outcome={proposal.get('outcome')}"
        f"（schema {proposal.get('stage0_schema')}）",
        f"solver_api={proposal.get('solver_api')}"
        "（反射发现，非硬编码；脚本必须用此 API）",
        f"n_particles={proposal.get('n_particles')}"
        f"（Stage0 上限；Stage1 硬天花板 {proposal.get('particle_cap')}）",
        f"gpu={gpu.get('name')}（peak {gpu.get('peak_mib')} MiB）",
        f"versions：{ver_line}",
        f"ts_utc={proposal.get('ts_utc')}",
    ])
