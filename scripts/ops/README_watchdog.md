# 看门狗 SOP（Phase 2.5 Task 2，AC-T2-1..8）

> 计划 §0.2 的可执行落地。**凡运行时间可能 >5 分钟的进程（容器实跑、
> 出生/修复轮、批量评测、HF 大下载、图 resume 等待），未经
> `run_watched.sh` 包装不得发起。** Task 5/9 起强制 dogfood。

- 核心实现：`orchestrator/common/watched.py`（薄壳只做解释器转发，
  选型留痕：lookbehind 失败关键字、monkeypatch 假网关、元监控续判都需要
  Python 表达；shell 无法设 `PR_SET_PDEATHSIG`）。
- 退出码约定：`0` 成功终态；`3` 死亡签名；`4` 静默签名；`5` 轮询预算
  耗尽/停指；`6` 元监控重启超限。
- 所有操作件落 `$SIMPRAXIS_PHASE25_ROOT/watchdog/`（0700；alerts/limits
  0600）。**0 付费**：监控不经方舟；飞书走 P2P 免费文本通道，测试一律
  `fake:` 假网关，真实发送只在 Task 8 真人 drill。

## 1. 七件套与心跳（AC-T2-1）

```bash
source scripts/00_env.sh
scripts/ops/run_watched.sh <name> \
    [--cwd DIR] [--silence-s 600] [--success-grep RE] \
    [--success-marker PATH] [--container NAME] \
    [--artifact PATH:sha256]... -- <cmd...>
```

产物目录 `watchdog/runs/<name>-<UTCts>/`：

| 件 | 含义 |
|---|---|
| `pid` | 目标子进程 pid |
| `start_iso` | 严格 UTC Z 启动时刻 |
| `cmd_sha256` | argv 以 NUL 连接后的 sha256（与 `meta.json.argv` 可对账） |
| `stdout` / `stderr` | 目标实时输出（0600，失败签名扫尾部 64KiB） |
| `heartbeat` | 每 15s 刷新（独立 toucher 进程） |
| `exit_code` | 终态退出码（信号死亡为 128+signo，如 SIGKILL→137） |

另有 `meta.json`（监控配置，watch-loop 据此恢复）、`toucher.err`。

**心跳不是目标自己刷的**：toucher 是独立 Python 小程序，fork 后 exec 前
在 preexec 钩上 `prctl(PR_SET_PDEATHSIG, SIGKILL)`（父死由内核直接杀），
exec 后再自断言一次 prctl 并每轮核 ppid。supervisor 被硬杀时 toucher
内核级同死，不会留下「父死假活」的心跳（已注入实测，见
`tests/milestone25/test_task2_watchdog.py::test_toucher_dies_with_supervisor_pdeathsig`）。

## 2. 双签名监控器（AC-T2-2）

```bash
scripts/ops/watch_loop.sh --run-dir watchdog/runs/<name>-<ts> \
    [--poll-s 30] [--silence-s 600] [--success-grep RE] \
    [--success-marker PATH] [--container NAME] \
    [--artifact PATH:sha256]... \
    [--notifier feishu|none|fake:PATH] [--once]
```

每轮同时取两类签名，**缺成功签名时绝不亮绿**：

- 成功：`rc=0`（若预注册了产物则还要逐件 sha 对，不符 fail-closed）；
  标志文件 / 成功 grep **不即终态**（评审 M1）：命中后若进程仍活只判
  PENDING 继续轮询；若随后出现任一死亡签名（清理段崩溃、OOM、rc=137/139
  等），按死亡判 DEATH——成功行可能先于晚期死亡出现，绝不压过死亡签名。
  - rc=0 是权威干净终态：rc0 路径在死亡族裁决之前（`watched.py:285`），
    即使日志尾部含裸 `Killed` 等失败关键字仍判 SUCCESS（退出码为应用自检
    背书）。需要「关键字即致命」语义的 run 不要用 rc0，改用
    `--success-marker` 或预注册产物 sha。
  - 已知角落：grep/marker 命中后进入 await_terminal，若目标永久挂起且
    心跳由 toucher 持续刷新，会恒判 PENDING（静默告警被压住）。长跑使用
    方必须同时挂 `--max-ticks` 或在编排层做终态回收（T5/T9 dogfood
    强制项），禁止「发起后裸等」。
