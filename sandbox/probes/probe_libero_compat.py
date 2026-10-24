#!/usr/bin/env python3
"""Phase 2 Task7 B 线 Stage0：LIBERO/robosuite 与共享 mujoco 3.15.0 兼容探针。

计划锚点：Phase_2_Plan_detailed.md Task7（L580-590，B 线 Stage0）、D-4（L119）、
附录 B Task7（L981，产物名 stage0_compat_report.json）。

硬规则边界：
  * 硬规则 10：A 线 tour 只做结构化克隆（/mnt/assets/simpraxis/tours/LIBERO_tour），
    本探针是 B 线容器内实跑；除把 tour 实体复制到 runs 区可写副本并对副本做
    ``pip install -e``（写 egg-info）外，不执行 tour 任何脚本/notebook，不构造
    任何仿真环境（只做 import 链）。
  * 硬规则 8 / D-4：宿主与共享 venv（/mnt/venvs/simpraxis）pin 一律不动。探针只在
    隔离 venv（VENV_PATH）内安装；robosuite 若拉入自有 mujoco，允许其只存在于隔离
    venv 内（pin_facts 落事实），numpy/torch 若被移动则判隔离失败（共享 torch 的
    ABI 前提被破坏）。
  * 锁内适配（Task7 B-line Stage0，2026-10-24 容器实证；规则 7 留痕）：镜像基线
    /usr/bin/python3.10 缺 ensurepip、shared venv 内也无 virtualenv，``python -m venv``
    直接失败；且 stdlib ``--system-site-packages`` 只暴露 base /usr 的包，不挂
    /mnt/venvs/simpraxis 的 site-packages（宿主/容器双向实证）。故隔离机制改为：
    ``venv --without-pip --system-site-packages``（不调 ensurepip）→ 双路 sysconfig
    动态取 purelib（禁硬编码 python3.10 段）→ 在子 venv 自己的 purelib 写单行
    simpraxis_shared.pth 指向共享 purelib（只读复用，不写共享树）→ 验证子解释器
    import numpy/torch 且 ``-m pip`` 可自发现（pip 模块本身经 .pth 从共享加载；
    2026-10-24 宿主临时 venv 实证 .pth-only 成立：numpy 1.26.4 / torch 2.4.1+cu124 /
    pip 26.2.1 均无需 PYTHONPATH）。pip argv 一律 ``<child>/bin/python -m pip``。
    这是机制适配，不是 D-4 兼容性结论。
  * 锁内适配（轮次 4，Task7 Stage0，2026-10-24 A800 容器实证；规则 7 留痕）：
    轮次 3 终版实证 venv 引导成功、共享 mujoco 3.15.0 pin 全 true
    （mujoco_diverged=false，D-4 担心的 mujoco 冲突没有发生），失败点经逐字
    核账是探针打包机制而非共享 pin 冲突——(1) robosuite-1.4.0-py3-none-any.whl
    （193.5MB）无 Requires-Dist（pip 对它零收集依赖），termcolor/future 两件纯
    Python 叶子依赖不会被自动安装（import 实测 ModuleNotFoundError）；(2)
    /mnt/assets 在 sandbox/docker-compose.yml L81 以 roro 只读挂载，LIBERO
    旧式 setuptools setup.py 的 editable egg_info 必须在源码树内写
    libero.egg-info，pip 逐字报错 ``error: could not create 'libero.egg-info':
    Read-only file system``。故安装步由 3 步扩为 5 步：venv_create →
    robosuite_bddl → isolated_leaf_deps（隔离 venv 内只补 termcolor/future，
    不触共享 pin）→ libero_src_copy（shutil.copytree 实体化复制到 runs 区可写
    副本）→ libero_editable（-e 只许指向可写副本，回装只读 /mnt/assets 由
    validate_report 回归守卫拒绝）。这是打包机制适配，不是 D-4 兼容性结论。
  * 锁内适配（轮次 5，Task7 Stage0，2026-10-24 06:12Z A800 容器实证 + git
    取证；规则 7 留痕）：轮次 4 五步全 rc=0 后 import libero.libero 仍逐字失败
    ``ModuleNotFoundError: No module named 'libero'``（同机 numpy 1.26.4 /
    torch 2.4.1+cu124 / mujoco 3.15.0 / robosuite 1.4.0 / bddl 1.0.1 全 ok，
    非 GL/ABI/mujoco 冲突）。根因是 upstream 打包缺陷：tour pin 在
    8f1084e3132a39270c3a13ebe37270a43ece2a01，git cat-file/ls-files 实证独缺
    顶层 libero/__init__.py，setup.py find_packages() 发现 0 包，editable
    finder MAPPING={}/NAMESPACES={}、top_level.txt 空。安装步再扩为 6 步：
    libero_src_copy 之后、libero_editable 之前插 libero_pkg_init_shim——仅在
    可写副本 libero/ 下原子补空 __init__.py marker（LIBERO 社区对该 upstream
    缺陷的 canonical 补法；原 /mnt/assets 只读树绝不触碰；marker 已存在且非空
    时 fail-closed 不覆盖，等 upstream 修复后本步应响报错）。outcome 仍由
    decide_outcome 单点推导，容器重跑若现真 GL/ABI 冲突诚实保持 conflict。
  * 宿主零安装：除容器内执行的 main()/execute_probe 经 SUBPROCESS 在隔离 venv 安装
    外，本模块任何函数都不向宿主装包；import 本模块只依赖标准库（宿主编排/单测可
    直接导入；测试在 tmp_path 真建 --without-pip 子 venv 做零成本 smoke）。

产物 schema：``simpraxis.task7.stage0_compat.v1``（validate_report 单一校验入口）。
两种结局都是合法 Stage0 产物（报告即机判输入）：
  compatible                 import 链全通且共享 numpy/torch pin 未移动；
  conflict_isolated_failed   安装失败 / import 链断 / numpy/torch pin 被移动。
mujoco 版本漂移（venv 内解析到非 3.15.0）单独落 pin_facts，不单独判失败——
D-4 明文允许隔离 venv 内使用其要求版本；能否跑通以 import 链事实为准。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 单点常量（禁止在函数里散落字面量；测试对这组常量做静态契约断言）
# ---------------------------------------------------------------------------

REPORT_SCHEMA = "simpraxis.task7.stage0_compat.v1"
REPORT_VERSION = 1

# D-4：隔离 venv 在容器内的路径。Task0 非 root 化（sandbox/docker-compose.yml：
# user 1000:1000、SIMPRAXIS_RUN_DIR=/home/agent/runs，宿主
# /mnt/artifacts/simpraxis/runs 经 bind mount 落同一路径）后，D-4 锁内适配
# 把计划里的 root 时代路径 /root/runs/phase2/venvs/libero 映射到
# /home/agent/runs/phase2/venvs/libero——/root/runs 是 Task0 前的 root 时代
# 旧位，禁止再用（非 root 容器内不可写）。
VENV_PATH = "/home/agent/runs/phase2/venvs/libero"
# 共享解释器：只允许用它「创建」venv；任何安装都不得写它的 site-packages。
BASE_PYTHON = "/mnt/venvs/simpraxis/bin/python"
SYSTEM_SITE_PACKAGES = True

# 共享 pin（硬规则 8；探针只观测、绝不升级/降级）。
SHARED_TORCH = "2.4.1"
SHARED_NUMPY = "1.26.4"
SHARED_MUJOCO = "3.15.0"
# 硬规则 8 的 torch pin 全称是 2.4.1+cu124（CUDA 12.4 构建）：pip 公共说明符
# torch==2.4.1 按 PEP440 忽略 local 段（resolver 侧 +cu124/+cu125 都「满足」），
# 但 D-4 的运行时问题是「子 venv 是否复用共享那一件 torch」——local 段是构建
# 身份，出现非共享 local（如 +cu125）即隔离 venv 自带 torch，照常判 conflict。
SHARED_TORCH_LOCAL = "cu124"

# A 线结构化克隆在容器内的同路径挂载（与 Phase1 probes 的 /mnt 挂载口径一致）。
TOUR_DIR = "/mnt/assets/simpraxis/tours/LIBERO_tour"

# 锁内适配（轮次 4，Task7 Stage0，2026-10-24 A800 容器实证；规则 7 留痕）：
# /mnt/assets 在 sandbox/docker-compose.yml L81 以 roro 只读挂载，LIBERO 旧式
# setuptools setup.py 的 editable egg_info 必须在源码树内写 libero.egg-info，
# 轮次 3 逐字实证 "error: could not create 'libero.egg-info': Read-only file
# system"。故先把 TOUR_DIR 实体复制到 runs 区可写副本，editable 只许指向副本；
# validate_report 对回装只读 /mnt/assets 有回归守卫。
RUNS_WRITABLE_ROOT = "/home/agent/runs"
WRITABLE_TOUR_ROOT = "/home/agent/runs/phase2/task7/libero_src"
WRITABLE_TOUR_COPY = f"{WRITABLE_TOUR_ROOT}/LIBERO_tour"

# 容器构建直连（SIMPRAXIS_USE_PROXY=0），pip 走清华源（硬规则 8）。
INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

# Stage0 最小 import 链 curated set：不装 tour 全量 requirements.txt
# （hydra/transformers/robomimic/wandb 等是 Stage1 训练期依赖，Gate1 批准后
# 在方案内扩展）；robosuite+bddl 两件走最小链（锁内适配轮次 4 实证 robosuite
# wheel 无 Requires-Dist，其缺失叶子依赖由 LEAF_PACKAGES 单独补），LIBERO
# 本体走可写副本的 editable（其 setup.py install_requires=[]，A 线已核实，
# 故 --no-deps）。
MINIMAL_PACKAGES: tuple[str, ...] = ("robosuite==1.4.0", "bddl==1.0.1")

# 锁内适配（轮次 4，2026-10-24 A800 容器实证）：robosuite 1.4.0 wheel 无
# Requires-Dist、bddl 1.0.1 sdist 不声明 future，pip 不会自动装这两件纯
# Python 叶子依赖（import 实测 ModuleNotFoundError: termcolor / future）；
# isolated_leaf_deps 步只许补装这两件，validate_report 白名单死钉（防借该步
# 偷渡 numpy/torch/mujoco 等共享件）。
LEAF_PACKAGES: tuple[str, ...] = ("termcolor", "future")

# 安装步骤 target 白名单（锁内适配轮次 4：3 步扩为 5 步；轮次 5 再插
# libero_pkg_init_shim 成 6 步——恰为下列、按序、各一次；validate_report
# 单点校验，上游断裂占位也必须补成这 6 步全形）。
EXPECTED_STEP_TARGETS: tuple[str, ...] = (
    "venv_create", "robosuite_bddl", "isolated_leaf_deps",
    "libero_src_copy", "libero_pkg_init_shim", "libero_editable",
)

# 定向查询的分发清单（importlib.metadata.version，不做全量 freeze 粘贴）。
TARGET_DISTRIBUTIONS: tuple[str, ...] = (
    "numpy", "torch", "mujoco", "mujoco-py",
    "robosuite", "bddl", "libero",
)

# import 链：前三个是共享 pin 事实，后三个是 B 线目标能力。
# 每项 (模块名, 探针代码)；版本一律 print 到 stdout 最后一行。
_IMPORT_SNIPPETS: dict[str, str] = {
    "numpy": "import numpy; print(numpy.__version__)",
    "torch": "import torch; print(torch.__version__)",
    "mujoco": "import mujoco; print(mujoco.__version__)",
    "robosuite": "import robosuite; print(robosuite.__version__)",
    "bddl": "import bddl; print(getattr(bddl, '__version__', 'unknown'))",
    # 上游代码实际用的是 libero.libero.*（见 libero/libero/envs/bddl_base_domain.py）；
    # 外层 libero/ 无 __init__.py（A 线结构事实），靠 editable 把仓库根挂上 path
    # 后按命名空间包解析——能否解析正是 Stage0 要取得的事实之一。
    "libero": "import libero.libero; print('libero-import-ok')",
}

# 保护「--system-site-packages 复用共享件」前提的约束：只钉 numpy/torch。
# 刻意不钉 mujoco：D-4 允许隔离 venv 内自带 robosuite 要求的 mujoco 版本，
# 钉死会把真实冲突在 resolver 阶段提前掩盖，违背 Stage0「取事实」的目的。
# torch==2.4.1 按 PEP440 公共版本说明符匹配时自动忽略候选 local 段（实测共享
# torch 逐字为 2.4.1+cu124，pip 侧本就满足，无需改写约束；见 _release_version）。
CONSTRAINTS_TEXT = (
    "# Task7 Stage0 隔离约束（探针生成，勿手改）：只保护共享复用件。\n"
    f"numpy=={SHARED_NUMPY}\n"
    f"torch=={SHARED_TORCH}\n"
)

OUTCOMES = ("compatible", "conflict_isolated_failed")
DEGRADATION = "structure_tour_plus_compat_whitepaper"

# 锁内适配（轮次 4 三条 + 轮次 5 一条，2026-10-24 A800 容器实证）：随每份
# Stage0 报告留证的机制实证（notes 是结构化文字证据；termcolor/future 版本不进
# installed 七键集，另由 pip_isolated_leaf_deps.log 资产留证）。
ADAPTATION_NOTES: tuple[str, ...] = (
    "锁内适配（Task7 Stage0 轮次 4，2026-10-24 容器实证）：robosuite==1.4.0 "
    "wheel（robosuite-1.4.0-py3-none-any.whl，193.5MB）pip 零收集依赖——该 "
    "wheel 无 Requires-Dist，termcolor 等叶子依赖不会被自动安装。",
    "锁内适配（Task7 Stage0 轮次 4，2026-10-24 容器实证）：/mnt/assets 在 "
    "sandbox/docker-compose.yml L81 以 roro 只读挂载，LIBERO 旧式 setuptools "
    "setup.py 的 editable egg_info 必须在源码树内写 libero.egg-info，pip "
    "逐字报错 \"error: could not create 'libero.egg-info': Read-only file "
    'system"；故先 shutil.copytree 实体复制到 runs 区可写副本再对副本 -e。',
    "锁内适配（Task7 Stage0 轮次 4，2026-10-24 容器实证）：import 链实测 "
    "ModuleNotFoundError: No module named 'termcolor'（robosuite 缺）与 "
    "No module named 'future'（bddl 缺）；二者是纯 Python 叶子依赖，与 "
    "numpy/torch/mujoco 无版本交集，已在隔离 venv 内补装这两件（不触共享 "
    "pin，mujoco 始终共享 3.15.0，pin_facts 全 true）。",
    "锁内适配（Task7 Stage0 轮次 5，2026-10-24 06:12Z 容器实证 + git 取证）："
    "轮次 4 五步全 rc=0 后 import libero.libero 仍逐字失败 "
    "\"ModuleNotFoundError: No module named 'libero'\"（同机 numpy 1.26.4 / "
    "torch 2.4.1+cu124 / mujoco 3.15.0 / robosuite 1.4.0 / bddl 1.0.1 全 ok，"
    "非 GL/ABI/mujoco 冲突）。根因是 upstream 打包缺陷：tour pin 在 "
    "8f1084e3132a39270c3a13ebe37270a43ece2a01（非 sparse、工作区 clean），"
    "git cat-file/ls-files 实证独缺顶层 libero/__init__.py，setup.py "
    "find_packages() 发现 0 包，editable finder MAPPING={}/NAMESPACES={}、"
    "egg-info/top_level.txt 空。仅在可写副本 libero/ 下原子补空 "
    "__init__.py marker（LIBERO 社区 canonical 补法；原 /mnt/assets 只读树"
    "绝不触碰；marker 已存在且非空即非零 rc fail-closed 不覆盖）。宿主零成本"
    "旁证：sys.path 预置 LIBERO_CONFIG_PATH/config.yaml 后 import "
    "libero.libero 成功、get_libero_path 可用，四 pin 原样保持。",
)

# 子 venv 内 .pth 文件名（单行内容 = 共享 purelib 路径；解释器启动时 site.py 处理）。
SHARED_PTH_NAME = "simpraxis_shared.pth"

# 子进程看门狗（容器内清华源装 robosuite+bddl 给足余量）。
VENV_CREATE_TIMEOUT_S = 300
PIP_TIMEOUT_S = 1800
IMPORT_TIMEOUT_S = 120
# 锁内适配（轮次 4）：tour 树实体复制到 runs 区可写副本的看门狗（本地复制）。
COPY_TOUR_TIMEOUT_S = 600
# pip 全量日志落文件，报告内只留尾部窗口（完整 verbatim 在 log_path）。
PIP_TAIL_CHARS = 4000

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _release_version(version: "str | None") -> "str | None":
    """PEP440 release 段归一化（锁内适配 2026-10-24，+cu124 local 段实证）：
    共享 torch 实测版本逐字为 '2.4.1+cu124'（宿主/容器 importlib.metadata 与
    torch.__version__ 双实证），而 SHARED_TORCH pin 是公共 release '2.4.1'。
    PEP440 规定公共版本说明符匹配时忽略候选的 local 段，故 pin 比较一律先截
    '+' 之后部分；只做这一件事，不引 packaging。None 透传，原值仍逐字进报告。"""
    if version is None:
        return None
    return version.split("+", 1)[0].strip()


def _local_segment(version: "str | None") -> str:
    """PEP440 local 段（'+' 之后；无 local 段返回 ''；None 安全）。"""
    if not isinstance(version, str) or "+" not in version:
        return ""
    return version.split("+", 1)[1].strip()


class Stage0ReportError(ValueError):
    """Stage0 报告缺失/手工伪造/结构违规（图接缝据此 fail-closed）。"""


# 锁内适配（轮次 4）：pip argv 带值选项表（仅服务校验器静态抽取安装目标，
# 刻意不做完整 pip 语义复刻）。
_PIP_OPTS_WITH_VALUE = frozenset({
    "-i", "--index-url", "--extra-index-url",
    "-c", "--constraint", "-r", "--requirement",
    "-f", "--find-links",
})


def _pip_install_spec_names(argv: list[str]) -> "list[str] | None":
    """从 ``<py> -m pip install [选项] <spec>...`` argv 静态抽取安装目标分发名
    （小写、按出现顺序）。吃掉带值选项与开关项，剩余 token 截到版本说明符前
    的分发名（termcolor / future==1.0 / pkg[extra] 等）。argv 无 install
    子命令返回 None。仅用于 validate_report 的步骤白名单校验。"""
    try:
        at = argv.index("install")
    except ValueError:
        return None
    names: list[str] = []
    j = at + 1
    while j < len(argv):
        tok = argv[j]
        if tok in _PIP_OPTS_WITH_VALUE:
            j += 2
            continue
        if tok.startswith("-"):
            j += 1
            continue
        names.append(re.split(r"[<>=!~\[;@\s]", tok, 1)[0].strip().lower())
        j += 1
    return names


# ---------------------------------------------------------------------------
# 纯函数：命令构造（宿主机可单测；任何安装目标都在隔离 venv 绝对路径下）
# ---------------------------------------------------------------------------

def venv_python(venv_path: str = VENV_PATH) -> str:
    return str(Path(venv_path) / "bin" / "python")


def venv_create_cmd(venv_path: str = VENV_PATH,
                    base_python: str = BASE_PYTHON) -> list[str]:
    """建 venv 命令（锁内适配 2026-10-24）：必须带 --without-pip——镜像基线缺
    ensurepip，裸 ``-m venv`` rc=1。子 venv 的 pip 随后经 simpraxis_shared.pth
    从共享 site-packages 只读发现，以 ``<child>/bin/python -m pip`` 调用。
    唯一允许触碰共享解释器的一步：只用它创建布局，不向共享树装包。"""
    return [base_python, "-m", "venv", "--without-pip",
            "--system-site-packages", venv_path]


def purelib_query_cmd(python: str) -> list[str]:
    """取某解释器视角的 purelib（创建方/子 venv 各查一次；禁硬编码版本段）。"""
    return [python, "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])"]


def shared_pth_text(shared_purelib: "str | Path") -> str:
    """simpraxis_shared.pth 文件内容：单行共享 purelib（site.py 启动时追加）。"""
    return f"{shared_purelib}\n"


def child_pth_path(child_purelib: "str | Path") -> Path:
    """子 venv 自己的 purelib 内的 .pth 目标路径（只在子 venv 内，不碰共享树）。"""
    return Path(child_purelib) / SHARED_PTH_NAME


def verify_shared_imports_cmd(child_python: str) -> list[str]:
    """引导自检：子解释器经 .pth 只读复用共享 numpy/torch（版本逐字进日志）。"""
    return [child_python, "-c",
            "import numpy, torch; "
            "print(numpy.__version__); print(torch.__version__)"]


def pip_version_cmd(child_python: str) -> list[str]:
    """引导自检：子解释器不经 PYTHONPATH 即可 -m pip（pip 模块经 .pth 发现）。"""
    return [child_python, "-m", "pip", "--version"]


def pip_install_chain_cmd(venv_path: str, constraints_file: "str | Path",
                          index_url: str = INDEX_URL) -> list[str]:
    """robosuite/bddl 最小链安装；-c 只保护 numpy/torch 共享 pin。
    pip 一律 ``<child>/bin/python -m pip``：--without-pip venv 没有 bin/pip。"""
    return [venv_python(venv_path), "-m", "pip", "install", "-i", index_url,
            "-c", str(constraints_file), *MINIMAL_PACKAGES]


def pip_install_leaf_deps_cmd(venv_path: str, constraints_file: "str | Path",
                              index_url: str = INDEX_URL) -> list[str]:
    """锁内适配（轮次 4，2026-10-24 容器实证）：隔离 venv 内只补装
    termcolor/future 两件实证缺失的纯 Python 叶子依赖（LEAF_PACKAGES 白名单
    死钉，validate_report 拒任何他件/共享件偷渡）；-c 沿用只保护 numpy/torch
    的同一约束，不触共享 pin。pip 一律 ``<child>/bin/python -m pip``。"""
    return [venv_python(venv_path), "-m", "pip", "install", "-i", index_url,
            "-c", str(constraints_file), *LEAF_PACKAGES]


def copy_tour_src_cmd(venv_path: str, src_dir: "str | Path",
                      dst_dir: "str | Path") -> list[str]:
    """锁内适配（轮次 4，2026-10-24 容器实证）：/mnt/assets roro 只读挂载，
    LIBERO 旧式 setuptools 的 editable egg_info 必须在源码树内写
    libero.egg-info（轮次 3 逐字 Read-only file system）。用隔离 venv 解释器
    跑 ``python -c`` shutil.copytree，把 src_dir（TOUR_DIR 字面进代码）原样
    实体复制（symlinks=False）到 runs 区可写副本 dst_dir；只做文件复制、不
    执行 tour 本体（硬规则 10）。目标已存在时 copytree 自然 FileExistsError
    （rc 保留、不覆盖）；src/dst 由 print 逐字进 tail_verbatim。"""
    code = (
        "import shutil\n"
        f"src = {str(src_dir)!r}\n"
        f"dst = {str(dst_dir)!r}\n"
        "print('src=' + src)\n"
        "print('dst=' + dst)\n"
        "shutil.copytree(src, dst, symlinks=False)\n"
        "print('copied ok')\n"
    )
    return [venv_python(venv_path), "-c", code]


def libero_pkg_init_shim_cmd(venv_python: str,
                             writable_tour_copy: "str | Path") -> list[str]:
    """锁内适配（轮次 5，2026-10-24 06:12Z 容器实证 + git 取证）：upstream
    tour pin 8f1084e3 独缺顶层 libero/__init__.py，find_packages() 发现 0 包，
    editable finder MAPPING={}/NAMESPACES={}、top_level.txt 空，import
    libero.libero 必然 ModuleNotFoundError。本步用隔离 venv 解释器跑 ``-c``，
    只在**可写副本** libero/ 下原子补空 __init__.py marker（canonical 社区补法）：
      * realpath 守卫：解析后 root/target 落任何只读/assets 挂载即非零退出，
        target 逃出副本（root 之外）即非零退出（禁字面 /mnt/assets——路径片段
        由 os.sep + 'mnt' + os.sep + 'assets' 拼出，validate 静态白名单）；
      * target 已存在且为空串：print no-op，绝不覆写；
      * target 已存在且非空：非零 rc 拒绝（upstream 未来补了 marker 时
        fail-closed，探针不再替它写）；
      * 不存在：mkstemp+fsync+os.replace 原子写空串，chmod 0o644。
    禁止 dirs_exist_ok、禁止 shutil.rmtree/copytree、禁止写任何其他路径；
    原 /mnt/assets 树只读不触碰。validate_report 对 -c 代码逐段静态校验。"""
    copy_path = str(writable_tour_copy)
    code = (
        "import os\n"
        "import tempfile\n"
        "\n"
        f"copy = {copy_path!r}\n"
        "# 禁字面拼只读挂载：片段组装，realpath 后仍命中即拒绝。\n"
        "forbidden = os.sep + 'mnt' + os.sep + 'assets'\n"
        "root = os.path.realpath(copy)\n"
        'target = os.path.realpath(os.path.join(copy, "libero",'
        ' "__init__.py"))\n'
        "if forbidden in root or forbidden in target:\n"
        "    raise SystemExit('shim refuse: resolved path under read-only "
        "assets mount')\n"
        "if target != root and not target.startswith(root + os.sep):\n"
        "    raise SystemExit('shim refuse: target escapes writable copy')\n"
        "parent = os.path.dirname(target)\n"
        "if not os.path.isdir(parent):\n"
        "    raise SystemExit('shim refuse: parent dir missing "
        "(libero_src_copy failed?)')\n"
        "if os.path.exists(target):\n"
        "    with open(target, 'r', encoding='utf-8') as handle:\n"
        "        existing = handle.read()\n"
        "    if existing == '':\n"
        "        print('shim no-op: empty libero/__init__.py marker already "
        "present')\n"
        "    else:\n"
        "        raise SystemExit('shim refuse: libero/__init__.py non-empty "
        "(upstream added marker?)')\n"
        "else:\n"
        "    fd, tmp = tempfile.mkstemp(prefix='.__init__shim.', dir=parent)\n"
        "    try:\n"
        "        with os.fdopen(fd, 'w', encoding='utf-8') as handle:\n"
        "            handle.write('')\n"
        "            handle.flush()\n"
        "            os.fsync(handle.fileno())\n"
        "        os.chmod(tmp, 0o644)\n"
        "        os.replace(tmp, target)\n"
        "    except BaseException:\n"
        "        try:\n"
        "            os.unlink(tmp)\n"
        "        except OSError:\n"
        "            pass\n"
        "        raise\n"
        "    print('shim created: empty libero/__init__.py marker in writable "
        "copy')\n"
    )
    return [venv_python, "-c", code]


def pip_install_libero_cmd(venv_path: str, tour_dir: "str | Path",
                           index_url: str = INDEX_URL) -> list[str]:
    """LIBERO editable 安装：install_requires=[]（A 线 setup.py 已核实）→
    --no-deps，依赖面由最小链/叶子依赖命令单点负责，避免 setuptools 顺带拉取。
    锁内适配（轮次 4）：生产路径 tour_dir 必须传 WRITABLE_TOUR_COPY（runs 区
    可写副本），回装只读 /mnt/assets 由 validate_report 回归守卫拒绝；构造器
    本身保持路径参数通用（单测可指 tmp_path）。"""
    return [venv_python(venv_path), "-m", "pip", "install", "-i", index_url,
            "--no-deps", "-e", str(tour_dir)]


def metadata_query_cmd(venv_path: str = VENV_PATH) -> list[str]:
    """venv 内定向分发版本查询（JSON 单行）；缺失分发记 null。"""
    names = ", ".join(repr(n) for n in TARGET_DISTRIBUTIONS)
    code = (
        "import importlib.metadata as m, json\n"
        f"names = [{names}]\n"
        "out = {}\n"
        "for n in names:\n"
        "    try:\n"
        "        out[n] = m.version(n)\n"
        "    except m.PackageNotFoundError:\n"
        "        out[n] = None\n"
        "print(json.dumps(out, sort_keys=True))"
    )
    return [venv_python(venv_path), "-c", code]


def import_check_cmd(module: str, venv_path: str = VENV_PATH) -> list[str]:
    """venv 内单模块 import 探针命令（每模块独立子进程，失败互不遮蔽）。"""
    return [venv_python(venv_path), "-c", _IMPORT_SNIPPETS[module]]


# ---------------------------------------------------------------------------
# 纯函数：结果归纳与兼容判定（不预断结论，只按事实矩阵机判）
# ---------------------------------------------------------------------------

def summarize_import(module: str, rc: int, stdout: str,
                     stderr: str) -> dict:
    """单子进程结果 → import_chain 条目；error_verbatim 逐字保留。"""
    ok = rc == 0
    version = stdout.strip().splitlines()[-1].strip() if stdout.strip() else None
    return {
        "module": module,
        "ok": ok,
        "rc": int(rc),
        "version": version if ok else None,
        "stdout_verbatim": stdout,
        "stderr_verbatim": stderr,
        # 失败时必须有非空原文（空字符串失败件 = 伪造，validate 拒绝）。
        "error_verbatim": (stderr if not ok else ""),
    }


def parse_metadata_versions(stdout: str) -> dict:
    """解析 metadata_query_cmd 的 JSON 单行；解析失败直接抛（不做静默兜底）。"""
    return json.loads(stdout.strip().splitlines()[-1])


def _shared_pins_from_chain(import_chain: list[dict]) -> dict:
    versions = {e["module"]: e.get("version") for e in import_chain}
    return {
        "numpy": versions.get("numpy"),
        "torch": versions.get("torch"),
        "mujoco": versions.get("mujoco"),
    }


def decide_outcome(install_steps: list[dict], import_chain: list[dict],
                   shared_pins: "dict | None" = None) -> tuple[str, dict, list[str], list[str]]:
    """机判规则（返回 outcome, pin_facts, reasons, notes）：

    判 conflict_isolated_failed（→ D-4 STOP/降级，不提 Gate1 运行案）：
      ① 任一安装步骤 rc != 0（锁内适配轮次 4 起含 libero_src_copy 复制步、
         轮次 5 起含 libero_pkg_init_shim marker 步）；
      ② import 链任一模块 ok=False；
      ③ venv 内 numpy 运行时版本不是共享 1.26.4（共享 torch 2.4.1 按该
        ABI 编译，numpy 被强降即 --system-site-packages 复用前提破坏）；
      ④ venv 内 torch 运行时版本不是共享 2.4.1+cu124（release 段必须
        2.4.1，local 段只许缺席或 cu124；异 local 的 2.4.1+cu125 是子
        venv 自带的另一件 torch，同前提，隔离 venv 不得自有 torch）。
    不判失败、只落事实：
      · mujoco 解析到非 3.15.0（pin_facts.mujoco_diverged=True；D-4 允许
        隔离 venv 内自带版本；import 链通即 compatible，事实进 Gate1 卡与
        兼容性白皮书，不在这里替实验下结论）。
    """
    reasons: list[str] = []
    notes: list[str] = []

    for step in install_steps:
        if step.get("rc") != 0:
            reasons.append(
                f"安装步骤 {step.get('target', '?')} 失败 rc={step.get('rc')}，"
                f"原文见 {step.get('log_path', '?')}")

    by_module = {e["module"]: e for e in import_chain}
    for module in _IMPORT_SNIPPETS:
        entry = by_module.get(module)
        if entry is None:
            reasons.append(f"import 链缺条目：{module}")
        elif not entry.get("ok"):
            first_line = (entry.get("error_verbatim") or "").strip().splitlines()
            reasons.append(
                f"import {module} 失败：{first_line[0][:200] if first_line else '（无原文）'}")

    if shared_pins is None:
        shared_pins = _shared_pins_from_chain(import_chain)

    # 锁内适配（2026-10-24，PEP440 +cu124 local 段实证）：比较走 release 段
    # 归一化；reasons/notes 与报告字段保留逐字原值（shared_pins 不做改写）。
    numpy_ver = shared_pins.get("numpy")
    torch_ver = shared_pins.get("torch")
    numpy_ok = _release_version(numpy_ver) == SHARED_NUMPY
    # torch 除 release 段外还查 local 构建身份：共享全称 2.4.1+cu124（硬规则 8）。
    # 无 local 段（'2.4.1'，合成件口径）或 local==cu124 才算共享那一件；
    # '2.4.1+cu125' 这类同 release 异 local 是子 venv 自带的另一件 torch。
    torch_ok = (_release_version(torch_ver) == SHARED_TORCH
                and _local_segment(torch_ver) in ("", SHARED_TORCH_LOCAL))
    mujoco_ver = shared_pins.get("mujoco")
    mujoco_shared = _release_version(mujoco_ver) == SHARED_MUJOCO
    mujoco_diverged = mujoco_ver is not None and not mujoco_shared
    if not numpy_ok:
        reasons.append(
            f"隔离 venv 内 numpy={numpy_ver!r}，"
            f"必须保持共享 {SHARED_NUMPY}（共享 torch ABI 前提）")
    if not torch_ok:
        local = _local_segment(torch_ver)
        if (_release_version(torch_ver) == SHARED_TORCH and local
                and local != SHARED_TORCH_LOCAL):
            reasons.append(
                f"隔离 venv 内 torch={torch_ver!r}：release 段虽为 {SHARED_TORCH}，"
                f"local 段 {local!r} 不是共享构建 {SHARED_TORCH_LOCAL!r}"
                f"（共享全称 {SHARED_TORCH}+{SHARED_TORCH_LOCAL}，硬规则 8）"
                "——隔离 venv 自带 torch，禁止")
        else:
            reasons.append(
                f"隔离 venv 内 torch={torch_ver!r}，"
                f"必须保持共享 {SHARED_TORCH}+{SHARED_TORCH_LOCAL}"
                "（隔离 venv 不得自带 torch）")
    if mujoco_diverged:
        notes.append(
            f"隔离 venv 内 mujoco 解析为 {mujoco_ver}（共享 pin {SHARED_MUJOCO}）："
            "D-4 允许隔离 venv 内自带版本；以 import 链事实为准，不升级共享 mujoco")

    pin_facts = {
        "numpy_is_shared_pin": numpy_ok,
        "torch_is_shared_pin": torch_ok,
        "mujoco_is_shared_pin": mujoco_shared,
        "mujoco_diverged": mujoco_diverged,
    }
    outcome = "compatible" if not reasons else "conflict_isolated_failed"
    return outcome, pin_facts, reasons, notes


# ---------------------------------------------------------------------------
# 报告校验（图接缝与探针共用的唯一 fail-closed 入口；不引第三方 jsonschema）
# ---------------------------------------------------------------------------

def validate_report(obj: object) -> list[str]:
    """返回错误列表（空 = 合法）。手工件/缺字段/枚举错/失败无原文一律拒。"""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["报告顶层必须是对象"]

    required = ["schema", "version", "ok", "outcome", "venv_path", "index_url",
                "tour_dir", "installed", "install_steps", "import_chain",
                "shared_pins_observed", "pin_facts", "reasons", "notes",
                "ts_utc"]
    for k in required:
        if k not in obj:
            errs.append(f"缺必填字段：{k}")
    if errs:
        return errs

    if obj["schema"] != REPORT_SCHEMA:
        errs.append(f"schema 必须是 {REPORT_SCHEMA}")
    if obj["version"] != REPORT_VERSION:
        errs.append(f"version 必须是 {REPORT_VERSION}")
    if not isinstance(obj["ok"], bool):
        errs.append("ok 必须是布尔")
    if obj["outcome"] not in OUTCOMES:
        errs.append(f"outcome 必须是 {OUTCOMES} 之一")
    if isinstance(obj["ok"], bool) and obj["ok"] != (obj["outcome"] == "compatible"):
        errs.append("ok 必须与 outcome 一致（compatible↔True）")
    for k in ("venv_path", "index_url", "tour_dir"):
        if not isinstance(obj[k], str) or not obj[k]:
            errs.append(f"{k} 必须是非空字符串")
    if not _TS_RE.match(obj["ts_utc"] if isinstance(obj["ts_utc"], str) else ""):
        errs.append("ts_utc 必须是严格 UTC Z（YYYY-MM-DDTHH:MM:SSZ）")

    installed = obj["installed"]
    installed_keys_ok = False
    if not isinstance(installed, dict) or not installed:
        errs.append("installed 必须是非空映射")
    else:
        # M3（三签冷读）：installed 键集必须恰为定向查询清单——多报伪造分发
        # 要拒，缺报（少查一件 = 证据不完整）同样要拒，错误同时列 missing/extra。
        missing = sorted(set(TARGET_DISTRIBUTIONS) - set(installed))
        extra = sorted(set(installed) - set(TARGET_DISTRIBUTIONS))
        if not missing and not extra:
            installed_keys_ok = True
        else:
            errs.append(
                "installed 键集必须恰为 TARGET_DISTRIBUTIONS="
                f"{sorted(TARGET_DISTRIBUTIONS)}：缺 {missing}、多 {extra}")
        for name, ver in installed.items():
            if not isinstance(name, str) or not name:
                errs.append("installed 键必须是非空字符串")
            if ver is not None and (not isinstance(ver, str) or not ver):
                errs.append(f"installed.{name} 必须是版本字符串或 null")

    steps = obj["install_steps"]
    steps_shape_ok = isinstance(steps, list) and bool(steps)
    if not steps_shape_ok:
        errs.append("install_steps 必须是非空数组")
    else:
        # 锁内适配（轮次 4）：5 步 target 白名单/顺序校验在循环后单点做。
        seen_targets: list[str] = []
        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                errs.append(f"install_steps[{i}] 必须是对象")
                continue
            if not isinstance(st.get("target"), str) or not st["target"]:
                errs.append(f"install_steps[{i}].target 必须非空")
            else:
                seen_targets.append(st["target"])
            argv_ok = (isinstance(st.get("argv"), list) and st["argv"]
                       and all(isinstance(a, str) for a in st["argv"]))
            if not argv_ok:
                errs.append(f"install_steps[{i}].argv 必须是非空字符串数组")
            else:
                # LOW-3：可执行入口约束——venv_create 只许用共享解释器创建
                # 布局；其余任何安装/查询步骤只许走隔离 venv 的 bin/。
                if st["target"] == "venv_create":
                    if st["argv"][0] != BASE_PYTHON:
                        errs.append(
                            f"install_steps[{i}]（venv_create）argv[0] 必须是 "
                            f"共享解释器 {BASE_PYTHON}，实际 {st['argv'][0]!r}")
                elif not st["argv"][0].startswith(VENV_PATH + "/bin/"):
                    errs.append(
                        f"install_steps[{i}]（{st['target']}）argv[0] 必须位于 "
                        f"隔离 venv {VENV_PATH}/bin/ 下，实际 {st['argv'][0]!r}")

                # 锁内适配（轮次 4，2026-10-24 容器实证）：新三步的语义白名单。
                if st["target"] == "isolated_leaf_deps":
                    leaf_names = _pip_install_spec_names(st["argv"])
                    if leaf_names is None:
                        errs.append(
                            f"install_steps[{i}]（isolated_leaf_deps）必须是 "
                            "pip install 命令")
                    else:
                        if (len(leaf_names) != len(LEAF_PACKAGES)
                                or set(leaf_names) != set(LEAF_PACKAGES)):
                            errs.append(
                                f"install_steps[{i}]（isolated_leaf_deps）锁内"
                                f"适配只许补装 {list(LEAF_PACKAGES)} 两件纯 "
                                f"Python 叶子依赖（2026-10-24 实证缺失件），"
                                f"实际安装目标 {leaf_names}")
                        smuggled = sorted(
                            set(leaf_names)
                            & {"numpy", "torch", "mujoco", "mujoco-py",
                               "robosuite", "bddl", "libero"})
                        if smuggled:
                            errs.append(
                                f"install_steps[{i}]（isolated_leaf_deps）禁止借"
                                f"叶子依赖步偷渡共享/主链分发：{smuggled}")
                elif st["target"] == "libero_src_copy":
                    copy_argv = st["argv"]
                    if len(copy_argv) != 3 or copy_argv[1] != "-c":
                        errs.append(
                            f"install_steps[{i}]（libero_src_copy）锁内适配 argv "
                            "必须恰为 [<venv python>, '-c', <copytree 代码>]")
                    else:
                        code = copy_argv[2]
                        if "shutil.copytree" not in code:
                            errs.append(
                                f"install_steps[{i}]（libero_src_copy）-c 代码"
                                "必须调用 shutil.copytree 把 tour 原样复制到"
                                "可写副本")
                        # 源目录与报告 tour_dir 字段跨字段逐字对账（生产运行
                        # tour_dir 恒为 TOUR_DIR 常量；允许接缝测试用 tmp_path
                        # 占位，但复制代码里的 src 字面必须就是报告声明的源）。
                        tour_src = obj.get("tour_dir")
                        if not (isinstance(tour_src, str) and tour_src
                                and tour_src in code):
                            errs.append(
                                f"install_steps[{i}]（libero_src_copy）-c 代码"
                                f"必须含源目录字面 {tour_src!r}（与报告 "
                                "tour_dir/TOUR_DIR 字段逐字一致）")
                        m = re.search(r"dst\s*=\s*['\"]([^'\"]+)['\"]", code)
                        if not m:
                            errs.append(
                                f"install_steps[{i}]（libero_src_copy）-c 代码"
                                "必须以字面量显式赋值 dst 目标路径")
                        else:
                            dst = m.group(1)
                            if not dst.startswith(RUNS_WRITABLE_ROOT + "/"):
                                errs.append(
                                    f"install_steps[{i}]（libero_src_copy）副本"
                                    f"目标 {dst!r} 必须位于 runs 可写根 "
                                    f"{RUNS_WRITABLE_ROOT}/ 下（禁止 runs 根外）")
                            if (isinstance(tour_src, str) and tour_src
                                    and (dst == tour_src
                                         or dst.startswith(tour_src + "/"))):
                                errs.append(
                                    f"install_steps[{i}]（libero_src_copy）副本"
                                    f"目标 {dst!r} 不得等于或位于只读 "
                                    "tour_dir/TOUR_DIR（原地复制无意义且仍在"
                                    "只读挂载内）")
                        if not re.search(r"symlinks\s*=\s*False", code):
                            errs.append(
                                f"install_steps[{i}]（libero_src_copy）copytree "
                                "必须 symlinks=False 实体化（禁止符号链接回"
                                "只读挂载）")
                        if re.search(r"dirs_exist_ok\s*=\s*True", code):
                            errs.append(
                                f"install_steps[{i}]（libero_src_copy）禁止 "
                                "dirs_exist_ok=True（既有副本必须响报错、绝不"
                                "静默覆盖）")
                elif st["target"] == "libero_pkg_init_shim":
                    # 锁内适配（轮次 5，2026-10-24 06:12Z 容器实证）：upstream
                    # 8f1084e 缺顶层 libero/__init__.py 的 marker shim。静态
                    # 白名单死钉——只许在可写副本原子补空 marker，任何越界/
                    # 非空写/删树/回写只读挂载的变异一律 fail-closed。
                    if len(st["argv"]) != 3 or st["argv"][1] != "-c":
                        errs.append(
                            f"install_steps[{i}]（libero_pkg_init_shim）锁内"
                            "适配 argv 必须恰为 [<venv python>, '-c', "
                            "<__init__.py marker 代码>]")
                    else:
                        code = st["argv"][2]
                        mc = re.search(
                            r"copy\s*=\s*(['\"])(.*?)\1", code, re.S)
                        if not mc or mc.group(2) != WRITABLE_TOUR_COPY:
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                "代码必须逐字声明 copy 字面量且只许等于可写"
                                f"副本 {WRITABLE_TOUR_COPY}（禁止改打他径）")
                        # 只读/assets 回归守卫（与 editable 步同口径）：代码
                        # 文本本身就不得出现 /mnt/assets 字面（运行时以片段
                        # 组装的 forbidden 再做一次 realpath 拒绝）。
                        if "/mnt/assets" in code:
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                "锁内适配回归守卫：-c 代码禁止出现只读/assets "
                                "挂载字面（/mnt/assets）——shim 只许写可写副本，"
                                "原 assets 树只读绝不触碰")
                        for banned in ("shutil.rmtree", "shutil.copytree",
                                       "rmtree", "copytree", "dirs_exist_ok"):
                            if banned in code:
                                errs.append(
                                    f"install_steps[{i}]（libero_pkg_init_shim）"
                                    f"-c 代码禁止 {banned}（只许单文件原子空写，"
                                    "禁止删树/复制/静默合并）")
                        if ('os.path.realpath(os.path.join(copy, "libero", '
                                '"__init__.py"))') not in code:
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                'target 必须逐字为 os.path.realpath('
                                'os.path.join(copy, "libero", "__init__.py"))'
                                "——只许补副本内 libero/__init__.py，禁止逃出"
                                "副本改打他径")
                        for tok, desc in (
                                ("os.path.realpath", "realpath 解析守卫"),
                                ("os.sep + 'mnt' + os.sep + 'assets'",
                                 "只读/assets 片段守卫"),
                                ("target.startswith(root + os.sep)",
                                 "target 不得逃出副本根守卫")):
                            if tok not in code:
                                errs.append(
                                    f"install_steps[{i}]（libero_pkg_init_shim）"
                                    f"-c 代码必须含{desc}：{tok}")
                        if not re.search(r"\.write\(\s*['\"]{2}\s*\)", code):
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                "必须以空串字面量原子写空 marker"
                                "（.write('')），禁止写任何内容")
                        if re.search(r"\.write\(\s*f?['\"][^'\"]", code):
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                "禁止把非空内容写入 __init__.py（只许空 marker；"
                                "upstream 修复后非空 marker 分支必须 fail-closed）")
                        for tok in ("mkstemp", "fsync", "os.replace", "0o644"):
                            if tok not in code:
                                errs.append(
                                    f"install_steps[{i}]（libero_pkg_init_shim）"
                                    f"空 marker 必须原子写（{tok}），权限 0o644")
                        if (not re.search(r"existing\s*==\s*['\"]{2}", code)
                                or "no-op" not in code):
                            errs.append(
                                f"install_steps[{i}]（libero_pkg_init_shim）"
                                "marker 已存在时必须读回判定：空串走 no-op "
                                "不覆写，非空非零 rc 拒绝（fail-closed）")
                elif st["target"] == "libero_editable":
                    e_idx = next((n for n, a in enumerate(st["argv"])
                                  if a in ("-e", "--editable")), None)
                    if e_idx is None or e_idx + 1 >= len(st["argv"]):
                        errs.append(
                            f"install_steps[{i}]（libero_editable）必须带 "
                            "-e/--editable 路径参数")
                    else:
                        epath = st["argv"][e_idx + 1]
                        if not epath.startswith(WRITABLE_TOUR_ROOT + "/"):
                            errs.append(
                                f"install_steps[{i}]（libero_editable）-e 路径"
                                f"必须指向 runs 区可写副本根 "
                                f"{WRITABLE_TOUR_ROOT}/ 下，实际 {epath!r}")
                        if (epath == "/mnt/assets"
                                or epath.startswith("/mnt/assets/")):
                            errs.append(
                                f"install_steps[{i}]（libero_editable）锁内适配"
                                f"回归守卫：-e 路径禁止回指只读/assets 挂载"
                                f"（{epath!r}）——旧式 egg_info 在只读 tour 必"
                                "写失败，回装只读件不得伪装 compatible")
            if not isinstance(st.get("rc"), int) or isinstance(st.get("rc"), bool):
                errs.append(f"install_steps[{i}].rc 必须是整数")
            if not isinstance(st.get("log_path"), str) or not st["log_path"]:
                errs.append(f"install_steps[{i}].log_path 必须非空")
            if st.get("rc") != 0 and not st.get("tail_verbatim"):
                errs.append(f"install_steps[{i}] 失败却无 pip 原文尾部")

        # 锁内适配（轮次 4/5）：target 必须恰为 6 步、按序、各一次（缺步/
        # 伪步/乱序/重复同罪）。
        if seen_targets != list(EXPECTED_STEP_TARGETS):
            missing = [t for t in EXPECTED_STEP_TARGETS
                       if t not in seen_targets]
            unknown = [t for t in seen_targets
                       if t not in EXPECTED_STEP_TARGETS]
            errs.append(
                "install_steps 目标必须恰为（按序、各一次）"
                f"{list(EXPECTED_STEP_TARGETS)}：缺 {missing}、"
                f"伪/序乱 {unknown}、实际 {seen_targets}")

    chain = obj["import_chain"]
    seen_modules: set[str] = set()
    chain_shape_ok = isinstance(chain, list) and bool(chain)
    if not chain_shape_ok:
        errs.append("import_chain 必须是非空数组")
    else:
        for i, e in enumerate(chain):
            if not isinstance(e, dict):
                errs.append(f"import_chain[{i}] 必须是对象")
                continue
            mod = e.get("module")
            if mod not in _IMPORT_SNIPPETS:
                errs.append(f"import_chain[{i}].module 非法：{mod!r}")
            elif mod in seen_modules:
                errs.append(f"import_chain 模块重复：{mod}")
            seen_modules.add(mod)
            if not isinstance(e.get("ok"), bool):
                errs.append(f"import_chain[{i}].ok 必须是布尔")
            # 版本列形状（M2 对账的形状前提）：ok=true 必须带非空版本字符串；
            # ok=false 时 summarize_import 恒置 null，非 null 即伪造。
            ver = e.get("version")
            if e.get("ok") is True:
                if not (isinstance(ver, str) and ver):
                    errs.append(
                        f"import_chain[{i}]（{mod}）ok=true 却缺非空 version")
            elif e.get("ok") is False and ver is not None:
                errs.append(
                    f"import_chain[{i}]（{mod}）ok=false 时 version 必须为 null")
            if not isinstance(e.get("rc"), int) or isinstance(e.get("rc"), bool):
                errs.append(f"import_chain[{i}].rc 必须是整数")
            for col in ("stdout_verbatim", "stderr_verbatim", "error_verbatim"):
                if not isinstance(e.get(col), str):
                    errs.append(f"import_chain[{i}].{col} 必须是字符串")
            if not e.get("ok") and not e.get("error_verbatim"):
                errs.append(
                    f"import_chain[{i}]（{mod}）ok=false 却缺失败原文"
                    "（error_verbatim 禁止为空）")
            if e.get("ok") and e.get("error_verbatim"):
                errs.append(
                    f"import_chain[{i}]（{mod}）ok=true 不应带 error_verbatim")
        if seen_modules != set(_IMPORT_SNIPPETS):
            errs.append(
                f"import_chain 模块集必须恰为 {sorted(_IMPORT_SNIPPETS)}")

    pins = obj["shared_pins_observed"]
    pins_shape_ok = (isinstance(pins, dict)
                     and set(pins) == {"numpy", "torch", "mujoco"})
    if not pins_shape_ok:
        errs.append("shared_pins_observed 必须恰含 numpy/torch/mujoco")
    else:
        for k, v in pins.items():
            if v is not None and (not isinstance(v, str) or not v):
                errs.append(f"shared_pins_observed.{k} 必须是版本字符串或 null")

    facts = obj["pin_facts"]
    expect_facts = {"numpy_is_shared_pin", "torch_is_shared_pin",
                    "mujoco_is_shared_pin", "mujoco_diverged"}
    facts_shape_ok = (isinstance(facts, dict) and set(facts) == expect_facts
                      and all(isinstance(v, bool) for v in facts.values()))
    if not isinstance(facts, dict) or set(facts) != expect_facts:
        errs.append(f"pin_facts 必须恰含 {sorted(expect_facts)}")
    elif not all(isinstance(v, bool) for v in facts.values()):
        errs.append("pin_facts 值必须全为布尔")
    elif facts["mujoco_is_shared_pin"] and facts["mujoco_diverged"]:
        # mujoco 缺失时两者可同为 False；唯同 True 不可能（共享且漂移矛盾）。
        errs.append("mujoco_is_shared_pin"
                    " 与 mujoco_diverged 不得同时为真")

    for k in ("reasons", "notes"):
        v = obj[k]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"{k} 必须是字符串数组")

    # 信任边界：结局与 pin_facts 不许口标，必须由记录事实单点派生
    # （decide_outcome 是生产者/校验器共用的唯一机判函数）。只在形状可信时
    # 派生（形状错误已各自登记）；链是否全 ok 是派生的输入而不是派生的
    # 前提——否则全败件会绕过结局反推（fail-open）。
    chain_values_sound = chain_shape_ok and all(
        isinstance(e, dict)
        and (e.get("version") is None or isinstance(e.get("version"), str))
        for e in chain)
    pins_values_sound = pins_shape_ok and all(
        v is None or isinstance(v, str) for v in pins.values())
    facts_derivable = (steps_shape_ok and chain_shape_ok
                       and seen_modules == set(_IMPORT_SNIPPETS)
                       and pins_shape_ok and pins_values_sound
                       and chain_values_sound
                       and isinstance(obj["venv_path"], str)
                       and all(isinstance(e, dict) and isinstance(
                           e.get("ok"), bool) for e in chain))
    if facts_derivable:
        # M2（三签冷读）：pins/installed 与 import_chain 是同一解释器同一
        # sys.path 的不同观测面（生产者 pins 由链逐字派生；installed 由同 venv
        # importlib.metadata 查询），三者互相矛盾即手工伪造件，fail-closed。
        chain_by_module = {e.get("module"): e for e in chain
                           if isinstance(e, dict)}
        for abi_name in ("numpy", "torch", "mujoco"):
            centry = chain_by_module.get(abi_name)
            chain_ver = centry.get("version") if centry else None
            chain_ok = bool(centry) and centry.get("ok") is True
            pin_ver = pins[abi_name]
            # (a) pins 是生产者 _shared_pins_from_chain 对 import_chain 行
            # version 的逐字拷贝（同一观测值，诚实件恒逐字相等，含 torch 的
            # '2.4.1+cu124'），故此处必须逐字对账（None===None 合法：ok=false
            # 行 version=None、pins 派生 None）。release 归一化只用于跨观测面
            # installed↔pins（importlib.metadata 与 runtime 输出格式差异）与
            # decide_outcome 的 pin 判定；若这里也归一化，手构件 chain
            # '2.4.1+cu125' + pins/installed '2.4.1+cu124' 会被洗白成
            # compatible（异 local 换构建伪造口子）。
            if chain_ver != pin_ver:
                errs.append(
                    f"shared_pins_observed.{abi_name}={pin_ver!r} 与 import_chain "
                    f"实测版本 {chain_ver!r} 矛盾（pins 必须由链逐字派生）")
            # (b) 只对账 ABI 三件；链 ok=true 时同 venv metadata 必须非空且
            # release 段一致（installed=null 与 import 成功矛盾）；ok=false 时
            # installed 任值不拒（装了但 import 挂是合法证据）。
            if installed_keys_ok and chain_ok:
                inst_ver = installed.get(abi_name)
                if not (isinstance(inst_ver, str) and inst_ver
                        and _release_version(inst_ver)
                        == _release_version(pin_ver)):
                    errs.append(
                        f"installed.{abi_name}={inst_ver!r} 与 import 成功事实"
                        f"矛盾：链中 {abi_name} import ok 版本 {chain_ver!r}，"
                        "同 venv metadata 必须非空且 release 段一致")

        # 结局/pin_facts 单点派生：校验器直接复用生产者同一判定函数，机判规则
        # （M1 PEP440 release 归一化 + torch local 构建身份 cu124、mujoco 漂移
        # 不判失败）只此一份，禁止在校验器里复制第二套口径。
        derived_outcome, derived_facts, _, _ = decide_outcome(
            steps, chain, pins)
        if obj["outcome"] != derived_outcome:
            errs.append(
                f"outcome={obj['outcome']!r} 与记录事实矛盾："
                f"按 install_steps/import_chain/shared_pins_observed 派生应为 "
                f"{derived_outcome!r}（结局禁止手标）")
        if facts_shape_ok:
            for flag, expected in derived_facts.items():
                if facts[flag] != expected:
                    errs.append(
                        f"pin_facts.{flag}={facts[flag]!r} 与 observed 版本"
                        f"（numpy={pins['numpy']!r}, torch={pins['torch']!r}, "
                        f"mujoco={pins['mujoco']!r}）矛盾，应为 {expected!r}")

    if obj["outcome"] == "conflict_isolated_failed" and not obj["reasons"]:
        errs.append("conflict_isolated_failed 必须给非空 reasons（禁止空口降级）")
    if obj["outcome"] == "compatible" and obj["reasons"]:
        errs.append("compatible 不得带 failure reasons")

    # M6（三签冷读）：degradation 与结局必须一致——字段本身非必填
    # （compatible 时缺席/None 皆可），但失败结局必须逐字落 D-4 降级口径常量。
    degradation = obj.get("degradation", None)
    if obj["outcome"] == "compatible":
        if degradation is not None:
            errs.append("compatible 时 degradation 必须缺席或为 null")
    elif obj["outcome"] == "conflict_isolated_failed":
        if degradation != DEGRADATION:
            errs.append(
                "conflict_isolated_failed 时 degradation 必须逐字为 "
                f"{DEGRADATION!r}，实际 {degradation!r}")

    return errs


def load_report(path: "str | Path") -> dict:
    """图接缝入口：缺件/坏 JSON/结构违规统一抛 Stage0ReportError。"""
    p = Path(path)
    if not p.is_file():
        raise Stage0ReportError(f"Stage0 兼容报告不存在：{p}")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise Stage0ReportError(f"Stage0 兼容报告无法解析：{p}: {exc}") from exc
    errs = validate_report(obj)
    if errs:
        raise Stage0ReportError(
            f"Stage0 兼容报告违规（fail-closed）：{p}: " + "; ".join(errs[:5]))
    return obj


# ---------------------------------------------------------------------------
# LIBERO 首次 import 前置配置（探针脚手架，不构造任何环境/不跑 tour 本体）
# ---------------------------------------------------------------------------

def libero_config_text(benchmark_root: "str | Path",
                       datasets_dir: "str | Path") -> str:
    """生成 ~/.libero/config.yaml 等价件，避开 libero/__init__.py 首跑的
    交互式 input()（探针 stdin 无 TTY，不交互；仅写路径表，不碰仿真）。"""
    base = str(benchmark_root)
    return (
        f"benchmark_root: {base}\n"
        f"bddl_files: {base}/bddl_files\n"
        f"init_states: {base}/init_files\n"
        f"datasets: {datasets_dir}\n"
        f"assets: {base}/assets\n"
    )


# ---------------------------------------------------------------------------
# 容器内编排（仅 main 调用；宿主编排/单测不触发任何子进程安装）
# ---------------------------------------------------------------------------

def _atomic_write_0600(path: "str | Path", text: str) -> None:
    """0600 原子写（LOW-2，三签冷读；与 fluid 兄弟探针同纪律）：

    父目录 0700（不依赖容器 uid 的 umask；已存在也显式 chmod）→
    mkstemp 0600 临时件 → write → fsync → os.replace 原子落位。
    读方永远只见完整件或旧件，不存在半截报告窗口。仅用标准库。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _run(argv: list[str], *, timeout: int, env: "dict | None" = None,
         log_path: "Path | None" = None) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, env=env, errors="replace")
    if log_path is not None:
        _atomic_write_0600(
            log_path,
            f"$ {' '.join(argv)}\n\n[stdout]\n{proc.stdout}\n"
            f"[stderr]\n{proc.stderr}\n")
    return proc.returncode, proc.stdout, proc.stderr


