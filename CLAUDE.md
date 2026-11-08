# CLAUDE.md — SimPraxis（纸浅）Agent 工作规则

面向具身智能仿真工具链复现与面试题实证的自治智能体。本仓是 Product A（智能体系统）；
Product B 知识库 `~/embodied-ai-sim-knowledge-base` 为公开内容层（Phase 2 Task0b
起解题脚本随其 `solutions/` 公开；数据产物闭源落 `/mnt`，三层分离纪律见硬规则 3）。
Phase 0/1 均已全绿推送：
Phase 0（8 commits 至 `99b3adf`）、Phase 1 Task0–9 + Q2 补点验（11 commits 至 `966fd00`）、
评审 P1/P2 修复批次（2026-10-16 晚，47+1 项 0 付费，三独立评审全 APPROVE，
7 commits `b9ba5f9..8b83193`，341 测全绿，详见 Complete 附 B，余 LOW 在 Phase 2）。
Phase 2 Milestone 2 引擎侧：Task0/0a/0b 六提交已随 2026-10-17 推送窗口在 origin/main
（至 `38ae3e4`）；**2026-10-28 推送窗口经用户指示已将攒批 106 条全部推送**
（`38ae3e4..b21d72b`，origin/main 至 `b21d72b`＝攢批106；含 Task1–9 三签 51 条
M2 提交 `2d83528..956dc22` 与 Task6 V2/Task7/Task8 战役攢批87–106；推送前密钥扫描
0 命中、无 >10MB 文件，内存 extraheader 不留盘。此前「暂不推送，攒批」常设指令
覆盖计划 L632 里程碑推送，锁内适配 M2-A2；本次为用户逐次指示，新提交恢复攒批）；
M2 终态干净树
1086 passed/1 skipped/42 deselected，Phase 2 台账 120 实呼（82 ok/38 fail）
+6 preflight_blocked。Task7 LIBERO Stage1 战役已收官（2026-10-27 Gate2 真人 accept
+ phase1_learnings accept 行 1b10ac8d + 方案回收 KB，攢批85/86）；Task8 Genesis
流体 **Stage1 已以诚实负结果结案（2026-10-28 用户裁决）**：五战役（四次真人
Gate2 reject 卡#1–#4 + run_5 三次机判 8 轮帽：非流形网格→哨兵/峰值测量墙→
铰链耦合运动学），85% 目标从未接近，Task8 实呼 128（seq309–436），不发成功
白皮书/不回收 KB/不抬阈值翻案；详见 `~/文档/Task8_fluid_stage1_negative_closeout_
2026-10-28.md`。Stage0 sph_ok 探针结论独立成立；重启（修复帽 8→12 或硬规则 1
换模型）须未来用户显式裁决。
规范与执行记录在仓库外（与 Plan 冲突时以 Complete 为准）：
`~/文档/Phase_0_Plan.md`（v4.2）+ `Phase_0_Plan_Complete.md`；
`~/文档/Phase_1_Plan_detailed.md`（v1.2）+ `Phase_1_Plan_Complete.md`（偏差 A1–A24、坑 P1–P10）；
`~/文档/Phase_2_Plan_detailed.md`（v1.2）+ `Phase_2_Plan_Complete.md`（偏差/锁内适配总表、付费台账、机判逐项对账、Open Items、三签记录）。

## 架构一句话

- `sandbox/`：CUDA 12.4 EGL 无头渲染沙盒（A800，真 GPU 探针；OSMesa 仅降级诊断，exit 2 不算通过）
- `orchestrator/gateway/`：火山方舟 Anthropic-Messages 网关（`/api/coding`），三阶段
  stage_models + 退避 + 配额长熔断 + 付费 ledger；唯一模型配置点 `stage_models.yaml`
- `orchestrator/graph/`：LangGraph 0.2.28 双轨八节点状态机
  （direction→gate1→agent_run→evaluate→gate2→persist_learning/human_failure→done），
  SQLite checkpoint；0.2.28 无 interrupt，用持久化阻塞节点 + pending 表
- `orchestrator/feishu_gateway/`：lark-oapi 1.7.4 长连接真实 HITL（卡片 schema 2.0 表单
  + P2P 文本双通道）；**No Fake Approval：Gate1 真人批准前禁止容器实跑**
- `orchestrator/common/`：跨域基础——fsutil 原子写 0600/0700、secrets 密钥正则单一源、
  validators charset、callcache 调用级重放缓存（付费点缓存落 phase1_root `.call_cache`，
  0700 dot-dir；命中 0 触网/0 账行，成功才写，不入证据包）
- `orchestrator/solutions/`：内容层方案闸口——只读解析 `$SIMPRAXIS_SOLUTIONS_ROOT`
  （默认 `$KB_ROOT/solutions`，容器内 `/kb:ro`）manifest v1，逐件 sha256/C4 fail-closed，
  `stage()` 0700 目录+入口/0600 其余；引擎仓不携带任何解题脚本（fixture 除外）
