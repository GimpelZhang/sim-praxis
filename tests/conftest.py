"""pytest 收集前引导。

litellm==1.99.4 在 Python 3.10 下的打包疏漏（NotRequired 急切导入，详见
orchestrator/gateway/__init__.py 的锁版本内修垫片）：测试模块在文件顶部直接
`from litellm.exceptions import ...`，早于对 orchestrator.gateway 的导入，
故在 conftest 阶段先导入本包，确保 typing 垫片在任何 litellm 导入前生效。
"""
import orchestrator.gateway  # noqa: F401  # 副作用：安装 py3.10 typing 兼容垫片
