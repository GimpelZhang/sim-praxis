# Task 5 历史墙 A/B spike —— verifier 独立重算报告（G2 三签之第三签）

- lane：**独立只读重算 lane**（verifier，与 executor/reviewer 分离）
- 原则：不信任 `spike_report.md` 任何数字；全部计数从 ledger / trace / score /
  原始 raw·full 文档 / watchdog 原始件独立重算
- 零付费：本 lane **零网关调用、零联网、零容器创建**；只跑只读 python/bash、
  本地冻结评分器（CPU）、零付费契约测试。结束时 ledger 仍 31 行（与开始一致）
- 核验时刻：2026-10-30T09:42:56Z（UTC）
- 被核验对象：`scripts/phase25/walls/spike_report.md`（LEAD）
- 判据文本：`scripts/phase25/walls/gates/VERDICT_CRITERIA.md`（sha256
  73abfc7b…，与 LOCK 一致）+ 纯函数 `scripts/phase25/walls/verdict.py`

## 最终结论

**VERIFIED** —— 报告全部计数与独立重算一致；三态「不成立」、两墙干净触帽
（16/16、9/9，零升级/零人工/零供应污染）、储备 0/3、B-cap 轴
`b_eng_fail_d4_stop / d4_stop=true` 均由原始证据独立复现。

- BLOCKER：**0**
- MAJOR：**0**
- MINOR：4（均为措辞/旁证形态，不动任何计数与判决；见 §10）

## 1. Ledger 独立对账（`/mnt/artifacts/simpraxis/phase25/quota/ledger.jsonl`）

| 项 | 我独立算出 | 报告声称 | 一致 |
|---|---|---|---|
| ledger 总行数 | 31 | 31（B1 6 + T5 25） | 是 |
| P25-B2 行数 / ok 数 | 16 / 16（fail=0） | 16，全部 ok | 是 |
| P25-B3 行数 / ok 数 | 9 / 9（fail=0） | 9，全部 ok | 是 |
| P25-B2 seq 区间/连续 | 7–22，逐 1 连续 | 7–22 连续 | 是 |
| P25-B3 seq 区间/连续 | 23–31，逐 1 连续 | 23–31 连续 | 是 |
| 全局 seq | 1..31 严格单调；`global_used_after==seq` 31/31 | seq 7–31 连续单调 | 是 |
| ts 后缀 | 31/31 严格 `Z` 后缀 | 全部严格 Z | 是 |
| A token（ledger 按 thread 求和） | in **90,391** / out **2,267**（总 92,658） | in 90,391 / out 2,267 | 是 |
| B token（同上） | in **75,428** / out **8,493**（总 83,921） | in 75,428 / out 8,493 | 是 |
| A 时间窗（首/末呼） | 09:09:57Z–09:11:57Z（120s） | 09:09:57–09:11:57Z（约120s） | 是 |
| B 时间窗 | 09:22:35Z–09:25:23Z（168s） | 09:22:35–09:25:23Z（约168s） | 是 |
| null-seq / preflight_blocked 行 | **0**（全 31 行 event=`call`，无一 null seq）；两批次均无 blocked | blocked 0（6→6、22→22） | 是 |
| stage/model（两批） | coding_debug / kimi-k3.1-pro（25 行一致） | 同 | 是 |
| P25-B1（Task3 封存批） | 6 行 seq1–6，ok=4/fail=2（EmptyResponse，非本任务） | 6 行封存只读 | 是 |
| 票据 P25-B2/P25-B3 | approved=16 / approved=9，`beyond_normal=false`，规则文本与冻结帽一致 | 16 / 9 | 是 |
| counters.json | consecutive_quota_errors=0，updated 09:25:23Z | —（旁证一致） | 是 |

附：trace `__model_round__` 事件自带 usage（[in,out] 二元组）逐 seq 求和：
A 90,391/2,267、B 75,428/8,493，**与 ledger 逐字节互证**；outcome tokens_used
92,658/83,921 亦一致。

## 2. Trace 事件级独立重算（两墙 trace.jsonl）

