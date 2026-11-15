"""R1.3 候选诊断契约与宿主独立复算（``analysis_output.v13``）。

Plan §4 的落地：诊断是**候选自己算**的描述性信息，不改变主假设/阈值，也不
事后新增有利对比或显著性筛选。宿主验证只做三件事：

1. **结构/来源**：phase/study/task/input/source hash 与冻结件一致；block 覆盖
   恰为确认集（缺 block、跨 study、重复 id 一律拒）；
2. **数值**：用公开线性对比算法从 trusted raw 逐 block 重算，候选值必须逐位
   一致（容差 ``CLAIM_ABS_TOL/CLAIM_REL_TOL``，与 judge 同一口径）；
3. **分组纪律**：任务未预登记分组时，候选只能标 ``exploratory``；声称
   ``preregistered`` 即拒（不得把数据驱动分层升级为主结论）。

本模块**不执行候选代码**，只从宿主 raw 复算；也不把正确均值/分组答案回灌
候选（错误只报 schema/来源/数值一致性，不报"正确答案"）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import contrasts as X
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import task_specs as TS

DIAG_SCHEMA = P.ANALYSIS_OUTPUT_SCHEMA_V13

_KEYS = frozenset({"schema", "schema_version", "phase", "study_id", "task_id",
                   "input_sha256", "source_sha256", "contrasts", "coverage",
                   "summaries", "threshold_counts", "phase_comparability"})

PHASES = ("explore", "confirm", "pilot")


class DiagnosticError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


@dataclass(frozen=True)
class DiagnosticVerdict:
    ok: bool
    codes: "tuple[str, ...]"
    checks: "tuple[dict[str, Any], ...]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": f"{P.SCHEMA_PREFIX_V13}.diagnostic_verdict.v1",
            "schema_version": 1,
            "ok": self.ok,
            "codes": list(self.codes),
            "checks": [dict(c) for c in self.checks],
        }


def _finite(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise DiagnosticError("WRONG_TYPE", f"{where} 不接受布尔")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise DiagnosticError("WRONG_TYPE", f"{where}={value!r}") from None
    if not math.isfinite(out):
        raise DiagnosticError("NONFINITE", f"{where}={value!r}")
    return out


def _close(candidate: Any, reference: float, *, where: str) -> None:
    got = _finite(candidate, where=where)
    if abs(got - reference) > float(R.CLAIM_ABS_TOL) + float(
            R.CLAIM_REL_TOL) * abs(reference):
        raise DiagnosticError(
            "DIAGNOSTIC_MISMATCH",
            f"{where}={got!r} 与宿主复算 {reference!r} 不一致（容差 "
            f"{R.CLAIM_ABS_TOL}/{R.CLAIM_REL_TOL}）")


def _summaries(summaries: Any, *, treat_preregistered: bool) -> "list[dict[str, Any]]":
    if summaries is None:
        return []
    if not isinstance(summaries, (list, tuple)):
        raise DiagnosticError("WRONG_TYPE", "summaries 必须是数组")
    out: "list[dict[str, Any]]" = []
    for i, raw in enumerate(summaries):
        if not isinstance(raw, Mapping):
            raise DiagnosticError("WRONG_TYPE", f"summaries[{i}]")
        keys = {"group_id", "metric", "n", "mean", "median", "min", "max",
                "preregistered"}
        if set(raw) != keys:
            raise DiagnosticError("KEY_MISMATCH", f"summaries[{i}] 键集合")
        n = int(raw["n"])
        if n <= 0:
            raise DiagnosticError("SUMMARY_INVALID", f"summaries[{i}].n")
        values = [_finite(raw[k], where=f"summaries[{i}].{k}")
                  for k in ("mean", "median", "min", "max")]
        if not (values[2] <= values[1] <= values[3]
                and values[2] <= values[0] <= values[3]):
            raise DiagnosticError("SUMMARY_INVALID",
                                  f"summaries[{i}] 顺序 min≤(median,mean)≤max")
        prereg = bool(raw["preregistered"])
        if prereg and not treat_preregistered:
            raise DiagnosticError(
                "GROUP_NOT_PREREGISTERED",
                f"summaries[{i}] 声称预登记，但任务未登记分组边界")
        out.append({**{k: raw[k] for k in ("group_id", "metric", "n",
                                           "preregistered")},
                    "exploratory": not prereg})
    return out


def validate_diagnostics(output: Any, *, task: TS.TaskSpecV13,
                         raw: Mapping[str, Mapping[str, Any]],
                         expected_phase: str,
                         expected_input_sha256: str,
                         expected_source_sha256: str,
                         registered_groups: "Sequence[str]" = (),
                         design_counts: "Mapping[str, int] | None" = None
                         ) -> DiagnosticVerdict:
    """从 trusted raw 独立复算候选诊断；不一致即拒（码进公开 refusal 表）。"""
    if not isinstance(output, Mapping):
        raise DiagnosticError("WRONG_TYPE", "analysis_output 必须是对象")
    unknown = sorted(set(output) - _KEYS)
    missing = sorted(_KEYS - set(output))
    if unknown or missing:
        raise DiagnosticError("KEY_MISMATCH", f"未知 {unknown} 缺 {missing}")
    if output["schema"] != DIAG_SCHEMA:
        raise DiagnosticError("SCHEMA_MISMATCH", f"schema={output['schema']!r}")
    if output["phase"] != expected_phase:
        raise DiagnosticError("PHASE_MISMATCH",
                              f"{output['phase']!r} != {expected_phase!r}")
    if output["task_id"] != task.task_id:
        raise DiagnosticError("TASK_MISMATCH", str(output["task_id"]))
    if output["study_id"] != task.mechanism_id.split("_")[0]:
        raise DiagnosticError("STUDY_MISMATCH", str(output["study_id"]))
    if output["input_sha256"] != expected_input_sha256:
        raise DiagnosticError("INPUT_DRIFT", "diagnostics.input_sha256 不符")
    if output["source_sha256"] != expected_source_sha256:
        raise DiagnosticError("SOURCE_DRIFT", "diagnostics.source_sha256 不符")

    coverage = output["coverage"]
    if not isinstance(coverage, Mapping):
        raise DiagnosticError("WRONG_TYPE", "coverage")
    blocks = coverage.get("blocks")
    if not isinstance(blocks, (list, tuple)) or not blocks:
        raise DiagnosticError("COVERAGE", "coverage.blocks 为空")
    if len(set(blocks)) != len(blocks):
        raise DiagnosticError("COVERAGE", "coverage.blocks 有重复")
    if set(blocks) != set(raw):
        raise DiagnosticError(
            "COVERAGE", f"coverage.blocks 与 trusted raw 不一致："
            f"缺 {sorted(set(raw) - set(blocks))} 多 {sorted(set(blocks) - set(raw))}")
    if design_counts is not None:
        for key in ("run_count", "unique_design_count", "independent_unit_count"):
            if int(coverage.get(key, -1)) != int(design_counts[key]):
                raise DiagnosticError("COVERAGE", f"coverage.{key} 与设计审计不符")

    contrasts = output["contrasts"]
    if not isinstance(contrasts, (list, tuple)) or not contrasts:
        raise DiagnosticError("CONTRASTS_EMPTY", "contrasts 为空")
    seen: "set[str]" = set()
    checks: "list[dict[str, Any]]" = []
    for i, row in enumerate(contrasts):
        if not isinstance(row, Mapping):
            raise DiagnosticError("WRONG_TYPE", f"contrasts[{i}]")
        cid = row.get("contrast_id")
        if cid in seen:
            raise DiagnosticError("DUPLICATE_CONTRAST", str(cid))
        seen.add(cid)
        if cid != task.primary.id:
            raise DiagnosticError("UNREGISTERED_CONTRAST", str(cid))
        contrast = task.primary
        reference = contrast.estimate(raw)
        _close(row.get("effect"), reference["effect"], where=f"{cid}.effect")
        for key in ("lower", "upper"):
            _close(row.get(key), reference[key], where=f"{cid}.{key}")
        if int(row.get("n_blocks", -1)) != int(reference["n_blocks"]):
            raise DiagnosticError("DIAGNOSTIC_MISMATCH", f"{cid}.n_blocks")
        block_rows = row.get("block_effects")
        expected = contrast.block_effects(raw)
        if not isinstance(block_rows, (list, tuple)) or len(block_rows) != len(expected):
            raise DiagnosticError("BLOCK_EFFECTS_INCOMPLETE", cid)
        got_blocks = {}
        for j, entry in enumerate(block_rows):
            if not isinstance(entry, Mapping) or set(entry) != {"block_id",
                                                                "effect"}:
                raise DiagnosticError("WRONG_TYPE", f"{cid}.block_effects[{j}]")
            got_blocks[str(entry["block_id"])] = _finite(
                entry["effect"], where=f"{cid}.{entry['block_id']}")
        if set(got_blocks) != set(expected):
            raise DiagnosticError("BLOCK_EFFECTS_INCOMPLETE", cid)
        for block_id, value in got_blocks.items():
            _close(value, expected[block_id], where=f"{cid}.{block_id}")
        checks.append({"contrast_id": cid, "n_blocks": reference["n_blocks"],
                       "effect": reference["effect"], "ok": True})

    delta = task.primary.delta
    threshold = output["threshold_counts"]
    if not isinstance(threshold, (list, tuple)) or len(threshold) != 1:
        raise DiagnosticError("THRESHOLD_COUNTS", "threshold_counts 必须恰一条")
    tc = threshold[0]
    if not isinstance(tc, Mapping) or set(tc) != {"contrast_id", "above_delta",
                                                  "below_minus_delta", "within"}:
        raise DiagnosticError("THRESHOLD_COUNTS", "字段集不符")
    if tc["contrast_id"] != task.primary.id:
        raise DiagnosticError("THRESHOLD_COUNTS", "contrast_id 不符")
    effects = task.primary.block_effects(raw)
    want = {
        "above_delta": sum(1 for v in effects.values() if Decimal(str(v)) > delta),
        "below_minus_delta": sum(1 for v in effects.values()
                                 if Decimal(str(v)) < -delta),
        "within": sum(1 for v in effects.values() if -delta <= Decimal(str(v)) <= delta),
    }
    for key, value in want.items():
        if int(tc[key]) != value:
            raise DiagnosticError("THRESHOLD_COUNTS",
                                  f"{key}={tc[key]} != {value}")

    summaries = _summaries(output["summaries"],
                           treat_preregistered=bool(registered_groups))
    comparability = output["phase_comparability"]
    if not isinstance(comparability, Mapping) or set(comparability) != {
            "explore_target", "confirm_target", "relation", "rationale"}:
        raise DiagnosticError("PHASE_COMPARABILITY", "字段集不符")
    if comparability["relation"] not in ("same_target", "different_target"):
        raise DiagnosticError("PHASE_COMPARABILITY", "relation 非法")
    if not str(comparability["rationale"]).strip():
        raise DiagnosticError("PHASE_COMPARABILITY", "缺 rationale")
    return DiagnosticVerdict(
        ok=True,
        codes=("DIAGNOSTICS_RECOMPUTED",),
        checks=tuple(checks) + tuple(
            {"summary": s["group_id"], "exploratory": s["exploratory"]}
            for s in summaries))


def public_schema() -> "dict[str, Any]":
    """候选可见的 diagnostics schema（与 ``validate_diagnostics`` 同源）。"""
    return {
        "schema": DIAG_SCHEMA,
        "keys": sorted(_KEYS),
        "phases": list(PHASES),
        "rules": {
            "coverage": "blocks 必须恰等于 trusted raw 的 block 集",
            "contrasts": "只允许任务登记的主对比；effect/lower/upper/n_blocks/逐 block 效应由宿主复算",
            "summaries": "任务未预登记分组时 preregistered 必须为 false（标 exploratory）",
            "threshold_counts": "预登记 δ 两侧计数，仅描述性，不构成新检验",
            "phase_comparability": "explore/confirm 目标总体异同必须声明 rationale",
        },
        "tolerance": {"abs": str(R.CLAIM_ABS_TOL), "rel": str(R.CLAIM_REL_TOL)},
    }
