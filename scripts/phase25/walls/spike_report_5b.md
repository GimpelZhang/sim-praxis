# Task 5b 协议加固批次 spike 报告（新实验证据件）

- 报告生成：2026-10-30（UTC+8）
- 实验身份：**5b**（用户 2026-10-30 D-4 裁决分叉 3「先修协议，新批次重测」；
  规格 `task5b_spec.md`，呼前冻结攢批115、加固攢批116、R1 修订攢批117=a01ac53）
- 工程三态：**不成立**（墙 A 到子帽未过；墙 B 干净通过）；聚合件
  `runs/verdict-20261030T134350Z.json`，`d4_stop_required=true`
- H1（协议服从性）：**支持**——25/25 模型轮 parse 成功、**0 协议空轮**
  （服从率 1.00，预注册支持界 ≥0.90；G2 基线 0.64）
- H2：墙 B 强预测**命中**（scorer rc0 / verdict=pass，12/12 检查过）；
  墙 A 弱预测**字面命中**（交付件出生并进评分；全 pass 未预承诺；终判 fail）
- 预登记矩阵落格：**工程「不成立」× H1「支持」→「协议可修但非充分：
  能力墙判定加强，建议维持 opt-in/冻结」**（规格 §2 矩阵第 2 行）
- 不翻案声明：Task 5 G2「不成立」（verdict-20261030T092635Z.json）原样
  在案，本实验是独立新实验，不构成对 G2 的上诉或改判（硬规则 7）。
- 模型：coding_debug 阶段 kimi-k3.1-pro，全实验零模型变更（硬规则 1）。
- 冻结：九件哈希 LOCK（2026-10-30T09:09:48Z 首呼锁，B2 批次）对 5b 两墙
  重入逐位比对通过；判据/评分器/任务文/镜像 digest 一字未改
  （abf96496a6d7…d012fed7）。

## 1. 工程判决（冻结纯函数 engineering_verdict，唯一输入两墙 wall_result）

| 墙 | 工程闸 | 实呼/子帽 | calls_to_rc0 | 50% 线 | cap_exhausted | 升级 | 人工 | 污染 |
|---|---|---|---|---|---|---|---|---|
| wallA | **fail**（三 provenance PASS 后 ImportError） | 16/16 | 无（未 rc0） | n/a | **true** | 0 | 0 | 0 |
| wallB | **pass**（12/12 检查） | 9/9 | 9 | 9≤15 **在内** | false | 0 | 0 | 0 |

纯函数输出（逐字）：

```json
{"verdict": "不成立",
 "reasons": ["工程闸到子帽未过：['wallA']（不抬帽/不换种/不放宽）"]}
```

按 VERDICT_CRITERIA.md §5 短路表：墙 A「到子帽未过」→ 不成立；墙 B
「两墙…」条件因墙 A 失败不满足。本 Agent 不编辑、不解释扩张判据。

两个必须同报的诚实细节：

1. **墙 B loop_kind=round_cap（9/9 轮用尽）但 passed=true**。冻结
   WallResult 定义 `cap_exhausted=(not passed) and n_calls>=subcap`，
   故 cap_exhausted=false；通过发生在最后一轮的产出经评分器独立 rc0。
   calls_to_rc0=9 在 50% 线 15 内。不粉饰：轮次没有余量，是「帽边上垒」。
2. **墙 B 的 b_cap 数值双过仍是空虚满足（vacuous pass）**。M1 壶体本次
   是真仪器（见 §3.2），但 `design_pour_trajectory()` 壶嘴仍写成空间
   恒定点 `np.tile([0,0,1.045],(161,1))`、间隙恒 0.025
   （agent_solution.py:219-220），speed_max=0.0/clearance_min=0.025 的
   pass 与 G2 同源——等价于「不倾倒」。冻结 §6 机械标签
   `b_eng_and_b_cap_pass, d4_stop=false`；科学读法：**B-eng 仪器合格是
   真进步，运动设计能力不能据此宣称「能力墙未复现」**。kinematic_witness
   判违仪本身有效（K1–K6 same_witness 全过），失效的是模型自选的轨迹设计。