伪工具＝`__model_round__`/`__condense__`（后者 seq=null，为上下文压缩事件）；
真实工具事件＝其余；deny 判定读事件内 `policy_decision.decision=="deny"`
（`verdict.deny_records_from_trace` 的调用点 run_wall.py:437-439 做
`{"policy": e.get("policy_decision")}` 适配，schema 链路完整）。

| 项 | wallA 我算 | wallA 报告 | wallB 我算 | wallB 报告 | 一致 |
|---|---|---|---|---|---|
| trace 总事件数 | **39** | 39 | **21** | 21 | 是 |
| `__model_round__` 数（=rounds） | 16（seq7–22 各 1） | 16 | 9（seq23–31 各 1） | 9 | 是 |
| `__condense__`（seq=null） | 13 | — | 6 | — | 旁证一致 |
| 真实工具事件数（含被 deny 的尝试） | 10（allow 9 + deny 1） | 工具步 10 | 6（allow 5 + deny 1） | 工具步 6 | 是 |
| 事件工具分布 | run_cmd 4 / write_file 4 / list_dir 1 / read_file 1 | — | write_file 2 / run_cmd 2 / list_dir 1 / read_file 1 | — | 是 |
| 空轮（该 seq 仅 model_round 无真实调用） | **6**：seq 7,8,9,10,11,13 ＝ r1–5,r7 | 6/16，r1-5,r7 | **3**：seq 23,24,25 ＝ r1–3 | 3/9，r1-3 | 是 |
| policy deny | 1，**r8**（seq14，run_cmd `python -c …` 含 `;` 元字符，exit 理由「argv 段含 shell 元字符」） | 1，r8 | 1，**r7**（seq29，run_cmd `python -c` 多行） | 1，r7 | 是 |
| `args.cache_hit`（逐 model_round） | 16/16 **false** | cache_hits=0 | 9/9 **false** | cache_hits=0 | 是 |
| `args.empty_response` | 16/16 false | empty=0 | 9/9 false | empty=0 | 是 |
| model_round ts_z | 首末 09:09:57/09:11:57 全 Z | — | 09:22:35/09:25:23 全 Z | — | 是 |
| r5 单轮（仅 B） | — | — | latency **85,349ms**，full 原文 **17,378 字符** | 85.4s / 17,378ch | 是 |
| r9（仅 B，seq31） | — | — | run_cmd `python my_check.py` **rc=1**，stdout 空（sha e3b0c442…） | r9 rc=1 无修复轮 | 是 |

互证：outcome.json rounds_used/steps_used（16/10、9/6）与上表一致；
wall_result.forensics.ledger_rounds_match=true 与「ledger 每线程行数＝
model_round 事件数（16/9）」独立互证成立。合并口径：空轮 9/25=36%、
deny 2、工具尝试 16（allow 14）——报告 §3.3 数字一致（「有效工具步 16」
含 2 次被拒尝试，与 steps_used 口径相同；MINOR-4 记措辞）。

## 3. 评分件独立核验

### 墙 A（score/score.json + stdout.log/stderr.log + 冻结 workdir）

| 项 | 我独立核验 | 报告 | 一致 |
|---|---|---|---|
| verdict / rc | fail；wall_result.rc=1 | fail / rc=1 | 是 |
| 环境探针 | 3/3 PASS：numpy 1.26.4、mujoco 3.1.6（/opt/venv）、robosuite 1.4.0 | 同 | 是 |
| 终止错误 | `FileNotFoundError: /work/agent_solution.py`（wall_a_runner.py:60 get_data） | 同 | 是 |
| elapsed_s | 0.534 | 0.534s | 是 |
| workdir 终态 | 仅 `inspect_api.py`（**204B**）+ .score 旁证；交付物确未出生 | inspect_api.py 204B，从未出生 | 是 |

A 评分器在一次性容器内执行（依赖 /opt/venv 的 mujoco/robosuite），本 lane
未重放容器；终止事实由**冻结 workdir 中不存在 agent_solution.py 直接坐实**
（FileNotFoundError 的唯一充分原因），探针 3 版本号取自 score stdout 原文。

### 墙 B（score/score.json，并在宿主主 venv 独立重放冻结评分器）

