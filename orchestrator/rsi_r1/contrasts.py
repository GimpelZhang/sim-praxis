"""R1.3 线性对比（pairwise / difference-in-differences）契约与估计。

背景（R1.2 §16 复盘）：旧 ``ContrastSpec`` 只表达两条件配对差，无法把
「效应随工况改变」（effect modification）写成主估计量，导致确认集测到的是
平均效应、探索集测到的是单点效应，两阶段估计对象不同却未声明。R1.3 引入
**线性对比**作为单一估计量语言：

* ``pairwise_difference``：``t = +1·A − 1·B``（旧行为的等价表达）；
* ``difference_in_differences``：``t = +1·A − 1·B − 1·C + 1·D``
  （两因子 2×2，A/B 与 C/D 分别同一因子水平）。

纪律（Plan §3/§4）：

* 权重是十进制字面量，和必须为 0（平移不变），且至少一个正项、一个负项；
* ``metric`` 必须已在公开量表注册，``delta`` 必须等于量表固定值（候选不能扩界）；
* 估计单位恒为 **block**：先逐 block 求线性组合，再对 block 效应做
  percentile bootstrap（复用 ``statistics.estimate``，方法不变）；
* 缺失任一条件 block 不是"少一个样本"而是分母偷换 → ``BLOCK_INCOMPLETE``；
* 本模块只做数学，不读盘、不触网、不 import judge。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import statistics as ST

CONTRAST_SCHEMA = "simpraxis.rsi_r13.contrast.v1"

KIND_PAIRWISE = "pairwise_difference"
KIND_DID = "difference_in_differences"
KINDS = (KIND_PAIRWISE, KIND_DID)

_CONTRAST_KEYS = frozenset({"schema", "schema_version", "id", "kind", "metric",
                            "proposition", "delta", "terms"})


class ContrastError(ValueError):
    """对比定义/估计拒绝（带机判码）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def _decimal(value: Any, *, where: str) -> Decimal:
    if isinstance(value, bool):
        raise ContrastError("WRONG_TYPE", f"{where} 不接受布尔")
    if isinstance(value, float):
        raise ContrastError("FLOAT_INPUT", f"{where} 必须是字符串十进制，不接受 float")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ContrastError("WRONG_TYPE", f"{where}={value!r} 不是十进制数") from None
    if not out.is_finite():
        raise ContrastError("NONFINITE", f"{where}={value!r}")
    return out


