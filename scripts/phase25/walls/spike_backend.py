"""Task 5 历史墙 spike 的兄弟容器后端（ops 件，零产品代码改动）。

产品 ``DockerSiblingBackend`` 只挂 workdir + 一个 venv，而两面历史墙需要
额外的冻结快照挂载（隔离 libero venv、主 venv 同路径软链、LIBERO tour、
冻结 gate_env）与固定环境变量（PYTHONPATH/LIBERO_CONFIG_PATH 等在模型
env 白名单之外，只能由后端注入、模型不可控）。本件实现产品
``RunBackend`` 鸭子接口（form="sibling_container"、nonprod=False，能过
``check_backend`` 的形态闸），create/start 分离与超时 ID 精确清理纪律
逐段照搬产品实现，另提供两墙的一次性评分调用。

唯一付费入口是 run_wall.py；本件不含任何网关/模型代码。
"""
from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestrator.agentic.tools.run import CommandResult, _check_no_docker_sock

IMAGE = "sim-praxis/agentic:phase25-t4"
IMAGE_ID = "abf96496a6d7"
IMAGE_REPODIGEST = (
    "sha256:abf96496a6d72cf4f61c37a3cfe7d657013c1a567f"
    "cc88b04ac2b616d012fed7")

# 冻结快照宿主路径（00_env 之外的固定真相；freeze 清单逐件登记）。
VENV_LIBERO = "/mnt/artifacts/simpraxis/runs/phase2/venvs/libero"
MAIN_VENV = "/mnt/venvs/simpraxis"
LIBERO_TOUR = (
    "/mnt/artifacts/simpraxis/runs/phase2/task7/libero_src/LIBERO_tour")

_BASE_DOCKER = [
    "docker", "create", "--rm", "-i", "--network=none",
    "--user", "1000:1000", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--read-only",
    "--pids-limit", "256", "--memory", "4g",
    "--tmpfs", "/tmp:rw,size=256m,mode=1777", "-w", "/work",
]
_FIXED_ENV = {
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "PYTHONUNBUFFERED": "1",
    "HF_HOME": "/opt/hf",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
}
# 墙专属固定 env（模型 env 白名单不含这些键，只能由本后端注入）。
WALL_ENV = {
    "wallA": {
        "CUDA_VISIBLE_DEVICES": "",
        "PYTHONPATH": "/opt/gate/pyshim",
        "LIBERO_CONFIG_PATH": "/opt/gate/libero_cfg",
        "NUMBA_CACHE_DIR": "/tmp/numba_cache",
        "MPLCONFIGDIR": "/tmp/mpl",
    },
    "wallB": {
        "CUDA_VISIBLE_DEVICES": "",
    },
}
# 墙专属挂载（src, dst, mode）；workdir 与两 venv 是公共底。
WALL_MOUNTS = {
    "wallA": (
        (LIBERO_TOUR, "/opt/libero_tour", "ro"),
    ),
    "wallB": (),
}

DEFAULT_TIMEOUT_S = 300


def freeze_root() -> Path:
    root = os.environ.get("SIMPRAXIS_T5_FREEZE_ROOT")
    if root:
        return Path(root)
    base = os.environ.get("SIMPRAXIS_PHASE25_ROOT")
    if not base:
        raise RuntimeError(
            "SIMPRAXIS_PHASE25_ROOT 未设置（先 source scripts/00_env.sh），"
            "且未显式给 SIMPRAXIS_T5_FREEZE_ROOT")
    return Path(base) / "walls" / "t5_freeze"


def _quiet(runner: "object", cmd: list[str]) -> "object | None":
    try:
        return runner(cmd, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                      timeout=20)
    except Exception:  # noqa: BLE001 - 清理失败不顶超时结论
        return None


def cleanup_label(runner: "object", label: str,
                  container_id: str = "") -> None:
    """ID 精确 rm -f，再按 label 兜底清扫。"""
    spec = f"simpraxis-agentic-run={label}"
    if container_id:
        _quiet(runner, ["docker", "rm", "-f", container_id])
    ps = _quiet(runner, ["docker", "ps", "-q", "--filter", f"label={spec}"])
    if ps is not None and getattr(ps, "returncode", 1) == 0:
        ids = (getattr(ps, "stdout", b"") or b"").decode(
            "utf-8", "replace").split()
        if ids:
            _quiet(runner, ["docker", "rm", "-f", *ids])