- `memory_flywheel/`：LanceDB 双路向量中枢（namespace=capabilities/troubleshooting）+ 切块/矩阵；
  Task0b 起库实体迁到 `$KB_ROOT/data/lancedb`（软链到 `/mnt/artifacts/embodied-kb/data`），
  学习回写只进独立新表 `phase1_learnings`，`kb_chunks` 永不写；方案切块只进
  `solution_chunks`（独立第三表，mpnet 768 复用，幂等可重跑）
- `templates/` + `scripts/verification/check_blueprint_evidence.py`：四 tour 范式与证据核验

## 环境（每个 shell 先来一遍）

```bash
source scripts/00_env.sh                       # 凭据/路径注入，值不回显
/mnt/venvs/simpraxis/bin/python                # 唯一指定解释器；禁止污染 conda cc
```

大文件只落 `/mnt`（venv/caches/assets/artifacts/lancedb/tours），大动作前后 `df -h /mnt`；
仓库内不得出现 >10MB 文件。

## 必须记住的硬规则

1. **模型变更必须先请示用户**：任何 stage 的 model_id（含同家族日期版本、含置 null）一律
   STOP → inventory 取证（`runs/inventory/latest/`）→ 报用户 → 获批后才改
   `stage_models.yaml` 并重验；禁止猜 ID、换供应商、按供应侧回显名分支。业务代码只能
   `ModelGateway.call("macro_ideation"|"coding_debug"|"visual_rollout", ...)`，
   禁止直填模型 ID。macro/coding 两阶段视觉未测，禁止向其发 image block；视觉唯一入口
   `call_visual` 硬绑 visual_rollout。
2. **付费调用有预算（2026-10-16 起授权自治）**：经方舟的实呼都计费；账套 Phase 0 用 8 次、
   Phase 1 台账 29/36（另有 3 次 test_04 误触，见 P1 Complete 事故补记）。**各阶段/Task 的
   实呼批次与预算上限（含提高项目总预算）不再需要报用户批准，由 Agent 自主决定**；但逐次
   ledger 记账、失败/发网前失败也占 seq、批次批准件落 `quota/approvals/`、完成时如实汇报
   用量等纪律不变。单测一律 monkeypatch 禁止真实联网，付费测试带 paid marker +
   `SIMPRAXIS_ALLOW_PAID=1` 首行守卫（test_04 已加双守卫，裸跑自动 skip 零触网；
   收尾仍按纪律带两排除项），收尾跑 `pytest tests -q -k "not test_04" -m "not paid"`。读 ledger 先看 `ts_iso` 时区后缀
   （历史行 +0000/+0800 混写）。模型 ID 变更仍按规则 1 必须请示，本授权不含模型变更。
3. **KB 三层分离纪律（Task0b 起；KB 仓公开、Apache-2.0）**：
   - **引擎层**（本仓）：不含解题脚本；只有 `orchestrator/solutions/` 解析器、
     `templates/solution_manifest.example.yaml` 契约与 `tests/fixtures/solutions/`
     合成 fixture。解题内容一律经 manifest（`simpraxis.solution_manifest.v1`，
     逐件 sha256 + C4 路径）从内容层读，缺件/不符即 `SolutionError` fail-closed。
   - **内容层**（`~/embodied-ai-sim-knowledge-base`）：`knowledge/` 32 个 md 的
     sha256 集逐字钉死 `aac53313…` 基线（只增不改；新 md 必须用户签收并显式登记
     `memory_flywheel/kb_pins.py::KNOWLEDGE_ADDITIONS`）；当前 HEAD 必须是迁移提交
     `c5712e9f73fc17cd2649cae359aad216e8f3bd07`（solutions/ 首次写入）的后代；
     `solutions/` 5 方案（7 脚本；Task7 攒批86 追加 track1_tools/03_libero Gate2 accept
     回收件）随 git 公开，追加走新提交，公开历史不回收。
     单真值在 kb_pins（preflight/ingest/e2e/matrix 四断言统一调它，禁止抄字面量）。
   - **数据层**（闭源、永不入库）：lancedb/blobs/bundles 实体在
     `/mnt/artifacts/embodied-kb/data`（0700），`$KB_ROOT/data` 是软链，
     `.gitignore` 必须用锚定 `/data`（尾斜杠 `data/` 不匹配软链本身），
     闸口打 `git check-ignore data`（穿越软链的 data/lancedb pathspec 会 fatal）。
   - **KB 首次 push 是人工 STOP 门**：push 前向用户呈 diff 清单 + 密钥扫描（公开不可回收）。
   - 行数口径：kb_chunks 1542 行/104 Q 块；phase1_learnings 当前 44 行（2026-10-28
     Task8 结案活表对账；2026-10-27 时点为 32 行，对账
     =M2 时 12（Task0a 后 5 + Task6 run1–6 human_failure 6 + run7a 1，id 0ef64b94）；
     + 升阶战役 10-25 十二条诚实 human_failure 回写 + persist 首崩 020327ed + Gate2
     accept 行 66bafb1d（gate2_accepted/rung=3，evidence `@phase2/` 第三根）
     + Task7 LIBERO 战役六行（run1/run2/run2j/run2k/侧车 human_failure 5
     + Gate2 accept 1b10ac8d rung=1，2026-10-26…27）；
     run7c key-collapse 塌到 24681e47 不增行仍登记；
     + Task8 流体战役十二行（2026-10-28：D10 三熔 human_failure_gate2 3 行
     de8910c9/c19b5c0d/b267d1d4 + human_failure_agent_run 9 行；run_5 三机判墙
     是三个独立新行 54b27b59/b6eb36a2/1b5db0a5——曾误记「key-collapse 不增行」，
     结案活库查询复核更正；仅 54b27b59 的 created_at 在重放事故被覆写））；
     solution_chunks 独立第三表（现 94 块/5 方案，Task0b 首灌 82 块/4 方案 +
     Task7 攒批86 追加 03_libero 12 块），老三表对它恒只读。