- 死亡：失败关键字（见下）、`exit_code≠0`、pid 消失无 exit_code、
  命名容器 `docker inspect` gone/非 running、心跳超时（>50s，意味着
  supervisor/toucher 已死）。
  - **pid 消失两阶段确认**：`kill -0` 返回 ESRCH 与 supervisor 写完
    `exit_code` 之间有 wait()→原子写调度窗，快速成功退出可能先被看见
    「pid 消失无 exit_code」。首见只 arm：宽限 2s
    （`SIMPRAXIS_WATCHDOG_VANISH_GRACE_S` 可压测压缩）后重采，下一拍
    仍消失才判死；生产延迟 ≈ poll 30s + 2s，仍在 60s 触发预算内。
    `--once` 探针同样过宽限复查（cron 防假红）。
- 静默：成功与失败双缺、进程仍活，但 stdout/stderr 最新 mtime 超静默帽。
- `awaiting_human`：run 目录出现 `awaiting_human` 标志文件即显式等 Gate
  态，**不告警、不计静默**（阻塞节点等待是合法态）。

失败关键字（常量区 `watched.py::FAILURE_PATTERNS`，收窄边界）：
`\bTraceback \(most recent call last\)`、`CUDA out of memory`、
`(?<![A-Za-z])Killed(?![A-Za-z])`（NF-4，不命中 `KilledWorker`）、
`OOM-kill(ed)?`、`\bOutOfMemoryError\b`、`exit code [1-9][0-9]*`。
良性白名单：同行含 `recovered|retried OK`；docstring/注释行
（`#`/`//`/`"""`/`'''`/`*`/`---` 前缀）。≥6 良性零告警与 ≥6 真失败
必告警均有合成日志反证测试。

触发动作序固定（60s 内）：①飞书 P2P 文本（含分诊包路径）→ ②七项快照包
（ps 树/nvidia-smi/docker logs --tail 200/日志 tail 100/ledger 末 3 seq/
quota pause 态/heartbeat mtime，单项不可用也在 manifest 记 note，七名恒齐）
→ ③`watchdog/alerts.jsonl` 0600 原子追加。

消费口径（评审 L4）：`alerts.jsonl` 是 **append-only 多 run 混写**全局流，
含历史演练、失败驱动重跑的全部行，机判消费者**必须按 `run_name` 过滤**；
正式证据以 `watchdog/drills/<ts>/final_alerts.json` 的过滤件为准，不得直接
对全流计数。

## 3. 元看门狗（AC-T2-8）

```bash
scripts/ops/watch_loop.sh guard --run-dir <run-dir> \
    --poll-s 30 --guard-poll-s 30   # 其余参数同 watch-loop
```

guard 是 watch-loop 的父进程：子监控器每拍刷**独立心跳键**
`watchdog/meta/<run>/watch_loop.heartbeat`（与目标 run 心跳分开）。
子监控器被 `kill -9` 或心跳连续丢 3 拍（30s 档 = 90s 阈，真实 kill 后
60–120s 窗内）→ 发 `meta_death` P2P + 分诊包 + alerts 行，然后重启
子监控器；重启从 run-dir 重新推导全部状态，原被监控进程的死亡/静默可
续判。干净退出（0/3/4）立即向上传播，不重复告警。guard 自身的存活由
下方挂载模板的上层 until 循环兜底。

## 4. 静默时限表（AC-T2-5）

常量唯一真值在 `watched.py::SILENCE_LIMITS`，播种为
`watchdog/silence_limits.json`（已存在不覆盖）：

| 键 | 值 | 场景 |
|---|---|---|
| `ark_single_call_s` | 300 (5min) | 方舟单呼（供应侧 120s 超时，5min 即异常） |
| `gpu_step_eval_s` | 600 (10min，默认) | 容器 GPU 步/评测 |
| `download_s` | 900 (15min) | HF/镜像下载 |
| `single_birth_s` | 1200 (20min) | 单轮出生帽（Complete G-4 有 605s CPU 先例） |

改动走「常量提交 + 票」：改 `watched.py` 常量并在票/提交说明引用本表，
**不许在调用处私传魔法数**（本 Task 0 票，留痕即本文件）。等真人 Gate
不计静默——用 `awaiting_human` 标志，不要抬时限。

