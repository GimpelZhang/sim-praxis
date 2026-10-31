# Task 6 召回质量 r3 预注册（作用域保底配额，新预注册轮）

> **v2（2026-10-31T03:09Z）：门 1 review lane REQUEST-CHANGES 五条阻断，
> 修订全部集中在 §5；v1 原文保留不删，与 v1 冲突处以 §5 为准（§5.1 取代
> §1 规则 3 的双属行歧义、§5.3 加细「恰好一次」、§5.4 钉死 H2 与分析
> 脚本、§5.5 封口可改面与测试污染、§5.6 固化时机与 NOTES 消歧）。
> q=4、阈值、fixture、判分器均不动。**
>
> **v2.2（2026-10-31T03:26Z）：双 lane 对 v2.1 APPROVE 后的绑定条件落字——
> §5.1 论证按 review lane 纠正精确化（读法 A 非纯空操作；G∩S 非压制对象
> 的表述按路限定）、§5.7 把门 2「规则轴翻案」五元合取判决公式写成冻结
> 文本、§5.3 活库 id 集哈希入 attempts.log。除这三处文本精确化外无规则
> 变更；q=4、阈值、三锚与 r3_nonregression.py（49367a5e）均不动。**

- 立项时间（UTC）：2026-10-31（iter3 任何实现/运行之前冻结）
- 上游：AC-T6-4 预注册负结果（iter1=iter2，overall 0.7447、solution
  0.6111、learning 0.9167，判不成立，攢批119 9c894eb 三签归档）
- 用户裁决（AskUserQuestion 逐字，2026-10-31）：
  「我决定采用2+3的路线继续，你来决定顺序」——同时授权路线 2（KB 内容
  补救，走硬规则 3 人工门）与路线 3（超冻结规则的新预注册轮）。
- lead 顺序决定：**先 r3 作用域配额（本件，0 付费），后内容补救**。
  依据：iter2 诊断显示作用域探针可召回 12/12 libero 块但纯距离合并下
  零席位；4 个 kb miss 的 gold 内容本就在库。RRF(k=60) 诊断 overall
  仍 0.745 已证明无效，不采用。换嵌入（整表 --rebuild + Q1/Q2/Q3
  重验）不在本预注册范围；r3＋内容后仍不过须重新 STOP 请示。

## 1. r3 规则（精确文本，实现必须逐字对应）

仅修改 `orchestrator/direction/planner_recall.py` 的合并策略；
`synonyms.yaml` **冻结不改**（sha b731ccbe…，防按 case 调映射＝
gold-fishing）。

对 kb 路与 solutions 路各自的合并结果（窗口 k=8）：

1. 项目识别沿用 r2 既有 `matched_projects`（基于未扩展 base hint，
   不级联），规则不变。
2. 全局 lane G（kb＝双 namespace 全局探针；solutions＝无过滤探针）
   与作用域 lanes S（kb＝命中项目 kb_projects 各 project 过滤探针；
   solutions＝命中项目 solution_id 各方案过滤探针）的定义与 r2 相同。
3. **保底配额 q = floor(k/2) = 4**（两路相同，先于数据选定的固定
   常数，不得按 case/route 调整）：
   - 保底席：取 ⋃S 中带作用域标签的行，**按 id 去重**（一行同时落在
     G 与 S 时不重复占席；多项目探针同 id 只留一行），按 (_distance, id)
     升序，至多 q 行（跨命中项目统一按距离择优，不按项目拆配）。
   - 开放席：G 中按 (_distance, id) 升序的行依次填入，跳过已选 id，
     至满 k 行；⋃S 保底候选不足 q 时，缺口全部由 G 距离序填补。
   - 输出 8 行的最终排列顺序：保底段在前（按 (_distance,id)）、
     开放段在后（按 (_distance,id)）；评测只看成员资格，生产注入
     依赖此固定顺序（作用域相关行显式前置，语义自洽）。
4. 退化情形：matched_projects 为空时 ⋃S 为空，输出与 r2 距离合并
   **逐字节相同**（零命中/probe 旧路径 golden 继续成立）。
5. learning 路**不引入配额**（r2 无学习作用域探针；本预注册不新增
   机制维度），仍纯距离合并。
6. 生产同构：`retrieve()` 生产主入口与判分器走同一实现；不得以
   eval 专用分支实现配额。

## 2. 冻结锚点（实现/运行前后必须 sha 一致）

