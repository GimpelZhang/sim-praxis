"""RSI-R1 T3：沙盒精确数据视图与代码归属（Plan §5.4；AC16/AC17）。

两条入口：

- :func:`run_design` —— 候选 design 代码在容器里读**只读数据视图**、写临时 ``/out``。
- :func:`run_analysis` —— 分析代码同构；只能看到 ``allowed_manifest`` 列出的 run 文件。

安全面（逐条对应 §5.4）：

- 数据视图每次新建 0700 目录，只复制 manifest 列出的文件并**逐件复核 sha256**；
  不跟随符号链接（``O_NOFOLLOW`` + 逐段 lstat），拒绝硬链接、目录穿越、私有路径。
- 容器：``--network none``、非 root（65534）、``--read-only``、``--cap-drop ALL``、
  pids 64、内存 512 MiB、1 CPU、30 s 墙钟、输出 ≤1 MiB、回包 ≤4 KiB。
- 不向容器传任何凭据/网关/宿主 docker.sock；env 只留最小集合。
- 资源耗尽（超时/输出炸弹）是显式失败并留证；``finally`` 无论成功/失败/取消都
  回收**本轮自有**容器，退出后逐名核对无残留。

真实容器由注入的 runner 执行；单测用内存假 runner，不触 docker、不触网。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

#: 容器安全常量（§5.4 逐项）
WALL_CLOCK_S = 30
MEMORY_LIMIT = "512m"
PIDS_LIMIT = 64
CPU_LIMIT = "1.0"
OUTPUT_MAX_BYTES = 1 << 20          # 1 MiB
REPLY_MAX_BYTES = 4096
SOURCE_MAX_BYTES = 64 * 1024
MAX_OUTPUT_FILES = 64
MAX_VIEW_DEPTH = 4
MAX_INPUT_BYTES = 8 << 20       # 单件视图输入上限（观察文件都很小）

#: 容器内的非 root uid/gid（nobody）
CONTAINER_UID = 65534
CONTAINER_GID = 65534

#: 私有路径关键词：任何 manifest 条目都不许指向它们。
PRIVATE_PARTS = frozenset({"confirm", "private", "seeds", "seed", "quarantine",
                           "keeper"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SandboxError(RuntimeError):
    """沙盒拒绝/失败（``code`` 供证据与测试引用）。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


# ---------------------------------------------------------------------------
# 视图构造
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ViewEntry:
    """一条进入数据视图的文件：证据根下的相对路径 + 已登记 sha256。"""

    relpath: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.relpath, str) or not self.relpath:
            raise SandboxError("INPUT_INVALID", "ViewEntry.relpath 必须是非空字符串")
        if not isinstance(self.sha256, str) or not _HEX64.fullmatch(self.sha256):
            raise SandboxError("INPUT_INVALID", "ViewEntry.sha256 必须是 64 位小写 hex")


def _safe_parts(relpath: str) -> "tuple[str, ...]":
    if "\x00" in relpath:
        raise SandboxError("PATH_TRAVERSAL", "relpath 含 NUL")
    if os.path.isabs(relpath) or relpath.startswith("~"):
        raise SandboxError("PATH_TRAVERSAL", f"relpath 不得是绝对路径：{relpath!r}")
    parts = Path(relpath).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise SandboxError("PATH_TRAVERSAL", f"relpath 含穿越分量：{relpath!r}")
    if any(p in PRIVATE_PARTS for p in parts):
        raise SandboxError("PRIVATE_INPUT",
                           f"relpath 指向私有面：{relpath!r}")
    if len(parts) > 8:
        raise SandboxError("PATH_TRAVERSAL", f"relpath 层级过深：{relpath!r}")
    for p in parts:
        if not _NAME_RE.fullmatch(p):
            raise SandboxError("PATH_TRAVERSAL", f"relpath 分量非法：{p!r}")
    return parts


def _resolve_no_symlink(root: Path, parts: "tuple[str, ...]") -> Path:
    """逐段 lstat，任何符号链接/非普通文件都在打开前拒绝。"""
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise SandboxError("INPUT_MISSING", f"{current} 不存在") from None
        if stat.S_ISLNK(st.st_mode):
            raise SandboxError("SYMLINK_FORBIDDEN", f"{current} 是符号链接")
        last = index == len(parts) - 1
        if last and not stat.S_ISREG(st.st_mode):
            raise SandboxError("INPUT_INVALID", f"{current} 不是普通文件")
        if not last and not stat.S_ISDIR(st.st_mode):
            raise SandboxError("INPUT_INVALID", f"{current} 不是目录")
    if st.st_nlink != 1:
        raise SandboxError("HARDLINK_FORBIDDEN", f"{current} 有 {st.st_nlink} 个硬链接")
    return current