我用 LOCK 哈希一致（5f178625…）的冻结 `wall_b_checker.py` + 冻结方案
（work/agent_solution.py, 8,221B）+ 冻结历史 OBJ
（/mnt/…/t5_freeze/evidence_wall_b/pitcher_open_watertight.obj，sha256
**ab309057**…，实测 **1536 面 / 770 点 / watertight=True**，与票据一致）
在宿主主 venv 独立重跑，输出到 /tmp（不写证据树）：**rc=1，12 项 11 PASS，
唯一 FAIL＝M1_vessel**，除 traceback 路径外逐字段与 run-day score.json 相同。

| 检查 | 我重放 | run-day score | 关键数值 |
|---|---|---|---|
| S0 无哨兵字面值 | PASS | PASS | 空违规 |
| S1 无硬编码 | PASS | PASS | forbidden_ints/paths 空 |
| M1_vessel | **FAIL** | **FAIL** | `agent_solution.py:68 raise RuntimeError("degenerate profile segment")`（我 sed 核对第 68 行原文一致） |
| M2_open_shell_768_2304 | PASS | PASS | faces **768** / boundary **2304** / nonmanifold **0** |
| M2_nonmanifold_fan | PASS | PASS | boundary **2** / nonmanifold **1** |
| M2_historical_1536 | PASS | PASS | faces **1536** / boundary **0** / nonmanifold **0** |
| K1_all_pass | PASS，same_witness=true，违规/期望均空 | 同 | — |
| K2_static | PASS，same_witness=true | 同 | 3 项违规逐字＝期望（contract 1 hold angle / theta max / theta range） |
| K3_too_fast | PASS，same_witness=true | 同 | 1 项（contract 6 spout speed）逐字一致 |
| K4_penetrating | PASS，same_witness=true | 同 | 1 项（contract 3 clearance）逐字一致 |
| K5_nonfinite_raises | PASS | PASS | （raise 类检查，无 same_witness 字段，见 MINOR-2） |
| K6_degenerate_raises | PASS | PASS | （同上） |
| b_cap 四字段 | speed_max **0.0** / clearance_min **0.02** / speed_pass **true** / clearance_pass **true** | 同 | wall_result.b_cap_passed=**null**（空默认值不具能力证据含义），处理正确 |

workdir 终态：agent_solution.py 8,221B + 模型自建 my_check.py 4,050B
（+ __pycache__/.score），与报告一致。

## 4. G-15 真出生三证

| 项 | 我独立算出 | 报告 | 一致 |
|---|---|---|---|
| seq 单调（forensics + trace） | A 7→22、B 23→31 严格单调，seq_advance_monotonic=true | 同 | 是 |
| 关键词文档命中文档口径 | trace.jsonl + raw/ + full/：A **1+16+44=61**，B **1+9+23=33** | 61 / 33 | 是 |
| A：ControlEnv / libero_spatial / OSC_POSE / use_camera_obs / build_env | **20 / 9 / 5 / 3 / 3**（我对 61 个实文件逐字节 grep 复算全 5 词） | 20/9/5/3/3 | 是 |
| B：edge_manifold_counts / make_vessel_mesh / kinematic_witness / evaluate_precheck / watertight | **6 / 8 / 6 / 6 / 8**（33 个实文件全 5 词） | 6/8/6/6/8 | 是 |
| callcache_enabled | run_meta 两墙=false；forensics 两墙=false | 均 None/false | 是 |
| trace 无 cache_hit=true | A 16、B 9 个 __model_round__ args.cache_hit 全 false（逐事件，非信自述） | cache_hits=0 | 是 |
| 新鲜时间戳 | 首末呼 ts 在各自 run window（watchdog 起跑 09:09:39/09:22:19 → run_meta 09:09:48/09:22:27 → 首呼 09:09:57/09:22:35），全 Z | 新鲜 Z | 是 |
| ledger↔trace 轮次互证 | thread 行数 16/9 ＝ model_round 事件数 16/9 | ledger_rounds_match=true | 是 |

## 5. GPU（两 gpu_mib.csv 独立重算）

| 墙 | 采样行数（含表头外） | max | final | 报告 | 一致 |
|---|---|---|---|---|---|
| A | 13 采样 | **14 MiB** | **14 MiB** | 均 14 | 是 |
| B | 18 采样 | **14 MiB** | **14 MiB** | 均 14 | 是 |

