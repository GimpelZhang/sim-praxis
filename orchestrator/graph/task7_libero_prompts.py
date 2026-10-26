"""Phase 2 Task7 B 线（libero variant）coding 出生合同文本。

单一来源家法同 ``task6_v2_prompts``：合同文本与装配代码分文件，防
``task7_libero.py`` 膨胀；本模块只有字符串与纯格式化函数（零网络/零付费/
零文件系统访问——stage0_facts_text 只吃 gate1_proposal 已装载的机器事实）。

出生语义（锁内适配，与 task6 V2 同构、拆块改单次）：``make_spec_factory``
在方案尚未回收进 KB 内容层（``solutions_root()/track1_tools/03_libero``
不存在）时，cmd 指向出生时不存在的 ``stage1_minimal.py``/``stage2_suite.py``，
首次 exec 必然「文件不存在」失败；coding 环按下方合同以单次 JSON 补丁
（``old=""``）在付费 workdir 出生脚本——引擎仓/KB 仓在 Gate2 回收前都不
持有该文件。回收后同工厂走 resolve+stage 逐件 sha256 路径，合同不再注入。
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# results.json 原始契约（出生脚本唯一必须自报的结构化产物）
#
# 引擎侧 evaluate（task7_libero.make_evaluate_seam）读它并经
# build_wj_report 组装 simpraxis.wj_report.v1——聚合/成功率/来源集由引擎
# 现算（数字不采信脚本自报的衍生值），脚本只报最原始的逐任务计数。
# ---------------------------------------------------------------------------

RESULTS_SCHEMA = "simpraxis.task7.libero_results.v1"

# judge 取值枚举（出生脚本必须逐字其一；libero_official → WJ 官方标签）。
JUDGE_OFFICIAL = "libero_official"
JUDGE_SELF = "自补"


def _results_contract_json(stage: str, n_episodes: int) -> str:
    """results.json 形态示例 + 机检规则（嵌入出生合同正文）。"""
    example = {
        "schema": RESULTS_SCHEMA,
        "stage": stage,
        "suite_name": "libero_spatial",
        "n_episodes_per_task": n_episodes,
        "judge": JUDGE_OFFICIAL,
        "tasks": [
            {"task_id": "libero_spatial_<task_slug>",
             "bddl": "libero_spatial/<task_slug>.bddl",
             "success": 1, "episodes": n_episodes},
        ],
    }
    return json.dumps(example, ensure_ascii=False, indent=2)


def stage_birth_contract(*, stage: str, script_name: str, n_episodes: int,
                         stage0_facts_text: str) -> str:
    """Stage1/2 出生合同正文（extra_prompt_sections 的 body）。

    机检规则全部可被引擎侧复验（validate_wj_report / 组装器 fail-closed）：
    任何一条不符 → evaluate GraphStateError → 线程 human_failure 结案，
    出生脚本无第二次机会——故合同把每个必写产物与每个字段界都写死。
    """
    return f"""\
本工作目录将出生 `{script_name}`（LIBERO 第五平台 track，{stage}）。
命令行已被外层固定：`<隔离venv python> {script_name} --out-dir <本工作目录> --run-id <id>`。
用一次 JSON 补丁创建该文件：`{{"patches":[{{"path":"{script_name}","old":"","new":"<完整源码>"}}]}}`
（old 空串=新文件出生；path 是相对本工作目录的字面名）。

## 运行环境（已由引擎注入，勿改）
- 解释器就是隔离 venv（robosuite/bddl/libero 已 editable 安装于 tour 可写副本）；
- MUJOCO_GL=egl、PYOPENGL_PLATFORM=egl：无头 EGL 渲染，禁止 glfw/窗口；
- LIBERO_CONFIG_PATH 指向本工作目录 `.libero/`（config.yaml 已预置，
  bddl/assets 指向 tour 可写副本）——直接 `from libero import libero` 即可，
  禁止再写任何 config、禁止交互式 input()（容器无 TTY）；
- 禁止联网（不下载数据集/权重）；禁止写 --out-dir 以外路径；禁止改
  site-packages/tour 副本；禁止改写外层已注入的环境变量。

## 脚本职责（{stage}）
- 用 LIBERO 官方任务定义与官方成功判据跑 suite（bddl 任务集见 results
  契约；{stage} 范围内逐任务固定 {n_episodes} 局）；
- 每局成功判据取 LIBERO 原生 check_success（官方判分，禁止自造阈值）；
- 用 EGL 离屏渲染把全过程写成 mp4（可 imageio/imageio-ffmpeg）；
- 任何一局崩/渲染失败/产物写不出 → 非零退出 + stderr 原文，禁止伪造成功。

