"""Phase 2.5 Task 5 —— 历史墙 A/B spike 的冻结与运行操作件。

模块纪律（计划 §3 Task 5，AC-T5-1..7）：

- :mod:`verdict` 是**纯函数**（子帽 half-up/clamp、双半值 tie-break、
  重跑算账、三态判决、B-cap 独立轴），单测用合成 trace 必红/必绿变异反证；
- ``gates/`` 是首呼前冻结的闸定义（GATE.md/TASK.md/评分器/期望向量/
  SEQ_SPLIT.json），冻结后哈希入 manifest，首呼后不得改字；
- ``freeze_wall_a.py``/``freeze_wall_b.py`` 只在首呼前跑：物化 env、
  快照登记、双密钥扫描、绿/红双向容器验证；
- ``run_wall.py`` 是唯一付费入口（双守卫 + run_watched 七件套在外层），
  生产代码零新增（除 Task 4 缺陷修复走锁内适配留痕）。
"""