4. **凭据纪律**：只从 `~/access/` 与 `~/.bashrc` 的 `ANTHROPIC_*` 按 export 名等值读取；
   禁止 `source ~/.bashrc`、禁止回显/入 URL/入日志/提交；git 认证用内存 extraheader 不落盘；
   fixture 不得像真密钥（scan_secrets.sh 10 条 + common.secrets Python 10 条严口径单一源）。
5. **GPU/容器收尾**：任务结束容器必须 `docker compose down`（含已退出容器不得残留），
   显存 ≤200 MiB；不擅杀他方 GPU 进程。禁止 `docker system prune -a`、不改 daemon.json/
   不迁移 data-root（本机有他方镜像）。
6. **删除守卫**：禁止 mkfs/fdisk/format；任何 rm 前先 ls，目标必须是 simpraxis 自有字面路径
   或 `case` 后缀守卫；不碰他方目录（`/mnt/artifacts/{run_a15_batch30_20261001,
   smoke_20261003,stage2}`、`/mnt/venvs/{alpamayo,alpasim,flashdreams}` 等）。root 属主文件
   用 `~/access/access_methods.txt` 口令管道 sudo，不回显。
7. **断言不许放水**：不弱化查询/阈值、不 test.skip、不留 TODO 占位、不用 numpy 假帧/硬编码
   KB 条目。计划 heredoc 与现实冲突时，做最小"锁内适配"并在代码注释（搜 `锁内适配` /
   `适配（计划`）与 commit body 留痕；模型类失败永远先走规则 1。**与预注册方向相反的实验
   结果（如 Q1 0.824<界 2.0）矛盾即结论，禁止调参/换种子/改界翻案。**
8. **版本与网络**：不松 pinned 版本（langgraph 0.2.28、lark-oapi 1.7.4、numpy 1.26.4、
   torch 2.4.1+cu124、mujoco 3.15.0、mani_skill 3.0.1、sapien 3.0.3、genesis-world 1.3.3、
   carla client 0.10.0 --no-deps）；pip/apt 清华源，HF 走 hf-mirror；GitHub 大克隆遇
   HTTP/2 curl 92 用 `git -c http.version=HTTP/1.1 -c http.postBuffer=524288000`；
   容器构建 `SIMPRAXIS_USE_PROXY=0` 直连。
9. **Git**：每条提交 author `Junchuan Zhang <zjunchuan@gmail.com>`（local config 已设），
   trailer `Co-Authored-By: Claude Code <noreply@anthropic.com>`；提交后跑
   `bash scripts/verification/check_git_author.sh`。已推送历史不得改写。
10. **Tour 纪律**：四 tour 只克隆与结构化（LFS 默认 skip），任何阶段不实跑 tour 本体；
    Phase 1 实跑的是 MuJoCo/ManiSkill/Genesis/Carla 平台 track（容器内、产物只落
    `/root/runs/phase1/`），与 tour 无关。
