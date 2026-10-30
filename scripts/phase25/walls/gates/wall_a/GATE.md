# 墙 A 闸定义（首呼前冻结；首呼后不得改字）

本文件与同目录 `TASK.md`、`wall_a_runner.py`、`expected_vectors.json` 的
哈希由 `freeze_wall_a.py` 写入冻结清单（`$SIMPRAXIS_T5_FREEZE_ROOT/
manifests/freeze_manifest.json`）。`run_wall.py` 每次启动（付费前）重算
哈希并与清单逐字节比对，不符即拒呼。首呼后任何改字（含空白）都使冻结失效，
本墙作废。

## 1. 闸是什么

历史真卡过的确定性 **CPU 探针**：模型自著 `agent_solution.py`（契约见
TASK.md，只暴露 `build_env()`），评分器独立驱动 LIBERO `ControlEnv` 的
构造→reset→3 个零动作 step，并独立重驱 Phase 2 Complete G-9 两坑：

1. `binding_utils.py` 的 `jnt_type in (mjJNT_HINGE, mjJNT_SLIDE)` 断言：
   评分器自己遍历活模型全部 13 个 hinge/slide 关节跑
   `get_joint_qpos_addr`，逐字比对冻结的关节名/类型/qpos 地址表
   （pit1，期望在 `expected_vectors.json`，模型不可见）；
2. `base_controller.py` 的 `mujoco.mj_fullM(..., sim.data.qM)`：reset+step
   后原始 qM 必须是紧凑稀疏（nv=43、nM=153、shape(153,)），OSC 控制器
   的 mass_matrix 必须是 7×7 finite 稠密矩阵（pit2，评分器独立取控制器
   对象，不接受模型自报）。

外加：构造后与 reset 后两次独立断言离屏渲染上下文不存在（开 GL 即 EGL
崩溃或被本检查判红）；观测键集合、零动作奖励/完成位、`check_success`
假值逐字钉死。共 **12 项检查**，全 PASS 且 rc=0 才算 B-eng 过墙。

## 2. 冻结环境

- 镜像：`sim-praxis/agentic:phase25-t4`
  - image id `abf96496a6d7`
  - RepoDigest `sha256:abf96496a6d72cf4f61c37a3cfe7d657013c1a567fcc88b04ac2b616d012fed7`
  - entrypoint Xvfb :99（本闸不用）；容器内 python 3.10；uid 1000
- 解释器：容器内 `/opt/venv/bin/python`（隔离 libero venv：
  numpy 1.26.4 / mujoco 3.1.6 / robosuite 1.4.0），其 python 为
  `/mnt/venvs/simpraxis/bin/python → /usr/bin/python3` 的绝对软链，
  故主 venv 必须以**相同宿主路径**只读挂载；
- LIBERO 源码树（tour 快照，只读）挂 `/opt/libero_tour`；
- 冻结闸环境（pyshim 软链 + libero_cfg/config.yaml）挂 `/opt/gate:ro`：
  - 容器内 `import libero` 经 `PYTHONPATH=/opt/gate/pyshim` 的
    `libero → /opt/libero_tour/libero` 命名空间软链解析（venv 内 editable
    finder 指向构建期失效路径，sys.path 前置优先）；
  - `LIBERO_CONFIG_PATH=/opt/gate/libero_cfg`（其 config.yaml 内的
    benchmark/bddl/init/assets 路径全部指向 /opt/libero_tour，datasets
    指向 /work/_datasets，缺失仅良性告警）；
- 纯 CPU：固定注入 `CUDA_VISIBLE_DEVICES=""`；numba/matplotlib 缓存
  定向 tmpfs：`NUMBA_CACHE_DIR=/tmp/numba_cache`、`MPLCONFIGDIR=/tmp/mpl`；
- 网络 `--network=none`；根文件系统只读；不挂 docker.sock。

## 3. 冻结工具白名单（Gate1 授予串逐字）

```
read_file
list_dir
grep
write:
run:python
run:ls
run:cat
run:grep
run:find
```

