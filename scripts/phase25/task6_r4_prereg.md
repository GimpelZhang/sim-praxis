# Task 6 Stage B 预注册：r4 词法混合召回（r3 稠密并集 ∪ BM25 词法通道，RRF 融合）

- 状态：**门 1 待评审冻结件**（本件 sha 一经登记不得改动；登记 commit 见 §11）
- 立项依据：用户 2026-11-02 会话内逐字裁决「Task 6 Stage B 确认立项，Task7/8/9
  不再继续进行」（同批裁决含硬规则 1 三阶段换供，见 攢批129 `8c7c754`）
- 前序：r3 保底配额（`task6_r3_prereg.md`，38/47=0.8085 三门过，AC-T6-4
  「r2 不成立 r3 成立」）；Stage A 换嵌入（Qwen3-0.6B/1024）**判负回滚**
  mpnet/768（`task6_embedding_swap_prereg.md` §7 五条件 2/4 FAIL，
  攢批122–128 未推送）
- 对应计划：`~/文档/Phase_2.5_Plan_detailed.md` §4 Task 6（AC-T6-1/2/4），
  执行记录 `~/文档/Phase_2.5_Plan_Complete.md` §9
- 性质：**新实验**，不是 AC-T6-4「只允许预注册改写规则一次」的延续
  （那一次已由 r3 用尽且达标）；Stage B 是用户单独立项的新预注册轮

## 0. 结论边界（先写死，防止事后解释）

本实验只回答一个问题：**在冻结的 47 案标注集、冻结判分器/阈值、冻结
mpnet/768 嵌入、零付费、活四表零写入的前提下，把 r3 的「稠密作用域保底
配额」规则形换成「r3 稠密并集 ∪ 精确术语词法通道，RRF 融合」这一**新规则形**，
能否在**零翻失**（H3 ⊆ H4）下回收 r3 的 9 个残留 miss。**

不回答、也不授权：换嵌入（Stage A 已判负）、改 fixture/阈值/gold 标注、
改同义词典、调 BM25/RRF 参数、加深词法通道深度、加第二词法通道、
改 r3 稠密检索本身、Task 7/8/9 任何内容。

## 1. 机制假设（先于看数）

r3 的 9 个残留 miss 不是「知识不在库」，而是**稠密通道对精确术语不敏感**
（mpnet 768 是语义压缩，`ExecuteMode` / `pick_offset_angle` 这类库内独有
拼写在其向量里几乎不占分量）。用**未扩展 base hint** 的术语集与各路词法
语料求文档频次（DF，本表为机产实测，语料规模 kb=1364 / sol=94 / lrn=45）：

| 残留案 | 路 | query 命中的低 DF 术语（term:DF） |
|---|---|---|
| kb_execmode | kb | `_check_success`:2、`shaping`:2、`ExecuteMode`:5、`TRAIN`:5 |
| kb_pipolicy | kb | `Chunking`:2、`get_payload`:2、`deque`:4、`PiPolicy`:5 |
| sol_win2_servo | sol | `servo`:4 |
| sol_win3_clearance | sol | `pick_offset_angle`:1 |
| sol_win4_region | sol | `ramekin`:1、`bddl_file_name`:2 |
| sol_win6_align | sol | `set_init_state`:1、`align_yaw`:2、`attempt_grasp`:2 |
| kb_check_success | kb | `check_success`:2（**但该标识符只出现在兄弟块，不在 gold 行**） |
| kb_16d_layout | kb | **无 ASCII 命中**：判别信号全在中文长片段（11 个 4-gram 命中） |
| l_fluid_wall | lrn | **零重叠**：query 术语在 45 行 learnings 里 0 命中 |

即 9 案里 7 案的 query 携带库内 DF ≤5 的精确 ASCII 术语（上表前 7 行），
而 r3 只从稠密 top-8 里取，这些行在稠密序上的名次远在探针深度之外。

因此假设 H-R4：**在同表同嵌入之上加一条确定性的精确术语词法通道**
（BM25，全表倒排，不受 k=8 稠密探针深度限制），与 r3 稠密 top-8 做
rank-based 融合，可把上述精确标识符案直接从全表捞回 top-8，且因为融合是
秩融合（不比较跨尺度分数），稠密侧已命中的 38 案不会被词法噪声挤掉。

同时先承认两条**结构性不可达**：`kb_16d_layout` 需靠中文 4-gram 命中
（可达但机理不同于标识符）、`l_fluid_wall` 的 gold 行是
`human_failure_agent_run` 叙述行，术语零重叠，本条通道在本轮**预期无效**——
这一点已写进 §6.2 点预测，不作为判负理由，也不允许事后改术语集去够它。

可反驳点（写在判决里）：若词法通道把稠密命中挤出 top-8（任何翻失），
或残留回收 <6/9，则 H-R4 不成立，按 §9 回滚 r3。

