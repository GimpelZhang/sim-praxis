"""Phase 2.8.1 T1：**仓外信任锚**的写与重算比对。

候选评测的信任前提是「候选碰不到判决所依赖的字节」。Phase 2.8.0 的教训
（评审 W3-01 / W3-04）是：判据文件在仓内、评测器与候选同处一树时，一份能改仓的
候选就能改判据。本模块把三样东西搬到**仓外**（``<phase281 root>/private/``）：

1. 三任务的 ``criteria`` 快照与 ``runtime`` 要求快照（逐件 sha256）；
2. 判据数学所在 ``orchestrator/benchmark/scoring.py`` 的**源码字节**摘要；
3. 评测器自身三个模块的源码摘要（``evaluator_sources``）。

外加一份**自锚**：``manifest_sha256`` 是本文件除该字段外、按
``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
规范化后再 sha256。这样「锚文件被改一个字节」必然被发现，且不依赖 mtime
（mtime 可 touch，内容哈希不可伪造）。

**为什么 ``runtime`` 也进锚**（计划 §5.5 的锚字段表未列，锁内适配登记于
Plan_Complete）：AC-P281-1-11 要求负控 parity 覆盖 Phase 2.8.0 的全部 20 例，
其中 ``l1_n03_version_mismatch`` / ``l1_n07_host_impersonation`` 的期望判决是
``INVALID_RUNTIME``（本阶段按计划 §5.4.2 投影为 ``BLOCKED_INFRA``）。没有冻结的
runtime 要求，评测器就无从判定「运行时身份不符」，这两例会被错判成 FAIL。
把 ``task.yaml.runtime`` 快照进锚是**加字段**而非改判据，仍在仓外。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.fsutil import atomic_write_bytes, ensure_private_dir
from orchestrator.benchmark.phase281 import constants

__all__ = (
    "ANCHOR_NAME",
    "AnchorError",
    "AnchorManifest",
    "CRITERIA_DIR",
    "RUNTIME_DIR",
    "anchor_path",
    "canonical_json",
    "load_anchor",
    "self_digest",
    "verify_anchor",
    "write_anchor",
)

ANCHOR_NAME = "anchor_manifest.json"
CRITERIA_DIR = "criteria"
RUNTIME_DIR = "runtime"

#: 锚里登记摘要的评测器模块（相对 ``orchestrator/benchmark/phase281/``）。
#: ``evaluate_child.py`` 是评测真正跑起来的进程入口、``references.py`` 是参考
#: 快照的构造者——两者都在信任面内，摘要一起进锚（加模块 = 加严，不改判定）。
EVALUATOR_MODULES = ("candidate_manifest.py", "anchor.py",
                     "candidate_evaluator.py", "evaluate_child.py",
                     "references.py")

REPO_ROOT = Path(__file__).resolve().parents[3]

CRITERIA_SOURCE_DIR = (REPO_ROOT / "benchmarks" / "research_engineering_v1"
                       / "criteria")
TASK_SOURCE_DIR = (REPO_ROOT / "benchmarks" / "research_engineering_v1"
                   / "tasks")
SCHEMA_SOURCE_DIR = (REPO_ROOT / "benchmarks" / "research_engineering_v1"
                     / "schemas")
SCORING_SOURCE = REPO_ROOT / "orchestrator" / "benchmark" / "scoring.py"
RUN_MANIFEST_SCHEMA = SCHEMA_SOURCE_DIR / "run_manifest.v1.schema.json"


class AnchorError(RuntimeError):
    """锚的写/读/校验失败。**任一失败都意味着不得付费运行**（STOP-05）。"""


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj: Mapping | list) -> str:
    """规范化 JSON（自锚的序列化口径，改一个字符就变摘要）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def self_digest(doc: Mapping) -> str:
    """``manifest_sha256`` 的重算规则：把该字段置空后规范化再 sha256。"""
    probe = {key: value for key, value in doc.items() if key != "manifest_sha256"}
    probe["manifest_sha256"] = ""
    return _sha256_bytes(canonical_json(probe).encode("utf-8"))