11. **上下文纪律（C1–C5，2026-11-07 立）**：长任务**禁止用重读源码恢复上下文**。
    实测反例（Phase 2.8.1 两段读文件循环，机读取证）：11-06 18:31–24:00 五小时半内
    压缩 **50 次**（每 6.4 分钟）、1,997 条 Bash（grep 829/sed 712）却只落盘 8 次、
    窗口内 0 提交；11-07 70 分钟内压缩 8 次。机制是压缩抖动正反馈——压缩清空工作记忆
    ⇒ 重 grep 同一文件重建现场 ⇒ 再灌满 ⇒ 再压缩。全文见
    `~/文档/Phase_2.8.1_Plan_Complete.md` 末节「过程复盘：两段读文件循环的实测与上下文纪律」。
    执行约束：
    **C1** 每个 Task 开工先用**一条**命令生成「文件→符号→行号区间」索引并落盘，此后
    取码一律 `sed -n 'A,Bp'` 精确切片，**禁止重复 grep 同一模式、禁止重复 `cat` 同一文件**；
    **C2** 每完成一个判断即落一行进度（`.omc/notepads/` + 对应 Complete 小节），
    **压缩后只读进度 note 恢复，不复读源码**；
    **C3** 多文件取数用一条批量 `sed -n`，大范围调研派 subagent/Explore 只回结论
    （其上下文用完即弃，不进主窗口）；
    **C4** **30 分钟内压缩 ≥3 次 ⇒ 立即停读、写状态、报用户**；
    **C5** 读文件阶段**连续 45 分钟不落盘即强制先写出可运行骨架**，单条 tool_result
    **>20 KB 视为事故**须改分段读。

## 向量库要点

- 当前嵌入 `paraphrase-multilingual-mpnet-base-v2`（768 维，用户批准替换 bge-512）；
  **换嵌入模型必须 `--rebuild` 整表重建**（跨维度用 `VectorEngine.drop_existing()` 先删后建，
  混维度守卫其他路径恒开）。重建后重跑 Q1/Q2/Q3 召回与 `check_chunk_integrity --expect 104`。
- 入库：`python -m memory_flywheel.scripts.ingest_kb --rebuild`；切块常量
  TARGET_CHARS=1200/HARD_CHARS=2000，Qxx 原子块，文件级幂等替换。
  Task0b 起库默认 uri 为 `$KB_ROOT/data/lancedb`（软链 `/mnt/artifacts/embodied-kb/data`）；
  数据迁移只走 `scripts/kb/migrate_data_layer.py --apply`（同盘 st_dev 守卫 + 行数/召回快照对账）。
- `phase1_learnings`（12 列含 vector，复用 mpnet 不二次下载）与 kb_chunks 同库独立；
  当前 44 行（2026-10-28 Task8 结案活表对账＝32+Task8 十二行；2026-10-27 对账
  32 行＝ M2 时 12——Task0a 授权 4→5 后的 5 行
  gate2_accepted_supplementary/rung=1 + Task6 run1–6 human_failure 6 行 + run7a 1 行
  （id 0ef64b94）——+ 升阶战役 10-25 十二条 human_failure + persist 首崩 020327ed
  + Gate2 accept 行 66bafb1d（rung=3，evidence `@phase2/…summary.json`）
  + Task7 LIBERO 战役六行（run1/run2/run2j/run2k human_failure 4 + persist 侧车 1
  + Gate2 accept 1b10ac8d rung=1，2026-10-26…27 攒批86）；
  run7c key-collapse 塌到 24681e47 不增行仍登记，f43d49 等旧行不重写；
  + Task8 流体战役十二行（2026-10-28：D10 三熔 human_failure_gate2 3 行
  de8910c9/c19b5c0d/b267d1d4 + human_failure_agent_run 9 行；run_5 三机判墙
  为三个独立新行 54b27b59/b6eb36a2/1b5db0a5，非 key-collapse——曾误记不增行，
  结案活库复核更正；仅 54b27b59 created_at 在 callcache 重放事故被覆写）；
  kb_chunks 恒 1542）。**生产召回 2026-10-26 已闭环**：555 行采纳遥测
  （Task6 线程 549 coding/6 direction，17 个学习 id 全为活表真实行），
  516 行经实际 req 证据逐字节互证 + 同线程 Gate2 accept 反证成功结果，
  机判 `scripts/verification/check_recall_closure.py` rc=0（报告
  recall/adoption_crosscheck_2026-10-26T021811Z.json）。
- `solution_chunks`（Task0b 第三张表）：5 方案 README/manifest 原子块 + 脚本 code 块，
  relpath 前缀 `solutions/`，现 94 块（Task0b 首灌 82 块/4 方案 + Task7 攒批86
  追加 03_libero 12 块）；`python -m memory_flywheel.scripts.ingest_solutions`
  幂等先删后写，入库前后强对账 kb_chunks=1542/phase1_learnings=44 不变（fail-closed
  期望值随活表基线，2026-10-28 Task8 结案活表基线对账；ingest_solutions CLI 的
  `--expect-learnings` 自 Task 6（攢批119）起为**必填无默认**（`required=True`，
  `ingest_solutions.py:357`），活库实跑须显式传当前活表行数（现 44）——Task0b 时点的
  默认 5 已修复，该 LOW 尾债关闭）。资产包：
  `scripts/kb/bundle_kb_asset.py --tier both` + `verify_bundle.py`（core=老两表，
  extended=全部表；sha256/行数/768 维/kb_sha/引擎 SHA 对账）。

