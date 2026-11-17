"""R1.3 设计声明 ↔ 实际执行审计（DesignIntentV13 / audit_design）。

Plan §3 的落地。R1.2 复盘（§16）的直接教训：候选 plan 声明
``inertia_ratio ∈ [0.5, 2.0]``，但探索期 48 个 run 全部用占位值 1.0，
确认期却由宿主逐 block 采样——声明与执行不一致，双审据此判 FAIL。R1.3 用
typed 声明 + **前后两次纯函数审计**把这件事变成可机判：

* 前执行 ``audit_design(intent, plan=..., proposed_cells=..., phase="pre")``：
  schema/范围/配对/重复/估计对象/分配责任声明；
* 后执行 ``audit_design(..., actual_manifest=..., phase="post")``：
  以宿主验证过的 manifest 为准，逐 run 核 actual params 与分配；
  **候选自报数组不替代 manifest**。

错误分类（Plan §3 末段）：

* 前执行声明不符且无副作用 → ``DESIGN_DECLARATION_INVALID``（可在既有 lane
  预算内修正，不重发已发生的 POST）；
* trusted actual ≠ assigned、输入来源漂移或未知执行 → ``INSTRUMENT_MISMATCH`` /
  ``EXECUTION_UNKNOWN``（仪器/安全终态，不允许重试掩盖）；
* 分布不同本身不判失败；**漏声明/虚假声明**才是合同失败。

计数口径：``run_count``（实际执行数）、``unique_design_count``（去重后的
参数组合数）、``independent_unit_count``（独立抽样单元数）。确定性重复只增
前二者，不增第三者；连续抽样偶发相同值不因此判伪重复。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import task_specs as TS

INTENT_KEYS = frozenset({"schema", "schema_version", "phase_role", "estimand",
                         "nuisance", "replication", "phase_comparability"})
PHASE_ROLES = ("fixed_pilot", "distributional_exploration", "confirmation")
RELATIONS = ("same_target", "different_target")

AUDIT_SCHEMA = P.DESIGN_AUDIT_SCHEMA_V13


class DesignError(ValueError):
    """设计审计拒绝；``code`` 公开且可机判。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _decimal(value: Any, *, where: str) -> Decimal:
    if isinstance(value, bool):
        raise DesignError("WRONG_TYPE", f"{where} 不接受布尔")
    if isinstance(value, float):
        raise DesignError("FLOAT_INPUT", f"{where} 必须是字符串十进制")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DesignError("WRONG_TYPE", f"{where}={value!r}") from None
    if not out.is_finite():
        raise DesignError("NONFINITE", f"{where}={value!r}")
    return out


@dataclass(frozen=True)
class DesignIntentV13:
    phase_role: str
    estimand: "dict[str, Any]"
    nuisance: "dict[str, dict[str, Any]]"
    replication: "dict[str, Any]"
    phase_comparability: "dict[str, Any]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": P.DESIGN_INTENT_SCHEMA_V13,
            "schema_version": 1,
            "phase_role": self.phase_role,
            "estimand": dict(self.estimand),
            "nuisance": {k: dict(v) for k, v in self.nuisance.items()},
            "replication": dict(self.replication),
            "phase_comparability": dict(self.phase_comparability),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, doc: Any, *, where: str = "intent") -> "DesignIntentV13":
        if not isinstance(doc, Mapping):
            raise DesignError("WRONG_TYPE", f"{where} 必须是对象")
        unknown = sorted(set(doc) - INTENT_KEYS)
        missing = sorted(INTENT_KEYS - set(doc))
        if unknown or missing:
            raise DesignError("KEY_MISMATCH", f"{where} 未知 {unknown} 缺 {missing}")
        if doc["schema"] != P.DESIGN_INTENT_SCHEMA_V13:
            raise DesignError("SCHEMA_MISMATCH", f"{where}.schema")
        role = doc["phase_role"]
        if role not in PHASE_ROLES:
            raise DesignError("PHASE_ROLE", f"{where}.phase_role={role!r}")
        nuisance = doc["nuisance"]
        if not isinstance(nuisance, Mapping):
            raise DesignError("WRONG_TYPE", f"{where}.nuisance")
        parsed: "dict[str, dict[str, Any]]" = {}
        for name, raw in nuisance.items():
            if not isinstance(raw, Mapping):
                raise DesignError("NUISANCE_INVALID", str(name))
            mode = raw.get("mode")
            if mode not in ("fixed", "varying"):
                raise DesignError("NUISANCE_INVALID", f"{name}.mode")
            owner = raw.get("assignment_owner")
            if owner not in ("host", "candidate"):
                raise DesignError("NUISANCE_INVALID", f"{name}.assignment_owner")
            support = raw.get("support")
            if not isinstance(support, (list, tuple)) or len(support) != 2:
                raise DesignError("NUISANCE_INVALID", f"{name}.support")
            lo = _decimal(support[0], where=f"{name}.support[0]")
            hi = _decimal(support[1], where=f"{name}.support[1]")
            if lo > hi:
                raise DesignError("NUISANCE_INVALID", f"{name} 区间倒置")
            parsed[str(name)] = {"mode": mode, "assignment_owner": owner,
                                 "support": [str(lo), str(hi)]}
        comparability = doc["phase_comparability"]
        if not isinstance(comparability, Mapping):
            raise DesignError("WRONG_TYPE", f"{where}.phase_comparability")
        relation = comparability.get("relation")
        if relation not in RELATIONS:
            raise DesignError("PHASE_COMPARABILITY", f"relation={relation!r}")
        rationale = comparability.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise DesignError("PHASE_COMPARABILITY", "必须给 rationale")
        replication = doc["replication"]
        if not isinstance(replication, Mapping):
            raise DesignError("WRONG_TYPE", f"{where}.replication")
        for key in ("unit_id", "independent_unit",
                    "deterministic_repeat_purpose"):
            if key not in replication:
                raise DesignError("REPLICATION_INVALID", key)
        estimand = doc["estimand"]
        if not isinstance(estimand, Mapping):
            raise DesignError("WRONG_TYPE", f"{where}.estimand")
        return cls(phase_role=str(role), estimand=dict(estimand),
                   nuisance=parsed, replication=dict(replication),
                   phase_comparability=dict(comparability))


