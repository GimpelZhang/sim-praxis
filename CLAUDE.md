# CLAUDE.md — SimPraxis（纸浅）Agent 工作规则

面向具身智能仿真工具链复现与面试题实证的自治智能体。本仓是 Product A（智能体系统）；
Product B 知识库 `~/embodied-ai-sim-knowledge-base` 为公开内容层（Phase 2 Task0b
起解题脚本随其 `solutions/` 公开；数据产物闭源落 `/mnt`，三层分离纪律见硬规则 3）。
Phase 0/1 均已全绿推送：
Phase 0（8 commits 至 `99b3adf`）、Phase 1 Task0–9 + Q2 补点验（11 commits 至 `966fd00`）、
评审 P1/P2 修复批次（2026-10-16 晚，47+1 项 0 付费，三独立评审全 APPROVE，
7 commits `b9ba5f9..8b83193`，341 测全绿，详见 Complete 附 B，余 LOW 在 Phase 2）。
Phase 2 Milestone 2 引擎侧：Task0/0a/0b 六提交已随 2026-10-17 推送窗口在 origin/main
（至 `38ae3e4`），Task1–9 三签提交 51 条本地攒批未推送（M2 终验 HEAD `956dc22`；用户常设指令
「暂不推送，攒批」覆盖计划 L632 里程碑推送，锁内适配 M2-A2）；终态干净树
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
  期望值随活表基线，2026-10-28 Task8 结案活表基线对账；ingest_solutions CLI 历史默认
  --expect-learnings 5 是 Task0b 时点默认，活库实跑须显式传当前活表行数（现 44），
  LOW 债务清理）。资产包：
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
Q2 补沉淀+图外回写通路、Task0b 三层分离（已推送部分），Task1–9 三签提交本地攒批（51 条
`2d83528..956dc22`）。终态：干净树两排除全套 **1086 passed/1 skipped/42 deselected/exit 0**；
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
（计划 L693 登记 Phase 3 backlog）；LOW 尾债（Task7 critic 5 条、Task9 r1/r2/r3 共 11 条、
t3 1 条、ingest_solutions CLI 默认 --expect-learnings 5 活库须显式传当前活表行数（现 44）、
run7c key-collapse 覆写 created_at、54b27b59 同类覆写）明细见 `~/文档/Phase_2_Plan_Complete.md` §5。
