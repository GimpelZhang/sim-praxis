# Task 6 换嵌入实验 Stage A 预注册（mpnet/768 → Qwen3-Embedding-0.6B/1024，只换嵌入）

状态：**v3.4 门 1 终审候选**（2026-10-31；critic v3.4 APPROVE 后冻结前吸收其
唯一 LOW——§3.5(b) 删人工列计数改 schema 机械派生措辞，sha 相应更新，
机判口径无变化）。v2 已经门 1 双 lane 独立评审：
gate1a-critic（科学轴，opus）与 gate1a-verifier（事实工程轴，opus）均在
v2 上给 `VERDICT: APPROVE`（条件＝MUST-FIX 全修后双 lane 对 delta 复签）。
v3 按两 lane MUST-FIX/MAJOR/MINOR/LOW 修订；v3.1 再收 critic 收口段补送的
MAJOR-3/4、MINOR-4/5/6；v3.2 采纳 critic 对 v3 的 BLOCK-1 最强修法——
prewindow 生成器与数据件从「门 2 引擎批」提升为**门 1 冻结集**，数据件已在
mpnet 活库产出并登记 sha（见 §0、§3.5、§6），并把共变 7 与 §5 第 6 步
戳记口径补齐到 solution_chunks；v3.3 收两 lane 对 v3.1 复签的 APPROVE 条件
（critic 冻结条件 2＝verifier MF-B 的 prereg_ok 总闸、MF-A threads 措辞
事实修正、两 LOW、critic MINOR1/2/3；critic 冻结条件 1 的「互锚」不采，
理由见 §0）；v3.4 收 critic 对 v3.2 的 REJECT：MAJOR-A（预冻件缺剔除两
戳记的归约哈希，「其余列全等」机判不可实现）已按其修法闭合——数据件
schema 加 meta_noprov_sha256/source_commit_values_prewindow/
prov_cols_present，header 加 kb_head 冻结值，postwindow 改凭预冻件单输入
机判；共变 7 对 solutions 的失实措辞更正；第 2 步 cmp 只能写 /tmp；
postwindow 硬编码预冻件 sha 常量。critic MINOR 余文四条一次性全收：
确定性抽侧 45 learning 全量＋kb/solutions 排序前 5 各（55 行）四份向量、
ingested_at 窗区间取 0600 attempts 日志机械界、防换底常量禁环境/路径
覆盖。修订溯源见 §0。
本件经双 lane delta 复签 APPROVE 后，本件、分析脚本、prewindow 生成器与
其机产数据件四件一体冻结提交（攒批122），
executor 此后才允许动代码；冻结提交前禁止改 embedding 配置/PREFERENCES、
禁止重建任何活表、禁止改 bundle/测试等任何引擎代码。**预注册文本在维护
窗口内一字不改**（MAJOR-3，见 §6）。

用户授权（2026-10-31 AskUserQuestion 逐字裁决）：「我决策2+3，怎么做按什么顺序
做你来定」——同时授权选项 2（换嵌入实验）与选项 3（新一轮规则形）。lead 决定
顺序：**先 Stage A（只换嵌入，规则冻结 r3），后 Stage B（在 Stage A 残差上设计
r4 新规则形，另行独立预注册）**。理由：换嵌入重排全部距离，r4 规则必须针对新
嵌入下实测失败模式冻结，否则规则效应与嵌入效应混杂；先嵌入可独立机判其因果，
也可能直接消解一部分结构墙，缩小 r4 工作面。模型单选（Qwen3）属用户「怎么做
你来定」授权范围，不再另开 STOP。

本件只管 Stage A。Stage B 不自动启动，待 Stage A 出结论后另写预注册件。

## 0. v2→v3 修订溯源（门 1 双 lane 意见处置）