## 常用命令

```bash
bash scripts/verification/gate_static.sh                      # 六步静态门禁（提交前必跑）
/mnt/venvs/simpraxis/bin/python -m pytest tests -q \
  -k "not test_04" -m "not paid and not kb_integration"      # 0 付费全套（收尾必带排除项）
# test_04 已有 paid marker + SIMPRAXIS_ALLOW_PAID=1 守卫：裸跑自动 skip 零触网；
# 真跑 test_04 批次须 SIMPRAXIS_ALLOW_PAID=1 且按规则 2 自批落 quota/approvals/
# kb_integration（活 KB/活数据层）默认同构 skip；迁移/入库后显式跑：
SIMPRAXIS_KB_INTEGRATION=1 /mnt/venvs/simpraxis/bin/python -m pytest tests -m kb_integration -q
python scripts/kb/check_solutions.py                          # 内容层分层校验（5 方案+data 忽略）
/mnt/venvs/simpraxis/bin/python -m memory_flywheel.scripts.validate_capability_matrix
python scripts/verification/check_blueprint_evidence.py
```

## Phase 边界

Phase 1 已完成：LangGraph 状态机、飞书真人双 Gate（含真实 reject→human_failure 补救线程）、
Rung1–4 分级（Rung4 VLM 为 record_only，用户签收）、四 track 平台实跑与对照、phase1_learnings
隔离新表、证据包（2026-10-16 评审批 --clean 重建 276 文件，两存活库 backup API 快照无侧车）
+ RSI 看板、Task9 真人 HITL 抽验 6/6 一致；评审 P1/P2 47+1 项已修（Complete 附 B）。

Phase 2 Milestone 2（引擎侧，2026-10-17 Task1 起 → 2026-10-24 终验）已完成：Task0a
Q2 补沉淀+图外回写通路、Task0b 三层分离（已推送部分），Task1–9 三签提交 51 条
`2d83528..956dc22`（M2 终验时本地攒批，**已随 2026-10-28 推送窗口全部上 origin/main**）。
终态：干净树两排除全套 **1086 passed/1 skipped/42 deselected/exit 0**；
证据包 570 文件/20.2 MiB（manifest git_commit=956dc22）；M2 时点四表活库
1542/12/82/不存在（phase2_learnings 未建表——gate2 accept 未发生；2026-10-27 攒批86 后活表现为
1542/32/94/1，见下 Task6 V2 与 Task7 段）；M2 时点 Phase 2 台账
120 次实呼（seq 1..120 连续、时间戳全严格 Z、82 ok/38 fail；by_task task4=24/task6=96；
by_stage macro=3/coding=93/visual=24）+6 次 preflight_blocked（seq=null，占批次不占实呼），
.call_cache 17 条与 17 个 ok seq 配对，Phase 1 账 29 行封存只读；blueprint 4 项目 16 stage、
能力矩阵 6 引擎 41 事实、check_solutions 5 方案全过；容器零残留、显存 14 MiB、/mnt 正常。
- Q1 冰面 DR 与预注册方向相反：ice/normal=0.824 < 界 2.0，结论 contradiction_control_not_robust
  矛盾即结论，已冻结未翻案；V2 升阶在 M2 终验时五战未跨过代码出生/结构闸、Gate2 未建行
  （pareto_verdict=null 诚实留空），2026-10-26 run27 后已闭合——v2_summary 已生成、
  看板机判 pareto_verdict=**dominated_by_v1**（见下 Task6 V2 段）。
- 生产召回 2026-10-26 **已闭环**（攒批第 70 条机判）：555 行采纳遥测（549 coding/
  6 direction，17 学习 id 全为活表真实行），516/555 行（167/180 prompt）经实际 req 证据
  逐字节复原且学习文本逐字在场 + 同线程 Gate2 accept 反证成功结果；
  `check_recall_closure.py` rc=0，39 行证据缺口如实挂牌 provenance_limited 不计入通过。
- Rung4 VLM 终态 **record_only**（min_agreement=0.8 但 defect_recall=0.0，D7 未达成，
  用户已签收；禁止调标签/换片/改阈翻案，active 只能以新付费批次重标定）。
- Task7 LIBERO Stage1 **战役收官**（2026-10-27，攢批85/86）：r26 官方判分抓取
  成功 → Gate2 卡#3 真人 approve（14:47:24 CST，No Fake Approval 完好）→ persist
  首崩（rungs 契约）→ 攢批85 修复 + checkpoint 手术 → r27 resume → phase1_learnings
  accept 行 1b10ac8d（rung=1 核验级）→ 方案按计划 L166 回收 KB
  solutions/track1_tools/03_libero（攢批86，94 块/5 方案）。