## 2. 变更面（单变量）

| 维度 | 变更 |
|---|---|
| 召回合并规则 | r3「稠密作用域保底配额」→ **r4「r3 稠密并集 ∪ BM25 词法通道，RRF 融合」**（唯一变量） |
| 嵌入 | 不变：`sentence-transformers/paraphrase-multilingual-mpnet-base-v2` / 768 |
| 数据层 | 不变：四表 1542/94/44/1，dim 768，零写入（§5 锚点） |
| fixture / 判分器 / synonyms | 不变，sha 钉死（§5） |
| 稠密侧 | **逐字沿用 r3**：`_merge_with_scoped_quota`、探针深度恒 8、查询串不变 |
| 付费 | 0（ledger 恒 56） |

## 3. 规则规格（r4，逐字可实现；生产实现必须与本规格与 §6 参考模拟逐位一致）

### 3.1 术语抽取（`tokenize_terms(text) -> sorted list[str]`）

1. ASCII 术语：正则 `[A-Za-z_][A-Za-z0-9_]{3,}` 的全部匹配，**大小写敏感**，
   长度 ≥4（3 字符以下的 `env`/`obs`/`act` 之类缩写全是高频噪声）。
2. CJK 术语：正则 `[一-鿿]+` 的每一段连续中文字符，取**长度 4 的
   滑窗 4-gram**（含重叠；段长 <4 不产术语）。
3. 去重后**按字典序排序**返回（保证浮点求和顺序确定、跨进程可复现）。

### 3.2 BM25 词法通道

对某一路的语料表 C（§3.5）：

- `N = len(C)`；`dl_i = len(text_i)`（**字符数**，含空白/标点）；
  `avgdl = Σdl_i / N`；
- `df(t) = |{i : t ∈ terms(text_i)}|`（含 0 即不入倒排）；
- `idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5))`
  （N 为语料行数，df 为含该术语的行数）；
- 行得分（Robertson/Walker 标准 BM25）：
  `score_i = Σ_{t ∈ terms(query)} idf(t) · (tf_{i,t}·(k1+1)) / (tf_{i,t} + k1·(1 - b + b·dl_i/avgdl))`，
  其中 `tf_{i,t} = text_i.count(t)`（**非重叠子串出现次数**；Python `str.count`
  不重叠，门 1 实测与重叠计数的数值差为 0，仅措辞更正，不改公式），
  `k1 = 1.2`、`b = 0.75`（标准默认值，不作任何调参）；
- 词法排序：`score` 降序、`str(id)` 升序（与 r2/r3 公共排序段的 tie-break
  约定一致），取前 `top_k = 8`；`score == 0` 的行不入列表。

### 3.3 RRF 融合（最终 top-8）

- 稠密侧 D = r3 生产 `retrieve()` 该路结果的前 8 行 id（保持 r3 语义与注解）；
- 词法侧 L = §3.2 的 top-8 `(score, id)`；
- `fused[id] = Σ_{每个通道} 1 / (60 + rank)`，rank 从 1 起；**两通道等权**；
- 最终排序：`fused` 降序、`str(id)` 升序，取前 8 行；
- 行对象选取：id 若在 D 中出现，沿用 D 的行对象（保留 `_distance`/
  `scoped_project`/`scoped_solution_id` 等稠密注解）；否则用词法通道扫描到的
  原始行（去掉 `vector` 列），并补 `source_route`/`relpath`/`lexical_rank`
  三键；
- **尾席保持（修订 A1，门 1 前由 LEAD 登记，2026-11-02）**：融合只定义最终
  top-8。`_learning_route` **不截断**（主库 top-8 + 额外库 top-8 去重后可达
  9 行：活库当前恒为 phase1 8 行 + phase2 演化志 1 行），而 setter 的
  `_planner_sections` 也不做切片——即 r3 生产会把第 9 行一并注入。因此
  `dense_rows` 长度超过 `top_k` 时，超出部分（第 9 行起）按 r3 原序**追加**
  在融合段之后，并跳过已进融合段的 id（防重复）。理由：若把整路输出截成 8，
  r4 会在 11/12 个 learning 案上静默丢弃 phase2 演化志行——那是未声明的
  第二变量（内容注入面变化），会污染「只换合并规则」的单变量归因。已实测
  另一备选（用完整 9 行做融合）会在 2/12 个 learning 案改变冻结 top-8
  （`l_rungs`/`l_fluid_reject`），故不采纳。尾席不进 §6 点预测（点预测按
  top-8 冻结），不影响 §8 判据。
- **退化性质（必须测试）**：L 为空时，融合结果与 D **逐 dict 相等**
  （稠密分数 1/(60+rank) 严格单调，排序不变）。

### 3.4 词法查询串