## 必写产物（全部相对 --out-dir；缺任何一件 rc 必须非零）
1. `results.json`——逐任务原始计数（唯一自报结构化产物；数字的聚合/比率
   由引擎现算，勿自报 success_rate/aggregate）：
{_results_contract_json(stage, n_episodes)}
   机检规则（引擎组装 wj_report 时 fail-closed 复验，违者整线程结案）：
   - schema/stage/judge 逐字匹配（judge 只允许 "{JUDGE_OFFICIAL}" 或 "{JUDGE_SELF}"；
     用了自补判据必须如实标 "{JUDGE_SELF}"，禁止冒充官方）；
   - 每任务 episodes == n_episodes_per_task；0 ≤ success ≤ episodes（整数）；
   - task_id 非空且互不重复；bddl 非空（任务定义可溯源到 BDDL 文件路径）；
   - 禁止出现 success_rate/aggregate 等衍生字段（引擎只认原始计数）。
2. `telemetry.json`——诚实自述（如 engine/run_id/dt）；若你完整给出共享
   物理契约键（dt/ee_positions/object_poses/goal_box/penetrations/
   contact_forces/contact_active/substeps_per_frame），引擎会对单任务 run
   附 Rung1–3 判定向量；给不齐则 Rung 聚合为 null（不判分，不算失败）。
3. `video.mp4`——全过程渲染（Gate2 真人裁决媒体，必写）。
4. `cover.png`——末帧封面（必写）。
5. `manifest.json`——{{"n_frames": <int>, "frame_dt_s": <float>}}（强烈建议：
   引擎免解码取时长；缺时回退解码）。

## Stage0 兼容事实（容器探针产物，非模型文案；据此选依赖形态）
{stage0_facts_text}

## 质量线（Gate2 真人按此对质，不达标即打回）
- 接触/抓取真实发生：无穿透、无瞬移、无悬空；物体到位后停稳；
- 成功率如实计入 results.json（失败局 success=0，禁止挑拣局数）；
- 视频与 results 同一次运行产出（禁止复用旧片）。"""


def stage0_facts_text(proposal: dict) -> str:
    """gate1_proposal 机器事实 → 合同/卡片共用的紧凑文本（调用方负责转义
    场景——合同走 prompt 原文、卡片走 cards.md_escape）。"""
    pins = proposal.get("pin_facts") or {}
    pin_line = "、".join(
        f"{k}={pins.get(k)}" for k in
        ("numpy_is_shared_pin", "torch_is_shared_pin",
         "mujoco_is_shared_pin", "mujoco_diverged"))
    reasons = proposal.get("reasons") or []
    notes = proposal.get("notes") or []
    return "\n".join([
        f"outcome={proposal.get('outcome')}"
        f"（schema {proposal.get('stage0_schema')}）",
        f"venv_path={proposal.get('venv_path')}",
        f"pin_facts：{pin_line}",
        "reasons：" + ("；".join(map(str, reasons)) if reasons else "无"),
        "notes：" + ("；".join(map(str, notes)) if notes else "无"),
        f"tour_dir={proposal.get('tour_dir')}",
        # LIBERO 实证 API/依赖事实（2026-10-26 run2h 撞墙后容器实测注入，
        # 锁内适配）：run2h 的模型 5 轮浪费在猜 API 名（benchmark_dict）与
        # 依赖（easydict/gym），从未到 env-sim。此段是**引擎侧实测真值**（容器
        # import 三连全绿取证），非模型猜测，供出生脚本一次写对官方 API：
        "libero_tour_api_facts（容器实测真值，2026-10-26）：",
        "  - 官方 benchmark API：from libero.libero.benchmark import "
        "get_benchmark_dict, get_benchmark（**不是** benchmark_dict；该 tour "
        "顶层只导出 get_benchmark_dict()/get_benchmark() 两函数，返回 "
        "BENCHMARK_MAPPING）。",
        "  - 官方环境类：from libero.libero.envs import OffScreenRenderEnv"
        "（venv 已补 easydict+gym==0.26.2，导入链已实测 import OK）。"
        "注意该 import 会触发 envs/venv.py 的 import gym 与 objects 的 "
        "easydict——二者为硬依赖，已在 venv 内装好，无需再 pip install。",
        "  - pin 未漂移：mujoco 3.15.0 / robosuite 1.4.0 / numpy 1.26.4 / "
        "torch 2.4.1+cu124；gym 0.26.2 与 gymnasium 1.4.0 共存（gym 的 "
        "deprecated warning 无害）。",
        "  - bddl 任务定义：tour 包内 libero/libero/bddl_files/"
        "{libero_spatial,libero_object,libero_goal,...}/*.bddl；config.yaml "
        "已由引擎在 .libero/ 预置（LIBERO_CONFIG_PATH 已注入），直接 "
        "OffScreenRenderEnv(bddl_file_name=...) 即可，勿交互式建 config。",
    ])