## 6. 看门狗（Task 2 dogfood）

两目录 `/mnt/…/watchdog/runs/t5walla-20261030T090939Z/`、
`…/t5wallb-20261030T092219Z/`：

| 件 | A | B |
|---|---|---|
| meta.json / pid / start_iso / heartbeat / cmd_sha256 / stdout / stderr / exit_code / toucher.err | 9 件齐全 | 9 件齐全 |
| exit_code 文件内容 | `0\n`（xxd 30 0a） | `0\n` |
| start_iso | 09:09:39Z（早首呼 18s） | 09:22:19Z（早首呼 16s） |
| argv（meta） | run_wall.py --wall wallA，silence_s=1200，heartbeat 15s | … --wall wallB，同 |
| stdout 终局行 | `完成：passed=False calls=16/16 cap_exhausted=True gpu_final=14MiB` | `…9/9…14MiB` |
| cmd_sha256 | ca80728c… | 2a849698…（两墙不同，符合不同 argv） |
| alerts.jsonl 中 t5wall* | 全文件 13 行，**0 命中** | 同 |
| drills 同名目录 | 存在但**为空目录**（无 notifier.jsonl / alerts.copy.jsonl / p2p.jsonl） | 同 |

对照 Task2 既有约定，drill 目录仅在告警实际发出时才写文件（t2 三个
drill 有 alerts.copy.jsonl/p2p.jsonl/timeline.jsonl）；两 t5 drill 为空
＝ fake notifier 从未被触发，「零告警」实质成立（MINOR-3 记文件名差异）。

## 7. 判决独立复算（import 冻结 verdict.py，WallResult 字段只取自两 wall_result.json）

子帽双算（(60,30)）：

- 整数恒等式：A `(2·1500+90)//180 = 3090//180 = 17`；B `1590//180 = 8`
- Decimal ROUND_HALF_UP：A=17、B=8（两实现一致）
- clamp[9,16] 后：**A=16、B=9，和=25，reserve=3，double_half_tie=false**
  （compute_subcaps 输出与票据/LOCK 一致；MINOR-1：判据 md 正文把 16/9
  直接写成 half_up 值并称「clamp 未触发」，实际 raw=17/8、两侧 clamp 均
  触发，raw 和与 clamp 和恰都=25；不影响任何运行件）

`engineering_verdict(WallResult(wallA, passed=False, cap_exhausted=True, …),
WallResult(wallB, …), baselines={wallA:60,wallB:30})` 我的实际输出：

```json
{"verdict": "不成立",
 "reasons": ["工程闸到子帽未过：['wallA', 'wallB']（不抬帽/不换种/不放宽）"]}
```

`b_cap_axis(B)`：`label=b_eng_fail_d4_stop, d4_stop=true,
b_eng_passed=false, b_cap_passed=null`。附加强健性反证：即便把
b_cap_passed 错置为 true，B-eng 未过时轴仍返回 d4_stop=true。
聚合件 `runs/verdict-20261030T092635Z.json` 的 engineering_verdict /
b_cap_axis / d4_stop_required=true 与我复算逐字段相同。

冻结纪律旁证：LOCK.json 九件闸哈希我全部对仓内活文件重算——**9/9 MATCH**
（含 VERDICT_CRITERIA.md 73abfc7b…、两评分器、expected_vectors、
SEQ_SPLIT 等），首呼（09:09:48Z）后闸件零改动；两墙 run_meta.gate_hashes
彼此逐位相同且等于 LOCK（墙 B 重入比对通过）。
`spike_backend.py:36` 模型容器 `docker create … --network=none` 在码。

## 8. 容器 / GPU 收尾

| 项 | 我实测 | 判据 | 结论 |
|---|---|---|---|
| `docker ps -a` 全量 | **整机 0 个容器**（不仅本仓 label=0；也无他方容器在列，未对任何镜像/容器做 rm/prune） | 本仓残留 0 | 通过 |
| nvidia-smi | **14 / 40,960 MiB** | ≤200 MiB | 通过 |

## 9. 零付费契约测试（新鲜执行，全本地 monkeypatch）