@dataclass(frozen=True)
class ContrastV13:
    id: str
    kind: str
    metric: str
    proposition: str
    delta: Decimal
    #: condition_id → 权重（按 condition_id 排序存储，保证哈希稳定）
    terms: "tuple[tuple[str, Decimal], ...]"

    def to_dict(self) -> "dict[str, Any]":
        return {
            "schema": CONTRAST_SCHEMA,
            "schema_version": 1,
            "id": self.id,
            "kind": self.kind,
            "metric": self.metric,
            "proposition": self.proposition,
            "delta": str(self.delta),
            "terms": [{"condition_id": cid, "weight": str(w)}
                      for cid, w in self.terms],
        }

    @property
    def condition_ids(self) -> "tuple[str, ...]":
        return tuple(cid for cid, _ in self.terms)

    @property
    def weights(self) -> "Mapping[str, Decimal]":
        return {cid: w for cid, w in self.terms}

    @classmethod
    def from_dict(cls, doc: Any, *, where: str = "contrast") -> "ContrastV13":
        if not isinstance(doc, Mapping):
            raise ContrastError("WRONG_TYPE", f"{where} 必须是对象")
        unknown = sorted(set(doc) - _CONTRAST_KEYS)
        missing = sorted(_CONTRAST_KEYS - set(doc))
        if unknown or missing:
            raise ContrastError("KEY_MISMATCH",
                                f"{where} 未知 {unknown} 缺 {missing}")
        if doc["schema"] != CONTRAST_SCHEMA:
            raise ContrastError("SCHEMA_MISMATCH",
                                f"{where}.schema={doc['schema']!r}")
        if doc["schema_version"] != 1:
            raise ContrastError("SCHEMA_MISMATCH", f"{where}.schema_version")
        cid = str(doc["id"])
        if not cid or not all(ch.isalnum() or ch in "_-." for ch in cid):
            raise ContrastError("ID_INVALID", f"{where}.id={cid!r}")
        kind = doc["kind"]
        if kind not in KINDS:
            raise ContrastError("KIND_INVALID", f"{where}.kind={kind!r}")
        raw_terms = doc["terms"]
        if not isinstance(raw_terms, (list, tuple)) or len(raw_terms) < 2:
            raise ContrastError("TERMS_INVALID", f"{where}.terms 至少两项")
        terms: "list[tuple[str, Decimal]]" = []
        for i, term in enumerate(raw_terms):
            if not isinstance(term, Mapping) or set(term) != {"condition_id",
                                                              "weight"}:
                raise ContrastError(
                    "TERMS_INVALID", f"{where}.terms[{i}] 必须是 condition_id/weight")
            cond = str(term["condition_id"])
            if not cond:
                raise ContrastError("TERMS_INVALID", f"{where}.terms[{i}] 空 id")
            terms.append((cond, _decimal(term["weight"],
                                         where=f"{where}.terms[{i}].weight")))
        return cls(
            id=cid,
            kind=str(kind),
            metric=str(doc["metric"]),
            proposition=str(doc["proposition"]),
            delta=_decimal(doc["delta"], where=f"{where}.delta"),
            terms=tuple(sorted(terms)),
        )

    def validate(self) -> "ContrastV13":
        if len(set(self.condition_ids)) != len(self.condition_ids):
            raise ContrastError("TERMS_INVALID", "条件 id 重复")
        total = sum((w for _, w in self.terms), Decimal("0"))
        if total != 0:
            raise ContrastError("WEIGHTS_NOT_CENTERED",
                                f"权重和 {total} != 0（平移不变要求）")
        positives = [w for _, w in self.terms if w > 0]
        negatives = [w for _, w in self.terms if w < 0]
        if not positives or not negatives:
            raise ContrastError("WEIGHTS_DEGENERATE", "至少一个正项与一个负项")
        if any(w == 0 for _, w in self.terms):
            raise ContrastError("WEIGHTS_DEGENERATE", "不允许零权重项")
        if self.kind == KIND_PAIRWISE:
            if len(self.terms) != 2 or sorted(abs(w) for _, w in self.terms) != [
                    Decimal("1"), Decimal("1")]:
                raise ContrastError(
                    "TERMS_INVALID", "pairwise 必须是 (+1, -1) 两条件")
        if self.kind == KIND_DID:
            if len(self.terms) != 4 or sorted(abs(w) for _, w in self.terms) != [
                    Decimal("1")] * 4:
                raise ContrastError(
                    "TERMS_INVALID", "difference_in_differences 必须是四条件 ±1")
        try:
            fixed = R.metric_delta(self.metric)
        except KeyError:
            raise ContrastError("METRIC_UNREGISTERED",
                                f"metric={self.metric!r} 未在量表注册") from None
        if self.kind != KIND_PAIRWISE and self.metric.endswith(
                "_negative_equivalence"):
            raise ContrastError("METRIC_INVALID",
                                "阴性等效指标只能用于 pairwise 对照")
        if self.proposition not in R.PROPOSITIONS:
            raise ContrastError("PROPOSITION_INVALID",
                                f"proposition={self.proposition!r}")
        if self.proposition == "equivalent" and self.metric != (
                "displacement_x_m_negative_equivalence"):
            raise ContrastError(
                "METRIC_INVALID", "equivalent 主张只能用阴性等效指标")
        if self.delta != fixed:
            raise ContrastError(
                "DELTA_DRIFT", f"delta={self.delta} != 量表固定 {fixed}")
        return self

    def block_effects(self, values: Mapping[str, Mapping[str, Any]]
                      ) -> "dict[str, float]":
        """``block_id → {condition_id: metric}`` → ``block_id → 线性对比效应``。

        每个 block 必须覆盖本对比的全部条件；缺项即 ``BLOCK_INCOMPLETE``
        （不静默丢弃该 block，也不把缺失当 0）。
        """
        if not isinstance(values, Mapping) or not values:
            raise ContrastError("EMPTY", "没有 block 观测")
        required = set(self.condition_ids)
        out: "dict[str, float]" = {}
        for block_id in sorted(values):
            per_cond = values[block_id]
            if not isinstance(per_cond, Mapping):
                raise ContrastError("WRONG_TYPE", f"{block_id} 不是条件映射")
            missing = sorted(required - set(per_cond))
            if missing:
                raise ContrastError(
                    "BLOCK_INCOMPLETE",
                    f"{block_id} 缺条件 {missing}（分母不得偷换）")
            total = 0.0
            for cid, weight in self.terms:
                try:
                    value = float(per_cond[cid])
                except (TypeError, ValueError):
                    raise ContrastError(
                        "WRONG_TYPE", f"{block_id}.{cid} 不是实数") from None
                if value != value or value in (float("inf"), float("-inf")):
                    raise ContrastError("NONFINITE", f"{block_id}.{cid}={value}")
                total += float(weight) * value
            out[str(block_id)] = total
        return out

    def estimate(self, values: Mapping[str, Mapping[str, Any]], *,
                 level: Any = R.CI_PRIMARY, seed: Any = ST.STATISTICS_SEED
                 ) -> "dict[str, Any]":
        """逐 block 线性组合 + block 级 percentile bootstrap。"""
        effects = self.block_effects(values)
        est = ST.estimate(effects, level=level, seed=seed)
        est["contrast_id"] = self.id
        est["contrast_kind"] = self.kind
        est["metric"] = self.metric
        est["weights"] = {cid: str(w) for cid, w in self.terms}
        est["claim"] = ST.classify(self.proposition, self.delta,
                                   est["lower"], est["upper"])
        return est


