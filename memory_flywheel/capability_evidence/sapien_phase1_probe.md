# SAPIEN / ManiSkill 一手探针证据索引（Phase 1 Task7）

- 证据等级：**[实践]**（本仓自采一手探针，非 KB tour/论文二手知识；KB
  `knowledge/` 词边界口径对 sapien 仍零命中，只读钉死 commit aac53313 不变）。
- 采集时间：2026-10-14（本机文件时间戳），Task7 付费图运行**之前**，全部
  **0 付费、0 LLM/VLM 实呼**；脚本在 sim-sandbox 容器内真实执行，所有帧均来自
  `env.render()` 真实回读（探针硬自检：首帧像素方差 >10、首末帧均差 >0.5，
  禁 numpy 合成帧）。
- 版本钉死：`sapien==3.0.3`、`mani_skill==3.0.1`（追加于
  `sandbox/docker/requirements-container.txt`，传递依赖锁
  `requirements-container.lock`），`numpy==1.26.4`、`torch==2.4.1+cu124`
  既有 pin 未松。
- 原始产物根：`$SIMPRAXIS_RUN_DIR/phase1/task7_track1/`
  （容器内同路径挂载，等于宿主 `/mnt/artifacts/simpraxis/runs/phase1/task7_track1/`）。
  本文件只做索引与结论，数值原件以 JSON 为准。

## §1 版本、安装与钉版

- 安装探针日志：`probe/container_maniskill_probe.attempt1.log`（首次失败，
  网络/二进制过程如实保留）、`probe/container_maniskill_probe.attempt2.log`
  （成功）；安装后环境快照 `probe/pip_freeze_after_maniskill.txt`。
- 成功装载组合（`probe/maniskill_render.json`）：maniskill 3.0.1 / sapien 3.0.3 /
  torch 2.4.1+cu124，`torch.cuda.is_available()=true`，Python 3.10.12。
- 结论：ManiSkill + SAPIEN 可在既有 sim-sandbox 镜像内 pip 安装并 import，
  版本已钉入容器 requirements，未改动 numpy/torch/imageio 既有 pin。

## §2 官方 PushCube-v1 headless 真渲染探针

- 脚本：`sandbox/probes/probe_maniskill.py`（官方 env `PushCube-v1`，
  obs_mode=state / render_mode=rgb_array，sim 与 render 后端均 gpu）、
  `sandbox/probes/run_maniskill_probe.sh`。
- 结果 `probe/maniskill_render.json`：30 步随机动作后，回读帧 shape
  [512,512,3]、uint8；首帧像素方差 4973.845、末帧 5009.218、首末帧均值差
  5.2982（场景真实运动，非纯色/非静止）；人类证据帧 `probe/maniskill_frame.png`
  （282742 B）；wall 57.45s。
- SAPIEN3 GPU 渲染后端为 Vulkan（日志有 "Failed to find Vulkan ICD file"
  warning，SAPIEN 自行兜底 ICD 后实际出帧成功）；EGL 离屏环境沿用 Phase0
  `verify_egl_real.py` 真 GPU 探针口径，OSMesa 不算通过。
- 结论：headless 容器内官方 env 可真实离屏渲染，满足 Task7 视频证据链路。

## §3 PushCube reach-push-hold 全流程一手复现（physx_cpu 物理）

- 场景脚本：`sandbox/track1/maniskill_pushcube.py`（内置大量硬自检，
  失败即非零退出；圆形成功区内接方盒判定 half=r/√2 为严格子集）。
- 权威运行 `run_mech_verify_sapien_1/manifest.json`（seed 0）：物理后端
  physx_cpu、渲染后端 gpu(Vulkan)；sim/control 100Hz，frame_dt=0.01s，
  substeps=1，573 帧完成 reach→push→hold 全程；规划器
  PandaArmMotionPlanningSolver。
- 结果：`env_success_end=true`；块末点距圆心 0.0515m（成功圆 r=0.1m）；
  末点 z=0.019999（桌面之上）。
- 旁证运行 `run_smoke_sapien_1/manifest.json`（同脚本另一标定参数组，
  486 帧、aim=-0.07）同样 `env_success_end=true`（末点 0.04595m），
  证明非单次巧合。
- 产物：两运行各自 telemetry.json（52–62KB，逐帧数值）、video.mp4
  （145699 B / 196338 B，全 I 帧探针链路口径）、cover.png。

## §4 接触测量能力：get_contacts、接触力稳定性与穿模深度