- 标注集 `tests/milestone25/fixtures/planner_golden_queries.json`
  sha256 f5c5cc58890ce5f9aff1c5f0dd6b97bf1486e716060bcd29d612793458b7c3a6
  （47 case、阈值 .80/.75/.75、gold 标签一律不动；MEDIUM-2/LOW-1
  不在本轮修）。
- 判分器 `scripts/phase25/eval_planner_recall.py`
  sha256 e73fe8b8cb3e3220050e98b00d4c070660641300ab5af16a023e1048991922d7
  （MEDIUM-1 流程债即此钉死；iter3 只允许以 `--iteration 3
  --rules-version r3 --prior-iterations <iter1> <iter2>` 运行，不改
  判分器一字）。
- `orchestrator/direction/synonyms.yaml` sha256
  b731ccbe3c9178c9f1a92fc549a76199ce46b06d957ddf2ea92a4435305339c3。
- 活库四表只读：kb_chunks=1542 / phase1_learnings=44 /
  solution_chunks=94 / phase2_learnings=1；不建表、不写块、不重切。
- 模型/嵌入不变：mpnet 本地缓存，断网三开关，0 付费，ledger 恒 56。

## 3. 运行与判读

- 恰好一次 iter3：SIMPRAXIS_KB_INTEGRATION=1 活库运行，报告落
  /mnt/artifacts/simpraxis/phase25/task6_recall/
  planner_gold_iter3_<ts>.json（0600），iterations 链接 [1,2,3]。
- 主口径与 iter1/2 完全相同（gold_text 兄弟块回退仍允许，保证纵向
  可比），判决只看判分器输出的主指标。**严口径另算**（仅 gold_id 命中
  计分、回退不计）：由独立只读分析脚本对 iter3 报告二次计算（不改钉死
  的判分器一字，沿用 iter1/2 时 0.617/0.444 同法复核），该脚本首呼前
  落盘并登记 sha256，结果随证据包呈交，仅供 MEDIUM-2 透明性，不作判决。
- **逐案非回归硬条件**（与聚合阈值并列，任一不满足即判不通过）：
  fixture 中 13 个带 `hard_negative` 字段的案例，iter2 时 9 中 4 失
  （失：kb_check_success、kb_16d_layout、sol_win2_servo、
  l_fluid_wall）。r3 命中集 H₃ 必须满足 **H₂ ⊆ H₃**——iter2 全部
  35 个命中（含 9 个在中的硬负例，尤其 kb_check_success 这类
  「LIBERO 项目词不得把 global 通用块挤出 8 席」的区分性案例）逐案
  不得翻失；配额挤掉任何旧命中，即使聚合指标达阈也判 r3 不通过。
  4 个已失硬负例允许翻中（增益即正确，gold 在场才计中）。
- 判决（阈值不变）：主口径 overall ≥.80 且 solution ≥.75 且
  learning ≥.75，**且**上述逐案非回归成立 → **r3 通过**：召回质量
  门在规则轴上翻案（工程件接受，AC-T6-4 以「r2 不成立、r3 成立」
  如实记录，不回改 iter1/2 报告）；任一不过 → 负结果维持，r3 是
  预注册授权的最后一轮规则修复，不再 r4，STOP 报用户（换嵌入/内容
  重切另行请示）。
- 禁止：重跑 iter3 挑数、按 miss 反调 q/映射/查询、改标注/阈值、
  改判分器、动活库。学习路 11/12 的既有 miss 预期保持。

## 4. 证据件与三签

- planner_recall.py r3 实现（含规则版本常量/注释指本件）、
  r3 合并纯函数 hermetic 测试（保底 4 席、不足回填、无项目＝r2
  逐字节、去重、顺序、生产同构）、iter3 报告、活库四表不变量、
  0 付费账。
- 门 1（实现前）：reviewer/verifier 对本预注册文本 APPROVE 后
  executor 才动代码；门 2（iter3 后）：双 lane 复核数字与冻结锚点。

## 5. v2 修订（门 1 REQUEST-CHANGES 应答，2026-10-31T03:09Z 冻结候选）

### 5.1 规则 3 语义裁决：保底席只给 S-only 行（阻断 1）

以下为**取代 §1 规则 3 的精确可执行文本**（§1 规则 1/2/4/5/6 不变）：