def parse_design_intent(doc: Any, *, profile: P.CampaignProfile,
                        task_spec: TS.TaskSpecV13) -> DesignIntentV13:
    """解析并绑定任务：估计对象必须与任务登记一致，nuisance 责任不得漂移。"""
    if not profile.is_r13_family:
        raise DesignError("PROFILE_NOT_ELIGIBLE", profile.profile_id)
    intent = DesignIntentV13.from_dict(doc)
    metric = task_spec.estimand["metric_id"]
    if intent.estimand.get("metric_id") != metric:
        raise DesignError(
            "ESTIMAND_INVALID",
            f"intent.metric_id={intent.estimand.get('metric_id')!r} != 任务 {metric!r}")
    contrast_ids = intent.estimand.get("contrast_ids")
    if not isinstance(contrast_ids, (list, tuple)):
        raise DesignError("ESTIMAND_INVALID", "contrast_ids 必须是数组")
    if task_spec.primary.id not in contrast_ids:
        raise DesignError("ESTIMAND_INVALID",
                          f"contrast_ids 未包含任务主对比 {task_spec.primary.id}")
    unknown = sorted(set(contrast_ids) - set(task_spec.estimand["contrast_ids"]))
    if unknown:
        raise DesignError("ESTIMAND_INVALID", f"任务未登记对比 {unknown}")
    for name, declared in task_spec.nuisance_schema.items():
        got = intent.nuisance.get(name)
        if got is None:
            raise DesignError("NUISANCE_DECLARATION_MISSING",
                              f"任务声明 nuisance {name}，intent 未声明")
        if got["mode"] != declared["mode"]:
            raise DesignError(
                "NUISANCE_DECLARATION_MISMATCH",
                f"{name}: intent={got['mode']} task={declared['mode']}")
        if got["assignment_owner"] == "candidate" and \
                declared.get("assignment_owner") == "host":
            raise DesignError(
                "ASSIGNMENT_OWNERSHIP_CLAIM",
                f"{name} 由 host 分配，候选不得声称自己实现随机化")
    for name, got in intent.nuisance.items():
        if name not in task_spec.nuisance_schema:
            raise DesignError("NUISANCE_UNDECLARED",
                              f"intent 声明了任务未登记的 nuisance {name}")
    if intent.phase_role == "fixed_pilot":
        if intent.phase_comparability["relation"] != "different_target":
            raise DesignError(
                "PILOT_SCOPE_UNDECLARED",
                "固定点 pilot 的结论只适用该点：必须声明 different_target")
    return intent


@dataclass(frozen=True)
class DesignAudit:
    phase: str              # pre｜post
    ok: bool
    codes: "tuple[str, ...]"
    run_count: int
    unique_design_count: int
    independent_unit_count: int
    checks: "tuple[dict[str, Any], ...]" = field(default_factory=tuple)
    actual_verified: bool = False

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": AUDIT_SCHEMA,
            "schema_version": 1,
            "phase": self.phase,
            "ok": self.ok,
            "codes": list(self.codes),
            "run_count": self.run_count,
            "unique_design_count": self.unique_design_count,
            "independent_unit_count": self.independent_unit_count,
            "checks": [dict(c) for c in self.checks],
            "actual_verified": self.actual_verified,
        }


