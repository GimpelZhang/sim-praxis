"""host_shim 包标记（Task6 run7，规格 task6-run7-host-shim-design v1.1a）。

本包是引擎侧主机迁移托底（非模型代码）：sitecustomize 引导件 +
shim_core 唯一实现/文案源。引擎内需要脱敏函数时只走包路径
``from orchestrator.common.host_shim.shim_core import redact_for_model``；
子进程激活只经 PYTHONPATH 前拼 prelude + sitecustomize 合成名加载，
两条解析路径互不混用（规格 §3.4）。
"""
