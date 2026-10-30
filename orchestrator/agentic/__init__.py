"""Phase 2.5 Task 4：agentic 内层工具循环（可选 opt-in，0 付费产品代码期）。

本包在 LangGraph 八节点治理环不变的前提下，为 agent_run 节点提供可选的
内层工具循环（``RunSpec.agentic=True`` 才启用；默认 False 时旧路径逐字节
不变，见 AC-T4-11）。接触面收敛在 smolagents 1.26.0 的五个文档化点：
``Model.generate`` / ``CodeAgent.run`` / ``CodeAgent.step``（_step_stream）/
``@tool`` / ``LocalPythonExecutor``，版本精确 pin，不追升级。

模块分工（计划 NF-6 文件名）：
- model.py     GatewayModel：smolagents Model → ModelGateway.call("coding_debug")
- loop.py      CodeAgent 产品装配（build_agent）/内层循环引擎/图分派缝/续写原语/Condenser
- policy.py    PreToolUse 硬拒绝层 + 三硬闸强制链（jail→schema→policy）
- jail.py      realpath 前缀 jail / 软链逃逸拒 / 大小行数上限
- schema.py    手写参数 schema 校验（不引新包；pydantic 既有依赖但不使用）
- executor.py  HardenedLocalPythonExecutor：AST 节点收紧（上游自认非安全沙盒）
- forensics.py trace.jsonl 取证 / 观察规范化 / agentic-v2 指纹 / 全量观察存储
- caps.py      三门预算（工具步数帽 / token 帽 / coding 轮帽）
- tools/       六组工具（read/write/run/probe/recall/terminate）

安全语义只由我方件提供：LocalPythonExecutor 上游文档自认非安全沙盒
（local_python_executor.py:1692-1693），故执行器 AST 收紧 + 工具 forward
闸 + policy 硬拒绝三重纵深，默认授权标准库只收紧不放宽。
"""
