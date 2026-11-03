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

# Genesis 载体（Phase 2.7 Task 1 Step 0 / scene_build 探针；L-13 单一源）。
#
# 为什么不是 AGENTIC_IMAGE：它是 D-3「零 wheel」形态（依赖全来自只读挂载的
# /opt/venv），系统解释器与它同源的 BASE_IMAGE 都没有 genesis——2026-11-03
# 实测 `ModuleNotFoundError: No module named 'genesis'`
# （$SIMPRAXIS_PHASE27_ROOT/task1_asset_zoo/probe_interpreter_check.log）。
# 宿主 import genesis 会段错误（A-06），所以既不能改宿主 venv，也不能靠挂载解决。
# 本机已存在、依赖逐项满足（genesis-world 1.3.3 / trimesh 5.1.1 / coacd 1.0.14 /
# numpy 1.26.4 / torch 2.4.1+cu124，装在镜像内 /usr/bin/python）的载体是
# Phase 1 Task 8 的流体沙盒镜像；计划 T1.4-E 的处置正是「不一致就改这条 argv，
# 不得改镜像」——故只换 argv 指向的镜像，不重建任何镜像、不动 AGENTIC_IMAGE。
SCENE_BUILD_IMAGE = (
    "sim-praxis/sandbox:phase1-task8@sha256:"
    "78be93b970e47d18283a275e1eef15856fcd1ebf607c315bfca45299f3a0d3d7")

# 该镜像里 Genesis 装在系统解释器上（不是 /opt/venv）。
SCENE_BUILD_PYTHON = "/usr/bin/python"

SMOLAGENTS_PIN = "1.26.0"
