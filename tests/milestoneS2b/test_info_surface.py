"""S2b 回归测试：F1 信息面 / F2 交叉校验 / F3 请求面清单 / K4 瘦身。

四组测试对应计划 §2 F3 的 AC-01..04：
① K8 request.json 必含 nc_evidence + 全字段 results（缺→红）
② G-REPORT 交叉校验正/负控（自相矛盾→拒、如实引用→过）
③ REQUIRED_INPUT_KEYS 四触点各一缺键负控
④ K4 触点请求瘦身回归（run 摘要代替全量 manifest）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.hk.guards import GuardCtx, GuardViolation, g_report
from orchestrator.hk.report_contract import crosscheck_nc_section
from orchestrator.hk.skills import SkillError, build_skills
from orchestrator.hk.touchpoints import (HeadlessActor, REQUIRED_INPUT_KEYS,
                                         TouchpointError, TouchpointRecord)


# ---------------------------------------------------------------------------
# 共用 fixture
# ---------------------------------------------------------------------------

class _FakeRunner:
    """不触网的假 runner：返回预设 response。"""

    def __init__(self, response: dict | None = None):
        self.response = response or {}
        self.requests: list[dict] = []

    def run(self, *, touchpoint, workspace, request, host_label=""):
        self.requests.append({"touchpoint": touchpoint, "request": request})
        ws = Path(workspace)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "request.json").write_text(json.dumps(request, default=str))
        (ws / "response.json").write_text(json.dumps(self.response))
        t = ws / "transcript.jsonl"
        t.write_text('{"type":"system","session_id":"s","model":"m"}\n')
        return TouchpointRecord(
            touchpoint=touchpoint, session_key="k", claude_session_id="s",
            model="m", workspace=ws, transcript=t, usage={},
            returncode=0, elapsed_s=0, started_at_utc="x",
            response_path=ws / "response.json")


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "test_run"
    d.mkdir(parents=True)
    return d


def _full_contrast_row(**overrides):
    row = {"id": "c1", "metric_id": "m1", "proposition": "equivalent",
           "delta": "0.01", "level": "0.90", "estimate": 0.001,
           "ci_low": -0.002, "ci_high": 0.004, "n_blocks": 32,
           "claim": "EQUIVALENT"}
    row.update(overrides)
    return row


def _nc_evidence(**overrides):
    ev = {"registered": True, "base": "ctrl", "sham": "sham",
          "non_degenerate": True, "sub_threshold": True,
          "sensitivity_calibrated": True,
          "estimate": 0.0, "ci_low": -0.001, "ci_high": 0.001}
    ev.update(overrides)
    return ev


# ---------------------------------------------------------------------------
# ① AC-01：K8 request 必含 nc_evidence + 全字段 results
# ---------------------------------------------------------------------------

class TestK8InfoSurface:
    """k8_submit_claim 的结构拒绝：缺 negative_control / 缺全字段行 → SkillError。"""

    def _make_skills(self, actor_response=None):
        def actor(name, contract, value):
            return actor_response or {"verdict": "EQUIVALENT", "statement": "s",
                                      "contrasts": []}
        return build_skills(actor=actor, batch_runner=lambda p, c=None: {},
                            analyzer=lambda m, c=None: {},
                            confirmer=lambda p, c=None: {})

    def test_k8_rejects_missing_negative_control(self):
        skills = self._make_skills()
        value = {"contrasts": [_full_contrast_row()]}  # 无 negative_control
        with pytest.raises(SkillError) as excinfo:
            skills["K8"].execute(value, {})
        assert "K8_INFO_SURFACE_INCOMPLETE" in str(excinfo.value)
        assert "negative_control" in str(excinfo.value)

    def test_k8_rejects_missing_full_fields(self):
        skills = self._make_skills()
        row = {"id": "c1", "estimate": 0.1}  # 缺 metric_id/proposition/delta 等
        value = {"negative_control": _nc_evidence(), "contrasts": [row]}
        with pytest.raises(SkillError) as excinfo:
            skills["K8"].execute(value, {})
        assert "K8_INFO_SURFACE_INCOMPLETE" in str(excinfo.value)

    def test_k8_accepts_full_surface(self):
        captured = {}

        def actor(name, contract, value):
            captured.update(value)
            return {"verdict": "EQUIVALENT", "statement": "s",
                    "contrasts": [_full_contrast_row()]}
        skills = build_skills(actor=actor, batch_runner=lambda p, c=None: {},
                              analyzer=lambda m, c=None: {},
                              confirmer=lambda p, c=None: {})
        value = {"negative_control": _nc_evidence(),
                 "contrasts": [_full_contrast_row()]}
        result = skills["K8"].execute(value, {})
        assert result["verdict"] == "EQUIVALENT"
        # F1.2：请求面必须有 nc_evidence + results 别名
        assert "nc_evidence" in captured
        assert "results" in captured
        assert captured["nc_evidence"] == _nc_evidence()


# ---------------------------------------------------------------------------
# ② AC-02：G-REPORT NC 交叉校验正/负控
# ---------------------------------------------------------------------------

class TestReportCrosscheck:
    def test_crosscheck_passes_when_faithful(self):
        report = {"negative_control_handling":
                  "base=ctrl sham=sham；非退化（max|v|=3.7e-5）；"
                  "亚阈（|est|=4.9e-6<0.001）；灵敏度校准通过"}
        problems = crosscheck_nc_section(report, _nc_evidence())
        assert problems == []

    def test_crosscheck_rejects_contradiction(self):
        report = {"negative_control_handling":
                  "NC 未注册/未验证，无法确认管线有效性"}
        problems = crosscheck_nc_section(report, _nc_evidence())
        assert len(problems) == 1
        assert "G-REPORT_NC_MISMATCH" in problems[0]

    def test_crosscheck_rejects_fabrication(self):
        report = {"negative_control_handling":
                  "base=ctrl sham=sham；非退化、亚阈、灵敏度校准三连通过"}
        problems = crosscheck_nc_section(report, None)
        assert len(problems) == 1
        assert "编造" in problems[0] or "缺失" in problems[0]

    def test_g_report_raises_on_nc_mismatch(self):
        report = {
            "question_hypothesis": "测试问题与可反驳假设方向",
            "methods": "条件矩阵与配对区块设计",
            "main_results": "主结果 estimate/CI 逐对比",
            "heterogeneity_scope": "结论覆盖范围与外推边界",
            "limitations_next_steps": "局限与后续改进方向",
            # 含锚词（非退化）但内容与 nc_evidence 矛盾（声称未验证）
            "negative_control_handling":
                "NC 注册了但非退化、亚阈、灵敏度校准均未验证，无法确认",
        }
        ctx = GuardCtx(stage="K8", report_doc=report, nc_evidence=_nc_evidence())
        with pytest.raises(GuardViolation) as excinfo:
            g_report(ctx)
        assert excinfo.value.code == "G-REPORT_NC_MISMATCH"

    def test_g_report_passes_when_faithful(self):
        report = {
            "question_hypothesis": "测试问题与可反驳假设方向",
            "methods": "条件矩阵与配对区块设计",
            "main_results": "主结果 estimate/CI 逐对比",
            "heterogeneity_scope": "结论覆盖范围与外推边界",
            "limitations_next_steps": "局限与后续改进方向",
            "negative_control_handling":
                "base=ctrl sham=sham；非退化、亚阈、灵敏度校准三连通过",
        }
        ctx = GuardCtx(stage="K8", report_doc=report, nc_evidence=_nc_evidence())
        g_report(ctx)  # 不抛 = 通过

    def test_g_report_rejects_missing_sections(self):
        ctx = GuardCtx(stage="K8", report_doc={"question_hypothesis": "x"})
        with pytest.raises(GuardViolation) as excinfo:
            g_report(ctx)
        assert excinfo.value.code == "REPORT_INCOMPLETE"


# ---------------------------------------------------------------------------
# ③ AC-03：REQUIRED_INPUT_KEYS 四触点缺键负控
# ---------------------------------------------------------------------------

class TestTouchpointContract:
    @pytest.mark.parametrize("touchpoint,partial_value", [
        ("propose_plan", {"_ctx": {"run_dir": "x"}}),  # 缺 spec
        ("write_analysis", {"_ctx": {"run_dir": "x"}}),  # 缺 run_manifest_summary
        ("submit_claim", {"_ctx": {"run_dir": "x"}}),  # 缺 nc_evidence + results
        ("diagnose_fail", {"_ctx": {"run_dir": "x"}}),  # 缺 human_notes + claim
    ])
    def test_missing_required_keys_rejected(self, tmp_path, touchpoint,
                                            partial_value):
        runner = _FakeRunner(response={"verdict": "S", "statement": "s",
                                       "contrasts": []})
        actor = HeadlessActor(runner, evidence_root=tmp_path)
        partial_value["_ctx"]["run_dir"] = str(_run_dir(tmp_path))
        with pytest.raises(TouchpointError) as excinfo:
            actor(touchpoint, "c", partial_value)
        assert excinfo.value.code == "REQUEST_INCOMPLETE"

    def test_required_keys_covers_all_touchpoints(self):
        # 四触点 + repair 都必须在清单里
        for tp in ("propose_plan", "write_analysis", "submit_claim",
                   "submit_claim_repair", "diagnose_fail"):
            assert tp in REQUIRED_INPUT_KEYS, f"{tp} 不在 REQUIRED_INPUT_KEYS"
            assert REQUIRED_INPUT_KEYS[tp], f"{tp} 必需键为空"


# ---------------------------------------------------------------------------
# ④ AC-04：K4 请求瘦身（run 摘要代替全量 manifest）
# ---------------------------------------------------------------------------

class TestK4Slim:
    def test_k4_request_has_summary_not_full_manifest(self):
        captured = {}

        def actor(name, contract, value):
            captured.update(value)
            return {"source": "# analysis\n"}

        skills = build_skills(actor=actor, batch_runner=lambda p, c=None: {},
                              analyzer=lambda m, c=None: {},
                              confirmer=lambda p, c=None: {})
        # 模拟真实大 manifest：每个 run 行含大量字段（S2 事故 223KB）
        big_manifest = {
            "runs": [{"condition_id": f"c{i}", "block_id": f"b{i}",
                      "record_path": f"/mnt/artifacts/runs/r{i}.json",
                      "record_sha256": "a" * 64, "value": float(i),
                      "qpos": [0.1] * 20, "qvel": [0.01] * 20,
                      "meta": {"step": i * 100, "wallclock": 1.5,
                               "notes": "x" * 200}}
                     for i in range(100)],
            "contrasts": [{"id": "main", "metric_id": "m",
                           "treatment_condition": "t",
                           "control_condition": "c",
                           "proposition": "equivalent", "delta": "0.01"}],
            "confirm_plan": {"nc_slot": {"ci_level": "0.90"},
                             "nc_contrast": {"id": "nc"}},
        }
        skills["K4"].execute(big_manifest, {})
        assert "run_manifest_summary" in captured
        summary = captured["run_manifest_summary"]
        assert summary["n_runs"] == 100
        assert len(summary["run_records"]) == 100
        assert summary["contrasts"] is not None
        assert summary["nc_slot"] is not None
        # 摘要必须远小于全量 manifest（瘦身语义）
        summary_size = len(json.dumps(summary, default=str))
        full_size = len(json.dumps(big_manifest, default=str))
        assert summary_size < full_size * 0.5, (
            f"摘要 {summary_size}B 未显著小于全量 {full_size}B")