def _cell_key(condition_id: str, params: Mapping[str, Any]) -> str:
    body = {"condition_id": condition_id,
            "params": {k: str(v) for k, v in sorted(params.items())}}
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_cells(intent: DesignIntentV13, task: TS.TaskSpecV13,
                 cells: Sequence[Mapping[str, Any]], *,
                 phase: str) -> "list[dict[str, Any]]":
    checks: "list[dict[str, Any]]" = []
    known = {c.id: c for c in task.conditions}
    if not cells:
        raise DesignError("CELLS_EMPTY", "没有设计单元")
    for i, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            raise DesignError("CELL_INVALID", f"cell[{i}] 不是对象")
        cid = cell.get("condition_id")
        if cid not in known:
            raise DesignError("CELL_INVALID", f"cell[{i}] 条件 {cid!r} 未登记")
        block = cell.get("block_id")
        if not isinstance(block, str) or not block.strip():
            raise DesignError("CELL_INVALID", f"cell[{i}] 缺 block_id")
        params = cell.get("params") or {}
        if not isinstance(params, Mapping):
            raise DesignError("CELL_INVALID", f"cell[{i}].params")
        base = dict(known[cid].params)
        merged = {**base, **{str(k): str(v) for k, v in params.items()}}
        # 只允许覆盖：任务条件本身的参数 / 已声明的 nuisance 参数
        allowed = set(base) | set(intent.nuisance)
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise DesignError("CELL_PARAM_UNDECLARED",
                              f"cell[{i}] 覆盖未声明参数 {unknown}")
        for name, spec in intent.nuisance.items():
            if name in params:
                value = _decimal(params[name], where=f"cell[{i}].{name}")
                lo = Decimal(spec["support"][0])
                hi = Decimal(spec["support"][1])
                if value < lo or value > hi:
                    raise DesignError(
                        "NUISANCE_RANGE", f"cell[{i}].{name}={value} 越出 [{lo},{hi}]")
        checks.append({"id": f"cell[{i}]", "condition_id": cid,
                       "block_id": block, "params": merged,
                       "key": _cell_key(str(cid), merged)})
    return checks


def audit_design(intent: DesignIntentV13, *, task: TS.TaskSpecV13,
                 proposed_cells: Sequence[Mapping[str, Any]] | None = None,
                 actual_manifest: Sequence[Mapping[str, Any]] | None = None,
                 ) -> DesignAudit:
    """纯函数审计：``proposed_cells`` 为前执行；``actual_manifest`` 为后执行。

    后执行以宿主 manifest 为唯一事实源：``actual != assigned`` 即
    ``INSTRUMENT_MISMATCH``（终态，不能伪装成候选失败/重新挑 seed）。
    """
    if actual_manifest is None:
        if proposed_cells is None:
            raise DesignError("INPUT_MISSING", "pre 需要 proposed_cells")
        checks = _check_cells(intent, task, proposed_cells, phase="pre")
        keys = [c["key"] for c in checks]
        units = {str(c["block_id"]) for c in checks}
        codes: "list[str]" = []
        if intent.phase_role == "distributional_exploration":
            for name, spec in intent.nuisance.items():
                if spec["mode"] != "varying":
                    continue
                values = {str(c["params"].get(name)) for c in checks
                          if name in c["params"]}
                if len(values) < 2:
                    # 连续抽样允许偶发重复；但**全部**用同一占位值 = 声明未执行
                    raise DesignError(
                        "DESIGN_DECLARATION_INVALID",
                        f"{name} 声明 varying，但全部 cell 取值 {sorted(values)}"
                        "（占位值不等于执行了分配）")
                codes.append("DECLARED_VARIATION_OBSERVED")
        return DesignAudit(
            phase="pre", ok=True, codes=tuple(codes),
            run_count=len(checks), unique_design_count=len(set(keys)),
            independent_unit_count=len(units), checks=tuple(checks))
    # post：manifest 必须是宿主验证件（带 verified 标记）
    actual: "list[dict[str, Any]]" = []
    for i, row in enumerate(actual_manifest):
        if not isinstance(row, Mapping):
            raise DesignError("MANIFEST_INVALID", f"manifest[{i}]")
        if row.get("verified_by_host") is not True:
            raise DesignError("EXECUTION_UNKNOWN",
                              f"manifest[{i}] 未经宿主验证，不能替代实际参数核对")
        actual.append(dict(row))
    checks = _check_cells(intent, task, actual, phase="post")
    if proposed_cells is not None:
        assigned = _check_cells(intent, task, proposed_cells, phase="pre")
        by_key = {c["key"] for c in assigned}
        for c in checks:
            if c["key"] not in by_key:
                raise DesignError(
                    "INSTRUMENT_MISMATCH",
                    f"{c['block_id']}/{c['condition_id']} 实际参数不在分配之内")
    keys = [c["key"] for c in checks]
    units = {str(c["block_id"]) for c in checks}
    return DesignAudit(
        phase="post", ok=True, codes=("ACTUAL_MATCHES_ASSIGNED",),
        run_count=len(checks), unique_design_count=len(set(keys)),
        independent_unit_count=len(units), checks=tuple(checks),
        actual_verified=True)


def counts_document(*, run_count: int, unique_design_count: int,
                    independent_unit_count: int) -> "dict[str, int]":
    """三计数（Plan §3）：确定性重复不得扩大 independent_unit_count。"""
    if min(run_count, unique_design_count, independent_unit_count) < 0:
        raise DesignError("COUNT_INVALID", "计数不得为负")
    if not (run_count >= unique_design_count >= independent_unit_count):
        raise DesignError(
            "COUNT_INVALID",
            "必须 run_count ≥ unique_design_count ≥ independent_unit_count")
    return {"run_count": int(run_count),
            "unique_design_count": int(unique_design_count),
            "independent_unit_count": int(independent_unit_count)}
