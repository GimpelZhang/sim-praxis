"""run_cmd：兄弟容器执行 + 本地 NONPROD 测试形态（AC-T4-5/17/18）。

硬约束：

- argv 永远是列表，``shell=False``；shell 元字符在 policy 层已拒；
- cwd 钉死 workdir（容器内 /work），env 只透传模型白名单变量，
  PATH 等固定值由后端给，模型不可控；
- 默认形态是**兄弟容器**：``--network none``、``--read-only`` 根文件系统、
  cap-drop ALL、no-new-privileges、不挂 docker.sock、依赖镜像内预装、
  workdir rw + venv/kb/probes 只读挂载、/tmp tmpfs；
- 本地形态仅测试用：NONPROD 横幅必出（缺横幅的测试必红），且 policy
  在 nonprod 下拒绝一切写类工具（AC-T4-17 本地部分）；
- timeout；stdout/stderr 头/尾截断，全量落 trace 存储并给 sha256。
"""
from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..forensics import decode_observation, sha256_bytes, truncate_observation
from ..policy import (
    PolicyDecision,
    PreToolUsePolicy,
    ToolContext,
)
from ..schema import Field, SchemaError, argv_field, int_field, validate
from . import ToolResult

NONPROD_BANNER = (
    "=== SIMPRAXIS NONPROD：本地 run_cmd 形态，仅供测试；"
    "写类工具已被 policy 拒绝（AC-T4-17） ===")

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900

# 模型可设置的容器环境变量白名单（值也过字符检查）；PATH/HOME 等由
# 后端固定，模型不可控。PYTHONPATH/PYTEST_ADDOPTS 刻意不在内：容器依赖
# 全部来自只读挂载 /opt/venv，放行这两个会让模型改写解释器导入面/注入
# 插件（2026-10-30 评审 MINOR-6）。
_MODEL_ENV_ALLOWLIST = frozenset({
    "PYTEST_TAIL_CHARS",
    "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "MPLBACKEND",
    "LANG", "LC_ALL", "MUJOCO_GL", "PYOPENGL_PLATFORM",
})
_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=+,@% -]{0,4096}$")

# 容器内固定环境（模型不可覆盖）。
_CONTAINER_ENV = {
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "PYTHONUNBUFFERED": "1",
    "HF_HOME": "/opt/hf",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
}

_DOCKER_SOCKET_PARTS = ("docker.sock",)


@dataclass
class CommandResult:
    rc: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    argv: list[str]
    form: str               # "sibling_container" | "local_nonprod"


class RunBackend:
    """run_cmd 执行后端接口（测试以 fake 后端注入，零 docker 依赖）。"""

    form = "abstract"
    nonprod = False

    def run(self, argv: list[str], *, cwd: Path,
            env: dict[str, str], timeout_s: int) -> CommandResult:
        raise NotImplementedError


def _check_no_docker_sock(mounts: list[tuple[str, str, str]]) -> None:
    for src, _dst, _mode in mounts:
        if any(part in src for part in _DOCKER_SOCKET_PARTS):
            raise ValueError("安全闸：禁止挂载 docker.sock（兄弟容器边界）")


def build_docker_create_argv(*, image: str, argv: list[str], workdir: Path,
                             venv_path: str = "/mnt/venvs/simpraxis",
                             kb_path: "str | None" = None,
                             probes_path: "str | None" = None,
                             env: "dict[str,str] | None" = None,
                             network: str = "none",
                             label: "str | None" = None
                             ) -> list[str]:
    """兄弟容器 ``docker create`` argv 构造（唯一构造点；测试逐段断言）。

    M2（2026-10-30 评审 MAJOR）：由 ``docker run --rm -i`` 改为先 create
    记录容器 ID、再 ``docker start -a -i <id>``——超时杀的是 docker 客户端，
    容器仍在跑，旧实现的 ``docker rm -f --filter`` 是死命令（unknown flag，
    rc125），必留孤儿；create/start 后可用 ID 精确删除。
    """
    if not isinstance(image, str) or not image:
        raise ValueError("镜像引用必须是非空字符串（digest pin 见 AC-T4-18）")
    label = label or f"simpraxis-agentic-{uuid.uuid4().hex}"
    mounts: list[tuple[str, str, str]] = [
        (str(Path(workdir).resolve()), "/work", "rw"),
        (venv_path, "/opt/venv", "ro"),
    ]
    if kb_path:
        mounts.append((kb_path, "/kb", "ro"))
    if probes_path:
        # M3：声称挂载 /probes 就必须是宿主上真实存在的目录，否则容器内
        # 白名单探针 ENOENT（docker 对不存在路径会静默创建 root 属主目录）。
        if not Path(probes_path).is_dir():
            raise ValueError(
                f"探针挂载源目录不存在或不是目录：{probes_path}（/probes "
                "挂载 fail-closed，M3）")
        mounts.append((probes_path, "/probes", "ro"))
    _check_no_docker_sock(mounts)

    cmd = [
        "docker", "create", "--rm", "-i",
        f"--network={network}",          # 默认 none；白名单外不可达
        "--user", "1000:1000",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",                    # 根文件系统只读
        "--pids-limit", "256",
        "--memory", "4g",
        "--tmpfs", "/tmp:rw,size=256m,mode=1777",
        "-w", "/work",
        "--label", f"simpraxis-agentic-run={label}",
    ]
    for src, dst, mode in mounts:
        cmd.extend(["-v", f"{src}:{dst}:{mode}"])
    for key, value in _CONTAINER_ENV.items():
        cmd.extend(["-e", f"{key}={value}"])
    for key, value in sorted((env or {}).items()):
        cmd.extend(["-e", f"{key}={value}"])
    cmd.append(image)
    cmd.extend(["--"])
    cmd.extend(argv)
    return cmd