## 2. H1 协议服从性（冻结定义，机器复算）

冻结定义：模型轮之后因 AgentParsingError 未执行任何工具即为协议空轮；
EmptyResponseError 与 policy deny 单列不计。

- 机器复算（trace 事件序 + 对 25 份原始模型输出跑冻结分类器
  `classify_format_violation`）：**两墙 25/25 轮分类标签全 None（合法），
  协议空轮 = 0**。其中 23 轮后有 ≥1 个放行工具执行；另 2 轮（墙 A
  seq33、墙 B seq49）仅触发 policy deny、零放行工具——按冻结定义
  policy deny 单列，不计协议空轮。
- trace 全文计数：`Error in code parsing` = 0，`AgentParsingError` = 0，
  `[SimPraxis 协议精确提示]` = 0，`空转` = 0；加固反馈通路零次触发。
- empty_responses=0（两墙），supplier_errors=0，cache_hits=0。

| 实验 | 实呼 | 协议空轮 | parse 成功率 | 判定 |
|---|---|---|---|---|
| G2（基线） | 25 | 9（A 6 + B 3） | 0.64 | — |
| **5b** | 25 | **0** | **1.00** | **支持**（界 ≥0.90；falsify ≥6；灰带 3–5 均远离） |

policy deny 3 起（不计空轮，白名单实战留证）：墙 A seq33 `python -c`
argv 元字符（该轮零放行工具）；墙 B seq49 同类（该轮零放行工具）、
seq56 `apply_patch` 补丁 JSONDecodeError——同一轮 run_cmd 回退**被放行
执行**但自检**未通过**：my_check.py 在 seq54/seq56 两跑均 rc=1，stderr
sha 逐字节相同（bdbfbf72…171B，line 6 断言），补丁未落盘、终态仍断言
(2,0)。工具层回退动作真实发生，模型自检两跑皆红到帽；12/12 PASS 是
loop 后冻结评分器的独立判定，不依赖模型自检转绿（详见 §3.2）。

框架 __condense__ 事件（A 13 / B 6）是上下文压缩，不是模型轮也不是空轮。

## 3. H2 逐墙取证

### 3.1 墙 A（LIBERO 信息墙，弱预测＝出生并进评分）

- rounds=16，steps_used=17，token 111,005（in 107,557 / out 3,448；
  账单侧 cache_read 68,096），trace 46 事件，deny 1（seq33）。
- workdir 终态：`probe_env.py`/`probe_source.py`/`probe_control.py`
  三份探针 + **`agent_solution.py` 出生** + `my_check.py`。
  G2 同墙终态只有一份 204B 探针、交付件从未出生——弱预测字面命中。
- 评分（容器内真跑）：numpy 1.26.4 / mujoco 3.1.6 / robosuite 1.4.0
  三 provenance PASS 后 rc=1：
  `ImportError: cannot import name 'ControlEnv' from 'libero.libero.envs'
  (/opt/gate/pyshim/libero/libero/envs/__init__.py)`，elapsed 1.902s。
- **失败性质（关键）**：正确路径 `libero.libero.envs.env_wrapper.ControlEnv`
  在 seq44 出生的 `probe_control.py` 第 2 行已被模型自己写出、seq45 运行
  成功（打印过源码）；seq46 的终稿却用了错误的
  `from libero.libero.envs import ControlEnv`（与 preflight 冒烟用的正确
  导入相对照）。模型**探明了事实但终稿未应用**；seq47 写完 my_check.py
  即触帽，自检与修复轮不存在。这是集成/工作记忆侧的能力失败，不是协议
  损耗（0 空轮），也不是知识不可达（事实已在自己的探针里）。

逐轮（全有效轮；r=模型轮序号）：

