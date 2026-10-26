"""Phase 2 Task8 流体（Q5 厨房倒水）coding 出生合同文本。

单一来源家法同 ``task7_libero_prompts``/``task6_v2_prompts``：合同文本与装配
代码分文件，防 ``task8_fluid.py`` 膨胀；本模块只有字符串与纯格式化函数
（零网络/零付费/零文件系统访问——stage0_facts_text 只吃 gate1_proposal
已装载的机器事实）。

出生语义（与 task7 libero 同构、task6 V2 家法、拆块改单次）：
``make_spec_factory`` 在方案尚未回收进 KB 内容层
（``solutions_root()/track2_questions/03_kitchen_pour`` 不存在）时，cmd 指向
出生时不存在的 ``genesis_fluid_pour.py``，首次 exec 必然「文件不存在」失败；
coding 环按下方合同以单次 JSON 补丁（``old=""``）在付费 workdir 出生脚本——
引擎仓/KB 仓在 Gate2 回收前都不持有该文件。回收后同工厂走 resolve+stage
逐件 sha256 路径，合同不再注入。

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


def _results_contract_json() -> str:
    """results.json 形态示例 + 机检规则（嵌入出生合同正文）。"""
    example = {
        "schema": RESULTS_SCHEMA,
        "solver_api": "SPH.Liquid",
        "n_particles": 12000,
        "frame_dt_s": 0.02,
        "cup": {"inner_radius": 0.045, "bottom_z": 0.92, "rim_z": 1.02},
    }
    return json.dumps(example, ensure_ascii=False, indent=2)


def stage_birth_contract(*, script_name: str, cmd_line: str,
                         stage0_facts_text: str,
                         particle_cap: int, vram_abort_gb: float,
                         max_reductions: int, settling_window_s: float,
                         cup_entry_target: float) -> str:
    """Stage1 出生合同正文（extra_prompt_sections 的 body）。

    机检规则全部可被引擎侧复验（validate_wj_fluid_report / 组装器
    fail-closed）：任何一条不符 → evaluate GraphStateError → 线程
    human_failure 结案，出生脚本无第二次机会——故合同把每个必写产物与
    每个字段界都写死。数值参数由工厂从探针单点常量传入（本模块零依赖）。
    """
    return f"""\
本工作目录将出生 `{script_name}`（Q5 厨房倒水流固耦合，Genesis 粒子流体）。
命令行已被外层固定：`{cmd_line}`。
用一次 JSON 补丁创建该文件：`{{"patches":[{{"path":"{script_name}","old":"","new":"<完整源码>"}}]}}`
（old 空串=新文件出生；path 是相对本工作目录的字面名）。

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
- 视频与 results/trajectory 必须同一次运行产出（禁止复用旧片/旧轨迹）。"""


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
