# Task 5 历史墙 A/B spike 报告（G2 证据件）

- 报告生成：2026-10-30（UTC+8）
- 三态结论：**不成立**（两工程闸均到子帽未过，零升级、零人工、零供应污染）
- D-4：**STOP 报用户**；`d4_stop_required=true`（聚合件
  `runs/verdict-20261030T092635Z.json`）
- 模型：coding_debug 阶段 kimi-k3.1-pro（2026-10-27 用户硬规则 1 裁决换供，
  本 Task 无任何模型变更）
- 冻结件：首呼锁 `manifests/LOCK.json`（thread `t5-walla-20261030T090948Z`，
  九件闸哈希），首呼后闸件/判据/评分器一字未改；LOCK 对墙 B 重入逐位比对通过。

## 1. 判决与依据（对照冻结判据 §5 短路表）

| 墙 | 工程闸结果 | 实呼/子帽 | cap_exhausted | 升级 | 人工 | 污染 | 50% 线 |
|---|---|---|---|---|---|---|---|
| wallA（LIBERO 信息墙） | fail（`agent_solution.py` 从未出生，评分器 FileNotFoundError） | 16/16 | true | 0 | 0 | 0 | n/a（未 rc0） |
| wallB（流体 B-eng 工程墙） | fail（12 项检查 11 PASS，仅 M1_vessel `degenerate profile segment`） | 9/9 | true | 0 | 0 | 0 | n/a（未 rc0） |

`engineering_verdict` 纯函数输出：

```json
{"verdict": "不成立",
 "reasons": ["工程闸到子帽未过：['wallA', 'wallB']（不抬帽/不换种/不放宽）"]}
```

两墙均为**干净触帽**：无供应商错误、无空响应（supplier_errors=0、
empty_responses=0）、无引擎脚手架升级、无任何人在环操作。按 AC-T5-4/§4，
储备 3 呼的唯一支取条件（供应侧污染三证）不成立，**储备原封未动**；不存在
重跑机会，也不得抬帽/换种子/放宽闸/换模型（硬规则 7，负结果照发即结论）。

B-cap 独立轴（§6）：**未进入独立结论形态**。B-eng 未过时 `b_cap_axis`
标签 `b_eng_fail_d4_stop, d4_stop=true`。评分器 b_cap 块（checker:294-305）
在 K6 之后**无条件独立执行**、不被 M1 短路（其字面默认是 `b_cap: None`）：
它实际调用了模型自著的 `design_pour_trajectory()` 与 `kinematic_witness()`，
score.json 的 b_cap 无 error 键即为真实计算证据。数值 speed_max=0.0 /
clearance_min=0.02 来自模型的退化轨迹设计——spout 写成空间恒定点
（`np.tile([0,0,1.045],(161,1))`）、clearance 恒 0.020（`np.full`），倾角
时序虽存在但壶嘴不动、间隙不变，等价于「不倾倒」；故 speed≤0.20 与
clearance≥0.012 两 pass=true 是**空虚满足（vacuous pass），不构成能力证据**，
run_wall 仅在 B-eng 过时才计算 `b_cap_passed`，此处为 null。「G.8 能力墙在
工具层复现」结论的前提（B-eng 过）未达成。（注：本 spike 的 B-cap 本就只有
运动学簿记、无 Genesis 物理仿真；未执行的是物理，不是簿记。）

## 2. 呼量与预算对账（规则 2）

| 项 | 值 |
|---|---|
| Task 5 实呼 | **25**（P25-B2 16 + P25-B3 9），全部 ok，seq 7–31 连续单调 |
| 基础池 25 | 25/25 用尽（A 16 + B 9，禁止跨墙借支被执行：两批独立自批票） |
| 储备 3 | **0/3 支用**（无污染，分支不可达即正确） |
| 科学硬帽 28 | 25 ≤ 28，合规 |
| preflight_blocked | 0（两次 --preflight 前后账行 6→6、22→22） |
| phase25 ledger 总行 | 31（Task 3 P25-B1 6 行 + Task 5 25 行；封存只读） |
| token（A） | 92,658（in 90,391 / out 2,267） |
| token（B） | 83,921（in 75,428 / out 8,493） |
| 时间戳 | 全部严格 Z 后缀；A 09:09:57–09:11:57Z（约 120s），B 09:22:35–09:25:23Z（约 168s） |
| call_cache | 两墙均 None，cache_hits=0（真出生） |