| seq | r | 产出 | 判定 |
|---|---|---|---|
| 32 | 1 | list_dir . | 有效 |
| 33 | 2 | python -c 元字符 | **deny**（占轮，非空轮） |
| 34–35 | 3–4 | probe_env.py 写/跑 | 有效 |
| 36–43 | 5–12 | probe_source.py 四次改写/运行（定位 API） | 有效 |
| 44–45 | 13–14 | probe_control.py（**含正确 env_wrapper 导入**）写/跑 | 有效 |
| 46 | 15 | **agent_solution.py 出生（错误导入路径）** | 有效 |
| 47 | 16 | my_check.py 出生，未及运行即触帽 | 有效 |

### 3.2 墙 B（流体 B-eng，强预测＝rc0/pass）

- rounds=9，steps_used=10，token 80,439（in 74,049 / out 6,390；
  cache_read 42,752），trace 25 事件，deny 2（seq49、seq56）。
- workdir：`env_check.py`、`agent_solution.py`（8,819B）、
  `my_check.py`（4,098B）、模型自产 `agent_vessel.obj`（56,745B）。
- 评分 **12/12 PASS，verdict=pass，rc=0**：

| 检查组 | 5b 实测 | 对照 G2 |
|---|---|---|
| S0 无哨兵字面值 / S1 无硬编码 | PASS（空违规） | PASS |
| M1_vessel 程序化单体中空壶 | **PASS**：898 顶点/1792 面，watertight，euler=2，体积 1.494e-4，convex hull 1.007e-3，vol_ratio 0.1483，distinct_from_history=true | **FAIL**（degenerate profile segment） |
| M2 开口壳 768/2304 | PASS（768 面/2304 边界/0 非流形） | PASS |
| M2 非流形 fan | PASS（boundary 2/nonmanifold 1） | PASS |
| M2 历史 1536 闭壳 | PASS（1536/0/0） | PASS |
| K1–K4 运动学见证 | PASS，same_witness=true（含 K3 too_fast/K4 penetrating 期望违例逐位复现） | PASS |
| K5/K6 抛错类 | PASS（非有限/退化输入正确抛错） | PASS |

强预测预注册原文（规格:72-73）「获得 ≥1 个修复轮，scorer rc0 /
verdict=pass」**命中**：G2 唯一失败点 M1 壶体本次为真 watertight 单体；
seq53（r6）出生自检器，seq54（r7）自检 rc1 见缺陷，此后模型实际获得
r8（seq55 读文件并正确诊断「自检期望写反，应为 (1,0)」）、r9（seq56）
两个修复轮；r9 的 apply_patch 被 policy deny、run_cmd 重跑自检仍 rc1
（与 r7 同一 171B stderr，sha bdbfbf72…），**模型自检到帽未转绿、终修
并未发生**。scorer rc0 / verdict=pass 由 loop 后冻结评分器对终态产物
独立判定（12/12 PASS）：「修复轮」确实获得（r8/r9），「scorer rc0」
指墙 B 冻结评分器而非模型自检 my_check.py——按冻结字面两项均成立。
B-cap 数值轴见 §1 诚实细节 2（真 B-eng + 空虚 b-cap）。

### 3.3 合并形态

协议损耗从 36% 降到 0，两墙交付件均出生；墙 B 仪器全过、墙 A 死在
「自探事实未进终稿 + 零修复余量」。主因轴从**协议层**移到**集成/
工作记忆与轨迹设计层**——与矩阵第 2 行的预登记解释一致。

## 4. 呼量与预算对账（规则 2；R1 修订后帽 53）

