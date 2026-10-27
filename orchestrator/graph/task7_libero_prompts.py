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
- 控制器必须目标驱动（servo 闭环）：随机动作/无目标抖动已于 2026-10-26
  被真人 Gate2 拒批（「夹爪在无序抖动，没有伸向任何一个物体」），再犯即拒；
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
        "  - pin：mujoco 3.1.6（2026-10-26 用户裁决降版破 robosuite 1.4.0 "
        "API 断裂墙：3.15 无 MjData.qM、np.int32 in 枚举断言失败；3.1.6 两墙全消）"
        "/ robosuite 1.4.0 / numpy 1.26.4 / torch 2.4.1+cu124；gym 0.26.2 与 "
        "gymnasium 1.4.0 共存（gym 的 deprecated warning 无害）。",
        "  - get_benchmark/get_benchmark_dict 返回的是**类/工厂**（如 LIBERO_SPATIAL "
        "类），不是实例——须再调用实例化：`suite = get_benchmark('libero_spatial')"
        "()`（末尾一对括号），否则 `suite.get_task_names()` 报 missing self。"
        "实测 get_benchmark('libero_spatial') 返回 abc.ABCMeta 类，加 () 实例化后 "
        "get_task_names()/get_task_bddl_file_path(index) 正常（bddl 路径用 int 索引，"
        "非任务名字符串）。",
        "  - bddl 任务定义：tour 包内 libero/libero/bddl_files/"
        "{libero_spatial,libero_object,libero_goal,...}/*.bddl；config.yaml "
        "已由引擎在 .libero/ 预置（LIBERO_CONFIG_PATH 已注入），直接 "
        "OffScreenRenderEnv(bddl_file_name=...) 即可，勿交互式建 config。",
        # —— run2j 实跑 API 真值（2026-10-26，攢批77）：run2j env 墙已过
        # （ENV_OK+RESET_OK），但死于 3 个 LIBERO env API 用法坑——gym 式
        # action_space 不存在、init_states 入参、渲染入口。以下全部容器实测
        # 真值（0 补丁单进程取证），防 run2k 再烧 5 轮同坑：
        "  - **无 gym API**：OffScreenRenderEnv 没有 action_space / "
        "action_spec / render()（实测全 AttributeError）。动作维数=7（传 8 报 "
        "AssertionError \"expected 7, got 8\"——7=delta 位姿 6 + 夹爪 1），"
        "动作值域 [-1,1] 归一化。step 返回经典 4 元组 "
        "(obs: OrderedDict, reward: float, done: np.bool_, info: dict)。"
        "**禁止随机动作脚本**（2026-10-26 Gate2 真人拒批实证：随机抖动无抓取"
        "=必拒），控制器必须按下条 servo 真值写目标驱动闭环。",
        "  - 成功判据（官方）：`env.check_success()` → bool（容器实测可调；"
        "零动作返回 False）。逐局判分入口就是它，勿自造阈值。",
        "  - 视频帧来源：无 env.render()——帧在 obs 里："
        "`obs['agentview_image']` 与 `obs['robot0_eye_in_hand_image']` 均 "
        "(128,128,3) uint8（容器实测），每 step 后取 agentview_image "
        "累积成帧列表 → imageio 写 mp4 即可。",
        "  - 初态回放：`suite.get_task_init_states(<int 索引>)`——入参是 int "
        "任务索引（传任务名字符串会 TypeError list indices），返回 50 局的 "
        "qpos 数组（每局 shape (92,) ndarray；内部 torch.load 自动完成，"
        "无需自己 load）。`env.set_init_state(qpos)` 设置后返回新 obs "
        "（OrderedDict），随后 step 正常。每局可 set_init_state(init[i]) "
        "复现官方第 i 局初始状态。",
        # —— run2k/r24 实证（2026-10-27，攢批82，锁内适配）：Gate2 首判拒批
        # （随机动作无抓取）后，修复环把脚本叠成「env.step 包装层 + 未防护
        # obs['object_positions'] 内联 phase 状态机」两套循环，5 轮补丁只修掉
        # 一个 SyntaxError，exec6 撞 KeyError 'object_positions' 后 5 轮帽尽
        # human_failure。以下为 0 付费容器探针 probe_obs_servo.py（2026-10-27，
        # libero_spatial task0 黑碗，与 evaluate exec 同 env 口径）机器真值：
        "  - **obs 键真值（容器实测 31 键）**：obs **没有** 'object_positions' "
        "聚合键（下标直接 KeyError——r24 死因）。每个物体世界系位姿直接在 obs："
        "<物体名>_pos (3,) float64 与 <物体名>_quat (4,) float64（如 "
        "akita_black_bowl_1_pos、plate_1_pos、glazed_rim_porcelain_ramekin_1_pos"
        "——物体全名以 bddl 为准）。另有 <物体名>_to_robot0_eef_pos/_quat "
        "相对位姿、object-state (70,)、robot0_eef_pos/_quat、"
        "robot0_gripper_qpos (2,)。",
        "  - **servo 抓取控制器已实测可行（0 付费探针机器真值，非猜测）**："
        "world-frame delta OSC P 控制——delta=clip(0.5*(target_pos-eef_pos), "
        "±0.05)，action[:3]=delta/0.05，夹爪维 action[6]=-1 张开/+1 闭合——"
        "60 步把 eef-物体距离 0.338m→0.022m；随后闭爪 5 步、保持闭爪抬升"
        "（action[2]=+1）30 步，物体随爪上升 0.365m（真实抓取在场）。"
        "放置类任务续：平移至目标物 _pos 上方→下放→开爪。整条 "
        "reach→grasp→lift→place 链即 Gate2 质量线「接触/抓取真实发生」。",
        "  - **单一控制循环纪律**：全脚本只允许一个 env.step 主循环（r24 "
        "实证：模型曾把 env.step 包一层又另写一套内联 phase 状态机，两套互抢、"
        "第二套未防护键读取直接崩）。物体位姿只从 <物体名>_pos 键读，禁止"
        "发明聚合键、禁止第二套循环。",
    ])
