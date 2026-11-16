"""RSI-G2 独立 scope / tuple / semantic 校验器（零 POST / 零物理）。

Plan `~/文档/RSI_G2_Plan_detailed.md` §2.2/§2.3。与旧 ``g1_probe`` **互不改写**：
G1 的 C2/C6 旧报告与分数不可重判；本模块只对 **G2 新 fixture** 生效，且：

* 每个 fixture 必须先声明 ``hypothesis_id/estimand_id/metric_id/proposition/
  delta/unit/target_population/aggregation/weighting/block_to_contrast_formula/
  censoring_rule/coverage``，缺一即 ``CASE_INVALID``（不运行评分）；
* ``semantic_label``、``target_population/scope``、``evidence_coverage``、
  ``binding/numeric`` **分开**判定；标签正确但 scope 错 ⇒ ``SCOPE_CONTRACT_MISMATCH``，
  不升级为「模型语义失败」；
* ``MATCH`` 永远不等于 ``science_status=SUPPORTED``——science 状态只来自
  fixture 的**预登记**期望，且 scope 未定义时为 ``SCIENCE_NOT_MEASURED``。

本模块不读 raw/reference：输入只有公开合同与候选回复。
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

from orchestrator.rsi_r1 import g2_contract as G2

CASE_SCHEMA = G2.CASE_SCHEMA
ABS_TOL = 1e-8
REL_TOL = 1e-6

#: fixture 必须公开声明的字段（Plan §2.3；缺项 ⇒ CASE_INVALID）。
DECLARED_FIELDS = (
    "hypothesis_id", "estimand_id", "metric_id", "proposition", "delta",
    "unit", "target_population", "aggregation", "weighting",
    "block_to_contrast_formula", "censoring_rule", "coverage",
)
CLAIM_FIELDS = (
    "hypothesis_id", "estimand_id", "metric_id", "proposition", "delta",
    "unit", "target_population", "aggregation", "weighting", "coverage",
    "semantic_label", "result_ref",
)

AXIS_NAMES = ("parse_ok", "binding_valid", "numeric_valid", "tuple_match",
              "scope_valid", "semantic_label_valid",
              "evidence_coverage_valid")


def canonical(obj: Any) -> bytes:
    return G2.canonical(obj)


def sha(obj: Any) -> str:
    return G2.sha_bytes(canonical(obj))


def tolerance(reference: float) -> float:
    return max(ABS_TOL, REL_TOL * abs(float(reference)))


def close(a: Any, b: Any) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(fa) and math.isfinite(fb)):
        return False
    return abs(fa - fb) <= tolerance(fb)


def recompute(values: Sequence[float], aggregation: str) -> float:
    """block → contrast 的**公开公式**（纯函数；不认识聚合即拒）。"""
    numbers = [float(v) for v in values]
    if not numbers or not all(math.isfinite(v) for v in numbers):
        raise G2.G2Error("CASE_INVALID", "block 值非有限/为空")
    if aggregation == "mean":
        return sum(numbers) / len(numbers)
    if aggregation == "paired_mean":
        return sum(numbers) / len(numbers)
    if aggregation == "median":
        ordered = sorted(numbers)
        mid = len(ordered) // 2
        return (ordered[mid] if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2.0)
    raise G2.G2Error("CASE_INVALID", f"未登记聚合 {aggregation!r}")


def _resolve_pointer(doc: Any, pointer: str) -> Any:
    if pointer == "":
        return doc
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise G2.G2Error("BINDING_INVALID", pointer)
    node = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise G2.G2Error("BINDING_INVALID", pointer)
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                raise G2.G2Error("BINDING_INVALID", pointer) from None
        else:
            raise G2.G2Error("BINDING_INVALID", pointer)
    return node


# ---------------------------------------------------------------------------
# fixture 校验（先声明后评分）
# ---------------------------------------------------------------------------

def validate_case(case: Mapping[str, Any]) -> "list[str]":
    """返回问题列表；非空 ⇒ CASE_INVALID（不评分）。"""
    problems: "list[str]" = []
    if not isinstance(case, Mapping):
        return ["case 必须是对象"]
    if case.get("schema") != CASE_SCHEMA or case.get("schema_version") != 1:
        problems.append("schema")
    for key in ("case_id", "version", "public", "expected"):
        if key not in case:
            problems.append(f"缺 {key}")
    public = case.get("public")
    if not isinstance(public, Mapping):
        return problems + ["public 必须是对象"]
    contract_doc = public.get("scope_contract")
    if not isinstance(contract_doc, Mapping):
        return problems + ["public.scope_contract 缺失"]
    declared = {key: contract_doc.get(key) for key in DECLARED_FIELDS}
    missing = [key for key, value in declared.items() if value in (None, "")]
    if missing:
        return problems + [f"未声明 {','.join(missing)}"]
    try:
        contract = G2.ScopeContractV1.from_dict(contract_doc)
    except G2.G2Error as exc:
        return problems + [f"scope_contract:{exc.code}"]
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        return problems + ["expected 必须是对象"]
    want_sha = expected.get("contract_sha256")
    if want_sha is not None and str(want_sha) != contract.sha256:
        problems.append("expected 绑定的 contract sha 与 public 不一致")
    for key in ("contrast_id", "semantic_label", "value"):
        if key not in expected:
            problems.append(f"expected 缺 {key}")
    if expected.get("semantic_label") not in contract.allowed_semantic_labels:
        problems.append("expected.semantic_label 不在公开允许集合")
    analysis = public.get("analysis")
    if not isinstance(analysis, Mapping) or not isinstance(
            analysis.get("doc"), Mapping):
        problems.append("public.analysis.doc 缺失")
    else:
        contrasts = analysis["doc"].get("contrasts")
        if not isinstance(contrasts, list) or not contrasts:
            problems.append("analysis.contrasts 为空")
        else:
            ids = [str(c.get("contrast_id")) for c in contrasts
                   if isinstance(c, Mapping)]
            if expected.get("contrast_id") not in ids:
                problems.append("expected.contrast_id 不在 analysis 中")
    coverage = public.get("coverage")
    if not isinstance(coverage, Mapping):
        problems.append("public.coverage 缺失")
    else:
        mode = coverage.get("mode")
        if mode not in G2.COVERAGES:
            problems.append("coverage.mode 非法")
        if mode == "observed_blocks":
            covered = [str(b) for b in (coverage.get("covered_block_ids") or [])]
            denominator = coverage.get("denominator")
            if not isinstance(denominator, int) or denominator <= 0:
                problems.append("coverage.denominator 非法")
            elif len(covered) != denominator:
                problems.append("coverage 覆盖数与分母不符")
    return problems


# ---------------------------------------------------------------------------
# 评分（轴分离）
# ---------------------------------------------------------------------------

def _tuple_fields(contract: G2.ScopeContractV1) -> "dict[str, Any]":
    return {"hypothesis_id": contract.hypothesis_id,
            "estimand_id": contract.estimand_id,
            "metric_id": contract.metric_id,
            "proposition": contract.proposition,
            "delta": contract.delta, "unit": contract.unit}


def _scope_fields(contract: G2.ScopeContractV1) -> "dict[str, Any]":
    return {"target_population": contract.target_population,
            "aggregation": contract.aggregation,
            "weighting": contract.weighting,
            "coverage": contract.coverage}


def score_case(case: Mapping[str, Any],
               claim: Mapping[str, Any]) -> "dict[str, Any]":
    """对一个 fixture + 一条候选主张给出**分离轴**与阻断码。"""
    problems = validate_case(case)
    out: "dict[str, Any]" = {name: False for name in AXIS_NAMES}
    out.update({"case_id": str(case.get("case_id")), "codes": [],
                "science_status": "NOT_MEASURED",
                "classification": "CASE_INVALID"})
    if problems:
        out["codes"] = ["CASE_INVALID"] + problems[:8]
        return out
    contract = G2.ScopeContractV1.from_dict(case["public"]["scope_contract"])
    expected = case["expected"]
    if not isinstance(claim, Mapping):
        out["codes"] = ["CLAIM_SHAPE"]
        out["classification"] = "MECHANICAL_FAIL"
        return out
    missing = [key for key in CLAIM_FIELDS if key not in claim]
    if missing:
        out["codes"] = ["CLAIM_MISSING:" + ",".join(missing)]
        out["classification"] = "MECHANICAL_FAIL"
        return out
    out["parse_ok"] = True

    # 1) 绑定：result_ref 必须指向公开 analysis 里的 contrast 对象。
    ref = claim.get("result_ref")
    try:
        if not isinstance(ref, Mapping):
            raise G2.G2Error("BINDING_INVALID", "result_ref 必须是对象")
        if str(ref.get("artifact_id")) != "analysis/output.json":
            raise G2.G2Error("BINDING_INVALID", "artifact_id")
        node = _resolve_pointer(case["public"]["analysis"]["doc"],
                                str(ref.get("json_pointer")))
        if not isinstance(node, Mapping) or "estimate" not in node:
            raise G2.G2Error("BINDING_INVALID", "pointer 未指向 contrast")
        out["binding_valid"] = True
    except G2.G2Error as exc:
        out["codes"].append(exc.code if exc.code != "BINDING_INVALID"
                            else "BINDING_INVALID")
        node = None
    if out["binding_valid"]:
        out["bound_contrast_id"] = str(node.get("contrast_id"))
        numeric = True
        if "estimate" in claim:
            numeric = all(close(claim.get(k), node.get(k))
                          for k in ("estimate", "lower", "upper"))
        out["numeric_valid"] = bool(numeric)
        if not numeric:
            out["codes"].append("NUMERIC_MISMATCH")

    # 2) tuple：身份字段逐项与公开合同比较。
    tuple_fields = _tuple_fields(contract)
    tuple_bad = [key for key, value in tuple_fields.items()
                 if not _same(claim.get(key), value)]
    out["tuple_match"] = not tuple_bad
    if tuple_bad:
        out["codes"].append("TUPLE_MISMATCH:" + ",".join(tuple_bad))

    # 3) scope：枚举与值都与公开合同一致（与 semantic label 独立）。
    scope_fields = _scope_fields(contract)
    scope_bad = [key for key, value in scope_fields.items()
                 if str(claim.get(key)) != str(value)]
    out["scope_valid"] = not scope_bad
    if scope_bad:
        out["codes"].append("SCOPE_MISMATCH:" + ",".join(scope_bad))

    # 4) semantic label：只判是否在允许集合 + 是否等于预登记期望。
    label = str(claim.get("semantic_label"))
    out["semantic_label"] = label
    label_allowed = label in contract.allowed_semantic_labels
    out["semantic_label_valid"] = bool(label_allowed)
    if not label_allowed:
        out["codes"].append("SEMANTIC_LABEL_INVALID")
    out["semantic_match"] = label == str(expected["semantic_label"])
    if label_allowed and not out["semantic_match"]:
        out["codes"].append("SEMANTIC_MISMATCH")

    # 5) evidence coverage：覆盖集合必须满足公开 coverage 声明。
    coverage = case["public"]["coverage"]
    claimed = str(claim.get("coverage"))
    if coverage.get("mode") == "observed_blocks":
        want = sorted(str(b) for b in (coverage.get("covered_block_ids") or []))
        got = sorted(str(b) for b in (claim.get("covered_block_ids") or []))
        out["evidence_coverage_valid"] = (claimed == "observed_blocks"
                                          and got == want)
    else:
        out["evidence_coverage_valid"] = claimed == str(coverage.get("mode"))
    if not out["evidence_coverage_valid"]:
        out["codes"].append("EVIDENCE_COVERAGE_MISMATCH")

    # 6) 分类：scope 错 ≠ 语义错；MATCH ≠ SUPPORTED。
    out["science_status"] = _science_status(label, expected)
    out["classification"] = _classify(out, expected)
    out["science_assessment"] = G2.science_assessment(
        binding_valid=out["binding_valid"],
        numeric_valid=out["numeric_valid"],
        tuple_match=out["tuple_match"], scope_valid=out["scope_valid"],
        evidence_coverage_valid=out["evidence_coverage_valid"],
        semantic_label=label, target_population=str(claim.get("target_population")),
        estimand_id=str(claim.get("estimand_id")),
        metric_id=str(claim.get("metric_id")),
        proposition_id=str(claim.get("proposition")),
        delta=claim.get("delta") if isinstance(claim.get("delta"), (int, float))
        else None)
    return out


def _same(got: Any, want: Any) -> bool:
    if isinstance(want, float):
        return close(got, want)
    return str(got) == str(want)


def _science_status(label: str, expected: Mapping[str, Any]) -> str:
    if "science_status" in expected:
        return str(expected["science_status"])
    return {"SUPPORTED": "SUPPORTED", "REFUTED": "REFUTED",
            "INCONCLUSIVE": "INCONCLUSIVE"}.get(label, "NOT_MEASURED")


def _classify(out: Mapping[str, Any], expected: Mapping[str, Any]) -> str:
    if not out["parse_ok"] or not out["binding_valid"]:
        return "MECHANICAL_FAIL"
    if not out["tuple_match"]:
        return "TUPLE_MISMATCH"
    if not out["numeric_valid"]:
        return "NUMERIC_MISMATCH"
    label_ok = bool(out["semantic_match"]) and bool(out["semantic_label_valid"])
    if label_ok and not out["scope_valid"]:
        return "SCOPE_CONTRACT_MISMATCH"
    if label_ok and not out["evidence_coverage_valid"]:
        return "EVIDENCE_COVERAGE_MISMATCH"
    if out["scope_valid"] and not label_ok:
        return "SEMANTIC_MISMATCH"
    if label_ok and out["scope_valid"] and out["evidence_coverage_valid"]:
        return "OK"
    return "MIXED_FAIL"


# ---------------------------------------------------------------------------
# G2 受控 fixture（全部合成；expected 只引用公开合同）
# ---------------------------------------------------------------------------

def _blocks(ids: Sequence[str], values: Sequence[float]) -> "list[dict[str, Any]]":
    return [{"block_id": bid, "value": float(value)}
            for bid, value in zip(ids, values)]


def _contract(*, case_id: str, semantic: str, target: str = "registered_distribution",
              aggregation: str = "mean", weighting: str = "equal",
              coverage: str = "observed_blocks", delta: float = 0.001,
              proposition: str = "increase_gt",
              metric: str = "displacement_x_m",
              formula_notes: str = "每配对 block 效应 x；等权 mean=sum(x)/n") -> "dict[str, Any]":
    return {
        "schema": G2.SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
        "contract_id": f"g2-{case_id}", "task_id": "rsi-g2-synthetic",
        "task_spec_sha256": "a" * 64,
        "hypothesis_id": "h_interaction", "estimand_id": "est_main",
        "metric_id": metric, "proposition": proposition, "delta": delta,
        "unit": "m", "target_population": target, "aggregation": aggregation,
        "weighting": weighting, "block_to_contrast_formula": aggregation,
        "formula_notes": formula_notes,
        "censoring_rule": "非有限/缺失 block 记 dropped，不补值",
        "coverage": coverage, "interval_level": 0.9,
        "allowed_semantic_labels": list(G2.SEMANTIC_LABELS),
        "notes": f"synthetic fixture {case_id}; expected 由公开合同推导",
    }


def _case(*, case_id: str, question: str, contract: "dict[str, Any]",
          contrast_id: str, blocks: "list[dict[str, Any]]",
          estimate: float, lower: float, upper: float,
          status: str = "SUPPORTED", semantic: str = "SUPPORTED",
          extra_contrasts: "list[dict[str, Any]] | None" = None,
          coverage_mode: str = "observed_blocks") -> "dict[str, Any]":
    values = [b["value"] for b in blocks]
    agg = contract["aggregation"]
    recomputed = recompute(values, agg)
    contrasts = [{"contrast_id": contrast_id,
                  "metric_id": contract["metric_id"],
                  "proposition": contract["proposition"],
                  "estimate": estimate, "lower": lower, "upper": upper,
                  "status": status, "n_blocks": len(blocks),
                  "block_values": values, "recomputed": recomputed}]
    contrasts.extend(extra_contrasts or [])
    covered = [b["block_id"] for b in blocks]
    return {
        "schema": CASE_SCHEMA, "schema_version": 1,
        "case_id": case_id, "version": 1, "maintainer_generated": True,
        "public": {
            "question": question,
            "scope_contract": contract,
            "analysis": {"artifact_id": "analysis/output.json",
                         "doc": {"schema": "simpraxis.rsi_g2.case_analysis.v1",
                                 "schema_version": 1, "contrasts": contrasts}},
            "coverage": {"mode": coverage_mode,
                         "covered_block_ids": covered,
                         "denominator": len(blocks)},
            "semantic_options": list(G2.SEMANTIC_LABELS),
        },
        "expected": {
            "contrast_id": contrast_id, "semantic_label": semantic,
            "contract_sha256": G2.ScopeContractV1.from_dict(contract).sha256,
            "value": {"estimate": estimate, "lower": lower, "upper": upper},
        },
    }


def build_cases() -> "list[dict[str, Any]]":
    """G2 版本化 fixture（与 G1 的 C1–C6 分开；旧 G1 不重判）。"""
    cases: "list[dict[str, Any]]" = []
    cases.append(_case(
        case_id="g2c01_supported_mean",
        question="平均效应是否超过 δ=0.001（注册分布，等权 mean）？",
        contract=_contract(case_id="g2c01_supported_mean", semantic="SUPPORTED"),
        contrast_id="mean_effect",
        blocks=_blocks(["b0", "b1", "b2", "b3"], [0.06, 0.04, 0.0005, 0.09]),
        estimate=0.047625, lower=0.01, upper=0.09))
    cases.append(_case(
        case_id="g2c02_refuted",
        question="预登记的正向假设是否被数据反驳？",
        contract=_contract(case_id="g2c02_refuted", semantic="REFUTED",
                           proposition="increase_gt"),
        contrast_id="refuted_effect",
        blocks=_blocks(["b0", "b1", "b2", "b3"], [-0.2, -0.19, -0.21, -0.2]),
        estimate=-0.2, lower=-0.3, upper=-0.1, status="REFUTED",
        semantic="REFUTED"))
    cases.append(_case(
        case_id="g2c03_inconclusive",
        question="区间跨 δ 时该说什么？",
        contract=_contract(case_id="g2c03_inconclusive", semantic="INCONCLUSIVE"),
        contrast_id="crossing_effect",
        blocks=_blocks(["b0", "b1", "b2", "b3"], [0.001, -0.002, 0.0, 0.003]),
        estimate=0.0005, lower=-0.02, upper=0.021,
        status="INCONCLUSIVE", semantic="INCONCLUSIVE"))
    cases.append(_case(
        case_id="g2c04_fixed_point",
        question="固定 pilot 单点的结论范围？",
        contract=_contract(case_id="g2c04_fixed_point", semantic="SUPPORTED",
                           target="fixed_point", coverage="fixed_point"),
        contrast_id="pilot_effect",
        blocks=_blocks(["p0"], [0.03]),
        estimate=0.03, lower=0.01, upper=0.05, coverage_mode="fixed_point"))
    cases.append(_case(
        case_id="g2c05_secondary_contrast",
        question="题目指定 contrast = secondary_effect；请引用它。",
        contract=_contract(case_id="g2c05_secondary_contrast",
                           semantic="SUPPORTED"),
        contrast_id="secondary_effect",
        blocks=_blocks(["b0", "b1", "b2", "b3"], [0.07, 0.06, 0.08, 0.07]),
        estimate=0.07, lower=0.02, upper=0.12,
        extra_contrasts=[{"contrast_id": "primary_effect",
                          "metric_id": "displacement_x_m",
                          "proposition": "increase_gt",
                          "estimate": 0.01, "lower": 0.005, "upper": 0.02,
                          "status": "SUPPORTED", "n_blocks": 4,
                          "block_values": [0.01] * 4}]))
    cases.append(_case(
        case_id="g2c06_inconsistent",
        question="分析结论与逐 block 数据是否一致？",
        contract=_contract(case_id="g2c06_inconsistent",
                           semantic="INCONSISTENT_EVIDENCE"),
        contrast_id="inconsistent_effect",
        blocks=_blocks(["b0", "b1", "b2", "b3"], [-0.05, -0.04, -0.06, -0.05]),
        estimate=0.05, lower=0.01, upper=0.09,
        semantic="INCONSISTENT_EVIDENCE"))
    return cases


def expected_claim(case: Mapping[str, Any]) -> "dict[str, Any]":
    """由**公开合同**推导一条理想主张（测试/离线演示用；maintainer_generated）。"""
    contract = G2.ScopeContractV1.from_dict(case["public"]["scope_contract"])
    want = str(case["expected"]["contrast_id"])
    contrasts = case["public"]["analysis"]["doc"]["contrasts"]
    index = next(i for i, c in enumerate(contrasts)
                 if str(c.get("contrast_id")) == want)
    node = contrasts[index]
    coverage = case["public"]["coverage"]
    return {
        "hypothesis_id": contract.hypothesis_id,
        "estimand_id": contract.estimand_id,
        "metric_id": contract.metric_id,
        "proposition": contract.proposition,
        "delta": contract.delta,
        "unit": contract.unit,
        "target_population": contract.target_population,
        "aggregation": contract.aggregation,
        "weighting": contract.weighting,
        "coverage": contract.coverage,
        "covered_block_ids": [str(b) for b in
                              (coverage.get("covered_block_ids") or [])],
        "semantic_label": str(case["expected"]["semantic_label"]),
        "estimate": node.get("estimate"),
        "lower": node.get("lower"),
        "upper": node.get("upper"),
        "result_ref": {"artifact_id": "analysis/output.json",
                       "json_pointer": f"/contrasts/{index}"},
        "maintainer_generated": True,
    }


def build_case_file(*, claims: "Mapping[str, Mapping[str, Any]] | None" = None
                    ) -> "dict[str, Any]":
    """版本化 case 文件：fixtures + 可选候选主张（用于离线评分）。"""
    return {"schema": CASE_SCHEMA + ".batch", "schema_version": 1,
            "cases": build_cases(),
            "claims": {str(k): dict(v) for k, v in (claims or {}).items()}}


def score_batch(doc: Mapping[str, Any]) -> "dict[str, Any]":
    """批量评分；缺 claim 的 case 记 ``CLAIM_MISSING``（不是 vacuous pass）。"""
    cases = list(doc.get("cases") or [])
    claims = doc.get("claims") or {}
    results: "list[dict[str, Any]]" = []
    for case in cases:
        cid = str(case.get("case_id"))
        claim = claims.get(cid)
        if claim is None:
            results.append({"case_id": cid, "classification": "CLAIM_MISSING",
                            "codes": ["CLAIM_MISSING"]})
            continue
        results.append(score_case(case, claim))
    ok = bool(results) and all(r["classification"] == "OK" for r in results)
    scope_mismatch = [r["case_id"] for r in results
                      if r["classification"] == "SCOPE_CONTRACT_MISMATCH"]
    semantic_mismatch = [r["case_id"] for r in results
                         if r["classification"] == "SEMANTIC_MISMATCH"]
    return {"schema": "simpraxis.rsi_g2.scope_report.v1", "schema_version": 1,
            "n_cases": len(results), "ok": ok, "results": results,
            "scope_mismatch": scope_mismatch,
            "semantic_mismatch": semantic_mismatch,
            "note": "scope 错与语义错分轴；MATCH 不等于 SUPPORTED"}


def public_schema() -> "dict[str, Any]":
    """公开 scope 合同 schema（renderer/evaluator 同读一份）。"""
    return {
        "schema": G2.SCOPE_CONTRACT_SCHEMA,
        "keys": sorted(G2.SCOPE_KEYS),
        "enums": {"target_population": list(G2.TARGET_POPULATIONS),
                  "aggregation": list(G2.AGGREGATIONS),
                  "weighting": list(G2.WEIGHTINGS),
                  "coverage": list(G2.COVERAGES),
                  "semantic_labels": list(G2.SEMANTIC_LABELS)},
        "rules": {
            "formula": "block_to_contrast_formula 必须等于 aggregation",
            "expected": "expected 只能引用公开合同与其 sha；不得反向生成规则",
            "scope_vs_semantic": "scope 错 ⇒ SCOPE_CONTRACT_MISMATCH，不是语义失败",
            "science": "science_status 只来自 fixture 预登记，不由 MATCH 晋升",
        },
        "serialization": "UTF-8 json.dumps(sort_keys=True,separators=(',',':'),"
                         "ensure_ascii=False,allow_nan=False)",
    }