def _pip_step(target: str, argv: list[str], logs_dir: Path,
              timeout: int = PIP_TIMEOUT_S) -> dict:
    log_path = logs_dir / f"pip_{target}.log"
    rc, out, err = _run(argv, timeout=timeout, log_path=log_path)
    combined = (out or "") + (err or "")
    return {
        "target": target,
        "argv": argv,
        "rc": rc,
        "log_path": str(log_path),
        "tail_verbatim": combined[-PIP_TAIL_CHARS:],
    }


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bootstrap_isolated_venv(venv_path: str, base_python: str,
                             logs_dir: Path) -> dict:
    """隔离 venv 引导全序列（锁内适配 2026-10-24，见模块 docstring）：

      创建（--without-pip --system-site-packages）→ sysconfig 双路取 purelib
      → 子 venv purelib 内写单行 simpraxis_shared.pth 指共享 purelib（只读复用）
      → 子解释器实测 numpy/torch 共享 pin 与 ``-m pip`` 自发现。

    合并原文落 venv_create.log（0600）；任一小步失败即整步 rc≠0（保留首个失败
    子步的真实 rc），由 execute_probe 走占位 import 链出 conflict 报告。
    返回单条 install_step（target=venv_create；argv 记录创建命令）。
    """
    log_path = logs_dir / "venv_create.log"
    lines: list[str] = []
    failed_rc = 0

    def record(argv: list[str], rc: int, out: str, err: str,
               note: str = "") -> None:
        lines.append(
            f"$ {' '.join(argv)}\n{note}[stdout]\n{out}\n[stderr]\n{err}\n")

    create_argv = venv_create_cmd(venv_path, base_python)
    rc, out, err = _run(create_argv, timeout=VENV_CREATE_TIMEOUT_S)
    record(create_argv, rc, out, err)
    if rc != 0:
        failed_rc = rc
    else:
        rc, out, err = _run(purelib_query_cmd(base_python),
                            timeout=IMPORT_TIMEOUT_S)
        record(purelib_query_cmd(base_python), rc, out, err,
               note="# 共享解释器 purelib\n")
        if rc != 0:
            failed_rc = rc
    if not failed_rc:
        shared_purelib = out.strip().splitlines()[-1].strip() \
            if out.strip() else ""
        child_py = venv_python(venv_path)
        q_argv = purelib_query_cmd(child_py)
        rc, out, err = _run(q_argv, timeout=IMPORT_TIMEOUT_S)
        record(q_argv, rc, out, err, note="# 子 venv 解释器 purelib\n")
        if rc != 0:
            failed_rc = rc
    if not failed_rc:
        child_purelib = out.strip().splitlines()[-1].strip() \
            if out.strip() else ""
        if not shared_purelib or not child_purelib:
            failed_rc = 1
            lines.append("# purelib 解析为空：shared="
                         f"{shared_purelib!r} child={child_purelib!r}\n")
        else:
            pth_path = child_pth_path(child_purelib)
            try:
                pth_path.parent.mkdir(parents=True, exist_ok=True)
                pth_path.write_text(shared_pth_text(shared_purelib),
                                    encoding="utf-8")
                os.chmod(pth_path, 0o644)
                lines.append(f"# 写入 {pth_path}（单行指向 {shared_purelib}，"
                             "只读复用共享 site-packages）\n")
            except OSError as exc:
                failed_rc = 1
                lines.append(f"# .pth 写入失败：{type(exc).__name__}: {exc}\n")
    if not failed_rc:
        v_argv = verify_shared_imports_cmd(child_py)
        rc, out, err = _run(v_argv, timeout=IMPORT_TIMEOUT_S)
        record(v_argv, rc, out, err,
               note="# 子解释器 import numpy/torch 实测（期望 "
                    f"{SHARED_NUMPY} / {SHARED_TORCH}）\n")
        # M4（三签冷读）：逐行解析取代子串判定——stdout 必须恰好两行，
        # release 段分别等于共享 numpy/torch（PEP440 +local 归一化）；警告行
        # 或任意文本混入即失败（子串匹配能被警告/伪造文本骗过）。
        ver_lines = (out or "").strip().splitlines()
        versions_ok = (
            rc == 0 and len(ver_lines) == 2
            and _release_version(ver_lines[0].strip()) == SHARED_NUMPY
            and _release_version(ver_lines[1].strip()) == SHARED_TORCH)
        if not versions_ok:
            failed_rc = rc or 1
            lines.append(
                "# numpy/torch 自检未过：要求 stdout 恰好两行且 release 段分别为 "
                f"{SHARED_NUMPY}/{SHARED_TORCH}（PEP440 +local 段归一化），"
                f"rc={rc} 实际行={ver_lines!r}\n")
    if not failed_rc:
        p_argv = pip_version_cmd(child_py)
        rc, out, err = _run(p_argv, timeout=IMPORT_TIMEOUT_S)
        record(p_argv, rc, out, err,
               note="# 子解释器 -m pip 自发现实测（不经 PYTHONPATH）\n")
        if rc != 0:
            failed_rc = rc

    combined = "\n".join(lines)
    _atomic_write_0600(log_path, combined)
    return {
        "target": "venv_create",
        "argv": create_argv,
        "rc": failed_rc,
        "log_path": str(log_path),
        "tail_verbatim": combined[-PIP_TAIL_CHARS:],
    }


