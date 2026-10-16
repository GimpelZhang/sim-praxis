# SimPraxis（纸浅）

面向具身智能仿真工具链复现与面试题实证的自治智能体。本仓库为 Product A（智能体系统）本体，
与外部知识库 `embodied-ai-sim-knowledge-base`（Product B，只读挂载）协同。

## Phase 0 能力边界

- Docker GPU 无头渲染沙盒（A800-40GB，真实 EGL 探针 + Xvfb 兼容层）
- 火山方舟 Anthropic Messages 兼容模型网关（三阶段 stage_models 配置 + 实呼校验 + 指数退避 + 配额长熔断状态）
- LanceDB 双路向量中枢（中文嵌入，全量 KB 真实入库，namespace=capabilities/troubleshooting）
- 4 个历史 Tour 的工程范式 blueprint 与能力矩阵

Phase 0 不包含 LangGraph 状态机、HITL 飞书 Gate、RL/VLA 执行（Phase 1+）。

## 本机布局（大文件全部在 /mnt，仓库保持轻量）

- venv：`/mnt/venvs/simpraxis`（系统 Python 3.10 创建，勿用 conda 环境）
- 向量库：Task0b 三层分离后实体迁到 KB 数据层 `/mnt/artifacts/embodied-kb/data/lancedb`（`$KB_ROOT/data` 软链；`SIMPRAXIS_LANCEDB_URI` 可覆盖；旧位置 `/mnt/artifacts/simpraxis/lancedb` 已废弃）
- 视频产物：`/mnt/artifacts/simpraxis/runs/`（`SIMPRAXIS_RUN_DIR`）
- Tour 克隆：`/mnt/assets/simpraxis/tours/`（`SIMPRAXIS_TOURS_DIR`）
- 缓存：`/mnt/caches/{hf,fastembed,modelscope}`，pip 缓存 `/mnt/caches/pip`

## 开发上手

```bash
# 1. 注入凭据与路径（HF/GitHub/NGC 来自 ~/access，网关凭据来自 ~/.bashrc 的 ANTHROPIC_*，均不入库、不回显）
source scripts/00_env.sh

# 2. 开工复核
bash scripts/01_preflight.sh

# 3. 宿主机侧（网关/向量库/测试）
/mnt/venvs/simpraxis/bin/python -m pytest tests -q

# 4. 沙盒侧
docker compose -f sandbox/docker-compose.yml build
docker compose -f sandbox/docker-compose.yml up -d
docker exec sim_praxis_sandbox python /workspace/scripts/verification/verify_egl_real.py
```

网络环境：pip 与 apt 均走清华镜像，HF 走 hf-mirror；详见 Phase 0 实施计划。