账侧另有 `tokens_cache_read`（A 累计 68,096 / B 39,168，cache_create=0），
那是方舟对重复系统提示的**供应商账单侧 prompt-cache**，与本地重放缓存
（call_cache=None / cache_hits=0）是两个层面，不矛盾、不影响真出生三证。

对照基线：sA=60、sB=30（B-eng；另 B-cap 9 不进分母），50% 效率线 30/15。
两墙均未产生 calls_to_rc0（评分器从未 rc0），效率线不可达即负结果，不做
任何「若多给 N 轮即可过」的外推——那在预注册之外。

## 3. AC-T5-3 逐墙指标与逐轮取证

### 3.1 墙 A（信息墙型，Task7 LIBERO）

- rounds=16，工具步=10，token 92,658；trace 39 事件；policy deny=1（r8，
  `run_cmd` argv 含 shell 元字符被拒，exit=2，AC-T5-6 deny 实战入证）。
- 评分：3 项环境探针 PASS（numpy 1.26.4 / mujoco 3.1.6 / robosuite 1.4.0）
  后 `FileNotFoundError: /work/agent_solution.py`，rc=1，elapsed 0.534s。
- workdir 终态：仅 `inspect_api.py`（204B），交付物从未出生。

| seq | r | 模型产出 | 判定 |
|---|---|---|---|
| 7 | 1 | 561ch Thought 散文（自省「是否允许 API 内省」） | 空轮 |
| 8 | 2 | 94ch「需要正确 code block」 | 空轮 |
| 9 | 3 | 97ch 同义重复 | 空轮 |
| 10 | 4 | 39ch「需要真正的 code block」 | 空轮 |
| 11 | 5 | 84ch，尾随孤立 ``` | 空轮（残缺围栏） |
| 12 | 6 | 首个可执行调用 `list_dir .` | 有效 |
| 13 | 7 | 1882ch 长审议，仍无工具调用 | 空轮 |
| 14 | 8 | `python -c`（含元字符） | **policy deny** |
| 15–22 | 9–16 | write/run(rc1)/read/write/run(rc0)/write/run(rc0)/write，全花在 `inspect_api.py` 探针 | 有效内省，已定位 `libero.libero.envs.env_wrapper.ControlEnv` 构造签名 |

16 轮中 **6 轮（37.5%）零工具产出**；有效动作始于 r6，有效生产仅 10 步；
到帽时模型仍在 API 内省阶段，尚未开始写交付物。

### 3.2 墙 B（合同反馈型，Task8 run_5 B-eng）

- rounds=9，工具步=6，token 83,921；trace 21 事件；policy deny=1（r7 同类
  argv 元字符拒绝）；r5 单轮延迟 85.3s（latency_ms=85349；17,378ch 原文，一次性出生完整方案）。
- workdir 终态：`agent_solution.py`（8,221B）+ 模型自建自检 `my_check.py`
  （4,050B）。
- 评分 12 项 **11 PASS / 1 FAIL**：

| 检查组 | 结果 | 实测 |
|---|---|---|
| S0 无哨兵字面值 / S1 无硬编码 | PASS | 空违规 |
| M1_vessel 程序化单体中空壶 | **FAIL** | `make_vessel_mesh()` line 68 `raise RuntimeError("degenerate profile segment")` |
| M2 开口壳拓扑 | PASS | faces=768, boundary=2304, nonmanifold=0 |
| M2 非流形 fan | PASS | boundary=2, nonmanifold=1 |
| M2 历史 1536 闭壳 | PASS | faces=1536, boundary=0, nonmanifold=0 |
| K1–K4 运动学见证判违 | PASS | same_witness=true；K2/K3/K4 期望违规三项/一项/一项逐位复现 |
| K5/K6 抛错类检查 | PASS | 非有限输入与退化输入正确抛错（raise 类，无 same_witness 字段） |

| seq | r | 模型产出 | 判定 |
|---|---|---|---|
| 23 | 1 | 683ch Thought | 空轮 |
| 24 | 2 | 131ch 同义 | 空轮 |
| 25 | 3 | 139ch 同义 | 空轮 |
| 26 | 4 | `list_dir .` | 有效 |
| 27 | 5 | 85s 长考后一次性 write `agent_solution.py` 8,221B | 有效（出生） |
| 28 | 6 | read 回读 | 有效 |
| 29 | 7 | argv 元字符 | **policy deny** |
| 30 | 8 | write 自检器 `my_check.py` | 有效 |
| 31 | 9 | `python my_check.py` **rc=1**（模型已自检发现失败，但轮帽耗尽，无修复轮） | 有效但终局失败 |

9 轮中 **3 轮（33%）零工具产出**；失败形态是「差一个缺陷的修复轮饥饿」：
拓扑仪与判违仪全部合格（含历史值 2304 边界边/1536 闭壳的逐位复现），唯一
不合格点是壶网格旋转剖面的退化线段，模型在 r9 已自己测到 rc=1，第 10 轮
不存在。

### 3.3 合并形态

25 个实呼中 9 个（36%）是纯文本协议空轮（残缺围栏/只说不调）；其余 16 个
工具步含 2 个 policy deny（同类 argv 元字符，exit=2 未执行），**实际放行
执行 14 步**（steps_used 口径 10+6=16 含被拒尝试，deny 同样消耗一轮）。
两墙失败机理不同但同源：**出生前协议损耗 + 冻结紧子帽下修复轮不足**；
不是知识不可达，也不是仪器不会造。

## 4. G-15 真出生三证与安全/环境取证（AC-T5-6/7）

- seq 推进：A seq7–22、B seq23–31 与 trace `__model_round__` 一一对应，
  `ledger_rounds_match=true` 两墙；
- 新鲜时间戳：first/last ts 均在各自 run window 内，全部 Z；
- 关键词文档命中（trace/full/raw 实文档计数，非模型自述）：
  A：ControlEnv 20 / libero_spatial 9 / OSC_POSE 5 / use_camera_obs 3 /
  build_env 3（61 文档）；
  B：edge_manifold_counts 6 / make_vessel_mesh 8 / kinematic_witness 6 /
  evaluate_precheck 6 / watertight 8（33 文档）；
- call_cache=None、cache_hits=0，无重放假跑面。
- GPU：两墙采样全程峰值/终值均 14 MiB（零 GPU，CPU 闸纪律成立）。
- 容器：loop 终局/评分后两次 label 残留断言均空；收尾 `docker ps -a` 0 个
  本仓容器；无 prune、无 daemon 改动。
- 网络：模型容器 `--network=none`；评分容器隔离挂载（/opt/inst 与冻结 OBJ
  从不挂进模型容器，反作弊挂载分离经契约测试与实跑双证）。
- policy deny 两起留 forensics（白名单实战拦截，非纸面）。
- 看门狗（Task 2 dogfood）：两跑均七件套 + 独立 PDEATHSIG toucher +
  30s 元 guard（fake notifier drill），静默帽 1200s；两跑 rc=0、零告警
  （alerts.jsonl 13 历史行中 0 行 t5wall*；fake notifier 在零告警时不创建
  jsonl，drill 目录为空即「无事件」证据），双后台同回合回收。
- 冻结快照只读挂载、0600/0700、双密钥扫描先于挂载（首呼前批次已入证），
  快照零写入。

## 5. D-8 Aider 两零件子建议

预注册触发条件：**仅当 trace 显示锚点失配/定位贫化是主要剩余墙**才建议
vendor RepoMap/diff 容错应用器。本 spike 证据不支持该前提：

- 墙 B 模型在一次出生中即逐位复现历史拓扑量（2304/1536/nonmanifold fan）
  与全部 K1–K6 期望违规，锚点定位是成功的；唯一失败点是自身剖面几何的
  退化线段，属代码正确性，不属定位贫化；
- 墙 A 模型在 r13 前已自主定位 `ControlEnv` 及其构造签名，失败于协议空轮
  损耗与子帽内未及出生，同样不是锚点问题。

**建议：不引入 Aider 两零件**（默认不预引入的预注册立场维持）。主要剩余墙
是（1）smolagents 纯文本 `<code>` 协议在 kimi-k3.1-pro 上的首轮格式服从性
（36% 空轮）与（2）紧帽下修复轮饥饿；前者的对症方向是执行器侧协议/解析
加固（属产品变更，需另立任务与回归，不得在本负结果上顺手改），后者正是
预注册帽的意图（以帽证伪，不以帽迁就）。若未来生产遥测显示锚点失配成为
主因，再按 D-8 重新评估（Apache-2.0 NOTICE）。

## 6. Track1 灰度建议

三态为「不成立」，按 AC-T10-10 处置建议：

- **RunSpec.agentic 保持 opt-in/冻结，不进生产默认**；Task 9（真实 VLA
  战役）不得在无用户显式裁决下以 agentic=true 启动；
- 证据上 agentic 内环「接近成立」的形态是墙 B：单缺陷、单文件、合同反馈
  即时的仪器题——11/12 检查通过且模型具备自检意识。此类任务可列为**未来
  协议加固后首批重测对象**（不是生产灰度许可）；
- 墙 A 类多文件 API 探索题在当前模型+协议下出生都不稳定，暂不建议任何
  灰度；
- 本结论不回收 KB（无 Gate2 approve）、不发成功白皮书、不改任何预注册量。

## 7. 三签

- executor（工程执行）：LEAD（team-lead），两跑全程执行、取证、冻结纪律；
- reviewer（证据完整性评审）：见 `spike_review.md`（独立 lane，只读）；
- verifier（独立重算全部计数）：见 `spike_verification.md`（独立 lane，
  从 ledger/trace/score/manifest 原始件重算，不信任本报告数字）。

## 8. 已登记的非阻塞项（verifier MINOR，均不改判决）

1. **冻结判据措辞瑕疵（只登记，不修字）**：`VERDICT_CRITERIA.md` §1 代入
   行写「A = half_up(16.667) = 16、B = half_up(8.333) = 9，clamp 未触发」，
   严格算术是 raw half-up 17/8、**两侧 clamp**（A 封顶 16、B 保底 9），
   最终子帽 16/9 与和 25 均正确；可执行真相源 `verdict.py` 经 verifier
   独立双算（整数恒等式 + Decimal ROUND_HALF_UP）复算无误。该件在首呼后
   冻结，按纪律一字不动，瑕疵仅在此与 Complete 文档登记（T5-LOW-1）。
2. 报告 K1–K6 措辞已按 verifier 意见改为 K1–K4（same_witness）+ K5/K6
   （raise 类）分列。
3. drill 无 notifier.jsonl＝零告警设计语义（fake notifier 无事件不建档），
   已在 §4 注明。
4. 「16 工具步」含 2 次 policy deny（steps_used 口径），实际放行执行 14 步，
   已在 §3.3 注明。
5. **T5-LOW-2 证据保留（reviewer MINOR-3，后续任务改进，不影响本判）**：
   (a) 两次 `--preflight` 的账行 6→6、22→22 原始 stdout 未单独归档（watchdog
   stdout 只包实跑）；结论由 cmd_preflight 写账即 SystemExit 的代码路径 +
   ledger 全局 seq 起点 7/23 + 0 条 non-call 行三方互证成立。
   (b) 两 freeze 脚本写同一 `snapshots/secret_scan.json`
   （freeze_common.py 固定文件名），墙 B 后写覆盖墙 A 的 Python 引擎扫描件；
   墙 A 段存在即当时 clean（write_secret_report 命中即中止冻结），存活 shell
   引擎为全仓扫描覆盖全部闸件。后续扫描件按墙分名。

## 9. 证据索引

- 冻结根（/mnt，0700）：`$SIMPRAXIS_PHASE25_ROOT/walls/t5_freeze/`
  - `manifests/LOCK.json`、`manifests/freeze_manifest.json`
  - `runs/wallA/20261030T090948Z/`（run_meta/task_prompt/outcome/score/
    forensics/wall_result/gpu_mib/traces/）
  - `runs/wallB/20261030T092227Z/`（同构）
  - `runs/verdict-20261030T092635Z.json`（聚合判决）
- 看门狗：`watchdog/runs/t5walla-20261030T090939Z/`、
  `watchdog/runs/t5wallb-20261030T092219Z/`（各七件套，exit_code=0），
  drill notifier 两目录（零告警）
- 票据：`quota/approvals/P25-B2.json`（16）、`P25-B3.json`（9）
- 账：`quota/ledger.jsonl`（phase25 31 行）、`quota/counters.json`
- 仓内冻结件：`scripts/phase25/walls/gates/`（九件，首呼后零改动）、
  `verdict.py`、`run_wall.py`、`spike_backend.py`、
  `tests/milestone25/test_task5_wall_contracts.py`