`finish`/`fail` 为控制原语，恒在。`run:python` 由 policy 归一化匹配
`/opt/venv/bin/python`；`python -c`/`python -`/`-m pip` 等偷渡形按
产品 policy 恒拒；其余 `-m` 形态在本宽授予下亦拒。不授 run_probe、
不授 recall（Task 6 才接线方案召回，本任务隔离「工具内环单独能否过墙」
前提）、不授任何网络探针。写路径仅限 /work（jail 到 workdir）。

## 4. 规范命令

### 4.1 模型执行容器（run_wall.py 的 spike RunBackend 逐段照此构造）

```
docker create --rm -i --network=none --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --read-only \
  --pids-limit 256 --memory 4g --tmpfs /tmp:rw,size=256m,mode=1777 \
  -w /work --label simpraxis-agentic-run=<uuid> \
  -v <workdir>:/work:rw \
  -v <phase2>/runs/phase2/venvs/libero:/opt/venv:ro \
  -v /mnt/venvs/simpraxis:/mnt/venvs/simpraxis:ro \
  -v <phase2>/runs/phase2/task7/libero_src/LIBERO_tour:/opt/libero_tour:ro \
  -v <SIMPRAXIS_T5_FREEZE_ROOT>/gate_env:/opt/gate:ro \
  -e PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
  -e PYTHONUNBUFFERED=1 -e HF_HOME=/opt/hf \
  -e TRANSFORMERS_OFFLINE=1 -e HF_HUB_OFFLINE=1 \
  -e CUDA_VISIBLE_DEVICES= -e PYTHONPATH=/opt/gate/pyshim \
  -e LIBERO_CONFIG_PATH=/opt/gate/libero_cfg \
  -e NUMBA_CACHE_DIR=/tmp/numba_cache -e MPLCONFIGDIR=/tmp/mpl \
  sim-praxis/agentic:phase25-t4 -- <argv>
docker start -a -i <id>     # 超时按 ID 精确 rm -f，label 兜底
```

模型容器**禁止**挂载 `/opt/inst`（评分器与期望向量对模型不可见）。

### 4.2 评分（agentic loop 自终局、全部模型容器消失后一次性运行）

在 4.1 同形容器上追加：

```
  -v <repo>/scripts/phase25/walls/gates/wall_a:/opt/inst:ro
```

argv 固定为：

```
/opt/venv/bin/python /opt/inst/wall_a_runner.py /work/agent_solution.py /work/.score
```

判读：stdout/JSON 的 `"verdict": "pass"` 且 rc=0；`/work/.score` 在评分
前由 run_wall 清空，从不挂进任何模型容器。

## 5. 冻结哈希（首呼前由 freeze 脚本复算并写入清单；此处留痕）

- TASK.md sha256 `c4fd5348b0e48dcefea7816293f24a9c6a2b5112dc72e049419251552a35a4ac`
- wall_a_runner.py sha256 `21507dedd634d00359693c5f4a0f6b1fa62ce76366bc6e4323e1c304b0b8ec2d`
- expected_vectors.json sha256 `b6a7ac6319bc2cfb1cbf5134a4d35cc74dd90bca6ed2cb33b419c52fea59bbe2`

## 6. 冻结时刻双向验证台账（lead-only，禁止挂进模型容器）

- must-green：lead 参考解（最小 build_env，三渲染全关）评分 12/12 PASS、
  rc=0（2026-10-30 UTC，容器内实跑，elapsed≈24s）；
- must-red：开启 offscreen 渲染的同构变体在 reset 期即
  `create_initialized_egl_device_display → ValueError`（无 GL 设备），
  评分 rc=1、verdict=fail；
- 证据（解、日志、评分 JSON、容器 argv）冻结于
  `$SIMPRAXIS_T5_FREEZE_ROOT/lead_only/wall_a/` 与
  `.../validation/wall_a/`，0700，清单逐件哈希；这些路径不出现在模型
  容器的任何挂载中。

## 7. 通过判据

工程闸过 = §4.2 verdict pass/rc0；三态与效率线、子帽、熔断规则不在本件
重复，统一以 `../VERDICT_CRITERIA.md` 为准（墙 A 子帽 16、禁借调、
到帽未过即「不成立」）。
