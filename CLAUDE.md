# CLAUDE.md — SimPraxis（纸浅）Agent 工作规则

面向具身智能仿真工具链复现与面试题实证的自治智能体。本仓是 Product A（智能体系统）；
Product B 知识库 `~/embodied-ai-sim-knowledge-base` **只读挂载**。Phase 0/1 均已全绿推送：
Phase 0（8 commits 至 `99b3adf`）、Phase 1 Task0–9 + Q2 补点验（11 commits 至 `966fd00`）、
评审 P1/P2 修复批次（2026-10-16 晚，47+1 项 0 付费，三独立评审全 APPROVE，
7 commits `b9ba5f9..8b83193`，341 测全绿，详见 Complete 附 B，余 LOW 在 Phase 2）。
规范与执行记录在仓库外（与 Plan 冲突时以 Complete 为准）：
`~/文档/Phase_0_Plan.md`（v4.2）+ `Phase_0_Plan_Complete.md`；
`~/文档/Phase_1_Plan_detailed.md`（v1.2）+ `Phase_1_Plan_Complete.md`（偏差 A1–A24、坑 P1–P10）。

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
- `memory_flywheel/`：LanceDB 双路向量中枢（namespace=capabilities/troubleshooting）+ 切块/矩阵；
  学习回写只进独立新表 `phase1_learnings`，`kb_chunks` 永不写
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
3. **KB 仓永远只读**：不提交、不修改；HEAD 钉死 `aac53313c620efa6217fd51a55834b006d78e689`
   （preflight/ingest/e2e 三处断言）；kb_chunks 1542 行/104 Q 块基线不变。
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
- `phase1_learnings`（12 列含 vector，复用 mpnet 不二次下载）与 kb_chunks 同库独立；
  当前 4 行。**生产召回尚未接线（production_recall_hits=0），勿声称飞轮已闭环**——Phase 2 项。

## 常用命令

```bash
bash scripts/verification/gate_static.sh                      # 六步静态门禁（提交前必跑）
/mnt/venvs/simpraxis/bin/python -m pytest tests -q \
  -k "not test_04" -m "not paid"                              # 0 付费全套（收尾必带两排除项）
# test_04 已有 paid marker + SIMPRAXIS_ALLOW_PAID=1 守卫：裸跑自动 skip 零触网；
# 真跑 test_04 批次须 SIMPRAXIS_ALLOW_PAID=1 且按规则 2 自批落 quota/approvals/
/mnt/venvs/simpraxis/bin/python -m memory_flywheel.scripts.validate_capability_matrix
python scripts/verification/check_blueprint_evidence.py
```

## Phase 边界

Phase 1 已完成：LangGraph 状态机、飞书真人双 Gate（含真实 reject→human_failure 补救线程）、
Rung1–4 分级（Rung4 VLM 为 record_only，用户签收）、四 track 平台实跑与对照、phase1_learnings
隔离新表、证据包（2026-10-16 评审批 --clean 重建 276 文件，两存活库 backup API 快照无侧车）
+ RSI 看板、Task9 真人 HITL 抽验 6/6 一致；评审 P1/P2 47+1 项已修（Complete 附 B）。

仍待 Phase 2（未经用户指示勿启动）：RL/VLA 训练、World Judge 实现、RSI Level 3 元 prompt
自演进、实跑四 tour；LearningStore 生产召回接线、ledger tokens 回填/时区统一、容器非 root 运行、
SAPIEN/Bullet/传感器光学噪声矩阵缺口、Q2 光学模型参数标定、quota_pause.json schema 签收。