| 项 | 值 |
|---|---|
| 5b 实呼 | **25**（P25-B4 16 + P25-B5 9），全部 ok，seq **32–56** 连续单调 |
| task5 累计 | **50/53**（G2 25 + 5b 25）；污染储备 **3 呼原封未动**（5b 非污染重跑，规格 §5 不借调） |
| phase25 ledger 总行 | **56**（Task3 6 + Task5 50）；全局门 221，余 165 |
| preflight_blocked | **0**（两墙 --preflight 前后账行 31→31；另一次入口误用 rc2 在任何付费呼前被 argparse 拒，零账行） |
| 票据 | P25-B4/P25-B5 由 write_approval 呼前自批（0600，task=5/stage=coding_debug/ledger_phase=25，note 引 R1 与三签），落 quota/approvals/ |
| token（B4 墙A） | in 107,557 / out 3,448（账单侧 cache_read 68,096，cache_create 0） |
| token（B5 墙B） | in 74,049 / out 6,390（cache_read 42,752） |
| 时间窗 | A 13:33:34–13:35:55Z（约 141s）；B 13:37:55–13:42:13Z（约 258s）；全部严格 Z |
| call_cache | None（run_wall 固有），cache_hits=0；提示指纹因系统契约改写而自然换新（真新出生） |
| 模型 | kimi-k3.1-pro 全程；呼级 0 失败、0 空响应（reviewer 复核补充：墙 B
开跑瞬间 seq48 呼内一次可重试 InternalServerError，2.2s 退避后成功，
watchdog stderr 留痕；B5 九行 ledger 均 ok=true/err=None，不占额外呼） |

## 5. G-15 真出生三证、安全与看门狗

- **seq 推进**：A 32–47、B 48–56 与 trace `__model_round__` 一一对应，
  两墙 `ledger_rounds_match=true`。
- **新鲜时间戳**：首/末呼 ts 均在 13:33–13:45Z 本次窗口（G2 为 09:09–
  09:25Z），全部 Z；raw blob mtime 同窗。
- **关键词实文档命中**（trace/full/raw 计数，非模型自述；A 检 72 文档 /
  B 检 37）：A＝ControlEnv 19 / libero_spatial 8 / OSC_POSE 11 /
  use_camera_obs 11 / build_env 5；B＝edge_manifold_counts 10 /
  make_vessel_mesh 6 / kinematic_witness 5 / evaluate_precheck 5 /
  watertight 6。全部非零，出生契约到达模型侧。
- **双密钥扫描先于挂载**：仓 324 文件 0 命中（scan_secrets.sh 10 条）；
  冻结根（runs/ 除外）+ gates 共 52 文件用 common.secrets 单一源严口径
  扫描 0 命中。
- **容器/GPU/网络**：模型容器 `--network=none --cap-drop ALL --read-only
  --pids-limit 256 --memory 4g`+tmpfs、CUDA_VISIBLE_DEVICES=""；两墙
  峰值/终值显存均 14 MiB；评分/终局后 label 残留断言两墙皆空，收尾
  `docker ps -a` 本仓容器 0；无 prune、无 daemon 改动；墙 B 宿主主 venv
  评分，冻结 OBJ 不挂入模型容器。
- **看门狗 dogfood**：两跑均 run_watched 七件套 + PDEATHSIG toucher +
  30s meta-guard（--notifier fake:<drill 路径>），静默帽 1200s；
  t5b-walla-20261030T133321Z / t5b-wallb-20261030T133741Z 两 run dir
  exit_code 均 0、guard rc 均 0、零告警。零告警的证据形态（verifier 复核
  更正）：FakeNotifier 零事件时**连文件带目录都不创建**——drills/ 下无
  t5b 目录（仅有 G2 时代 t5wall{a,b}-0909/0922 两件）；零告警实质由
  alerts.jsonl 无 t5b 行 + incidents/ 空 + exit 0 三方独立支撑。
  已知尾债：run meta.json 未记录 notifier_spec，`--notifier fake:` 调用
  本身不能从产物独立复核（LOW-10，门后入库）。双后台同回合回收。
- **冻结纪律**：九件/判据/评分器/任务文/镜像全程只读，LOCK 重入比对通过；
  快照零写入；0600/0700 权限保持。

## 6. 不翻案与处置建议

- G2 verdict-20261030T092635Z「不成立」原样封存；聚合件 prior_g2_verdict
  为只读指针，不参与 engineering_verdict 计算。