class DockerSiblingBackend(RunBackend):
    """默认后端：宿主机 docker CLI 起兄弟容器（不挂 sock、默认禁网）。"""

    form = "sibling_container"

    def __init__(self, image: str, *, venv_path: str = "/mnt/venvs/simpraxis",
                 kb_path: "str | None" = None,
                 probes_path: "str | None" = None,
                 network: str = "none", runner: Any = subprocess.run):
        self.image = image
        self.venv_path = venv_path
        self.kb_path = kb_path
        self.probes_path = probes_path
        self.network = network
        self._runner = runner
        # M3：构造期校验 /probes 挂载源（给了就必须是真实目录；不给则
        # run_probe 调用侧 fail-closed 拒绝，见 tools/probe.py）。
        if probes_path is not None and not Path(probes_path).is_dir():
            raise ValueError(
                f"探针挂载源目录不存在或不是目录：{probes_path}（/probes "
                "挂载 fail-closed，M3）")

    def run(self, argv, *, cwd, env=None, timeout_s=DEFAULT_TIMEOUT_S) -> CommandResult:
        label_uuid = uuid.uuid4().hex
        create_cmd = build_docker_create_argv(
            image=self.image, argv=list(argv), workdir=cwd,
            venv_path=self.venv_path, kb_path=self.kb_path,
            probes_path=self.probes_path, env=env, network=self.network,
            label=label_uuid)

        def _run(cmd: list[str], timeout: int, *, stdin=subprocess.DEVNULL):
            return self._runner(
                cmd, cwd=str(cwd), timeout=timeout, stdin=stdin,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # M2：先 create 拿容器 ID（--rm 保证正常退出自删），再 start -a -i
        # 附着执行；TimeExpired 杀掉的只是 docker 客户端，容器仍存活——
        # 必须按记录的 ID 精确 docker rm -f（见 _cleanup）。
        try:
            created = _run(create_cmd, timeout=30)
        except subprocess.TimeoutExpired as exc:
            self._cleanup("", label_uuid)
            return CommandResult(
                rc=124, stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                timed_out=True, argv=create_cmd, form=self.form)
        cid = (created.stdout or b"").decode("utf-8", "replace").strip()
        if created.returncode != 0 or not cid:
            return CommandResult(
                rc=created.returncode or 125, stdout=created.stdout or b"",
                stderr=created.stderr or b"", timed_out=False,
                argv=create_cmd, form=self.form)
        start_cmd = ["docker", "start", "-a", "-i", cid]
        try:
            proc = _run(start_cmd, timeout_s + 10)
            return CommandResult(
                rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                timed_out=False, argv=create_cmd, form=self.form)
        except subprocess.TimeoutExpired as exc:
            # 超时清理：先按记录的容器 ID 精确删，再按 label 兜底扫尾。
            self._cleanup(cid, label_uuid)
            return CommandResult(
                rc=124, stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                timed_out=True, argv=create_cmd, form=self.form)

    def _cleanup(self, container_id: str, label_uuid: str) -> None:
        """超时孤儿清理（M2）：docker rm -f <id> 主路 + label 扫描兜底。

        全部 argv 列表，永不 shell=True；旧实现 ``docker rm -f --filter``
        是死命令（unknown flag rc125），真容器被孤儿。
        """
        label_filter = f"label=simpraxis-agentic-run={label_uuid}"

        def _quiet(cmd: list[str]):
            try:
                return self._runner(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, timeout=20)
            except Exception:  # noqa: BLE001 - 清理失败不得顶掉超时结论
                return None

        if container_id:
            _quiet(["docker", "rm", "-f", container_id])
        # 兜底：ID 丢失/竞态时按 run label 扫出仍存活的同族容器再删。
        ps = _quiet(["docker", "ps", "-q", "--filter", label_filter])
        if ps is not None and getattr(ps, "returncode", 1) == 0:
            ids = (getattr(ps, "stdout", b"") or b"").decode(
                "utf-8", "replace").split()
            if ids:
                _quiet(["docker", "rm", "-f", *ids])


class LocalSubprocessBackend(RunBackend):
    """测试专用本地后端：shell=False、env 清洗、NONPROD 横幅进 stderr。"""

    form = "local_nonprod"
    nonprod = True

    def __init__(self, runner: Any = subprocess.run):
        self._runner = runner

    def run(self, argv, *, cwd, env=None, timeout_s=DEFAULT_TIMEOUT_S) -> CommandResult:
        scrubbed = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for key in ("LANG", "LC_ALL"):
            if os.environ.get(key):
                scrubbed[key] = os.environ[key]
        scrubbed.update(env or {})
        banner = (NONPROD_BANNER + "\n").encode("utf-8")
        try:
            proc = self._runner(
                list(argv), cwd=str(cwd), env=scrubbed,
                timeout=timeout_s, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False)
            return CommandResult(
                rc=proc.returncode, stdout=proc.stdout,
                stderr=banner + proc.stderr, timed_out=False,
                argv=list(argv), form=self.form)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                rc=124, stdout=exc.stdout or b"",
                stderr=banner + (exc.stderr or b""),
                timed_out=True, argv=list(argv), form=self.form)


