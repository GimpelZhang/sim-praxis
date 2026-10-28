#!/usr/bin/env python3
"""Task8 Q5 厨房倒水拆块出生（chunked birth）六块固定划分——纯文本常量模块。

硬规则 3 合规声明：本模块**只**含拆块出生所需的提示词契约文本（块标题、
边界与行数预算的中文描述、CLI 参数名、产物文件名、Genesis API 名称序列），
**不是解题脚本**：不 import genesis/torch、不构造场景、不产生任何
results.json/trajectory.npz/video.mp4。引擎层自始至终不携带
genesis_fluid_pour.py 本体；它只能在 Gate1 批准后的付费 workdir 内由 runner
拆块出生，Gate2 真人验收后才回收进 KB 内容层
solutions/track2_questions/03_kitchen_pour/。

锁内适配（计划 v1.2 Task8 外，2026-10-27，T8-D2 账外取证批次）：
kimi-k3.1-pro 经硬规则 1 用户逐字裁决成为 coding_debug 模型（b20c45e）后，
T8-D2 探针矩阵证实它在方舟 anthropic-messages shim 上对完整流体出生合同
**单次出活结构性不可行**——
- probe B（缺省思考形态）：792s/32758 SSE 事件/128466 思考字符/正文 0 字/
  stop=max_tokens（32768）；
- probe E（thinking={"type":"disabled"} + temperature=0.6，供应侧唯一接受
  的成对约束，C1 证实 disabled+temp1 被 400 拒、C4 证实小调用 1.9s 收敛）：
  605.1s/32759 事件/126682 字符规划独白**改走 text 通道**/stop=max_tokens，
  JSON 补丁未开始即截断。
同款模型在小编排任务上可收敛（C2/C4，2-4s）。Task6 V2 自 seq42-47 起已有
拆块出生家法（runner 六块 sentinel 围栏通道，n=4→6 是工厂规格变更，见
task6_v2_prompts.py 与 .omc/handoffs/task6-sentinel-seam-design.md），本模块
是该家法在 Task8 流体出生的工厂规格：n 固定 6、块标题/边界/行预算起飞前
冻结，撞窗按预注册结案（硬规则 7），n=7/调窗属新提案。不涉任何 model_id
变更（硬规则 1 不触发、kimi-k3.1-pro 不动）。

行预算 170/200/170/200/150/150 合计约 1040 行（30000 拼接地板之上、各块
字符量在 runner 800/18000/36000 三窗内）；边界只允许写章节级声明，不提供
任何 Genesis 实现代码——流体场景的创造性工作全部属于出生模型。
"""
from __future__ import annotations

# n 固定来自工厂（runner.ChunkSpec 构造即校 chunk_count==len(titles)
# ==len(boundaries)）；禁止运行时由模型自报块数（防模型逃单块回到 probe E
# 的单次截断路径）。
FLUID_CHUNK_COUNT = 6

FLUID_CHUNK_TITLES: tuple[str, ...] = (
    "模块头/常量/CLI 参数/结构化失败与看门狗工具",
    "Genesis 初始化/厨房场景几何/SPH 求解器/目标杯",
    "SPH 液体采样/源容器装载/倾倒运动学",
    "步进主循环/VRAM 看门狗/两次参数降级/轨迹采集",
    "EGL 离屏渲染/video.mp4/cover.png",
    "产物落盘/main 入口/退出码",
)