| 来源 | 编号 | 处置 | 落点 |
|---|---|---|---|
| critic | MF1 残留回收 gold_text 假性满足 | 保留条件 3 钉 `matched_by=="gold_id"` 严口径，分析脚本出 residual_recovery_strict | §7 |
| critic | MF2 新基线常量时序悖论 | 机械生成器随引擎批预冻；判分前独立 lane 机产快照、独占登记提交；判分器永远最后 | §3.5、§5、§8 |
| critic | MF3 Q1Q2Q3「语义命中」过软 | 改同过滤器同 N **有序 id 集 == 冻结集**机判；13 id 门 1 正式冻结 | §6、§7 |
| critic | MF4 点预测无牙 | 分析脚本机判 point_prediction_match + calibration 判词模板 | §7 |
| critic | MF5 共变登记不完备 | 补 pooling/归一化/tokenizer 族/Lance L2 几何；并列「不共变」清单 | §1 |
| 双 lane | MF6 onnx 校验 | sources.hf 钉源、两件字节+sha、拒量化件、禁 rglob 取末；窗口每步 export 离线三开关 | §3.1、§5 |
| critic | MF7 尝试边界不清 | 有约束尝试定义＋出分后黑/白名单写死 | §7 |
| critic | MAJOR 回滚四组合 | 四种 config×表新旧组合拦截点入件；fuser/lsof 机判；备份后 count_rows；中间态成对回滚 | §4 |
| critic | MAJOR 单选理由失实 | bundle 不公开是事实，apache 兼容写前瞻理由不写当前事实；jina 按注册表改措辞 | §2 |
| critic | MINOR2 | test_learning_isolation:184-189 对 /mnt mpnet 旧件交叉核对拆双轨 | §3.4、§8 |
| critic | MINOR3 | 预测表加注「探针可见性门＋席位门」 | §7 |
| verifier | V-MF1 | 补漏红点 test_task0a_q2_backfill.py:522-524（保留断网物证语义） | §3.4 |
| verifier | LOW | migrate_data_layer verify_only 登记历史冻结；embedding_input 模块级函数措辞；§8 互引 milestone0:115；陈旧 docstring 加封存注 | §3.3/§3.2/§8/§3.4 |
| critic 收口段 | MAJOR-3 冻结文本自改通道关闭 | 删「窗口内追加登记 §6 附录」；可前置定值项全部进冻结快照数据件；窗口产物只落 0600 证据件 | §3.5、§5、§6 |
| critic 收口段 | MAJOR-4 分析脚本规格 | 随门 1 双签冻结登记 sha；干跑自检 stdout 留证（H4=H3 模拟已跑，0600 证据件） | §3.5 |
| critic 收口段 | MINOR-4 重嵌确定性抽侧 | §5 第 6 步加约 50 行同批/跨批重算逐位一致抽侧 | §5 |
| critic 收口段 | MINOR-5 sha 口径写死 | sorted(str(id))、"\n".join、utf-8、sha256 写入 §6（四值已按此口径复算一致） | §6 |
| critic 收口段 | MINOR-6 sha 入代码常量 | 双件 sha 为引擎批内常量，禁 SIMPRAXIS_ONNX_SHA256_* 环境位覆盖（min_bytes 只防截断不防掉包） | §3.1 |
| — | **v3.2 BLOCK-1（critic 对 v3 REJECT）** | 采纳最强修法：prewindow 生成器与数据件**提升到门 1 冻结集**（原随门 2 引擎批）。lead 已起草生成器、在 mpnet 活库连跑两次逐字节相同，数据件随攒批122 冻结提交；sha/字节数/13-id 三方对账登记 §6；窗口第 2 步只做「活库重算==预冻件」全等复核 | §3.5、§5、§6 |
| lead 自查 v3.2 | 共变 7 漏登记 solution_chunks | solution_chunks 官方重灌同样刷新 ingested_at/source_commit；补登并把 §5 第 6 步「全部非向量列全等」拆成：phase1/2 全列逐字保留、kb/solutions 仅两戳记允许前进 | §1、§5 |
| lead 自查 v3.2 | 分析脚本↔预注册互锚循环 | 单向锚：分析脚本钉本件 sha（冻结时回填一次成形），本件不内嵌分析脚本 blob sha（提交后 git blob 由门 2 前核验登记） | §3.5、§6 |
| 双 lane v3.1 复签 | critic 冻结条件 2＝verifier MF-B prereg_ok 只报告不判 | 三判决布尔（nonregression_pass/residual_recovery_strict/point_prediction_match）均 `and prereg_ok`；§7 立为门 2 总前提 | §7、脚本 |
| verifier v3.1 复签 | MF-A「threads=1」非现状 | §1 改登记「默认线程 threads=None（四处生产构造均未显式传），确定性靠 §5.6 四份逐位抽侧兜底」；§5 第 6 步同步去 threads=1 | §1、§5 |
| verifier v3.1 复签 | LOW 证据件权限/case 集 | --json-out 照 eval_planner_recall.py:241 os.open 0600；iter4/iter3 case id 集强断言相等 | 脚本 |
| critic v3.1 复签 | MINOR3 身份互换对冲盲区 | 保留命中 H3∩H4 的 matched_by 互换出 retained_matched_by_swaps/goldid_to_goldtext 透明字段（不改判决定义） | §7、脚本 |
| critic v3.1 复签 | MINOR1 postwindow 确定性 | gen_post_rebuild_snapshots 明文同 prewindow 确定性纪律＋双 lane cmp 前置 | §3.5 |
| critic v3.1 复签 | MINOR2 缺席红易误读 | §8 写明 qwen 轨数据件缺席红＝设计态预期，三签单列「预期缺席」，全绿只在第 7 步登记提交后认证 | §8 |
| lead 裁定 v3.3 | critic 冻结条件 1（互钉 blob sha）**不采** | 两文件内容互相内嵌对方 sha256 无不动点（脚本含预注册 sha→脚本 blob 依赖预注册 sha；预注册又含脚本 blob sha），r3 的 49367a5e 是单向 documentary 钉点；本件维持单向锚（脚本机判钉本件 sha），脚本提交后 blob sha 由门 2 前核验登记 attempts/Complete，接受双 lane 复审 | §3.5、§6 |
| critic v3.2 REJECT | **MAJOR-A 归约哈希缺失** | 数据件每行加 meta_noprov_sha256（剔除 ingested_at/source_commit 的 canonical sha）＋prov_cols_present；kb/solutions 加 source_commit_values_prewindow；header 加 kb_head。postwindow 产物④改凭预冻件单输入机判：noprov 全等＋post source_commit 全等 kb_head＋ingested_at 落窗区间；生成器/数据件重产，§6 两 sha/字节更新 | §3.5、§5、§6 |
| critic v3.2 REJECT | 共变 7 对 solutions 失实 | 活库实测：kb 全 aac53313（→c5dd7175 变）、solutions 全 c5dd7175（==冻结 kb_head，**不变**）；两表 ingested_at 都刷新。措辞按实测更正，窗口加 KB HEAD==kb_head 前置 | §1、§5 |
| critic v3.2 REJECT | MINOR：cmp 不得重写跟踪件 | 第 2 步必须 `--out /tmp/...` 再 cmp，禁止重写仓内预冻数据件 | §5 |
| critic v3.2 REJECT 余文 | MINOR 3 postwindow 防换底 | gen_post_rebuild_snapshots 硬编码预冻数据件 sha256 常量（同 onnx MINOR-6 模式），不符即拒跑；**禁环境变量覆盖**该常量；--prewindow 路径参数如保留只许指向字节 sha 相符的件，默认指向仓内冻结件 | §3.5 |
| critic v3.2 REJECT 余文 | MINOR 1 确定性抽侧覆盖 | 四份向量抽侧确定性覆盖**全部 45 行 learning**（phase1 44＋phase2 1；向量源是活库行文本/embedding_input 截断函数，非钉死源文件，不留未覆盖行）＋kb/solutions 各按 str(id) 排序取**前 5 行**（共 55 行，选择规则无自由参数）；每行同批嵌两份＋分两批各嵌一份共四份，exact bitwise 逐位一致，任一不符停窗 | §5 |
| critic v3.2 REJECT 余文 | MINOR 4 ingested_at 机械界 | 窗区间＝**闭区间** [t_backup, t_post]（UTC）：下界＝§5 第 3 步整目录备份完成 ts、上界＝postwindow 生成器运行 ts，两界逐字取 embedswap_attempts.log（0600）记录；生成器不接受 --window-start/--window-end 自由参数（保留则必须与日志 ts 强断言一致）；逐行判定，任一越界或不晚于旧值即 exit≠0 | §3.5、§5 |
| critic v3.4 APPROVE | LOW (b)「九类」计数失实 | 冻结前修字（冻结后一字不改）：删人工计数，改「列集按各表实际 schema 机械派生」＋活库机读列数 kb 9/sol 5，注明 11 名为两表并集说明、content_hash 归 (a)；noprov 机判口径本不受影响；§3.5 prewindow 段「九列」同改 | §3.5 |
| verifier v3.4 APPROVE | LOW① §1「约 50 行」旧措辞 | 因 critic LOW 修字已使 sha 失效，一并冻结前修准为 55 行（45＋5＋5）；verifier LOW②schema 名不升版（消费方仅门 2 postwindow，强制共享 import＋硬编码 sha，风险可接受）、LOW③1811 全套留门 2 代码门、LOW④df 窗口步内重跑——均登记不改 | §1、本行 |

## 1. 变更面（单变量＋全部换型自带共变显式登记）

**唯一干预**：嵌入模型 sentence-transformers/paraphrase-multilingual-mpnet-base-v2
（768 维）→ **Qwen/Qwen3-Embedding-0.6B（1024 维，fastembed sources.hf=
Qdrant/Qwen3-Embedding-0.6B-onnx）**，四表
kb_chunks / solution_chunks / phase1_learnings / phase2_learnings 全部整表
重建（文本源不变，id 与非向量列不变）。

**冻结不变**：r3 合并规则（`_merge_with_scoped_quota`，q=top_k//2、仅 S-only
保底、探针深度恒 8）、synonyms.yaml、查询/hint 构造、golden fixture 47 案与
阈值 .80/.75/.75、判分器 eval_planner_recall.py、mpnet 模型缓存（保留不删）、
Q1/Q2/Q3 与 gold@8 历史报告/结论文件（封存不回改；Track2 冰面物理题 0.824
矛盾结论与嵌入无关——其证据在 /mnt runs 与 KB solution 脚本，零 lancedb
依赖，原样封存，本任务任何脚本不得触其文件）。

**换型自带、无法在「不换模型」前提下隔离的共变（非旋钮，逐项登记）**：

