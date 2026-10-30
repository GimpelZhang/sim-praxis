"""run_cmd 兄弟容器镜像 digest pin（AC-T4-18 单一源，M4 返修）。

本件在 Task 4 交付的是**常量与证据契约**：图接线（Task 5）构造
DockerSiblingBackend 时只能引用本常量，不得在业务码另写镜像 tag/digest。
当前 graph 生产路径尚未装配 agentic 分派（fail-closed/未组装），本常量
在 Task 4 内只被测试与构建证据消费。digest 由
scripts/phase25/build_agentic_image.sh 在 SIMPRAXIS_USE_PROXY=0、
``docker build --network=none --pull=false`` 下构建并写入证据
sandbox/docker/agentic_build_evidence.json；test_task4_image_pin.py
逐字段对账常量/Dockerfile/wheel manifest/证据四方一致，漂移即红。

引用形态统一为 docker 可解析的 ``tag@sha256:<64hex>``：``tag@<裸hex>``
是 invalid reference（2026-10-30 评审 M4 宿主侧实证 docker rc125）。

D-3 主 venv 形态：镜像内零 wheel，运行期解释器与依赖全部来自只读挂载
/opt/venv（见 agentic_wheel_manifest.json 的零项理由）。
"""
from __future__ import annotations

# 产物镜像（tag@sha256:<image_id hex>；与证据 image_ref_digest 同形）。
AGENTIC_IMAGE = (
    "sim-praxis/agentic:phase25-t4@sha256:"
    "abf96496a6d72cf4f61c37a3cfe7d657013c1a567fcc88b04ac2b616d012fed7")

# 基础镜像 digest（与 sandbox/docker/Dockerfile.agentic FROM 逐字一致）。
BASE_IMAGE = (
    "sim-praxis/sandbox@sha256:"
    "50b6e10df854db40ea41e33f3ed9731a9d5d35b2d9d2ccf9b9f178d8565e4bfe")

SMOLAGENTS_PIN = "1.26.0"
