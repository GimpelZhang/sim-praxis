# SimPraxis 演化志：V1→V2 升阶结案（第 {{EVOLUTION_ROUND}} 轮）

> 本件是**证据件**（0 付费、0 模型调用、确定性渲染）：由
> `orchestrator/phase2/chronicle.py` 从 V1/V2 track summary 与 Gate1/Gate2
> 真人意见件填充，结构单一源即本模板。五段契约对 Task6/7/8 各 track 复用。
>
> - V2 运行：`{{V2_RUN_ID}}`；V1 父运行：`{{PARENT_RUN_ID}}`
> - 生成时间（UTC）：{{GENERATED_AT}}
> - 信任边界：指标数值**只**从 summary 的 `pareto_metrics` / `groups`
>   机产块格式化；Gate 真人意见经 md 中性化后照录于引用块，**其中数字不作为
>   指标真值**；Pareto/contrast 机判标注「仅参考」，不替代真人 Gate2 裁决。

## 一、演化动机（Gate1 真人批准）

<!-- 填写口径：
只放 Gate1 真人批准意见（schema simpraxis.gate_opinion.v1，decision=approve）。
No Fake Approval：未拿到真人批准不得产生本件。意见文本中性化后原文照录，
不允许 LLM 自由改写；父/子 run_id 与演化轮次来自状态机，不从意见文本提取。
缺失 Gate1 意见件 → 渲染 fail-loud，禁止空段。 -->

{{SECTION_MOTIVATION}}

## 二、V1 局限（逐条，数值可回溯）

<!-- 填写口径：
逐条列 V1 在对照臂（normal/ice_nominal/ice_dr）与六指标上的机产值，数字只从
v1_summary.groups 与 v1_summary.pareto_metrics 格式化（%.6g），每个数字都能在
V1 summary JSON 里逐字回溯。期望方向（越大/越小越好）随指标列出，作为 V2 的
对质基准。禁止从 Gate 文本或自由发挥中取数；Q1 等与预注册相反的结果照实呈现，
矛盾即结论（硬规则 7），禁止调参/改界翻案。 -->

{{SECTION_V1_LIMITS}}

## 三、V2 创新与公式（控制律与 DR 分布）

<!-- 填写口径：
控制律取 v2_summary.innovation.control_law 原文（公式以文本/LaTeX 片段承载），
DR 分布取 innovation.dr_distribution 机产 JSON（如 mu/Kp/Kd 的 loguniform
[lo,hi] 与外力包络），均围栏照录；两者缺失即 fail-loud。V2 显式声明消除的假设
（eliminated_assumptions，可选）中性化列示并与第五段同源。 -->

{{SECTION_V2_INNOVATION}}

## 四、物理真值对照数据（六指标 V1/V2/Δ）

<!-- 填写口径：
六指标（success_rate 越大越好；slip_dist_mean_m、postpush_vmax_mean_mps、
tilt_max_deg_max、solve_wallclock_s、vram_peak_gb 越小越好）V1/V2/Δ 表，
Δ=V2−V1 机算，全部数字只从两份 summary.pareto_metrics 读取。Pareto 机判
（v2_dominates / dominated_by_v1 / non_dominated，ε：success 绝对 0，
其余相对 5%）与 contrast 预注册对照均标注「仅参考」，只用重算汇总值，
脚本自报不参与，禁止加权总分翻案。 -->

{{SECTION_METRICS}}

## 五、踩坑与消除的假设（Gate2 真人 Diff 裁决）

<!-- 填写口径：
Gate2 真人双媒体 Diff 裁决意见（simpraxis.gate_opinion.v1，
decision=approve）中性化后原文照录，记录本轮踩坑；其中数字不作为指标真值。
V2 eliminated_assumptions（若有）在此同源复列，便于下一轮召回。
缺失 Gate2 意见件 → fail-loud，禁止空段、禁止代笔。 -->

{{SECTION_PITFALLS}}