def _task_source_dir(task_id: str) -> Path:
    """``<task_id>`` → ``benchmarks/.../tasks/<name>``（去掉 ``_v1`` 后缀）。"""
    name = task_id[:-3] if task_id.endswith("_v1") else task_id
    return TASK_SOURCE_DIR / name


@dataclass(frozen=True)
class AnchorManifest:
    """仓外锚的内存形态（字段与计划 §5.5 的锚字段表一一对应）。"""

    experiment_id: str
    criteria: Mapping[str, dict]
    runtime: Mapping[str, dict]
    scoring_sha256: str
    evaluator_sources: Mapping[str, str]
    run_manifest_schema_sha256: str
    written_at: str
    written_by: str
    manifest_sha256: str

    def to_json(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "criteria": {k: dict(v) for k, v in self.criteria.items()},
            "runtime": {k: dict(v) for k, v in self.runtime.items()},
            "scoring_sha256": self.scoring_sha256,
            "evaluator_sources": dict(self.evaluator_sources),
            "run_manifest_schema_sha256": self.run_manifest_schema_sha256,
            "written_at": self.written_at,
            "written_by": self.written_by,
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "AnchorManifest":
        required = ("experiment_id", "criteria", "runtime", "scoring_sha256",
                    "evaluator_sources", "run_manifest_schema_sha256",
                    "written_at", "written_by", "manifest_sha256")
        missing = [key for key in required if key not in doc]
        if missing:
            raise AnchorError(f"锚缺字段：{missing}")
        extra = sorted(set(doc) - set(required))
        if extra:
            raise AnchorError(f"锚多出未登记字段：{extra}")
        if doc["experiment_id"] != constants.EXPERIMENT_ID:
            raise AnchorError(
                f"锚的 experiment_id 不符：{doc['experiment_id']!r} "
                f"（应为 {constants.EXPERIMENT_ID!r}）")
        return cls(
            experiment_id=str(doc["experiment_id"]),
            criteria={str(k): dict(v) for k, v in dict(doc["criteria"]).items()},
            runtime={str(k): dict(v) for k, v in dict(doc["runtime"]).items()},
            scoring_sha256=str(doc["scoring_sha256"]),
            evaluator_sources={str(k): str(v)
                               for k, v in dict(doc["evaluator_sources"]).items()},
            run_manifest_schema_sha256=str(doc["run_manifest_schema_sha256"]),
            written_at=str(doc["written_at"]),
            written_by=str(doc["written_by"]),
            manifest_sha256=str(doc["manifest_sha256"]),
        )


def anchor_path(root: "str | Path") -> Path:
    """锚的固定落点：``<root>/private/anchor_manifest.json``。"""
    return Path(root) / "private" / ANCHOR_NAME


def _load_task_runtime(task_id: str) -> dict:
    import yaml  # Phase 2.8.0 已依赖，不新增

    path = _task_source_dir(task_id) / "task.yaml"
    if not path.is_file():
        raise AnchorError(f"任务 yaml 不在场：{path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    runtime = doc.get("runtime") if isinstance(doc, Mapping) else None
    if not isinstance(runtime, Mapping):
        raise AnchorError(f"{path} 缺 runtime 段")
    return dict(runtime)


def write_anchor(root: "str | Path", *, written_by: str = "phase281_anchor"
                 ) -> AnchorManifest:
    """把三任务判据/运行时与四份源码摘要快照进 ``<root>/private/``，返回锚对象。

    幂等：同一仓状态下重复写得到**同一份 bytes**（除 ``written_at``）。
    """
    base = Path(root)
    if not base.is_dir():
        raise AnchorError(f"数据根不在场：{base}（锚必须写在既有的 0700 数据根里）")
    private = ensure_private_dir(base / "private")
    crit_dir = ensure_private_dir(private / CRITERIA_DIR)
    rt_dir = ensure_private_dir(private / RUNTIME_DIR)

    criteria: "dict[str, dict]" = {}
    runtime: "dict[str, dict]" = {}
    for task_id in constants.TASK_IDS:
        src = CRITERIA_SOURCE_DIR / f"{task_id}.criteria.json"
        if not src.is_file():
            raise AnchorError(f"判据源文件不在场：{src}")
        data = src.read_bytes()
        dst = crit_dir / f"{task_id}.json"
        atomic_write_bytes(dst, data)
        criteria[task_id] = {
            "path": str(dst),
            "sha256": _sha256_bytes(data),
            "remote_path": f"benchmarks/research_engineering_v1/criteria/"
                           f"{task_id}.criteria.json",
        }
        runtime_doc = _load_task_runtime(task_id)
        runtime_bytes = (json.dumps(runtime_doc, sort_keys=True, indent=2,
                                    ensure_ascii=False) + "\n").encode("utf-8")
        rt_dst = rt_dir / f"{task_id}.json"
        atomic_write_bytes(rt_dst, runtime_bytes)
        runtime[task_id] = {
            "path": str(rt_dst),
            "sha256": _sha256_bytes(runtime_bytes),
            "remote_path": f"benchmarks/research_engineering_v1/tasks/"
                           f"{task_id[:-3]}/task.yaml#runtime",
        }

    module_dir = Path(__file__).resolve().parent
    evaluator_sources = {
        name: _sha256_file(module_dir / name) for name in EVALUATOR_MODULES
        if (module_dir / name).is_file()
    }
    if set(evaluator_sources) != set(EVALUATOR_MODULES):
        # anchor.py 自己总在场；另外两个模块缺失说明包结构被破坏了
        missing = sorted(set(EVALUATOR_MODULES) - set(evaluator_sources))
        raise AnchorError(
            f"评测器模块不在场，无法登记源码摘要：{missing}；"
            "（首次写锚必须在 T1 四个模块全部落盘之后）")

    doc = {
        "experiment_id": constants.EXPERIMENT_ID,
        "criteria": criteria,
        "runtime": runtime,
        "scoring_sha256": _sha256_file(SCORING_SOURCE),
        "evaluator_sources": evaluator_sources,
        "run_manifest_schema_sha256": _sha256_file(RUN_MANIFEST_SCHEMA),
        "written_at": _now_z(),
        "written_by": str(written_by),
        "manifest_sha256": "",
    }
    doc["manifest_sha256"] = self_digest(doc)
    payload = (json.dumps(doc, sort_keys=True, indent=2,
                          ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(anchor_path(base), payload)
    return AnchorManifest.from_json(doc)


def load_anchor(path: "str | Path") -> AnchorManifest:
    """读取并**立刻自检自锚**；自锚不符即抛（不允许把坏锚当锚用）。"""
    target = Path(path)
    if not target.is_file():
        raise AnchorError(f"锚不在场：{target}")
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnchorError(f"锚不可解析：{target}（{exc}）") from exc
    if not isinstance(doc, Mapping):
        raise AnchorError(f"锚顶层必须是对象：{target}")
    manifest = AnchorManifest.from_json(doc)
    recomputed = self_digest(doc)
    if recomputed != manifest.manifest_sha256:
        raise AnchorError(
            f"manifest_sha256 不符：锚内登记 {manifest.manifest_sha256} ≠ "
            f"重算 {recomputed}（锚文件被改过）")
    return manifest


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def verify_anchor(path: "str | Path", *,
                  repo_root: "Path | None" = None,
                  scoring_source: "Path | None" = None) -> "list[str]":
    """重算全部摘要，返回问题列表（空列表 = 锚可评）。

    比对面：自锚、``scoring.py`` 源码、评测器三模块源码、三份判据快照、
    三份运行时快照、``run_manifest.v1`` 冻结 schema，以及**锚路径是否落在仓内**
    （计划 §5.9 的负例：判据指向仓内即视为锚不合格）。

    ``scoring_source`` 只为**测试**留缝（AC-P281-1-05 要拿一份被改过的
    ``scoring.py`` 副本喂进同一条校验路径）；缺省恒用仓内真件。
    """
    target = Path(path)
    repo = Path(repo_root) if repo_root is not None else REPO_ROOT
    scoring_file = Path(scoring_source) if scoring_source else SCORING_SOURCE
    problems: "list[str]" = []
    if not target.is_file():
        return [f"锚不在场：{target}"]
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"锚不可解析：{exc}"]
    if not isinstance(doc, Mapping):
        return [f"锚顶层必须是对象：{type(doc).__name__}"]
    try:
        manifest = AnchorManifest.from_json(doc)
    except AnchorError as exc:
        return [str(exc)]

    recomputed = self_digest(doc)
    if recomputed != manifest.manifest_sha256:
        problems.append(
            f"manifest_sha256 不符：锚内登记 {manifest.manifest_sha256} ≠ "
            f"重算 {recomputed}")

    if _inside(target, repo):
        problems.append(f"锚路径在仓内：{target}（仓外信任锚不得落进引擎仓）")

    if scoring_file.is_file():
        actual = _sha256_file(scoring_file)
        if actual != manifest.scoring_sha256:
            problems.append(
                f"scoring_sha256 不符：锚登记 {manifest.scoring_sha256} ≠ "
                f"实得 {actual}（判据数学源码被改：{scoring_file}）")
    else:
        problems.append(f"scoring.py 不在场：{scoring_file}")

    module_dir = Path(__file__).resolve().parent
    for name in sorted(manifest.evaluator_sources):
        module = module_dir / name
        if not module.is_file():
            problems.append(f"评测器模块不在场：{module}")
            continue
        actual = _sha256_file(module)
        if actual != manifest.evaluator_sources[name]:
            problems.append(
                f"evaluator_sources.{name} 不符：锚登记 "
                f"{manifest.evaluator_sources[name]} ≠ 实得 {actual}")

    if RUN_MANIFEST_SCHEMA.is_file():
        actual = _sha256_file(RUN_MANIFEST_SCHEMA)
        if actual != manifest.run_manifest_schema_sha256:
            problems.append(
                f"run_manifest.v1.schema.json 被改动：锚登记 "
                f"{manifest.run_manifest_schema_sha256} ≠ 实得 {actual}")
    else:
        problems.append(f"run_manifest.v1 冻结 schema 不在场：{RUN_MANIFEST_SCHEMA}")

    for label, table in (("criteria", manifest.criteria),
                         ("runtime", manifest.runtime)):
        if set(table) != set(constants.TASK_IDS):
            problems.append(
                f"锚的 {label} 键集不符：{sorted(table)} ≠ "
                f"{list(constants.TASK_IDS)}")
        for task_id in sorted(table):
            entry = table[task_id]
            snapshot = Path(str(entry.get("path") or ""))
            if not entry.get("path"):
                problems.append(f"锚的 {label}.{task_id} 缺 path")
                continue
            if _inside(snapshot, repo):
                problems.append(
                    f"锚的 {label}.{task_id}.path 在仓内：{snapshot}"
                    "（必须指向仓外快照）")
            if not snapshot.is_file():
                problems.append(f"锚的 {label}.{task_id} 快照不在场：{snapshot}")
                continue
            actual = _sha256_file(snapshot)
            if actual != str(entry.get("sha256") or ""):
                problems.append(
                    f"锚的 {label}.{task_id}.sha256 不符：锚登记 "
                    f"{entry.get('sha256')} ≠ 实得 {actual}")
    return problems