- 词法通道一律用**未扩展的 base hint**（`legacy_direction_hint(track, payload)`：
  track2 = `payload["question"]`；track1 = `"{name} 架构 物理引擎 特性 闭环仿真"`），
  **不是**稠密侧那份带同义扩展与 policy_closed_loop 主题句的 hint。
- 理由（机制而非结果）：同义扩展（`LIBERO`/`robosuite`/`MuJoCo`/`BDDL`…）
  是为稠密语义召回服务的泛化项，进入精确术语通道会把低频标识符的权重
  稀释掉——`pick_offset_angle`（DF=1/94）会在扩展串的高频项目词竞争下
  掉出词法 top-8。精确术语通道必须锚在用户原始措辞上。
- 接口：`retrieve_with_hints(..., lexical_hints=None)`，缺省 `= hints`
  （向后兼容）；`retrieve()` 显式传 base hint 三路同串。

> **修订 A4（2026-11-02 门 1 登记）**：上面「缺省 `= hints`」与本节首条
> 「词法通道一律用未扩展 base hint」自相矛盾——直接调 `retrieve_with_hints`
> 会静默拿扩展串当词法查询。改为 **r4 下 `lexical_hints` 必填**，缺省即
> `ValueError`（fail-closed，不猜不降级）；r3 分支该参数仍可选且被忽略。
> 详见 §13。

### 3.5 语料（与稠密侧同表、只读）

| 路 | 语料 | 行数（冻结） |
|---|---|---|
| kb | `kb_chunks` 全表，**排除** `doc_type ∈ {root_index, index}` | 1364 / 1542 |
| solutions | `solution_chunks` 全表 | 94 |
| learnings | `phase1_learnings` ∪ `phase2_learnings` | 44 + 1 = 45 |

- 词法通道**不分 namespace、不分 project、不分 solution_id**：它是跨作用域
  的救援通道（与 AC-T6-2「项目目录边界锁死」的治本方向一致）；
- 读取一律 `table.search(None).select([...]).limit(1e6)` 只读扫描，
  `vector` 列不入内存；行按 `str(id)` 排序后建索引。

> **修订 A3（2026-11-02 门 1 登记）**：`select([...])` 的列清单不是
> 「id+text 够用」，必须覆盖 setter 渲染器读取的**全部来源字段**（kb 要
> `kb_relpath`/`section_path`/`doc_type`；solutions 要 `relpath`/`kind`/
> `chunk_index`/`solution_id`；learnings 要 `evidence_relpath`/`run_id`/
> `gate_source`/`rung`，外加按库注入的 `learning_table` 标签）。理由：被词法
> 通道提进 top-8 的行是从语料原样复制的，缺列就在 prompt 里留 `?` 占位
> （伪造来源）。该修订**只加列**，不改文本、不改排序，因此点预测逐案
> `top8_ids` 不变。详见 §13。

### 3.6 索引缓存与失效

- 每个 `PlannerRecall` 实例惰性构建一次三路词法索引（约 1500 行、2MB 级），
  以「该路语料行数」为失效键：每次查询前比对 `count_rows()`，行数变化即重建
  （生产期 phase1_learnings 会继续增长，缓存必须自失效）；
- 建索引只读，任何写/建表路径都不经过本模块。

### 3.7 规则版本与开关

- `R4_RULES_VERSION = "r4"`，`SUPPORTED_RULES_VERSIONS = ("r3", "r4")`；
- `PlannerRecall(..., rules_version="r4")` 为**构造函数默认值**（生产
  `DirectionSetter` 经 DI 注入的实例即走默认 → 生产默认 r4）；
  **修订 A7（2026-11-02 门 1 登记）：这句括注目前不成立**——`orchestrator/
  graph/cli.py:326` 构造 `DirectionSetter(gateway, vector_engine,
  recall=recall)` 时**从不传** `planner_recall`，全仓也没有第二个注入点，
  所以 `PlannerRecall` 今天**没有生产消费者**。本件所说的「生产默认 r4」只
  指构造函数默认值（判分器/参考模拟直接构造时生效）；把 planner 接进
  setter/cli 是另一件事，不在本件授权范围，接了必须另行登记。详见 §13。
- `rules_version="r3"` 必须逐字复现 r3 行为（`_merge_with_scoped_quota`，
  不加词法通道），用于历史锚点/回归测试；非法版本 fail-closed `ValueError`。

## 4. 引擎批范围（门 2 代码门三签后冻结）

1. `orchestrator/direction/lexical_index.py`（新）：§3.1/§3.2/§3.3 的纯函数
   + `Bm25Index` + `rrf_fuse`，无 I/O、无全局可变态；
2. `orchestrator/direction/planner_recall.py`：`rules_version` 参数、
   `lexical_hints` 参数、融合入口 `_fuse_route()`（原稿误写 `_lexical_route()`，
   修订 A8-L3 校正；词法通道装配在 `_lexical_route()` 之外由它调用）、
   r3 分支代码零 diff；