def did_terms(*, treatment_high: str, treatment_low: str,
              control_high: str, control_low: str) -> "tuple[tuple[str, Decimal], ...]":
    """构造 2×2 交互项：``+T_high −T_low −C_high +C_low``。

    约定：``treatment_*`` 是因子 A 的两个水平，``control_*`` 是因子 A 的
    另一个水平；两对之间只差因子 B（由调用方在任务规格里保证）。
    """
    return (
        (control_low, Decimal("1")),
        (control_high, Decimal("-1")),
        (treatment_high, Decimal("1")),
        (treatment_low, Decimal("-1")),
    )


def public_schema() -> "dict[str, Any]":
    """公开给候选的对比 schema（与 ``from_dict`` 同源，不另写一套键）。"""
    return {
        "schema": CONTRAST_SCHEMA,
        "kinds": list(KINDS),
        "keys": sorted(_CONTRAST_KEYS),
        "term": {"condition_id": "str", "weight": "decimal-string"},
        "rules": {
            "weights_sum": "0",
            "pairwise": "two conditions ±1",
            "difference_in_differences": "four conditions ±1",
            "delta": "取自公开量表，不可扩界",
            "equivalence_metric": "displacement_x_m_negative_equivalence（仅 A 阴性控制）",
        },
        "method": ST.public_method_document() if hasattr(ST, "public_method_document")
        else {"note": "paired block bootstrap（见 statistics 模块）"},
    }


def estimate_series(contrast: ContrastV13,
                    values: Mapping[str, Mapping[str, Any]],
                    *, level: Any = R.CI_PRIMARY) -> "Sequence[float]":
    """逐 block 效应列表（供审查包逐 block 表直接使用）。"""
    return [contrast.block_effects(values)[b] for b in sorted(values)]