def _validate_env(raw: Any) -> "dict[str,str] | str":
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return "env 必须是对象（键值均为字符串）"
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in _MODEL_ENV_ALLOWLIST:
            return f"env 变量 {key!r} 不在模型白名单 {sorted(_MODEL_ENV_ALLOWLIST)}"
        if not isinstance(value, str) or not _ENV_VALUE_RE.match(value):
            return f"env {key} 值非法（仅允许 {_ENV_VALUE_RE.pattern}）"
        out[key] = value
    return out


def check_backend(ctx: ToolContext) -> "RunBackend | ToolResult":
    """后端装配/形态一致性闸（run_cmd 与 run_probe 共用）。"""
    backend = ctx.run_backend
    if backend is None:
        return ToolResult.denied_result(PolicyDecision.deny(
            "run_backend 未装配（生产必须注入兄弟容器后端）"))
    if ctx.nonprod and not getattr(backend, "nonprod", False):
        return ToolResult.denied_result(PolicyDecision.deny(
            "nonprod 上下文只允许 LocalSubprocessBackend"))
    if not ctx.nonprod and getattr(backend, "nonprod", False):
        return ToolResult.denied_result(PolicyDecision.deny(
            "生产上下文禁止使用本地 NONPROD 后端"))
    return backend


def execute_render(ctx: ToolContext, argv: list[str], *, tag: str,
                   timeout_s: int, env: "dict[str,str] | None" = None
                   ) -> ToolResult:
    """跑 argv 并渲染观察（全量 bytes 落 trace 存储，头尾截断给模型）。"""
    backend = ctx.run_backend
    result = backend.run(
        list(argv), cwd=ctx.workdir, env=env or {}, timeout_s=timeout_s)
    out_text, _out_bin_flag = decode_observation(result.stdout)
    err_text, _err_bin_flag = decode_observation(result.stderr)
    out_sha = sha256_bytes(result.stdout)
    err_sha = sha256_bytes(result.stderr)
    refs: dict[str, str] = {}
    if ctx.tracer is not None:
        # 全量原文（含二进制）按 bytes 落 trace 存储；模型只见替换解码文本。
        refs["stdout_ref"] = ctx.tracer.store_full(result.stdout).relpath
        refs["stderr_ref"] = ctx.tracer.store_full(result.stderr).relpath
    header = (
        f"[{tag}] form={result.form} argv={argv!r}\n"
        f"rc={result.rc} timed_out={result.timed_out}\n"
        f"stdout_sha256={out_sha} stderr_sha256={err_sha}\n"
        f"stdout_bytes={len(result.stdout)} stderr_bytes={len(result.stderr)}\n"
        + "".join(f"{k}={v}\n" for k, v in sorted(refs.items())))
    full = f"{header}\n--- stdout ---\n{out_text}\n--- stderr ---\n{err_text}"
    visible, _h, _t, _truncated = truncate_observation(full)
    return ToolResult(output=visible, full=full,
                      decision=PolicyDecision.allow(), denied=False)


def run_cmd(ctx: ToolContext, **kwargs) -> ToolResult:
    fields = (
        argv_field("argv"),
        int_field("timeout_s", default=DEFAULT_TIMEOUT_S, minimum=1,
                  maximum=MAX_TIMEOUT_S),
        Field("env", required=False, default=None),
    )
    try:
        args = validate(fields, kwargs)
    except SchemaError as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"schema 拒绝：{exc}"))
    env_or_err = _validate_env(args.get("env"))
    if isinstance(env_or_err, str):
        return ToolResult.denied_result(PolicyDecision.deny(env_or_err))
    args["env"] = env_or_err

    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("run_cmd", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)

    backend = check_backend(ctx)
    if isinstance(backend, ToolResult):
        return backend
    return execute_render(ctx, args["argv"], tag="run_cmd",
                          timeout_s=args["timeout_s"], env=args["env"])
