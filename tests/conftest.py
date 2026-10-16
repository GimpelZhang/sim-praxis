"""pytest 收集前引导。

litellm==1.99.4 在 Python 3.10 下的打包疏漏（NotRequired 急切导入，详见
orchestrator/gateway/__init__.py 的锁版本内修垫片）：测试模块在文件顶部直接
`from litellm.exceptions import ...`，早于对 orchestrator.gateway 的导入，
故在 conftest 阶段先导入本包，确保 typing 垫片在任何 litellm 导入前生效。
"""
import os

import pytest

import orchestrator.gateway  # noqa: F401  # 副作用：安装 py3.10 typing 兼容垫片


@pytest.fixture(autouse=True)
def _hermetic_phase2_quota_root(tmp_path, monkeypatch):
    """默认把 Phase 2 台账根重定向到本测试专属临时目录（全仓 hermetic）。

    为何隔离：Task0 起 quota ledger/批次凭据路径只认 SIMPRAXIS_PHASE2_ROOT
    （orchestrator/common/runtime_paths.py），而 source scripts/00_env.sh 后
    shell 已导出真实账根 /mnt/artifacts/simpraxis/phase2；早期 milestone1
    测试只重定向 SIMPRAXIS_PHASE1_ROOT，若不兜底会把 write_approval 凭据与
    preflight_blocked 审计行写穿真账（2026-10-17 QA 第 1 轮实证：固定
    batch_id 凭据首跑落真账、重跑即「批次凭据已存在，禁止覆盖」）。

    与测试体内 setenv/delenv 的优先级：本夹具与测试函数共用同一个
    function-scoped monkeypatch 实例，夹具先设默认值，测试体内自行
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", ...) 直接覆盖、delenv 则按
    缺省回退语义生效（撤销时 LIFO 统一还原到 shell 原值）。

    付费例外：SIMPRAXIS_ALLOW_PAID=1 表示显式授权的真实付费批次（test_04
    双守卫），实呼必须落真实账——检测到该变量时夹具完全不动 env。
    """
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") == "1":
        yield
        return
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT",
                       str(tmp_path / "_phase2_hermetic"))
    yield


def pytest_configure(config):
    # Phase 1：真实付费/真实飞书事件测试打 paid 标记，裸跑 pytest 时函数体内自行 skip
    # （需显式 SIMPRAXIS_ALLOW_PAID=1 才触网）；不新增 pytest.ini/setup.cfg/pyproject.toml。
    config.addinivalue_line("markers", "paid: 真实付费/真实飞书，需显式批准（SIMPRAXIS_ALLOW_PAID=1）")