3. `memory_flywheel/vector_engine.py`：新增只读 `count_rows(name)`（词法索引
   失效键）与 `scan_rows(name, columns)`（AC-T6-1 授权的「通用表查询」缝，
   不建表不写）；
4. `memory_flywheel/phase1/learning_store.py`：新增只读 `scan_rows(columns)`
   （缺表返回 `[]`，与 `table_exists()` 同语义）；
5. `tests/milestone25/test_task6_r4_lexical.py`（新，hermetic）：术语抽取、
   BM25 数值、RRF 融合/平局、退化等值、注解、路由、缓存失效、确定性；
6. `tests/milestone25/test_task6_r4_live.py`（新，`kb_integration`）：活库
   三路语料行数/id sha、**生产 r4 逐案 == §6 冻结点预测快照**（逐字节 id 列表
   全等）、r3 分支仍复现冻结 iter3；
7. 历史锚点钉子：`gen_r3_prod_union_snapshot.py` 显式 `rules_version="r3"`；
   既有 r3 测试经 helper 显式 `rules_version="r3"`（§10）；
8. 窗口取证/机判工具与自测（随引擎批一并落地，修订 A8-L3 补列）：
   `scripts/phase25/gen_task6_r4_window_evidence.py`（§7.1 门 2 取证）+
   `scripts/phase25/task6_r4_verdict.py`（§7.2 机判）；hermetic 自测
   `tests/milestone25/test_task6_r4_window_evidence.py`、
   `tests/milestone25/test_task6_r4_verdict.py`、
   `tests/milestone25/test_task6_r4_registration.py`（§7.2 表 ↔ 件字节锁存）；
9. 参考模拟修订（A8-L4）：`task6_r4_reference_sim.py::_build_planner` 的
   r3 钉子改为签名前置 + 构造后属性复核，不再 `except TypeError` 静默回退。

## 5. 冻结锚点（门 1 双 lane 复算一致）

| 件 | 冻结值 |
|---|---|
| golden fixture | sha256 `f5c5cc58890ce5f9aff1c5f0dd6b97bf1486e716060bcd29d612793458b7c3a6`（47 案，阈值 .80/.75/.75） |
| 判分器 `eval_planner_recall.py` | sha256 `e73fe8b8cb3e3220050e98b00d4c070660641300ab5af16a023e1048991922d7` |
| 同义词典 `synonyms.yaml` | sha256 `b731ccbe3c9178c9f1a92fc549a76199ce46b06d957ddf2ea92a4435305339c3` |
| r3 稠密基准（iter3 报告） | sha256 `f1c0e7f4caf32e066ef7ed4dbac9a75f02e32fc9e3876e42180e10ba267fa307`（38/47=.8085、sol 14/18、lrn 11/12） |
| 活四表 id 集 sha（前缀） | kb `35d4bf61…`(1542) / solutions `cdab79c7…`(94) / phase1 `022341f7…`(44) / phase2 `82058d2f…`(1) |
| 嵌入 | mpnet 768（离线三开关，0 下载） |
| phase25 ledger | 56 行（Stage B 全程 0 付费，恒 56） |
| 点预测快照 | `scripts/phase25/snapshots/task6_r4_point_predictions.json` sha256 `e7e1bc3853ac199bdc25bb99feabda19b1f688b5f4f214f10483f6a9d0740a76` |

## 6. 设计期参考模拟与点预测（**如实披露**）

### 6.1 披露（方法学，必须原样保留）

本实验的规则形**在设计期已用只读离线模拟在本 47 案上比较过若干候选合并形**
（尾席替换 L=1..4、去词法无支撑行、RRF），并按「教科书默认参数 + 机制理由」
选定 RRF 形式与 base-hint 词法查询串。因此：

- **这不是盲测**：§6.2 的点预测是确定性机械推论（参考模拟机产，sha 注册于
  §5），不是 Stage A 式的概率性最佳判断；
- 有约束力的判分仍是**唯一一次**冻结判分器生产跑（§7），其作用是
  「生产实现是否逐位复现冻结规格 + 三门阈值机判」；两者不一致即实现缺陷；
- 诚实标注：本轮的「预注册」防的是**判分后**改规则/改阈值/改标注（§7 黑名单），
  不能防「设计期在同一标注集上选形」。这是本标注集上做规则实验的结构性局限，
  因此结论表述限定为「在该冻结标注集上的验收结果」，不外推为「新规则形
  普遍更优」。任何外推需要新标注集，属未来立项。

### 6.2 点预测（参考模拟 `task6_r4_reference_sim.py`，确定性）

