"""Phase 2.8.0 Task 4：独立评测器（快照式输入 / 硬门优先 / 七态判决）。

不变量（T4.1）：

1. **不 import、不 exec 候选代码**：只读快照与结构化产物；候选的任何 Python
   文件只做 AST 静态扫描（``adapter.py`` 的 exec/compile 与 tour 演示扫描）；
2. **候选不能改评测器/判据**：启动先做自校验（``evaluator_files`` sha256）与
   判据锁复算，不符 ⇒ ``BLOCKED_INFRA``；
3. **Hard Gate 优先于分数**：命中任一硬门即按优先级定态，高分不得掩盖；
4. **七态保留**（D4-6），永不压成布尔；
5. **权重/阈值只来自 ``criteria.json``**（D4-4）；
6. **可独立重算**：``scoring.py`` 从 telemetry 逐帧重算并把中间量落到
   ``recompute_trace.jsonl``。

硬门两层（T4.5 步骤 3）：通用七门 ``HG-EVAL-1..7`` + 任务判据声明的域门
（``HG-M1-*`` / ``HG-L1-*`` / ``HG-R1-*``）。两层都会**逐门**记录到
``gates_checked``，被触发的进 ``hard_gates``；域门与通用门对同一事实各判一次
是刻意的纵深防御（两份独立实现互证）。
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from orchestrator.benchmark import runtime_fingerprint, scoring
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.result_schema import (
    EVALUATION_NAME,
    Dimension,
    EvaluationResult,
    GateHit,
    aggregate_status,
    candidate_digest,
    write_result,
)
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

EVALUATOR_ID = "benchmark_evaluator_v1"
EVALUATOR_VERSION = 1

#: D4-7 自校验对象（与 ``criteria.evaluator_files`` 逐条比对）
EVALUATOR_SOURCES = (
    "orchestrator/benchmark/evaluator.py",
    "orchestrator/benchmark/scoring.py",
    "orchestrator/benchmark/contracts.py",
)

SNAPSHOT_DIR = "snapshot"
SNAPSHOT_MANIFEST = "snapshot_manifest.json"
SNAPSHOT_SCHEMA = "simpraxis.benchmark_snapshot.v1"
TRACE_NAME = "recompute_trace.jsonl"

#: 运行级附加证据（不属于候选 outputs；``required_extras()`` 决定哪些是必需）
EXTRA_EVIDENCE = (
    "runtime_fingerprint.json",
    "reproducibility.json",
    "hidden_access_audit.json",
    "birth/functional_birth.json",
    # Task 5：证据清单本身也是证据（run_manifest.evidence_manifest_sha256
    # 必须与它的实测 sha256 绑定，见 HG-EVAL-6）
    "evidence/manifest.json",
)

#: 候选目录里出现这些名字即视为「伪造判决件」（Task 5 / 路线图 §9.4）：
#: 判决只能由评测器写进 out_dir，候选自带的 PASS/判决文件一律不采信。
FORGED_VERDICT_NAMES = frozenset({
    "evaluation_result.json",
    "evaluation_result.sha256",
    "judge_report.json",
    "closure_judge.json",
    "acceptance.json",
    "snapshot_manifest.json",
    "PASS",
    "PASS.txt",
    "pass.json",
})

#: 候选无权访问的私有前缀（命中任一即 HG-EVAL-2）
PRIVATE_PREFIX = "private/"

#: 硬禁止把 tour 自带 demo 跑起来（硬规则 10 / HG-L1-7）
FORBIDDEN_TOUR_IMPORTS = ("libero.benchmark",)
FORBIDDEN_TOUR_STRINGS = ("run_demo", "libero.benchmark")

#: 通用七门的命中态（T4.5 步骤 3 的语义）
GATE_STATUS = {
    "HG-EVAL-1": "INVALID_RUNTIME",
    "HG-EVAL-2": "FAIL",
    "HG-EVAL-3": "FAIL",
    "HG-EVAL-4": "FAIL",
    "HG-EVAL-5": "INCOMPLETE_EVIDENCE",
    "HG-EVAL-6": "INVALID_ARTIFACT",
    "HG-EVAL-7": "BLOCKED_INFRA",
    # Task 5（T5.9 失败处理的修复，Complete A28 系列留痕）：验收门。
    # 路线图 §8.6 的 PASS 语义是「所有必要验收项通过」——没有硬门命中并不等于
    # 通过：任一度量维度 < 1（含部分通过）或 birth 契约不满足，一律 FAIL。
    # 该门是评测器定义的通用门，不写在 criteria.hard_gates 里（判据锁不变）。
    "HG-EVAL-8": "FAIL",
    # 域门（criteria/*.criteria.json 冻结声明的 HG-<task>-<n>）
    "HG-M1-1": "INVALID_RUNTIME", "HG-M1-2": "FAIL", "HG-M1-3": "FAIL",
    "HG-M1-4": "FAIL", "HG-M1-5": "FAIL", "HG-M1-6": "FAIL",
    "HG-M1-7": "FAIL",
    "HG-L1-1": "INVALID_RUNTIME", "HG-L1-2": "FAIL", "HG-L1-3": "FAIL",
    "HG-L1-4": "FAIL", "HG-L1-5": "FAIL", "HG-L1-6": "FAIL",
    "HG-L1-7": "FAIL",
    "HG-R1-1": "FAIL", "HG-R1-2": "FAIL", "HG-R1-3": "FAIL",
    "HG-R1-4": "FAIL", "HG-R1-5": "FAIL", "HG-R1-6": "FAIL",
    "HG-R1-7": "FAIL",
}


class EvaluatorError(RuntimeError):
    """评测调用参数/环境不合法（不是候选问题）。候选问题一律落状态。"""


# --------------------------------------------------------------------------
# D4-7 自校验
# --------------------------------------------------------------------------

def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def evaluator_digests(source_root: "str | Path | None" = None) -> "dict[str, str]":
    root = Path(source_root) if source_root is not None else repo_root()
    return {rel: ts.sha256_file(root / rel) for rel in EVALUATOR_SOURCES}


def format_evaluator_files(source_root: "str | Path | None" = None) -> "list[str]":
    """``criteria.evaluator_files`` 的登记格式：``<relpath>:<sha256>``。"""
    return [f"{rel}:{digest}"
            for rel, digest in evaluator_digests(source_root).items()]


def parse_evaluator_files(entries: Iterable[str]) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    for entry in entries:
        if not isinstance(entry, str) or ":" not in entry:
            raise EvaluatorError(f"evaluator_files 条目非法：{entry!r}")
        rel, _, digest = entry.rpartition(":")
        out[rel.strip()] = digest.strip()
    return out


def self_check(spec: ts.TaskSpec, *,
               source_root: "str | Path | None" = None) -> "list[str]":
    """返回不合规清单（空列表才允许继续评分）。"""
    problems: "list[str]" = []
    expected = parse_evaluator_files(spec.criteria.get("evaluator_files") or [])
    if not expected:
        problems.append("criteria.evaluator_files 为空（评测器未 LOCK，D4-7）")
    actual = evaluator_digests(source_root)
    for rel in EVALUATOR_SOURCES:
        want = expected.get(rel)
        if want is None:
            problems.append(f"{rel} 未在 criteria.evaluator_files 登记")
        elif actual[rel] != want:
            problems.append(f"{rel} sha256 不符：登记 {want}，实测 {actual[rel]}")
    unknown = sorted(set(expected) - set(EVALUATOR_SOURCES))
    if unknown:
        problems.append(f"criteria.evaluator_files 登记了未知文件：{unknown}")
    declared = [gate["id"] for gate in spec.hard_gates]
    unmapped = sorted(set(declared) - set(GATE_STATUS))
    if unmapped:
        problems.append(f"criteria 声明了评测器未实现的硬门：{unmapped}")
    return problems


def criteria_integrity(spec: ts.TaskSpec) -> "list[str]":
    """重读盘上 criteria 文件并复算判据锁（防「改阈值不重 LOCK」）。"""
    path = ts.BENCHMARK_ROOT / spec.criteria_file
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"criteria 文件不可读：{path}: {exc}"]
    try:
        recomputed = ts.criteria_lock_sha256(doc)
    except ts.TaskSpecError as exc:
        return [f"criteria 文件不合规：{exc}"]
    if recomputed != spec.criteria_lock_sha256:
        return [f"criteria_lock_sha256 不符：task.yaml 声明 "
                f"{spec.criteria_lock_sha256}，重算 {recomputed}"]
    return []


def find_forged_verdicts(run_dir: "str | Path", *, max_depth: int = 4,
                         limit: int = 32) -> "list[str]":
    """在候选目录里找伪造判决件（只看名字，不跟随符号链接，深度有界）。"""
    root = Path(run_dir)
    hits: "list[str]" = []
    if not root.is_dir():
        return hits
    stack: "list[tuple[Path, int]]" = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                rel = str(Path(entry.path).relative_to(root))
            except ValueError:                      # pragma: no cover - 防御
                continue
            if entry.is_symlink():
                if entry.name in FORGED_VERDICT_NAMES:
                    hits.append(rel)
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth + 1 <= max_depth:
                    stack.append((Path(entry.path), depth + 1))
                continue
            if entry.name in FORGED_VERDICT_NAMES:
                hits.append(rel)
        if len(hits) >= limit:
            break
    return sorted(hits)[:limit]


# --------------------------------------------------------------------------
# D4-1 快照
# --------------------------------------------------------------------------

@dataclass
class Snapshot:
    run_dir: Path
    directory: Path
    files: "dict[str, Path]"
    digests: "dict[str, str]"
    aggregate_sha256: str
    declared_sha256: "str | None"
    digest_ok: bool
    missing: "list[str]" = field(default_factory=list)
    escaped: "list[str]" = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    manifest_error: "str | None" = None
    manifest_sha256: "str | None" = None
    manifest_path: "Path | None" = None
    # 参与 candidate_artifact_sha256 聚合的相对路径（= 契约 outputs 去掉
    # run_manifest.json 的自我引用）。附加证据件只复制留档，不进聚合。
    aggregate_files: "list[str]" = field(default_factory=list)
    #: 候选目录里出现的伪造判决件（Task 5，HG-EVAL-6）
    forged: "list[str]" = field(default_factory=list)

    @property
    def digest_set(self) -> "list[str]":
        return sorted(self.digests)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def snapshot(run_dir: "str | Path", out_dir: "str | Path",
             outputs: Iterable[str], *,
             extras: Iterable[str] = EXTRA_EVIDENCE,
             required_extras: Iterable[str] = ()) -> Snapshot:
    """把候选产物复制进 ``snapshot/`` 并逐件 sha256（此后只读该目录）。

    ``outputs`` 与 ``required_extras`` 缺件记 ``missing``（→ INCOMPLETE_EVIDENCE）；
    其余 ``extras`` 只「在场即复制」，缺失不罚。复制失败（权限/空间）由调用方转
    ``BLOCKED_INFRA``（T4.9），不得跳过快照直接读候选目录。
    """
    source = Path(run_dir)
    if not source.is_dir():
        raise EvaluatorError(f"run_dir 不存在或不是目录：{source}")
    directory = ensure_private_dir(Path(out_dir) / SNAPSHOT_DIR)
    required = list(outputs) + list(required_extras)
    wanted: "list[str]" = []
    for rel in required + list(extras):
        if rel not in wanted:
            wanted.append(rel)

    snap = Snapshot(run_dir=source, directory=directory, files={}, digests={},
                    aggregate_sha256="", declared_sha256=None, digest_ok=False)
    snap.forged = find_forged_verdicts(source)
    for rel in wanted:
        src = source / rel
        must_exist = rel in required
        if src.is_symlink():
            snap.escaped.append(f"{rel}: 符号链接（候选不得用链接绕过快照）")
            continue
        if not src.exists():
            if must_exist:
                snap.missing.append(rel)
            continue
        if not src.is_file():
            if must_exist:
                snap.escaped.append(f"{rel}: 不是普通文件")
            continue
        if not _inside(source, src):
            snap.escaped.append(f"{rel}: 解析后落在 run_dir 之外")
            continue
        data = src.read_bytes()
        _write_bytes(directory / rel, data)
        snap.files[rel] = directory / rel
        snap.digests[rel] = ts.sha256_bytes(data)

    manifest_rel = "run_manifest.json"
    manifest_path = snap.files.get(manifest_rel)
    if manifest_path is not None:
        snap.manifest_path = manifest_path
        try:
            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("run_manifest.json 顶层必须是对象")
            snap.manifest = doc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            snap.manifest_error = str(exc)
        snap.manifest_sha256 = snap.digests.get(manifest_rel)

    digest_set = {rel: snap.digests[rel]
                  for rel in outputs
                  if rel != manifest_rel and rel in snap.digests}
    snap.aggregate_files = sorted(digest_set)
    snap.aggregate_sha256 = candidate_digest(digest_set)
    declared = snap.manifest.get("candidate_artifact_sha256")
    snap.declared_sha256 = declared if isinstance(declared, str) else None
    snap.digest_ok = (snap.declared_sha256 is not None
                      and snap.declared_sha256 == snap.aggregate_sha256)
    return snap


def write_snapshot_manifest(snap: Snapshot, *, run_id: str,
                            task_id: str) -> "tuple[Path, str]":
    doc = {
        "schema": SNAPSHOT_SCHEMA,
        "run_id": run_id,
        "task_id": task_id,
        "run_dir": str(snap.run_dir),
        "files": {rel: {"sha256": digest,
                        "bytes": (snap.files[rel].stat().st_size
                                  if rel in snap.files else 0)}
                  for rel, digest in sorted(snap.digests.items())},
        "digest_set": sorted(snap.aggregate_files),
        "aggregate_sha256": snap.aggregate_sha256,
        "declared_sha256": snap.declared_sha256,
        "digest_ok": snap.digest_ok,
        "missing": sorted(snap.missing),
        "escaped": sorted(snap.escaped),
        "forged_verdicts": sorted(snap.forged),
        "manifest_sha256": snap.manifest_sha256,
        "manifest_error": snap.manifest_error,
    }
    path = snap.directory / SNAPSHOT_MANIFEST
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", 0o600)
    return path, ts.sha256_file(path)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def parse_artifact(rel: str, raw: bytes) -> Any:
    text = raw.decode("utf-8")
    suffix = Path(rel).suffix
    if suffix == ".json":
        return json.loads(text)
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return text


def scan_python_source(text: str) -> dict:
    """静态扫描候选 Python 源（不执行）：exec/compile 与 tour 演示痕迹。"""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {"exec_calls": [], "tour_hits": [],
                "error": f"SyntaxError: {exc}"}
    exec_calls: "list[str]" = []
    tour_hits: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name in ("exec", "compile"):
                exec_calls.append(f"{name}()@L{node.lineno}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_TOUR_IMPORTS):
                    tour_hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(FORBIDDEN_TOUR_IMPORTS):
                tour_hits.append(f"from {node.module}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for marker in FORBIDDEN_TOUR_STRINGS:
                if marker in node.value:
                    tour_hits.append(f"字面量含 {marker!r}")
    return {"exec_calls": exec_calls, "tour_hits": tour_hits, "error": None}


# --------------------------------------------------------------------------
# 硬门记录
# --------------------------------------------------------------------------

class GateLog:
    """逐门记录 ``checked`` / 触发态；同一门只检查一次（schema uniqueItems）。"""

    def __init__(self) -> None:
        self.hits: "list[GateHit]" = []
        self.checked: "list[str]" = []
        self.not_applicable: "list[str]" = []

    def check(self, gate_id: str, *, fired: bool, detail: str,
              applicable: bool = True) -> bool:
        if gate_id in self.checked:
            raise EvaluatorError(f"硬门 {gate_id} 被重复检查（门表重复）")
        self.checked.append(gate_id)
        if not applicable:
            self.not_applicable.append(gate_id)
            return False
        if fired:
            self.hits.append(GateHit(gate_id, detail or f"{gate_id} 命中"))
            return True
        return False


def _joined(items: Iterable[str], limit: int = 6) -> str:
    values = list(items)
    head = "; ".join(values[:limit])
    return head + (f" …共 {len(values)} 项" if len(values) > limit else "")


# --------------------------------------------------------------------------
# 评测器
# --------------------------------------------------------------------------

@dataclass
class Evaluator:
    spec: ts.TaskSpec
    source_root: "Path | None" = None

    # -- 公共入口 ---------------------------------------------------------
    def evaluate(self, run_dir: "str | Path", out_dir: "str | Path", *,
                 run_id: "str | None" = None) -> dict:
        run_path = Path(run_dir)
        out_path = Path(out_dir)
        rid = run_id or run_path.name
        problems = self_check(self.spec, source_root=self.source_root)
        problems += criteria_integrity(self.spec)
        if problems:
            return self._blocked(out_path, rid, _joined(problems))
        try:
            snap = snapshot(run_path, out_path, self.outputs,
                            required_extras=self._required_extras())
        except OSError as exc:
            return self._blocked(out_path, rid, f"快照复制失败：{exc}")
        manifest_run_id = snap.manifest.get("run_id")
        if isinstance(manifest_run_id, str) and manifest_run_id:
            rid = manifest_run_id
        snap_path, snap_sha = write_snapshot_manifest(
            snap, run_id=rid, task_id=self.spec.task_id)
        return self._evaluate_snapshot(snap, out_path, rid,
                                       snapshot_manifest=(snap_path, snap_sha))

    @property
    def outputs(self) -> "list[str]":
        return list(self.spec.outputs)

    def _required_extras(self) -> "list[str]":
        """运行身份对所有任务必需；复现/隐藏审计按判据与复现声明决定。"""
        required = ["runtime_fingerprint.json"]
        thresholds = self.spec.thresholds or {}
        if "repro_abs_tol" in thresholds:
            required.append("reproducibility.json")
        if self.spec.reproducibility.get("hidden_seed_ref") is not None:
            required.append("hidden_access_audit.json")
        return required

    # -- 内部 -------------------------------------------------------------
    def _zero_dimensions(self, reason: str) -> "dict[str, Dimension]":
        weights = self.spec.weights
        return {name: Dimension(weight=float(weight), score=0.0,
                                detail=f"评分未执行：{reason}")
                for name, weight in weights.items()}

    def _emit(self, out_dir: Path, run_id: str, status: str,
              dimensions: "Mapping[str, Dimension]",
              log: "GateLog | None" = None,
              trace_path: "Path | None" = None) -> dict:
        log = log or GateLog()
        result = EvaluationResult(
            task_id=self.spec.task_id,
            run_id=run_id,
            evaluator_id=EVALUATOR_ID,
            evaluator_version=EVALUATOR_VERSION,
            criteria_lock_sha256=self.spec.criteria_lock_sha256,
            status=status,
            dimensions=dict(dimensions),
            hard_gates=list(log.hits),
            gates_checked=list(log.checked),
        )
        written = write_result(out_dir, result)
        written.update({
            "task_id": self.spec.task_id,
            "run_id": run_id,
            "weighted_score": result.weighted_score,
            "dimensions": {name: dim.to_json()
                           for name, dim in result.dimensions.items()},
            "hard_gate_ids": [hit.id for hit in log.hits],
            "gates_checked": list(log.checked),
            "not_applicable_gates": list(log.not_applicable),
            "trace_path": str(trace_path) if trace_path else None,
        })
        return written

    def _blocked(self, out_dir: Path, run_id: str, detail: str) -> dict:
        log = GateLog()
        log.check("HG-EVAL-7", fired=True, detail=f"评测器不可信：{detail}")
        return self._emit(out_dir, run_id, "BLOCKED_INFRA",
                          self._zero_dimensions(detail), log)

    def _evaluate_snapshot(self, snap: Snapshot, out_dir: Path, run_id: str,
                           *, snapshot_manifest: "tuple[Path, str]") -> dict:
        log = GateLog()
        files: "dict[str, Any]" = {}
        parse_errors: "dict[str, str]" = {}
        for rel, path in sorted(snap.files.items()):
            try:
                files[rel] = parse_artifact(rel, path.read_bytes())
            except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError,
                    ValueError) as exc:
                parse_errors[rel] = f"{type(exc).__name__}: {exc}"

        facts: "dict | None" = None
        dims: "dict[str, Dimension]"
        scoring_error: "str | None" = None
        try:
            facts = scoring.facts_task(self.spec.task_id, files)
            dims = scoring.score_task(self.spec.task_id, files, self.spec.criteria)
        except scoring.ScoringError as exc:
            scoring_error = str(exc)
            dims = self._zero_dimensions(scoring_error)

        trace_path = out_dir / TRACE_NAME
        self._write_trace(trace_path, facts)

        self._generic_gates(log, snap, files, facts, parse_errors, scoring_error,
                            dims)
        self._domain_gates(log, snap, files, facts, parse_errors, scoring_error)

        status = aggregate_status(log.hits, GATE_STATUS)
        written = self._emit(out_dir, run_id, status, dims, log, trace_path)
        written.update({
            "snapshot_manifest": str(snapshot_manifest[0]),
            "snapshot_manifest_sha256": snapshot_manifest[1],
            "snapshot_aggregate_sha256": snap.aggregate_sha256,
            "candidate_artifact_sha256": snap.declared_sha256,
            "evaluation_path": written.get("result_path"),
            "evaluation_sha256": written.get("result_sha256"),
        })
        return written

    # -- 通用七门 ---------------------------------------------------------
    def _runtime_mismatches(self, files: Mapping[str, Any]) -> "list[str] | None":
        fingerprint = files.get("runtime_fingerprint.json")
        if not isinstance(fingerprint, Mapping):
            return None
        probe = files.get("env_probe.json")
        observed = None
        if isinstance(probe, Mapping):
            observed = {"action_dim": probe.get("action_dim"),
                        "obs_keys": probe.get("observation_keys")}
        mismatches = runtime_fingerprint.compare(
            dict(self.spec.runtime), fingerprint, spec=self.spec,
            observed=observed)
        return [f"{item.code}: {item.detail}" for item in mismatches]

    def _hidden_access(self, files: Mapping[str, Any]) -> "list[str] | None":
        audit = files.get("hidden_access_audit.json")
        if not isinstance(audit, Mapping):
            return None
        touched: "list[str]" = []
        for key in ("read", "written", "modified"):
            values = audit.get(key) or ()
            if isinstance(values, (list, tuple)):
                touched += [str(v) for v in values
                            if str(v).startswith(PRIVATE_PREFIX)]
        return touched

    def _generic_gates(self, log: GateLog, snap: Snapshot,
                       files: Mapping[str, Any], facts: "dict | None",
                       parse_errors: Mapping[str, str],
                       scoring_error: "str | None",
                       dims: Mapping[str, Any] | None = None) -> None:
        spec = self.spec
        # HG-EVAL-1 运行时身份
        mismatches = self._runtime_mismatches(files)
        if mismatches is None:
            log.check("HG-EVAL-1", fired=False, detail="无指纹可对", applicable=False)
        else:
            log.check("HG-EVAL-1", fired=bool(mismatches),
                      detail=f"运行时不符：{_joined(mismatches)}")

        # HG-EVAL-2 隐藏输入
        hidden_ref = spec.reproducibility.get("hidden_seed_ref")
        touched = self._hidden_access(files)
        if hidden_ref is None:
            log.check("HG-EVAL-2", fired=False, applicable=False,
                      detail="本任务无隐藏输入")
        elif touched is None:
            log.check("HG-EVAL-2", fired=False, applicable=False,
                      detail="审计件缺失（由 HG-EVAL-5 计证据缺失）")
        else:
            log.check("HG-EVAL-2", fired=bool(touched),
                      detail=f"访问/改动私有输入：{_joined(touched)}")

        # HG-EVAL-3 关键物理约束
        if facts is None or spec.task_id == "icy_ground_dr_v1":
            log.check("HG-EVAL-3", fired=False, applicable=False,
                      detail="事实不可得或无物理约束")
        elif spec.task_id == "mujoco_contact_push_v1":
            limit = scoring.threshold(spec.criteria, "max_penetration_m")
            fired = facts["max_penetration_m"] > limit
            log.check("HG-EVAL-3", fired=fired,
                      detail=f"最大穿模 {facts['max_penetration_m']:.9f} m vs 上限 {limit} m")
        else:  # L1
            fired = not facts["state_finite"]
            log.check("HG-EVAL-3", fired=fired,
                      detail="one_step 状态非 finite" if fired
                      else "one_step 状态 finite")

        # HG-EVAL-4 可复现
        repro = files.get("reproducibility.json")
        if not isinstance(repro, Mapping):
            log.check("HG-EVAL-4", fired=False, applicable=False,
                      detail="无 reproducibility.json（本任务不声明复现产物）")
        else:
            same_seed = repro.get("same_seed_identical") is True
            delta = repro.get("cross_seed_max_delta_m")
            tol = self._threshold_or_none("repro_abs_tol")
            delta_ok = True
            if delta is not None and tol is not None:
                try:
                    delta_ok = float(delta) <= tol
                except (TypeError, ValueError):
                    delta_ok = False
            log.check("HG-EVAL-4", fired=not (same_seed and delta_ok),
                      detail=f"同种子逐位相等={same_seed}；跨种子差 {delta!r}")

        # HG-EVAL-5 证据缺失
        missing = list(snap.missing)
        log.check("HG-EVAL-5", fired=bool(missing),
                  detail=f"缺件：{_joined(missing)}")

        # HG-EVAL-6 产物完整性与权限
        problems: "list[str]" = []
        if snap.manifest_path is not None and not snap.digest_ok:
            problems.append(f"快照聚合 sha256 {snap.aggregate_sha256} ≠ run_manifest "
                            f"声明 {snap.declared_sha256!r}")
        problems += snap.escaped
        problems += [f"候选目录含伪造判决件 {rel}（判决只由评测器写）"
                     for rel in snap.forged]
        problems += [f"{rel} 不可解析（{msg}）" for rel, msg in parse_errors.items()]
        if snap.manifest_error:
            problems.append(f"run_manifest.json 不合规：{snap.manifest_error}")
        problems += self._manifest_bindings(snap)
        paid = snap.manifest.get("paid_model_calls")
        if paid is not None and paid != 0:
            problems.append(f"paid_model_calls={paid!r}（本阶段必须为 0）")
        mode = snap.manifest.get("mode")
        if mode is not None and mode not in ("reference", "candidate",
                                             "negative_control"):
            problems.append(f"未知 run mode：{mode!r}")
        if scoring_error:
            problems.append(f"评分无法重算：{scoring_error}")
        log.check("HG-EVAL-6", fired=bool(problems),
                  detail=f"产物完整性/权限：{_joined(problems)}")

        # HG-EVAL-7 由自校验前置处理；此处记「已检查、未触发」
        log.check("HG-EVAL-7", fired=False,
                  detail="评测器/判据自校验通过")

        # HG-EVAL-8 验收门（Task 5）：PASS 语义 = 所有必要验收项通过
        unmet = self._acceptance_problems(files, dims or {})
        log.check("HG-EVAL-8", fired=bool(unmet),
                  detail=f"必要验收项未通过：{_joined(unmet)}")

    def _manifest_bindings(self, snap: Snapshot) -> "list[str]":
        """run_manifest 与本次评测身份/快照实测值的绑定（Task 5 / HG-EVAL-6）。

        刻意**不绑定** ``criteria_sha256``：判据文件整文件哈希会随
        ``evaluator_files`` 重登记而变，绑定它会让每次重 LOCK 都把历史运行
        判成无效；锁定语义由 ``criteria_lock_sha256``（白名单子集）承担。
        """
        manifest = snap.manifest
        if not manifest:
            return []
        spec = self.spec
        problems: "list[str]" = []
        for key, expected in (("task_id", spec.task_id),
                              ("task_version", spec.version),
                              ("evaluator_version", EVALUATOR_VERSION),
                              ("criteria_lock_sha256",
                               spec.criteria_lock_sha256),
                              ("task_spec_sha256", spec.task_file_sha256)):
            got = manifest.get(key)
            if got != expected:
                problems.append(f"run_manifest.{key}={got!r} ≠ 期望 {expected!r}")
        for rel, key in (("runtime_fingerprint.json",
                          "runtime_fingerprint_sha256"),
                         ("birth/functional_birth.json",
                          "birth_result_sha256"),
                         ("evidence/manifest.json",
                          "evidence_manifest_sha256")):
            actual = snap.digests.get(rel)
            declared = manifest.get(key)
            if actual is not None and declared != actual:
                problems.append(f"run_manifest.{key}={declared!r} ≠ 实测 "
                                f"sha256({rel})={actual}")
        if "evidence/manifest.json" not in snap.digests and \
                manifest.get("evidence_manifest_sha256") not in (None, ""):
            problems.append("run_manifest 声明 evidence_manifest_sha256，"
                            "但缺 evidence/manifest.json")
        return problems

    def _acceptance_problems(self, files: Mapping[str, Any],
                             dims: Mapping[str, Any]) -> "list[str]":
        """路线图 §8.6：PASS 必须是「所有必要验收项通过」。

        1. 任一度量维度 score < 1（含部分通过）即不满足；
        2. birth 产物在场时：``birth_status`` 必须是 ``BORN``，且
           ``birth_contract`` 要求的每个等级都必须 ``PASS``。
           **容错读取**（只看 ``birth_status``/``levels`` 两个字段，
           不对 schema id 做硬校验）——Task 4 的合成样本用的是占位 schema id，
           这里只判语义，不为 schema 兼容性放水也不拿它当额外判据。
        """
        problems: "list[str]" = []
        for name in sorted(dims):
            dim = dims[name]
            try:
                score = float(dim.score)
            except (TypeError, ValueError):     # pragma: no cover - 防御
                score = 0.0
            if score < 1.0:
                problems.append(f"维度 {name}={score:.6f} < 1.0")
        birth = files.get("birth/functional_birth.json")
        if not isinstance(birth, Mapping):
            return problems
        status = birth.get("birth_status")
        if status != "BORN":
            problems.append(f"birth_status={status!r} ≠ BORN")
        levels = birth.get("levels")
        if not isinstance(levels, Mapping):
            problems.append("birth.levels 缺失（无法证明逐级通过）")
            return problems
        contract = dict(self.spec.birth_contract)
        required = {
            "B0": True,
            "B1": bool(contract.get("import_required")),
            "B2": bool(contract.get("construction_required")),
            "B3": bool(contract.get("contract_required")),
            "B4": bool(contract.get("minimal_step_required")),
        }
        for level in ("B0", "B1", "B2", "B3", "B4"):
            if not required[level]:
                continue
            entry = levels.get(level)
            got = entry.get("status") if isinstance(entry, Mapping) else None
            if got != "PASS":
                problems.append(f"{level}={got!r}（birth_contract 要求 PASS）")
        return problems

    def _threshold_or_none(self, name: str) -> "float | None":
        entry = (self.spec.thresholds or {}).get(name)
        if isinstance(entry, Mapping) and "value" in entry:
            return float(entry["value"])
        return None

    # -- 域门 -------------------------------------------------------------
    def _domain_gates(self, log: GateLog, snap: Snapshot,
                      files: Mapping[str, Any], facts: "dict | None",
                      parse_errors: Mapping[str, str],
                      scoring_error: "str | None") -> None:
        task = self.spec.task_id
        if task == "mujoco_contact_push_v1":
            self._gates_mujoco(log, files, facts)
        elif task == "libero_env_adapter_v1":
            self._gates_libero(log, files, facts, parse_errors)
        elif task == "icy_ground_dr_v1":
            self._gates_research(log, facts)
        else:  # pragma: no cover - 注册表已限定三任务
            raise EvaluatorError(f"未知任务：{task!r}")

    def _gates_mujoco(self, log: GateLog, files: Mapping[str, Any],
                      facts: "dict | None") -> None:
        spec = self.spec
        mismatches = self._runtime_mismatches(files) or []
        log.check("HG-M1-1", fired=bool(mismatches),
                  detail=f"BLOCKED_RUNTIME_MISMATCH：{_joined(mismatches)}")
        touched = self._hidden_access(files) or []
        log.check("HG-M1-6", fired=bool(touched),
                  detail=f"私有输入被动过：{_joined(touched)}")
        if facts is None:
            for gate in ("HG-M1-2", "HG-M1-3", "HG-M1-4", "HG-M1-5", "HG-M1-7"):
                log.check(gate, fired=False, applicable=False,
                          detail="评分事实不可得（已由 HG-EVAL-6 记）")
            return
        log.check("HG-M1-2", fired=not facts["all_finite"],
                  detail="metrics/telemetry 出现 NaN/Inf"
                  if not facts["all_finite"] else "全部数值 finite")
        limit = scoring.threshold(spec.criteria, "max_penetration_m")
        log.check("HG-M1-3", fired=facts["max_penetration_m"] > limit,
                  detail=f"最大穿模 {facts['max_penetration_m']:.9f} m vs 上限 {limit} m")
        moved = facts["cube_moved"] and facts["contact_seen"]
        log.check("HG-M1-4", fired=not moved,
                  detail=f"cube_moved={facts['cube_moved']} "
                         f"contact_peak={facts['contact_force_peak_n']}")
        repro = files.get("reproducibility.json")
        if isinstance(repro, Mapping):
            same_seed = repro.get("same_seed_identical") is True
            log.check("HG-M1-5", fired=not same_seed,
                      detail=f"同种子逐位相等={same_seed}")
        else:
            log.check("HG-M1-5", fired=False, applicable=False,
                      detail="无复现产物（已由 HG-EVAL-5 记）")
        consistent = facts["metrics_consistent"]
        if consistent is None:
            log.check("HG-M1-7", fired=False, applicable=False,
                      detail="metrics.json 未对账（缺键或缺失）")
        else:
            log.check("HG-M1-7", fired=not consistent,
                      detail=f"自报 vs 重算：{_joined(facts['metrics_mismatches'])}")

    def _gates_libero(self, log: GateLog, files: Mapping[str, Any],
                      facts: "dict | None",
                      parse_errors: Mapping[str, str]) -> None:
        mismatches = self._runtime_mismatches(files) or []
        # HG-L1-1 判据原文还覆盖「宿主冒充容器（captured_in != container 而
        # 声称容器版本）」；Task 5 负例 l1_07 暴露此前的实现只对 runtime 段，
        # 这里按判据原文补齐（只认 backend↔captured_in 这一条，不扩大口径）。
        impersonation: "list[str]" = []
        fingerprint = files.get("runtime_fingerprint.json")
        probe = files.get("env_probe.json")
        if isinstance(fingerprint, Mapping) and isinstance(probe, Mapping):
            backend = str((fingerprint.get("container") or {}).get("backend") or "")
            expected_in = {"docker": "container", "host": "host"}.get(backend)
            got_in = str(probe.get("captured_in") or "")
            if expected_in and got_in != expected_in:
                impersonation.append(
                    f"captured_in={got_in!r} 与指纹 backend={backend!r} "
                    f"（应为 {expected_in!r}）不符")
        log.check("HG-L1-1", fired=bool(mismatches) or bool(impersonation),
                  detail=f"运行时不符：{_joined(mismatches + impersonation) or '无'}")
        if facts is None:
            for gate in ("HG-L1-2", "HG-L1-3", "HG-L1-4", "HG-L1-5", "HG-L1-6"):
                log.check(gate, fired=False, applicable=False,
                          detail="评分事实不可得（已由 HG-EVAL-6 记）")
        else:
            log.check("HG-L1-2", fired=facts["probe_consistent"] is False,
                      detail=f"探针 python {facts['probe_python']} vs 指纹 "
                             f"{facts['fingerprint_python']}；captured_in="
                             f"{facts['captured_in']!r}")
            log.check("HG-L1-4", fired=facts["levels"].get("B2") != "PASS",
                      detail=f"B2={facts['levels'].get('B2')}（构造必须真实）")
            log.check("HG-L1-5", fired=not facts["contract_match"],
                      detail=f"动作/观测契约：{_joined(facts['contract_notes'])}")
            log.check("HG-L1-6", fired=not (facts["state_finite"] and facts["advanced"]),
                      detail=f"advanced={facts['advanced']} "
                             f"finite={facts['state_finite']}")
        source = files.get("adapter.py")
        scan = ({"exec_calls": [], "tour_hits": []}
                if not isinstance(source, str) else scan_python_source(source))
        log.check("HG-L1-3", fired=bool(scan["exec_calls"]),
                  detail=f"以 compile/exec 代替真实 import："
                         f"{_joined(scan['exec_calls']) or '无'}")
        log.check("HG-L1-7", fired=bool(scan["tour_hits"]),
                  detail=f"tour 演示痕迹：{_joined(scan['tour_hits']) or '无'}")

    def _gates_research(self, log: GateLog,
                        facts: "dict | None") -> None:
        if facts is None:
            for gate in ("HG-R1-1", "HG-R1-2", "HG-R1-3", "HG-R1-4",
                         "HG-R1-5", "HG-R1-6", "HG-R1-7"):
                log.check(gate, fired=False, applicable=False,
                          detail="评分事实不可得（已由 HG-EVAL-5/6 记）")
            return
        log.check("HG-R1-1", fired=bool(facts["missing_sections"]),
                  detail=f"缺 13 项必填段：{_joined(facts['missing_sections'])}")
        log.check("HG-R1-2", fired=not facts["units_ok"],
                  detail="参数缺物理单位" if not facts["units_ok"]
                  else "参数单位齐全")
        log.check("HG-R1-3", fired=not facts["leak_ok"],
                  detail=f"互斥声明={facts['disjoint_declared']}，"
                         f"重叠 id={_joined(facts['overlap_ids']) or '无'}")
        log.check("HG-R1-4", fired=not facts["holdout_ok"],
                  detail="无独立 Holdout" if not facts["holdout_ok"]
                  else "Holdout 独立存在")
        log.check("HG-R1-5", fired=not facts["mapped_ok"],
                  detail="指标未映射到 13 项必填段")
        log.check("HG-R1-6", fired=facts["contradiction"],
                  detail="判据未达却宣称成功")
        log.check("HG-R1-7", fired=bool(facts["forbidden_claims"]),
                  detail=f"翻案/成功措辞：{_joined(facts['forbidden_claims'])}")

    # -- 重算轨迹 ---------------------------------------------------------
    def _write_trace(self, path: Path, facts: "dict | None") -> None:
        lines: "list[str]" = []
        if facts is not None and isinstance(facts.get("trace"), list):
            for row in facts["trace"]:
                lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def evaluate_run(task_id: str, run_dir: "str | Path", out_dir: "str | Path",
                 *, source_root: "str | Path | None" = None,
                 registry=None) -> dict:
    """按 ``task_id`` 加载冻结任务定义后评测一次。"""
    from orchestrator.benchmark.registry import load_registry
    reg = registry or load_registry()
    spec = reg.get(task_id)
    evaluator = Evaluator(spec=spec,
                          source_root=Path(source_root) if source_root else None)
    return evaluator.evaluate(run_dir, out_dir)
