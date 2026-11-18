"""合成探针：验证 conftest 的 container 收集期 deselect。"""
import pytest


@pytest.mark.container
def test_container_only():
    assert True


def test_plain():
    assert True