- Task8 Genesis 流体 **Stage1 诚实负结果结案**（2026-10-28 用户 AskUserQuestion
  裁决）：Stage0 sph_ok 探针（A800 1000 粒子 10 步坐标全 finite）独立成立；
  Stage1 五战役——run_1..4 四次真人 Gate2 reject（静止液体/穿模漏桌/壶杯不动/
  散件壶飞脱，卡#1–#4 逐字在 checkpoint），run_5 合同 v2+C6+C7 三次机判 8 轮帽
  （非流形网格→哨兵 -1/角速均值折算→铰链耦合运动学 speed/clearance 平台），
  卡#5 未发、85% 从未接近。Task8 实呼 128（seq309–436，macro3/coding125）+
  blocked 2；终态全局 436/450、task8 128/140（HARD_CAP/任务顶第 37 次自批值，
  攢批104 deae146）。**不发成功白皮书、不回收 KB（无 Gate2 approve）、不抬
  85%/轮帽/模型翻案**；报告 `~/文档/Task8_fluid_stage1_negative_closeout_
  2026-10-28.md`，证据 closed/evidence-run5c{,6,7}-* 四包；线程 human_failure
  封存（rejects=1；活表 44 行，run_5 三机判墙各写独立 rung=0 行
  54b27b59/b6eb36a2/1b5db0a5 均 append-only 保留——曾误记「key-collapse 不增行」，
  2026-10-28 活库复核更正）。重启仅经未来显式裁决：修复帽
  8→12 同 workdir 续修，或硬规则 1 换模型。
- coding_debug 换供 deepseek-v4.1-flash（commit 4df0e1d）经硬规则 1 两次用户裁决，为 M2 期间
  唯一模型变更；M2 收官后 2026-10-24 用户再直接裁决三阶段换供 mimo 家族（macro=mimo-v2.6-pro、
  coding_debug/visual_rollout=mimo-v2.6-flash，commit fa007c5，硬规则 1 全流程：inventory 404
  取证→用户逐字裁决→改 stage_models.yaml→validate 四请求全绿，账外批次 M2-V-mimo1）；
  2026-10-27 用户再逐字裁决三阶段换供（commit b20c45e 攢批87，硬规则 1 全流程）：
  macro=glm-5.3、coding_debug=kimi-k3.1-pro、visual_rollout=deepseek-v4.1-flash，
  Task8 流体五战役即在 kimi coding 下完成（负结果，见下）。

Task6 V2 升阶闭环已达成（2026-10-26 攒批第 68/69 条）：run27 机器侧全通 + Gate2 真人 accept
（decision=approve "V2通过"，首次 accept）+ @phase2/ 第三证据根修复后 persist 成功，
phase2_learnings 首行落库（1 行）+ 五段式演化志；看板机判 Pareto = **dominated_by_v1**
（V2 六指标与 V1 逐位相同、仅 solve_wallclock 14.03→21.02s 回退），与真人 Gate2 accept
两轴正交自洽；Q1 矛盾 0.824<2.0 原样冻结未翻案。ledger tokens 回填（194/196 ok 行，
余 2 为 deepseek→mimo 过渡批已登记）、quota_pause schema v2 已签收、task8 stage0_wheels
越界符号链接联合已拆除（M2-A6，cache 目标未动、stage0 报告保留）。

Phase 2 余项状态：**Task8 Stage1（流体）已诚实负结果结案（2026-10-28，见上，
CLI 接线攢批74 task8_fluid execute_stage1 合同桩仍在 orchestrator.graph.cli）；
Task7 已收官**——计划内 Phase 2 余项全部有终态（完成或诚实负结果）。Phase 3
各项（未经用户指示勿启动）：RL/VLA 训练、World Judge 实现、RSI Level 3 元 prompt
自演进、实跑四 tour、SAPIEN/Bullet/传感器光学噪声矩阵缺口、Q2 光学模型参数标定
（计划 L693 登记 Phase 3 backlog）、**Phase 2.8.1 能力对比重设计**（2026-11-08 用户裁决
C：`legacy/agentic` 对照判无效——两臂同时在地板 + 四维混淆 + 63+120>180 预算死结，
2.8.1 按 `HOLD` 封存、只出「可用性」结论，条件 A 出生阈值校准 + 条件 B 同引擎消融族
一并移入 Phase 3，见 `~/文档/Phase_2.8.1_Plan_Complete.md` 末节「结案」）；LOW 尾债（Task7 critic 5 条、Task9 r1/r2/r3 共 11 条、
t3 1 条、run7c key-collapse 覆写 created_at、54b27b59 同类覆写；ingest_solutions CLI
`--expect-learnings` 必填已由 Task 6（攢批119）落地关闭，不再列尾债）
明细见 `~/文档/Phase_2_Plan_Complete.md` §5。

