"""任务定义（Task Specification）的加载、校验与判据 LOCK（Plan Task 1 / D1-2..D1-5）。

三层边界（与 ``orchestrator/skills/registry.py`` 同构的先例）：

- 任务定义是**数据**（``benchmarks/research_engineering_v1/tasks/<name>/task.yaml``），
  代码只解析、校验、返回只读视图；
- **绝不执行** ``solution.py``、绝不 import 内容层技能代码——本模块只用
  ``yaml`` / ``jsonschema`` / ``hashlib`` / ``json``；
- 任何不合规（未知字段、缺字段、类型不符、判据锁不符）一律 ``TaskSpecError``
  fail-closed，不存在「尽力而为地加载」。

判据锁（D1-5）口径：对 ``criteria/*.criteria.json`` 的**白名单子集**
``criteria_version / weights / thresholds / hard_gates / derivation`` 做
canonical JSON（``sort_keys=True, separators=(",", ":")``）后取 sha256。
``evaluator_files`` / ``locked_at`` 与任何未来新增键**一律不进哈希**，所以
Task 4 回填它们不会改变锁值；Task 1 提交时冻结，Task 6 只校验不回写（STOP-H）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import jsonschema
import yaml

SCHEMA_ID = "simpraxis.benchmark_task.v1"
CRITERIA_SCHEMA_ID = "simpraxis.benchmark_criteria.v1"
MANIFEST_SCHEMA_ID = "simpraxis.benchmark_manifest.v1"

BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "research_engineering_v1"
SCHEMA_PATH = BENCHMARK_ROOT / "schemas" / "task_spec.v1.schema.json"

#: D1-5 白名单：只有这五个键参与 criteria_lock_sha256
CRITERIA_LOCK_KEYS = ("criteria_version", "weights", "thresholds",
                      "hard_gates", "derivation")

#: criteria 文件允许的顶层键（多一个即 unknown_field）
CRITERIA_ALLOWED_KEYS = frozenset(CRITERIA_LOCK_KEYS) | {
    "schema", "task_id", "evaluator_files", "locked_at"}

GATE_ID_RE = re.compile(r"^HG-[A-Z0-9]+-[0-9]+$")
_WEIGHT_SUM_TOL = 1e-9


class TaskSpecError(RuntimeError):
    """任务定义不合规：调用方必须停止，不得兜底加载。

    ``reason`` 是机判口径（AC-T1-2/3 断言精确取值），``path`` 指向出错文件，
    ``detail`` 给人读。
    """

    def __init__(self, reason: str, path: "str | Path", detail: str = "") -> None:
        self.reason = reason
        self.path = str(path)
        self.detail = detail
        super().__init__(f"[{reason}] {self.path}: {detail}" if detail
                         else f"[{reason}] {self.path}")


# --------------------------------------------------------------------------
# canonical JSON / 哈希
# --------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """确定性序列化：key 排序、无多余空白、UTF-8 原文（不转义中文）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: "str | Path") -> str:
    return sha256_bytes(Path(path).read_bytes())


def _is_number(value: Any) -> bool:
    # bool 是 int 的子类，必须显式排除（true/false 不是阈值）
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# 只读视图
# --------------------------------------------------------------------------

def _freeze(obj: Any) -> Any:
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(v) for v in obj)
    return obj


def _thaw(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _thaw(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_thaw(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# criteria 校验与 LOCK
# --------------------------------------------------------------------------

def validate_criteria(doc: Any, path: "str | Path" = "<criteria>") -> dict:
    """校验 criteria 文档；返回**原样**的 dict（不修改入参）。"""
    if not isinstance(doc, dict):
        raise TaskSpecError("schema_violation", path, f"顶层必须是对象，实为 {type(doc).__name__}")

    unknown = sorted(set(doc) - CRITERIA_ALLOWED_KEYS)
    if unknown:
        raise TaskSpecError("unknown_field", path, f"未知字段：{unknown}")

    missing = [k for k in CRITERIA_LOCK_KEYS if k not in doc]
    if missing:
        raise TaskSpecError("missing_field", path, f"缺必要字段：{missing}")

    if "schema" in doc and doc["schema"] != CRITERIA_SCHEMA_ID:
        raise TaskSpecError("schema_violation", path,
                            f"schema 必须是 {CRITERIA_SCHEMA_ID}，实为 {doc['schema']!r}")

    if not _is_int(doc["criteria_version"]) or doc["criteria_version"] < 1:
        raise TaskSpecError("schema_violation", path,
                            f"criteria_version 必须是 ≥1 的整数：{doc['criteria_version']!r}")

    weights = doc["weights"]
    if not isinstance(weights, dict) or not weights:
        raise TaskSpecError("schema_violation", path, "weights 必须是非空对象")
    for name, weight in weights.items():
        if not isinstance(name, str) or not name:
            raise TaskSpecError("schema_violation", path, f"权重名非法：{name!r}")
        if not _is_number(weight) or not (0.0 < float(weight) <= 1.0):
            raise TaskSpecError("schema_violation", path,
                                f"权重 {name} 必须在 (0, 1] 内：{weight!r}")
    total = sum(float(w) for w in weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOL:
        raise TaskSpecError("schema_violation", path,
                            f"权重和必须为 1.0（容差 {_WEIGHT_SUM_TOL}），实为 {total!r}")

    thresholds = doc["thresholds"]
    if not isinstance(thresholds, dict):
        raise TaskSpecError("schema_violation", path, "thresholds 必须是对象（可为空 {}）")
    for key, item in thresholds.items():
        if not isinstance(item, dict):
            raise TaskSpecError("schema_violation", path, f"阈值 {key} 必须是对象")
        extra = sorted(set(item) - {"value", "unit", "derivation"})
        if extra:
            raise TaskSpecError("unknown_field", path, f"阈值 {key} 含未知字段：{extra}")
        miss = [k for k in ("value", "unit", "derivation") if k not in item]
        if miss:
            raise TaskSpecError("missing_field", path, f"阈值 {key} 缺字段：{miss}")
        if not _is_number(item["value"]):
            raise TaskSpecError("schema_violation", path,
                                f"阈值 {key}.value 必须是数值：{item['value']!r}")
        if not isinstance(item["unit"], str) or not item["unit"].strip():
            raise TaskSpecError("schema_violation", path, f"阈值 {key}.unit 必须是非空字符串")
        if not isinstance(item["derivation"], str) or not item["derivation"].strip():
            raise TaskSpecError("schema_violation", path,
                                f"阈值 {key}.derivation 必须是非空字符串（预设即编造）")

    gates = doc["hard_gates"]
    if not isinstance(gates, list):
        raise TaskSpecError("schema_violation", path, "hard_gates 必须是数组")
    seen = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != {"id", "statement"}:
            raise TaskSpecError("schema_violation", path,
                                f"hard_gates 每项必须是 {{id, statement}}：{gate!r}")
        if not isinstance(gate["id"], str) or not GATE_ID_RE.match(gate["id"]):
            raise TaskSpecError("schema_violation", path, f"硬门 id 非法：{gate['id']!r}")
        if gate["id"] in seen:
            raise TaskSpecError("schema_violation", path, f"硬门 id 重复：{gate['id']}")
        seen.add(gate["id"])
        if not isinstance(gate["statement"], str) or not gate["statement"].strip():
            raise TaskSpecError("schema_violation", path, f"硬门 {gate['id']} 缺 statement")

    if not isinstance(doc["derivation"], str) or not doc["derivation"].strip():
        raise TaskSpecError("schema_violation", path, "derivation 必须是非空字符串")

    if "evaluator_files" in doc:
        files = doc["evaluator_files"]
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise TaskSpecError("schema_violation", path, "evaluator_files 必须是字符串数组")
    if "locked_at" in doc and doc["locked_at"] is not None and \
            not isinstance(doc["locked_at"], str):
        raise TaskSpecError("schema_violation", path, "locked_at 必须是字符串或 null")
    return doc


def criteria_lock_sha256(doc: dict) -> str:
    """D1-5 规范子集的 sha256（白名单之外的键一律不进哈希）。"""
    validate_criteria(doc)
    subset = {key: doc[key] for key in CRITERIA_LOCK_KEYS}
    return sha256_bytes(canonical_json(subset).encode("utf-8"))


def criteria_aggregate_sha256(locks: Mapping[str, str]) -> str:
    """manifest.criteria_lock：按 task_id 排序的 ``id\\0lock\\n`` 拼接后 sha256。"""
    h = hashlib.sha256()
    for task_id in sorted(locks):
        h.update(f"{task_id}\0{locks[task_id]}\n".encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------
# task.yaml 校验
# --------------------------------------------------------------------------

def _load_task_schema(schema_path: "str | Path" = SCHEMA_PATH) -> dict:
    return json.loads(Path(schema_path).read_text(encoding="utf-8"))


#: jsonschema 错误 → TaskSpecError.reason（AC-T1-2/3 的机判口径）
_VALIDATOR_REASON = {
    "additionalProperties": "unknown_field",
    "required": "missing_field",
}
#: 同一文档多处违规时的择优顺序：未知字段 > 缺字段 > 其余
_REASON_PRIORITY = {"unknown_field": 0, "missing_field": 1, "schema_violation": 2}


def validate_task_spec(doc: Any, path: "str | Path" = "<task>",
                       *, schema_path: "str | Path" = SCHEMA_PATH) -> dict:
    """按 ``task_spec.v1.schema.json`` 校验；返回原 dict。"""
    if not isinstance(doc, dict):
        raise TaskSpecError("schema_violation", path,
                            f"顶层必须是对象，实为 {type(doc).__name__}")
    validator = jsonschema.Draft202012Validator(_load_task_schema(schema_path))
    errors = list(validator.iter_errors(doc))
    if errors:
        def sort_key(err: jsonschema.ValidationError):
            reason = _VALIDATOR_REASON.get(err.validator, "schema_violation")
            return (_REASON_PRIORITY[reason], list(map(str, err.absolute_path)),
                    err.message)

        best = sorted(errors, key=sort_key)[0]
        reason = _VALIDATOR_REASON.get(best.validator, "schema_violation")
        where = "/".join(str(p) for p in best.absolute_path) or "<root>"
        raise TaskSpecError(reason, path, f"{where}: {best.message}")
    return doc


# --------------------------------------------------------------------------
# TaskSpec
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一条任务定义的只读视图（D1-4：不可就地修改）。"""

    task_id: str
    version: int
    track: str
    task_type: str
    objective: str
    runtime: Mapping
    inputs: Mapping
    entrypoint: Mapping
    birth_contract: Mapping
    outputs: tuple
    evaluation: Mapping
    budget: Mapping
    reproducibility: Mapping
    constraints: Mapping
    criteria: Mapping
    criteria_lock_sha256: str
    criteria_sha256: str
    task_file_sha256: str
    task_file: str
    criteria_file: str

    # -- 便捷只读派生 ------------------------------------------------------
    @property
    def criteria_version(self) -> int:
        return int(self.criteria["criteria_version"])

    @property
    def weights(self) -> Mapping:
        return self.criteria["weights"]

    @property
    def thresholds(self) -> Mapping:
        return self.criteria["thresholds"]

    @property
    def hard_gates(self) -> tuple:
        return self.criteria["hard_gates"]

    def to_json(self) -> dict:
        """确定性可序列化视图（快照 / AC-T1-6 逐字节比较用）。"""
        return {
            "task_id": self.task_id,
            "version": self.version,
            "track": self.track,
            "task_type": self.task_type,
            "objective": self.objective,
            "runtime": _thaw(self.runtime),
            "inputs": _thaw(self.inputs),
            "entrypoint": _thaw(self.entrypoint),
            "birth_contract": _thaw(self.birth_contract),
            "outputs": list(self.outputs),
            "evaluation": _thaw(self.evaluation),
            "budget": _thaw(self.budget),
            "reproducibility": _thaw(self.reproducibility),
            "constraints": _thaw(self.constraints),
            "criteria": _thaw(self.criteria),
            "criteria_lock_sha256": self.criteria_lock_sha256,
            "criteria_sha256": self.criteria_sha256,
            "task_file": self.task_file,
            "task_file_sha256": self.task_file_sha256,
        }


def default_criteria_dir(task_path: "str | Path") -> Path:
    """``<root>/tasks/<name>/task.yaml`` → ``<root>/criteria``。"""
    return Path(task_path).resolve().parents[2] / "criteria"


def default_benchmark_root(task_path: "str | Path") -> Path:
    return Path(task_path).resolve().parents[2]


def load_task_spec(task_path: "str | Path", *,
                   criteria_dir: "str | Path | None" = None,
                   schema_path: "str | Path" = SCHEMA_PATH) -> TaskSpec:
    """加载并校验一条任务定义（含判据锁复算）。"""
    path = Path(task_path)
    if not path.is_file():
        raise TaskSpecError("file_not_found", path, "任务定义不存在")
    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise TaskSpecError("invalid_yaml", path, str(exc)) from exc
    validate_task_spec(doc, path, schema_path=schema_path)

    root = default_benchmark_root(path)
    crit_dir = Path(criteria_dir) if criteria_dir else default_criteria_dir(path)
    evaluator_id = doc["evaluation"]["evaluator_id"]
    crit_path = crit_dir / f"{evaluator_id}.criteria.json"
    if not crit_path.is_file():
        raise TaskSpecError("file_not_found", crit_path,
                            f"任务 {doc['task_id']} 的判据文件不在场")
    crit_raw = crit_path.read_bytes()
    try:
        criteria = json.loads(crit_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskSpecError("invalid_yaml", crit_path, str(exc)) from exc
    validate_criteria(criteria, crit_path)
    if criteria.get("task_id") != doc["task_id"]:
        raise TaskSpecError("criteria_task_mismatch", crit_path,
                            f"criteria.task_id={criteria.get('task_id')!r} 与 "
                            f"task_id={doc['task_id']!r} 不符")

    computed = criteria_lock_sha256(criteria)
    declared = doc["evaluation"]["criteria_lock_sha256"]
    if computed != declared:
        raise TaskSpecError(
            "criteria_lock_mismatch", path,
            f"判据锁不符：声明 {declared}，重算 {computed}"
            "（判据被改动而未重新 LOCK，或 LOCK 被手改）")

    return TaskSpec(
        task_id=doc["task_id"],
        version=doc["version"],
        track=doc["track"],
        task_type=doc["task_type"],
        objective=doc["objective"],
        runtime=_freeze(doc["runtime"]),
        inputs=_freeze(doc["inputs"]),
        entrypoint=_freeze(doc["entrypoint"]),
        birth_contract=_freeze(doc["birth_contract"]),
        outputs=tuple(doc["outputs"]),
        evaluation=_freeze(doc["evaluation"]),
        budget=_freeze(doc["budget"]),
        reproducibility=_freeze(doc["reproducibility"]),
        constraints=_freeze(doc["constraints"]),
        criteria=_freeze(criteria),
        criteria_lock_sha256=declared,
        criteria_sha256=sha256_bytes(crit_raw),
        task_file_sha256=sha256_bytes(raw),
        task_file=_rel_to(path, root),
        criteria_file=_rel_to(crit_path, root),
    )


def _rel_to(path: Path, root: Path) -> str:
    """根内路径用相对形式（跨机器可复现）；根外路径（测试用 tmp）原样绝对。"""
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())