def docker_create_start(runner: "object", create_cmd: list[str], *,
                        cwd: Path, label: str,
                        timeout_s: int) -> CommandResult:
    """create/start 分离：stdout/stderr 只取 start -a 的容器输出。

    create 失败原样返回（rc!=0）；start 超时按容器 ID 精确清理、label 兜底。
    """
    def _r(cmd: list[str], timeout: int) -> "object":
        return runner(cmd, cwd=str(cwd), timeout=timeout,
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE)

    try:
        created = _r(create_cmd, 30)
    except subprocess.TimeoutExpired as exc:
        cleanup_label(runner, label)
        return CommandResult(124, exc.stdout or b"", exc.stderr or b"",
                             True, create_cmd, "sibling_container")
    cid = (created.stdout or b"").decode("utf-8", "replace").strip()
    if created.returncode != 0 or not cid:
        return CommandResult(created.returncode or 125,
                             created.stdout or b"", created.stderr or b"",
                             False, create_cmd, "sibling_container")
    try:
        proc = _r(["docker", "start", "-a", "-i", cid], timeout_s + 10)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr,
                             False, create_cmd, "sibling_container")
    except subprocess.TimeoutExpired as exc:
        cleanup_label(runner, label, cid)
        return CommandResult(124, exc.stdout or b"", exc.stderr or b"",
                             True, create_cmd, "sibling_container")
    except Exception:
        # 非超时的 start 失败同样不能留 Created 态孤儿（create 已成功、
        # --rm 只在 start 后生效）：ID 精确清扫后原样抛。
        cleanup_label(runner, label, cid)
        raise


@dataclass
class SpikeWallBackend:
    """两面墙各自的模型执行后端（sibling_container 形态）。"""

    wall: str
    freeze_dir: "Path | None" = None
    runner: "object" = subprocess.run

    form = "sibling_container"
    nonprod = False

    def __post_init__(self) -> None:
        if self.wall not in ("wallA", "wallB"):
            raise ValueError(f"未知墙：{self.wall}")
        for p in (VENV_LIBERO, MAIN_VENV):
            if not Path(p).is_dir():
                raise RuntimeError(f"冻结快照挂载源不存在：{p}")
        if self.wall == "wallA":
            if not Path(LIBERO_TOUR).is_dir():
                raise RuntimeError(f"LIBERO tour 快照不存在：{LIBERO_TOUR}")
            gate = (self.freeze_dir or freeze_root()) / "gate_env"
            if not (gate / "libero_cfg" / "config.yaml").is_file():
                raise RuntimeError(
                    f"gate_env 未冻结：{gate}（先跑 freeze_wall_a.py）")
            if not (gate / "pyshim" / "libero").is_symlink():
                raise RuntimeError(
                    f"pyshim/libero 必须是冻结软链：{gate}/pyshim/libero")

    def _mounts(self, cwd: Path) -> list[tuple[str, str, str]]:
        mounts = [
            (str(Path(cwd).resolve()), "/work", "rw"),
            (VENV_LIBERO, "/opt/venv", "ro"),
            (MAIN_VENV, MAIN_VENV, "ro"),
        ]
        mounts.extend(WALL_MOUNTS[self.wall])
        if self.wall == "wallA":
            mounts.append(
                (str((self.freeze_dir or freeze_root()) / "gate_env"),
                 "/opt/gate", "ro"))
        _check_no_docker_sock(mounts)
        return mounts

    def create_argv(self, argv: list[str], *, cwd: Path,
                    env: "dict[str,str] | None",
                    label: str) -> list[str]:
        cmd = list(_BASE_DOCKER)
        cmd.extend(["--label", f"simpraxis-agentic-run={label}"])
        for src, dst, mode in self._mounts(Path(cwd)):
            cmd.extend(["-v", f"{src}:{dst}:{mode}"])
        merged = dict(_FIXED_ENV)
        merged.update(WALL_ENV[self.wall])
        for k, v in sorted(merged.items()):
            cmd.extend(["-e", f"{k}={v}"])
        # 模型请求 env 仅允许白名单键（产品 _validate_env 已在工具层拦过，
        # 这里不重复实现：工具层是强制链，本后端只做二次 fail-closed 断言）。
        for key in (env or {}):
            if key in WALL_ENV[self.wall] or key in _FIXED_ENV:
                raise ValueError(f"模型不可覆盖固定 env：{key}")
        for k, v in sorted((env or {}).items()):
            cmd.extend(["-e", f"{k}={v}"])
        cmd.append(IMAGE)
        cmd.extend(["--"])
        cmd.extend(argv)
        return cmd

    def run(self, argv: list[str], *, cwd: Path,
            env: "dict[str,str] | None" = None,
            timeout_s: int = DEFAULT_TIMEOUT_S) -> CommandResult:
        label = f"t5-{self.wall}-{uuid.uuid4().hex}"
        create_cmd = self.create_argv(list(argv), cwd=Path(cwd), env=env,
                                      label=label)
        return docker_create_start(self.runner, create_cmd, cwd=Path(cwd),
                                   label=label, timeout_s=timeout_s)