- P_G＝全局探针返回行的按 id 去重并集（kb＝双 namespace 两条探针，
  solutions＝一条全局探针）；P_S＝全部作用域探针返回行的按 id 去重并集。
  **每条探针 top_k 恒为该路 8（kb_top_k/solution_top_k 现状常量），
  不得加深、不得按 case 变**（封「12/12 诱导加深探针」）。
- **S-only 行**＝{r ∈ P_S : id(r) ∉ ids(P_G)}。G∩S 双属行**不进保底段、
  不消耗保底席**，只在开放段按 G 距离序参与竞争。
- 保底段 R＝S-only 行按 (_distance, id) 升序的前 min(q, |S-only|) 行，
  q=floor(k/2)=4 不变。
- 开放段＝P_G 按 (_distance, id) 升序去重、跳过已在 R 中的 id，补足至
  k=8；|S-only|<q 的缺口全部由开放段按序填补。
- 输出＝R 在前（距离序）＋开放段在后（距离序）。
- 裁决理由（机制先于结果，非阈值向；经门 1 review lane 精确化）：本干预
  在 v1 文首登记的因果靶是「作用域探针可达但纯距离合并下零席位」——
  零席位行按定义即 S-only 行。读法 A 并非机械空操作：保底 q 席中若有
  d 席被双属行占用，靶人群只得 q−d 席（门 1 模拟实测 d=2，A 仍放进 2 个
  S-only 行＝增益 2）；读法 B 则把 q 个保底席足额给靶人群。作为「带标签
  探针代表额」A 可自洽，但与登记的「零席位保底」靶不符，故取 B。
  「G∩S 行并非被压制对象」一句的严格适用域是 **solutions 路**（P_G 为
  单条全局探针 top8，其成员即 r2 全部输出）；kb 路 P_G 达 16 行（双
  namespace），个别双属行本身可能不在 r2 前 8——但两种读法下 kb 路均
  零增益，经验上 moot，门 2 叙述必须按路限定该句。此论证不依赖门 1
  模拟数字（d 值仅为精确化举例，非选择依据）。
- learning 路不变；matched_projects 为空时 P_S 为空、R 为空，输出与 r2
  距离合并**逐字节相同**（hermetic 强制用例钉死）。生产仍只走 retrieve()
  同一实现，禁止 eval 分支。

### 5.2 门 1 对抗模拟披露与逐案预测登记（阻断 1 的透明化）

门 1 review lane 在语义定稿前独立复算了 r2（29 个 kb+solutions 案例对
冻结 iter2 报告零分歧；q=1 严口径复算 0.6170/0.4444 与历史严口径精确
一致，模拟可信）并给出全 q 曲线（读法 A：q=1..3 零增益、q=4 增益 2、
q=5 增益 3、q=6/7 增益 4；读法 B：q=4 增益 3）。lead 在看到该曲线后
仍按 §5.1 的机制理由选定读法 B，并把其逐案结果**登记为首跑前预测**，
活库 iter3 对预测做确认而非用于调参：

- **预测命中集 H3 = H2 ∪ {sol_win1_env, sol_win5_finger, sol_win10_exit}
  （恰 3 增益，无第 4 个）；H2⊆H3，零翻失**。
- 预测指标：overall 38/47=0.8085、kb 13/17（零增益）、solutions
  14/18=0.7778、learnings 11/12=0.9167。
- 登记的结构性负预测：kb 路在任意 q 下零增益（4 个 kb miss 均不可由
  配额翻中，如 kb_check_success 的 gold 是 global 通用块而 libero 只声明
  kb_projects=[lw_benchhub]）；13 个硬负例中 4 个已失案全部仍失。
- 边界坦白：overall 通过裕度仅 1 案（38 vs 37）。
- 实测与预测逐案不符（多/少任何一个增益或翻失）＝prediction_match=false：
  即使聚合阈值碰巧过线也不得宣称规则轴翻案，按异常处置（查数据漂移、
  禁止调代码凑预测）并在门 2 如实上报。

### 5.3 尝试次数与失败分类（阻断 2）

- **有约束力的 iter3 报告**＝在同一 shell 内先核验全部锚点（仓内三件
  sha256 + iter1/iter2 两报告 sha256）之后，**首个 exit 0/3** 的报告；
  由 lead 亲自发命令，executor 不接触评测调用。
- exit 2（前置失败，判分器不写报告）或出分前崩溃**不算一次尝试**，
  在 planner_recall.py 与测试文件**零改动**前提下可原样重跑；每次尝试
  （命令全文、rc、ts、报告路径、锚点核验输出、§5.6 NOTES-6 四表 id 集
  前后哈希快照）追加登记
  /mnt/artifacts/simpraxis/phase25/task6_recall/iter3_attempts.log（0600）。
