# Task 5b 规格与预注册：执行器侧纯文本协议加固 + 同冻结墙新批次重测

- 立项依据：用户 2026-10-30 D-4 裁决分叉 3（AskUserQuestion，逐字选择
  「先修协议，新批次重测（推荐）」）；Complete §5 在案。
- 前置结论：Task 5 G2 三态 **「不成立」**（verdict-20261030T092635Z.json，
  攢批114 `49b1016`）。本任务是**独立新实验**，G2 结论原样保留、不翻案
  （硬规则 7）；无论 5b 结果如何，不抬帽、不换种、不换模型、不放宽闸。
- 模型不换：coding_debug 仍 kimi-k3.1-pro（硬规则 1，任何模型变更 STOP）。
- Task 6–10 暂缓，直到 5b 出自己的三态。
- 本规格先于实现冻结并单独提交（攢批115）；付费批次三签门在 §7。

## 1. 根因证据链（全部来自冻结 trace，零新呼）

1. **指令冲突（主因）**：smolagents 1.26.0 内置系统提示
   `prompts/code_agent.yaml` 规则 1 逐字：
   > 1. Always provide a 'Thought:' sequence, and a
   > '```(?:python|py)' sequence ending with '` ````', else you will fail.

   我方 `_SYSTEM_CONTRACT` 输出格式行（loop.py:120-121）：
   > 每轮只输出一个 ```python 围栏代码块……不要输出围栏以外的解释文本。

   两条指令对 Thought 散文的态度直接相反；框架规则以
   `custom_instructions`（self.instructions）**追加**在 yaml 之后，
   并未覆盖规则 1。

2. **模型逐字自陈冲突**：墙 B r1（seq23，683ch）原文：
   > "I must output only code block per latest instructions, Thought
   > sequence required by earlier but output format says no explanation
   > outside. I can include Thought as comment?"

3. **空轮形态**（parse 失败 = 0 工具执行，但各占一次付费 seq/轮帽）：
   - 墙 A：r1 Thought-only 561ch；r2「I need act with proper code block
     only per final instructions. First inspect workdir.」（元散文，
     仍无围栏）94ch；r3 同类 97ch；r4「I need emit actual code block.」
     39ch；r5 残缺开栏「…code block.```」84ch；r6 起首次成形
     （Thought+围栏），r7 又退回 1882ch 纯审议散文。
   - 墙 B：r1–r3 Thought-only（683/131/139ch），r4 起成形。
   - 合计 25 呼中 9 轮协议空轮（36%）、2 起 policy deny、放行执行 14 步。

4. **框架反馈弱且反向鼓励**：`CodeAgent._step_stream`（agents.py:1710）
   对 parse 失败只给英文
   `Error in code parsing:\n<ValueError>\nMake sure to provide correct
   code blobs.`；`utils.parse_code_blobs` 的 ValueError 示例逐字是
   `Thoughts: Your thoughts` + 围栏——继续鼓励先写散文；memory.py:138-145
   再包 `Error:` 与 `Now let's retry...`。模型在该反馈下墙 A 连续 5 轮
   才收敛，墙 B 连续 3 轮。

5. **记账事实**：parse 失败轮在 model.generate 内已先 `budget.add_round()`
   （model.py:367-369）并完成实呼/账行，框架随后把
   `AgentParsingError`（AgentError 子类，agents.py:597-599）挂到
   action_step.error 继续下一轮。空轮**从不免费**——这是协议加固可
   honest 介入的前提：我们只提高格式服从性，不改变任何记账语义。

6. **反事实账**（Complete §5 已记）：同帽 16/9 下消除 36% 空轮
   ≈ 多 6/3 个有效轮；墙 B r9 已自检见 rc1（my_check.py 行14 命中
   M1 退化剖面），只差 1 个修复轮即触帽。非知识不可达、非锚点失配
   （故 D-8 不引入 Aider）。

## 2. 科学假设与判定规则（预注册，呼前冻结）

**H1（协议服从性）**：加固后，两墙全部模型轮中 parse 成功轮占比
≥ 0.90（即 25 呼规模下协议空轮 ≤ 2）。

- 协议空轮定义：该模型轮之后因 AgentParsingError 未执行任何工具
  （trace 中 action_step.error 以 "Error in code parsing" 起始）。
  EmptyResponseError 空回答、policy deny 各自单列，**不计入**协议空轮。
