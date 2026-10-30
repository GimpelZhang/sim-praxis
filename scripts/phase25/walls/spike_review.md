# Task 5 历史墙 A/B spike — G2 证据完整性独立评审

- reviewer: review-t5 (code-reviewer lane)
- review_utc: 2026-10-30T09:59Z（初评）；2026-10-30T10:04Z 复评（§9），最终 APPROVE
- 评审方式：独立只读 lane；零付费、零联网、零容器实跑（仅 hermetic 契约测试与
  只读核对）。所有数字从 ledger/trace/score/manifest/lock/代码原件重算，不以
  `spike_report.md` 自报为准。
- 被评审对象：`scripts/phase25/walls/spike_report.md`（LEAD 署名，untracked）
- 评审范围：team-lead 任务书六项（冻结纪律/付费纪律/隔离反作弊/干净失败/
  报告数字一致性/负结果合规）

## 0. 结论

| 项 | 件级结论 |
|---|---|
| 1 冻结纪律 | APPROVE |
| 2 付费纪律 | APPROVE |
| 3 隔离/反作弊 | APPROVE |
| 4 干净失败论证 | APPROVE |
| 5 报告与原始件一致性 | APPROVE（初评 REQUEST-CHANGES，1 MAJOR+3 MINOR 复评全闭，见 §9） |
| 6 负结果合规 | APPROVE |

**最终（复评 10:04Z）：APPROVE。** 初评 REQUEST CHANGES 的 1 MAJOR +
3 MINOR 经 team-lead 仅改报告文字全部闭环（复评见 §9，逐项独立复核通过，
无新增失实）；评审期间冻结件/原始件/代码零改动（`git diff 4592c4e` 空、
九件哈希仍全 MATCH）。三态判决「不成立」、`d4_stop_required=true`、25 呼账证、
冻结/隔离/反作弊全部经原始件独立重算成立。

初评计数：CRITICAL 0 / MAJOR 1 / MINOR 3；复评后全部关闭，无新增问题。

## 1. 冻结纪律 — APPROVE

1. 九件闸哈希逐位独立重算（`freeze_common.gate_hashes()` 与 `sha256sum`
   双路），与 `LOCK.json.gate_hashes`、`freeze_manifest.json.gate_hashes`、
   两 run 的 `run_meta.json.gate_hashes`、契约测试 PINNED 常量五方全部一致：

   ```
   MATCH  common VERDICT_CRITERIA.md 73abfc7b…
   MATCH  wall_a TASK.md c4fd5348… / GATE.md 5d0b4474… /
          wall_a_runner.py 21507ded… / expected_vectors.json b6a7ac63…
   MATCH  wall_b TASK.md 5725313e… / GATE.md 91ececd2… /
          wall_b_checker.py 5f178625… / SEQ_SPLIT.json 861f5e77…
   ALL_MATCH True
   ```

2. 首呼时间线无懈：闸内容件 mtime ≤ 16:08 CST（`__pycache__` 17:00 亦早于
   锁），基座提交 `4592c4e` author/committer 时间 17:07:27 CST，LOCK 落盘
   17:09:48 CST（=locked_iso 09:09:48Z，即墙 A 首呼时刻）；LOCK 仅一次写入
   （mtime 单一），墙 B 重入走 `write_lock` 哈希比对分支且墙 B 实际开跑——
   即「LOCK 对墙 B 逐位比对通过」是被执行路径覆盖的，不是纸面声明。
3. `git log -- scripts/phase25/walls` 只有 4592c4e 一条；
   `git diff 4592c4e` 与 index 均空；工作区唯一变动是 untracked 报告与本
   评审件（另 verifier 件 spike_verification.md）。首呼后闸件/判据/评分器
   零改动，与报告 §头声明一致。
