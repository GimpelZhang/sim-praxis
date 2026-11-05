"""Task 6 参考基线执行（P0-6，0 付费）：参考实现的私有解析、执行与归档。

**纪律**（Plan T6.1/T6.3）：

- **D6-1 参考实现私有化**：三个任务的「答案」只落
  ``<bench_root>/private/references/<task_id>/``；引擎仓里只有编排脚手架，
  本模块 :func:`assert_private` 对任何落在引擎仓内的参考路径 fail-closed
  （AC-T6-7）。
- **D6-2 脚手架不含解题逻辑**：物理/环境/模板细节全在私有参考实现里，
  这里只负责「校验输入 sha → 起子进程/容器 → 收产物 → 出生验证 → 指纹 →
  复现对账 → run_manifest → 独立评测器判决」。
- **D6-3/D6-4**：L1 只在 ``sim-praxis/libero:task6@sha256:…`` 容器内执行，
  挂载白名单 ``{/work,/skills,/assets,/out}``，除 ``/out`` 外一律 ``:ro``，
  且记录的命令**至少 3 个 :ro**（AC-T6-8）。
- **D6-7**：M1 同种子重跑 metrics 逐字段相等（排除 wall_time），换种子差值
  对账进 ``reproducibility.json``。
- **D6-8**：``run_id = <task_id>-<UTC compact>-<seed>``，目录已存在即拒。

参考实现**只读复用** KB 资产：M1 的物理原语来自
``skills/mujoco/contact_push.py`` 的 staged 副本，L1 的方案来自
``solutions/track1_tools/03_libero`` 的 staged 副本；本模块只做 stage 与
逐件 sha256 复核，**不修改 KB**。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.benchmark import birth, paths, registry, runtime_fingerprint as rf
from orchestrator.benchmark import task_spec as ts
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

REPO_ROOT = Path(__file__).resolve().parents[2]

RECIPE_SCHEMA = "simpraxis.benchmark_reference_recipe.v1"
RUN_MANIFEST_SCHEMA = "simpraxis.benchmark_run.v1"
INPUT_MANIFEST_SCHEMA = "simpraxis.benchmark_input_manifest.v1"
REPRO_SCHEMA = "simpraxis.benchmark_reproducibility.v1"
HIDDEN_AUDIT_SCHEMA = "simpraxis.benchmark_hidden_access_audit.v1"
EVIDENCE_SCHEMA = "simpraxis.benchmark_evidence.v1"

#: D6-4 挂载白名单（``dst`` 只能是这四个之一；除 /out 外必须 :ro）
MOUNT_WHITELIST = ("/work", "/skills", "/assets", "/out")
#: AC-T6-8：容器命令里 ``:ro`` 至少三个
MIN_RO_MOUNTS = 3

DEFAULT_LIBERO_SOLUTION = "track1_tools/03_libero"
DEFAULT_LIBERO_IMAGE_NAME = "sim-praxis/libero:task6"

EXIT_OK = 0


class ReferenceError(RuntimeError):
    """参考基线无法继续（配置/路径/输入不合规）——一律 fail-closed。"""


# --------------------------------------------------------------------------
# 参考实现的登记与私有性
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceSpec:
    """一个任务的参考实现登记（入口、执行形态、必需文件、公开种子）。"""

    task_id: str
    entry: str
    mode: str                  # host | docker | document
    files: tuple[str, ...]
    seeds: tuple[int, ...]


REFERENCE_SPECS: "dict[str, ReferenceSpec]" = {
    "mujoco_contact_push_v1": ReferenceSpec(
        task_id="mujoco_contact_push_v1", entry="solution.py", mode="host",
        files=("solution.py", "reference_recipe.json"), seeds=(0, 1)),
    "libero_env_adapter_v1": ReferenceSpec(
        task_id="libero_env_adapter_v1", entry="adapter.py", mode="docker",
        files=("adapter.py", "reference_recipe.json"), seeds=(0,)),
    "icy_ground_dr_v1": ReferenceSpec(
        task_id="icy_ground_dr_v1", entry="build.py", mode="document",
        files=("build.py", "reference_recipe.json"), seeds=()),
}


@dataclass(frozen=True)
class ResolvedReference:
    spec: ReferenceSpec
    directory: Path
    recipe: Mapping[str, Any]

    @property
    def entry_path(self) -> Path:
        return self.directory / self.spec.entry


def references_root() -> Path:
    """私有参考根：``<bench_root>/private/references``（0700，永不入 git）。"""
    return ensure_private_dir(paths.private_dir() / "references")


def skills_stage_root() -> Path:
    return ensure_private_dir(paths.private_dir() / "skills_stage")


def solutions_stage_root() -> Path:
    return ensure_private_dir(paths.private_dir() / "solutions_stage")


def assert_private(path: "str | Path", *,
                   repo_root: "str | Path | None" = None) -> Path:
    """参考实现/私有材料的路径守卫：**落在引擎仓内即抛错**（AC-T6-7）。

    只做路径判定，不创建目录；返回 ``resolve(strict=False)`` 后的绝对路径。
    """
    target = Path(path).expanduser().resolve(strict=False)
    repo = Path(repo_root or REPO_ROOT).resolve(strict=False)
    if target == repo or repo in target.parents:
        raise ReferenceError(
            f"参考实现不得落在引擎仓内：{target}（仓根 {repo}）——"
            "答案只允许存在于 <bench_root>/private/")
    return target


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: "str | Path") -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, doc: Any) -> Path:
    return atomic_write_text(
        path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600)


def load_recipe(directory: "str | Path") -> dict:
    """读 ``reference_recipe.json`` 并做结构与逐件 sha256 校验。"""
    path = Path(directory) / "reference_recipe.json"
    if not path.is_file():
        raise ReferenceError(f"缺 reference_recipe.json：{path}")
    doc = _read_json(path)
    if not isinstance(doc, dict) or doc.get("schema") != RECIPE_SCHEMA:
        raise ReferenceError(
            f"{path}: schema={doc.get('schema')!r} 非 {RECIPE_SCHEMA}")
    files = doc.get("files")
    if not isinstance(files, dict) or not files:
        raise ReferenceError(f"{path}: files 必须是非空对象（relpath→sha256）")
    for rel, want in sorted(files.items()):
        candidate = Path(directory) / rel
        if not candidate.is_file():
            raise ReferenceError(f"{path}: 登记的 {rel} 不在场")
        actual = _sha256_file(candidate)
        if actual != want:
            raise ReferenceError(
                f"{path}: {rel} sha256 不符（recipe={str(want)[:12]}… "
                f"实际={actual[:12]}…）")
    return doc


def resolve_reference(task_id: str, *,
                      root: "str | Path | None" = None) -> ResolvedReference:
    """解析一个任务的私有参考实现；目录/文件/recipe 任一不合规即抛错。"""
    try:
        spec = REFERENCE_SPECS[task_id]
    except KeyError:
        raise ReferenceError(
            f"未登记参考实现：{task_id!r}（已登记 {sorted(REFERENCE_SPECS)}）"
        ) from None
    base = Path(root) if root is not None else references_root()
    directory = assert_private(base / task_id)
    if not directory.is_dir():
        raise ReferenceError(f"参考实现目录不存在：{directory}")
    missing = [name for name in spec.files if not (directory / name).is_file()]
    if missing:
        raise ReferenceError(f"{directory}: 缺件 {missing}")
    recipe = load_recipe(directory)
    if recipe.get("task_id") != task_id or recipe.get("entry") != spec.entry:
        raise ReferenceError(
            f"{directory}: recipe 的 task_id/entry 与登记不符"
            f"（{recipe.get('task_id')!r}/{recipe.get('entry')!r}）")
    if recipe.get("mode") != spec.mode:
        raise ReferenceError(
            f"{directory}: recipe.mode={recipe.get('mode')!r} ≠ {spec.mode!r}")
    return ResolvedReference(spec=spec, directory=directory, recipe=recipe)


# --------------------------------------------------------------------------
# KB 输入 stage（只读复用，不修改 KB）
# --------------------------------------------------------------------------

def _copy_verified(src: Path, dst: Path, want: str) -> Path:
    data = src.read_bytes()
    actual = _sha256_bytes(data)
    if actual != want:
        raise ReferenceError(
            f"KB 源件 sha256 不符：{src}（manifest={want[:12]}… 实际={actual[:12]}…）")
    if dst.exists():
        if _sha256_file(dst) != want:
            raise ReferenceError(f"staged 副本已被改写：{dst}（先删除再重 stage）")
        return dst
    ensure_private_dir(dst.parent)
    dst.write_bytes(data)
    os.chmod(dst, 0o600)
    return dst


def materialize_skills(*, stage_root: "str | Path | None" = None) -> dict:
    """把 KB 技能清单逐件 stage 到 ``<bench_root>/private/skills_stage/<sha12>/``。

    与 ``scripts/phase27/stage_skills.py`` 同口径（清单 sha256 单一源），但落在
    benchmark 私有根，不碰 phase27 封存树。
    """
    from orchestrator.skills.registry import (
        load_skills_manifest, manifest_sha12, skills_root, staged_path_for)

    root = skills_root()
    manifest = load_skills_manifest(root)
    sha12 = manifest_sha12(root)
    target = Path(stage_root) if stage_root is not None else (
        skills_stage_root() / sha12)
    target = ensure_private_dir(assert_private(target))
    kb_root = root.parent
    rows = []
    items = list(manifest.get("skills") or ()) + list(manifest.get("packages") or ())
    for item in items:
        rel = str(item["relpath"])
        want = str(item["sha256"])
        dst = staged_path_for(rel, target)
        _copy_verified(kb_root / rel, dst, want)
        if dst.is_symlink() or not dst.is_file():
            raise ReferenceError(f"staged 件不是常规文件：{dst}")
        rows.append({"relpath": rel, "staged": str(dst.relative_to(target)),
                     "sha256": want})
    if not rows:
        raise ReferenceError(f"技能清单为空：{root}")
    return {
        "schema": INPUT_MANIFEST_SCHEMA,
        "kind": "skills",
        "source_root": str(root),
        "manifest_sha256": _sha256_file(root / "manifest.yaml"),
        "manifest_sha12": sha12,
        "stage_root": str(target),
        "files": rows,
    }


def materialize_solution(solution_id: str = DEFAULT_LIBERO_SOLUTION,
                         *, stage_root: "str | Path | None" = None) -> dict:
    """经 ``solutions.resolver.stage()`` 把内容层方案逐件落到私有根。"""
    from orchestrator.solutions.resolver import resolve_solution

    manifest = resolve_solution(solution_id)
    base = Path(stage_root) if stage_root is not None else solutions_stage_root()
    target = assert_private(base / solution_id)
    staged = manifest.stage(target)
    rows = []
    for sf in manifest.files:
        path = staged.files[sf.relpath]
        actual = _sha256_file(path)
        if actual != sf.sha256:
            raise ReferenceError(f"staged 方案件 sha256 不符：{path}")
        rows.append({"relpath": sf.relpath, "staged": str(path),
                     "sha256": actual})
    return {
        "schema": INPUT_MANIFEST_SCHEMA,
        "kind": "solution",
        "solution_id": solution_id,
        "entrypoint": manifest.entrypoint,
        "stage_root": str(target),
        "files": rows,
    }


# --------------------------------------------------------------------------
# 挂载白名单与容器命令（D6-3/D6-4，AC-T6-8）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Mount:
    source: Path
    destination: str
    mode: str = "ro"

    @property
    def flag(self) -> str:
        return f"{Path(self.source)}:{self.destination}:{self.mode}"

    def fingerprint_entry(self) -> dict:
        return {"source": str(Path(self.source)), "destination": self.destination,
                "mode": self.mode, "source_sha256": None}


def validate_mounts(mounts: Iterable[Mount]) -> "list[str]":
    """白名单 + 只读数量检查；返回问题清单（空才允许起容器）。"""
    problems: "list[str]" = []
    seen = set()
    ro = 0
    for mount in mounts:
        if mount.destination not in MOUNT_WHITELIST:
            problems.append(f"挂载目标不在白名单：{mount.destination}")
        if mount.mode not in ("ro", "rw"):
            problems.append(f"挂载模式非法：{mount.mode!r}")
        if mount.mode == "ro":
            ro += 1
        if mount.destination in seen:
            problems.append(f"挂载目标重复：{mount.destination}")
        seen.add(mount.destination)
        if not str(mount.source):
            problems.append(f"挂载源为空：{mount.destination}")
    if ro < MIN_RO_MOUNTS:
        problems.append(f"只读挂载 {ro} 个 < 下限 {MIN_RO_MOUNTS}")
    return problems


def docker_argv(image_ref: str, mounts: Iterable[Mount],
                inner_argv: Iterable[str], *, workdir: str = "/work",
                gpus: bool = True, ipc_host: bool = True,
                label: str = "simpraxis.benchmark.reference=1",
                docker: str = "docker") -> "list[str]":
    """按 D6-3 拼 docker run 命令；挂载不合规**先于**起容器抛错。

    容器一律以镜像默认用户运行：LIBERO 的配置与数据集指针在 ``/root/.libero``
    （0700），换成非 root 会让导入期弹交互提问并直接 EOF 失败。
    出生层结果件的属主问题由运行器内的 ``chown_uid/chown_gid`` 交回收尾
    （见 ``birth._RUNNER_SRC``），不靠这里提权参数。
    """
    mount_list = list(mounts)
    problems = validate_mounts(mount_list)
    if problems:
        raise ReferenceError("挂载不合规：" + "; ".join(problems))
    if "@sha256:" not in image_ref:
        raise ReferenceError(f"镜像必须按 digest 调用：{image_ref!r}")
    argv = [docker, "run", "--rm"]
    if gpus:
        argv += ["--gpus", "all"]
    if ipc_host:
        argv += ["--ipc=host"]
    argv += ["--network", "none", "--label", label, "-w", workdir]
    for mount in mount_list:
        argv += ["-v", mount.flag]
    argv.append(image_ref)
    argv += [str(item) for item in inner_argv]
    return argv


def parse_mount_flags(argv: Iterable[str]) -> "list[tuple[str, str]]":
    """从一条 docker 命令里取出 ``-v`` 的 ``(spec, mode)``（无 mode 记 rw）。"""
    out: "list[tuple[str, str]]" = []
    items = [str(a) for a in argv]
    for index, token in enumerate(items):
        if token != "-v" or index + 1 >= len(items):
            continue
        spec = items[index + 1]
        parts = spec.split(":")
        mode = parts[-1] if len(parts) >= 3 else "rw"
        out.append((spec, mode))
    return out


def validate_container_argv(argv: Iterable[str]) -> "list[str]":
    """AC-T6-8 的机判面：``-v`` 目标 ⊆ 白名单，且 ``:ro`` ≥ 3。"""
    problems: "list[str]" = []
    flags = parse_mount_flags(argv)
    ro = 0
    for spec, mode in flags:
        parts = spec.split(":")
        dst = parts[1] if len(parts) >= 2 else ""
        if dst not in MOUNT_WHITELIST:
            problems.append(f"-v 目标不在白名单：{dst!r}（{spec}）")
        if mode == "ro":
            ro += 1
        elif mode != "rw":
            problems.append(f"-v 模式非法：{mode!r}（{spec}）")
    if ro < MIN_RO_MOUNTS:
        problems.append(f"-v :ro 只有 {ro} 个 < 下限 {MIN_RO_MOUNTS}")
    return problems


def libero_image_ref(spec: ts.TaskSpec,
                     *, image_name: str | None = None) -> str:
    digest = spec.runtime.get("image_digest")
    if not digest:
        raise ReferenceError(f"{spec.task_id}: runtime.image_digest 为空")
    name = image_name or DEFAULT_LIBERO_IMAGE_NAME
    return f"{name}@{digest}"


# --------------------------------------------------------------------------
# 输入清单（私有根单一文件：逐件 sha256 + 每任务用到哪些输入）
# --------------------------------------------------------------------------

def input_manifest_path() -> Path:
    return assert_private(paths.private_dir() / "input_manifest.json")


def _merge_inputs(rows: Iterable[Mapping], existing: Any) -> "list[dict]":
    """按 ``(kind, stage_root)`` 去重合并（同一份 stage 只登记一次）。"""
    merged: "list[dict]" = []
    seen: "set[tuple[str, str]]" = set()
    for row in list(existing or ()) + list(rows):
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("kind")), str(row.get("stage_root")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(row))
    return merged


def record_input_manifest(task_id: str, rows: Iterable[Mapping], *,
                          recipe_sha256: "str | None" = None) -> dict:
    """读-改-写 ``private/input_manifest.json``（不丢其他任务的登记）。

    评审 W4-12：已有清单解析失败时**抛错**而不是静默重置为 ``{}``——重置会把
    其他任务的登记整份丢掉，写回去等于无声截断（硬规则 7 fail-closed）。
    """
    path = input_manifest_path()
    doc: "dict[str, Any]" = {}
    if path.is_file():
        try:
            loaded = _read_json(path)
        except json.JSONDecodeError as exc:
            raise ReferenceError(
                f"输入清单不可解析（拒绝覆盖写）：{path}：{exc}") from exc
        if not isinstance(loaded, dict):
            raise ReferenceError(f"输入清单根节点不是对象：{path}")
        if loaded.get("schema") != INPUT_MANIFEST_SCHEMA:
            raise ReferenceError(
                f"输入清单 schema 不符（期望 {INPUT_MANIFEST_SCHEMA}，实为 "
                f"{loaded.get('schema')!r}）：{path}")
        doc = loaded
    tasks = dict(doc.get("tasks") or {}) if isinstance(doc.get("tasks"), dict) else {}
    recipes = dict(doc.get("recipes") or {}) if isinstance(
        doc.get("recipes"), dict) else {}
    rows = [dict(row) for row in rows]
    tasks[task_id] = rows
    if recipe_sha256:
        recipes[task_id] = str(recipe_sha256)
    out = {
        "schema": INPUT_MANIFEST_SCHEMA,
        "tasks": tasks,
        "recipes": recipes,
        "inputs": _merge_inputs(rows, doc.get("inputs")),
    }
    _write_json(path, out)
    return out


# --------------------------------------------------------------------------
# 运行目录（D6-8）
# --------------------------------------------------------------------------

def run_id_for(task_id: str, seed: int, *,
               now: "datetime | None" = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{task_id}-{stamp}-{seed}"


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    root: Path
    candidate: Path
    birth_dir: Path
    raw: Path
    out: Path

    @property
    def evaluation_dir(self) -> Path:
        return paths.evaluator_results_dir() / self.run_id


def prepare_run(task_id: str, seed: int, *,
                now: "datetime | None" = None) -> RunPaths:
    """建 run 目录（已存在即拒，不静默覆盖：D6-8）。"""
    run_id = run_id_for(task_id, seed, now=now)
    root = paths.runs_dir() / run_id
    if root.exists():
        raise ReferenceError(f"run 目录已存在，拒绝覆盖：{root}")
    ensure_private_dir(root)
    return RunPaths(
        run_id=run_id, root=root,
        candidate=ensure_private_dir(root / "candidate"),
        birth_dir=ensure_private_dir(root / "birth"),
        raw=ensure_private_dir(root / "raw"),
        out=ensure_private_dir(root / "out"))


# --------------------------------------------------------------------------
# 执行：三种形态
# --------------------------------------------------------------------------

def _run_command(argv: "list[str]", *, cwd: Path, env: Mapping[str, str],
                 timeout: float, log: Path) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=dict(env),
                              capture_output=True, text=True, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except OSError as exc:
        raise ReferenceError(f"子进程无法启动：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReferenceError(f"子进程超时（{timeout}s）：{argv[0]}") from exc
    atomic_write_text(log,
                      f"$ {' '.join(argv)}\n--- stdout ---\n{out}"
                      f"\n--- stderr ---\n{err}", mode=0o600)
    return {"rc": rc, "stdout": out, "stderr": err,
            "duration_s": round(time.monotonic() - started, 6)}


def _filtered_output(text: str) -> str:
    """丢掉 EGL 拆除噪声（成功跑也会在退出时打印，非错误）。"""
    keep = [line for line in text.splitlines()
            if not line.startswith("OpenGL.raw.EGL._errors.EGLError")
            and "exception str() failed" not in line]
    return "\n".join(keep)


def _require_rc(result: Mapping, what: str) -> None:
    if int(result["rc"]) != 0:
        tail = _filtered_output(str(result["stderr"]))[-1200:]
        raise ReferenceError(f"{what} 失败 rc={result['rc']}：{tail}")


def stage_skills_into(skills_rows: Mapping, target: "str | Path") -> Path:
    """把已 stage 的技能件逐件复制进**执行目录**（每件 sha256 复核；幂等）。

    参考入口按「本文件同目录」解析 ``simpraxis_skills``（出生验证子进程只把
    repo_root 放进 sys.path，见 ``birth._RUNNER_SRC``），因此每次执行的目录
    （候选/同种子重跑/换种子）都必须自带这一份技能包。技能包**不是**任务产物：
    上提（``_promote``）只复制 ``spec.outputs``，故运行根不会被它污染。
    """
    source = Path(skills_rows["stage_root"])
    dst_root = ensure_private_dir(Path(target))
    for item in skills_rows["files"]:
        src = source / str(item["staged"])
        want = str(item["sha256"])
        data = src.read_bytes()
        actual = _sha256_bytes(data)
        if actual != want:
            raise ReferenceError(f"staged 技能件被改写：{src}"
                                 f"（{want[:12]}… ≠ {actual[:12]}…）")
        dst = dst_root / str(item["staged"])
        if dst.exists():
            if _sha256_file(dst) != want:
                raise ReferenceError(f"执行目录里的技能件内容不符：{dst}")
            continue
        ensure_private_dir(dst.parent)
        dst.write_bytes(data)
        os.chmod(dst, 0o600)
    return dst_root


def run_host_reference(ref: ResolvedReference, run: RunPaths, *,
                       python: str, skills_rows: Mapping, seed: int,
                       out_dir: "Path | None" = None,
                       log_name: "str | None" = None,
                       timeout: float = 600.0) -> dict:
    """M1：主 venv 里真跑参考实现（真实 ``mj_step``）。

    入口先落进目标目录、技能包随后逐件复核复制——两者都在 ``-I`` 隔离下由
    入口自己（``sys.path.insert(_HERE)``）导入，与出生验证同一路径形态。
    """
    target = ensure_private_dir(Path(out_dir) if out_dir is not None
                                else run.candidate)
    entry = _install_entry(ref, target)
    stage_skills_into(skills_rows, target)
    argv = [python, "-I", str(entry),
            "--out-dir", str(target), "--seed", str(seed)]
    result = _run_command(argv, cwd=target,
                          env=rf.sanitized_env(), timeout=timeout,
                          log=run.raw / (log_name or f"run_seed{seed}.log"))
    _require_rc(result, f"{ref.spec.task_id} 参考执行")
    return {"command": argv, "seed": seed, "rc": result["rc"],
            "duration_s": result["duration_s"],
            "stdout": _filtered_output(str(result["stdout"]))}


def run_docker_reference(ref: ResolvedReference, run: RunPaths, *,
                         image_ref: str, skills_dir: Path, assets_root: Path,
                         out_dir: Path, seed: int, chown_uid: int,
                         chown_gid: int, timeout: float = 900.0) -> dict:
    """L1：隔离容器内跑参考 adapter（``--network none``，白名单只读挂载）。

    ``/out`` 挂的是 ``out_dir``（默认 run.candidate）：adapter 以 root 运行，
    写完按 ``--chown-uid/--chown-gid`` 把产物交回调用者（T6.5 步骤 2 实测口径，
    不用 sudo、不 chmod 777）。
    """
    mounts = (
        Mount(ref.directory, "/work", "ro"),
        Mount(skills_dir, "/skills", "ro"),
        Mount(assets_root, "/assets", "ro"),
        Mount(out_dir, "/out", "rw"),
    )
    inner = ["python3", "/work/adapter.py", "--out-dir", "/out",
             "--seed", str(seed), "--chown-uid", str(chown_uid),
             "--chown-gid", str(chown_gid)]
    argv = docker_argv(image_ref, mounts, inner)
    result = _run_command(argv, cwd=run.raw,
                          env=rf.sanitized_env(), timeout=timeout,
                          log=run.raw / f"container_seed{seed}.log")
    _require_rc(result, f"{ref.spec.task_id} 容器参考执行")
    missing = [name for name in ("env_probe.json", "one_step_result.json")
               if not (out_dir / name).is_file()]
    if missing:
        raise ReferenceError(f"容器产物缺件：{missing}（/out 未落齐）")
    return {"command": argv, "mounts": [m.fingerprint_entry() for m in mounts],
            "seed": seed, "rc": result["rc"],
            "duration_s": result["duration_s"],
            "stdout": _filtered_output(str(result["stdout"]))}


def run_document_reference(ref: ResolvedReference, run: RunPaths, *,
                           python: str, seed: int,
                           out_dir: "Path | None" = None,
                           timeout: float = 120.0) -> dict:
    """R1：确定性模板生成四件研究设计产物（不训练、不调模型）。"""
    target = Path(out_dir) if out_dir is not None else run.candidate
    argv = [python, "-I", str(ref.entry_path),
            "--out-dir", str(target), "--seed", str(seed)]
    result = _run_command(argv, cwd=target,
                          env=rf.sanitized_env(), timeout=timeout,
                          log=run.raw / f"run_seed{seed}.log")
    _require_rc(result, f"{ref.spec.task_id} 参考执行")
    return {"command": argv, "seed": seed, "rc": result["rc"],
            "duration_s": result["duration_s"],
            "stdout": _filtered_output(str(result["stdout"]))}


# --------------------------------------------------------------------------
# 出生验证（host / 容器两形态）
# --------------------------------------------------------------------------

def birth_host(ref: ResolvedReference, workdir: Path, out_dir: Path, *,
               python: str, timeout: float = 120.0) -> dict:
    """宿主出生验证（M1/R1）：直接复用 ``BirthValidator``（单一口径）。"""
    spec = registry.BenchmarkRegistry().get(ref.spec.task_id)
    bspec = birth.BirthSpec.from_task_spec(spec)
    validator = birth.BirthValidator(
        bspec, workdir=workdir, python=python,
        limits=birth.Limits(timeout_s=timeout))
    result = validator.validate(out_dir)
    written = birth.write_birth(out_dir, result)
    return {"written": written, "status": result.birth_status,
            "levels": {L: result.levels[L].status for L in birth.LEVELS}}


def _birth_payload(bspec: birth.BirthSpec, level: str, *, workdir: str,
                   repo_root: str, ctx: Mapping[str, Any]) -> dict:
    return {
        "level": level,
        "workdir": workdir,
        "entry": {"kind": bspec.entry_kind, "path": bspec.entry_path},
        "contract": bspec.contract,
        "repo_root": repo_root,
        "min_bytes": bspec.min_bytes,
        "generic_reason": birth._GENERIC_REASON[level],
        "ctx": dict(ctx),
    }


def birth_container(ref: ResolvedReference, workdir: Path, out_dir: Path, *,
                    image_ref: str, skills_dir: Path, assets_root: Path,
                    pythonpath: "Sequence[str]" = (), timeout: float = 180.0
                    ) -> dict:
    """容器内出生验证（L1）：五层各起一次 ``--network none`` 容器。

    复用 ``birth._RUNNER_SRC``（单一口径）与 ``birth.BirthValidator._aggregate``
    （结果语义单一源）；只把「子进程在宿主」换成「子进程在容器」。

    ``-I`` 会丢掉镜像 ``ENV PYTHONPATH``——而 LIBERO 正是靠它（``/opt/libero-src``）
    才能被 import。故 ``pythonpath`` 由 recipe 显式登记、以一行 ``sys.path.append``
    前置到运行器源码里（**不改** ``birth._RUNNER_SRC`` 本体，宿主路径语义不变）。
    """
    spec = registry.BenchmarkRegistry().get(ref.spec.task_id)
    bspec = birth.BirthSpec.from_task_spec(spec)
    required = bspec.required_levels()
    prologue = "".join(f"import sys as _s; _s.path.append({p!r})\n"
                       for p in pythonpath if str(p))
    runner_src = prologue + birth._RUNNER_SRC
    levels_dir = ensure_private_dir(out_dir / "levels")
    aggregate = birth.BirthValidator(bspec, workdir=workdir)
    levels: "dict[str, birth.LevelResult]" = {}
    for level in birth.LEVELS:
        level_dir = ensure_private_dir(levels_dir / level)
        if not required[level]:
            levels[level] = _not_applicable(level, level_dir)
            continue
        payload = _birth_payload(bspec, level, workdir="/work",
                                 repo_root="/assets", ctx={"workdir": "/work"})
        nonce = secrets.token_hex(16)
        result_name = f"runner_result.{nonce}.json"
        payload["nonce"] = nonce
        payload["result_path"] = f"/out/levels/{level}/{result_name}"
        #: 容器以镜像默认用户（root）运行——LIBERO 的配置在 /root 下且 0700；
        #: 结果件由运行器按 chown_uid/chown_gid 交回调用者（见 birth._RUNNER_SRC）。
        payload["chown_uid"] = os.getuid()
        payload["chown_gid"] = os.getgid()
        payload_path = level_dir / "payload.json"
        _write_json(payload_path, payload)
        mounts = (
            Mount(workdir, "/work", "ro"),
            Mount(skills_dir, "/skills", "ro"),
            Mount(assets_root, "/assets", "ro"),
            Mount(out_dir, "/out", "rw"),
        )
        inner = ["python3", "-I", "-c", runner_src,
                 f"/out/levels/{level}/payload.json"]
        argv = docker_argv(image_ref, mounts, inner)
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, env=rf.sanitized_env())
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            levels[level] = birth.LevelResult(
                level=level, status="BLOCKED_INFRA", reason="blocked_infra",
                detail=f"容器出生验证无法启动：{exc}", data={})
            _write_level_evidence(level_dir, "", "", levels[level])
            continue
        atomic_write_text(level_dir / "stdout.txt",
                          birth._truncate_capture(stdout), mode=0o600)
        atomic_write_text(level_dir / "stderr.txt",
                          birth._truncate_capture(stderr), mode=0o600)
        parsed = birth.read_runner_result(level_dir / result_name,
                                          level=level, nonce=nonce)
        forged = birth.detect_forged_marker(stdout, stderr)
        if forged is not None:
            result = birth.LevelResult(
                level=level, status="FAIL", reason="contract_error",
                detail=f"候选伪造出生标记行（已拒绝采信）：{forged}", data={})
        elif parsed is None:
            result = birth.LevelResult(
                level=level, status="FAIL",
                reason=birth._GENERIC_REASON[level],
                detail=f"容器内运行器未产出层结果（rc={rc}）："
                       f"{_filtered_output(stderr).strip()[-600:]}", data={})
        else:
            result = birth.LevelResult(
                level=level, status=parsed.get("status"),
                reason=parsed.get("reason"),
                detail=str(parsed.get("detail") or ""),
                data=dict(parsed.get("data") or {}))
        result.duration_s = round(time.monotonic() - started, 6)
        result.exit_code = rc
        result.stdout_bytes = len(stdout.encode("utf-8"))
        result.stderr_bytes = len(stderr.encode("utf-8"))
        levels[level] = result
        _write_level_evidence(level_dir, stdout, stderr, result)

    hits, scanned = birth._scan_network_tree(workdir)
    status, problems = aggregate._aggregate(levels)
    result = birth.BirthResult(
        spec=bspec, levels=levels, birth_status=status, problems=problems,
        limits=birth.Limits(timeout_s=timeout),
        network_audit={"mode": "enforced", "network_mode": "none",
                       "static_import_hits": hits,
                       "scanned_files": scanned,
                       "note": "容器 --network none：断网被强制执行"},
        workdir=str(workdir), ts_iso=_now_iso())
    written = birth.write_birth(out_dir, result)
    return {"written": written, "status": result.birth_status,
            "levels": {L: levels[L].status for L in birth.LEVELS}}


def _not_applicable(level: str, level_dir: Path) -> "birth.LevelResult":
    result = birth.LevelResult(
        level=level, status="NOT_APPLICABLE",
        detail="birth_contract 未要求该层（不适用层不计入 PASS）", data={})
    _write_level_evidence(level_dir, "", "", result)
    return result


def _write_level_evidence(level_dir: Path, stdout: str, stderr: str,
                          result: "birth.LevelResult") -> None:
    atomic_write_text(level_dir / "stdout.txt",
                      birth._truncate_capture(stdout), mode=0o600)
    atomic_write_text(level_dir / "stderr.txt",
                      birth._truncate_capture(stderr), mode=0o600)
    _write_json(level_dir / "meta.json", result.to_json())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------
# 运行身份、复现与证据
# --------------------------------------------------------------------------

def capture_runtime_fingerprint(task_id: str, *, backend: str,
                                python: str | None = None,
                                image_ref: str | None = None,
                                image_name: str | None = None,
                                mounts: Iterable[Mount] = (),
                                simulator: Mapping | None = None
                                ) -> "tuple[dict, Any]":
    """在真正执行任务的环境里采集六段身份（宿主 / 容器）。

    返回 ``(指纹 dict, Capture)``：Capture 给调用方取 ``probe.packages``
    （L1 需要 ``libero`` 版本填 simulator 段）。
    """
    spec = registry.BenchmarkRegistry().get(task_id)
    packages = rf.declared_packages(spec)
    if backend == "docker":
        if not image_ref:
            raise ReferenceError("docker 后端需要 image@sha256:…")
        capture = rf.ContainerBackend(image_ref, image_name=image_name,
                                      packages=packages).capture()
    elif backend == "host":
        capture = rf.HostBackend(python, packages=packages).capture()
    else:
        raise ReferenceError(f"未知指纹后端：{backend!r}")
    doc = rf.build_fingerprint(
        spec, capture, mounts=[m.fingerprint_entry() for m in mounts],
        simulator=simulator)
    return doc, capture


def write_runtime_fingerprint(directory: Path, doc: Mapping) -> dict:
    return rf.write_fingerprint(directory, doc)


def libero_simulator_segment(task_id: str, *, probe: Mapping,
                             packages: Mapping) -> dict:
    """把实测契约写进指纹 ``simulator`` 段（observation/action contract）。"""
    keys = [str(k) for k in (probe.get("observation_keys") or ())]
    dim = probe.get("action_dim")
    if not keys or not isinstance(dim, int) or isinstance(dim, bool):
        raise ReferenceError(
            f"{task_id}: 探针未给出可用的观测键/动作维度（{keys!r}/{dim!r}）")
    return {
        "name": "libero",
        "version": str(packages.get("libero") or "<未知>"),
        "api_identity": "libero.libero.envs.OffScreenRenderEnv",
        "observation_contract": {"keys": keys, "dtype": None, "shape": []},
        "action_contract": {"space": "Box", "dim": int(dim),
                            "low": [], "high": []},
        "scene_identity": f"task:{task_id}",
    }


def compare_metrics_same_seed(a: Mapping, b: Mapping) -> dict:
    """同种子逐字段比对（排除 wall_time_seconds）。"""
    keys = sorted(set(a) | set(b))
    diffs = []
    for key in keys:
        if key == "wall_time_seconds":
            continue
        if key not in a or key not in b or a[key] != b[key]:
            diffs.append({"key": key, "a": a.get(key), "b": b.get(key)})
    return {"identical": not diffs, "diffs": diffs}


def cross_seed_delta(a: Mapping, b: Mapping) -> float:
    """换种子最大端态差（米）：末端距离与最大穿模的最大差。"""
    deltas = [abs(float(a["endpoint_distance_m"]) - float(b["endpoint_distance_m"])),
              abs(float(a["max_penetration_m"]) - float(b["max_penetration_m"]))]
    if not all(math.isfinite(value) for value in deltas):
        raise ReferenceError(f"跨种子差值非有限：{deltas}")
    return max(deltas)


def evidence_manifest(run_id: str, files: Iterable[str], root: Path) -> dict:
    rows = []
    for rel in files:
        path = Path(root) / rel
        if not path.is_file():
            raise ReferenceError(f"证据清单登记了不存在的件：{rel}")
        rows.append({"rel": rel, "sha256": _sha256_file(path),
                     "bytes": path.stat().st_size})
    return {"schema": EVIDENCE_SCHEMA, "case_id": run_id,
            "files": sorted(rows, key=lambda row: row["rel"])}


def write_hidden_access_audit(path: Path, *, note: str) -> Path:
    """参考运行全程不触碰 ``private/``：审计件如实写空列表（HG-EVAL-2）。"""
    return _write_json(path, {
        "schema": HIDDEN_AUDIT_SCHEMA,
        "read": [], "written": [], "modified": [],
        "note": note,
    })




# --------------------------------------------------------------------------
# 参考运行编排（T6.5 步骤 1–6）
# --------------------------------------------------------------------------

#: 主 venv 解释器（M1/R1 的执行环境；task.yaml.runtime.python 的同一路径）
MAIN_PYTHON = "/mnt/venvs/simpraxis/bin/python"

#: 未评测前 run_manifest 的占位状态：**绝不**先写 PASS（崩溃时宁可留下
#: 「未完成」也不留下假的成功）。评测后无条件回填成独立评测器的判决。
PENDING_STATUS = "BLOCKED_INFRA"


@dataclass
class RunOutcome:
    """一次参考运行的结局（供 CLI 打印与测试断言）。"""

    task_id: str
    run_id: str
    seed: int
    root: Path
    status: str
    birth_status: str
    fingerprint_sha256: str
    manifest_path: Path
    evaluation_dir: Path
    evaluation: dict
    inputs: dict


class ReferenceRunner:
    """把一次参考实现跑成「可评测的运行记录」。

    固定顺序（每步 fail-closed，不留半成品 PASS）：

    ① stage KB 输入（逐件 sha256）→ ② 执行参考实现（M1 另跑同种子 + 换种子）
    → ③ 出生验证 → ④ 运行指纹 → ⑤ 复现/隐藏审计证据 → ⑥ 产物上提到运行根
    （评测器从**一个根**解析全部相对路径）→ ⑦ run_manifest（身份 + 实测摘要）
    → ⑧ **独立进程**评测（``python -I scripts/benchmark/evaluate_run.py``）
    → ⑨ ``status`` 回填成该判决 → ⑩ 按最终字节**再评测一次**
    → ⑪ 只读副本抄到 ``<bench_root>/references/<task_id>/<run_id>/``。
    """

    def __init__(self, task_id: str, *, seed: "int | None" = None,
                 python: str = MAIN_PYTHON,
                 ref_root: "str | Path | None" = None,
                 docker: str = "docker",
                 now: "datetime | None" = None) -> None:
        self.task_id = task_id
        self.seed = seed
        self.python = python
        self.ref_root = ref_root
        self.docker = docker
        self.now = now

    # -- 入口 ---------------------------------------------------------------
    def run(self) -> RunOutcome:
        ref = resolve_reference(self.task_id, root=self.ref_root)
        spec = registry.BenchmarkRegistry().get(self.task_id)
        seed = self._seed(ref)
        for prereq in ref.recipe.get("prerequisites") or ():
            if not Path(prereq).exists():
                raise ReferenceError(
                    f"{ref.spec.task_id}: 前置条件不在场：{prereq}")
        run = prepare_run(self.task_id, seed, now=self.now)

        inputs = self._stage_inputs(ref)
        execution = self._execute(ref, run, seed)
        birth = self._birth(ref, run)
        fingerprint = self._fingerprint(ref, run)
        self._promote(run)
        extras = self._extras(ref, run, execution)

        manifest = self._write_manifest(spec, run, seed)
        verdict = self._evaluate(run, log_name="evaluate_first.log")
        if verdict["status"] != manifest["status"]:
            manifest["status"] = str(verdict["status"])
            _write_json(run.root / "run_manifest.json", manifest)
        final = self._evaluate(run, log_name="evaluate_final.log")

        _write_json(run.root / "raw" / "execution.json", execution)
        _write_json(run.root / "raw" / "inputs.json", inputs)
        _write_json(run.root / "raw" / "extras.json", extras)
        self._copy_reference(run)
        return RunOutcome(
            task_id=self.task_id, run_id=run.run_id, seed=seed, root=run.root,
            status=str(final["status"]),
            birth_status=str(birth["status"]),
            fingerprint_sha256=str(fingerprint["fingerprint_sha256"]),
            manifest_path=run.root / "run_manifest.json",
            evaluation_dir=run.evaluation_dir, evaluation=final, inputs=inputs)

    # -- ① 输入 stage -------------------------------------------------------
    def _seed(self, ref: ResolvedReference) -> int:
        if self.seed is not None:
            if ref.spec.seeds and int(self.seed) not in ref.spec.seeds:
                raise ReferenceError(
                    f"{ref.spec.task_id}: 种子 {self.seed} 不在登记集合 "
                    f"{list(ref.spec.seeds)} 内（不得凭空加种子）")
            return int(self.seed)
        return int(ref.spec.seeds[0]) if ref.spec.seeds else 0

    def _stage_inputs(self, ref: ResolvedReference) -> dict:
        rows: "list[dict]" = []
        if ref.spec.task_id == "mujoco_contact_push_v1":
            rows.append(materialize_skills())
        elif ref.spec.task_id == "libero_env_adapter_v1":
            rows.append(materialize_skills())
            rows.append(materialize_solution(
                str(ref.recipe.get("solution_id") or DEFAULT_LIBERO_SOLUTION)))
        return record_input_manifest(
            ref.spec.task_id, rows,
            recipe_sha256=_sha256_file(ref.directory / "reference_recipe.json"))

    def _skills_rows(self) -> dict:
        """stage 技能包并返回清单行（幂等；逐件 sha256 复核）。"""
        return materialize_skills()

    @staticmethod
    def _assets_root() -> Path:
        """L1 的 ``/assets`` 挂载源 = **引擎仓根**（只读）。

        容器里跑的出生验证器是 ``birth._RUNNER_SRC``，它第一步就
        ``sys.path.insert(0, cfg["repo_root"])`` 再 ``from orchestrator.benchmark
        import contracts``（该模块只用 stdlib，无第三方依赖），所以 ``/assets``
        必须是引擎仓。L1 的 ``task.yaml.assets_manifest_sha256`` 为 null（任务
        本身没有 assets），因此这一格不与「任务资产」冲突；KB 技能包另有
        ``/skills`` 挂载。挂载仍走白名单（⊆ {/work,/skills,/assets,/out}）。
        """
        return REPO_ROOT

    # -- ② 执行 -------------------------------------------------------------
    def _execute(self, ref: ResolvedReference, run: RunPaths, seed: int) -> dict:
        if ref.spec.mode == "host":
            return self._execute_host(ref, run, seed)
        if ref.spec.mode == "docker":
            return self._execute_docker(ref, run, seed)
        if ref.spec.mode == "document":
            return self._execute_document(ref, run, seed)
        raise ReferenceError(f"未知执行形态：{ref.spec.mode!r}")

    def _execute_host(self, ref: ResolvedReference, run: RunPaths,
                      seed: int) -> dict:
        skills = self._skills_rows()
        timeout = float(ref.recipe.get("timeout_s", 600))
        primary = run_host_reference(ref, run, python=self.python,
                                     skills_rows=skills, seed=seed,
                                     timeout=timeout)
        repro_root = ensure_private_dir(run.raw / "repro")
        rerun_dir = ensure_private_dir(repro_root / f"seed{seed}")
        rerun = run_host_reference(ref, run, python=self.python,
                                   skills_rows=skills, seed=seed,
                                   out_dir=rerun_dir,
                                   log_name=f"run_seed{seed}_rerun.log",
                                   timeout=timeout)
        same = compare_metrics_same_seed(
            _read_json(run.candidate / "metrics.json"),
            _read_json(rerun_dir / "metrics.json"))

        others = [s for s in ref.spec.seeds if s != seed]
        cross_seed = int(others[0]) if others else None
        cross_delta = None
        cross_run = None
        if cross_seed is not None:
            cross_dir = ensure_private_dir(repro_root / f"seed{cross_seed}")
            cross_run = run_host_reference(ref, run, python=self.python,
                                          skills_rows=skills, seed=cross_seed,
                                          out_dir=cross_dir,
                                          log_name=f"run_seed{cross_seed}.log",
                                          timeout=timeout)
            cross_delta = cross_seed_delta(
                _read_json(run.candidate / "metrics.json"),
                _read_json(cross_dir / "metrics.json"))
        return {"mode": "host", "primary": primary, "rerun": rerun,
                "same_seed": same, "cross_seed": cross_seed,
                "cross_run": cross_run, "cross_seed_delta_m": cross_delta}

    def _execute_docker(self, ref: ResolvedReference, run: RunPaths,
                        seed: int) -> dict:
        spec = registry.BenchmarkRegistry().get(self.task_id)
        image_ref = libero_image_ref(
            spec, image_name=str(ref.recipe.get("image_name")
                                 or DEFAULT_LIBERO_IMAGE_NAME))
        #: AC-T6-5（锁内适配 2）：LIBERO 依赖只允许存在于镜像内。容器跑之前与
        #: 之后各取一次主 venv ``pip freeze``，逐字节相等才继续；不等即 fail-closed
        #: （说明 L1 把依赖装进了主 venv，主 venv 隔离已破）。
        freeze_before = _venv_freeze(self.python)
        result = run_docker_reference(
            ref, run, image_ref=image_ref,
            skills_dir=Path(self._skills_rows()["stage_root"]),
            assets_root=self._assets_root(), out_dir=run.candidate, seed=seed,
            chown_uid=os.getuid(), chown_gid=os.getgid(),
            timeout=float(ref.recipe.get("timeout_s", 900)))
        freeze_after = _venv_freeze(self.python)
        _write_json(run.raw / "venv_freeze_before.json", freeze_before)
        _write_json(run.raw / "venv_freeze_after.json", freeze_after)
        if freeze_after["stdout_sha256"] != freeze_before["stdout_sha256"]:
            raise ReferenceError(
                "主 venv 在 L1 执行前后变化（pip freeze 不等）："
                f"{freeze_before['stdout_sha256'][:12]}… ≠ "
                f"{freeze_after['stdout_sha256'][:12]}…")
        _install_entry(ref, run.candidate)
        return {"mode": "docker", "image_ref": image_ref,
                "venv_freeze_sha256": freeze_before["stdout_sha256"], **result}

    def _execute_document(self, ref: ResolvedReference, run: RunPaths,
                          seed: int) -> dict:
        result = run_document_reference(
            ref, run, python=self.python, seed=seed,
            timeout=float(ref.recipe.get("timeout_s", 120)))
        # 出生验证 B0 在 workdir（候选目录）里核入口本身，故入口也要落进候选。
        _install_entry(ref, run.candidate)
        return {"mode": "document", **result}

    # -- ③ 出生验证 ---------------------------------------------------------
    def _birth(self, ref: ResolvedReference, run: RunPaths) -> dict:
        if ref.spec.mode == "docker":
            spec = registry.BenchmarkRegistry().get(self.task_id)
            image_ref = libero_image_ref(
                spec, image_name=str(ref.recipe.get("image_name")
                                     or DEFAULT_LIBERO_IMAGE_NAME))
            return birth_container(
                ref, workdir=run.candidate, out_dir=run.birth_dir,
                image_ref=image_ref,
                skills_dir=Path(self._skills_rows()["stage_root"]),
                assets_root=self._assets_root(),
                pythonpath=tuple(ref.recipe.get("container_pythonpath") or ()),
                timeout=float(ref.recipe.get("birth_timeout_s", 180)))
        return birth_host(ref, workdir=run.candidate, out_dir=run.birth_dir,
                          python=self.python,
                          timeout=float(ref.recipe.get("birth_timeout_s", 120)))

    # -- ④ 运行指纹 ---------------------------------------------------------
    def _fingerprint(self, ref: ResolvedReference, run: RunPaths) -> dict:
        spec = registry.BenchmarkRegistry().get(self.task_id)
        packages = rf.declared_packages(spec)
        if ref.spec.mode == "docker":
            image_ref = libero_image_ref(
                spec, image_name=str(ref.recipe.get("image_name")
                                     or DEFAULT_LIBERO_IMAGE_NAME))
            capture = rf.ContainerBackend(
                image_ref, image_name=str(ref.recipe.get("image_name")
                                          or DEFAULT_LIBERO_IMAGE_NAME),
                packages=packages).capture()
            probe = _read_json(run.candidate / "env_probe.json")
            simulator = libero_simulator_segment(
                self.task_id, probe=probe,
                packages=capture.probe.get("packages") or {})
            doc = rf.build_fingerprint(
                spec, capture, simulator=simulator,
                mounts=[Mount(ref.directory, "/work", "ro").fingerprint_entry(),
                        Mount(Path(self._skills_rows()["stage_root"]),
                              "/skills", "ro").fingerprint_entry(),
                        Mount(self._assets_root(), "/assets", "ro").fingerprint_entry(),
                        Mount(run.candidate, "/out", "rw").fingerprint_entry()])
        else:
            capture = rf.HostBackend(self.python, packages=packages).capture()
            doc = rf.build_fingerprint(spec, capture)
        rf.write_fingerprint(run.root, doc, capture)
        return doc

    # -- ⑤ 附加证据 ---------------------------------------------------------
    def _extras(self, ref: ResolvedReference, run: RunPaths,
                execution: Mapping) -> dict:
        written: "dict[str, str]" = {}
        hidden_ref = registry.BenchmarkRegistry().get(
            self.task_id).reproducibility.get("hidden_seed_ref")
        if hidden_ref is not None:
            path = write_hidden_access_audit(
                run.root / "hidden_access_audit.json",
                note="参考实现只读公开种子；private/ 未被读取、写入或修改")
            written["hidden_access_audit.json"] = _sha256_file(path)
        if ref.spec.mode == "host":
            repro = {
                "schema": REPRO_SCHEMA,
                "same_seed_identical": bool(execution["same_seed"]["identical"]),
                "cross_seed_max_delta_m": execution["cross_seed_delta_m"],
                "same_seed_diffs": list(execution["same_seed"]["diffs"]),
                "seed": execution["primary"]["seed"],
                "cross_seed": execution["cross_seed"],
                "note": "同种子重跑逐字段比对（排除 wall_time_seconds）；"
                        "跨种子差取末端距离与最大穿模的最大差",
            }
            path = _write_json(run.root / "reproducibility.json", repro)
            written["reproducibility.json"] = _sha256_file(path)
            #: 评审 W3-10：把两次运行各自的 metrics/telemetry 一并交给评测器。
            #: 没有这两份，HG-EVAL-4 与 20% 复现维度只能采信自报件。
            written.update(self._publish_repro_runs(run, execution))
        evidence = evidence_manifest(
            run.run_id, self._evidence_rels(ref, run.root), run.root)
        evidence_dir = ensure_private_dir(run.root / "evidence")
        path = _write_json(evidence_dir / "manifest.json", evidence)
        written["evidence/manifest.json"] = _sha256_file(path)
        return written

    def _publish_repro_runs(self, run: RunPaths,
                            execution: Mapping) -> "dict[str, str]":
        """把同种子/换种子两次运行的 metrics 与 telemetry 复制到运行根。

        评审 W3-10：``reproducibility.json`` 是**自报**。评测器要能独立重算
        「同种子逐字段相等」与「跨种子差」就必须拿到两份原始件；因此这里把
        ``raw/repro/seed<seed>``（同种子重跑）与 ``raw/repro/seed<cross>``
        （换种子）的 ``metrics.json`` / ``telemetry.jsonl`` 逐件落进
        ``<run.root>/repro/{same_seed,cross_seed}/``，随证据清单一起被 sha 钉住。

        缺件即 fail-closed：宁可让参考执行报错，也不让评测器退回采信自报。
        """
        seed = int(execution["primary"]["seed"])
        pairs: "list[tuple[str, Path]]" = [
            ("same_seed", run.raw / "repro" / f"seed{seed}")]
        cross_seed = execution.get("cross_seed")
        if cross_seed is not None:
            pairs.append(
                ("cross_seed", run.raw / "repro" / f"seed{int(cross_seed)}"))
        written: "dict[str, str]" = {}
        for label, source in pairs:
            target = ensure_private_dir(run.root / "repro" / label)
            for name in ("metrics.json", "telemetry.jsonl"):
                src = source / name
                if not src.is_file():
                    raise ReferenceError(
                        f"{label} 运行缺 {name}，无法交付可重算的复现件：{src}")
                dst = target / name
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
                written[f"repro/{label}/{name}"] = _sha256_file(dst)
        return written

    def _evidence_rels(self, ref: ResolvedReference, root: Path) -> "list[str]":
        spec = registry.BenchmarkRegistry().get(self.task_id)
        rels = [rel for rel in spec.outputs if rel != "run_manifest.json"]
        rels.append(rf.FINGERPRINT_NAME)
        for name in ("reproducibility.json", "hidden_access_audit.json",
                     "birth/functional_birth.json",
                     "repro/same_seed/metrics.json",
                     "repro/same_seed/telemetry.jsonl",
                     "repro/cross_seed/metrics.json",
                     "repro/cross_seed/telemetry.jsonl"):
            if (Path(root) / name).is_file():
                rels.append(name)
        return rels

    # -- ⑥ 上提 + ⑦ run_manifest -------------------------------------------
    def _promote(self, run: RunPaths) -> None:
        """把 ``candidate/`` 里**登记过的产物**逐件上提到运行根。

        评测器只认一个根（``snapshot()`` 从运行根解析全部相对路径），所以产物必须
        在根上；但候选目录里还带着执行用的技能包（``simpraxis_skills/``），它**不是**
        任务产物，按 ``spec.outputs`` 白名单上提即可把它挡在运行根之外。
        """
        spec = registry.BenchmarkRegistry().get(self.task_id)
        for rel in spec.outputs:
            if rel == "run_manifest.json":
                continue
            src = run.candidate / rel
            if not src.is_file():
                continue
            dst = run.root / rel
            ensure_private_dir(dst.parent)
            if dst.exists():
                if _sha256_file(dst) != _sha256_file(src):
                    raise ReferenceError(f"上提冲突（根已有不同内容）：{rel}")
                continue
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)

    def _write_manifest(self, spec: ts.TaskSpec, run: RunPaths,
                        seed: int) -> dict:
        doc = {
            "schema": RUN_MANIFEST_SCHEMA,
            "run_id": run.run_id,
            "mode": "reference",
            "task_id": spec.task_id,
            "task_version": int(spec.version),
            "repository_sha": _git_head(REPO_ROOT),
            "runtime_fingerprint_sha256": _sha256_file(
                run.root / rf.FINGERPRINT_NAME),
            "task_spec_sha256": spec.task_file_sha256,
            "criteria_sha256": spec.criteria_sha256,
            "criteria_lock_sha256": spec.criteria_lock_sha256,
            "seed": int(seed),
            "candidate_artifact_sha256": _candidate_digest(spec, run.root),
            "birth_result_sha256": _sha256_file(
                run.root / "birth" / "functional_birth.json"),
            "evaluator_version": _evaluator_version(),
            "status": PENDING_STATUS,
            "paid_model_calls": 0,
            "evidence_manifest_sha256": _sha256_file(
                run.root / "evidence" / "manifest.json"),
        }
        _validate_manifest(doc)
        _write_json(run.root / "run_manifest.json", doc)
        return doc

    # -- ⑧ 独立评测 ---------------------------------------------------------
    def _evaluate(self, run: RunPaths, *, log_name: str) -> dict:
        script = REPO_ROOT / "scripts" / "benchmark" / "evaluate_run.py"
        argv = [self.python, "-I", str(script), "--task", self.task_id,
                "--run-dir", str(run.root), "--out", str(run.evaluation_dir)]
        result = _run_command(argv, cwd=REPO_ROOT, env=rf.sanitized_env(),
                              timeout=900.0, log=run.raw / log_name)
        _require_rc(result, f"{self.task_id} 独立评测")
        path = run.evaluation_dir / "evaluation_result.json"
        if not path.is_file():
            raise ReferenceError(f"评测器未产出判决件：{path}")
        doc = _read_json(path)
        if not isinstance(doc, dict) or "status" not in doc:
            raise ReferenceError(f"{path}: 判决件不合规")
        return doc

    # -- ⑪ 只读副本 ---------------------------------------------------------
    def _copy_reference(self, run: RunPaths) -> Path:
        target = ensure_private_dir(
            assert_private(paths.references_dir() / self.task_id / run.run_id))
        for src in sorted(run.root.rglob("*")):
            rel = src.relative_to(run.root)
            if rel.parts and rel.parts[0] == "raw":
                continue
            if not src.is_file():
                continue
            dst = target / rel
            ensure_private_dir(dst.parent)
            if dst.exists() and _sha256_file(dst) == _sha256_file(src):
                continue
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o400)
        return target


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _venv_freeze(python: str, *, timeout: float = 120.0) -> dict:
    """取主 venv 的 ``pip freeze`` 快照（AC-T6-5：L1 不得改动主 venv）。

    只落 stdout 的 sha256 与行数；包行本身留在 ``raw/venv_freeze_*.json`` 供人看。
    """
    try:
        res = subprocess.run([python, "-m", "pip", "freeze"], capture_output=True,
                             text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReferenceError(f"取 pip freeze 失败：{exc}") from exc
    if res.returncode != 0:
        raise ReferenceError(
            f"pip freeze rc={res.returncode}：{(res.stderr or '')[-800:]}")
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    return {"python": str(python), "rc": int(res.returncode),
            "lines": len(lines), "packages": lines,
            "stdout_sha256": _sha256_bytes(res.stdout.encode("utf-8"))}


def _install_entry(ref: ResolvedReference, target: Path) -> Path:
    """把参考入口本身放进候选目录（``solution.py`` / ``adapter.py`` 是产物）。"""
    dst = Path(target) / ref.spec.entry
    if dst.exists() and _sha256_file(dst) == _sha256_file(ref.entry_path):
        return dst
    shutil.copyfile(ref.entry_path, dst)
    os.chmod(dst, 0o600)
    return dst


def _git_head(root: Path) -> str:
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReferenceError(f"取仓库 HEAD 失败：{exc}") from exc
    value = res.stdout.strip()
    if res.returncode != 0 or len(value) != 40:
        raise ReferenceError(f"取仓库 HEAD 失败：rc={res.returncode} {value!r}")
    return value


def _candidate_digest(spec: ts.TaskSpec, root: Path) -> str:
    from orchestrator.benchmark.result_schema import candidate_digest
    digests = {rel: _sha256_file(Path(root) / rel)
               for rel in spec.outputs
               if rel != "run_manifest.json" and (Path(root) / rel).is_file()}
    return candidate_digest(digests)


def _evaluator_version() -> int:
    from orchestrator.benchmark.evaluator import EVALUATOR_VERSION
    return int(EVALUATOR_VERSION)


def _validate_manifest(doc: Mapping) -> None:
    import jsonschema
    path = ts.BENCHMARK_ROOT / "schemas" / "run_manifest.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(dict(doc)),
        key=lambda e: (list(map(str, e.absolute_path)), e.message))
    if errors:
        first = errors[0]
        where = "/".join(map(str, first.absolute_path)) or "<root>"
        raise ReferenceError(
            f"run_manifest 不合 schema：{where}: {first.message}")