## Phase 2.5（2026-10-28 用户立项；R2 smolagents 强 agentic 层）

计划 `~/文档/Phase_2.5_Plan_detailed.md`（用户 2026-10-28 正式全部签收），执行台账
`~/文档/Phase_2.5_Plan_Complete.md`（与 Plan 冲突以 Complete 为准）。独立账套命名空间
`QuotaLedger.phase25()`（现 56 行，**Phase 2.6 起已封存只读**）；提交 攢批108–133
（`b70a2e2..e259436`，已随 2026-11-02 推送窗口上 origin/main；`main...origin/main`
无分歧，至 `4b5d134`）。

- **Task 0–5b 全闭**（开工基座/呼量 v2/看门狗 SOP/Stage0 探针/agentic 内环/历史墙 A-B
  三态）：Task 5 判「不成立」（两墙干净触帽 16/16+9/9 共 25 呼），Task 5b 同冻结墙
  重测支持 H1（空轮 9/25→0/25），矩阵维持 opt-in/冻结。
- **Task 6 Stage A（换嵌入 mpnet/768→Qwen3-0.6B/1024）诚实负结果**：§7 五条件
  1 PASS/2 FAIL/3 PASS/4 FAIL/5 PASS → 形态甲整目录回滚 mpnet/768（攢批122–128），
  MF4 盲点点预测失准不救济。
- **Task 6 Stage B（r4 = r3 稠密 top-8 ⊕ 精确词 BM25 top-8，RRF k=60）判 `RETAIN_R4`**
  （2026-11-02 绑定跑唯一一次实呼，iter5 45/47=0.9574 / sol 18/18 / lrn 11/12，
  H3=38⊆H4=45 零翻失，9 残留回收 7 其中 gold_id 直中 6，点预测 47/47 逐字相符，
  四表/向量身份不变，ledger 恒 56）：r4 保留为 `PlannerRecall` 构造函数默认
  （按修订 A7 今日无生产消费者，未接线 `DirectionSetter`）；证据
  `/mnt/artifacts/simpraxis/phase25/task6_r4_window/2026-11-02T031952Z/`。
- **Task 7/8/9 经用户 2026-11-02 裁决不再进行**；Phase 2.5 计划内余项只剩 Task 10 收尾。
- **模型换供（硬规则 1，2026-11-02 用户逐字裁决 + 换供应商）**：端点 env 实读为
  Tencent tokenhub（`ANTHROPIC_BASE_URL` 指 tokenhub，原火山方舟 `/api/coding`
  不再使用）；macro=glm-5.3、coding_debug=deepseek-v4.1-flash-1017、
  visual_rollout=deepseek-v4.1-flash-1017（攢批129 `8c7c754`）。stage_models.yaml
  文件头注释为完整沿革与清点取证（新供应商无模型清单端点，GET /models、/v1/models
  均 404，逐字采用用户给定 ID 并以 `--validate-models` 实呼为最终闸）。
- 纪律不变：模型变更仍须 STOP→inventory→用户批准；实呼预算自批但逐次记账、批次件落
  `quota/approvals/`；测试禁网、禁 skip/xfail/TODO；矛盾即结论不翻案；推送/公开是人工
  STOP 门。Phase 2.5 尾债（T5/T6SA/T6SB 各 LOW、PROC-6/7/8）见 Complete §4/§8.1/§9.1。
