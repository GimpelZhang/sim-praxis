# RSI-G1 开源项目资料（架构参考，非 SOTA 证据）

本文件是**静态资料清单**（Plan §7）。G1 不 clone / install / run 这些项目，
代码中也不导入它们；运行时不触网。资料只用于借一项机制，不能当本项目的
能力、安全或泛化证据。

| 项目 | 已核机制（只读一手资料） | 只借一项 | 明确不借 |
|---|---|---|---|
| Darwin Gödel Machine（README 明示 Apache-2.0；arXiv:2505.22954） | 用 foundation model 产生可修改程序变体，保留 candidate/archive 并评估 | `candidate_id / parent_id / patch hash / eval_run_id / decision` 的谱系字段（G1 trace 的 parent 链） | 不引入自改源码、开放 archive；不把 benchmark/SOTA 自称当证据；未核其独立 replay 与安全保证 |
| AI Scientist-v2（README 标 Source Code License / Responsible AI License；arXiv:2504.08066） | experiment-manager/tree search、timestamped experiments/logs、论文与审阅产物；官方声明可能无 PDF/review、成本与依赖不保证 | parent/candidate 节点关系 + 时间戳运行 + artifact 引用 | 不接入其 tree-search / 外网 / 依赖；不把自动论文或自评审当独立科学证据 |
| GEPA（README 明示 MIT；arXiv:2507.19457） | execution traces、诊断侧信息、预算 `max_metric_calls`、候选/validation 分离与 Pareto | `score + trace_ref + diagnostics` 与硬预算字段 | 不引入 GEPA 依赖；不把 prompt/optimizer 称递归科研；reflection 文本不替代独立评估 |
| OpenEvolve | **UNVERIFIED**（本次未纳入已核一手来源） | 无 | 不凭项目名或宣传补机制/SOTA 结论 |

## 共通边界

- 这些资料支持**机制借鉴**，不支持安全保证、SOTA 排名、跨任务泛化或成功预期。
- G1 只在既有证据包上加入 parent/candidate 谱系、实际 artifact 引用、预算与
  fail-closed 门；不复制框架、不增加运行时依赖。
- 本清单不进入候选提示，不触发网络运行时；含凭据的字段一律不写入。
