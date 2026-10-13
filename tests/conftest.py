"""pytest 收集前引导。

litellm==1.99.4 在 Python 3.10 下的打包疏漏（NotRequired 急切导入，详见
orchestrator/gateway/__init__.py 的锁版本内修垫片）：测试模块在文件顶部直接
`from litellm.exceptions import ...`，早于对 orchestrator.gateway 的导入，
故在 conftest 阶段先导入本包，确保 typing 垫片在任何 litellm 导入前生效。
"""
import orchestrator.gateway  # noqa: F401  # 副作用：安装 py3.10 typing 兼容垫片


def pytest_configure(config):
    # Phase 1：真实付费/真实飞书事件测试打 paid 标记，裸跑 pytest 时函数体内自行 skip
    # （需显式 SIMPRAXIS_ALLOW_PAID=1 才触网）；不新增 pytest.ini/setup.cfg/pyproject.toml。
    config.addinivalue_line("markers", "paid: 真实付费/真实飞书，需显式批准（SIMPRAXIS_ALLOW_PAID=1）")