1. **查询/文档对称、不加 Instruct 前缀**：fastembed 登记 Qwen3 查询侧
   「建议」`Instruct: {task}\nQuery:` 前缀、文档无前缀（optional，非强制）。
   本实验显式登记对称偏差：入库与查询共用同一个无前缀 `embed()`，与现行
   mpnet 用法一致，不引入前缀/task 分流这一额外旋钮。
2. **截断预算 512 → 32768 token**：mpnet tokenizer model_max_length=512，
   现行切块（TARGET 1200/HARD 2000 字）超长块尾部当前被截断；Qwen3 32k
   使全块入向量。不人为再截断到 512；Stage A 效应＝「模型＋其原生上下文
   预算」的合并处置，如实命名。
3. **pooling 体制 mean → last-token**：mpnet 走 fastembed pooled_embedding
   （mean pooling）；Qwen3 走 last_token_normalized_embedding（末 token）。
4. **输出归一化 无 → L2**：mpnet pooled 路径不做 L2 归一化；Qwen3 输出
   L2 单位向量。
5. **tokenizer 族 XLM-R SentencePiece → Qwen2.5 BPE**。
6. **距离几何体制**：两侧建表均未指定 metric（vector_engine.py 建表仅
   schema），LanceDB 默认 L2。Qwen3 单位向量下 L2 与余弦单调等价，mpnet
   非单位向量下 L2 还含模长因素——全表排序几何不同，这正是处置本身，
   不在分析中混入「归一化旧向量」之类的再处理。
7. **provenance 戳记前进（仅官方重灌两表；critic MAJOR-A 实测更正）**：
   kb_chunks 与 solution_chunks 行内 `source_commit`/`ingested_at` 盖入库
   时刻；二者不进 id/content_hash。活库实测（2026-10-31，已入预冻件
   source_commit_values_prewindow/header.kb_head）：kb 1542 行全
   aac53313、solutions 94 行全 c5dd7175；KB 仓 HEAD=c5dd7175（clean，
   ==origin/main）。窗口前置冻结 **KB HEAD 必须仍 == c5dd7175**，故重建后：
   kb source_commit **aac53313→c5dd7175（变）**，solutions source_commit
   **c5dd7175→c5dd7175（不变）**；两表 ingested_at 都刷新为窗口时刻
   （solutions 现戳为 2026-10-31，窗内仍前进）。postwindow 对 source_commit
   的机判是**全等 kb_head 冻结值**，不是含糊「允许集后代」。
   phase1_learnings/phase2_learnings 走 §3.2 一次性 reembed 脚本，**全部
   非向量列（含 created_at）逐字搬运**，无任何戳记前进——这是两轨重建的
   显式分界。

**显式「不共变/不做的事」**：不取 matryoshka 短维（全 1024，不裁剪）；
用 fp 主件不用 int8 `-Q` 量化变体（量化件需 onnxruntime≥1.23 且另引入精度
共变）；fastembed 构造维持现状默认线程（threads=None；verifier MF-A
核实四处生产构造均未显式传 threads，本实验不把「threads=1」伪登记为
现状），batch 同构（batch_size 默认 256）——重嵌确定性不靠线程假设，
由 §5 第 6 步 55 行（45 learning 全量＋kb/solutions 排序前 5 各）×四份
向量逐位抽侧机判兜底，任一非确定性即停窗；
不引入查询/文档前缀分流（见共变 1）；不改切块常量、不改任何查询串。

## 2. 模型单选理由（先于任何 gold@8 看数）

fastembed 0.9.0 本地可选的三个多语 1024 候选，在**未测性能前**按非性能理由
收敛到一个，避免逐模型试到过阈（model-shopping）：

| 候选 | 许可 | 前缀/task | 取舍 |
|---|---|---|---|
| Qwen/Qwen3-Embedding-0.6B | apache-2.0 | 可选（本实验不用） | **选用**：多语+代码标识符、32k 上下文、last-token 新代际；许可前瞻理由见下 |
| intfloat/multilingual-e5-large | MIT | fastembed 注册「Prefixes necessary」，强制 query:/passage: 双前缀且 fastembed 不代注 | 排除：强制双前缀是单变量之外的管线改造，且 512 token 截断不变，引入混杂 |
| jinaai/jina-embeddings-v3 | **cc-by-nc-4.0** | 注册表原文「Prefixes not necessary」，但查询须 query_embed task 分流（fastembed 默认 .embed() 全按 passage） | 排除：NC 许可对商业实体内部使用本身有保守合规风险（不以再分发为前提）；task 分流亦是额外管线改造 |

许可论证诚实边界（critic MAJOR 修正）：bundle/资产包现产物落 /mnt、不入库、
不公开分发（bundle_kb_asset.py:2、DEFAULT_OUT=/mnt/...；KB 仓零 bundle
跟踪），故「与公开分发相容」**不是当前事实**；采用 apache-2.0 的真实理由是
①保守消除 jina NC 的内部使用合规不确定性，②**前瞻**：数据层未来若公开分发
零追加许可成本。单选结论不依赖 bundle 当前公开。

## 3. 引擎批范围（门 2 代码门三签后冻结，重建前入 git）

实现者只许在下列文件面作业，不许多拿；本批全部改动 hermetic 测试覆盖，
默认 skip 的 kb_integration 门不裸跑。

### 3.1 embedding_selector.py
- PREFERENCES 置首 Qwen3 条目（mpnet 行保留不删，回滚＝revert）。条目结构
  从现三元组扩为带来源与双件清单的显式结构：模型名、dim=1024、
  sources.hf 仓名 `Qdrant/Qwen3-Embedding-0.6B-onnx`、相对路径两件
  `onnx/model.onnx`（4,031,521B，sha256
  `754d6ba1c7a323d7b3af0d85b8268f8b716b0cd362ce73295e0f71d3dd831639`）与
  `onnx/model.onnx_data`（2,383,106,048B，sha256
  `15d02a315e3b95296700c73d72a436e79facf4e17d919345bed730e98433bcb4`；
  snapshot af95f2c416ffe9379369ad64f9113e865db6112c，2026-10-31 门 1 前
  实测、双 lane 各自复核登记）。
- 双件字节/sha 为**引擎批内代码常量**（MINOR-6）：禁止以
  SIMPRAXIS_ONNX_SHA256_* 等环境位覆盖（min_bytes 只防截断不防掉包，
  Qdrant 转换仓是新外部供应链来源）。
- **缓存目录取 sources.hf 推导**（`models--{hf_org}--{hf_repo}`），禁止
  照抄 `models--{model_name.replace('/','--')}`（mpnet 实际缓存在
  models--Xenova-- 仓、bge 在 models--Qdrant--bge…，照抄必错位）。
- `_verify_onnx_artifact` 重写：解析 snapshot symlink，对登记的**逐件精确
  相对路径**校「存在＋字节数＋sha256」，外部数据件缺失/截断即 RuntimeError；
  删除一切 glob/`rglob` 取末写法；显式拒绝 `onnx/model_quantized.onnx`
  进入 fp 选择的校验与选择（同仓 -Q 变体指向该件，两件并存时旧代码
  sorted()[-1] 会错拿）。
- 选择末尾 sanity 三条中文探针（related-unrelated>0.05）保留，新增
  「输出向量恰 1024 维且全 finite」断言。