- **一旦产生 exit 0/3 报告，再改 planner_recall.py 或测试一字，本轮
  立即作废判负**，STOP 报用户，不得以「修 bug/重跑」任何名义再跑；
  exit 3 负结果即最终结论，不 r4。

### 5.4 H2 来源钉哈希 + 统一分析脚本（阻断 3）

- §2 增钉两份历史报告 sha256（2026-10-31 lead 已独立复核一致）：
  iter1 05a6c2647bece5243616fdb473596c11fcb276424f4259d29f4cbd4d8ca2a053；
  iter2 5aacab55e6fe3ebd6a520eed0b6283f1c5974cba88967682b542a4da841f5c81。
- `--prior-iterations` 只允许这两个路径；判分前由 lead 在同一 shell
  校哈希（钉死判分器无法代校，属操作纪律，命令留痕 attempts.log）。
- H2⊆H3（含 H1⊆H3）与严口径 gold_id-only 复算由**同一个 lead 自有
  只读脚本**计算：scripts/phase25/r3_nonregression.py，sha256
  49367a5eeb47b34710bc775bde30bb2caccc0a688b407dd4172097e8939fc520
  （纯标准库、0755；已用 iter2 干跑自检：严口径 29/47=0.617、
  solutions 8/18=0.4444 与历史一致，predicted_metrics_per_route
  恰为 overall=False/kb=True/solutions=False/learnings=True），与本件
  同时冻结，门 2 只认其 stdout JSON 的
  anchors_ok/nonregression_pass/prediction_match/strict。
- 门 1 两 lane 复审时须校该脚本 sha 并干跑确认。
- 修订留痕：该脚本首版 sha b86e12b2…被 verify lane 干跑抓出路由键
  缺陷（内部名 solutions/learnings 未映射报告键 solution_route/
  learning_route，致 prediction_match 恒假）与 775 模式；lead 修后
  即当前 sha，模式 0755。修复仅触及报告键映射，无任何判定语义变更。

### 5.5 r3 可改面与测试污染封口（阻断 4）

r3 新预注册轮的**全部可改面**（fixture 的 changeable_files 是 r2 旧授权，
本件作为用户「2+3」裁决授权的 append-only 修订另列）：

1. `orchestrator/direction/planner_recall.py`：仅合并策略与
   _kb_route/_solution_route 对新合并的调用；探针签名/深度/查询串、
   _learning_route、retrieve() 入口语义一律不动；加规则版本常量/注释
   指向本件。
2. `tests/milestone25/test_task6_r3_quota.py`（**新文件**，唯一测试
   改动面；既有 test_task6_planner_recall.py 字节冻结）。
3. lead 自有 scripts/phase25/r3_nonregression.py（分析件，非实现）与
   本预注册件本身。

封口规则：

- 新测试**只用合成 id/文本/查询**；**不得 import 或读取 golden fixture、
  iter1/iter2 报告、/tmp/attest 件**；门 2 对新测试与 fixture 的
  gold_id/gold_text/query 做双向字面量子串 grep，任何命中即违规。
- 强制用例：①G∩S 双属行在 S 序前 q 也**不占保底席**（q 席全是 S-only
  行；双属行仅当 G 序带入时出现）；②S-only 不足 q 时开放段按序回填；
  ③projects 为空时输出与现行 _merge_lanes 在同合成输入上逐行相等；
  ④按 id 去重（含多 S 探针同 id）；⑤保底段/开放段顺序；⑥生产同构
  （retrieve() 与 retrieve_with_hints 同结果，无 eval 分支）。
- **不新增 kb_integration 测试**；活库证据仅 iter3 报告 +
  r3_nonregression.py 输出 + 活库四表行数只读核验（1542/44/94/1）。

### 5.6 固化时机（阻断 5）与 NOTES 消歧

**阻断 5 修法**：本件 v2（含 §5 全部）与 scripts/phase25/r3_nonregression.py
经门 1 双 lane 对 delta 复签 APPROVE 后、executor 动任何代码之前，由 lead
把两件作为一个独立「r3 预注册冻结」提交入 git（不含任何实现/测试代码）；
门 2 锚点清单增列：本件提交后 sha256、r3_nonregression.py sha
49367a5e…（若复签要求改分析脚本则重新登记并复签）。iter3 首条运行命令
晚于该提交。