- 本实验**只此一个预注册批次**：不二次重跑、不调温/调帽/改提示、不改
  评分器、不换种/换模型（规格 §2 禁止行为，硬规则 7）。
- 按矩阵第 2 行：**协议加固被证明有效但不充分**；建议维持 Track1
  agentic opt-in/冻结，能力侧（长程工作记忆/自探事实整合、运动轨迹
  设计真实性）作为后续命题。D-4 STOP：三签后报用户裁决，Task 6–10
  继续暂缓。
- 尾债（门后，不阻塞判决）：LOW-3/4/5/6 登记于冻结规格附录 A:287-289
  （wall_result 哈希钉、逐轮标签/ActionOutput 透传断言、AST 禁项补
  __import__/getattr、isort）；LOW-7/8/9 与 LOW-R1-1 登记于
  `.omc/notepad.md`（两处悬空「§7」指针、关门钉 sum() 断言、g2_used
  快照导出、interval_t5.json R1 双轨表示注释）。门 2 评审 LOW-A/B/C
  三条文字修正（放行工具轮次口径、seq56 自检未通过、尾债指引位置）
  已按 reviewer 复核证据并入本版正文，不触冻结件、不改任何判决数字。
  verifier 门 2 另挂 LOW-10（watchdog run meta 落 notifier_spec）与
  LOW-11（宿主评分在 gates/wall_{a,b}/ 生成 __pycache__，门后清理＋
  未来 PYTHONDONTWRITEBYTECODE 防再污染），均不阻塞判决。

## 7. 三签

- lead（team-lead）：报告与全部数字自产自核（含冻结分类器 25 输出复算、
  账表复算、b-cap 空虚满足的源码复核）。
- reviewer 门 2（gate1-reviewer，GATE2-5B-REVIEW，2026-10-30）：
  **APPROVE**（HIGH 置信，0 CRITICAL/HIGH/MEDIUM；LOW-A/B/C 三条文字
  修正已按其独立复核证据并入本版，不触冻结件、不改判决与数字）。
- verifier 门 2（gate1-verifier，GATE2-5B-VERIFY，2026-10-30）：
  **APPROVE**（high 置信，0 CRITICAL/0 MAJOR/3 LOW，零付费零模型调用；
  两墙冻结评分器独立重放——墙 B 主 venv 复制件 rc0 12/12 且 score
  payload 与封存逐字节相同、墙 A 容器内 rc1 ImportError 逐字复现——
  25 份 raw 分类全 None、seq/token/G-15/G2 不可变性/九件 sha/票据/
  看门狗/容器显存全部独立重算一致）。LOW：①干净树门待本报告提交后
  复绿；②空 drill 措辞（已按其证据更正，notifier_spec 挂 LOW-10）；
  ③gates 下 __pycache__ 清理（LOW-11）。

## 8. 证据索引

- 聚合：`$F/runs/verdict-20261030T134350Z.json`（$F＝
  /mnt/artifacts/simpraxis/phase25/walls/t5_freeze）
- 墙 A run dir：`$F/runs/wallA/20261030T133330Z/`
  （wall_result.json/score/score.json/work/agent_solution.py/
  traces/t5b-walla-20261030T133330Z/{trace.jsonl,raw/,full/}）
- 墙 B run dir：`$F/runs/wallB/20261030T133751Z/`（同上结构）
- 账：/mnt/artifacts/simpraxis/phase25/quota/{ledger.jsonl,
  approvals/P25-B4.json,P25-B5.json}
- 看门狗：watchdog/runs/t5b-wall{a,b}-20261030T13{3321,3741}Z-*
- 票前：两墙 --preflight 输出在执行会话留痕（task_used=25、票号
  P25-B4/B5、ledger 行数 31→31 零写账、SMOKE_OK ControlEnv /
  trimesh 5.1.1、container_residue=[]、paid_calls=0）；preflight 不产盘文件
