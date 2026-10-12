# CLAUDE.md — SimPraxis（纸浅）Agent 工作规则

面向具身智能仿真工具链复现与面试题实证的自治智能体。本仓是 Product A（智能体系统）；
Product B 知识库 `~/embodied-ai-sim-knowledge-base` **只读挂载**。Phase 0 已全绿推送
（8 commits，HEAD `99b3adf`）。完整实施规范与执行期偏差见仓库外文档：
`~/文档/Phase_0_Plan.md`（规范 v4.2）与 `~/文档/Phase_0_Plan_Complete.md`（实际执行记录，
与 Plan 冲突时以 Complete 为准）。

## 架构一句话

- `sandbox/`：CUDA 12.4 EGL 无头渲染沙盒（A800，真 GPU 探针；OSMesa 仅降级诊断，exit 2 不算通过）
- `orchestrator/gateway/`：火山方舟 Anthropic-Messages 网关（`/api/coding`），三阶段
  stage_models + 退避 + 配额长熔断；唯一模型配置点 `stage_models.yaml`
- `memory_flywheel/`：LanceDB 双路向量中枢（namespace=capabilities/troubleshooting）+ 切块/矩阵
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
   禁止直填模型 ID。macro/coding 两阶段视觉未测，禁止向其发 image block。
2. **付费调用有预算**：经方舟的实呼都计费；Phase 0 已用满计划内 8 次。新的实呼/重验批次
   须用户知情批准、逐次记录；单测一律 monkeypatch（`tests/test_gateway.py`），禁止真实联网。
3. **KB 仓永远只读**：不提交、不修改；HEAD 钉死 `aac53313c620efa6217fd51a55834b006d78e689`
   （preflight/ingest/e2e 三处断言）。
4. **凭据纪律**：只从 `~/access/` 与 `~/.bashrc` 的 `ANTHROPIC_*` 按 export 名等值读取；
   禁止 `source ~/.bashrc`、禁止回显/入 URL/入日志/提交；git 认证用内存 extraheader 不落盘；
   fixture 不得像真密钥。
5. **GPU/容器收尾**：任务结束容器必须 `docker compose down`（含已退出容器不得残留），
   显存 ≤200 MiB；不擅杀他方 GPU 进程。禁止 `docker system prune -a`、不改 daemon.json/
   不迁移 data-root（本机有他方镜像）。
6. **删除守卫**：禁止 mkfs/fdisk/format；任何 rm 前先 ls，目标必须是 simpraxis 自有字面路径
   或 `case` 后缀守卫；不碰他方目录（`/mnt/artifacts/{run_a15_batch30_20261001,
   smoke_20261003,stage2}`、`/mnt/venvs/{alpamayo,alpasim,flashdreams}` 等）。root 属主文件
   用 `~/access/access_methods.txt` 口令管道 sudo，不回显。
7. **断言不许放水**：不弱化查询/阈值、不 test.skip、不留 TODO 占位、不用 numpy 假帧/硬编码
   KB 条目。计划 heredoc 与现实冲突时，做最小"锁内适配"并在代码注释（搜 `锁内适配` /
   `适配（计划`）与 commit body 留痕；模型类失败永远先走规则 1。
8. **版本与网络**：不松 pinned 版本；pip/apt 清华源，HF 走 hf-mirror；GitHub 大克隆遇
   HTTP/2 curl 92 用 `git -c http.version=HTTP/1.1 -c http.postBuffer=524288000`；
   容器构建 `SIMPRAXIS_USE_PROXY=0` 直连。
9. **Git**：每条提交 author `Junchuan Zhang <zjunchuan@gmail.com>`（local config 已设），
   trailer `Co-Authored-By: Claude Code <noreply@anthropic.com>`；提交后跑
   `bash scripts/verification/check_git_author.sh`。已推送历史不得改写。
10. **Tour 纪律**：只克隆与结构化（LFS 默认 skip），Phase 0/1 不实际跑 tour、不装仿真平台本体。

## 向量库要点

- 当前嵌入 `paraphrase-multilingual-mpnet-base-v2`（768 维，用户批准替换 bge-512）；
  **换嵌入模型必须 `--rebuild` 整表重建**（跨维度用 `VectorEngine.drop_existing()` 先删后建，
  混维度守卫其他路径恒开）。重建后重跑 Q1/Q2/Q3 召回与 `check_chunk_integrity --expect 104`。
- 入库：`python -m memory_flywheel.scripts.ingest_kb --rebuild`；切块常量
  TARGET_CHARS=1200/HARD_CHARS=2000，Qxx 原子块，文件级幂等替换。

## 常用命令

```bash
bash scripts/verification/gate_static.sh                      # 六步静态门禁（提交前必跑）
/mnt/venvs/simpraxis/bin/python -m pytest tests -q            # 全部单测（0 付费）
/mnt/venvs/simpraxis/bin/python -m pytest \
  tests/milestone0/test_milestone0_e2e.py -v                  # 注意 test_04 含真实付费调用
/mnt/venvs/simpraxis/bin/python -m memory_flywheel.scripts.validate_capability_matrix
python scripts/verification/check_blueprint_evidence.py
```

## Phase 边界

Phase 1 才允许：LangGraph/状态机、飞书 HITL、RL/VLA 训练、RSI 指标、World Judge、实跑 tour。
配额熔断 schema（quota_pause.json）待 Phase 1 设计者签收；SAPIEN/Bullet/传感器光学噪声等
矩阵缺口在 Phase 1 backlog。
