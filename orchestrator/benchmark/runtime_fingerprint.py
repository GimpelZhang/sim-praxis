"""Runtime Fingerprint：有效执行环境身份（Plan Task 2 / T2.1–T2.7）。

**要解决的问题**：Agent 看到的环境 ≠ 真正执行解题代码的环境。历史事故里
「宿主 mujoco 3.15.0 冒充容器内 2.3.7」这类错配不会报错，只会让结论失真。
本模块在**真正执行任务的那个解释器/容器内**采集六段身份，并用确定性序列化
把它钉成一个 sha256；任务运行前用 :func:`compare` 比对
``Task Runtime Requirement == Actual Runtime Identity``，不等即
``BLOCKED_RUNTIME_MISMATCH``（不变量 4）。

六段（路线图 §6.3 A–F）：``repository / container / python / mount / config /
simulator``。

**四条纪律**：

1. **禁止宿主冒充容器**（不变量 1）：``HostBackend`` 的 ``container`` 段显式
   ``kind=host`` 且 ``image_digest/image_id/container_id/container_python``
   一律 ``None``；只有 ``ContainerBackend`` 才填镜像身份（对应
   ``runtime_fingerprint.v1.schema.json`` 的 ``allOf`` if/then）。
2. **确定性**（D2-4）：``json.dumps(sort_keys=True, separators=(",",":"),
   ensure_ascii=False)``，指纹本体不含时间戳/随机量/secret。一次性 ``--rm``
   容器的 ``container_id`` 每次随机，故**恒记为 None**——容器身份由
   ``image_digest``（不可变）承担，否则同一环境两次采集 sha 不同，指纹就
   失去复现判据的意义。时间戳只进旁路 ``capture_meta.json``。
3. **冻结不覆盖**（不变量 3）：``write_fingerprint()`` 用
   ``O_CREAT|O_EXCL|O_WRONLY, 0o600``，已存在即 ``FingerprintError``。
4. **不进 Prompt**（不变量 6）：本模块只落盘，不参与 prompt 渲染。

不重写 ``orchestrator/agentic/env_spec.py``（不变量 5）；``env_spec`` 仍是
给模型看的 concise view，完整身份只给审计与复现。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestrator.benchmark import task_spec as ts
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

FINGERPRINT_SCHEMA_ID = "simpraxis.runtime_fingerprint.v1"
FINGERPRINT_NAME = "runtime_fingerprint.json"
SHA256_NAME = "runtime_fingerprint.sha256"
META_NAME = "capture_meta.json"

SEGMENTS = ("repository", "container", "python", "mount", "config", "simulator")

PROBE_MARKER = "__SIMPRAXIS_FINGERPRINT__"
PROBE_TIMEOUT_S = 120
DOCKER_TIMEOUT_S = 300

#: 允许透传进探针的环境变量名（只记名字，值绝不进指纹；凭据一律不传）
ENV_PASSTHROUGH = ("MUJOCO_GL", "PYOPENGL_PLATFORM", "LANG", "LC_ALL", "PATH",
                   "HOME")

#: 采集后要清掉的环境变量前缀（凭据/代理；硬规则 4）
_STRIP_PREFIXES = ("ANTHROPIC_", "ARK_", "AWS_", "AZURE_", "OPENAI_",
                   "DASHSCOPE_", "HF_TOKEN", "GIT_ASKPASS")
_STRIP_NAMES = {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "all_proxy", "http_proxy", "https_proxy", "no_proxy"}


class FingerprintError(RuntimeError):
    """指纹无法生成/无法写入（一律 fail-closed，不得兜底编造）。"""


# --------------------------------------------------------------------------
# 探针源码（D2-2：内联常量，不写临时文件）
# --------------------------------------------------------------------------

_PROBE_SRC = r'''
import json, os, platform, sys
from importlib import metadata

_PKGS = ("mujoco", "robosuite", "bddl", "libero", "numpy", "torch", "trimesh")
#: 固定观测集之外的包名由调用方经 argv 传入（``python -I -c <src> <pkg> ...``）：
#: 探针必须实测 task.yaml.runtime.packages 里**声明的每一个**包（``compare()``
#: 按声明名逐个对账），否则声明了固定集之外的包（如 R1 的 PyYAML/jsonschema）
#: 会恒报「<未安装>」——那是探针覆盖缺口，不是真实的环境不符。
_pkgs = {}
for _name in tuple(_PKGS) + tuple(_a for _a in sys.argv[1:] if _a):
    try:
        _pkgs[_name] = metadata.version(_name)
    except Exception:
        _pkgs[_name] = None

_cuda = None
_torch_cuda = None
try:
    import torch
    _cuda = bool(torch.cuda.is_available())
    _torch_cuda = getattr(torch.version, "cuda", None)
except Exception:
    pass

_out = {
    "argv": list(sys.argv),
    "cuda_available": _cuda,
    "cuda_runtime": _torch_cuda,
    "cwd": os.getcwd(),
    "executable": sys.executable,
    "gid": os.getgid(),
    "implementation": platform.python_implementation(),
    "kernel": platform.release(),
    "machine": platform.machine(),
    "mujoco_gl": os.environ.get("MUJOCO_GL"),
    "packages": _pkgs,
    "pyopengl_platform": os.environ.get("PYOPENGL_PLATFORM"),
    "sys_path": list(sys.path),
    "uid": os.getuid(),
    "version": "%d.%d.%d" % tuple(sys.version_info[:3]),
}
sys.stdout.write("\n" + "__SIMPRAXIS_FINGERPRINT__"
                 + json.dumps(_out, ensure_ascii=False, sort_keys=True))
'''


def probe_source() -> str:
    """探针源码（测试与审计要能逐字看到被执行的字符串）。"""
    return _PROBE_SRC


def declared_packages(spec: ts.TaskSpec) -> "tuple[str, ...]":
    """任务 ``runtime.packages`` 声明的包名（排序去重）。

    采集时必须把这份名单传给后端：探针只按名实测，没传的包在指纹里就没有
    实测值，``compare()`` 便会把整份声明报成不符（HG-EVAL-1 假 RED）。
    """
    packages = (spec.runtime or {}).get("packages") or {}
    if not isinstance(packages, Mapping):
        return ()
    return tuple(sorted({str(name) for name in packages}))


# --------------------------------------------------------------------------
# 采集后端（D2-1 双后端）
# --------------------------------------------------------------------------

def sanitized_env(source: Mapping[str, str] | None = None) -> "dict[str, str]":
    """只保留白名单变量名的值；凭据/代理前缀一律剔除。"""
    src = dict(os.environ if source is None else source)
    out: "dict[str, str]" = {}
    for name in ENV_PASSTHROUGH:
        if name in src:
            out[name] = src[name]
    for name in list(out):
        if name.startswith(_STRIP_PREFIXES) or name in _STRIP_NAMES:
            out.pop(name)
    return out


def _parse_probe_output(stdout: str, where: str) -> dict:
    if PROBE_MARKER not in stdout:
        raise FingerprintError(
            f"{where}: 探针输出里没有标记 {PROBE_MARKER}（stdout 前 400 字符："
            f"{stdout[:400]!r}）")
    payload = stdout.split(PROBE_MARKER, 1)[1].strip()
    try:
        doc = json.loads(payload.splitlines()[0] if payload else "")
    except (ValueError, IndexError) as exc:
        raise FingerprintError(f"{where}: 探针 JSON 无法解析：{exc}") from exc
    if not isinstance(doc, dict):
        raise FingerprintError(f"{where}: 探针输出必须是对象")
    return doc


@dataclass(frozen=True)
class Capture:
    """一次采集的原始结果（backend + 探针事实 + 容器身份）。"""

    backend: str
    probe: Mapping
    container: Mapping
    command: tuple
    duration_s: float
    image_name: "str | None" = None
    #: 本次采集真实使用的子进程超时（评审 W2-08：config 段必须记实测值，
    #: 不能记模块常量——构造参数 ``timeout`` 可改）。
    timeout: "float | None" = None
    #: 采集期如实降级的字段名（评审 W2-10：``image_id`` 取不到时不可静默为
    #: null 而无人知晓）。
    degraded: tuple = ()

    def meta(self) -> dict:
        return {
            "backend": self.backend,
            "command": list(self.command),
            "duration_s": round(self.duration_s, 3),
            "ts_iso": _now_micro(),
            "image_name": self.image_name,
            "timeout_s": self.timeout,
            "degraded": list(self.degraded),
        }


class HostBackend:
    """主 venv 采集（M1）：``python -I -c <probe>``，隔离用户 site 与 PYTHONPATH。"""

    name = "host"

    def __init__(self, python: str | None = None, timeout: int = PROBE_TIMEOUT_S,
                 packages: Iterable[str] = ()):
        self.python = python or sys.executable
        self.timeout = timeout
        #: 任务声明的包名（:func:`declared_packages`）；探针按名逐个实测
        self.packages = tuple(str(p) for p in packages if str(p))

    def command(self) -> "list[str]":
        return [self.python, "-I", "-c", _PROBE_SRC, *self.packages]

    def capture(self) -> Capture:
        cmd = self.command()
        started = time.monotonic()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=self.timeout, env=sanitized_env(),
                                 cwd=str(ts.BENCHMARK_ROOT.parents[1]))
        except (OSError, subprocess.SubprocessError) as exc:
            raise FingerprintError(f"宿主探针启动失败：{exc}") from exc
        if res.returncode != 0:
            raise FingerprintError(
                f"宿主探针 rc={res.returncode}：{(res.stderr or '')[-2000:]}")
        probe = _parse_probe_output(res.stdout, "host")
        container = {
            "backend": "host",
            "image_name": None,
            "image_digest": None,
            "image_id": None,
            "container_id": None,
            "container_python": None,
            "uid": int(probe.get("uid", -1)),
            "gid": int(probe.get("gid", -1)),
            "kernel": str(probe.get("kernel", "")),
            "gpu_visible": bool(probe.get("cuda_available") or False),
            "cuda_runtime": probe.get("cuda_runtime"),
            "egl_available": None,
        }
        return Capture("host", probe, container, tuple(cmd),
                       time.monotonic() - started, timeout=self.timeout)


class ContainerBackend:
    """容器采集（L1）：``docker run --rm --network none <image@digest> python3 -c``。

    默认**不传** ``--gpus``（渲染由 Task 6 显式决定，D6-3）；因此容器内
    ``torch.cuda.is_available()`` 如实为 False——这是「没给 GPU」的事实，
    不是「没有 GPU」的结论。
    """

    name = "docker"

    def __init__(self, image: str, *, image_name: str | None = None,
                 timeout: int = DOCKER_TIMEOUT_S, docker: str = "docker",
                 packages: Iterable[str] = ()):
        if "@sha256:" not in image:
            raise FingerprintError(
                f"容器镜像必须按 digest 调用（image@sha256:...），收到 {image!r}")
        self.image = image
        self.image_name = image_name or image.split("@", 1)[0]
        self.timeout = timeout
        self.docker = docker
        #: 任务声明的包名（:func:`declared_packages`）；探针按名逐个实测
        self.packages = tuple(str(p) for p in packages if str(p))

    def command(self) -> "list[str]":
        return [self.docker, "run", "--rm", "--network", "none",
                "--label", "simpraxis.benchmark.fingerprint=1",
                self.image, "python3", "-c", _PROBE_SRC, *self.packages]

    def _image_id(self) -> "str | None":
        try:
            res = subprocess.run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}",
                 self.image],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        value = res.stdout.strip()
        return value or None

    def capture(self) -> Capture:
        cmd = self.command()
        started = time.monotonic()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FingerprintError(f"容器探针启动失败：{exc}") from exc
        if res.returncode != 0:
            raise FingerprintError(
                f"容器探针 rc={res.returncode}：{(res.stderr or '')[-2000:]}")
        probe = _parse_probe_output(res.stdout, "container")
        digest = self.image.split("@", 1)[1]
        image_id = self._image_id()
        container = {
            "backend": "docker",
            "image_name": self.image_name,
            "image_digest": digest,
            "image_id": image_id,
            # 一次性 --rm 容器的 id 每次随机：恒记 None，身份由 digest 承担（D2-4）
            "container_id": None,
            "container_python": str(probe.get("executable") or ""),
            "uid": int(probe.get("uid", -1)),
            "gid": int(probe.get("gid", -1)),
            "kernel": str(probe.get("kernel", "")),
            "gpu_visible": bool(probe.get("cuda_available") or False),
            "cuda_runtime": probe.get("cuda_runtime"),
            "egl_available": None,
        }
        return Capture("docker", probe, container, tuple(cmd),
                       time.monotonic() - started, image_name=self.image_name,
                       timeout=self.timeout,
                       degraded=(() if image_id else ("image_id",)))


# --------------------------------------------------------------------------
# 组装六段
# --------------------------------------------------------------------------

def _now_micro() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def _git(args: "list[str]", cwd: Path, timeout: int = 60) -> "str | None":
    try:
        res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                             text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def repository_segment(spec: ts.TaskSpec, *, manifest_version: int = 1,
                       repo_root: Path | None = None,
                       kb_root: Path | None = None) -> dict:
    """``repository`` 段：引擎/KB 的 HEAD 与脏标记 + 任务定义的 sha256。"""
    root = Path(repo_root) if repo_root is not None else ts.BENCHMARK_ROOT.parents[1]
    engine_head = _git(["rev-parse", "HEAD"], root) or ""
    porcelain = _git(["status", "--porcelain"], root) or ""
    kb = Path(kb_root) if kb_root is not None else Path(
        os.environ.get("KB_ROOT")
        or (Path.home() / "embodied-ai-sim-knowledge-base"))
    kb_head = _git(["rev-parse", "HEAD"], kb) if kb.is_dir() else None
    kb_dirty = None
    if kb_head is not None:
        kb_dirty = bool(_git(["status", "--porcelain"], kb) or "")
    return {
        "engine_head": engine_head,
        "engine_dirty": bool(porcelain),
        "kb_head": kb_head,
        "kb_dirty": kb_dirty,
        "benchmark_version": int(manifest_version),
        "task_version": int(spec.version),
        "task_spec_sha256": spec.task_file_sha256,
        "criteria_sha256": spec.criteria_sha256,
        "assets_manifest_sha256": spec.inputs.get("assets_manifest_sha256"),
        "skills_manifest_sha256": spec.inputs.get("skills_manifest_sha256"),
    }


def _config_segment(capture: Capture) -> dict:
    probe = capture.probe
    env = {}
    for name in ("MUJOCO_GL", "PYOPENGL_PLATFORM"):
        value = probe.get(name.lower())
        if isinstance(value, str) and value:
            env[name] = value
    return {
        "env_passthrough": sorted(env),
        "env": env,
        "network_mode": "none" if capture.backend == "docker" else "host",
        # 评审 W2-08：记本次采集**实际**使用的超时（后端构造参数可改），
        # 模块常量只在 Capture 未带 timeout 时兜底。
        "timeout_s": (capture.timeout if capture.timeout is not None
                      else (DOCKER_TIMEOUT_S if capture.backend == "docker"
                            else PROBE_TIMEOUT_S)),
        "cpu_limit": None,
        "memory_limit": None,
        "gpu": {"name": None, "total_mib": None},
    }


def _simulator_segment(spec: ts.TaskSpec, capture: Capture,
                       override: Mapping | None = None) -> "dict | None":
    """``simulator`` 段。R1（document_only）整段为 null。"""
    if override is not None:
        return dict(override)
    packages = capture.probe.get("packages") or {}
    runtime_id = str(spec.runtime.get("runtime_id") or "")
    if runtime_id == "document_only":
        return None
    if capture.backend == "docker":
        name, version = "libero", packages.get("libero")
        api = "libero.libero.envs.OffScreenRenderEnv"
    else:
        name, version = "mujoco", packages.get("mujoco")
        api = "mujoco.MjModel/MjData"
    if not version:
        raise FingerprintError(f"simulator 版本采集失败（{name} 不可见）")
    return {
        "name": name,
        "version": str(version),
        "api_identity": api,
        # 观测/动作契约由出生验证（B3）与运行实测回填，Task 2 只登记身份
        "observation_contract": None,
        "action_contract": None,
        "scene_identity": f"task:{spec.task_id}",
    }


def build_fingerprint(spec: ts.TaskSpec, capture: Capture, *,
                      mounts: Iterable[Mapping] | None = None,
                      simulator: Mapping | None = None,
                      manifest_version: int = 1,
                      repo_root: Path | None = None,
                      kb_root: Path | None = None) -> dict:
    """把一次采集 + 任务定义组装成完整指纹（含 ``fingerprint_sha256``）。"""
    probe = capture.probe
    doc: "dict[str, Any]" = {
        "schema": FINGERPRINT_SCHEMA_ID,
        "captured_in": "container" if capture.backend == "docker" else "host",
        "task_id": spec.task_id,
        "runtime_id": str(spec.runtime.get("runtime_id") or ""),
        "repository": repository_segment(
            spec, manifest_version=manifest_version, repo_root=repo_root,
            kb_root=kb_root),
        "container": dict(capture.container),
        "python": {
            "executable": str(probe.get("executable") or ""),
            "version": str(probe.get("version") or ""),
            "implementation": str(probe.get("implementation") or ""),
            "sys_path": [str(p) for p in probe.get("sys_path") or ()],
            "workdir": str(probe.get("cwd") or ""),
            "packages": {str(k): str(v) for k, v in
                         (probe.get("packages") or {}).items() if v},
            "argv": [str(a) for a in probe.get("argv") or ()],
        },
        # 评审 W2-09：同一挂载集合不得因传入顺序不同而换指纹——按
        # (destination, source) 排序后再序列化（D2-4 确定性）。
        "mount": {"entries": [mount_entry(m) for m in sorted(
            (mounts or ()), key=lambda m: (str(m.get("destination")),
                                           str(m.get("source"))))]},
        "config": _config_segment(capture),
        "simulator": _simulator_segment(spec, capture, simulator),
    }
    doc["fingerprint_sha256"] = fingerprint_sha256(doc)
    validate_fingerprint(doc)
    return doc


def mount_entry(item: Mapping) -> dict:
    """挂载身份（D2-5）：只记 ``src/dst/mode/ro/source_sha256``，不记 inode。"""
    missing = [k for k in ("source", "destination", "mode") if k not in item]
    if missing:
        raise FingerprintError(f"mount entry 缺字段：{missing}")
    mode = str(item["mode"])
    if mode not in ("ro", "rw"):
        raise FingerprintError(f"mount mode 必须是 ro/rw：{mode!r}")
    return {
        "source": str(item["source"]),
        "destination": str(item["destination"]),
        "mode": mode,
        "readonly": mode == "ro",
        "source_sha256": item.get("source_sha256"),
    }


# --------------------------------------------------------------------------
# 哈希、校验、落盘
# --------------------------------------------------------------------------

def fingerprint_sha256(doc: Mapping) -> str:
    """对**除 ``fingerprint_sha256`` 自身以外**的 payload 取 sha256。"""
    payload = {k: v for k, v in doc.items() if k != "fingerprint_sha256"}
    return ts.sha256_bytes(ts.canonical_json(payload).encode("utf-8"))


def validate_fingerprint(doc: Mapping, *,
                         schema_path: Path | None = None) -> dict:
    """按 ``runtime_fingerprint.v1.schema.json`` 校验；不合规即 fail-closed。"""
    import jsonschema

    path = Path(schema_path) if schema_path is not None else (
        ts.BENCHMARK_ROOT / "schemas" / "runtime_fingerprint.v1.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(doc)),
                    key=lambda e: (list(map(str, e.absolute_path)), e.message))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise FingerprintError(
            f"指纹不合规（{len(errors)} 处）：{where}: {first.message}")
    declared = doc.get("fingerprint_sha256")
    recomputed = fingerprint_sha256(doc)
    if declared != recomputed:
        raise FingerprintError(
            f"fingerprint_sha256 不符：声明 {declared}，重算 {recomputed}")
    return dict(doc)


def write_fingerprint(directory: "str | Path", doc: Mapping,
                      capture: Capture | None = None) -> dict:
    """落盘三件（D2-4）：冻结本体 + sha256 + 旁路 capture_meta。

    本体用 ``O_CREAT|O_EXCL|O_WRONLY, 0o600``（不变量 3）：已存在即拒绝，
    绝不静默覆盖已冻结的环境身份。
    """
    validate_fingerprint(doc)
    out = ensure_private_dir(directory)
    target = out / FINGERPRINT_NAME
    body = ts.canonical_json(dict(doc)) + "\n"
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FingerprintError(
            f"指纹已冻结，拒绝覆盖：{target}（需要新身份请换目录）") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        raise FingerprintError(f"指纹写入失败：{exc}") from exc

    digest = str(doc["fingerprint_sha256"])
    atomic_write_text(out / SHA256_NAME, f"{digest}  {FINGERPRINT_NAME}\n", 0o600)
    if capture is not None:
        meta = capture.meta()
        meta["fingerprint_sha256"] = digest
        meta["schema"] = "simpraxis.benchmark_capture_meta.v1"
        atomic_write_text(out / META_NAME,
                          ts.canonical_json(meta) + "\n", 0o600)
    return {
        "fingerprint_path": str(target),
        "sha256_path": str(out / SHA256_NAME),
        "meta_path": str(out / META_NAME) if capture is not None else None,
        "fingerprint_sha256": digest,
    }


# --------------------------------------------------------------------------
# 比对（不变量 4）：Task Runtime Requirement vs Actual Runtime Identity
# --------------------------------------------------------------------------

class MismatchCode:
    """十类 mismatch（D2-6）。``compare()`` 返回**全部**不符项，不只第一处。"""

    PYTHON_VERSION = "python_version"
    PYTHON_EXECUTABLE = "python_executable"
    MUJOCO_VERSION = "mujoco_version"
    PACKAGE_VERSION = "package_version"
    IMAGE_DIGEST = "image_digest"
    WORKDIR = "workdir"
    MOUNT_MISSING = "mount_missing"
    MOUNT_READONLY = "mount_readonly"
    ACTION_DIM = "action_dim"
    OBS_SCHEMA = "obs_schema"
    SKILL_MANIFEST = "skill_manifest"
    ASSET_MANIFEST = "asset_manifest"
    #: 评审 W2-04：要求声明了 action_dim/obs_keys、但运行期没有实测证据时，
    #: 不再静默通过（缺证据 ≠ 相符）。
    OBSERVATION_MISSING = "observation_missing"


#: 十类（P0-AC-05「均能识别」的机判清单；PACKAGE_VERSION 是附加细分）
REQUIRED_CODES = (
    MismatchCode.PYTHON_VERSION,
    MismatchCode.MUJOCO_VERSION,
    MismatchCode.IMAGE_DIGEST,
    MismatchCode.WORKDIR,
    MismatchCode.MOUNT_MISSING,
    MismatchCode.MOUNT_READONLY,
    MismatchCode.ACTION_DIM,
    MismatchCode.OBS_SCHEMA,
    MismatchCode.SKILL_MANIFEST,
    MismatchCode.ASSET_MANIFEST,
)


#: 生产接线（评审 W2-03 的诚实边界）：HG-EVAL-1 的 requirement 来自冻结
#: ``task.yaml.runtime`` 段，该段 schema 只声明 ``runtime_id / image_digest /
#: python / packages`` 四键（``additionalProperties: false``），所以生产链路
#: 能真正对上的只有解释器、包版本、镜像 digest 这几类。
#: 评审 W2-03 修复：``runtime_id`` 不参与比对（它是身份标签不是要求），故生产
#: 生效的要求键恰为 ``PRODUCTION_REQUIREMENT_KEYS``——机判见
#: ``partition_problems()`` 与 ``test_w2_03_*``（冻结 schema ↔ 常量逐键对齐）。
PRODUCTION_REQUIREMENT_KEYS = ("image_digest", "packages", "python")

PRODUCTION_WIRED_CODES = (
    MismatchCode.PYTHON_EXECUTABLE,
    MismatchCode.MUJOCO_VERSION,
    MismatchCode.PACKAGE_VERSION,
    MismatchCode.IMAGE_DIGEST,
)

#: 仅 **API 能力**（``compare()`` 支持、但当前生产调用方传不进要求）：这九类
#: 在真实评测里恒不触发（``observation_missing`` 的唯一触发条件同样是要求侧声明
#: ``action_dim``/``obs_keys``，而冻结 schema 不收这两个键）；action_dim/obs 的
#: 一致性由 Task 6 的域门（env_probe ↔ 指纹 simulator 段）承担。
#: **不得表述为「十类生产全覆盖」**——计划 P0-AC-05 的「均能识别」只在 API/单测
#: 层面成立，边界见 Phase 2.8.0 Complete §9.3。两张清单由 ``partition_problems()``
#: 保证**恰好划分**全部 ``MismatchCode``（不重不漏）。
API_ONLY_CODES = (
    MismatchCode.PYTHON_VERSION,
    MismatchCode.WORKDIR,
    MismatchCode.MOUNT_MISSING,
    MismatchCode.MOUNT_READONLY,
    MismatchCode.ACTION_DIM,
    MismatchCode.OBS_SCHEMA,
    MismatchCode.OBSERVATION_MISSING,
    MismatchCode.SKILL_MANIFEST,
    MismatchCode.ASSET_MANIFEST,
)


def all_mismatch_codes() -> "tuple[str, ...]":
    """``MismatchCode`` 声明的全部码（按定义顺序，供划分机判）。"""
    return tuple(value for name, value in vars(MismatchCode).items()
                 if not name.startswith("_") and isinstance(value, str))


def partition_problems() -> "list[str]":
    """评审 W2-03：生产接线 / 仅 API 两张清单必须恰好划分全部 mismatch 码。

    返回问题清单（空列表才合格）；重复归属、漏项、未声明的码都算问题。
    """
    universe = set(all_mismatch_codes())
    wired = list(PRODUCTION_WIRED_CODES)
    api_only = list(API_ONLY_CODES)
    problems: "list[str]" = []
    dup = sorted(set(wired) & set(api_only))
    if dup:
        problems.append(f"同时登记为生产接线与仅 API：{dup}")
    if len(set(wired)) != len(wired):
        problems.append("生产接线清单内部重复")
    if len(set(api_only)) != len(api_only):
        problems.append("仅 API 清单内部重复")
    missing = sorted(universe - set(wired) - set(api_only))
    if missing:
        problems.append(f"两张清单都未登记的码：{missing}")
    unknown = sorted((set(wired) | set(api_only)) - universe)
    if unknown:
        problems.append(f"清单里出现未声明的码：{unknown}")
    return problems


@dataclass(frozen=True)
class Mismatch:
    code: str
    detail: str

    def to_json(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def _requirement_of(requirement: Mapping, spec: ts.TaskSpec | None) -> dict:
    """把 ``task.yaml.runtime``（或完整 requirement）归一成比对输入。"""
    req = dict(requirement)
    if spec is not None:
        req.setdefault("runtime_id", spec.runtime.get("runtime_id"))
        req.setdefault("image_digest", spec.runtime.get("image_digest"))
    packages = req.get("packages")
    if isinstance(packages, Mapping):
        req["packages"] = {str(k): str(v) for k, v in packages.items()}
    return req


def compare(requirement: Mapping, fingerprint: Mapping, *,
            spec: ts.TaskSpec | None = None,
            observed: Mapping | None = None) -> "list[Mismatch]":
    """比对一个任务要求与一份实际指纹；返回全部不符项（空列表才允许继续）。

    ``requirement`` 可含：``packages`` / ``python_version`` / ``image_digest`` /
    ``workdir`` / ``mounts``（``[{destination, mode}]``）/ ``action_dim`` /
    ``obs_keys`` / ``assets_manifest_sha256`` / ``skills_manifest_sha256``。
    ``observed`` 是运行期实测（``env_probe.json`` 的 ``action_dim``/``obs_keys``）；
    要求声明了观测契约而实测缺失时返回 ``observation_missing``（不得静默通过）。

    **生产接线边界（评审 W2-03）**：HG-EVAL-1 只传冻结 ``task.yaml.runtime``
    的四键，因此实际生效的只有 ``PRODUCTION_WIRED_CODES``；``API_ONLY_CODES``
    里各类仅是该函数的 API 能力，流水线恒不触发（详见 Complete 登记）。
    """
    req = _requirement_of(requirement, spec)
    out: "list[Mismatch]" = []
    fp_python = fingerprint.get("python") or {}
    fp_packages = fp_python.get("packages") or {}
    fp_container = fingerprint.get("container") or {}
    fp_repo = fingerprint.get("repository") or {}

    want_version = req.get("python_version")
    if want_version:
        got = str(fp_python.get("version") or "")
        if not (got == str(want_version) or got.startswith(f"{want_version}.")):
            out.append(Mismatch(MismatchCode.PYTHON_VERSION,
                                f"要求 python {want_version}，实测 {got or '<缺>'}"))

    # 解释器路径：宿主路径不得冒充容器路径（HG-L1-1 的机判面）
    want_python = req.get("python")
    if want_python and str(want_python) != str(fp_python.get("executable") or ""):
        out.append(Mismatch(
            MismatchCode.PYTHON_EXECUTABLE,
            f"要求解释器 {want_python}，实测 {fp_python.get('executable')!r}"))

    for name, want in sorted((req.get("packages") or {}).items()):
        got = fp_packages.get(name)
        if got is None or str(got) != str(want):
            code = (MismatchCode.MUJOCO_VERSION if name == "mujoco"
                    else MismatchCode.PACKAGE_VERSION)
            out.append(Mismatch(code,
                                f"要求 {name}=={want}，实测 "
                                f"{got if got is not None else '<未安装>'}"))

    want_image = req.get("image_digest")
    got_image = fp_container.get("image_digest")
    if want_image != got_image:
        out.append(Mismatch(
            MismatchCode.IMAGE_DIGEST,
            f"要求镜像 {want_image!r}，实测 {got_image!r}"
            f"（backend={fp_container.get('backend')!r}）"))

    want_workdir = req.get("workdir")
    if want_workdir:
        got = str(fp_python.get("workdir") or "")
        if got != str(want_workdir):
            out.append(Mismatch(MismatchCode.WORKDIR,
                                f"要求 workdir {want_workdir}，实测 "
                                f"{got or '<缺>'}"))

    got_mounts = {str(e.get("destination")): e
                  for e in (fingerprint.get("mount") or {}).get("entries") or ()}
    for item in req.get("mounts") or ():
        dst = str(item.get("destination"))
        actual = got_mounts.get(dst)
        if actual is None:
            out.append(Mismatch(MismatchCode.MOUNT_MISSING,
                                f"要求挂载 {dst}，实测无此挂载"))
            continue
        want_ro = str(item.get("mode", "ro")) == "ro"
        if want_ro and not actual.get("readonly"):
            out.append(Mismatch(MismatchCode.MOUNT_READONLY,
                                f"要求 {dst} 只读，实测 {actual.get('mode')!r}"))

    obs = dict(observed or {})
    want_dim = req.get("action_dim")
    if want_dim is not None:
        if obs.get("action_dim") is None:
            # 评审 W2-04：要求已声明、实测缺失 ⇒ 显式不符（缺证据 ≠ 相符）。
            out.append(Mismatch(
                MismatchCode.OBSERVATION_MISSING,
                f"要求 action_dim={want_dim}，但运行时无实测观测证据"))
        elif int(obs["action_dim"]) != int(want_dim):
            out.append(Mismatch(MismatchCode.ACTION_DIM,
                                f"要求 action_dim={want_dim}，实测 "
                                f"{obs['action_dim']}"))
    want_obs = req.get("obs_keys")
    if want_obs is not None:
        if obs.get("obs_keys") is None:
            out.append(Mismatch(
                MismatchCode.OBSERVATION_MISSING,
                f"要求观测键 {list(want_obs)}，但运行时无实测观测证据"))
        else:
            missing = [k for k in want_obs if k not in set(obs["obs_keys"])]
            if missing:
                out.append(Mismatch(MismatchCode.OBS_SCHEMA,
                                    f"观测缺键：{missing}"))

    for key, code in (("skills_manifest_sha256", MismatchCode.SKILL_MANIFEST),
                      ("assets_manifest_sha256", MismatchCode.ASSET_MANIFEST)):
        want = req.get(key)
        if want is None:
            continue
        got = fp_repo.get(key)
        if got != want:
            out.append(Mismatch(code, f"要求 {key}={want}，实测 {got!r}"))

    return out


def require_match(requirement: Mapping, fingerprint: Mapping, *,
                  spec: ts.TaskSpec | None = None,
                  observed: Mapping | None = None) -> None:
    """不符即 ``BLOCKED_RUNTIME_MISMATCH``——调用方不得继续执行任务。"""
    mismatches = compare(requirement, fingerprint, spec=spec, observed=observed)
    if mismatches:
        detail = "; ".join(f"{m.code}: {m.detail}" for m in mismatches)
        raise FingerprintError(f"BLOCKED_RUNTIME_MISMATCH: {detail}")


def load_fingerprint(path: "str | Path") -> dict:
    """读一份已冻结指纹并验 sha（读侧也要 fail-closed）。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise FingerprintError(f"{path}: 指纹必须是对象")
    return validate_fingerprint(doc)