## 5. 三种挂载模板（AC-T2-6）

> 编排侧铁律：**驱动 agent 结束自身回合前，每个长跑必须处于「有活监控器
> 或已到终态」之一；禁止发起后裸等。** 后台进程必须在同一回合内回收终态。

### 模板 A：后台 Bash until 循环（无额外工具，最常用）

```bash
source scripts/00_env.sh
RD="$SIMPRAXIS_PHASE25_ROOT/watchdog/runs/t5-run1-<ts>"
scripts/ops/run_watched.sh t5-run1 --silence-s 600 -- \
        bash scripts/some_long_run.sh &
RUN_PID=$!
scripts/ops/watch_loop.sh guard --run-dir "$RD" \
        --notifier feishu --poll-s 30 &
GUARD_PID=$!
wait "$RUN_PID"          # 前台等终态；guard 随 0/3/4 退出
wait "$GUARD_PID" || echo "watchdog rc=$?（3=死亡 4=静默，按决策表走）"
```

上层 until（守 guardian 自己；machine restart/会话断开后自举）：

```bash
until scripts/ops/watch_loop.sh guard --run-dir "$RD" --notifier feishu; do
    rc=$?
    [ "$rc" = 6 ] && { echo "guard 重启超限，STOP 报用户"; exit 6; }
    echo "guard 异常退出 rc=$rc，10s 后自举"; sleep 10
done
```

### 模板 B：Monitor/活监控挂载（Claude Code 会话内）

用 run_in_background 起 run-watched，同时把 `watch_loop.sh guard
--notifier fake:<drill 证据路径>`（未到 T8 不真发飞书时）挂为受监控后台
任务；必须在**同一回合内**等到 guard 终态（0/3/4）并回收，禁止「起后台
然后结束回合」（T0 静默 2h10m 事故的正面回应）。

### 模板 C：ScheduleWakeup 等价物（cron/systemd timer）

```bash
# 每 5 分钟一次 --once：只采一轮，3/4 时由 cron 包装发通知/续作分支
*/5 * * * * cd ~/sim-praxis && source scripts/00_env.sh && \
    scripts/ops/watch_loop.sh --run-dir "$RD" --once --notifier feishu \
    || true
```
`--once` 只打印裁决 JSON 并映射退出码，不重复告警（持续监控仍用模板 A）。

## 6. 续作决策表（AC-T2-6）

| 签名 | 第一动作 | 续作（按各 Task 预注册处置表） |
|---|---|---|
| 死亡 rc=3（关键字/rc≠0/pid 消失/容器消失/心跳超时） | 先读 triage bundle 七项 | 瞬时类（供应侧 5xx、拉取失败）按 Task 重试表有界重试；同一签名重复 ≥2 次 → 熔断报用户；容器消失 → 按硬规则 5 清残留后决定，禁止 prune |
| 静默 rc=4 | 先 triage，不自动重试 | 区分「真卡」与「合法长步」：ps/GPU 利用率在动 = 合法长步（事后在本表补证据，**不抬帽**）；无利用 = 取栈/杀轮/按 Task 出生帽走 |
| OOM（cuda_oom/oom_kill/OutOfMemoryError） | 读 bundle nvidia-smi | 走该 Task **预注册降载分支**（batch/并行度下调档），不现场发明参数 |
| meta_death | 读 `meta/<run>/` 与 bundle | guardian 已自动重启；restarts>3（rc=6）→ STOP 报用户 |
| awaiting_human | 不动作 | Gate 裁决回来后删标志文件，监控自动续判 |

## 7. 故障注入反证（AC-T2-4）

`tests/milestone25/test_task2_watchdog.py` 七类全部 hermetic、0 付费、
只杀测试自己 fork 的进程（伪 docker 夹具，禁依赖真 GPU/真容器）：
kill -9→rc3 七项齐、伪 OOM 行、只读致静默 rc4、容器强删 mock、
变异反证（只盯成功标志的退化监控器在死亡时仍亮绿，真实监控器必判 DEATH）、
成功不误报/等 Gate 不告警、≥6 良性与 ≥6 真失败合成日志。元看门狗另有
生产 30s 档真实 kill 前台演练记录（drill 证据目录随执行留痕到
`watchdog/drills/`）。
