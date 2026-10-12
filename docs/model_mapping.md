# 三阶段模型与换供流程（v4；v4.2 决策 3 收紧：换供先请示）

SimPraxis 生产运行期全程使用火山引擎大模型，经方舟 Anthropic Messages 兼容端点
（`/api/coding`）接入。开发期 AI coding agent 走 shell 自己的 `ANTHROPIC_*` 环境
（Claude Code / ark-code-latest），与本文件的产品运行期路由互不混淆。

## 三阶段配置（2026-10-12 用户直接指令）

> 本表是留档快照；运行真相一律以直接提交的 `stage_models.yaml` 为准（v4.1 n5：
> 供应变化只改该文件，本文档不保证逐字同步，冲突时以 yaml + --validate-models 实呼为准）。

| stage | 职责 | model_id（请求值） | 响应回显 |
|---|---|---|---|
| macro_ideation | 方向探索与可行性调研（Macro Ideation） | `deepseek-v4.1-flash` | `deepseek-v4-1-flash` |
| coding_debug | 环境适配与代码编写（Coding & Debugging） | `glm-5.3-flash` | `glm-5-3-flash` |
| visual_rollout | 闭环仿真评估与视频审阅（Visual & Rollout Evaluation） | `ark-code-latest`（视觉已实证：32×32 红/蓝图） | `auto` |

配置唯一存放处：`orchestrator/gateway/stage_models.yaml`（直接提交，无密钥）。
业务代码/LangGraph 只能按 stage 名调用 `ModelGateway.call(stage, ...)`，禁止直填
模型 ID；火山供应变化时**只允许**改该文件的 `model_id`，但 v4.2 决策 3 收紧：执行期
任何 `model_id` 变更（含同家族日期版本、含置 null）都必须先停止并请示用户，获批后才改。

## 凭据（只写路径名，值永不入库）

- `source scripts/00_env.sh`：从 `~/.bashrc` 按精确 export 名等值解析
  `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`
  （禁止 source 整个 bashrc），导出为 `SIMPRAXIS_LLM_BASE_URL` /
  `SIMPRAXIS_LLM_TOKEN` / `SIMPRAXIS_LLM_DEFAULT_MODEL`；环境已导出时环境优先。
- 缺失或形状（https + ark.cn-beijing.volces.com、ark- 前缀长度形状）不符即非零退出。

## 换供流程（浮动别名被供应侧调整时；v4.2 决策 3：先请示，后动手）

铁序：**停止 → 清点 → 请示 → 获批后改 YAML → 重验**。未获用户明确批准前，禁止改
`stage_models.yaml` 任何 `model_id`、禁止换供应商、禁止现场猜 ID。

1. **STOP**：`--validate-models` 对某 stage 报 404/400/model-not-found，或响应回显名
   发生漂移（点号请求/连字符回显属正常对账，不在此列；"漂移"指回显家族明显改变）时，
   立即停止后续动作；失败证据先落新时间戳目录 `FAILURE.json`（`latest` 不移动，m4）。
2. **清点取证（不花钱）**：`python -m orchestrator.gateway.inventory_models`，日期版本
   清单落 `$SIMPRAXIS_RUN_DIR/inventory/<时间戳>/ark_models.json`（不入库，0600）+
   相对 `latest`。2026-10-12 实证共 135 个，全为日期版本；三个浮动别名不在册属正常，
   inventory 仅供请示取证，router 不对它做成员资格断言。
3. **请示用户**：把【建议的同家族日期版本（如 `glm-5-3-flash-260828`）+ 错误原文 +
   inventory 对应行 + requested/echo 回显对照】报给用户，等待明确批准；无批准不得进入
   第 4 步。
4. **获批后只改一个文件**：`stage_models.yaml` 对应 stage 的 `model_id`，其余内容不动
   （合规置空同理：经用户批准允许 `model_id: null`，但必须同时写 `open_question`，且其
   文本逐字出现在 `.omc/plans/open-questions.md`；null stage 不付费校验、报告
   SKIP-DUE-TO-NULL、调用期抛 StageUnresolvedError）。
5. **重验（预算外另计）**：重跑 `python -m orchestrator.gateway.router --validate-models`：
   三个非 null stage 各一次 `max_tokens=8` Messages ping，断言 HTTP 200 且
   `type=message`；visual_rollout 额外一次 32×32 纯红图视觉 ping（`max_tokens=16`，
   断言答 red/红，用词边界匹配防 "tired" 子串假绿；Ark 图像最小边实测 14px，8px 报
   InvalidParameter）。一次换供重验一批 4 请求、输出上限 40 token；**该批次在计划两批
   8 次的预算之外，另计、逐次把 requested/echo 对照与用户批准记录入 Milestone 报告**。
   证据落 `$SIMPRAXIS_RUN_DIR/model_validate/<时间戳>/result.json` + 相对 `latest`；
   失败时落同根新时间戳目录的 `FAILURE.json` 且 `latest` 不移动（m4）。
6. 单测：`/mnt/venvs/simpraxis/bin/python -m pytest tests/test_gateway.py -q`
   （v4.1 共 18 条，全 monkeypatch，不联网不花钱；换供流程文本变化不影响断言）。
7. 失败语义：429/5xx 先尊重 Retry-After（响应头秒数；m3 两传输层一致），无头再
   指数退避 + jitter；配额判别结构化字段优先（m9：Messages 信封 error.type/error.code
   明示 rate_limit 的即使 message 含 quota 字样也按瞬时限流；无 type/code 才退回
   quota/balance/欠费/余额全文正则），判为配额立即写 PAUSED_WAITING_QUOTA 长熔断；
   404/400/401/403 零重试立即失败（exit 2），消息携带 SWAP_HINT 请示指引（第 1~3 步）。
   `quota_pause.json` 生命周期：校验启动时过期文件自动清除；窗口内默认 BLOCKED
   （exit 3），人工确认恢复用 `--ignore-pause`。Phase 1 LangGraph checkpointer
   直接消费该文件，不得自行无限重试。