def capture_and_build(spec: ts.TaskSpec, *, image: str | None = None,
                      backend: str | None = None, python: str | None = None,
                      image_name: str | None = None,
                      mounts: Iterable[Mapping] | None = None,
                      manifest_version: int = 1) -> "tuple[dict, Capture]":
    """按任务定义选后端并组装指纹（``image`` 覆盖 ``runtime.image_digest``）。"""
    digest = image or spec.runtime.get("image_digest")
    chosen = backend or ("docker" if digest else "host")
    packages = declared_packages(spec)
    if chosen == "docker":
        if not digest:
            raise FingerprintError("docker 后端需要 image@sha256:... 镜像引用")
        ref = digest if "@" in str(digest) else f"{spec.runtime.get('image_name') or _default_image(spec)}@{digest}"
        cap = ContainerBackend(ref, image_name=image_name,
                               packages=packages).capture()
    elif chosen == "host":
        # 评审 W2-07 / T2.1 不变量 1：任务已声明容器镜像时，禁止用宿主后端
        # 冻结指纹（宿主冒充容器）。冻结件是 O_EXCL 写的，一次误操作会把规范
        # 路径占死，只能人工清目录——所以在源头拦下。
        if spec.runtime.get("image_digest"):
            raise FingerprintError(
                "任务 %s 的 runtime 已声明镜像 %r：禁止以 host 后端冻结指纹"
                "（T2.1 不变量 1「禁止宿主冒充容器」；容器失败不得降级为宿主）"
                % (spec.task_id, spec.runtime.get("image_digest")))
        cap = HostBackend(python, packages=packages).capture()
    else:
        raise FingerprintError(f"未知后端：{chosen!r}")
    return build_fingerprint(spec, cap, mounts=mounts,
                             manifest_version=manifest_version), cap


def _default_image(spec: ts.TaskSpec) -> str:
    return {"libero_env_adapter_v1": "sim-praxis/libero:task6"}.get(
        spec.task_id, spec.task_id)


def docker_available() -> bool:
    return shutil.which("docker") is not None