- falsify：协议空轮 ≥ 6（服从率 ≤ 0.76），即较基线 0.64 无实质改善。
- 灰带：3–5 轮空转（0.77–0.88），假设既不支持也不证伪，如实报用户。

**H2（工程结果，主预测）**：同帽 16/9、同一冻结九件评分下——

- 墙 B（强预测）：模型在 r5 出生、r9 自检见缺陷的既证轨迹上，获得
  ≥1 个修复轮，scorer rc0 / verdict=pass。
- 墙 A（弱预测）：多出的约 6 个有效轮足以让 agent_solution.py
  **出生并进评分**（基线 16 轮内从未出生）；是否全 pass 不预承诺，
  以冻结 VERDICT_CRITERIA §5 三态为准。

**工程三态**：完全沿用冻结件 `gates/VERDICT_CRITERIA.md` §5 与
verdict.py（不编辑、不解释扩张）：双墙帽内干净 pass = 成立；
干净触帽/超 50% 线等 = 不成立；污染/升级/人工等 = 不确定或不成立
按条文。combine 纯函数，唯一输入是两墙各自 wall_result.json。

**结果解释矩阵**（工程三态 × H1，预登记四种组合的处置）：

| 工程 | H1 | 解释与处置 |
|---|---|---|
| 成立 | 支持 | 协议损耗为主因获证据支持；G2「不成立」仍不翻案（不同实验）；agentic opt-in 是否解冻报用户裁决，本 Agent 不自决 |
| 不成立 | 支持 | 协议可修但非充分：能力墙判定加强，建议维持 opt-in/冻结 |
| 任意 | 灰带 | 机制作废一半，结论挂账报用户，不再加跑 |
| 任意 | 证伪 | 执行器协议加固无效，失败主因回到模型/能力侧；维持 Task 5 处置建议 |

禁止行为：任何组合下都不得因结果不如预期而二次重跑、调温/调帽/
改提示再呼、改评分器、修墙后称通过；本任务**只此一个预注册批次**。

## 3. 0 付费加固设计

### 3.1 允许变更（白名单）

1. **消解提示冲突（loop.py `_SYSTEM_CONTRACT` 输出格式段重写）**：
   不再禁止 Thought，而是把框架强制的 Thought+围栏对规定为**唯一
   合法形状**，并显式禁止半成品：
   - 每轮恰好两部分，顺序固定：一行 `Thought: `（一句话动作）+
     紧随一个 ```python 围栏（块内只放工具调用代码）；
   - 禁止只有 Thought 没有围栏（点名：等于空转，照样占一轮付费与
     轮帽、什么都不会发生）；禁止未闭合围栏、裸 ```、多围栏、
     围栏外其他文本；
   - 给一个 3 行正例（Thought + `print(list_dir("."))` 围栏）和一个
     反例（`Thought: I will inspect the directory.` 之后什么都没有，
     标注「0 个工具调用」）；
   - 终局轮保持同形状，围栏内 finish/fail。
   - 工具契约/五条工作纪律/finish≠通过等既有锚点逐字保留（T4 契约
     测试 `test_system_prompt_declares_tool_contract_and_disciplines`
     必须继续绿）。
