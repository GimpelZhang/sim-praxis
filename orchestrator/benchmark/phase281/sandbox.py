"""Phase 2.8.1 T4：两臂的执行底座（宿主进程 / 兄弟容器 / 任务镜像）与运行形态闸。

计划 §8.5.3 的硬约束：**agentic 臂只能跑在兄弟容器里**。

* ``LocalSubprocessBackend`` 带类属性 ``nonprod = True``；``check_backend()``
  在生产上下文（``nonprod=False``）见到它就直接 deny（"生产上下文禁止使用本地
  NONPROD 后端"）；反过来，``nonprod=True`` 时 ``GrantTable._check_write`` 拒绝
  一切写入。两条合起来意味着：**本地进程形态下 agentic 臂写不了任何文件**——
  那不是能力问题，是配置错误伪装成实验结论。
* 所以 agentic 臂一律 ``DockerSiblingBackend``（M1/R1 用 ``AGENTIC_IMAGE`` +
  只读挂载宿主 venv；L1 用任务镜像，见下）。

**L1 的运行形态（Form A，2026-11-06 双探针实测裁定）**：

=====================  ==========================  =========  ========
形态                   容器 / 解释器                 MUJOCO     结论
=====================  ==========================  =========  ========
A（**采用**）          ``libero:task6@sha256:…`` +
                       ``/usr/bin/python3``          2.3.7      rc=0
B（否决）              ``AGENTIC_IMAGE`` +
                       ``/opt/venv/bin/python``      3.15.0     ✗ 与
                                                              ``task.yaml``
                                                              要求的 2.3.7
                                                              不符
=====================  ==========================  =========  ========

形态 B 之所以**根本不可用**：``DockerSiblingBackend`` 无条件把宿主的
``/mnt/venvs/simpraxis`` 挂到 ``/opt/venv``，且容器 ``PATH`` 以
``/opt/venv/bin`` 打头——LIBERO 任务镜像自带的系统解释器会被挡住，
``python`` 解析到宿主编译期（mujoco 3.15.0）。而 ``venv_path=None`` 更糟：
``build_docker_create_argv`` 在挂载安全闸（``_check_no_docker_sock`` 对源路径
做 ``in`` 判断）上**直接抛 ``TypeError``**，连 argv 都拼不出来（AC-P281-4-18
负例④；锁内适配：计划写的是「拼出 ``None:/opt/venv:ro`` 这种非法挂载」，
实测是 fail-closed 抛错，比非法挂载更早、更强）。故 L1 走
:class:`TaskImageBackend`：
兄弟容器形态（``form == "sibling_container"``）、不挂宿主 venv、argv 由本模块
按 §8.5.3 的形状构造后再经 ``reference.validate_container_argv`` 自查。

锁内适配（计划 §8.5.3 只写了「L1 的 argv 经 ``build_docker_create_argv``」）：
``build_docker_create_argv`` 强制挂 venv、不认任务镜像 —— 与 AC-P281-4-14
要求的 ``validate_container_argv``（``-v`` 白名单 + ``:ro`` >= 3）也不兼容。
故 L1 的 argv 改由 ``reference.docker_argv`` 同形构造，AC-14 判据不变。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.benchmark import reference as _ref

from . import constants

__all__ = (
    "ContainerSandbox",
    "ExecResult",
    "HostSandbox",
    "SCRUB_PREFIXES",
    "SandboxError",
    "TaskImageBackend",
    "build_run_backend",
    "clean_env",
    "sandbox_for",
)

#: 子进程环境**必须剔除**的前缀/键：凭据（硬规则 4）与实验路径坐标
#: （``SIMPRAXIS_PHASE281_ROOT`` 会直接把候选引到 ``private/``；这正是
#: §8.3.1 通道ⓐ 想检测的坐标，**不该由运行器主动奉上**）。
SCRUB_PREFIXES = ("ANTHROPIC_", "SIMPRAXIS_", "AWS_", "GITHUB_", "GH_",
                  "KB_ROOT", "OPENAI_", "HF_TOKEN")
#: 容器内固定标签（与 ``reference`` 的参考运行同族，便于事后按 label 收尾）
RUN_LABEL = "simpraxis.benchmark.reference=1"


class SandboxError(RuntimeError):
    """执行底座无法装配/运行形态非法（配置问题，不是候选问题）。"""


@dataclass(frozen=True)
class ExecResult:
    """与 ``agent_runner.runner._ExecResult`` 同形的三元组（鸭子类型）。"""

    rc: int
    stdout: str
    stderr: str


def clean_env(extra: "Mapping[str, str] | None" = None) -> "dict[str, str]":
    """宿主环境 → 子进程环境：剔除凭据与实验坐标，再叠加引擎给的额外变量。"""
    out: "dict[str, str]" = {}
    for key, value in os.environ.items():
        if any(key == p or key.startswith(p) for p in SCRUB_PREFIXES):
            continue
        out[key] = value
    for key, value in (extra or {}).items():
        if any(key == p or key.startswith(p) for p in SCRUB_PREFIXES):
            # 引擎自己要往子进程塞凭据也不行：fail-closed 直接丢。
            continue
        out[str(key)] = str(value)
    return out


# ---------------------------------------------------------------------------
# 宿主进程底座（M1/R1 的 legacy 臂）
# ---------------------------------------------------------------------------

class HostSandbox:
    """宿主进程执行：``python <entry>`` 直接跑在 Trial workdir 里。

    仅用于 ``runtime == "host"`` 的任务（M1/R1）的 **legacy 臂**；agentic 臂
    必须走容器（见模块 docstring）。
    """

    kind = "host"
    form = "host_process"

    def __init__(self, python: str, *, runner: Callable[..., Any] = subprocess.run):
        if not isinstance(python, str) or not python:
            raise SandboxError("HostSandbox 需要非空解释器路径")
        self.python = python
        self._runner = runner

    def subprocess_runner(self) -> Callable[[list, Path, float, dict], ExecResult]:
        def _run(cmd, workdir, timeout_s, env_extra) -> ExecResult:
            proc = self._runner(
                [str(item) for item in cmd], cwd=str(workdir),
                env=clean_env(env_extra), capture_output=True, text=True,
                timeout=timeout_s)
            return ExecResult(int(proc.returncode), proc.stdout or "",
                              proc.stderr or "")

        return _run

    def run_backend(self) -> Any:
        raise SandboxError(
            "宿主任务没有容器运行形态：agentic 臂必须使用兄弟容器后端 "
            "（计划 §8.5.3）")

    def describe(self) -> dict:
        return {"kind": self.kind, "form": self.form, "python": self.python}


# ---------------------------------------------------------------------------
# 容器底座
# ---------------------------------------------------------------------------

class ContainerSandbox:
    """把任意 ``RunBackend`` 同时暴露成 ``subprocess_runner`` 与 ``run_backend``。

    同一个后端服务两臂：legacy 臂通过 ``subprocess_runner`` 跑 ``spec.cmd``，
    agentic 臂通过 ``run_backend`` 跑模型选的 argv——**两条路走的是同一种容器**，
    这正是「两臂只差引擎」的一部分。
    """

    kind = "container"

    def __init__(self, backend: Any):
        if backend is None:
            raise SandboxError("ContainerSandbox 需要 RunBackend")
        self.backend = backend

    @property
    def form(self) -> str:
        return str(getattr(self.backend, "form", "unknown"))

    def subprocess_runner(self) -> Callable[[list, Path, float, dict], ExecResult]:
        def _run(cmd, workdir, timeout_s, env_extra) -> ExecResult:
            result = self.backend.run(
                [str(item) for item in cmd], cwd=Path(workdir),
                env=dict(env_extra or {}), timeout_s=float(timeout_s))
            return ExecResult(
                int(result.rc),
                (result.stdout or b"").decode("utf-8", "replace")
                if isinstance(result.stdout, (bytes, bytearray)) else str(result.stdout),
                (result.stderr or b"").decode("utf-8", "replace")
                if isinstance(result.stderr, (bytes, bytearray)) else str(result.stderr))

        return _run

    def run_backend(self) -> Any:
        return self.backend

    def describe(self) -> dict:
        return {"kind": self.kind, "form": self.form,
                "backend": type(self.backend).__name__,
                "image": getattr(self.backend, "image", None)}


class TaskImageBackend:
    """在**任务镜像**里跑 argv 的兄弟容器后端（L1；``form == "sibling_container"``）。

    与 ``DockerSiblingBackend`` 的差别只有两处，都是 L1 的硬需求：

    1. **不挂宿主 venv**——任务镜像自带 ``/usr/bin/python3`` 与
       robosuite/mujoco 2.3.7/bddl/libero；
    2. 挂载形状按 ``reference`` 的参考运行：``/work``（只读输入快照）、
       ``/skills``、``/assets`` 三个只读 + ``/out``（工作目录，rw）。
    """

    form = "sibling_container"
    nonprod = False

    def __init__(self, image_ref: str, *, workdir: Path,
                 input_dir: Path, skills_dir: "Path | None" = None,
                 assets_dir: "Path | None" = None,
                 network: str = "none", gpus: bool = True,
                 runner: Callable[..., Any] = subprocess.run):
        if not isinstance(image_ref, str) or "@sha256:" not in image_ref:
            raise SandboxError(
                f"任务镜像必须按 tag@sha256:<hex> 调用：{image_ref!r}")
        if network != "none":
            raise SandboxError(
                f"任务镜像后端只允许 --network none：{network!r}")
        self.image = image_ref
        self.workdir = Path(workdir)
        self.input_dir = Path(input_dir)
        self.skills_dir = Path(skills_dir) if skills_dir is not None else None
        self.assets_dir = Path(assets_dir) if assets_dir is not None else None
        self.network = network
        self.gpus = bool(gpus)
        self._runner = runner

    # ---- 挂载与 argv ----

    def mounts(self) -> "list[_ref.Mount]":
        """四个挂载点（三个 ``:ro`` + 一个 ``:rw``，全在 ``MOUNT_WHITELIST`` 内）。"""
        out = [
            _ref.Mount(self.input_dir, "/work", "ro"),
            _ref.Mount(self.workdir, "/out", "rw"),
        ]
        # 空目录也比「不挂」诚实：任务声明的 ``readonly_skills: true`` 有承载物，
        # 且 AC-14 的 ``:ro >= 3`` 在无技能件的 Trial 上依然成立。
        skills = self.skills_dir or (self.input_dir / "skills")
        out.append(_ref.Mount(skills, "/skills", "ro"))
        assets = self.assets_dir or (self.input_dir / "assets")
        out.append(_ref.Mount(assets, "/assets", "ro"))
        return out

    def build_argv(self, argv: "list[str]", *, env: "Mapping[str,str] | None" = None
                   ) -> "list[str]":
        """构造 ``docker run`` argv；不合规**先于**起容器抛错。"""
        mounts = self.mounts()
        problems = _ref.validate_mounts(mounts)
        if problems:
            raise SandboxError("挂载不合规：" + "; ".join(problems))
        cmd = ["docker", "run", "--rm"]
        if self.gpus:
            cmd += ["--gpus", "all"]
        cmd += ["--ipc=host", "--network", "none", "--label", RUN_LABEL,
                "-w", "/out"]
        for mount in mounts:
            cmd += ["-v", mount.flag]
        for key, value in sorted(dict(env or {}).items()):
            cmd += ["-e", f"{key}={value}"]
        cmd.append(self.image)
        cmd += [str(item) for item in argv]
        problems = _ref.validate_container_argv(cmd)
        if problems:
            raise SandboxError("容器 argv 不合规：" + "; ".join(problems))
        return cmd

    # ---- 执行 ----

    def run(self, argv, *, cwd, env=None, timeout_s=300):
        """跑一次 ``docker run``；返回 ``agentic.tools.run.CommandResult`` 形态。"""
        from orchestrator.agentic.tools.run import CommandResult

        cmd = self.build_argv(list(argv), env=env)
        try:
            proc = self._runner(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                rc=124, stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                timed_out=True, argv=cmd, form=self.form)
        return CommandResult(rc=int(proc.returncode),
                             stdout=proc.stdout or b"", stderr=proc.stderr or b"",
                             timed_out=False, argv=cmd, form=self.form)


# ---------------------------------------------------------------------------
# 运行形态闸（AC-P281-4-18 的唯一构造点）
# ---------------------------------------------------------------------------

def _task_python(spec: Any) -> str:
    runtime = dict(getattr(spec, "runtime", {}) or {})
    python = runtime.get("python")
    if not isinstance(python, str) or not python:
        raise SandboxError(f"{getattr(spec, 'task_id', '?')}: runtime.python 为空")
    return python


def build_run_backend(spec: Any, *, trial_workdir: Path, input_dir: Path,
                      skills_dir: "Path | None" = None,
                      assets_dir: "Path | None" = None,
                      runner: Callable[..., Any] = subprocess.run) -> Any:
    """按任务建 **agentic 臂**的 ``RunBackend``（容器形态，AC-18 单点）。

    * M1/R1（``runtime == "host"``）：``DockerSiblingBackend(AGENTIC_IMAGE)``，
      ``--network none``，依赖来自只读挂载的宿主 venv（mujoco 3.15.0 /
      PyYAML / jsonschema 都在里面）。
    * L1（``runtime == "docker"``）：:class:`TaskImageBackend` 指向任务镜像
      （``libero:task6@<digest>``），系统解释器 ``/usr/bin/python3``。
    """
    from . import arm_adapter  # 避免循环（arm_adapter 不认识本模块）

    del arm_adapter  # 仅用于 import 期断言依赖方向，无运行期作用
    task_id = str(getattr(spec, "task_id", ""))
    runtime = constants.RUNTIME_BY_ID.get(task_id)
    if runtime is None:
        raise SandboxError(f"未登记的任务：{task_id!r}（{constants.TASK_IDS}）")
    if runtime == "docker":
        return TaskImageBackend(
            _ref.libero_image_ref(spec), workdir=trial_workdir,
            input_dir=input_dir, skills_dir=skills_dir,
            assets_dir=assets_dir, runner=runner)
    from orchestrator.agentic.image_pin import AGENTIC_IMAGE
    from orchestrator.agentic.tools.run import DockerSiblingBackend

    return DockerSiblingBackend(image=AGENTIC_IMAGE, network="none")


def sandbox_for(spec: Any, arm: Any, *, trial_workdir: Path, input_dir: Path,
                skills_dir: "Path | None" = None,
                assets_dir: "Path | None" = None,
                runner: Callable[..., Any] = subprocess.run) -> Any:
    """两臂的底座选择（**唯一**选择点）：

    ===================  ======================  ==========================
    任务 / 臂             底座                     形态
    ===================  ======================  ==========================
    M1/R1 + legacy_v1    ``HostSandbox``          宿主进程
    M1/R1 + agentic_v1   ``ContainerSandbox``     兄弟容器 ``AGENTIC_IMAGE``
    L1 + 任一臂           ``ContainerSandbox``     兄弟容器 任务镜像
    ===================  ======================  ==========================

    L1 的 legacy 臂也走容器——LIBERO 的 robosuite/mujoco 2.3.7 只装在任务镜像
    里，宿主 ``/mnt/venvs/simpraxis`` 是 3.15.0，跑起来必崩（那是环境错误，
    会被误读成「旧引擎不行」）。
    """
    task_id = str(getattr(spec, "task_id", ""))
    runtime = constants.RUNTIME_BY_ID.get(task_id)
    if runtime is None:
        raise SandboxError(f"未登记的任务：{task_id!r}（{constants.TASK_IDS}）")
    if runtime == "docker":
        return ContainerSandbox(build_run_backend(
            spec, trial_workdir=trial_workdir, input_dir=input_dir,
            skills_dir=skills_dir, assets_dir=assets_dir, runner=runner))
    if bool(getattr(arm, "runspec_agentic", False)):
        return ContainerSandbox(build_run_backend(
            spec, trial_workdir=trial_workdir, input_dir=input_dir,
            skills_dir=skills_dir, assets_dir=assets_dir, runner=runner))
    return HostSandbox(_task_python(spec), runner=runner)