@dataclass(frozen=True)
class DataView:
    """一次沙盒运行的数据视图（宿主侧临时目录，0700）。"""

    path: Path
    entries: "tuple[dict[str, Any], ...]"

    def file_map(self) -> "dict[str, Path]":
        return {e["name"]: self.path / e["name"] for e in self.entries}


def build_view(root: "Path | str", entries: Iterable[ViewEntry], *,
               dest_root: "Path | str | None" = None) -> DataView:
    """从证据根 ``root`` 新建 0700 视图目录（放在 ``dest_root``，默认同根），
    逐件复核 sha 后复制为 0600 平铺文件。

    平铺（只保留 basename）让容器看到的名字与容器内路径一一对应，且天然吞掉
    目录结构——候选拿不到证据根的目录布局。
    """
    base = Path(root)
    if base.is_symlink():
        raise SandboxError("SYMLINK_FORBIDDEN", f"证据根 {base} 是符号链接")
    dest = base if dest_root is None else Path(dest_root)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    view = dest / f"view-{uuid.uuid4().hex}"
    view.mkdir(mode=0o700, parents=False)
    records: "list[dict[str, Any]]" = []
    names: "set[str]" = set()
    try:
        for entry in entries:
            parts = _safe_parts(entry.relpath)
            name = parts[-1]
            if name in names:
                raise SandboxError("DUPLICATE_ID", f"视图内重名文件 {name!r}")
            names.add(name)
            src = _resolve_no_symlink(base, parts)
            target = view / name
            _copy_verified(src, target, entry.sha256)
            records.append({"name": name, "sha256": entry.sha256,
                            "bytes": target.stat().st_size})
    except BaseException:
        shutil.rmtree(view, ignore_errors=True)
        raise
    return DataView(path=view, entries=tuple(records))


