"""docker_integration 测试开关（Phase 2.5 Task4 返修 M2/M3/M4）。

与 kb_integration 同构（见 tests/kb_support.py 与 tests/conftest.py
pytest_configure）：默认裸跑 pytest 时，真起兄弟容器的测试一律 skip，
不是 test.skip 放水、不弱化任何断言；只有显式
SIMPRAXIS_DOCKER_INTEGRATION=1 且本机 docker daemon 可用时才执行。

硬门（返修单）：Task 5 首次付费调用前，M2/M3/M4 + 一个 run_cmd echo
烟测必须在本机真跑录证；跑后零 agentic 容器残留、显存 <=200 MiB。

用法：
    @pytest.mark.docker_integration
    def test_x():
        require_docker_integration()
        ...
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

ENV_FLAG = "SIMPRAXIS_DOCKER_INTEGRATION"


def require_docker_integration() -> None:
    """真 docker 前置；不满足即 skip（与 kb_integration 同构，非放水）。"""
    if os.environ.get(ENV_FLAG) != "1":
        pytest.skip(f"需显式 {ENV_FLAG}=1（真起兄弟容器，默认 skip）")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI 不在 PATH")
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("docker daemon 不可达")
    if proc.returncode != 0:
        pytest.skip("docker daemon 不可达")