- **Task 10 收尾已闭（2026-11-02，攢批135–143 `f11b7f5..d196242`，已推送至 `12538e4`）**：
  提交后冷跑两排除全套 **2057 passed/7 skipped/50 deselected/rc=0**（351.95s，HEAD
  `d196242`）+ kbint 活库套 **35 passed/2079 deselected/rc=0**（264.55s）；phase25 账
  56 行（seq 1..56 连续、ts 全严格 Z、54 ok/2 fail seq3/4、by_task T3=6/T5=50、
  by_stage coding_debug×56、5 张批准件批批齐备）；四表 1542/94/44/1（768 维，
  kb 恒 1542）；kbint 的 PROC-7 重盖章已逐行取证（`proc7_restamp_diff.json`：
  solution_chunks 94 行仅两戳记变，id/文本/meta_noprov/内容哈希集/召回全等，
  其余三表 0 变化；机判按**明细重算**，不信差异件自述）；证据包 `--clean` 落
  `phase25/task10_milestone/evidence_pack`（**992 件/6,017,589 B**，manifest 逐件
  sha256/0700-0600/无符号链接/无 >10MB/密钥 0 命中，证据根登记区含
  `@runs/phase25/<thread>/stage_<id>/` tag=@runs/ subdir=phase25 rows_using=0，
  包内 closeout 四件+冷跑/kbint 两日志与根件**逐字节相等**）；
  机判 `scripts/verification/check_phase25_closure.py --report …/closure_judge.json`
  **rc=0 problems=0 gaps=3**（gaps 全为 T7/T8/T9 用户停做挂牌，不计入通过），判决件
  sha256 `1e3355a5…` 与包内副本逐字节相同（**不动点**）；AC 去重计数 102 与逐 Task
  7/7/8/8/18/7/8/8/9/12/10 由看板与机判两套独立实现互证（ids_sha256 `5a6b46a5…`）；
  三签 executor/reviewer/verifier 全 APPROVE 且绑定 `d196242`（R4 的 3 条 REJECT
  整目录留档 `_signs_r1_440cbc2_reject/`，修复见攢批143）。
  环境：容器 0、显存 14 MiB、/mnt 42G/500G(9%)、无看门狗残留。
  `RunSpec.agentic` 保持 **opt-in/冻结**（Task 5 判「不成立」），**已由用户 2026-11-02
  逐字签收**；Phase 3 拟解冻须新预注册 + 新门 1 + 重跑 T4 契约与 T5/T5b 墙，本节签收
  不构成解冻许可；
  偏差/坑表 P11–P16/方法学对账/R4 尾债 T10-M1..M4+L1/Open Items→Phase 3 backlog
  见 `Phase_2.5_Plan_Complete.md` §10。

## Phase 2.6（2026-11-02 用户正式全部签收；路线图状态机 + LIBERO VLA 多 Stage 战役）

计划：`~/文档/Phase_2.6_Plan_detailed.md`（v1.4，3052 行 / 62 AC / 附录 C 53 条）；
执行台账：`~/文档/Phase_2.6_Plan_Complete.md`（进行中；与 Plan 冲突以 Complete 为准）。
独立账套命名空间 `QuotaLedger.phase26()`；**Phase 1/2/2.5 三本账自 Phase 2.6 起一律
封存只读**（`runtime_paths.sealed_ledger_paths()`，写打开即 `SealedLedgerError`）。
预算：受账实呼 Cap T4=56 / T7=16 / T8=70 / T9=2 = 144，`PHASE26_HARD_CAP=148`
（独立字面量，不取 sum(caps)）、`PHASE26_NORMAL_BUDGET=148`；账外 `P26-B0`
（`--validate-models`，恰 4 次付费 POST）⇒ 真实总量 144+4=148。T4 的 56 是
两个 28 呼信封的授权上限之和（V3 `P26-B1` + 2026-11-03 用户 STOP-4a 裁决 (b)
授权的 V4 `P26-B1b`），**按信封授权相加、不按实花相加**（实花 20+28=48）。
开账行：生产账首行 `event="selfcheck"`、`seq=1`、`model=null`、`ok=true`，
**占 seq 不计实呼**（机判用 `ledger_summary(..., allow_events=("selfcheck",))`，
Task 9 对账按 `event != "call"` 排除），永不删除以保 seq 连续。
看门狗缺省监护对象已换期到 phase26（`watched._default_ledger_path()` /
`_default_pause_path()`），且长跑仍须显式传 `--ledger` / `--pause-state` 双保险。

**2026-11-03 收束（STOP-4b 用户裁决）**：Task 0–4b 已闭——Task 4 与 Task 4b 两次预注册
均判 `NOT_ESTABLISHED`（三签 APPROVE 绑 `d880065`，修复批 `950ed67`，冷跑
2198 passed/7 skipped/51 deselected/rc=0）。用户当日逐字指示「就到这里停止 Phase 2.6
的运行：不再进行 Task5 及以后的 Task」⇒ **Task 5–9 一律不执行**（不是延期、不是待裁决）。
`RunSpec.agentic` 维持 **opt-in / 冻结**。账：116 受账实呼 + selfcheck 开账行 1（占 seq 不计
实呼）+ 账外 validate 4 = 真实 120；T4 实花 48（V3 20 + V4 28），帽 56 余 8 不再使用；
三本旧账仍封存只读。T4b 尾债 10 条留档，重启任一 Task 须新票据 + 用户裁决。
**推送状态（2026-11-05 更新）**：本地攒批 12 条已随 2026-11-05 推送窗口
（`12538e4..4b5d134`，共 34 条＝Phase 2.6 的 12 条 + Phase 2.7 的 22 条，经用户
指示过人工 STOP 门）全部上 origin/main；`main...origin/main` 无分歧，HEAD `4b5d134`。
Phase 2.6 的产物口径：Task 0–3 全闭 + Task 4 两次预注册负结论，**不得表述为「全部完成」**。