- physx_cpu 物理后端下 SAPIEN `scene.get_contacts()` 逐接触对可用
  （基础计划 L517 点名的 Rung1 接口实测落地），脚本据此输出逐帧接触力、
  连续接触段、冲量与穿模深度。
- `run_mech_verify_sapien_1/manifest.json` contact_profile：首次接触
  frame 237；最长连续接触 336 帧 = 3.36s；该段接触力均值 0.2276 N、
  变异系数 CV 0.246（推送段 0.2513 N / CV 0.1471；持位段 0.19 N /
  CV 0.3173）；全程最大瞬时接触力 0.704 N。
- 穿模：`max_penetration_m = 7.6e-6`（smoke 运行为 4.3e-5），低于 Rung1
  0.005m 阈值两个数量级以上——逐接触对穿透深度统计链路可用且本场景实测干净。
- 结论：Track1 Rung1（逐子步接触/穿模）与 Rung2（接触时长、力稳定性 CV）
  在 SAPIEN/physx_cpu 上有真实数据支撑，不需要 numpy 假帧或硬编码。

## §5 持位力学机制判定（摩擦楔紧，非墙 / 非水平预紧）

- `run_mech_verify_sapien_1/manifest.json` + `hold_motion_diagnostic.json`：
  130 帧持位期间指尖水平合力均值 0.1900 N、竖直分量 ≈ 2e-5 N（≈0），
  桌面法向力均值 0.7212 N（≈块重支撑）；持位全程最大速度 0.0892 m/s
  并在 30 帧 settle 后衰减，静态窗（100 帧）最大速度 0.003866 m/s、
  净位移 0.0003928 m（末 10 帧速度 5e-6–2.8e-5 m/s 量级）。
- 墙面接触 `wall_s=5.028s` 发生在任务完成之后；持位阶段无墙参与。
- 机制结论（manifest mechanism_note 原文）："steady hold contact is
  PD-maintained finger-on-cube top-rear edge (vertical-dominant, finger
  friction mu=2.0); no wall; cube static by pose finite-difference"——
  指尖骑在块后上棱、以 μ=2.0 摩擦形成竖直主导楔紧，而非水平对夹预紧、
  非靠墙。Task7 Gate2 说明卡片须照此口径陈述，不得宣称墙面/预紧机制。

## §6 已知缺口：GPU 物理后端接触接口缺失（诚实负面证据）

- `probe/pushcube_forces_gpu.json`（gpu sim backend 三次 dump）：
  `PhysxGpuSystem` 对象无 `get_contacts`（AttributeError 原文落 JSON，
  contacts_n=null 全三次）；探针枚举实体同样失败
  （'Scene' object has no attribute 'get_all_entities'）。
- 力坍塌：hold_1 时两指 link force 尚在（合力 0.188 N），hold_60/hold_120
  link_forces 为空、合力 0.0 N，桌面力仍在（0.625–0.687 N）——GPU 物理
  后端下持位接触力不可测且行为退化。
- 锁内结论：Track1 Rung1–3 测量**必须** physx_cpu 物理后端，GPU 仅用于
  Vulkan 渲染；该缺口与参数选择无关，不得通过调参掩盖。SAPIEN 传感器
  （相机/光学噪声）矩阵项仍缺，留 Phase 1 backlog。

## 附录：原始产物清单（相对 $SIMPRAXIS_RUN_DIR/phase1/task7_track1/）

| 路径 | 内容 |
|---|---|
| `probe/maniskill_render.json`、`probe/maniskill_frame.png` | §2 官方 env 真渲染探针 |
| `probe/container_maniskill_probe.attempt{1,2}.log`、`probe/pip_freeze_after_maniskill.txt` | §1 安装过程与环境快照 |
| `probe/pushcube_forces_gpu.json`、`.log`、`probe/pushcube_forces_cpu.json` | §6/§4 双后端接触力对照 |
| `run_smoke_sapien_1/{manifest,telemetry}.json`、`video.mp4`、`cover.png`、`hold_motion_diagnostic.json` | §3/§4 旁证运行（486 帧） |
| `run_mech_verify_sapien_1/{manifest,telemetry}.json`、`video.mp4`、`cover.png`、`hold_motion_diagnostic.json`、`run.log` | §3–§5 权威运行（573 帧） |
| `sandbox/probes/probe_maniskill.py`、`sandbox/probes/run_maniskill_probe.sh`（仓内） | §2 探针脚本 |
| `sandbox/track1/maniskill_pushcube.py`（仓内） | §3–§6 Task7 场景脚本 |
