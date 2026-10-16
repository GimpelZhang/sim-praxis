"""解题方案 manifest 解析器（Task0b 三层分离，引擎/内容边界唯一闸口）。

内容层 = ``$SIMPRAXIS_SOLUTIONS_ROOT``（默认 ``$KB_ROOT/solutions``，
再缺省 /home/vipuser/embodied-ai-sim-knowledge-base/solutions）。容器内挂载点
为 /kb（docker-compose 只读绑定，见 sandbox/docker-compose.yml），由
SIMPRAXIS_SOLUTIONS_ROOT=/kb 覆盖。

安全口径与 common.validators 同源（C4）：solution_id 与每个文件 relpath 逐段
字符集校验，manifest 中任何路径都必须仍解析在方案目录内；sha256 逐件复算。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from orchestrator.common.fsutil import atomic_write_bytes, ensure_private_dir

SCHEMA_ID = "simpraxis.solution_manifest.v1"
DEFAULT_KB_ROOT = "/home/vipuser/embodied-ai-sim-knowledge-base"

# solution_id：track{1,2}_{tools|questions}/NN_slug（与 validators._ID_RE 同族，
# 仅多允许一个字面 "/" 分隔与两位序号前缀）。
_SOLUTION_ID_RE = re.compile(
    r"^track[12]_(?:tools|questions)/[0-9]{2}_[a-z0-9_]+$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRACKS = ("track1", "track2")


class SolutionError(Exception):
    """方案清单/落件不合规：调用方必须停止，不得兜底生成。"""


@dataclass(frozen=True)
class SolutionFile:
    relpath: str
    sha256: str


@dataclass(frozen=True)
class StagedSolution:
    workdir: Path
    entrypoint: Path
    files: dict[str, Path]
    manifest: "SolutionManifest"


@dataclass(frozen=True)
class SolutionManifest:
    solution_dir: Path
    solution_id: str
    title: str
    entrypoint: str
    args: tuple[str, ...]
    sim_slug: str
    sim_min_version: str
    dependencies: dict
    rung_thresholds: dict
    frozen_inputs: tuple[tuple[str, int], ...]
    files: tuple[SolutionFile, ...]
    v1_run_relpath: str | None
    provenance: dict

    @property
    def file_map(self) -> dict[str, SolutionFile]:
        return {f.relpath: f for f in self.files}

    def stage(self, workdir: "str | Path") -> StagedSolution:
        """把方案逐件落进 workdir（0700 目录；入口 0700，其余 0600）。

        幂等语义对齐 Task7/8 既有 stage：已存在的落件不覆盖——coding 轮次在
        workdir 内的补丁必须跨 spec 重建保留；新副本（新 run 目录）才逐件写入。
        新写入件落盘后复算 sha256，不符即 SolutionError（理论不可能，防御性）。
        """
        work = ensure_private_dir(Path(workdir), mode=0o700)
        staged: dict[str, Path] = {}
        for sf in self.files:
            src = self.solution_dir / sf.relpath
            dst = work / sf.relpath
            if not dst.exists():
                ensure_private_dir(dst.parent, mode=0o700)
                mode = 0o700 if sf.relpath == self.entrypoint else 0o600
                atomic_write_bytes(dst, src.read_bytes(), mode=mode)
            if not dst.is_file():
                raise SolutionError(
                    f"stage 落件不是常规文件：{dst}（方案 {self.solution_id}）")
            actual = hashlib.sha256(dst.read_bytes()).hexdigest()
            if actual != sf.sha256:
                raise SolutionError(
                    f"stage 后 sha256 不符：{sf.relpath} manifest="
                    f"{sf.sha256[:12]}… 实际={actual[:12]}…（"
                    f"方案 {self.solution_id}，疑似 workdir 内被改写）")
            staged[sf.relpath] = dst
        entry = staged.get(self.entrypoint)
        if entry is None:
            raise SolutionError(
                f"entrypoint {self.entrypoint!r} 不在 files[] 中"
                f"（方案 {self.solution_id}）")
        return StagedSolution(workdir=work, entrypoint=entry,
                              files=staged, manifest=self)


def solutions_root(kb_root: "str | Path | None" = None) -> Path:
    """内容层根：$SIMPRAXIS_SOLUTIONS_ROOT > $KB_ROOT/solutions > 默认 KB 路径。"""
    override = os.environ.get("SIMPRAXIS_SOLUTIONS_ROOT")
    if override:
        return Path(override)
    if kb_root is not None:
        return Path(kb_root) / "solutions"
    return Path(os.environ.get("KB_ROOT", DEFAULT_KB_ROOT)) / "solutions"


def _require_mapping(obj: object, key: str, what: str) -> dict:
    if not isinstance(obj, dict):
        raise SolutionError(f"{what}：{key} 必须是映射，收到 {type(obj).__name__}")
    return obj


def _validate_relpath(rel: object, solution_dir: Path) -> Path:
    if not isinstance(rel, str) or not rel:
        raise SolutionError(f"files[].relpath 必须是非空字符串，收到 {rel!r}")
    if rel.startswith("/") or rel.startswith("\\"):
        raise SolutionError(f"files[].relpath 不允许绝对路径：{rel!r}")
    norm = rel.replace("\\", "/")
    segments = norm.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise SolutionError(f"files[].relpath 含空段/'.'/'..'：{rel!r}")
    for seg in segments:
        if not _SEGMENT_RE.match(seg):
            raise SolutionError(
                f"files[].relpath 段 {seg!r} 不符 C4 字符集 [A-Za-z0-9_.-]")
    target = (solution_dir / norm).resolve()
    if target != solution_dir.resolve() \
            and solution_dir.resolve() not in target.parents:
        raise SolutionError(f"files[].relpath 逃逸方案目录：{rel!r}")
    return target


def _validate_v1_relpath(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SolutionError("v1_run_relpath 必须是非空字符串或 null")
    norm = value.replace("\\", "/")
    if norm.startswith("/"):
        raise SolutionError(f"v1_run_relpath 必须相对 $SIMPRAXIS_RUN_DIR：{value!r}")
    segments = norm.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise SolutionError(f"v1_run_relpath 含空段/'.'/'..'：{value!r}")
    for seg in segments:
        if not _SEGMENT_RE.match(seg):
            raise SolutionError(
                f"v1_run_relpath 段 {seg!r} 不符 C4 字符集 [A-Za-z0-9_.-]")
    return norm


def load_manifest(solution_dir: "str | Path") -> SolutionManifest:
    """解析并全量校验单个方案目录（含逐件 sha256 复算）。"""
    sdir = Path(solution_dir).resolve()
    manifest_path = sdir / "manifest.yaml"
    if not sdir.is_dir():
        raise SolutionError(f"方案目录不存在：{sdir}")
    if not manifest_path.is_file():
        raise SolutionError(f"方案缺少 manifest.yaml：{manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SolutionError(f"manifest.yaml 不可读/非法 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise SolutionError("manifest.yaml 顶层必须是映射")

    schema = raw.get("schema")
    if schema != SCHEMA_ID:
        raise SolutionError(f"schema 必须是 {SCHEMA_ID}，收到 {schema!r}")

    sid = raw.get("solution_id")
    if not isinstance(sid, str) or not _SOLUTION_ID_RE.match(sid):
        raise SolutionError(
            "solution_id 必须形如 track{1,2}_{tools|questions}/NN_slug，"
            f"收到 {sid!r}")
    if sdir.name != sid.split("/")[-1] or sdir.parent.name != sid.split("/")[0]:
        raise SolutionError(
            f"solution_id {sid!r} 与目录名 {sdir.parent.name}/{sdir.name} 不一致")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SolutionError("title 必须是非空字符串")

    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise SolutionError("entrypoint 必须是非空字符串")
    entry_norm = entrypoint.replace("\\", "/")
    if "/" in entry_norm:
        # 入口允许子目录形式，但逐段仍受 C4 校验（files[] 循环会覆盖）。
        for seg in entry_norm.split("/"):
            if not _SEGMENT_RE.match(seg) or seg in (".", ".."):
                raise SolutionError(f"entrypoint 段 {seg!r} 不符 C4")

    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise SolutionError("args 必须是字符串列表")

    eng = _require_mapping(raw.get("sim_engine"), "sim_engine", sid)
    sim_slug = eng.get("slug")
    sim_min_version = eng.get("min_version")
    if not isinstance(sim_slug, str) or not re.match(r"^[a-z0-9_]+$", sim_slug):
        raise SolutionError(f"sim_engine.slug 非法：{sim_slug!r}")
    if not isinstance(sim_min_version, str) or not sim_min_version.strip():
        raise SolutionError("sim_engine.min_version 必须是非空字符串")

    deps = _require_mapping(raw.get("dependencies"), "dependencies", sid)
    pip = deps.get("pip", [])
    if not isinstance(pip, list) or not all(isinstance(p, str) for p in pip):
        raise SolutionError("dependencies.pip 必须是字符串列表（空列表=引擎 pins）")

    rungs = _require_mapping(
        raw.get("rung_thresholds"), "rung_thresholds", sid)
    ref = rungs.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise SolutionError("rung_thresholds.ref 必须是非空字符串")
    constants = rungs.get("constants", [])
    if not isinstance(constants, list) \
            or not all(isinstance(c, str) for c in constants):
        raise SolutionError("rung_thresholds.constants 必须是字符串列表")

    fin = raw.get("frozen_inputs")
    if not isinstance(fin, list) or not fin:
        raise SolutionError("frozen_inputs 必须是非空列表")
    frozen: list[tuple[str, int]] = []
    for item in fin:
        if not isinstance(item, dict):
            raise SolutionError(f"frozen_inputs 项必须是映射：{item!r}")
        track = item.get("track")
        index = item.get("index")
        if track not in _TRACKS:
            raise SolutionError(f"frozen_inputs.track 必须 ∈ {_TRACKS}：{track!r}")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise SolutionError(f"frozen_inputs.index 必须是 >=1 整数：{index!r}")
        frozen.append((track, index))

    files_raw = raw.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise SolutionError("files 必须是非空列表")
    files: list[SolutionFile] = []
    seen: set[str] = set()
    for item in files_raw:
        if not isinstance(item, dict):
            raise SolutionError(f"files 项必须是映射：{item!r}")
        target = _validate_relpath(item.get("relpath"), sdir)
        rel = item["relpath"].replace("\\", "/")
        if rel in seen:
            raise SolutionError(f"files[].relpath 重复：{rel}")
        seen.add(rel)
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise SolutionError(
                f"files[{rel}].sha256 必须是 64 位小写十六进制")
        if not target.is_file():
            raise SolutionError(f"清单文件不存在或非常规文件：{target}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            raise SolutionError(
                f"sha256 不符（fail-closed）：{rel} manifest={digest[:12]}… "
                f"实际={actual[:12]}…（方案 {sid}）")
        files.append(SolutionFile(relpath=rel, sha256=digest))

    if entry_norm not in seen:
        raise SolutionError(
            f"entrypoint {entrypoint!r} 不在 files[] 中（方案 {sid}）")

    v1 = _validate_v1_relpath(raw.get("v1_run_relpath"))

    provenance = raw.get("provenance", {})
    if not isinstance(provenance, dict):
        raise SolutionError("provenance 必须是映射或缺省")
    repo_sha = provenance.get("repo_version_sha256")
    if repo_sha is not None:
        if not isinstance(repo_sha, str) or not _SHA256_RE.match(repo_sha):
            raise SolutionError(
                "provenance.repo_version_sha256 必须是 64 位小写十六进制")

    return SolutionManifest(
        solution_dir=sdir,
        solution_id=sid,
        title=title.strip(),
        entrypoint=entry_norm,
        args=tuple(args),
        sim_slug=sim_slug,
        sim_min_version=sim_min_version.strip(),
        dependencies=deps,
        rung_thresholds={"ref": ref, "constants": constants},
        frozen_inputs=tuple(frozen),
        files=tuple(files),
        v1_run_relpath=v1,
        provenance=provenance,
    )


def resolve_solution(solution_id: str,
                     kb_root: "str | Path | None" = None) -> SolutionManifest:
    """按 solution_id 从内容层解析方案（缺目录/违规均 SolutionError）。"""
    if not isinstance(solution_id, str) or not _SOLUTION_ID_RE.match(solution_id):
        raise SolutionError(
            "solution_id 必须形如 track{1,2}_{tools|questions}/NN_slug，"
            f"收到 {solution_id!r}")
    root = solutions_root(kb_root).resolve()
    sdir = root / solution_id
    if not sdir.is_dir():
        raise SolutionError(
            f"方案不存在：{sdir}（solutions_root={root}；"
            "容器内需挂载 /kb 并设 SIMPRAXIS_SOLUTIONS_ROOT=/kb）")
    return load_manifest(sdir)