2. **精确化 parse 失败观察（新件 `orchestrator/agentic/protocol.py`
   + `ProtocolHardenedCodeAgent`）**：
   - `classify_format_violation(text) -> dict | None`：空回答 /
     散文无围栏（prose_only）/ 裸 ``` 畸形栏 / 围栏未闭合 /
     围栏内无代码，五类标签；合法 Thought+围栏返回 None。
     **有效性以框架 `parse_code_blobs(text, self.code_block_tags)`
     为唯一裁决器**（产品配置 tags=("```python","```")；其内置
     ("```(?:python|py)","\n```") 回退与 ast.parse 裸 blob 分支一并
     生效——无围栏但本身是合法 Python 的输出会被框架执行，不算空轮，
     分类器不得另立判据）；分类器只在解析抛错后对原文做形态归因。
   - `build_parse_feedback(text, exc, streak) -> str`：**保留**框架
     原英文两行（证据连续），追加中文精确块：违例标签、合法形状
     复述、`这是连续第 N 次协议空转（仍计 1 次付费轮与轮帽）`。
     反馈是纯常量形状，**不插入任何任务/墙/文件名信息**（契约测试
     锁：两个不同任务下同输入输出逐字节相同）。
   - 子类仅 override `_step_stream`：以 smolagents 1.26.0 安装源逐字
     复制为基线，**唯一 hunk** 是 parse except 分支改调
     `self._protocol_parse_feedback(output_text, e)`；streak 从
     `self.memory.steps` 尾部连续 error 步计数（不加第二个 hunk、
     不抄其他段）；Auto-close 围栏等其余逻辑逐字不动。
   - build_agent 换用子类；model.py / executor.py / caps.py /
     policy.py / run_wall.py / 冻结九件一律不改。
3. **combine 报告加只读字段**（report-only）：verdict JSON 增
   `prior_g2_verdict` 指针（指 verdict-20261030T092635Z.json，
   标「不成立，不翻案」），不参与判定；契约测试覆盖。

### 3.2 禁止变更（黑名单）

- parse 失败轮不占账/不占帽、任何形式的同轮免费重试或一轮多呼；
- 反馈中夹带任务提示、答案线索、墙的判分标准；
- 温度/max_tokens/thinking/timeout/stop 序列、模型/供应侧、容器镜像、
  grants、workdir 播种、任务文本、冻结九件、verdict.py、轮帽 16/9；
- 改 BudgetTracker 语义、改 fingerprint 版本串造假旧缓存命中
  （提示变了就必须新指纹新出生——实测断言新提示指纹 ≠ 旧提示指纹）；
- 编辑 gates/ 任何文件（T5-LOW-1 冻结措辞只登记不修，继续挂账）。

## 3.3 实现缝细化（2026-10-30，实现前补记，先于任何产品代码冻结）

规格 §3.1 原写「逐字复制 _step_stream 127 行 + 唯一 hunk」。实现前复核
1.26.0 源码确认存在更小等价缝，改采之（行为不变、表面更小）：

- 事实锚：`memory_step.model_output = output_text`（agents.py:1697）
  在 parse 块（1702-1711）**之前**赋值，含 auto-close 补闭栏后的
  实际入解析文本；`AgentParsingError`（smolagents.utils:104，
  AgentError 子类）是 parse 块唯一异常类型。
- 子类 override 改为 ~10 行**生成器包装**：`yield from
  super()._step_stream(memory_step)` 捕获 AgentParsingError，从
  `memory_step.model_output` 取被解析原文，重抛经
  build_parse_feedback 精确化的**新** AgentParsingError（同
  (message, logger) 构造形状，from exc 保留链）；成功路径逐字透传，
  parse 失败在 yield 任何工具调用之前发生故零重复副作用。
- 因此不复制上游方法体、零全局名漂移面；§4 第 3 项 golden anchor
  调整为双锚：①上游 `CodeAgent._step_stream` 安装源 sha256 前缀
  `e027f0eaa32a8d94` 钉死（版本漂移即红，硬规则 8 已 pin 1.26.0）；
  ②行为锚——用 ScriptedGateway 对**库存 CodeAgent** 喂畸形输出，
  断言抛 AgentParsingError 且 memory_step.model_output 已就位
  （包装缝依赖的两个上游事实任一被上游改坏即红）；另测子类包装
  成功路径零改写（透传 ActionOutput/final answer 不变）。
- 规格其余各节（记账语义、黑白名单、H1/H2、批次设计）一字不变。

补记②（同日，写产品件时核实，先于付费批次冻结）：
1. agents.py:1693-1695 在解析前自动为不以 ``` 结尾的输出补一个结尾
   ```，于是 parse 失败后「Thought 散文」与「残缺裸 ```」文本上不可
   区分（末尾都恰有一个无开栏的 ```），纠正动作相同——分类器合并为
   单一 `no_code_fence` 标签，文案点破「自动补的闭栏不能替代开栏」；
   `fence_unclosed`/`empty_code_block` 仅对纯函数直接调用可达（行为锚
   测试锁此不可达性：缺闭栏的输出会被 auto-close 补成可解析）。
2. smolagents max-steps 时 `provide_final_answer` 会再发一次 generate
   （agents.py:625-637），该次在 model.generate 入口先撞
   budget.add_round 抛 CapExceeded、不触网，故「干净触帽」的账是
   付费呼=帽（16/9）、终局 kind=round_cap——与 G2 实测一致，hermetic
   触帽剧本按此断言（calls==cap、steps==0）。
3. run_wall 增 `--experiment {g2,5b}` 选择器（默认 g2 历史路径
   逐字不动）：5b 选票据 P25-B4/B5、线程前缀 t5b-、run_meta/
   wall_result 加 experiment 字段；combine 从两墙结果派生实验名、
   拒跨实验聚合，5b 输出挂 prior_g2_verdict 只读指针。LOCK 重入仍只
   认九件哈希（B4/B5 新批次不改锁，是「同冻结墙」的物证）。

## 4. Hermetic 测试设计（0 付费，先红后绿）

新件 `tests/milestone25/test_task5b_protocol_hardening.py`，全部
ScriptedGateway/禁网：

1. **分类器**：嵌入冻结 trace 的逐字短样本（墙 A r2/r4/r5/r6、
   墙 B r1/r4、空串、合成未闭合/空块/合法 finish），逐案锁标签；
   r6/r4 合法混合形状必须 None（防误伤有效轮）。
2. **反馈常量性**：含框架英文原两行 + 中文标签 + streak 文案；
   两任务下输出逐字节相同；无路径/任务词注入面。
3. **Golden anchor**：对安装环境 `inspect.getsource`
   CodeAgent._step_stream 取 sha256 钉死 1.26.0 基线；断言子类
   override 与上游源仅差登记的唯一 hunk（old→new 替换后逐字相等）。
4. **E2E 五空转剧本**：ScriptedGateway 逐字回放墙 A r1–r5 后接 r6
   合法再 finish——断言：gateway 实呼 7 次、rounds_used=7、每轮有
   seq（空轮不免费）；第 2–6 次呼的 payload 中含上一轮中文违例
   标签；list_dir 恰好执行一次；终局 finish。
5. **收敛剧本**：反馈响应型假模型（见中文标签即出合法形状）2 轮
   收敛，工具只执行一次。
6. **触帽剧本**：永远散文的模型 → KIND_ROUND_CAP，呼数恰好=帽、
   0 工具执行（证明加固不放水、不吞帽）。
7. **提示契约**：旧冲突句「不要输出围栏以外的解释文本」已移除；
   新锚点齐（成对形状/正例/反例/空转换算）；T4 旧契约测试仍绿。
8. **指纹诚实**：同 task/round/prefix 下新提示 disk_key ≠ 旧提示。
9. agentic 包既有 AST 白名单/零 model-id/零网络闸自动覆盖新件。

## 5. 新付费批次设计（呼前再冻结一次票据）

- 入口不变：`scripts/phase25/walls/run_wall.py run <wall>`
  （SIMPRAXIS_ALLOW_PAID=1 + paid marker 双守卫）；九件哈希比对
  写锁分支自动复用；任务文本 `_task_text`、IMAGE
  （abf96496a6d7 / phase25-t4）、grants、--network=none/--cap-drop
  ALL/--read-only/--pids-limit 256/--memory 4g/tmpfs、CUDA_VISIBLE_
  DEVICES=""、墙 B 宿主主 venv 评分，全部同 Task 5。
- 票据：P25-B4（墙 A，16）、P25-B5（墙 B，9），开呼前自批落
  quota/approvals/（2026-10-16/28 预算自治；ledger task 仍记 5，
  batch 区分）；seq 从 32 起连续；储备 3 呼**不动**（本批不是
  污染重跑，不适用储备）。
- call_cache=None（run_wall 固有）；提示变更自然产生新指纹，
  G-15 三证（seq 推进 + 新鲜 chunk0 ts + 关键词实文档计数）逐墙取证。
- 顺序 A→B；各自 `scripts/ops/run_watched.sh` 看门狗（死亡+静默
  双覆盖，静默帽 1200s，fake notifier 演练）；双密钥扫描先于挂载；
  终局/评分后容器零残留断言、显存收尾、进程全回收。
- 每墙产物新 stamp 目录（旧 090948Z/092227Z 只读不碰）；combine
  生成新 verdict-<stamp>.json（含 prior_g2_verdict 指针），旧 verdict
  件原样保留。

## 6. 报告与三签门

- §7 门 1（呼前）：本规格 → 代码+hermetic 测试（冷跑全套两排除
  绿）→ reviewer / verifier 两 lane 独立签（含 golden anchor 复核、
  黑名单逐条核查、0 付费 ledger 行数不变＝31）。三签齐才准开票据。
- §7 门 2（呼后）：spike_report_5b.md（台账、两墙逐轮取证、H1/H2
  判定、矩阵落格、G-15/安全/看门狗、与 G2 的不翻案声明）→
  reviewer / verifier 再三签；verifier 独立重放墙 B 评分器、
  重算空轮率与 seq/token 账。
- 任何门不过：停，报用户，不带病呼下一轮。