**lead 的冻结前诊断 attest**（应 NOTES Q1）：q=floor(k/2) 选择之前，
lead 实际做过的诊断只有两项——RRF(k=60) 全标签诊断（overall 0.745，
无效弃用）与作用域探针聚合召回确认（libero 12/12 可达）；lead 未做过
任何 q 值扫描、未按 q 模拟过判决。全 q 曲线（读法 A/B）是门 1 review
lane 在裁决中独立产出的，发生在 q=4 文本冻结之后。lead 据此**不改 q**
（q=4 恒定，评审居间不反向调参），仅对 v1 文本的双属行歧义做语义裁决
（§5.1，机制理由，非指标理由），并把逐案预测登记为首跑前确认（§5.2）。

**NOTES 消歧（并入裁决文本）**：

1. tie-break 逐字钉死为 `(_distance, str(id))`，id 按字符串比较，与
   r2 现行 `planner_recall.py:307` 一致；保底段、开放段内排序同此。
2. G∩S 双属行出现在开放段时保留其**全局 lane 注解形态**（无
   scoped_project/scoped_solution_id 键，与 r2 逐字节一致）；S-only
   行带来源作用域键（现网标签只写不读，此条仅钉实现形态）。
3. 候选总数不足 8 时输出实际并集行数（保底+开放去重后全部），不填充、
   不报错；hermetic 用例钉死。
4. q 不是独立常数：合并函数签名取 `q = top_k // 2`（floor(k/2)）；
   q=4 的一切预测仅在生产/判分器钉死的 top_k=8 下成立。构造器允许
   k≠8 的既有能力不变，其 q 随之 floor(k/2)。
5. 严口径双数并陈为强制：门 2 证据/STOP 叙述必须同时给主口径与严口径
   （r3_nonregression.py 的 strict 段），禁止只引主口径。
6. 门 2 活库不变量除行数（1542/44/94/1）外，加校 kb_chunks 与
   solution_chunks 的 id 集哈希（iter3 前后各导一次排序 id 列表 sha256，
   必须相等），防等数重切；phase1/phase2_learnings 同样导 id 集。
7. 背景措辞更正：7 个 solution miss **全部**是 libero 代码窗
   （win1/2/3/4/5/6/10），无「6/7」说法（v1 正文未含该误述，此条仅
   备案）。

### 5.7 门 2「规则轴翻案」五元合取判决公式（review lane 绑定条件 2）

门 2 判 r3 在规则轴上翻案，当且仅当以下五项**同时**为真（五元合取，
逐项机判、逐项留证，缺一即不成立）：

1. `all_pass`：钉死判分器 iter3 报告 metrics.all_pass=true（主口径三门
   overall ≥.80、solution ≥.75、learning ≥.75；预期 38/47、14/18、11/12）。
2. `anchors_ok`：r3_nonregression.py 校 iter1/iter2 两报告 sha 通过；
   仓内三锚与本件/脚本 sha 门 2 复核一致。
3. `nonregression_pass`：H2⊆H3 且 H1⊆H3（零翻失）。
4. `prediction_match`：H3 恰为 H2∪{sol_win1_env, sol_win5_finger,
   sol_win10_exit}，且四路指标点预测逐位相符。
5. `live_invariants`：四表行数 1542/44/94/1 不变且**四表排序 id 集
   sha256 iter3 前后相等**（NOTES-6，防等数重切）。

判决规则：

- 五项全真 → 规则轴翻案成立，AC-T6-4 记为「r2 不成立、r3 成立」，随后
  按用户「2+3」继续路线 2（KB 内容补救，硬规则 3 人工门，独立 iter4）。
- 第 4 项 false 为**强制 STOP**：即使第 1 项碰巧为真也不得判通过，
  查数据漂移/实现偏差，禁止改代码凑预测，报用户。
- 第 1/3/5 任一 false → 负结果维持，r3 是最后一轮规则修复（不 r4），
  STOP 报用户；路线 2 内容补救仍按计划排队。
- 所有对外叙述（证据包/STOP/看板）主口径与严口径双数并陈（§5.6 NOTES-5），
  「G∩S 非压制对象」表述限定 solutions 路（§5.1）。

冻结提交（§5.6）只含本件与 r3_nonregression.py 两个路径，逐字节为双签
版本；任何 __pycache__/*.pyc 不得入提交（显式 git add 两路径，不 git add -A）。