- `VectorEngine.query_solutions` 读路径补 dim fail-fast（现唯一无任何 dim
  守卫的生产读路；ingest 侧守卫在构造器）：开表读 vector 列 list_size，
  与 self.dim 不符即 RuntimeError。四种新旧组合拦截矩阵见 §4，hermetic
  测试逐格钉死。

### 3.2 scripts/kb/reembed_learnings.py（新文件，一次性迁移件，0755）
phase1/phase2 两表重算 vector 的唯一授权通路：
- 裸 `lancedb.connect(uri)`，**不实例化** LearningStore/EvolutionStore
  （旧维度表在时两店构造即抛守卫）；删表前先全量
  `search(None).select(<全部非向量列>).limit(1_000_000).to_list()` 读全行
  （lancedb 0.40 /tmp 合成库已验证该 API），落 0600 JSONL 导出件（/mnt，
  phase1/phase2 各一）作删表前保险；
- 直接 fastembed.TextEmbedding 按新 config 批量算向量；phase1 嵌**行内
  text 列全文**（已核实活库 44/44 行 text 即生产 compose_text 的同一字符串，
  failure_symptom 等不独立存列，重放 compose_text 无米下炊、会引入漂移，
  禁止）；phase2 逐字复用 `embedding_input`——它是
  evolution_store.py 的**模块级函数**（`from memory_flywheel.phase2.
  evolution_store import embedding_input`，≤2000 全文否则前 1200 字；
  活库唯一演化志 8513 字，生产向量本就锚定前 1200 字），禁自行截断；
- **书面删表授权**：learning_store.py:26-30 docstring 要求「新表删除需
  单独书面授权」——本预注册件经门 1 双签 + 用户 2+3 裁决即构成该授权；
  脚本 --apply 首行校验四表 id 集快照 sha 与本件 §6 冻结值全等，不符即拒；
  dry-run 默认、--apply 才写；
- 先 drop 再以 create_table(schema, mode="create") 建 1024 维新表，逐行
  tbl.add 带**原始 created_at 与全部元数据列**（绕过 add_learning/
  add_evolution 的时间戳重生成与证据复检——迁移专用，不是生产写入口；
  脚本头部注释写明）；顺序**先 phase1 后 phase2**（EvolutionStore 构造
  先跑 LearningStore 初始化）。run7c/54b27b59 两个历史 created_at 尾债
  原样带过，不在迁移中「修复」。

### 3.3 bundle/历史迁移件
- `scripts/kb/bundle_kb_asset.py` + `scripts/kb/verify_bundle.py`：
  mpnet 模型名/768 字面量（bundle :43,131-135,157-159,171；
  verify :39,80-83,140-141）改为以 bundle manifest 记录值为准（manifest
  驱动），旧六 bundle（3 core+3 extended，2026-10-17）与新 bundle 都可
  对账；新 bundle manifest 记 model/dim/四表行数/逐文件 sha/kb_sha/引擎 SHA。
- `scripts/kb/migrate_data_layer.py:275-276` verify_only 的 768/manifest
  对账：**登记为历史冻结件**，换嵌入后不重跑（该一次性校验器在活表现状
  本就 dormant：manifest 行数 5/null 早已不符，不被常规测试调用）；在其
  文件头与本件登记，不改代码。
- 全仓 grep 第三消费者已扫：除上述与 §3.4 测试外无其他 768/mpnet 活断言。

### 3.4 测试（历史冻结与新基线分离）
只许改下列面；**不得**改判分器/fixture/synonyms/planner_recall 一字。
- tests/milestone1/test_kb_invariants.py：EXPECTED_VECTOR_DIM=768（:26）
  与活库 dim 断言（:53-61）改读 `select_embedding()["dim"]`；1542 行数、
  --expect 104 等内容身份断言保留增强。
- tests/milestone1/test_learning_isolation.py：Q1/Q2/Q3 有序 id 清单
  （:41-53）与两处有序断言（:180-189）拆双轨——mpnet 清单逐字保留为
  **封存常量** _MPNET768 后缀（加「mpnet/768 封存基线，不对活库断言」
  注释），活库断言按当前 config 选轨：mpnet 轨 == 封存常量且与
  /mnt 旧基线 json 交叉核对；qwen 轨 == §3.5 生成器机产的 qwen 有序 id
  数据文件且与同批机产的 qwen baseline json 交叉核对（:184-189 的 /mnt
  mpnet 旧件路径不进 qwen 轨，MINOR2）。dim 断言（:157）config 驱动。
- tests/milestone2/test_task0b_layers.py：:613 dim 改 config；:616-617
  recall_snapshot 等值拆「历史证据链（inventory==mpnet 冻结快照，恒真、
  不再对活库）＋活链（state==qwen 机产快照）」；:617-629 的 44/44 id
  集合断言**保留不动**（id 不涉向量）；:643/:670-671/:691 合成 bundle
  往返 768/mpnet 断言改 manifest 驱动。
- tests/milestone2/test_task5_evolution.py:275、
  tests/milestone2/test_task0a_q2_backfill.py:266：dim config 驱动。
- **tests/milestone2/test_task0a_q2_backfill.py:522-524（verifier 补漏）**：
  现断言 model_name endswith mpnet + onnx_path 存在，其语义是「断网
  0 下载物证」；改造为尾缀取 config 模型名（保留物证语义）＋ onnx/onnx_data
  两件 is_file（qwen 外部数据件也要物证），不得删断言。
- tests/milestone0/test_milestone0_e2e.py:115（§1 共变7 已授权，此处互引）：
  拆双轨——mpnet 封存轨保留 source_commit==KNOWLEDGE_BASELINE_SHA
  （aac53313）逐字断言；qwen 机产数据轨断言活库 source_commit **全等
  预冻 header.kb_head（c5dd7175，从 §3.5 数据件读，禁手写第二事实源）**＋
  内容身份不变（id/content_hash 与冻结快照全等），强度只增不减，commit
  body 留痕「锁内适配（Phase 2.5 换嵌入）」。
- 引擎内 mpnet/768 陈旧 docstring（bundle 两文件头等）随批加封存注。
- reembed_learnings.py 配 hermetic 测试（合成临时 lancedb：768 旧表→
  dry-run/apply→1024 新表，断言非向量列逐行 sha 全等、id 集全等、
  created_at 保留、--apply 对快照 sha 不符 fail-closed）；§3.1 四组合
  dim 守卫各一格 hermetic；不新增 kb_integration 活库测试（活库证据仅
  窗口件+iter4）。

### 3.5 机械快照生成器与判后分析脚本（杜绝事后迁就）
- **两生成器共同的输出确定性纪律**（critic MINOR-2）：JSON 一律
  `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)` + 单一
  换行尾；列表先排序再序列化（id/字典键）；**禁时间戳、禁 wall-clock、
  禁主机名/PID 等环境字段**（产物头只含静态 config 模型名/dim、KB 仓
  HEAD 与四表 id 集 sha，均由活库/仓库决定）；同一活库连续两次运行输出
  必须逐字节全等（hermetic/活库各验一次），否则双 lane diff 无意义、
  门 2 拒收。