# 每块边界文案（行数预算 + 只允许写的章节）；runner 在拆块清单与本块边界
# 两处原样渲染。键顺序与 FLUID_CHUNK_TITLES 一一对应。
_CHUNK_BOUNDARIES: tuple[str, ...] = (
    "行数预算：约 170 行。只允许写的章节：模块 docstring、imports、全部常量"
    "（results schema 逐字 simpraxis.task8.fluid_results.v1、产物文件名、"
    "50Hz 采样、沉降窗秒数、几何字段名，以及合同 v2 的全部阈值常量：壶嘴"
    "漂移 0.010m、壶嘴合速度 0.20m/s、角速度 80°/s、静置 0.3s、转动历时"
    " 1.5s、爆炸护栏范数 2.5m 与坏粒子比例 2%——数值必须逐字取自合同）；"
    "argparse（只接外层固定命令行的"
    "六个参数：--particle-cap/--vram-abort-gb/--substep-start/"
    "--settling-window-s/--thread/--run-idx；粒子数超过硬天花板即 parser.error"
    "拒绝）；只写当前工作目录的路径助手；原子写 JSON 工具；结构化"
    "write_failure（traceback 原文 + 已尝试降级次数，非零退出配套）；"
    "OOM/VRAM 文本判定助手。"
    "不得写 gs.init/场景/实体、倾倒控制、步进循环、渲染、results/trajectory/"
    "manifest 的最终落盘与 main。",
    "行数预算：约 200 行。只允许写的章节：Genesis 初始化（gs.init GPU 后端、"
    "GS_ENABLE_ZEROCOPY=0 等环境前提只读取不重设）、gs.Scene/SimOptions/"
    "SPHOptions（用 Stage0 反射证实的 SPH 求解器 API，不得硬编别的求解器）、"
    "仿真域边界、厨房静态几何（台面等刚体）、**源容器**（壶/罐类刚体）与"
    "**目标杯**刚体几何；**合同 v2：源容器必须是单体刚体**——本块用"
    "程序化 trimesh 构造开口空心壶网格（壁+底一体 watertight，STL/OBJ 写"
    "当前工作目录），经一次 gs.morphs.Mesh(file=该网格, fixed=True, "
    "decompose_nonconvex=True) + 一次 add_entity 创建（凹面盛水必须开非凸"
    "分解），壶实体句柄全脚本恰一个；**严禁**多个独立 Box/Cylinder 拼壶或"
    "只驱动一组实体中的部分；目标杯的 inner_radius/bottom_z/rim_z 必须是本块"
    "定义的原始几何常量（后续 results.json 原样自报这三个数，机判由引擎按"
    "同几何从轨迹复算——禁止在任何地方自算入杯率/守恒等衍生比率）；"
    "EGL 离屏相机（res/pos/lookat/fov，禁止 glfw/窗口/viewer）；scene.build。"
    "不得重复块 0 的 import/常量/CLI；不得写液体材料与采样、倾倒运动学、"
    "步进循环、渲染帧写入与产物落盘。",
    "行数预算：约 170 行。只允许写的章节：SPH.Liquid 材料参数与粒子采样器"
    "（在源容器内腔体积内布粒子，粒子数受 CLI 粒子预算约束、绝不超硬天花板）；"
    "液体实体创建与初始静置；源容器**倾倒运动学**——时间→俯仰角的函数"
    "（起始直立、匀速/平滑加速倾斜、液体流出、回正），把位姿逐帧下发给"
    "块 1 那个单体壶实体（fixed 运动学 set_pos/set_quat）；**绕壶嘴尖点"
    "铰链**（倾斜/停留窗壶嘴世界漂移 ≤0.010m、合速度 ≤0.20m/s），起倒前"
    "直立静置 ≥0.3s，θ=0→≥120° 历时 ≥1.5s、smoothstep 平滑、角速度"
    " ≤80°/s，禁瞬移；倾倒起止时刻常量。**必须在本块实现两个自检函数："
    "（a）「倾倒运动学构造性合同 + 合同 v2」的 kinematic_precheck()（纯"
    "CPU、不建场景：trimesh 加载壶网格确认三角面 >0，并按 θ(t)/位姿解析式"
    "全程采样+数值求导，逐条核验真倾倒/壶嘴对口 ρ≤0.035 与嘴高 "
    "1.025–1.07/壶杯间隙≥0.012/壶嘴漂移 0.010/合速度 0.20/角速度 80/"
    "静置 0.3/历时 1.5，违规结构化 FAILURE）；（b）sim_blowout()（仿真后"
    "纯 numpy 读 positions：NaN/Inf 或距原点 >2.5m 的粒子比例 >2% 即"
    " kind=sim_blowout 结构化 FAILURE；只认物理爆炸，严禁按入杯率触发）。**"
    "不得重复块 0/1 的常量、初始化与几何定义；不得写步进主循环与 VRAM 看门狗"
    "（属块 3）、相机帧渲染与视频编码（属块 4）、results/trajectory 落盘与"
    "main（属块 5）。",
    "行数预算：约 200 行。只允许写的章节：仿真步进主循环——substeps 起点取"
    "CLI 的 --substep-start；按 50Hz 抽帧；每子步前下发倾倒位姿；每子步后"
    "采样 torch.cuda.max_memory_allocated()（采样后 reset_peak），超过"
    "CLI 的 --vram-abort-gb 立即优雅中止（结构化 FAILURE + 非零退出，绝不"
    "放任 OOM 崩溃）；参数下调最多 CLI 给定次数（先砍粒子预算重建、再砍"
    "子步，两次降级必须重新初始化场景而非热改）；每帧取全部粒子位置攒"
    "(F,N,3) float32，N 必须等于自报 n_particles，逐帧 finite 检查，NaN/Inf"
    "即失败；末沉降窗至少覆盖 --settling-window-s（50Hz 下 25 帧）。"
    "不得重复块 1/2 的场景几何与倾倒函数定义；不得写相机帧编码/视频落盘"
    "（属块 4）、results/trajectory/manifest 写盘与 main（属块 5）。",
    "行数预算：约 150 行。只允许写的章节：EGL 离屏渲染全过程——倾倒→流动→"
    "入杯/飞溅必须在画面中可辨，透明/半透明液体要调到人眼可辨液面与入杯"
    "瞬间（材质/光照/背景对比手段自选，但不得改物理参数去迁就画面）；"
    "imageio/imageio-ffmpeg 写 video.mp4（H.264/yuv420p 类通用编码），帧数与"
    "仿真帧一致；末帧写 cover.png。视频/封面只写当前工作目录、与本次运行"
    "同一次产出（禁止复用旧片、禁止占位图）。"
    "不得重复块 1 的相机构造与块 3 的步进循环本体（只允许在其帧点上取图的"
    "必要接线）；不得写 results/trajectory/manifest 数据产物与 main（属块 5）。",
    "行数预算：约 150 行。只允许写的章节：五件必写产物的最终落盘与 main——"
    "results.json 只含 schema/solver_api/n_particles/frame_dt_s/cup 五段"
    "原始自报（**禁止** in_cup_ratio/entry_ratio_median/splash_ratio/"
    "out_of_bounds_ratio/conservation/cup_entry_rate 等任何衍生比率字段，"
    "出现即整线程结案）；trajectory.npz 用 np.savez_compressed(positions="
    "<float32 (F,N,3)>)；manifest.json 为 {n_frames, frame_dt_s} 且 "
    "frame_dt_s 与 results.json 一致；main 必须**最先**调用块 2 的 "
    "kinematic_precheck()（在任何 gs.init/scene.build/实体创建之前；不通过即"
    "非零退出），随后依次接线初始化→（降级重试）→步进→**步进结束立即调用"
    "块 2 的 sim_blowout()**（爆炸即非零退出，不得带爆炸结果进入渲染/成功"
    "结论）→渲染→落盘，缺任何"
    "一件产物 rc 必须非零，全部成功才打印 SUCCESS 行"
    "并 rc=0；禁止联网/写 workdir 外路径/改 site-packages/改外层环境变量。"
    "不得重复块 0-4 的场景、采样、控制、步进与渲染实现，只允许调用它们。",
)


def build_chunk_boundary(i: int) -> str:
    """返回第 i 块（0 起）的边界与行数预算文案；越界 fail-loud。"""
    if not isinstance(i, int) or isinstance(i, bool) \
            or not 0 <= i < len(_CHUNK_BOUNDARIES):
        raise ValueError(
            f"chunk 边界下标非法：{i!r}（共 {len(_CHUNK_BOUNDARIES)} 块）")
    return _CHUNK_BOUNDARIES[i]