def _copy_verified(src: Path, target: Path, expected_sha: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(src, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SandboxError("SYMLINK_FORBIDDEN", f"{src} 是符号链接") from None
        raise SandboxError("INPUT_MISSING", f"{src}: {exc}") from None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise SandboxError("INPUT_TOO_LARGE",
                           f"{src} 超过 {MAX_INPUT_BYTES} B（视图不挂大文件）")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha:
        raise SandboxError("HASH_MISMATCH",
                           f"{src} sha256={digest} != 登记 {expected_sha}")
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600)
    with os.fdopen(out, "wb") as sink:
        sink.write(data)
    os.chmod(target, 0o600)


# ---------------------------------------------------------------------------
# runner：容器执行的一个薄抽象（生产= docker CLI；测试=内存假实现）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False


class ContainerRunner(Protocol):
    def run(self, argv: "list[str]", *, timeout_s: int,
            container: str) -> RunResult: ...

    def residue(self, container: str) -> bool: ...

    def force_remove(self, container: str) -> None: ...


class DockerRunner:
    """生产 runner：只用 ``docker`` CLI 列表参数，不拼 shell。"""

    def __init__(self, docker: str = "docker") -> None:
        self.docker = docker

    def run(self, argv, *, timeout_s, container) -> RunResult:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
            return RunResult(exit_code=proc.returncode,
                             stdout_tail=proc.stdout[-2048:],
                             stderr_tail=proc.stderr[-2048:])
        except subprocess.TimeoutExpired:
            self.force_remove(container)
            return RunResult(exit_code=-1, timed_out=True)
        except FileNotFoundError as exc:
            raise SandboxError("DOCKER_UNAVAILABLE", str(exc)) from None

    def residue(self, container: str) -> bool:
        proc = subprocess.run([self.docker, "ps", "-a", "--quiet",
                               "--filter", f"name=^{container}$"],
                              capture_output=True, text=True, check=False)
        return bool(proc.stdout.strip())

    def force_remove(self, container: str) -> None:
        subprocess.run([self.docker, "rm", "-f", container],
                       capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# 输出面
# ---------------------------------------------------------------------------

def _collect_outputs(out_dir: Path) -> "list[dict[str, Any]]":
    files: "list[dict[str, Any]]" = []
    total = 0

    def walk(path: Path, depth: int) -> None:
        nonlocal total
        if depth > MAX_VIEW_DEPTH:
            raise SandboxError("OUTPUT_INVALID", f"输出目录层级过深：{path}")
        for child in sorted(path.iterdir()):
            st = os.lstat(child)
            if stat.S_ISLNK(st.st_mode):
                raise SandboxError("SYMLINK_FORBIDDEN", f"输出含符号链接：{child.name}")
            if stat.S_ISDIR(st.st_mode):
                walk(child, depth + 1)
                continue
            if not stat.S_ISREG(st.st_mode):
                raise SandboxError("OUTPUT_INVALID", f"输出含非普通文件：{child.name}")
            total += st.st_size
            if total > OUTPUT_MAX_BYTES:
                raise SandboxError("OUTPUT_TOO_LARGE",
                                   f"输出累计 {total} 超过 {OUTPUT_MAX_BYTES} B")
            if len(files) >= MAX_OUTPUT_FILES:
                raise SandboxError("OUTPUT_TOO_MANY",
                                   f"输出文件数超过 {MAX_OUTPUT_FILES}")
            rel = child.relative_to(out_dir).as_posix()
            digest = hashlib.sha256(child.read_bytes()).hexdigest()
            files.append({"name": rel, "bytes": st.st_size, "sha256": digest,
                          "path": str(child)})

    walk(out_dir, 0)
    return files


def _read_reply(out_dir: Path) -> "dict[str, Any] | None":
    reply = out_dir / "reply.json"
    if not reply.exists():
        return None
    if os.lstat(reply).st_size > REPLY_MAX_BYTES:
        raise SandboxError("REPLY_TOO_LARGE",
                           f"reply.json {os.lstat(reply).st_size} > {REPLY_MAX_BYTES}")
    try:
        doc = json.loads(reply.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SandboxError("REPLY_INVALID", f"reply.json 解析失败：{exc}") from None
    if not isinstance(doc, dict):
        raise SandboxError("REPLY_INVALID", "reply.json 必须是对象")
    return doc


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxEvidence:
    """一次容器运行的完整证据（宿主可逐字段复核）。"""

    kind: str                       # "design" | "analysis"
    source_sha256: str
    inputs: "tuple[dict[str, Any], ...]"
    image: str
    limits: "Mapping[str, Any]"
    exit_code: int
    timed_out: bool
    duration_ms: int
    outputs: "tuple[dict[str, Any], ...]"
    reply: "dict[str, Any] | None" = None
    container: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> "dict[str, Any]":
        return {"schema": "simpraxis.rsi_r1.sandbox_evidence.v1",
                "schema_version": 1, "kind": self.kind,
                "source_sha256": self.source_sha256,
                "inputs": [dict(x) for x in self.inputs],
                "image": self.image, "limits": dict(self.limits),
                "exit_code": self.exit_code, "timed_out": self.timed_out,
                "duration_ms": self.duration_ms,
                "outputs": [{k: v for k, v in o.items() if k != "path"}
                            for o in self.outputs],
                "reply": self.reply, "container": self.container,
                "stderr_tail": self.stderr_tail}


def _limits_doc() -> "dict[str, Any]":
    return {"wall_clock_s": WALL_CLOCK_S, "memory": MEMORY_LIMIT,
            "pids": PIDS_LIMIT, "cpus": CPU_LIMIT,
            "output_max_bytes": OUTPUT_MAX_BYTES,
            "reply_max_bytes": REPLY_MAX_BYTES,
            "source_max_bytes": SOURCE_MAX_BYTES,
            "uid": CONTAINER_UID, "gid": CONTAINER_GID,
            "network": "none", "rootfs": "read-only",
            "cap_drop": "ALL", "no_new_privileges": True}


def _normalise_source(source: "str | bytes") -> bytes:
    if isinstance(source, str):
        raw = source.encode("utf-8")
    elif isinstance(source, bytes):
        raw = source
    else:
        raise SandboxError("INPUT_INVALID", f"source 类型 {type(source).__name__}")
    if not raw:
        raise SandboxError("INPUT_INVALID", "source 为空")
    if len(raw) > SOURCE_MAX_BYTES:
        raise SandboxError("SOURCE_TOO_LARGE",
                           f"source {len(raw)} B > {SOURCE_MAX_BYTES}")
    if b"\x00" in raw:
        raise SandboxError("INPUT_INVALID", "source 含 NUL 字节")
    return raw


def _container_argv(*, image: str, container: str, view: Path, out: Path,
                    script_name: str) -> "list[str]":
    return [
        "docker", "run", "--rm", "--name", container,
        "--network", "none",
        "--user", f"{CONTAINER_UID}:{CONTAINER_GID}",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", MEMORY_LIMIT,
        "--memory-swap", MEMORY_LIMIT,
        "--cpus", CPU_LIMIT,
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONHASHSEED=0",
        "-e", "HOME=/tmp",
        "-v", f"{view}:/data:ro",
        "-v", f"{out}:/out:rw",
        image,
        "python3", f"/data/{script_name}",
    ]


def _run_in_sandbox(*, kind: str, source: "str | bytes", entries: Iterable[ViewEntry],
                    evidence_root: "Path | str", work_root: "Path | str",
                    runner: ContainerRunner, image: str) -> SandboxEvidence:
    if not isinstance(image, str) or not image.strip():
        raise SandboxError("IMAGE_MISSING", "必须由 host 指定容器镜像（候选不可覆盖）")
    raw = _normalise_source(source)
    src_sha = hashlib.sha256(raw).hexdigest()
    base = Path(work_root)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    view = build_view(evidence_root, list(entries), dest_root=base)
    out = base / f"out-{uuid.uuid4().hex}"
    out.mkdir(mode=0o700)
    container = f"rsir1-{kind}-{uuid.uuid4().hex[:12]}"
    script_name = "design.py" if kind == "design" else "analysis.py"
    try:
        script = view.path / script_name
        try:
            fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise SandboxError("DUPLICATE_ID",
                               f"视图内已有同名 {script_name}（与候选输入冲突）") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        argv = _container_argv(image=image, container=container, view=view.path,
                               out=out, script_name=script_name)
        started = time.monotonic()
        try:
            result = runner.run(argv, timeout_s=WALL_CLOCK_S, container=container)
        finally:
            # 本轮自有容器：成功/失败/取消都回收
            runner.force_remove(container)
        duration_ms = int((time.monotonic() - started) * 1000)
        if runner.residue(container):
            raise SandboxError("CONTAINER_RESIDUE",
                               f"容器 {container} 退出后仍有残留")
        outputs = _collect_outputs(out)
        reply = _read_reply(out)
        if result.timed_out:
            raise SandboxError(
                "TIMEOUT",
                f"{kind} 容器超过 {WALL_CLOCK_S}s 墙钟被回收（container={container}）")
        return SandboxEvidence(
            kind=kind, source_sha256=src_sha,
            inputs=tuple({"name": e["name"], "sha256": e["sha256"],
                          "bytes": e["bytes"]} for e in view.entries),
            image=image, limits=_limits_doc(), exit_code=result.exit_code,
            timed_out=result.timed_out, duration_ms=duration_ms,
            outputs=tuple(outputs), reply=reply, container=container,
            stderr_tail=result.stderr_tail[-512:])
    finally:
        shutil.rmtree(view.path, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def run_design(source: "str | bytes", inputs: Iterable[ViewEntry], *,
               evidence_root: "Path | str", work_root: "Path | str",
               runner: ContainerRunner, image: str) -> SandboxEvidence:
    """候选 design 代码：读 /data 只读视图，写 /out。"""
    return _run_in_sandbox(kind="design", source=source, entries=inputs,
                           evidence_root=evidence_root, work_root=work_root,
                           runner=runner, image=image)


def run_analysis(source: "str | bytes", allowed_manifest: Iterable[ViewEntry], *,
                 evidence_root: "Path | str", work_root: "Path | str",
                 runner: ContainerRunner, image: str) -> SandboxEvidence:
    """分析代码：只挂 ``allowed_manifest`` 列出的 run 文件（同构安全面）。"""
    return _run_in_sandbox(kind="analysis", source=source,
                           entries=allowed_manifest,
                           evidence_root=evidence_root, work_root=work_root,
                           runner=runner, image=image)
