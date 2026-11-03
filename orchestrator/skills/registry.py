"""可执行技能库的**索引**层（Phase 2.7 Task 2 / D2-3）。

三层分离（硬规则 3 + BR-4）：

- 技能代码在**内容层** ``$KB_ROOT/skills``（KB 仓，公开 Apache-2.0），
  清单 schema ``simpraxis.skills_manifest.v1``；
- 本模块**只读清单**、返回元数据（``SkillEntry``），**永不 import** 技能
  代码——引擎进程里跑内容层代码会同时破坏分层与供应链边界（D2-3），
  且宿主机没有 genesis / libero 依赖；
- 容器内的可用性由 ``scripts/phase27/stage_skills.py`` 的 staged 副本
  提供（``$SIMPRAXIS_PHASE27_ROOT/skills_stage/<manifest_sha12>/``，
  0700 目录 + 0600 文件 + 无符号链接），以 ``/skills:ro`` 挂载，
  任务脚本自行 ``sys.path.insert(0, "/skills")`` 后
  ``import simpraxis_skills.<domain>.<name>``。

fail-closed 口径与 ``orchestrator/solutions/resolver.py`` 同源：manifest
不符、文件被改、entry 名与源码不符，一律 ``SkillsError``，不存在
「尽力而为地加载」。
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKILLS_SCHEMA_ID = "simpraxis.skills_manifest.v1"
DEFAULT_KB_ROOT = "/home/vipuser/embodied-ai-sim-knowledge-base"
MANIFEST_NAME = "manifest.yaml"

# 容器内包名：staged 目录把 skills/ 重根成 simpraxis_skills/ 后以
# sys.path.insert(0, "/skills") 导入（Plan §T2.4/D2-4、§D4-4 的
# ``from simpraxis_skills.wall_b import ...`` 依赖同一名字）。
PACKAGE_NAME = "simpraxis_skills"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVENANCE_KINDS = ("new", "extracted")

_REQUIRED_SKILL_KEYS = ("name", "relpath", "sha256", "entry", "tests",
                        "tests_sha256", "requires", "provenance")


class SkillsError(RuntimeError):
    """技能清单/落件不合规：调用方必须停止，不得兜底加载。"""


@dataclass(frozen=True)
class SkillEntry:
    """一条技能登记的只读视图。

    ``staged_path`` 为 ``None`` 表示「只有元数据」（未暂存或调用方只要
    清单信息）；给出 ``staged_root`` 时必须是已存在的 staged 副本。
    """

    name: str
    sha256: str
    staged_path: "Path | None"
    entry: "tuple[str, ...]"
    doc: str
    requires: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    relpath: str = ""


# --------------------------------------------------------------- roots


def skills_root(kb_root: "str | Path | None" = None) -> Path:
    """内容层技能根：$SIMPRAXIS_SKILLS_ROOT > $KB_ROOT/skills > 默认 KB 路径。"""
    override = os.environ.get("SIMPRAXIS_SKILLS_ROOT")
    if override:
        return Path(override)
    if kb_root is not None:
        return Path(kb_root) / "skills"
    return Path(os.environ.get("KB_ROOT", DEFAULT_KB_ROOT)) / "skills"


def manifest_path(root: "str | Path | None" = None) -> Path:
    return (Path(root) if root is not None else skills_root()) / MANIFEST_NAME


def manifest_sha12(root: "str | Path | None" = None) -> str:
    """清单文件字节的 sha256 前 12 位：staged 目录名的唯一来源（D2-5）。"""
    path = manifest_path(root)
    if not path.is_file():
        raise SkillsError(f"技能清单不存在：{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# ------------------------------------------------------------ helpers


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SkillsError(msg)


def _check_relpath(rel: object, root: Path, what: str) -> Path:
    """relpath C4 校验 + 逃逸检查；返回绝对路径（不保证存在）。"""
    _require(isinstance(rel, str) and bool(rel), f"{what} 必须是非空字符串：{rel!r}")
    norm = rel.replace("\\", "/")
    _require(not norm.startswith("/"), f"{what} 不允许绝对路径：{rel!r}")
    segments = norm.split("/")
    _require(all(seg not in ("", ".", "..") for seg in segments),
             f"{what} 含空段/'.'/'..'：{rel!r}")
    for seg in segments:
        _require(bool(_SEGMENT_RE.match(seg)),
                 f"{what} 段 {seg!r} 不符 C4 字符集 [A-Za-z0-9_.-]")
    root_r = root.resolve()
    target = (root_r / norm).resolve()
    _require(target == root_r or root_r in target.parents,
             f"{what} 逃逸技能根：{rel!r}")
    return target


def _module_doc(text: str, relpath: str) -> str:
    """模块 docstring 的首个非空行（用 ast 解析，**不执行**代码）。"""
    try:
        tree = ast.parse(text, filename=relpath)
    except SyntaxError as exc:                                      # pragma: no cover
        raise SkillsError(f"技能源码无法解析（{relpath}）：{exc}") from exc
    doc = ast.get_docstring(tree) or ""
    first = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
    _require(bool(first), f"技能模块缺少 docstring（{relpath}）——技能必须自述口径")
    return first


def _top_level_functions(text: str, relpath: str) -> set:
    tree = ast.parse(text, filename=relpath)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# ------------------------------------------------------------ manifest


def load_skills_manifest(root: "str | Path | None" = None,
                         verify_provenance_: bool = False) -> dict:
    """读取并**逐件复算**清单；任何不符即 ``SkillsError``。

    ``verify_provenance_`` 为真时对 ``kind: extracted`` 的条目执行
    ``git -C <KB> cat-file -e <kb_commit>:<source>``（不触网，只读本地
    对象库）。活表 ``phase1_learnings`` 的 evidence 校验在机判测试里做
    （需要 LanceDB，见 tests/milestone27/test_task2_skill_library.py）。
    """
    root_path = Path(root) if root is not None else skills_root()
    path = root_path / MANIFEST_NAME
    if not path.is_file():
        raise SkillsError(f"技能清单不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SkillsError(f"技能清单 YAML 解析失败：{exc}") from exc
    _require(isinstance(data, dict), f"技能清单必须是映射：{path}")
    _require(data.get("schema") == SKILLS_SCHEMA_ID,
             f"技能清单 schema 必须是 {SKILLS_SCHEMA_ID}，"
             f"收到 {data.get('schema')!r}")

    skills = data.get("skills")
    _require(isinstance(skills, list) and bool(skills),
             "技能清单 skills 必须是非空列表")

    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    entries: list[dict] = []
    for idx, item in enumerate(skills):
        where = f"skills[{idx}]"
        _require(isinstance(item, dict), f"{where} 必须是映射")
        missing = [k for k in _REQUIRED_SKILL_KEYS if k not in item]
        _require(not missing, f"{where} 缺字段 {missing}")
        name = item["name"]
        _require(isinstance(name, str) and bool(_NAME_RE.match(name)),
                 f"{where}.name 必须形如 <domain>.<snake_case>：{name!r}")
        _require(name not in seen_names, f"技能名重复：{name}")
        seen_names.add(name)

        rel = item["relpath"]
        _require(isinstance(rel, str) and rel.startswith("skills/"),
                 f"{where}.relpath 必须以 skills/ 开头：{rel!r}")
        _require(rel not in seen_paths, f"技能 relpath 重复：{rel}")
        seen_paths.add(rel)
        target = _check_relpath(rel, root_path.parent, f"{where}.relpath")
        _require(target.is_file(), f"技能文件不存在：{rel}")
        sha = item["sha256"]
        _require(isinstance(sha, str) and bool(_SHA256_RE.match(sha)),
                 f"{where}.sha256 必须是 64 位小写 hex：{sha!r}")
        actual = _sha256_file(target)
        _require(actual == sha,
                 f"技能文件 sha256 不符：{rel} manifest={sha[:12]}… "
                 f"实际={actual[:12]}…（改了文件就必须改清单）")

        entry = item["entry"]
        _require(isinstance(entry, list) and bool(entry),
                 f"{where}.entry 必须是非空列表")
        _require(all(isinstance(n, str) and _IDENT_RE.match(n) for n in entry),
                 f"{where}.entry 每项必须是标识符：{entry!r}")
        _require(len(set(entry)) == len(entry), f"{where}.entry 含重复名：{entry!r}")
        text = target.read_text(encoding="utf-8")
        defined = _top_level_functions(text, rel)
        missing_fns = [n for n in entry if n not in defined]
        _require(not missing_fns,
                 f"{where}.entry 声明了源码里不存在的顶层函数 {missing_fns}（{rel}）")
        doc = _module_doc(text, rel)

        tests = item["tests"]
        _require(isinstance(tests, str) and tests.startswith("skills/"),
                 f"{where}.tests 必须以 skills/ 开头：{tests!r}")
        test_path = _check_relpath(tests, root_path.parent, f"{where}.tests")
        _require(test_path.is_file(), f"技能测试文件不存在：{tests}")
        tsha = item["tests_sha256"]
        _require(isinstance(tsha, str) and bool(_SHA256_RE.match(tsha)),
                 f"{where}.tests_sha256 必须是 64 位小写 hex：{tsha!r}")
        t_actual = _sha256_file(test_path)
        _require(t_actual == tsha,
                 f"技能测试文件 sha256 不符：{tests} manifest={tsha[:12]}… "
                 f"实际={t_actual[:12]}…（改判据必须同步改清单）")

        requires = item["requires"]
        _require(isinstance(requires, dict), f"{where}.requires 必须是映射")
        prov = item["provenance"]
        _require(isinstance(prov, dict), f"{where}.provenance 必须是映射")
        kind = prov.get("kind")
        _require(kind in _PROVENANCE_KINDS,
                 f"{where}.provenance.kind 必须是 {_PROVENANCE_KINDS}：{kind!r}")
        evidence = prov.get("evidence")
        _require(isinstance(evidence, str) and bool(evidence.strip()),
                 f"{where}.provenance.evidence 必填（没有验证证据的代码不入库）")
        if kind == "new":
            _require(prov.get("source") is None,
                     f"{where}.provenance.kind=new 时 source 必须为 null")
        else:
            source = prov.get("source")
            _require(isinstance(source, str) and bool(source),
                     f"{where}.provenance.kind=extracted 时 source 必填")
            _check_relpath(source, root_path.parent, f"{where}.provenance.source")
            _require(isinstance(prov.get("kb_commit"), str)
                     and bool(prov.get("kb_commit").strip()),
                     f"{where}.provenance.kind=extracted 时 kb_commit 必填")

        entries.append({"name": name, "relpath": rel, "sha256": sha,
                        "entry": tuple(entry), "doc": doc,
                        "requires": dict(requires), "provenance": dict(prov),
                        "tests": tests, "tests_sha256": tsha})

    packages = data.get("packages", []) or []
    _require(isinstance(packages, list), "技能清单 packages 必须是列表")
    for idx, item in enumerate(packages):
        where = f"packages[{idx}]"
        _require(isinstance(item, dict), f"{where} 必须是映射")
        rel = item.get("relpath")
        _require(isinstance(rel, str) and rel.startswith("skills/"),
                 f"{where}.relpath 必须以 skills/ 开头：{rel!r}")
        target = _check_relpath(rel, root_path.parent, f"{where}.relpath")
        _require(target.is_file(), f"包标记文件不存在：{rel}")
        sha = item.get("sha256")
        _require(isinstance(sha, str) and bool(_SHA256_RE.match(sha)),
                 f"{where}.sha256 必须是 64 位小写 hex：{sha!r}")
        actual = _sha256_file(target)
        _require(actual == sha,
                 f"包标记文件 sha256 不符：{rel} manifest={sha[:12]}… "
                 f"实际={actual[:12]}…")

    out = {"schema": SKILLS_SCHEMA_ID, "root": str(root_path),
           "skills": entries, "packages": list(packages)}
    if verify_provenance_:
        verify_provenance(out)
    return out


def verify_provenance(manifest: dict) -> None:
    """``kind: extracted`` 的 source 必须真实存在于 ``kb_commit`` 里（本地 git 对象库）。"""
    root = Path(manifest["root"])
    kb_root = root.parent
    for item in manifest["skills"]:
        prov = item["provenance"]
        if prov.get("kind") != "extracted":
            continue
        spec = f"{prov['kb_commit']}:{prov['source']}"
        try:
            proc = subprocess.run(
                ["git", "-C", str(kb_root), "cat-file", "-e", spec],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:      # pragma: no cover
            raise SkillsError(f"git 溯源检查失败（{spec}）：{exc}") from exc
        _require(proc.returncode == 0,
                 f"provenance.source 在 kb_commit 里不存在：{spec}"
                 f"（git rc={proc.returncode} {proc.stderr.strip()[:200]}）")


# -------------------------------------------------------------- entries


def list_skills(root: "str | Path | None" = None,
                staged_root: "str | Path | None" = None) -> "tuple[SkillEntry, ...]":
    """返回全部技能（按 name 排序的确定性顺序；D2-6 静态列举，不做向量召回）。"""
    manifest = load_skills_manifest(root)
    out = []
    for item in manifest["skills"]:
        staged = None
        if staged_root is not None:
            staged = staged_path_for(item["relpath"], staged_root)
            _require(staged.is_file(), f"staged 副本不存在：{staged}")
            _require(_sha256_file(staged) == item["sha256"],
                     f"staged 副本 sha256 与清单不符：{staged}")
        out.append(SkillEntry(
            name=item["name"], sha256=item["sha256"], staged_path=staged,
            entry=item["entry"], doc=item["doc"], requires=item["requires"],
            provenance=item["provenance"], relpath=item["relpath"]))
    return tuple(sorted(out, key=lambda e: e.name))


def get_skill(name: str, root: "str | Path | None" = None,
              staged_root: "str | Path | None" = None) -> SkillEntry:
    """按名取技能；未登记即 ``SkillsError``（不返回 None）。"""
    for entry in list_skills(root=root, staged_root=staged_root):
        if entry.name == name:
            return entry
    raise SkillsError(f"未登记的技能：{name!r}")


def staged_path_for(relpath: str, staged_root: "str | Path") -> Path:
    """``skills/<domain>/<file>.py`` → ``<staged_root>/simpraxis_skills/<domain>/<file>.py``。"""
    _require(isinstance(relpath, str) and relpath.startswith("skills/"),
             f"relpath 必须以 skills/ 开头：{relpath!r}")
    inner = relpath[len("skills/"):]
    return Path(staged_root) / PACKAGE_NAME / inner


def env_spec_skills_block(root: "str | Path | None" = None) -> str:
    """注入 ENV_SPEC 的「可用技能」段（纯文本、确定性顺序，不含时间戳）。"""
    lines = [
        "可用技能（staged 于容器 /skills；脚本内 "
        'sys.path.insert(0, "/skills") 后 import '
        f"{PACKAGE_NAME}.<domain>.<module>）：",
    ]
    for entry in list_skills(root=root):
        domain, _, _ = entry.name.partition(".")
        mod = entry.relpath.rsplit("/", 1)[-1].removesuffix(".py")
        calls = " / ".join(entry.entry)
        lines.append(f"- {entry.name}（{PACKAGE_NAME}.{domain}.{mod}）：{calls}")
        lines.append(f"    口径：{entry.doc}")
    return "\n".join(lines)