- **scripts/kb/gen_prewindow_snapshots.py（新文件，门 1 冻结集，v3.2
  BLOCK-1 提升，v3.4 schema 升级）**：在 **mpnet 活库**上只读运行，机械
  产出固定路径
  scripts/phase25/snapshots/embedswap_prewindow_snapshot.json（仅哈希/行数/
  id，不含原文/向量），内容：header（config 模型名/dim/uri、**kb_head＝
  KB 仓 HEAD 全 sha**、四表 id 集 sha）；四表行数；四表排序 id 集 sha
  （口径见 §6）；kb/solutions 逐行 content_hash 集与
  source_commit_values_prewindow 观测值集；四表逐行
  (id, sha256(text), meta_sha256＝canonical_json(全部非向量列),
  **meta_noprov_sha256＝canonical_json(剔除 ingested_at/source_commit 后
  的非向量列)**，prov_cols_present 标明该表实际存在的戳记列（kb/solutions
  两列，phase1/2 零列故 noprov==meta）；Q1/Q2/Q3 mpnet 有序 13 id（查询串/
  过滤器/N 直接 import migrate_data_layer.SNAPSHOT_QUERIES，与 §6 同源，
  门 2 起该常量冻结）。noprov 归约哈希是 v3.4 MAJOR-A 修复：postwindow
  据此**仅凭预冻件**机判「除两戳记外其余非向量列结构身份全等」（列集按
  各表实际 schema 机械派生，不人工计数），不需引用窗口内
  备份。生成器与数据件**随攒批122 冻结提交入 git**（不等到门 2/窗口前）：
  lead 已在 mpnet 活库离线三开关下连跑两次，输出逐字节相同（sha/字节数/
  13-id/新字段三方对账见 §6）；门 1 复签要求两 lane 各自独立重跑一次
  `--out /tmp/...` 并与预冻件 `cmp` 全等。窗口第 2 步只做「活库重算 ==
  预冻件」全等复核（同样只许写 /tmp），不生成新值、不向本件追加文本
  （MAJOR-3）。
- **scripts/kb/gen_post_rebuild_snapshots.py（新文件，随引擎批预冻）**：
  只读活库、机械产出，无任何人手可编辑值；输出确定性纪律与 prewindow
  生成器完全相同（本条首项：sort_keys/indent=2/单一尾换行、列表排序、
  禁时间戳/wall-clock/主机名/PID，critic MINOR1；双 lane 连跑 cmp 全等
  是第 7 步登记提交的前置）。运行前置断言：config 为
  qwen/1024 且四表 id 集 sha == §6 冻结值。产物：
  ①Q1/Q2/Q3 有序 id（同过滤器同 N：q1 capabilities+project=lw_benchhub
  N=3；q2 troubleshooting N=5；q3 capabilities N=5，查询串逐字取
  migrate_data_layer.py:53-64 SNAPSHOT_QUERIES）写 qwen 快照数据文件
  （测试消费，路径在引擎批固定）；②task0b recall_snapshot 同构 qwen 件；
  ③qwen baseline json（与 /mnt 旧 mpnet 件同 schema，落
  /mnt/artifacts/simpraxis/phase25/task6_recall/ 下，0600）；
  ④**pre/post 机判对账件（v3.4 MAJOR-A：预冻件单输入）**：
  postwindow 生成器**硬编码预冻数据件 sha256 常量**（同 onnx MINOR-6
  模式，不符即拒跑，防换底），**禁环境变量覆盖**该常量；--prewindow
  路径参数如保留只许指向字节 sha 相符的件（不符即拒跑），默认指向仓内
  冻结件；读件后对活库机判：
  (a) 四表行数/排序 id 集 sha/逐行 text_sha256 全等；kb+solutions
  content_hash 集全等；
  (b) 四表逐行 **meta_noprov_sha256 全等**——namespace/kb_relpath/
  project/layer/doc_type/section_path/chunk_index/evidence_refs/
  solution_id/relpath/kind 等结构身份列一字未动；noprov 列集按各表
  **实际 schema 机械派生**（2026-10-31 活库机读：kb_chunks 9 列、
  solution_chunks 5 列，上举 11 名为两表列名的并集说明、不构成第二列集；
  两表 content_hash 另由 (a) 的 content_hash 集全等覆盖，本项判决不依赖
  人工枚举计数——critic v3.4 APPROVE LOW 修字）；
  (c) phase1/phase2：prov_cols_present=[]，故 meta_sha256 必须全等
  （created_at 等全列逐字保留）；
  (d) kb/solutions：post source_commit 逐行全等 header.kb_head
  （=c5dd7175；kb 由 aac53313 变来、solutions 保持不变），且不等于
  prewindow 值的变更方向必须与 source_commit_values_prewindow 实测一致
  （kb 变/solutions 不变，反向即异常）；ingested_at 不验具体时刻，但
  必须逐行落在**闭窗口区间** [t_backup, t_post]（UTC）——下界＝§5 第 3
  步整目录备份完成时间戳、上界＝postwindow 生成器自身运行时间戳，两界
  逐字取 embedswap_attempts.log（0600）记录的 ts；生成器不接受
  --window-start/--window-end 类自由 CLI 参数（如保留参数则必须与日志
  ts 强断言一致，否则 exit≠0），且每行严格晚于 prewindow 旧值，任一
  不符 exit≠0（critic v3.2 余文 MINOR 4）；
  (e) 以上任一不符 exit≠0、停窗。**实现约束**：postwindow 不得自行重写
  canonical/noprov 口径，必须直接 import prewindow 生成器的
  PROV_COLS/_canonical/table_snapshot（或抽共享 helper 由两生成器同引），
  防列集/排序口径漂移；其硬编码预冻件 sha 常量值＝本节登记的
  3784e1c9…。产物每份附活 config 模型名/dim/四表 sha 头。
- **scripts/phase25/r3_embedswap_nonregression.py（lead 起草，门 1 随本件
  双签冻结，范式同 r3_nonregression.py）**：读 iter1..4 报告，输出
  H3⊆H4、gains/flips 逐案、主/严双口径四路数、机判字段
  `residual_recovery_strict`（9 残留案中至少 1 案 matched_by=="gold_id"）、
  `point_prediction_match`（见 §7 预测）＋失配逐案明细、anchors 增钉
  iter3/f1c0e7f4 与本件 sha。**单向锚（v3.2）**：脚本内
  EMBEDSWAP_PREREG_SHA256 常量在本件定稿后回填、随攒批122 一次成形；本件
  不内嵌该脚本 blob sha（互锚无不动点），其提交后 git blob sha 由门 2 前
  核验登记 attempts/Complete。只读 stdlib，0600 报告。干跑自检（以 iter3
  报告模拟 H4=H3：anchors 三绿、nonregression_pass=true、
  residual_recovery_strict=false、点预测按 H4=H3 判 false、主/严观测数
  38/13/14/11 与 35/11/13/11 逐位正确）stdout 已留证：
  /mnt/artifacts/simpraxis/phase25/task6_recall/
  embedswap_analysis_dryrun_H4eqH3_2026-10-31.json（0600）。该 14:46 件系
  pin 回填**前**起草期产物，其 prereg_ok=false 属预期（防误读，
  verifier LOW②）；v3.4 终稿 pin 回填后 lead 重跑 v34pin 干跑
  （prereg_ok=true、anchors/prereg/nonreg 三绿、residual/point 仍 false）
  留 0600 物证，门 1 双 lane 各自独立重跑并对全部判决键 CONTENT_MATCH；
  v34pin 干跑件内容 sha 不内嵌本件（否则与脚本 blob 构成同类互锚，适用
  §0 互锚不采裁定）。

### 3.6 config
embedding_config.json 由窗口内 `SIMPRAXIS_RESELECT=1` 重选重新生成
（git 跟踪，随引擎批之后的快照登记提交或回滚提交收口）；**严禁**在窗口外
提前换 config（任何生产构造即撞维度守卫）。

## 4. 备份/回滚与新旧组合拦截矩阵

- 实体 `$KB_ROOT/data/lancedb` → `/mnt/artifacts/embodied-kb/data/lancedb`
  （0700，实测 25MB，四表 .lance 含 _versions/_transactions + data/manifest.json）。
- 窗口前 `fuser -v <dir>/* 2>&1; lsof +D <dir>` **机判**无持表进程
  （他方 jupyter 不触此库仍须登记），随后整目录 `cp -a` 到
  `/mnt/artifacts/embodied-kb/data/backups/lancedb_pre_embedswap_<UTC>/`
  （继承 0700；backups/ 现不存在，无冲突）；另存 §3.2 两 JSONL 导出。
- 备份后、换 config 前，用旧 config **只读开四表 count_rows** 验证备份
  可读、行数 1542/94/44/1。
- config×表新旧四组合拦截点（hermetic 逐格钉死，§3.4）：
  ①新 config+旧 kb 表：VectorEngine.__init__ 即抛（vector_engine.py:60-70）；
  ②新 config+旧 phase1/phase2 表：两店构造守卫抛
  （learning_store.py:147-156、evolution_store.py:131-136）；
  ③新 config+旧 solution 表：SolutionIngester 构造守卫
  （ingest_solutions.py:142-149），**读路同被 §3.1 新增 query_solutions
  dim fail-fast 拦截**（开表 list_size 768≠self.dim 1024 即抛）；
  ④旧 config+新（任一）表：kb 查路 Lance 查询期 fail-closed，phase 两店
  构造守卫反向抛，**solution 读路由 §3.1 新增 dim fail-fast 拦截**（改造前
  仅查询期 Lance 报错）。
- 中途崩溃于中间态（如 phase1 已 drop、phase2 未动）：config 与四表必须
  **成对回滚**——整目录 cp -a 还原后再 checkout config，禁止「新 config+
  半新半旧表」状态下重跑重建；每次恢复命令/rc/ts 落 attempts 日志。
- 回滚触发：§7 任一保留条件失败、窗口任一对账不过、或判分后依规判负。
  动作：fuser/lsof 确认无进程 → 整目录还原 → git checkout/revert
  embedding_config.json 与引擎批 → 复跑 §6 快照全等为止。备份成功前不删
  （删除走删除守卫）。

## 5. 重建窗口顺序（lead 亲发；attempts 全程留痕 0600）

**每个 python/shell 步骤前逐字 `export HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1`（三脚本自身不置离线开关，
已核源码；fastembed 在 HF_HUB_OFFLINE=1 下 local_files_only，缺件只报错）。
任一步出现下载/网络异常（而非缺件 RuntimeError）即停窗判前置失败。**

同一维护窗口连续完成（config 换与四表重建之间不许插入任何其他进程）：

1. **前置**：引擎/KB 干净树，**KB 仓 `git rev-parse HEAD` 必须全等预冻
   header.kb_head=`c5dd717504665b4a01423bfff3e7ee2d5d800666`**（不符即
   停窗——source_commit 全等断言的前提，MAJOR-A）；`df -h /mnt`（实测余
   461G）；fuser/lsof
   机判无持表进程；门 2 已三签冻结引擎批；**窗前快照冻结件已随攒批122
   在仓**（§3.5 生成器与数据件属门 1 冻结集，门 1 复签时双 lane 已各跑
   一次 cmp 全等；窗口只消费，不生成新值）；**双件离线加载复验**（三开关下
   构造 TextEmbedding(Qwen3)、三条 sanity 探针，输出恰 {1024} 维全 finite、
   related>unrelated+0.05——门 1 前已由下载任务与 verifier lane 各验一次，
   窗口前再验一次）。
2. **窗前快照对账**：对活库（此时仍 mpnet）重跑 prewindow 生成器，
   **必须 `--out /tmp/embedswap_prewindow_<ts>.json`**（禁止重写仓内跟踪
   的预冻数据件，critic MINOR），与已登记的
   embedswap_prewindow_snapshot.json `cmp` **逐字节相同**（防止登记后活库
   被他方改动；不符即停窗，不进入备份/重建）。
3. cp -a 整目录备份＋两 JSONL 导出＋备份只读 count_rows 验证。
4. `SIMPRAXIS_RESELECT=1` 重选落 config（Qwen3/1024，sanity＋双件校验）。
5. 重建（顺序硬约束，每步后行数/向量维度即席对账）：
   ① `python -m memory_flywheel.scripts.ingest_kb --rebuild`（前置 KB git
   干净＋kb_pins 分层钉点；只 drop kb_chunks）；
   ② `python scripts/kb/reembed_learnings.py --apply --table phase1_learnings`（44）；
   ③ 同脚本 `--table phase2_learnings`（1）；
   ④ 裸 drop solution_chunks → **先** `ingest_solutions --dry-run`
   读计划块数（内容未变须仍 94；drop 必须先于含 dry-run 的任何 ingest
   调用，dry-run 同过构造器守卫）→ `python -m
   memory_flywheel.scripts.ingest_solutions --expect-learnings 44
   --expect-kb-rows 1542`（strict 自动卡 1542/44；ingest 自身不 drop，
   按 solution_id 先删后写）。
6. **重建后对账**（fail-closed，机判实现见 §3.5 产物④）：四表行数
   1542/94/44/1；四表排序 id 集 sha == §6；四表逐行 text_sha256 全等；
   kb/solutions content_hash 集全等；**四表逐行 meta_noprov_sha256 全等**
   （预冻件单输入可机判，MAJOR-A）。非向量列按共变 7 **分轨**：
   phase1/phase2 prov_cols_present=[]，meta_sha256 全等（created_at/
   run_id/rung/gate_source/evidence_relpath/namespace/layer/track/
   sim_engine/evolution_round/parent_run_id/pareto_gain_ratio 一个不许动）；
   kb_chunks/solution_chunks 仅 ingested_at/source_commit 两戳记允许变化，
   post source_commit 必须**全等 kb_head c5dd7175**（kb aac→c5dd 变向、
   solutions c5dd→c5dd 不变向，与 source_commit_values_prewindow 实测
   一致），ingested_at 逐行落**闭窗口区间** [t_backup, t_post]（UTC：下界＝
   本流程第 3 步整目录备份完成 ts、上界＝postwindow 生成器运行 ts，两界
   逐字取 embedswap_attempts.log（0600），无 --window-start/--window-end
   自由参数）且严格晚于旧值。白名单仅此两列两表，
   任何其他差异即停窗。新向量全 1024 维 float32、无 null/全零；**重嵌确定性
   抽侧**（MINOR-4＋critic v3.4 MINOR 1）：样本**确定性**选取——phase1 44 行
   ＋phase2 1 行 learning **全量 45 行**（其向量源自活库行文本/embedding_input
   截断函数，非钉死源文件，必须逐行覆盖、不留未覆盖行）＋kb/solutions
   各按 str(id) 排序取**前 5 行**（共 55 行，选择规则零自由参数）；每行文
   同批嵌两份＋分两批各嵌一次，四份向量逐位
   一致（exact bitwise，不设容差；维持 fastembed 默认线程，不设 threads=1；
   抽侧自身即非确定性探针，任一不符即停窗——verifier MF-A）；check_solutions 5
   方案、check_chunk_integrity --expect 104、validate_capability_matrix、
   kb_pins 四断言全绿；KB .gitignore 锚定 /data 不动。
7. **机产快照（判分前，MF2 时序）**：独立 verify lane 运行
   gen_post_rebuild_snapshots.py，reviewer lane 独立重跑逐字节 diff
   一致；产物独占一个**只含机器生成快照数据**的登记提交（无任何测试/判分
   逻辑改动，三签；含 qwen config 若第 4 步产生了 git diff）。随后跑
   kb_integration 显式门（SIMPRAXIS_KB_INTEGRATION=1）与两排除 hermetic
   全套确认绿。
8. **判分器＝窗口最后一个动作**（§7）。判分之后直到收口，黑名单生效。

## 6. 冻结锚点（门 1 双 lane 复算一致）

- kb_chunks=1542，id 集 sha256
  `35d4bf61849c3108348a0aa61bea89a269a2bd748b115dc2105b83de88da1b72`
- solution_chunks=94，id 集 sha256
  `cdab79c7e1de3d6bd821d28e65c8ac60ac412ccac3ba4c94bf5be90a4fd68197`
- phase1_learnings=44，id 集 sha256
  `022341f732776f33b33314db3e34ddfd915c9ed2ab88cfe6f8c0c9e87af43a9d`
- phase2_learnings=1，id 集 sha256
  `82058d2fa23796c4130749ef0a86dce1504aeff3648ddde0caa7f3fbb5731b4e`
- **Q1/Q2/Q3 mpnet 冻结目标（13 id，门 1 正式冻结；与
  tests/milestone1/test_learning_isolation.py:41-53 及
  /mnt/.../task2_direction/recall_baseline_q1q2q3.json 三处逐字一致，
  该 json sha256
  `d67db07c78780ad7fcee5e902db374a45a4624bfab01a084919f33a91724f5e7`，
  8603B，0600）**：
  Q1(lw_benchhub,N=3)=a5ac6af02544fc330202e3a512198d12 /
  69bc86bd5f56599c09754864816a5aa7 / 36d88099b89eb2d7bc95cb8b63ae1b20；
  Q2(troubleshooting,N=5)=398ef19f41b0901b04c3a7f3984e8ece /
  a4a1083844306f192273f41763fa7020 / 5f3dd7341b0553129fcf87746a5a4aac /
  5c9058abbc25c15702dc9249e813aab6 / 667621d5b55057536b0658d9e7801593；
  Q3(capabilities,N=5)=8fd71136657b7b29cf551ddb17224fa8 /
  65b9c51cd155fe49adbd9ac0609b7696 / 3936c514758ed8d5d33f1235533c57a0 /
  33b9cd6b58aa0b00c06bb433b61e2c9a / c8fe70ddd727296a11a80cfb59e90c50。
- 仓锚（iter4 前须仍为这些值）：fixture f5c5cc58…7c3a6；判分器
  e73fe8b8…22d7；synonyms b731ccbe…339c3；r3 合同 919de10d…f75d9；
  r3_nonregression.py 49367a5e…fc520；iter1 05a6c264…a053、
  iter2 5aacab55…5c81、iter3 f1c0e7f4…a307。
- **id 集 sha 口径写死**（MINOR-5）：sorted(str(id)) 列表以
  `"\n".join` 连接、utf-8、sha256（无尾换行、不经 json.dumps——后者产生
  另一组值）；四表四值已经门 1 双 lane 按此口径复算一致。
- **窗前冻结快照（门 1 冻结集，v3.2 BLOCK-1 提升、v3.4 MAJOR-A 升级
  schema；随攒批122 入 git）**：
  - 生成器 scripts/kb/gen_prewindow_snapshots.py（0755）sha256
    `bb1031c4caea601df26cc715189dd41e33c25f92219e41e6f4b8fdecc330201a`；
  - 数据件 scripts/phase25/snapshots/embedswap_prewindow_snapshot.json
    （0644，605065 B，schema simpraxis.phase25.embedswap_prewindow.v1）
    sha256
    `3784e1c9aef6521d7155a670988945bb7257d2e1ab721c896d4615f65648bace`；
  - header.kb_head 冻结值
    `c5dd717504665b4a01423bfff3e7ee2d5d800666`（机取自 KB 仓 HEAD，
    clean==origin/main；重建后两表 source_commit 的全等目标）；
  - source_commit_values_prewindow：kb_chunks=[aac53313…]（1542 行
    全值），solution_chunks=[c5dd7175…]（94 行全值）；prov_cols_present
    kb/solutions=[ingested_at, source_commit]、phase1/2=[]（其
    meta_noprov_sha256==meta_sha256 已机判）；
  - 机产取证（lead，2026-10-31，离线三开关只读活库）：v3.4 schema
    连跑两次 cmp 逐字节相同；四表 per_row=1542/94/44/1，content_hash_count
    kb=1542/solutions=94；Q1/Q2/Q3 有序 13 id 与本节上条、
    tests/milestone1/test_learning_isolation.py:41-53、/mnt Task2
    基线 json（sha d67db07c…）三处逐字一致（机判 MATCH×3）；
    header config=mpnet/768，四表 id 集 sha 与本节四条全等；
  - 门 1 复签动作：两 lane 各自独立重跑生成器（`--out /tmp/...`）与
    预冻件 cmp（须全等），并独立复算上述三方 13-id 对账、kb_head 与
    source_commit 两观测值集。**MAJOR-3 冻结文本纪律**：凡门 1/
    窗前可定值项一律前置冻结；纯窗口产物（重建后对账值、确定性抽侧、
    iter4 报告）只落 0600 证据件由 verify lane 背书，预注册文本在维护
    窗口内**一字不改**。

## 7. 绑定评测、尝试纪律与判决

- **有约束力的尝试**：§5 第 6/7 步对账全过＋仓锚/报告锚核验通过后，
  判分器的**首个** exit 0/3。命令：
  `SIMPRAXIS_KB_INTEGRATION=1`（＋离线三开关）
  `eval_planner_recall.py --iteration 4 --rules-version r3-qwen3a
  --prior-iterations <iter1> <iter2> <iter3>`（判分器 --rules-version
  无白盘，r3-qwen3a 原样入报告；--iteration 接受 4，门 1 已核 argparse）。
  报告 planner_gold_iter4_<ts>.json（0600），日志 embedswap_attempts.log
  （0600，命令全文/rc/ts/四表 id sha/ledger 行数）。
- 首个 exit 0/3 **之前**：重建中途对账失败/崩溃/exit 2/装配失败，整目录
  回滚或修复后重来**不算尝试**，前提是引擎代码、测试、判分器、fixture、
  synonyms、config 模板零改动。
- 首个 exit 0/3 **之后黑名单**（碰任一即本轮判负、STOP，不得以修 bug/重跑
  名义再跑）：任何引擎代码/测试/判分器/fixture/synonyms/planner_recall/
  embedding_config.json/PREFERENCES/分析脚本/本预注册件的改动；任何第二次
  判分器调用（含只读复跑）；任何按结果回填的常量。**白名单**：只读复核与
  证据读取；本件预授权的整目录回滚 mpnet/768（回滚是预注册结局路径）；
  门 2 取证脚本；第 7 步发生在判分首呼**之前**的快照登记提交。
- 判分后跑 r3_embedswap_nonregression.py（锚点含本件 sha），ledger 须
  恒 56（memory_flywheel 全包零 ModelGateway 引用，门 1 已核）。
  **门 2 接受任何结论的总前提**（critic 冻结条件 2／verifier MF-B）：
  分析输出 anchors_ok==true 且 prereg_ok==true；nonregression_pass、
  residual_recovery_strict、point_prediction_match 三个判决布尔均
  `and prereg_ok`（脚本已实现，sha 未回填/不符即 fail-closed，下列条件
  1–5 在此前提下才有效）。另：iter4/iter3 case id 集脚本内强断言相等
  （verifier LOW，defense-in-depth）；保留命中内部 matched_by 身份
  互换（gold_id→gold_text 退化）出 retained_matched_by_swaps 透明
  字段并陈（critic MINOR3，不改任何判决定义）。

**首跑前点预测（冻结；概率性最佳判断，非 r3 式确定性机械推论）**。
两道可达门注记（critic MINOR3）：r3 探针深度恒 8——残留案 gold 先要在
生产 k=8 探针内可见（可见性门），再要进 4 全局开放席或 4 作用域保底席
（席位门）。mpnet 名次（双 lane 独立复算一致）：

| miss 案 | mpnet 关键名次 | 两门判定 | 预测 |
|---|---|---|---|
| kb_check_success | P_G 11，不属 lw_benchhub 作用域（P_S 不可见） | 全局 11→≤4 | 仍失 |
| kb_execmode | P_G 443/作用域 205 | ≤4 | 仍失 |
| kb_16d_layout | P_G 318/作用域 168 | ≤4 | 仍失 |
| kb_pipolicy | P_G 30/作用域 24（S-only@8 16 块不含 gold） | ≤4 | 仍失 |
| sol_win2_servo | 窗内 11（P_G 47） | k8 探针**不可见**，须连跃 | 仍失 |
| sol_win3_clearance | 窗内 9（P_G 70） | k8 不可见 | 仍失 |
| sol_win4_region | 窗内 6（P_G 40），**生产 k8 可见、S-only 第 5 席（距第 4 席仅差过 1 块，d=6.155 vs 5.661）** | 唯一两门都差 1 步的案 | **中** |
| sol_win6_align | 窗内 9（P_G 57） | k8 不可见 | 仍失 |
| l_fluid_wall | phase1 44 行中 37（phase2 唯一表无此 id） | 37→≤8 | 仍失 |

预测：**恰 1 增益 {sol_win4_region}、零翻失**；主口径 39/47=0.8298、
kb 13/17、solutions 15/18=0.8333、learnings 11/12；严口径
36/47=0.7660、kb 11/17、solutions 14/18=0.7778、learnings 11/12。
win4 回收必须是 gold_id 直命中（其 gold_text=resolve_target 为同文件
多块共享词法，兄弟块回退不计）。

**保留 Qwen3（嵌入轴成立）当且仅当全部满足**：
1. iter4 三门主口径全过（.80/.75/.75），主/严双口径同句并陈；
2. H3 ⊆ H4：iter3 的 38 个主口径命中零翻失；
3. `residual_recovery_strict==true`：≥1 个 9 残留案在 iter4 报告
   matched_by=="gold_id" 直命中（gold_text 兄弟块回退不计；9 案全有
   gold_id，机判可行）；
4. **Q1/Q2/Q3 有序 id 集 == §6 冻结的 13 id 集**（同过滤器同 N，机判）；
   四表内容身份对账按 §3.5 产物④(a)–(e) **全过**：行数/排序 id 集 sha/
   逐行 text_sha256 全等、kb+solutions content_hash 集全等、四表逐行
   meta_noprov_sha256 全等、phase1/2 meta_sha256 全列全等、kb/solutions
   post source_commit 全等 kb_head（c5dd7175）且 kb 变/solutions 不变向、
   ingested_at 逐行落机械闭窗区间且晚于旧值；integrity/矩阵/
   check_solutions/bundle verify/
   check_recall_closure（门 1 复跑 rc0，516/555 闭环）全绿；
5. 0 付费（ledger 恒 56）、备份可回滚、两排除全套与 kb_integration 门绿。

**否则整目录回滚 mpnet/768**，嵌入轴对本标注集判负（负结果即结论，不试
e5/jina/第二模型逐猎），Stage B 在 mpnet 上设计；保留则 Stage B 在 Qwen3
上设计。

**点预测判决模板（MF4，写死）**：保留条件 1–5 全真但 point_prediction_match
为 false 时，结论必须以「保留成功但点预测失准（calibration failure）」
同句呈现，逐案列出失配明细，不得只报保留；预测全中亦须显式报
point_prediction_match=true。概率预测与 r3 确定性预测的不对称已经门 1
评审确认成立。

## 8. 测试改造纪律（随 §3.4 引擎批，三签）

- 历史 mpnet 排序快照（test_learning_isolation Q1/Q2/Q3、task0b
  :616-617 迁移快照、milestone0:115 source_commit）逐字保留为封存常量/
  历史证据链，加 mpnet/768 封存注释；新嵌入结果一律来自 §3.5 生成器
  机产数据文件，**不存在人手填值路径**；qwen 数据文件在窗口第 7 步
  （判分前）才生成并随独占登记提交，引擎批冻结时其消费测试已在、文件
  由生成器产出（测试在文件缺席时按 qwen 轨 fail-closed 并提示先跑生成器，
  不得静默 skip）。**缺席红≠失败**（critic MINOR2）：门 2 三签时 qwen 轨
  因数据件未产出而红是设计态预期，须在三签记录中单列为「预期缺席」，与
  真实断言失败区分；qwen 轨全绿只在窗口第 7 步登记提交后认证，此前任何人
  不得把红测洗成 skip/xfail。
- dim 断言一律改读 `select_embedding()["dim"]`，禁止新增 1024 字面量
  第二事实源（§3.1 登记的 onnx 字节/sha 是缓存物证不是向量 dim）。
- 内容身份断言（1542/94/44/1、id 集、44 phase1 id、content_hash）只许
  增强不许弱化。
- 登记提交后：SIMPRAXIS_KB_INTEGRATION=1 跑活库门，两排除 hermetic 全套
  须绿（r3 闭环时基线 1811 passed/7 skipped/47 deselected，新基线数以
  实际为准并如实报告，不得为凑数改断言）。

## 9. 衔接与禁止

- iter1/2/3 报告、fixture、判分器、Track2 物理题证据一字不改；MEDIUM-2
  gold_text 回退修仍留 Task 9（本件保留条件 3 已用严口径绕开其判决面）。
- Stage B（r4 规则形候选：作用域探针深度解耦、LIBERO 代码窗按窗口元数据
  确定性路由等）只在 Stage A 出分后按实测残差另写预注册件。
- 全程 0 方舟调用；模型缓存已完整（门 1 前 DL_EXIT=0、双 lane 离线加载
  验证），窗口断网三开关，任何下载企图只报错。
- 本任务不起容器；收尾核 GPU/他方进程无新增残留，回收全部本会话后台进程。