def score_wall_a(workdir: Path, gate_dir: Path, freeze_dir: "Path | None" = None,
                 *, runner: "object" = subprocess.run,
                 timeout_s: int = 300) -> CommandResult:
    """一次性评分容器（loop 终局后）：挂 /opt/inst，跑冻结 runner。

    ``gate_dir`` 是仓内 wall_a 闸件目录（挂 /opt/inst）；``freeze_dir``
    是冻结根（其 gate_env 挂 /opt/gate），缺省取环境变量推导的冻结根。
    """
    workdir = Path(workdir).resolve()
    gate_env_dir = (Path(freeze_dir) if freeze_dir else freeze_root()) / "gate_env"
    score_dir = workdir / ".score"
    score_dir.mkdir(parents=True, exist_ok=True)
    label = f"t5-wallA-score-{uuid.uuid4().hex}"
    cmd = list(_BASE_DOCKER)
    cmd.extend(["--label", f"simpraxis-agentic-run={label}"])
    mounts = [
        (str(workdir), "/work", "rw"),
        (VENV_LIBERO, "/opt/venv", "ro"),
        (MAIN_VENV, MAIN_VENV, "ro"),
        (LIBERO_TOUR, "/opt/libero_tour", "ro"),
        (str(gate_env_dir), "/opt/gate", "ro"),
        (str(gate_dir), "/opt/inst", "ro"),
    ]
    _check_no_docker_sock(mounts)
    for src, dst, mode in mounts:
        cmd.extend(["-v", f"{src}:{dst}:{mode}"])
    for k, v in sorted({**_FIXED_ENV, **WALL_ENV["wallA"]}.items()):
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend([
        IMAGE, "--", "/opt/venv/bin/python",
        "/opt/inst/wall_a_runner.py",
        "/work/agent_solution.py", "/work/.score",
    ])
    return docker_create_start(runner, cmd, cwd=workdir, label=label,
                               timeout_s=timeout_s)


def score_wall_b(workdir: Path, checker: Path, frozen_obj: Path,
                 *, python: str = "/mnt/venvs/simpraxis/bin/python",
                 runner: "object" = subprocess.run,
                 timeout_s: int = 300) -> CommandResult:
    """宿主侧一次性评分（主 venv 有 numpy 1.26.4/trimesh 5.1.1）。"""
    workdir = Path(workdir).resolve()
    score_dir = workdir / ".score"
    score_dir.mkdir(parents=True, exist_ok=True)
    cmd = [python, str(checker), str(workdir / "agent_solution.py"),
           str(frozen_obj), str(workdir), str(score_dir)]
    proc = runner(cmd, cwd=str(workdir), timeout=timeout_s,
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr,
                         False, cmd, "host_main_venv")
