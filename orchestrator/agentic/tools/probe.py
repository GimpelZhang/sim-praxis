"""run_probe：白名单探针执行（AC-T4-5/7）。

- 只有 ``ProbeRegistry`` 内登记且 Gate1 ``probes`` 逐项授权的探针可执行；
- 网络探针（``network=True``）还须出现在 Gate1 ``network_probes``，
  默认注册表中五个平台探针全部 ``network=False``（HF_HUB_OFFLINE=1）；
- 探针 argv 由注册表固定，模型只能传名字，不能拼参数/命令；
- 执行复用 run_cmd 的后端与渲染路径（兄弟容器默认、NONPROD 本地测试形）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext
from ..schema import SchemaError, int_field, str_field, validate
from ..image_pin import SCENE_BUILD_PYTHON
from . import ToolResult
from .run import check_backend, execute_render

DEFAULT_PROBE_TIMEOUT_S = 600
MAX_PROBE_TIMEOUT_S = 1_800

# M3（2026-10-30 评审 MAJOR 返修）：探针 argv 一律用容器内绝对路径
# /probes/probe_*.py（兄弟容器把宿主 sandbox/probes 只读挂到 /probes）；
# 旧实现写相对路径 "sandbox/probes/..."，容器 WORKDIR 是 /work，必 ENOENT。
CONTAINER_PROBES_DIR = "/probes"
# 本地 NONPROD 形态不经容器：/probes/<f> 解析回仓内绝对路径。
_REPO_ROOT = Path(__file__).resolve().parents[3]
HOST_PROBES_DIR = _REPO_ROOT / "sandbox" / "probes"


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    argv: tuple[str, ...]
    network: bool = False


class ProbeRegistry:
    """探针白名单注册表：显式登记，未知名即 deny（默认五平台探针）。"""

    def __init__(self, specs: "tuple[ProbeSpec, ...] | list[ProbeSpec]"):
        table: dict[str, ProbeSpec] = {}
        for spec in specs:
            if not isinstance(spec, ProbeSpec):
                raise ValueError("ProbeRegistry 只接受 ProbeSpec")
            if spec.name in table:
                raise ValueError(f"探针重名：{spec.name}")
            if not spec.argv or not all(
                    isinstance(a, str) and a for a in spec.argv):
                raise ValueError(f"探针 {spec.name} argv 必须是非空字符串元组")
            table[spec.name] = spec
        self._specs = dict(table)

    def is_known(self, name: str) -> bool:
        return name in self._specs

    def is_network(self, name: str) -> bool:
        spec = self._specs.get(name)
        return bool(spec and spec.network)

    def get(self, name: str) -> ProbeSpec:
        return self._specs[name]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


def default_registry() -> ProbeRegistry:
    """五个平台探针（全部离线）；argv 是容器内绝对路径（M3）。

    脚本经 /probes 只读挂载入容器；--out 一律写容器 /tmp（tmpfs，模型
    不可控路径）。mujoco 用 ``--phase step``：纯物理步进、不触 GL/EGL，
    集成烟测无需渲染依赖。
    """
    return ProbeRegistry((
        ProbeSpec("mujoco_probe",
                  ("python", "/probes/probe_mujoco.py",
                   "--phase", "step",
                   "--out", "/tmp/simpraxis_probe_mujoco_step.json")),
        ProbeSpec("maniskill_probe",
                  ("python", "/probes/probe_maniskill.py",
                   "--out", "/tmp/simpraxis_probe_maniskill.json")),
        ProbeSpec("genesis_probe",
                  ("python", "/probes/probe_genesis.py",
                   "--out", "/tmp/simpraxis_probe_genesis.json")),
        ProbeSpec("genesis_fluid_probe",
                  ("python", "/probes/probe_genesis_fluid.py",
                   "--out", "/tmp/simpraxis_probe_genesis_fluid.json")),
        ProbeSpec("libero_compat_probe",
                  ("python", "/probes/probe_libero_compat.py",
                   "--out", "/tmp/simpraxis_probe_libero_compat.json")),
        # Phase 2.7 Task 1（D1-4 / AC-T1-2）：场景装配探针。镜像内 Genesis 装在
        # **系统解释器**（L-13：AGENTIC_IMAGE 是零 wheel 形态、没有 genesis），
        # 故 argv 恒用 /usr/bin/python；资产经 run.py 的 extra_ro_mounts
        # 以 `/assets:ro` 挂入，spec 在 workdir（容器 /work/scene_spec.json）。
        ProbeSpec("scene_build",
                  (SCENE_BUILD_PYTHON, "/probes/probe_scene_build.py")),
    ))


_FIELDS = (
    str_field("name", max_chars=128),
    int_field("timeout_s", default=DEFAULT_PROBE_TIMEOUT_S, minimum=1,
              maximum=MAX_PROBE_TIMEOUT_S),
)


def resolve_probe_paths(backend: Any, argv: list[str]) -> "list[str] | ToolResult":
    """M3：把 argv 里的容器路径 ``/probes/<f>`` 解析到当前后端可执行的位置。

    兄弟容器形态：要求 ``backend.probes_path`` 是真实目录（宿主源的只读挂载
    已注入），否则 fail-closed 返回 deny——**禁止**容器内 ENOENT 裸跑。
    本地 NONPROD 形态：``/probes/<f>`` → 仓内 ``sandbox/probes/<f>`` 绝对路径，
    缺件同样 deny。

    Phase 2.7 Task3（D3-5）：``render_quick_preview`` 也要按同一口径解析探针
    路径，故从 ``run_probe`` 里抽出（行为逐字不变，单一源）。
    """
    uses_probes = any(a.startswith(CONTAINER_PROBES_DIR + "/") for a in argv)
    if not uses_probes:
        return list(argv)
    if getattr(backend, "nonprod", False):
        resolved = []
        for seg in argv:
            if seg.startswith(CONTAINER_PROBES_DIR + "/"):
                host_file = HOST_PROBES_DIR / seg[len(CONTAINER_PROBES_DIR) + 1:]
                if not host_file.is_file():
                    return ToolResult.denied_result(PolicyDecision.deny(
                        f"NONPROD 探针脚本不存在：{host_file}（M3 fail-closed）"))
                seg = str(host_file)
            resolved.append(seg)
        return resolved
    probes_path = getattr(backend, "probes_path", None)
    if not probes_path or not Path(probes_path).is_dir():
        return ToolResult.denied_result(PolicyDecision.deny(
            "/probes 未挂载或宿主源目录不存在：兄弟容器形态必须注入真实 "
            "probes_path（M3 fail-closed，禁止容器内 ENOENT 裸跑）"))
    return list(argv)


def run_probe(ctx: ToolContext, **kwargs) -> ToolResult:
    try:
        args = validate(_FIELDS, kwargs)
    except SchemaError as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"schema 拒绝：{exc}"))
    # policy：白名单 + Gate1 逐项授权 + 网络逐项授权（三连）。
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("run_probe", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)

    backend = check_backend(ctx)
    if isinstance(backend, ToolResult):
        return backend

    spec = ctx.probe_registry.get(args["name"])
    argv = list(spec.argv)

    # M3：/probes 挂载/存在性 fail-closed（抽到 resolve_probe_paths，单一源）。
    resolved = resolve_probe_paths(backend, argv)
    if isinstance(resolved, ToolResult):
        return resolved
    argv = resolved

    return execute_render(ctx, argv, tag=f"run_probe:{spec.name}",
                          timeout_s=args["timeout_s"], env=None)