| 指标 | r3（基准） | r4 预测 |
|---|---|---|
| overall | 38/47 = 0.8085 | **45/47 = 0.9574** |
| kb 子路 | 13/17 = 0.7647 | 16/17 = 0.9412 |
| solutions 子路 | 14/18 = 0.7778 | **18/18 = 1.0** |
| learnings 子路 | 11/12 = 0.9167 | 11/12 = 0.9167 |
| 翻失（H3 命中 → r4 miss） | — | **0** |
| 残留回收（9 案） | — | **7** |

残留 9 案逐案预测（id 列表见快照 `cases[].fused_ids`）：

| miss 案 | 预测 | 融合名次 | 命中方式 |
|---|---|---|---|
| kb_execmode | 回收 | 1 | gold_id |
| kb_pipolicy | 回收 | 1 | gold_id |
| kb_16d_layout | 回收 | 4 | gold_id |
| sol_win6_align | 回收 | 4 | gold_id |
| sol_win2_servo | 回收 | 5 | gold_id |
| sol_win4_region | 回收 | 5 | gold_id |
| sol_win3_clearance | 回收 | 5 | **gold_text**（同文件兄弟块 `c7d2a7a8`，非 gold_id 行） |
| kb_check_success | **仍失** | — | 词法名次 9（DF 竞争下恰在 top-8 外一格） |
| l_fluid_wall | **仍失** | — | 词法通道 0 匹配（gold 行是 `human_failure_agent_run` 叙述行，与 query 无 4-gram/标识符交集） |

敏感性披露（同设计期测得，一并冻结留痕）：若词法查询串改用**扩展 hint**
而非 base hint，预测为 44/47=0.9362、sol 17/18、lrn 11/12、翻失 0、
回收 6（少 `sol_win3_clearance`）。两种取法都过三门且零翻失——即本实验的
结论方向**不依赖** raw/hint 这一选择；选 base hint 系 §3.4 的机制理由。

### 6.3 标签与实际规则的耦合（修订 A2，门 1 前由 LEAD 登记，2026-11-02）

冻结判分器的 `--rules-version`（sha `e73fe8b8…`）**只是报告标签**：
`build_planner()` 不把它透传给 `PlannerRecall`。因此绑定跑的实际语义来自
§3.7 的构造函数默认 `r4`，`--rules-version r4` 保证标签与默认一致。
该解耦的残余风险由 §7.2 机判兜底：`point_prediction_match` 逐案比对
`top8_ids` 与 §6 冻结快照，若行为实为 r3（或任何非 r4 实现），逐案 id
必然不同 → `retain=false` → 判负。即「标签说谎」不可能换来保留结论。

## 7. 绑定评测、尝试纪律与判决

### 7.1 唯一有约束力的尝试

前置全部满足后（引擎批三签、冻结锚点复算一致、`--check` 逐字节复核通过、
门 2 绿），判分器**首次**调用的 exit 0/3：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
/mnt/venvs/simpraxis/bin/python scripts/phase25/eval_planner_recall.py \
  --iteration 5 --rules-version r4 \
  --prior-iterations <iter1.json> <iter2.json> <iter3.json>
