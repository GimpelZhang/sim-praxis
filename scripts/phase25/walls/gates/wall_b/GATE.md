# 墙 B 闸定义（首呼前冻结；首呼后不得改字）

本文件与同目录 `TASK.md`、`wall_b_checker.py`、`SEQ_SPLIT.json` 的哈希由
`freeze_wall_b.py` 写入冻结清单（`$SIMPRAXIS_T5_FREEZE_ROOT/
manifests/freeze_manifest.json`）。`run_wall.py` 启动（付费前）重算哈希并
逐字节比对，不符即拒呼。首呼后任何改字都使冻结失效，本墙作废。

## 1. 闸是什么（B-eng 工程轴；B-cap 独立另记）

Phase 2 Task8 run_5 机判战役真正卡死人的不是物理、也不是 85%，而是**测量
仪器不合格**：非水密/非流形网格误判、哨兵数值、峰值当均值、铰链耦合运动学。
墙 B 把这场历史战役还原成确定性 CPU 仪器资质闸。模型自著 `agent_solution.py`
（契约见 TASK.md：边流形计数仪、程序化单体中空壶、运动学见证仪/判违仪），
评分器独立复算：

- S0 无哨兵字面量（AST 扫 −1/−999，下标切片豁免）；
- S1 无面向测试硬编码（768/2304/1536 整型字面量、历史见证件路径提示）；
- M1 程序化壶：重载复检边界/非流形边为 0、水密、单连通体、≥96 面、
  体积/凸包比 ∈(0,0.6)、与冻结历史件 sha256 不同且 (面,点)≠(1536,770)；
- M2 三冻结拓扑向量：768 散三角⇒(边界 2304, 非流形 0)、三面扇形共享边⇒
  非流形≥1、冻结历史件⇒(1536, 0, 0, watertight)；
- K1..K4 冻结合成轨迹：11 个见证量与评分器独立实现逐项 1e-9 相等，
  违例名逐字（合同 1 三项 / 合同 6 spout speed / 合同 3 clearance）；
- K5/K6 非有限与退化输入必须抛异常。

全部检查 PASS 且 rc=0 才算 **B-eng 过墙**。`b_cap` 段（speed≤0.20、
clearance≥0.012，来自可选 `design_pour_trajectory()`）只描述性记录，
不参与 rc——B-eng 过而 B-cap 不过是「G.8 能力墙在工具层复现」独立结论，
不触发 D-4（见 `../VERDICT_CRITERIA.md` §6）。

## 2. 历史规模冻结（子帽分母）

- `SEQ_SPLIT.json`（schema `simpraxis.phase25.wall_b_seq_split.v1`，
  sha256 `861f5e77e378161d9f0337b97b76e1cecc91ea161e317fb0e1fde4096a57ee9c`）：
  run_5 机判战役 = T8-C9/C10/C11 = **seq398–436 共 39 呼**，逐项
  byte_confirmed；其中 B-eng 30（子帽分母 sB=30）、B-cap 9（不进分母）；
  seq309–397 属四次真人 Gate2 reject 战役，明确排除；
- 子帽：clamp(half_up(25·30/90),9,16)=**9**（无跨墙借调，储备 3 仅
  供应商污染重跑）。

## 3. 冻结环境

- 模型执行容器与墙 A 同一镜像 `sim-praxis/agentic:phase25-t4`
  （id `abf96496a6d7`，RepoDigest
  `sha256:abf96496a6d72cf4f61c37a3cfe7d657013c1a567fcc88b04ac2b616d012fed7`），
  但只挂三件：workdir（/work rw）、隔离 venv（/opt/venv ro，含
  numpy 1.26.4 与 trimesh 5.1.1）、主 venv（/mnt/venvs/simpraxis ro，
  绝对软链所需）；**不挂** LIBERO tour、不挂 /opt/gate、不挂任何历史
  证据目录；
- 固定 env：PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin、
  PYTHONUNBUFFERED=1、HF_HOME=/opt/hf、TRANSFORMERS_OFFLINE=1、
  HF_HUB_OFFLINE=1、CUDA_VISIBLE_DEVICES=""；
- 网络 none、根只读、cap-drop ALL、no-new-privileges、pids 256、
  mem 4g、tmpfs /tmp 256m；create/start 分离、超时按 ID 精确清理。
- 冻结历史见证件（评分器专用，模型容器永不挂载）：
  `$SIMPRAXIS_T5_FREEZE_ROOT/evidence_wall_b/pitcher_open_watertight.obj`
  （48527 B，1536 面/770 点/watertight/euler 2/单体；
  sha256 `ab30905766eff1309563f99469566e323e230bff449ba3f51d30bf230df2d4f8`，
  自 run_5 工作目录只读复制，清单钉哈希）。

## 4. 冻结工具白名单（Gate1 授予串逐字）

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

语义与墙 A 相同；不授 run_probe、不授 recall、不授网络探针。

## 5. 规范命令

### 5.1 模型执行容器

同 §3 的 docker create/start 形态（label simpraxis-agentic-run=<uuid>，
argv 仅白名单内 `/opt/venv/bin/python …` 等）。

### 5.2 评分（loop 自终局、模型容器全部消失后；宿主侧主 venv 一次性运行）

```
/mnt/venvs/simpraxis/bin/python \
  <repo>/scripts/phase25/walls/gates/wall_b/wall_b_checker.py \
  <workdir>/agent_solution.py \
  <SIMPRAXIS_T5_FREEZE_ROOT>/evidence_wall_b/pitcher_open_watertight.obj \
  <workdir> <workdir>/.score
```

判读：`"verdict": "pass"` 且 rc=0；评分前 run_wall 清空 `.score`。
评分器源码与冻结 OBJ 对模型不可见（不在任何模型容器挂载内，TASK.md 不含
拓扑向量/面数/路径/哈希）。

## 6. 冻结哈希

- TASK.md sha256 `5725313ee73d3c89085ba4937ced74746d603ea1c7942bd2862c5fdb0276546c`
- wall_b_checker.py sha256 `5f178625d004ee8d3c239c1d967cb1610ad3502ffee4ffe0888e615caffd26c7`
- SEQ_SPLIT.json sha256 `861f5e77e378161d9f0337b97b76e1cecc91ea161e317fb0e1fde4096a57ee9c`

## 7. 冻结时刻验证台账（lead-only）

- oracle 参考解：全项 PASS、rc=0；
- 六个 must-red 变异（计数器恒零、实心立方体、哨兵速度、垃圾文件、
  NaN 透传、复制历史件）全部 rc=1，且各自命中其预定红项（变异名/红项
  映射在冻结清单 `validation/wall_b/ledger.json`）；
- 解、变异、日志与评分 JSON 冻结于
  `$SIMPRAXIS_T5_FREEZE_ROOT/lead_only/wall_b/` 与
  `.../validation/wall_b/`，0700，永不挂进模型容器。