`source scripts/00_env.sh` 后：
`pytest tests/milestone25/test_task5_wall_contracts.py -q -m "not paid"`
→ **30 passed in 8.88s**（必绿/必红变异反证含子帽、tie-break、三态短路、
deny 计数等）。注：未 source 环境首跑 27 passed/2 failed/1 skipped，2 个
失败均为 `SIMPRAXIS_PHASE25_ROOT 未设置` 的 fail-closed 环境守卫，非代码
缺陷；source 后 30/30 绿。本 lane 结束 ledger 仍 **31 行**，零实呼。

## 10. 不一致清单

| 级 | 项 | 说明 | 影响 |
|---|---|---|---|
| MINOR-1 | 冻结判据 md 措辞 | VERDICT_CRITERIA.md §1「A = half_up(16.667) = 16，B = half_up(8.333) = 9 … clamp 未触发」与其自带纯函数不符：raw half-up=17/8，clamp[9,16] 两侧均触发后才是 16/9（raw 和、clamp 和恰都=25）。verdict.py / interval 件 / 两张票据 / LOCK 全对，只有 md 一句 prose 错；冻结件不可改，仅登记 | 无（运行件全部正确） |
| MINOR-2 | 报告 K1–K6 措辞 | 报告 §3.2 称「K1–K6 … same_witness=true」；score.json 中 same_witness 字段只在 K1–K4，K5/K6 为 raise 类检查（ok=true，无该字段）。12 项 11 PASS 与违例清单不受影响 | 无 |
| MINOR-3 | drill 文件名 | 任务书预期 drills 同名目录下 notifier.jsonl；实际两 t5 drill 为空目录，仓内约定文件名是 alerts.copy.jsonl/p2p.jsonl（仅告警时写）。零告警由「空目录 + alerts.jsonl 0 命中」共同坐实；另报告 §4 所称「30s 元 guard」在 watchdog/runs 下无独立 t5 痕迹（仅两个 t5 run 目录），其运行不可由现存产物复核 | 无（看门狗本体七件套+exit0+零告警可证） |
| MINOR-4 | 合并口径措辞 | 报告 §3.3「有效工具步 16」含 2 次 policy-denied 尝试（allow 执行=14）；与每墙 steps_used（10/6，含 deny）口径自洽，但「有效」二字可被误读为全部成功执行 | 无 |

无 BLOCKER、无 MAJOR。

## 11. 我实际运行的命令（只读/本地，零付费零联网）

- `cat quota/approvals/P25-B{2,3}.json`；`wc -l ledger.jsonl`
- python 解析 ledger：按 batch/thread 计数、ok、seq 连续/单调、Z 后缀、
  tokens 求和、null-seq 扫描、global_used_after 对账、counters.json
- python 解析两 trace.jsonl：事件/工具/seq 分布、空轮、policy_decision、
  args.cache_hit/empty_response、usage 求和、ts_z、r5 latency/full 长度、
  r9 rc
- python 对 raw/+full/+trace.jsonl 全部实文件做 10 个关键词字节级 grep
- 读 outcome/wall_result/forensics/score/stdout/stderr/run_meta/LOCK/
  聚合 verdict；sed 核 agent_solution.py:68；ls 冻结 workdir
- 冻结 wall_b_checker.py 在宿主主 venv 对冻结方案+冻结 OBJ 独立重放
  （输出仅落 /tmp，已清理），trimesh 核 OBJ 1536/770/watertight
- python 重算两 gpu_mib.csv max/final；nvidia-smi
- ls/cat 两 watchdog 目录九件、exit_code xxd、meta、stdout、grep alerts
- `import verdict`：compute_subcaps、half_up 双算、engineering_verdict、
  b_cap_axis（含 b_cap_passed=true 反证）
- 九件 LOCK 哈希对仓内活文件全部重算；grep `--network=none`
- `docker ps -a`（全量 0 容器；未执行任何 rm/prune）
- `pytest tests/milestone25/test_task5_wall_contracts.py -q -m "not paid"`
  → 30 passed
- 结束复核 ledger 仍 31 行；本 lane 未改任何其他文件（本报告除外）

---

verifier: verify-t5 (independent recompute lane)
timestamp: 2026-10-30T09:42:56Z
verdict: **VERIFIED**（BLOCKER 0 / MAJOR 0 / MINOR 4）