```

- 报告 `$SIMPRAXIS_PHASE25_ROOT/task6_recall/planner_gold_iter5_<ts>.json`（0600）；
- 判分器/ fixture / synonyms **一字不改**（sha 见 §5）；
- 首呼之前：装配失败/崩溃/exit 2/**生产实现与冻结点预测快照不一致**
  （视为实现缺陷）都不算尝试，可在引擎批内修复重来，前提是规则规格（§3）、
  点预测快照、fixture/阈值零改动；
- 首呼之后**黑名单**（碰任一即本轮判负、STOP）：任何引擎代码/测试/判分器/
  fixture/synonyms/预注册件/点预测快照的改动；任何第二次判分器调用
  （含只读复跑）；任何按结果回填的常量。
- 白名单：只读复核与证据读取；§9 预授权回滚；门 2 取证脚本；
  首呼**之前**的快照/预注册登记提交。

### 7.2 判分后机判脚本（首呼前落盘并登记 sha）

`scripts/phase25/task6_r4_verdict.py`（新，只读**四**份 JSON：iter3/iter5/
点预测快照/窗口工程证据）——**修订 A6（2026-11-02 门 1 登记，按落地件逐字
校正本节）**：

- CLI：`--iter3 --iter5 --predictions --evidence [--out]`；`--evidence`
  即 §11/门 2 证据机产器输出的窗口工程证据（四表身份/Q1Q2Q3/写入面/
  ledger/容器/显存/门禁声明），本件原写的 `anchors_ok` 已由「输入件 sha
  钉死」取代：iter3 报告 sha 必须等于 §5 的 `f1c0e7f4…`，点预测快照必须
  等于 `e7e1bc38…`，否则 exit 2（不作判据）；
- 输出：`conditions{1_thresholds,2_h3_subset_h4,3_residual_recovery,
  4_data_identity,5_engineering}`、`point_prediction_match`、
  `verdict`（`RETAIN_R4`/`NEGATIVE_ROLLBACK_R3`）、`retain`；
- `retain` = **五条件 ∧ `point_prediction_match`**（见修订 A5/§13）。

**首呼前登记的三个件 sha**（登记后改动即黑名单命中；下表为门 1 修复轮后的
**最终登记值**，见修订 A8/§13.6）：

| 件 | sha256 |
|---|---|
| `scripts/phase25/task6_r4_verdict.py` | `adea3f524446a743ecec38b799af1ae0878b6299f2cab0a79332644e7756d3e2` |
| `scripts/phase25/task6_r4_reference_sim.py` | `836cf8a4198c164e6989a9b65d329e27876f67b76fd5f247228faa1341b1143f` |
| `scripts/phase25/gen_task6_r4_window_evidence.py` | `a6743aa570a957ec10729bbecb5e84e60ff2dc961e072db78003612f31f2e1b3` |

一致性机判：`tests/milestone25/test_task6_r4_registration.py` 逐字解析本表并
比对三件实际字节；prereg 表与落地件一旦漂移即红（防"登记的是上一版"）。

### 7.3 PROC-7 溯源过程（预注册的例外程序）

`kb_integration` 门内 `tests/milestone2/test_task0b_layers.py:590` 含非 dry-run
幂等重灌，会把 `solution_chunks` 94 行 `ingested_at` 重戳（内容/向量不变）。
预注册的顺序是：**全部 kbint 跑完 → 旁置 mv + `cp -a` 溯源自窗前备份终复 →
四表全列（含 `ingested_at`）+ 768 维向量 + id 集 sha 逐字复核 → 之后不再跑
kbint**。终复不加跑任何门；32 passed 类结论只依赖内容/身份，不因终复失效。
零载模、零付费；旁置件不删。

## 8. 保留条件（**保留 r4 当且仅当全部满足**）

1. **三门阈值全过**（判分器 exit 0）：overall ≥0.80、solution ≥0.75、
   learning ≥0.75（阈值取自 fixture，不在此复述为可调项）；
2. **零翻失**：r3 的 38 个命中案在 iter5 全部仍命中（H3 ⊆ H4，机判）；
3. **残留回收 ≥6/9，其中 gold_id 直中 ≥5**（gold_text 兄弟块回退计回收但
   **不计**直中，机判 `matched_by`）；
4. **数据层与嵌入身份不变**：四表行数 1542/94/44/1、dim 768、id 集 sha 与 §5
   全等；Q1/Q2/Q3 有序 13 id 集与冻结集恒等；**活库零写入**（唯一例外是
   §7.3 记录的 kbint 门自身重戳 + 门后溯源终复；例外**范围**另由证据
   `write_surface.gate_restamp_other_rows == 0` 单独机判，修订 A8）；
5. **零付费与工程门**：phase25 ledger 恒 56（0 实呼）；两排除 hermetic 全套
   rc0；`kb_integration` 门绿；`gate_static.sh` 六步绿；容器**存量** 0
   （`docker ps -a` 口径，含已退出/created，硬规则 5；修订 A8）、显存
   ≤200 MiB（至少读到一行合法读数，空输出按未测处理）。

**五条全真 → 保留 r4 为生产默认**（写 Complete、更新 CLAUDE.md/记忆、回收
证据）。**任一为假 → H-R4 不成立**，按 §9 回滚，结论按「矛盾即结论」封存，
不得调参/换深度/换融合再试。

> **修订 A5（2026-11-02 门 1 登记）**：「五条件」是**科学**判据，不包含
> 实现正确性。落地件另设第六道闸 `point_prediction_match`（iter5 逐案
> `top8_ids` == §6 冻结快照）：它不是第六个科学条件，而是「生产实现确实
> 复现了冻结规格」的前置——不一致时任何指标解读都无意义。`retain` 取
> **五条件 ∧ 该闸**（更严方向，见 `task6_r4_verdict.py`）。「生产默认」按
> 修订 A7 理解为构造函数默认值，不涉及 setter/cli 行为。

## 9. 判负处置（预授权，形态甲）

若 §8 任一条件为假：

- **形态甲回滚**：新增一个提交把 `DEFAULT_RULES_VERSION` 改回 `"r3"`，
  `rules_version="r4"` 代码路径保留并以测试封存（默认关闭、无生产消费者），
  不改写任何已提交历史；
- 生产默认回到 r3，`DirectionSetter` 行为与 r3 时点逐字一致（按修订 A7：
  setter 今天根本不装配 `PlannerRecall`，故这条在回滚前后**都成立**，不是
  回滚带来的差异）；
- Complete 记录负结论（五条件逐条机判 + 点预测对账），**不重试、不加深度、
  不换参数、不换第二词法通道**；
- 未来重启须用户显式裁决（新预注册），本件不授权自动 r5。

## 10. 测试改造纪律

- 既有 r3 锚点测试（`test_task6_planner_recall.py`、`test_task6_r3_quota.py`）
  经 helper 显式传 `rules_version="r3"`，**断言内容一字不改**（它们测的是 r3
  语义，r3 语义未变）；`gen_r3_prod_union_snapshot.py` 同法显式钉 r3，
  重跑须与注册快照 sha `53c043dc…` 逐字节相同；
- 新 r4 测试**不得**复用 r3 的期望值；数值断言全部来自 §3 规格的独立推导
  （小语料手算）或 §6 冻结快照（活库测试）；
- 禁 `skip`/`xfail`/TODO/放宽阈值；r4 分支必须有「退化 == r3」的等值测试。

## 11. 证据落位与收尾

- 仓内：本件、`task6_r4_reference_sim.py`、`task6_r4_verdict.py`、
  `snapshots/task6_r4_point_predictions.json`、引擎批与测试；
- /mnt：iter5 报告（0600）、窗口日志 `task6_r4_window.log`（0600）、
  kbint 前后四表全列快照与终复比对件；
- 收尾：`docker compose down`（无容器则跳过并记录）、显存 ≤200 MiB、
  两排除全套冷跑、`check_git_author.sh`、author/trailer 合规则；
- 提交策略：本地攒批（main ahead origin 23 → 递增），**push 永远人工 STOP 门**。

## 12. 衔接与禁止

- 本件不授权：Stage C/嵌入再试/r5 自动迭代/Task 7/8/9 恢复（用户已裁决停止）；
- 与 AC-T6-4 的关系：r3 已满足「gold@8 三门」；Stage B 是**在同一验收集上的
  新规则形验收**，不改 AC-T6-4 的达标判据，也不回写 r3 结论；
- 模型：本实验 0 实呼，不涉及任何 model_id 变更（硬规则 1 不触发）。

## 13. 门 1 修订登记（A3–A7，2026-11-02，首呼前由 LEAD 登记）

门 1 规格 lane（laneA）对落地件与本节条款的**对账**提出 2 CRITICAL + 5
MAJOR。全部按「修落地件 + 登记修订」处理，**不动 §1 机制假设、§3.1/§3.2/
§3.3 规则形、§5 冻结锚点、§6 点预测、§8 五条件**（科学侧零改动）。逐条：

| 编号 | 位置 | 性质 | 处置 |
|---|---|---|---|
| A3 | §3.5 词法语料扫描列 | MAJOR（来源字段缺失 → prompt 出现 `?`） | 加列，不改文本/排序 |
| A4 | §3.4 词法查询串缺省 | MAJOR（规格自相矛盾） | r4 下 `lexical_hints` 必填，fail-closed |
| A5 | §7.2 `retain` 定义 | MAJOR（§7.2 与落地件不一致） | 明确 `retain` = 五条件 ∧ 点预测闸 |
| A6 | §7.2 输入/输出 | MAJOR（按落地件校正） | 逐字写入 CLI 与输出键 + 三件 sha |
| A7 | §3.7/§9「生产默认 r4」 | MAJOR（事实错误） | 更正为「构造函数默认值；今日无生产消费者」 |
| A8 | §8.4/§8.5 工程判据 | C×2 + M×2（门 1 双 lane 修复轮） | 容器改 `docker ps -a` 存量口径；非授权戳记计数入硬条件；缺表/空显存读数为观测缺口 |

### 13.1 A3 词法语料扫描列（落地件已修）

- `LEXICAL_KB_COLUMNS = (id, text, doc_type, kb_relpath, section_path)`；
  `LEXICAL_SOLUTION_COLUMNS = (id, text, relpath, kind, chunk_index,
  solution_id)`；`LEXICAL_LEARNING_COLUMNS = (id, text, evidence_relpath,
  run_id, gate_source, rung)`；learnings 另按库注入 `learning_table` 标签
  （与稠密路 `_store_label` 同一来源）。
- 机判：`test_task6_r4_lexical.py::test_lexical_{kb,solution,learning}_row_
  carries_renderer_fields` 直接跑 setter 渲染器，断言输出文本无 `?`；
  `test_lexical_corpus_scan_columns_cover_renderer_fields` 做列集合对账。
- 排序不变性：扫描列不参与 BM25（`text` 未变）、不参与 RRF，故 §6 点预测
  逐案 `top8_ids` 不变——由 `test_live_production_r4_equals_frozen_point_
  predictions` 复验。

### 13.2 A4 词法查询串必填（落地件已修）

- `retrieve_with_hints(..., lexical_hints=None)` 在 `rules_version="r4"`
  下直接 `ValueError`，**不猜**（原「缺省 = hints」会让扩展串静默充当词法
  查询，等于一条未登记的第二种语义）；r3 分支参数可选且被忽略。
- 生产入口 `retrieve()` 一直显式传三路同 base hint，行为不变。
- 机判：`test_r4_requires_explicit_lexical_hints`（且断言失败发生在任何
  检索之前）、`test_r3_ignores_lexical_hints_entirely`。

### 13.3 A5/A6 判决口径与件登记

- `retain` = 五条件 ∧ `point_prediction_match`（第六道闸是实现正确性前置，
  不是第六个科学条件）；`verdict` ∈ {`RETAIN_R4`, `NEGATIVE_ROLLBACK_R3`}。
- 件 sha 见 §7.2 表格；三者自登记起进入 §7.1 黑名单（首呼后任何改动即
  本轮判负）。

### 13.4 A7 生产消费者的真实状态（事实更正，不改变实验设计）

- 事实：`orchestrator/graph/cli.py:326` 构造
  `DirectionSetter(gateway, vector_engine, recall=recall)`，**不传**
  `planner_recall`；全仓无第二处注入点 → `PlannerRecall` 目前**没有生产
  消费者**。
- 后果（如实写进判决与 Complete）：本实验测的是**规则形**能否在冻结 47 案
  上回收残留且零翻失；「保留 r4」= 保留构造函数默认值与代码路径，「回滚」
  = 把 `DEFAULT_RULES_VERSION` 改回 `"r3"`。两者都**不改变**图/setter 的
  生产行为（它走 `ProductionRecall` 旧路）。
- 未做也**不得**在本件内做：把 `PlannerRecall` 接进 `DirectionSetter` /
  `cli.py`。那是单变量之外的新改动，若将来要做，须新预注册 + 新门 1。

### 13.5 未采纳项（记录理由）
- **A1 尾席保持**（§3.3 原修订）：维持。全 9 行融合会改 12 个 learning 案里
  2 案的冻结 top-8（`l_rungs`、`l_fluid_reject`），构成未声明的第二变量。
- 规格 lane 若另提「换 K1/B/深度/融合式」类建议：一律不受理——§0/§8 已写死
  「不得调参翻案」，且实验前不得再造第二轮设计期比较。

### 13.6 A8 门 1 修复轮（2026-11-02，双 lane 报后、绑定跑前）

门 1 双 lane（`gate1_laneA_report.md` REJECT C=3/M=6/L=10；
`gate1_specLane_report.md` 附加 1 FAIL + MAJOR 2）后，对**取证/机判工具**与
引擎批做的修复。全部为「让判据不可空真」的收窄方向，不改规则规格（§3）、
不改 fixture/阈值/点预测快照，故不构成第二变量：

1. **C2 容器判据**：`_containers_present()` 改用 `docker ps -a` 并做分隔符
   归一（`sim_praxis_sandbox` 含下划线，旧子串法恒 False）。证据键由
   `containers_running` 改名 `containers_present`；verdict 只认新键，旧键
   视为「未观测」（`test_engineering_rejects_legacy_containers_running_key`）。
2. **C3(b) 例外范围**：evidence 的 `write_surface.gate_restamp_other_rows`
   进 verdict 硬条件（必须为 0）；缺 `write_surface` 亦判负。
3. **M6 缺表**：`_write_surface` 对**任一侧**缺表/缺 `rows`/`id_set_sha256`/
   `per_row` 即 `EvidenceError`——"两侧都缺"不得记成零写入。
4. **L10 显存**：`_vram_mib` 空输出/非数值即 `EvidenceError`，不再
   `sum([])==0` 恒过。
5. **L8 Q1Q2Q3**：窗口前快照某查询 id 集为空即 `EvidenceError`，`[] == []`
   不作身份证据。
6. **L4 参考模拟 r3 钉子**：`_build_planner` 去掉 `try/except TypeError`
   （参数改名会静默退回构造器默认 r4），改为签名前置 + 构造后
   `rules_version == "r3"` 复核，不符即 `AnchorError`。
7. **C1 kbint 门**：`test_task6_r4_planner_recall.py` 补 `_build_r3_planner`
   装配 helper（原引用未定义符号 → kbint 门必红），活库复跑 4 passed。
8. **M1 词法语料扫描列**（A3）与 **A4 显式 `lexical_hints`** 已随引擎批落地。

已登记的残余口径（不改代码、如实留痕）：evidence 的 `hermetic/kbint/
gate_static/provenance_restore` 四项仍是**执行方声明**（`declared_by_executor`
键逐项列出），脚本只保证不默认 true；"执行方说谎"由仲裁与门 2 复核兜底，
不属机判可判范围。修复轮后三件 sha 见 §7.2，由
`test_task6_r4_registration.py` 锁存。
