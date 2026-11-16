"""RSI-G1 交付编译器：保真复制、拒绝面与隔离面（零 POST）。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import g1_delivery as DV
from tests.g1_cases import build_cases


def test_all_nine_cases_match_expectations():
    results = [DV.run_case(case) for case in build_cases()]
    assert len(results) == 9
    bad = [r for r in results if not r["ok"]]
    assert not bad, bad


def test_by_ref_and_verbatim_produce_identical_claim():
    cases = {c["case_id"]: c for c in build_cases()}
    v = DV.run_case(cases["c01_verbatim"])["actual"]["claim"]
    r = DV.run_case(cases["c01_by_ref"])["actual"]["claim"]
    for key in ("estimate", "interval", "metric_id", "contrast_id",
                "value_origin"):
        assert v[key] == r[key], key
    assert v["value_origin"]["estimate"] == "candidate_analysis"


def test_compiler_copies_wrong_candidate_value_verbatim():
    """c06：候选分析本身错时编译器保真复制，不代填 host 正确值。"""
    cases = {c["case_id"]: c for c in build_cases()}
    claim = DV.run_case(cases["c06_wrong_value_faithful"])["actual"]["claim"]
    assert claim["estimate"] == pytest.approx(-0.0042)
    assert claim["interval"]["lower"] == pytest.approx(-0.0151)
    assert claim["interval"]["upper"] == pytest.approx(0.0063)


def test_rounded_values_are_rejected_not_silently_overwritten():
    cases = {c["case_id"]: c for c in build_cases()}
    actual = DV.run_case(cases["c02_rounded"])["actual"]
    assert actual["code"] == "CLAIM_NUMERIC_MISMATCH"


def test_refuted_and_inconclusive_labels_are_not_structural_errors():
    """合法 REFUTED 走同一结构；不因非 SUPPORTED 被拒（Plan case 5）。"""
    cases = {c["case_id"]: c for c in build_cases()}
    claim = DV.run_case(cases["c05_refuted"])["actual"]["claim"]
    assert claim["status"] == "REFUTED"
    assert claim["direction"] == "decrease_gt"


def test_store_refuses_forbidden_and_traversal_paths():
    store = DV.MemoryArtifactStore(blobs={"analysis/output.json": b"{}"})
    with pytest.raises(DV.DeliveryError) as exc:
        store.read("private/analysis/A-confirm.raw.json")
    assert exc.value.code == "ARTIFACT_FORBIDDEN"
    with pytest.raises(DV.DeliveryError) as exc:
        store.read("analysis/../../etc/passwd")
    assert exc.value.code == "ARTIFACT_PATH"
    with pytest.raises(DV.DeliveryError) as exc:
        store.read("judge/report_A.json")
    assert exc.value.code == "ARTIFACT_FORBIDDEN"


def test_compiler_module_never_imports_judge_or_oracle():
    """静态依赖面：编译器不得够到 raw/judge/oracle（Plan G1-06）。"""
    src = Path(DV.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = [m for m in imported
                 if any(tok in m for tok in ("judge", "oracle", "closeout_r13",
                                              "gateway", "research_transport"))]
    assert forbidden == [], forbidden


def test_claim_carries_sample_scope_and_typed_evidence():
    """C2：编译器自动补 sample_scope 与 typed_locator（Plan §2 字段）。"""
    cases = {c["case_id"]: c for c in build_cases()}
    claim = DV.run_case(cases["c01_by_ref"])["actual"]["claim"]
    scope = claim["sample_scope"]
    assert scope["denominator"] == 2 and scope["dropped"] == []
    assert len(scope["run_ids_sha"]) == 64 and len(scope["block_ids_sha"]) == 64
    ev = claim["evidence"][0]
    assert set(ev) == {"artifact_id", "artifact_sha256", "json_pointer",
                       "field_sha256"}
    assert ev["field_sha256"] == claim["evidence"][0]["field_sha256"]
    assert claim["interval"]["level"] == 0.95