def execute_probe(out_path: Path, tour_dir: str, venv_path: str,
                  index_url: str, base_python: str,
                  work_dir: Path) -> dict:
    """全流程编排；任何结局都返回结构完整的报告 dict（失败带逐字原文）。"""
    logs_dir = work_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs_dir, 0o700)

    install_steps: list[dict] = []
    installed: dict = {}
    import_chain: list[dict] = []
    shared_pins = {"numpy": None, "torch": None, "mujoco": None}
    infra_error = ""
    try:
        # 1) 建隔离 venv 并完成 .pth 只读复用引导（锁内适配；子步原文同日志）。
        venv_step = _bootstrap_isolated_venv(venv_path, base_python, logs_dir)
        install_steps.append(venv_step)
        if venv_step["rc"] != 0:
            raise RuntimeError("venv 隔离引导失败（--without-pip+.pth，"
                               "见 venv_create.log）")

        # 2) 共享 pin 保护约束（numpy/torch；刻意不含 mujoco）。
        constraints_file = work_dir / "constraints.txt"
        constraints_file.write_text(CONSTRAINTS_TEXT, encoding="utf-8")
        os.chmod(constraints_file, 0o600)

        # 3) 最小链 robosuite+bddl（wheel 无 Requires-Dist，传递依赖面由探针
        # 下一步单点补，不靠 setuptools 隐式收集）。
        install_steps.append(_pip_step(
            "robosuite_bddl",
            pip_install_chain_cmd(venv_path, constraints_file, index_url),
            logs_dir))

        # 锁内适配（轮次 4，2026-10-24 容器实证）：robosuite 1.4.0 wheel 无
        # Requires-Dist、bddl sdist 不声明 future——import 链实证缺 termcolor/
        # future 两件纯 Python 叶子依赖。只在隔离 venv 内补装这两件（-c 同一
        # 约束只保护 numpy/torch），不触任何共享 pin；validate 白名单死钉。
        install_steps.append(_pip_step(
            "isolated_leaf_deps",
            pip_install_leaf_deps_cmd(venv_path, constraints_file, index_url),
            logs_dir))

        # 锁内适配（轮次 4）：/mnt/assets roro 只读挂载，旧式 setup.py 的
        # editable egg_info 必须在源码树写 libero.egg-info（轮次 3 逐字实证
        # Read-only file system）。用隔离 venv 解释器把 TOUR_DIR 原样实体复制
        # （shutil.copytree symlinks=False；文件复制非执行 tour 本体，硬规则
        # 10）到 runs 区可写副本；父目录显式 0700；副本已存在时 copytree
        # 自然 FileExistsError（rc 保留、不覆盖）；src/dst 逐字进 tail。
        copy_parent = Path(WRITABLE_TOUR_COPY).parent
        copy_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(copy_parent, 0o700)
        copy_argv = copy_tour_src_cmd(venv_path, tour_dir, WRITABLE_TOUR_COPY)
        copy_log_path = logs_dir / "libero_src_copy.log"
        rc_copy, out_copy, err_copy = _run(
            copy_argv, timeout=COPY_TOUR_TIMEOUT_S, log_path=copy_log_path)
        install_steps.append({
            "target": "libero_src_copy",
            "argv": copy_argv,
            "rc": rc_copy,
            "log_path": str(copy_log_path),
            "tail_verbatim": (
                (out_copy or "") + (err_copy or ""))[-PIP_TAIL_CHARS:],
        })

        # 锁内适配（轮次 5，2026-10-24 06:12Z 容器实证 + git 取证）：upstream
        # 8f1084e 独缺顶层 libero/__init__.py（find_packages 0 包、finder
        # MAPPING={}、top_level.txt 空），import libero.libero 必败。只在可写
        # 副本 libero/ 下原子补空 marker（原 /mnt/assets 树不触碰；已存在且
        # 非空即非零 rc fail-closed）；rc 如实入步，outcome 仍由单点推导。
        shim_argv = libero_pkg_init_shim_cmd(
            venv_python(venv_path), WRITABLE_TOUR_COPY)
        shim_log_path = logs_dir / "libero_pkg_init_shim.log"
        rc_shim, out_shim, err_shim = _run(
            shim_argv, timeout=IMPORT_TIMEOUT_S, log_path=shim_log_path)
        install_steps.append({
            "target": "libero_pkg_init_shim",
            "argv": shim_argv,
            "rc": rc_shim,
            "log_path": str(shim_log_path),
            "tail_verbatim": (
                (out_shim or "") + (err_shim or ""))[-PIP_TAIL_CHARS:],
        })

        # 6) LIBERO editable 只许指向 runs 区可写副本（--no-deps；绝不回装
        # 只读 /mnt/assets，validate_report 有回归守卫）。
        install_steps.append(_pip_step(
            "libero_editable",
            pip_install_libero_cmd(venv_path, WRITABLE_TOUR_COPY, index_url),
            logs_dir))

        # 7) venv 内定向分发版本。
        rc, out, err = _run(metadata_query_cmd(venv_path),
                            timeout=IMPORT_TIMEOUT_S,
                            log_path=logs_dir / "metadata_versions.log")
        if rc != 0:
            raise RuntimeError(
                f"venv 内版本查询失败：{err.strip()[:300]}")
        installed = parse_metadata_versions(out)

        # 8) import 链（逐模块独立子进程；libero 预置配置避开交互 input）。
        config_dir = work_dir / "libero_config"
        datasets_dir = work_dir / "datasets"
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        datasets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        inner_pkg = Path(tour_dir) / "libero" / "libero"
        (config_dir / "config.yaml").write_text(
            libero_config_text(inner_pkg, datasets_dir), encoding="utf-8")
        os.chmod(config_dir / "config.yaml", 0o600)

        import_chain: list[dict] = []
        for module in _IMPORT_SNIPPETS:
            env = dict(os.environ)
            if module == "libero":
                env["LIBERO_CONFIG_PATH"] = str(config_dir)
            argv = import_check_cmd(module, venv_path)
            try:
                rc_m, out_m, err_m = _run(
                    argv, timeout=IMPORT_TIMEOUT_S, env=env,
                    log_path=logs_dir / f"import_{module}.log")
            except subprocess.TimeoutExpired as exc:
                rc_m, out_m, err_m = -1, exc.stdout or "", (
                    (exc.stderr or "") + f"\n[probe] import {module} 超时"
                    f"（>{IMPORT_TIMEOUT_S}s）")
            import_chain.append(summarize_import(module, rc_m, out_m, err_m))
    except Exception as exc:  # noqa: BLE001 - 基础设施异常也要落 fail 件
        infra_error = f"{type(exc).__name__}: {exc}"

    if not installed:
        # 版本查询本身没跑成（如 venv 创建失败）：按缺失记 null，保持报告可校验。
        installed = {n: None for n in TARGET_DISTRIBUTIONS}

    # import 链因上游断裂（venv 创建失败/pip 步失败或超时/metadata 查询抛错）
    # 整体或部分未执行时，为缺失模块补 ok=False 占位条目并逐字写明未执行原因。
    # schema 不放宽（链恒恰含六模块、失败必带非空 error_verbatim）——这保证 D-4
    # 设计的 conflict_isolated_failed 机判件在任何上游断裂点都能落盘，而不是
    # 被自校验 RuntimeError 吞掉（validate_report 要求 import_chain 非空且恰六件）。
    if len(import_chain) < len(_IMPORT_SNIPPETS):
        failed_step = next((s for s in install_steps if s.get("rc") != 0), None)
        if failed_step:
            why = (f"上游安装失败（{failed_step.get('target')} "
                   f"rc={failed_step.get('rc')}，见 {failed_step.get('log_path')}）")
        elif infra_error:
            why = f"上游编排中断（{infra_error[:200]}）"
        else:
            why = "上游安装失败（探针未进入 import 阶段）"
        present = {e.get("module") for e in import_chain}
        for module in _IMPORT_SNIPPETS:
            if module not in present:
                import_chain.append(
                    summarize_import(module, -1, "", f"未执行：{why}"))
    shared_pins = _shared_pins_from_chain(import_chain)

    # 锁内适配（轮次 4/5，6 步全形）：上游在任一步骤前后中断（venv 引导失败/
    # 子进程超时/编排抛错）时，凡未留下记录的安装步骤一律补 rc=-1 占位步，
    # argv 用各步规范构造器生成（仍满足 argv[0] 契约与 6 步语义白名单），
    # tail 逐字写「未执行：…」——保证 D-4 conflict 报告在任何断裂点都以 6 步
    # 全形落盘，而不是被 validate_report 白名单挡成 RuntimeError 吞件。
    present_targets = [s.get("target") for s in install_steps]
    missing_targets = [t for t in EXPECTED_STEP_TARGETS
                       if t not in present_targets]
    if missing_targets:
        log_path = logs_dir / "orchestration.log"
        prior_fail = next((s for s in install_steps if s.get("rc") != 0), None)
        if prior_fail:
            skip_why = (f"上游安装失败（{prior_fail.get('target')} "
                        f"rc={prior_fail.get('rc')}），本步未执行")
        elif infra_error:
            skip_why = f"上游编排中断（{infra_error[:200]}），本步未执行"
        else:
            skip_why = "上游安装失败（探针未进入本步），本步未执行"
        _atomic_write_0600(
            log_path, infra_error or f"未执行：{skip_why}")
        # 占位 argv 只作记录不执行；constraints 路径允许尚不存在。
        constraints_argv_path = work_dir / "constraints.txt"
        canonical_argv = {
            "venv_create":
                lambda: venv_create_cmd(venv_path, base_python),
            "robosuite_bddl":
                lambda: pip_install_chain_cmd(
                    venv_path, constraints_argv_path, index_url),
            "isolated_leaf_deps":
                lambda: pip_install_leaf_deps_cmd(
                    venv_path, constraints_argv_path, index_url),
            "libero_src_copy":
                lambda: copy_tour_src_cmd(
                    venv_path, tour_dir, WRITABLE_TOUR_COPY),
            "libero_pkg_init_shim":
                lambda: libero_pkg_init_shim_cmd(
                    venv_python(venv_path), WRITABLE_TOUR_COPY),
            "libero_editable":
                lambda: pip_install_libero_cmd(
                    venv_path, WRITABLE_TOUR_COPY, index_url),
        }
        for tgt in missing_targets:
            install_steps.append({
                "target": tgt,
                "argv": canonical_argv[tgt](),
                "rc": -1,
                "log_path": str(log_path),
                "tail_verbatim": f"未执行：{skip_why}",
            })

    outcome, pin_facts, reasons, notes = decide_outcome(
        install_steps, import_chain, shared_pins)
    # 锁内适配（轮次 4 三条 + 轮次 5 一条，2026-10-24 容器实证）：机制实证
    # 随每份报告留证（robosuite wheel 零 Requires-Dist、只读挂载致旧式
    # egg_info 失败逐字引文、隔离 venv 补 termcolor/future 叶子依赖不触共享
    # pin、upstream 8f1084e 缺顶层 libero/__init__.py 的 shim 补法）。
    notes = [*notes, *ADAPTATION_NOTES]
    if infra_error and outcome == "compatible":
        outcome = "conflict_isolated_failed"
        reasons.append(f"探针基础设施异常：{infra_error[:300]}")

    report = {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "ok": outcome == "compatible",
        "outcome": outcome,
        "venv_path": venv_path,
        "index_url": index_url,
        "tour_dir": tour_dir,
        "installed": installed,
        "install_steps": install_steps,
        "import_chain": import_chain,
        "shared_pins_observed": shared_pins,
        "pin_facts": pin_facts,
        "reasons": reasons,
        "notes": notes,
        "degradation": DEGRADATION if outcome != "compatible" else None,
        "ts_utc": _utc_now(),
    }
    errs = validate_report(report)
    if errs:  # 探针自身 bug：宁可响点也不吐非法件
        raise RuntimeError("生成的 Stage0 报告自校验失败：" + "; ".join(errs[:5]))
    _atomic_write_0600(
        out_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Task7 B 线 Stage0 LIBERO 隔离 venv 兼容探针（容器内执行）")
    ap.add_argument("--out", required=True, help="stage0_compat_report.json 路径")
    ap.add_argument("--tour-dir", default=TOUR_DIR,
                    help="A 线结构化克隆挂载路径（默认 %(default)s）")
    ap.add_argument("--venv", default=VENV_PATH, help="隔离 venv 路径")
    ap.add_argument("--index-url", default=INDEX_URL, help="pip 源（清华）")
    ap.add_argument("--base-python", default=BASE_PYTHON,
                    help="创建 venv 用的共享解释器（只创建、不装包）")
    args = ap.parse_args()

    out_path = Path(args.out)
    work_dir = out_path.parent / (out_path.stem + "_assets")
    report = execute_probe(out_path, args.tour_dir, args.venv, args.index_url,
                           args.base_python, work_dir)
    print(json.dumps({"schema": report["schema"], "outcome": report["outcome"],
                      "ok": report["ok"], "report": str(out_path),
                      "pin_facts": report["pin_facts"],
                      "reasons": report["reasons"]},
                     ensure_ascii=False))
    # 两种结局都是合法 Stage0 机判产物（报告即 Gate1 输入）；rc 恒 0。
    # 只有报告自校验失败（抛错在上游）才非零退出。
    print("T7_LIBERO_STAGE0_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
