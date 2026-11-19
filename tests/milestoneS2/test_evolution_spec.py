"""S2 EvolutionSpec 机检（AC-06/AC-09）：白名单 + AST 闭包 + 三越界负控。"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.hk import evolution as EV

_REPO = Path(__file__).resolve().parents[2]
_INCUMBENT_GUARDS = (_REPO / "orchestrator" / "hk" / "guards.py").read_text(
    encoding="utf-8")

_CANDIDATE_GUARDS = _INCUMBENT_GUARDS + '''

def g_report(ctx):
    """候选新增：六段报告结构自检。"""
    report = ctx.report_doc
    if report is None:
        raise GuardViolation("g_report", "REPORT_MISSING", "缺报告")
    missing = [k for k in ("methods", "main_results") if not report.get(k)]
    if missing:
        raise GuardViolation("g_report", "REPORT_INCOMPLETE", str(missing))


GUARDS["G-REPORT"] = g_report
'''

_TOOLKIT = '''"""候选 NC 设计助手（亚阈扰动 + 非退化自检）。"""


def propose_perturbation(base_value, relative=1e-3):
    """亚阈微扰：相对扰动 <= 1e-3（低于等效界）。"""
    return float(base_value) * (1.0 + relative)


def non_degenerate(values):
    return any(abs(float(v)) > 1e-15 for v in values)
'''

_TOUCHPOINTS = '''"""候选触点模板 v2。"""

TOUCHPOINT_INSTRUCTIONS_V2 = {
    "propose_plan": "设计 negative_control 并在 response.json 提名 base/sham",
    "submit_claim": "verdict 五值：NOT_DETECTED 与 EQUIVALENT 语义；附 report 六段",
    "submit_claim_repair": "补全 report 后重交 claim",
}
'''


def _package(**overrides):
    package = {
        "schema": EV.CANDIDATE_SCHEMA,
        "mechanism_hypothesis": (
            "报告缺段通过=K8 出口无结构守卫；NC 退化=缺少亚阈扰动设计助手"),
        "prediction_signature": {
            "dimensions": {"negative_control": "+",
                           "limitations_next_steps": "+",
                           "problem_hypothesis": "0"},
            "primary": "review_pass_rate 提升集中在 NC 与局限两维",
        },
        "files": [
            {"path": "orchestrator/hk/guards.py", "content": _CANDIDATE_GUARDS},
            {"path": "orchestrator/hk/nc_toolkit.py", "content": _TOOLKIT},
            {"path": "orchestrator/hk/touchpoints.py", "content": _TOUCHPOINTS},
        ],
    }
    package.update(overrides)
    return package


def test_valid_candidate_passes():
    report = EV.validate_candidate(_package(), incumbent_root=_REPO)
    assert report["validated"] is True
    assert report["files"] == ["orchestrator/hk/guards.py",
                               "orchestrator/hk/nc_toolkit.py",
                               "orchestrator/hk/touchpoints.py"]
    assert len(report["diff_sha256"]) == 64


def test_out_of_surface_frozen_file_rejected():
    """负控①：往冻结面塞一行改动必须被拒。"""
    package = _package()
    package["files"] = package["files"] + [
        {"path": "orchestrator/hk/verdicts.py", "content": "# 偷改词表\n"}]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "MUTATION_OUT_OF_SURFACE"


def test_out_of_surface_vocabulary_whitelist_rejected():
    """负控②：改词表白名单外的判据文件（report_contract）同样越界。"""
    package = _package()
    package["files"] = package["files"] + [
        {"path": "orchestrator/hk/report_contract.py", "content": "# x\n"}]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "MUTATION_OUT_OF_SURFACE"


def test_network_egress_rejected():
    """负控③：新增网络出口必须被拒。"""
    package = _package()
    package["files"] = [
        {"path": "orchestrator/hk/nc_toolkit.py",
         "content": "import requests\n\n\ndef f():\n    return requests.get('http://x')\n"},
    ]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "NETWORK_EGRESS"


def test_forbidden_dynamic_execution_rejected():
    package = _package()
    package["files"] = [
        {"path": "orchestrator/hk/nc_toolkit.py",
         "content": "def f(src):\n    return eval(src)\n"},
    ]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "FORBIDDEN_CALL"


def test_template_only_candidate_rejected():
    """ShadowGate 坏候选特征：只改提示词/模板、无机制实现。"""
    package = _package()
    package["files"] = [{"path": "orchestrator/hk/touchpoints.py",
                         "content": _TOUCHPOINTS}]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "NO_MECHANISM"


def test_guards_regression_rejected():
    """候选 guards.py 丢掉既有守卫 → 回归拒绝（只允许新增一个）。"""
    package = _package()
    package["files"] = [
        {"path": "orchestrator/hk/guards.py",
         "content": "GUARDS = {}\n\n\ndef g_report(ctx):\n    return None\n\n"
                    "GUARDS['G-REPORT'] = g_report\n"},
        {"path": "orchestrator/hk/nc_toolkit.py", "content": _TOOLKIT},
    ]
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "GUARD_REGRESSION"


def test_prediction_must_be_falsifiable():
    package = _package()
    package["prediction_signature"] = {
        "dimensions": {"negative_control": "+", "limitations_next_steps": "+"}}
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate(package, incumbent_root=_REPO)
    assert excinfo.value.code == "PREDICTION_NOT_FALSIFIABLE"


def test_incomplete_candidate_rejected():
    with pytest.raises(EV.EvolutionError) as excinfo:
        EV.validate_candidate({"schema": EV.CANDIDATE_SCHEMA})
    assert excinfo.value.code == "CANDIDATE_INCOMPLETE"
    package = _package(mechanism_hypothesis="")
    with pytest.raises(EV.EvolutionError):
        EV.validate_candidate(package, incumbent_root=_REPO)
