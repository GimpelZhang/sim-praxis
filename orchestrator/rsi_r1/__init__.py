"""RSI-R1：最小自主具身研究闭环（Plan ``~/文档/RSI_R1_Plan_detailed.md``）。

模块边界（Plan §2.1）：
  lanes.py       预算 lane 单一真值（零 import）
  contracts.py   类型/公开 schema/参数/单位/指标/错误规则唯一源
  runtime.py     ledger/gateway/准入/预算装配（无裸发网入口）
  state.py       study/batch/confirm 状态与恢复
  loop.py        文本 JSON 决策循环
  experiments.py 候选矩阵验证与批执行
  sandbox.py     候选代码沙盒与精确数据视图
  observations.py 可信 raw 采集
  scenes.py      两场景模板与参数应用
  statistics.py  主估计量/区间/Claim 分类
  judge.py       原始观测到科学证据判决
  replay.py      独立根真物理重放
  watchdog.py    进程监护与人工事件计时
  closure.py     结案归集与证据包
  cli.py         init/check/controls/freeze/probe/study/replay/closeout

本包不触网、不调用模型、不写旧根，除非经显式 runtime 装配。
"""

__all__ = ["lanes", "contracts"]
