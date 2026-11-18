"""S0-2：九类研究域 Contract（单一真值 + mismatches 负控）。"""
from __future__ import annotations

from pathlib import Path

from orchestrator.hk._physicalrsi import Contract
from orchestrator.hk import contracts as C

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_nine_contracts_constructible_with_design_shape():
    expected = {
        "experiment_spec": (("", "", "mujoco_box_slide", None, "json")),
        "study_plan": ("", "", "mujoco_box_slide", None, "json"),
        "analysis_program": ("", "", "", None, "py-src"),
        "batch_plan": ("", "", "mujoco_box_slide", (None, None), "int32"),
        "run_manifest": ("m", "world", "mujoco_box_slide", (None,), "jsonl"),
        "analysis_result": ("m", "world", "mujoco_box_slide", (None, 3), "float64"),
        "confirm_plan": ("", "", "mujoco_box_slide", None, "json"),
        "confirm_result": ("m", "world", "mujoco_box_slide", (None, 3), "float64"),
        "claim": ("", "", "", None, "json"),
    }
    assert set(C.ALL_CONTRACTS) == set(expected)
    for name, (unit, frame, embodiment, shape, dtype) in expected.items():
        contract = C.get(name)
        assert isinstance(contract, Contract)
        assert (contract.unit, contract.frame, contract.embodiment,
                contract.shape, contract.dtype) == (unit, frame, embodiment, shape, dtype)


def test_unknown_contract_name_fails_closed():
    try:
        C.get("experiment_spec_v2")
    except KeyError as exc:
        assert "未登记" in str(exc)
    else:  # pragma: no cover - 走到这里即失败
        raise AssertionError("未登记 Contract 名必须拒绝")


def test_mismatches_hits_unit_error():
    hits = C.mismatches(C.RUN_MANIFEST,
                        Contract(name="run_manifest", unit="s"))
    assert any(hit.startswith("unit:") for hit in hits)


def test_accepts_same_name_and_unit():
    assert C.RUN_MANIFEST.accepts(C.RUN_MANIFEST)
    consumer = Contract(name="run_manifest")  # 未声明的约束不校验
    assert consumer.accepts(C.RUN_MANIFEST)


def test_mismatches_hits_shape_rank_and_wildcard():
    rank = C.mismatches(C.RUN_MANIFEST,
                        Contract(name="run_manifest", shape=(None, 3)))
    assert any("rank" in hit for hit in rank)
    wildcard = C.mismatches(Contract(name="run_manifest", shape=(None,)),
                            Contract(name="run_manifest", shape=(3,)))
    assert any("wildcard" in hit for hit in wildcard)


def test_single_contract_source_in_hk():
    """S0-2 完成判据：契约字面量只允许出现在 contracts.py / _physicalrsi.py。"""
    offenders = []
    for path in sorted((REPO_ROOT / "orchestrator" / "hk").glob("*.py")):
        if path.name in ("contracts.py", "_physicalrsi.py"):
            continue
        if "Contract(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []
