"""Phase 2.8.0 Task 4：评测结果 schema（七态保留，不压布尔）。

`simpraxis.benchmark_evaluation.v1` 的冻结 schema 在
``benchmarks/research_engineering_v1/schemas/evaluation_result.v1.schema.json``；
本模块只做**确定性构造 + 序列化 + fail-closed 校验**，不含任何评分逻辑。

纪律：

- 七态枚举（D4-6）：``PASS/FAIL/BLOCKED_INFRA/INVALID_RUNTIME/INVALID_ARTIFACT/
  INCOMPLETE_EVIDENCE/NOT_APPLICABLE``，未知状态一律拒绝；
- ``hard_gates`` 只列**被触发**的门（空列表＝无硬门命中，AC-T4-1）；
- ``gates_checked`` 列**检查过**的门（含未触发），便于审计「有没有查」；
- ``weighted_score`` 只作汇总参考，任何硬门优先于分数（D4-3）；权重与阈值
  一律来自 ``criteria.json``，代码里不写字面量（D4-4）；
- 序列化必须确定性（canonical JSON），保证 sha256 可复算。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from orchestrator.benchmark import task_spec as ts
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

EVALUATION_SCHEMA = "simpraxis.benchmark_evaluation.v1"
EVALUATION_NAME = "evaluation_result.json"
EVALUATION_SHA_NAME = "evaluation_result.sha256"

#: D4-6 七态（顺序即优先级：聚合时前者优先）
STATUSES = ("BLOCKED_INFRA", "INVALID_RUNTIME", "INCOMPLETE_EVIDENCE",
            "INVALID_ARTIFACT", "FAIL", "PASS", "NOT_APPLICABLE")

#: 状态裁决优先级（越小越优先）
_STATUS_PRIORITY = {name: index for index, name in enumerate(STATUSES[:5])}

SCHEMA_PATH = ts.BENCHMARK_ROOT / "schemas" / "evaluation_result.v1.schema.json"


class EvaluationError(RuntimeError):
    """评测结果本身不合法（构造/写盘/校验失败）；候选问题一律落状态，不抛本异常。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationError(message)


@dataclass(frozen=True)
class Dimension:
    """一个评分维度：权重来自 criteria，分数在 [0,1]，detail 给人读。"""

    weight: float
    score: float
    detail: str

    def __post_init__(self) -> None:
        _require(0.0 <= float(self.weight) <= 1.0,
                 f"维度权重越界：{self.weight!r}")
        _require(0.0 <= float(self.score) <= 1.0,
                 f"维度分数越界：{self.score!r}")

    def to_json(self) -> dict:
        return {"weight": round(float(self.weight), 9),
                "score": round(float(self.score), 9),
                "detail": str(self.detail)}


@dataclass(frozen=True)
class GateHit:
    """被触发的硬门；``id`` 必须形如 ``HG-XXX-1``。"""

    id: str
    detail: str

    def __post_init__(self) -> None:
        import re
        _require(bool(re.fullmatch(r"HG-[A-Z0-9]+-[0-9]+", self.id)),
                 f"硬门 id 非法：{self.id!r}")
        _require(bool(self.detail), f"硬门 {self.id} 必须给出 detail")

    def to_json(self) -> dict:
        return {"id": self.id, "detail": self.detail}


@dataclass
class EvaluationResult:
    task_id: str
    run_id: str
    evaluator_id: str
    evaluator_version: int
    criteria_lock_sha256: str
    status: str
    dimensions: "Mapping[str, Dimension]"
    hard_gates: "list[GateHit]" = field(default_factory=list)
    gates_checked: "list[str]" = field(default_factory=list)
    schema: str = EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        _require(self.status in STATUSES, f"未知状态：{self.status!r}")
        _require(bool(self.dimensions), "dimensions 不得为空（schema minProperties=1）")
        _require(len(self.criteria_lock_sha256) == 64,
                 "criteria_lock_sha256 必须是 64 位十六进制")
        _require(int(self.evaluator_version) >= 1, "evaluator_version 必须 ≥ 1")

    @property
    def weighted_score(self) -> float:
        total = sum(float(d.score) * float(d.weight)
                    for d in self.dimensions.values())
        weight_sum = sum(float(d.weight) for d in self.dimensions.values())
        if weight_sum <= 0:
            return 0.0
        return max(0.0, min(1.0, total / weight_sum))

    def to_json(self) -> dict:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": int(self.evaluator_version),
            "criteria_lock_sha256": self.criteria_lock_sha256,
            "status": self.status,
            "dimensions": {name: dim.to_json()
                           for name, dim in self.dimensions.items()},
            "weighted_score": round(self.weighted_score, 9),
            "hard_gates": [hit.to_json() for hit in self.hard_gates],
            "gates_checked": list(self.gates_checked),
        }

    def canonical_json(self) -> str:
        return ts.canonical_json(self.to_json())


def aggregate_status(hits: "list[GateHit]", statuses: "Mapping[str, str]") -> str:
    """``statuses`` 是 gate id → 该门命中时对应的状态。"""
    if not hits:
        return "PASS"
    picked = [statuses.get(hit.id, "FAIL") for hit in hits]
    return sorted(picked, key=lambda s: _STATUS_PRIORITY.get(s, 99))[0]


def validate_result(doc: Mapping) -> dict:
    """按冻结 schema 校验；不合规即 fail-closed（未知状态字符串必被拒）。"""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(doc)),
                    key=lambda e: (list(map(str, e.absolute_path)), e.message))
    if errors:
        first = errors[0]
        where = "/".join(map(str, first.absolute_path)) or "<root>"
        raise EvaluationError(f"评测结果不合 schema：{where}: {first.message}"
                              f"（共 {len(errors)} 处）")
    return dict(doc)


def candidate_digest(files: "Mapping[str, str]") -> str:
    """候选产物聚合摘要：``sha256(canonical_json({relpath: sha256}))``。

    口径在 Task 4 定死并由 Task 6 的 run_manifest 构造器复用：聚合集合 =
    ``task.yaml.outputs`` 去掉 ``run_manifest.json``（自我引用必须排除）。
    """
    body = {str(rel): str(digest) for rel, digest in files.items()}
    return hashlib.sha256(ts.canonical_json(body).encode("utf-8")).hexdigest()


def write_result(out_dir: "str | Path", result: EvaluationResult) -> dict:
    """原子写 ``evaluation_result.json`` + ``evaluation_result.sha256``（0600）。"""
    doc = result.to_json()
    validate_result(doc)          # 先校验再落盘，坏结果不落地
    directory = ensure_private_dir(Path(out_dir))
    path = atomic_write_text(directory / EVALUATION_NAME,
                             json.dumps(doc, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", 0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha_path = atomic_write_text(directory / EVALUATION_SHA_NAME,
                                 f"{digest}  {EVALUATION_NAME}\n", 0o600)
    return {"result_path": str(path), "sha256_path": str(sha_path),
            "result_sha256": digest, "status": result.status}