4. `freeze_common.assert_unlocked` 使冻结脚本见锁即拒跑；契约测试
   `test_gate_hashes_pinned_byte_for_byte` /
   `test_gate_embedded_hashes_truthful` /
   `test_freeze_manifest_matches_live_hashes` /
   `test_lock_hash_mismatch_refuses` 在活冻结根下 30/30 全绿（§8 命令 12）。
5. 补登记（与 verifier T5-LOW-1 独立互证，非报告问题）：
   `VERDICT_CRITERIA.md` §1 代入行写「A = half_up(16.667) = 16、
   B = half_up(8.333) = 9，clamp 未触发」，严格算术是 raw half-up 17/8、
   **两侧 clamp**（A 封顶 16、B 保底 9）；最终子帽 16/9、和 25、储备 3
   全部正确，可执行真相源 `verdict.py`（整数恒等式 + Decimal ROUND_HALF_UP
   双实现）独立复算无误。该件已在首呼后冻结，按纪律一字不动，仅登记。

## 2. 付费纪律 — APPROVE

1. 双守卫实测：无 `SIMPRAXIS_ALLOW_PAID=1` 裸跑
   `run_wall.py --wall wallA` 实测 stderr「拒绝裸跑付费墙…」**rc=2**
   （在 `verify_freeze` 之前短路，零触网）；第二守卫为账套票据：
   `QuotaLedger.preflight` 缺票即 `_record_blocked` + `GatewayPausedError`，
   不发网；两张票 `P25-B2.json`(approved=16)/`P25-B3.json`(approved=9)
   均 08:40Z 先于开跑，分票独立，跨墙借支在结构上不可能（两墙不同
   batch_id，batch_used_after 各计各帽）。
2. 唯一付费入口：walls/ 目录仅 `run_wall.py` import ModelGateway /
   run_agentic_inner_loop（grep 实证）；`spike_backend.py` 文件头明示不含
   网关代码。
3. phase25 命名空间：`QuotaLedger.phase25()` 独立
   quota/ledger/gateway_state；账行 `approval_ref` 全指向
   `quota/approvals/P25-B{2,3}.json`；Phase 1/2 账未触（本账套总行 31＝
   Task3 P25-B1 6 行 + Task5 25 行）。
4. ledger 独立重算（`/mnt/artifacts/simpraxis/phase25/quota/ledger.jsonl`）：

   ```
   P25-B2 n=16 seq 7..22 连续=True  all ok=True  event=call stage=coding_debug
           model=kimi-k3.1-pro  thread 单一  ts 全 Z
           tokens in/out/total = 90391/2267/92658
           batch_used_after 1..16 单调
   P25-B3 n=9  seq 23..31 连续=True  all ok=True
           tokens in/out/total = 75428/8493/83921
           batch_used_after 1..9 单调
   两墙 seq 交集=空；全局 global_used_after 7..31；非 call 事件 0 行
   ```

   与报告 §2 全部数字逐位相符（含 in/out 拆分）。窗口 A 09:09:57–09:11:57Z
   （120s）、B 09:22:35–09:25:23Z（168s），与报告「约 120s/约 168s」相符。
5. 储备 3 未支用：无 P25-B2/B3 之外的 task5 批次/账行，无 rerun 行，
   counters `consecutive_quota_errors=0`；preflight_blocked 0（31 行全是
   call，无 seq=null 行；首呼前账行数 6 与 22 与全局 seq 起点 7/23 互证，
   另见 MINOR-3a）。
6. call_cache=None 真出生：run_wall.py 付费循环显式 `call_cache=None`，
   run_meta `callcache_enabled:false`；trace 25 个 `__model_round__` 的
   args 逐轮 `cache_hit=false`（另见 §4）。
7. 补充澄清（不属问题）：ledger 有供应商侧 prompt-cache 计费字段
   tokens_cache_read（A 累计 68,096 / B 39,168），那是方舟对重复系统提示的
   账单缓存，不是本地重放缓存，与 `cache_hits=0（真出生）` 不矛盾。报告
   未提此区别，建议再版加半句以防读者对账误读（MINOR-2）。

## 3. 隔离 / 反作弊 — APPROVE

1. 模型容器 flags（spike_backend.py `_BASE_DOCKER`，两墙共用）：
   `--network=none --user 1000:1000 --cap-drop ALL
   --security-opt no-new-privileges --read-only --pids-limit 256
   --memory 4g --tmpfs /tmp …`，并经契约测试
   `test_model_container_argv_never_mounts_scorer` 对活后端断言。
2. 模型容器挂载面（`SpikeWallBackend._mounts`）：
   - 公共：`/work`(rw)、隔离 libero venv→`/opt/venv`(ro)、主 venv 同路径 ro；
   - wallA 另挂 LIBERO tour→`/opt/libero_tour`(ro) 与
     gate_env→`/opt/gate`(ro，仅 config.yaml + pyshim 软链，链向容器内
     `/opt/libero_tour/libero`，宿主侧为空目标，不构成逃逸）；
   - wallB 无 tour、无 /opt/gate。
   代码与测试双证：模型 argv **永不**含 `/opt/inst`、
   `pitcher_open_watertight`（wallA 测试还反向断言 tour/gate 只在 A）。
   `_check_no_docker_sock(mounts)` 二次防 docker sock。
3. 评分器只在评分路径出现：墙 A 评分容器才挂 `gate_dir→/opt/inst:ro`
   （score_wall_a），且在 loop 自终局+容器清零后一次性启动；墙 B 评分在
   **宿主主 venv** 跑（score_wall_b，form=host_main_venv），score.json
   traceback 路径 `/home/vipuser/sim-praxis/scripts/.../wall_b_checker.py`
   与 work 绝对路径即宿主执行的直接证据。冻结 OBJ
   （ab309057…，sha 实测一致，48,527B，manifest frozen_evidence 1536面/
   770点）只作为宿主评分 argv 出现，不进任何模型容器。
4. `my_check.py` 接触答案可能性论证：该件是模型在 /work 内自建（4,050B，
   r8 出生、r9 `python my_check.py` rc=1）。其可读面只有 /work 与两个
   ro venv 的 site-packages；主 venv 无指向仓的 editable .pth（实测仅
   coloredlogs/distutils pth），仓与 /mnt 冻结根都未挂载；network=none；
   TASK.md 经 grep 不含 768/2304/1536/OBJ 路径/opt/inst（NO_LEAK）；
   lead_only/（oracle+6 变异）0700 且从不挂载。my_check 的 rc=1 失败点在
   其第 14 行 `make_vessel_mesh("selfcheck_vessel.obj")`（M1 同一缺陷），
   第 77 行后的轨迹自检根本没执行到——属真实自检失败而非泄题。
5. 环境终态：`docker ps -a` 全空、label 残留 0、镜像 digest
   `sha256:abf96496…12fed7` 与冻结值一致；GPU 现在 14 MiB；且模型容器固定
   env `CUDA_VISIBLE_DEVICES=""`（墙 A 评分容器复用同 env），CPU 墙不仅靠
   采样、更是构造性禁 GPU。

## 4. 干净失败论证 — APPROVE

1. 两 outcome.json：`kind=round_cap`（A 16/16、B 9/9），
   supplier_errors=0、empty_responses=0、cache_hits=0、is_failure_artifact=true。
2. trace 侧逐轮互证：25/25 模型轮 `args.empty_response=false`、
   `cache_hit=false`；ledger 25/25 `ok=true err=null`；deny 是 policy 层
   拒绝（argv 含 shell 元字符，obs 原件 `[POLICY DENY exit=2]`，两墙各 1，
   deny_tools=[run_cmd]），不是供应商错误也不是空回答。
3. 「储备不可支用、无重跑机会」符合 VERDICT_CRITERIA.md §4/§5/§7：
   储备唯一支取条件（供应污染＋seq/原文/时间戳三证）一个都不成立；
   §5 短路第 4 条「到子帽未过」两墙命中 → 不成立；§7 立即停、D-4 报用户、
   禁止抬帽/换种/改界/换模型。聚合件 reasons 仅一条
   「工程闸到子帽未过：['wallA','wallB']（不抬帽/不换种/不放宽）」，
   per_wall escalated/human/contaminated 全 false，与纯函数
   `engineering_verdict` 输出一致（我按 verdict.py 短路顺序核过）。
4. B-cap 轴处置符合 §6：B-eng 不过 → label `b_eng_fail_d4_stop`、
   d4_stop=true、`b_cap_passed=null`（run_wall.py 仅在 passed=true 时计算
   b_cap_passed）。数值的机制陈述问题见 MAJOR-1，但轴处置本身正确。

## 5. 报告声称 vs 原始件 — REQUEST-CHANGES（数字全对，一处机制陈述需修）

逐项独立重算，**全部数字核对通过**：

- trace 事件 39（＝16 model_round + 13 `__condense__` + 10 工具事件）/
  21（9+6+6）；空轮 A r1–5,r7＝6/16＝37.5%，B r1–3＝3/9＝33%，
  合并 9/25＝36%；deny 各 1（A r8/seq14、B r7/seq29，obs 原文一致）。
- **工具步口径报告已正确披露**：§3.3 明写「16 个工具步含 2 个 policy
  deny…**实际放行执行 14 步**（steps_used 口径 10+6=16 含被拒尝试，
  deny 同样消耗一轮）」，§8 第 4 条再次登记；与我独立重算一致
  （工具事件 A10+B6=16、实执行 A9+B5=14），此处无问题。
- token 92,658 / 83,921（in/out 拆分亦逐位符）；GPU 31 个采样点全 14 MiB、
  final 14；墙 B 11/12 PASS（12 项名单逐项对 score.json，唯一 FAIL＝
  M1_vessel，traceback line 68 一致）；拓扑 768/2304/nonmanifold 0、
  fan boundary 2/nonmanifold 1、历史件 1536/0/0 逐位符；K1–K4
  same_witness=true、违例三项/一项/一项，K5/K6 raise 类无 same_witness
  字段——报告 §3.2 表格分列正确。
- 关键词文档计数独立重算：A 在 61 个文档（trace.jsonl+full 44+raw 16）上
  ControlEnv 20 / libero_spatial 9 / OSC_POSE 5 / use_camera_obs 3 /
  build_env 3；B 在 33 个文档（1+23+9）上 6/8/6/6/8——与报告完全一致。
- 逐轮表抽查：A 五轮空轮原文 561/94/97/39/84 ch、r5 尾随行
  「…valid Python code block.```」孤立围栏、r7 1,882ch、
  r9–r16 write/run(rc1)/read/write/run(rc0)/write/run(rc0)/write 全符；
  B 683/131/139 ch、r5 17,378ch（wc -c 实测 17,378）一次性出生 8,221B、
  r6 回读、r8 my_check 4,050B、r9 rc=1（stdout 空 sha e3b0c44…、stderr
  即 M1 traceback）全符；work 终态 204B / 8,221B+4,050B 全符；墙 A 评分
  3 探针 PASS 后 FileNotFoundError、elapsed 0.534s 全符。
- 看门狗：两 run 目录九文件（七件套+cmd_sha256+toucher.err），
  exit_code=0、meta guardian child_exit=0/restarts=0、silence_s=1200、
  heartbeat 15s；alerts.jsonl `grep -c t5wall` 计数 **0**（报告「13 历史行
  0 命中」口径一致）；两 drills 目录无 notifier jsonl（fake notifier 零
  通知的设计语义，报告 §4/§8 已注明）；无残留进程（仅系统 watchdogd）。
- 冻结快照 0600/根 0700、双引擎密钥扫描件 clean（ts 08:32:05Z，先于首呼；
  留存弱点见 MINOR-3b）。

发现的问题：

- **[MAJOR-1，HIGH] b_cap 数值来源的机制陈述失实**
  文件：`spike_report.md:32-34`。
  报告称 b_cap 的 speed_max=0.0/clearance_min=0.02「是 M1 壶网格生成抛错后
  轨迹从未真正执行的**空默认值**」。原始件证据相反，我三点独立坐实：
  1. 评分器 wall_b_checker.py:294-305 的 b_cap 块在 K6 之后**无条件独立
     执行**，不依赖 M1 结果；评分器字面默认是第 44 行 `"b_cap": None`；
  2. score.json 的 b_cap 为
     `{"speed_max":0.0,"clearance_min":0.02,"speed_pass":true,
     "clearance_pass":true}`，**无 "error" 键**——证明模型自著的
     `design_pour_trajectory()`（work/agent_solution.py:182-198）被实际
     调用、评分器真实的 `kinematic_witness()` 实际计算了：0.0 来自模型把
     spout 写成恒定点
     （`np.tile([0,0,1.045],(161,1))`，work/agent_solution.py:196），
     0.02 来自模型恒值 clearance 数组（:197 `np.full(...,0.020)`），
     是**模型退化设计被评分器算出的计算值**，不是评分器空默认；
  3. 「轨迹从未真正执行」只在「无 Genesis 物理仿真」意义上成立（B-cap 在
     本 spike 本来就只有运动学簿记）；运动学簿记本身执行了。
  报告的判决方向（无能力证据含义、b_cap_passed=null、§6 d4 STOP）正确且
  处置无误——空虚满足（vacuous pass）不构成能力证据；但 G2 证据件对评分器
  行为作了错误机制陈述，三签/用户 STOP 材料被追问时站不住，也低估了评分器
  的独立性（它没有被 M1 短路）。
  修法（仅改报告文字，不动任何冻结件/原始件）：改为「b_cap 数值由评分器对
  模型自著 design_pour_trajectory() 实际计算得到（b_cap 块独立于 M1）：
  模型给出 spout 恒定点、clearance 恒 0.020 的退化轨迹，故 speed_max=0.0、
  两 pass=true 属空虚满足，不构成能力证据；依 §6 B-eng 不过，
  b_cap_passed=null」。

- **[MINOR-1，HIGH] r5 时延一位小数误舍入**
  文件：`spike_report.md:84`。trace 原件 `latency_ms=85349`（85.349s，
  raw 17,378B 与报告一致），一位小数应为 **85.3s**，报告写 85.4s。
  不影响任何判决。

- **[MINOR-2，LOW] 建议补一句两类缓存的区别**：§2 可注明 ledger 的
  tokens_cache_read（68,096/39,168）是供应商账单侧 prompt cache，与
  call_cache=None/cache_hits=0 的本地重放互不相干，防对账误读。

- **[MINOR-3，LOW] 证据保留两处薄弱（非报告失实，后续任务改进）**：
  (a) 两次 `--preflight` 的「账行 6→6、22→22」原始 stdout 未归档（watchdog
  stdout 只包实跑）；现有代码（cmd_preflight 写账即 SystemExit）+ ledger
  全局 seq 起点（7/23）+ 0 条 non-call 行可互证，结论成立但精确数字无独立
  留痕；
  (b) 两个 freeze 脚本写同一 `snapshots/secret_scan.json`
  （freeze_common.py:255 固定文件名），墙 B 后写覆盖，墙 A 的 Python 引擎
  扫描结果未单独留存（write_secret_report 命中即中止冻结，墙 A 段存在即
  当时 clean；且存活的 shell 引擎是全仓扫描覆盖全部闸件，风险可忽略）。
  建议后续扫描件按墙分名。

## 6. 负结果合规 — APPROVE

1. `git status --porcelain`：仅 untracked 三件
   （spike_report.md / spike_verification.md / spike_review.md）；
   4592c4e 即 HEAD，无 tracked 改动、无 staged、无新提交、无 stash。
2. 无抬帽（SUBCAPS 硬编码 16/9，两墙 batch_used_after 恰好到帽即熔断）、
   无换种/改界（判据/评分器/期望向量九件哈希锁后零动）、无换模型
   （stage_models.yaml coding_debug=kimi-k3.1-pro 未改，25 账行 model 字段
   全部 kimi-k3.1-pro）、无顺手产品代码改动（报告 §5 还明确拒绝在负结果上
   改执行器协议，建议另立任务）。
3. 无重跑/补发：seq 7–31 一次性连续，无重复 thread、无 reserve 批次。
4. 不回收 KB、不发成功白皮书、Track1 保持 opt-in，与硬规则 7/Task 纪律一致。

## 7. Positive observations

- 冻结工程做到了五方哈希互证（LOCK/manifest/run_meta/契约常量/实算）且
  LOCK 比对在墙 B 是真实执行路径；`merge_section` 见锁先比对再拒写的设计
  正确（另段冻结后闸件被动过即拒）。
- 隔离是「构造性」而非「观测性」的：--network=none/--read-only/cap-drop ALL/
  CUDA_VISIBLE_DEVICES=""/OBJ 不入模型容器，均有代码+契约测试双证，不依赖
  运行时自觉；墙 B 评分器 b_cap 块对 M1 失败不短路，独立算出模型退化轨迹，
  反而是仪器诚实性的加分项。
- trace 留存质量高：25 轮原文+obs 哈希+usage+latency+policy 决定全在，使
  「空轮 36%」「两起 deny」「真出生」均可从原件复算，本评审全部数字一次对平。
- 负结果处置克制：D-8 零件不引入、Track1 不灰度、B-cap 空虚满足不抬结论，
  预注册立场维持得很干净；报告 §8 主动登记判据措辞瑕疵 T5-LOW-1 且不违禁
  改冻结件，做法正确。

## 8. 评审独立执行的命令/核对清单（关键摘录）

1. `python -I` 调 `freeze_common.gate_hashes()` 对 LOCK 九件 → ALL_MATCH
   True；`sha256sum` 九件交叉一致。
2. `git log/status/diff 4592c4e`、`git show -s --format=… 4592c4e`（提交
   17:07:27 < 锁 17:09:48）。
3. 读 run_wall.py / spike_backend.py / verdict.py / freeze_common.py /
   wall_b_checker.py（含 :44 默认值、:294-305 b_cap 块）/ 两 GATE.md /
   TASK.md（grep 泄题 NO_LEAK）。
4. ledger.jsonl python 重算：16+9 行、seq 7–31 连续、全 ok、全 Z、token
   92,658/83,921、两墙无交集、非 call 事件 0；counters.json=0。
5. 读 P25-B2/B3 票据（16/9，08:40Z，禁跨墙/储备条款逐字在 note）。
6. trace.jsonl 逐轮解析：39/21 事件、空轮 6/3、deny 各 1（obs
   `[POLICY DENY exit=2]`）、usage 求和、r5 latency=85,349ms/raw 17,378B
   （wc -c 复核）、r9 rc=1 与 stderr。
7. 关键词在 trace.jsonl+full+raw（61/33 文档）独立 grep 计数，全符。
8. score.json/stdout.log：墙 A 3 探针+FileNotFoundError 0.534s；墙 B
   12 项 11 PASS、M1 line 68、拓扑/K 组逐位符；b_cap 来源追到 checker
   :294-305 与 work/agent_solution.py:182-198（恒定点 :196、恒 clearance
   :197）；work/my_check.py 全文（失败点 :14，无答案路径）。
9. gpu_mib.csv 31 点全 14；`docker ps -a` 空、label 残留 0、镜像 digest
   符；`nvidia-smi` 现 14 MiB。
10. watchdog runs/meta/drills/alerts：两跑 exit 0、guardian child_exit 0、
    silence 1200、`grep -c t5wall alerts.jsonl`=0、drills 无 notifier、
    无残留进程。
11. 裸跑守卫实测 rc=2；interval_t5.json 子帽 raw 17/8→clamp 16/9；
    SEQ_SPLIT 398–436 连续 39（30 eng/9 cap）；OBJ sha=ab309057…；
    主 venv 无 editable .pth；gate_env 仅 config.yaml+容器内软链。
12. `SIMPRAXIS_T5_FREEZE_ROOT=… pytest tests/milestone25/
    test_task5_wall_contracts.py -q -m "not paid"` → **30 passed**（活冻结
    根，含九件哈希/白名单/反挂载/LOCK 拒跑/fail-closed 全套，0 付费）。

reviewer: review-t5 (code-reviewer lane)
review_utc: 2026-10-30T09:59Z

## 9. 复评记录（2026-10-30T10:04Z）— 四项全部闭环，转 APPROVE

team-lead 仅改 `spike_report.md` 文字（未动冻结件/原始件/代码，未重跑；
`git status` 仍只有三件 untracked 报告，`git diff 4592c4e` 空）。逐项复核：

1. **MAJOR-1 已闭（修法与建议一致，无新增失实）**：报告 §1（现 :31-42）重写
   后逐子陈述核对——(a) checker:294-305 b_cap 块在 K6 后无条件独立执行、不被
   M1 短路，属实（M1 失败是局部 check 记录，score.json 中 M2/K1-K6 均执行）；
   (b) 字面默认 `b_cap: None`（checker:44），属实；(c) 实际调用模型
   `design_pour_trajectory()` + 评分器 `kinematic_witness()`，score.json 无
   error 键，属实；(d) 数值来源 work/agent_solution.py:196
   `np.tile([0,0,1.045],(161,1))` 恒定点 → speed_max=0.0、:197
   `np.full(...,0.020)` → clearance_min=0.02，逐位属实；(e) 两 pass=true
   定性为 vacuous pass、不构成能力证据、`b_cap_passed=null`（run_wall 仅
   B-eng 过才算），判决方向正确；(f)「未执行的是 Genesis 物理，不是运动学
   簿记」边界陈述准确。我初评建议的修法被完整采纳且未过度外推（「等价于不倾倒」
   是对恒定 spout+恒 clearance 的合理解读，不改变判决）。
2. **MINOR-1 已闭**：§3.2 :95 改为「85.3s（latency_ms=85349）」，与 trace
   原件一致；逐轮表 :116「85s」整数粗口径保留，无误。
3. **MINOR-2 已闭**：§2 :59-61 加注供应商账单侧 prompt-cache。新增数字我已
   从 ledger 独立重算：tokens_cache_read P25-B2=68,096 / P25-B3=39,168、
   **tokens_cache_create 两批均 0**（25 行求和），与报告逐位相符；两层缓存
   区分表述准确。
4. **MINOR-3 已登记**：§8 第 5 条 T5-LOW-2（:211-218）逐字登记 preflight
   stdout 未归档与 secret_scan.json 同名覆盖两点，三方互证逻辑与「后续按墙
   分名」建议均在，明确不影响本判——符合「只登记不改冻结件」纪律。

另确认 verifier 同步修字（K1–K4 分列 :107-108、14/16 步 :129-131、
drill 空目录语义 :154/:207-208）与我独立重算一致；T5-LOW-1 只登记不修字
处理正确（首呼后冻结件一字不动）。

**复评结论：APPROVE。无 CRITICAL、无未闭 MAJOR/MINOR（T5-LOW-1/2 为
登记项，按纪律不改本判）。三签可继续。**

reviewer: review-t5 (code-reviewer lane)
review_utc: 2026-10-30T10:04Z
