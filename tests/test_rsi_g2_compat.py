"""RSI-G2 兼容：legacy 逐字不变、G2 仅显式启用、能力表与状态 sha（零 POST）。"""
from __future__ import annotations

import json

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import g2_contract as G2
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import state as S
from tests import rsi_r1_helpers as H


def _turn(text: str, result: "dict") -> T.Turn:
    return T.Turn(model_text=text, result=result)


def test_default_policy_is_byte_identical_to_explicit_legacy():
    turns = [_turn('{"action":"analyze"}',
                   {"ok": True, "action": "analyze", "analysis_id": "a1"}),
             _turn('{"action":"run_confirm"}',
                   {"ok": True, "action": "run_confirm",
                    "candidate_output": {"x": "y" * 9000}})]
    default = T.build_request(system="s", task_brief="b", turns=turns,
                              counter=lambda text: 1)
    legacy = T.build_request(system="s", task_brief="b", turns=turns,
                             counter=lambda text: 1,
                             delivery_policy=T.POLICY_LEGACY)
    assert default.serialized == legacy.serialized
    assert default.sha256 == legacy.sha256


def test_g2_policy_requires_explicit_flag():
    turns = [_turn("{}", {"ok": True, "action": "x",
                          T.G2_ENVELOPE_KEY: {"schema": "x"}})]
    # 默认 legacy：根本不看 envelope，仍按旧裁剪处理（不会抛 G2 错）
    request = T.build_request(system="s", task_brief="b", turns=turns,
                              counter=lambda text: 1)
    assert "result_envelope" in request.serialized
    with pytest.raises(T.ResultViewIncomplete):
        T.build_request(system="s", task_brief="b", turns=turns,
                        counter=lambda text: 1,
                        delivery_policy=T.POLICY_G2_ATOMIC)


def test_legacy_refuses_result_context():
    with pytest.raises(ValueError):
        T.build_request(system="s", task_brief="b", turns=(),
                        counter=lambda text: 1, result_context={"x": 1})


def test_action_registry_keeps_legacy_prompt_unchanged():
    assert len(L.ACTION_NAMES) == 7
    assert L.G2_ACTION_NAMES == ("read_result_artifact",)
    text = L.render_system(extra_rules="x")
    for name in L.ACTION_NAMES:
        assert name in text
    assert "read_result_artifact" not in text       # legacy 不暴露新动作


def test_read_action_requires_analysis_prefix_and_bounded_page():
    ok = ('{"action":"read_result_artifact",'
          '"artifact_id":"analysis/output.json",'
          '"output_sha256":"%s","cursor":0,"page_bytes":4096}' % ("a" * 64))
    action = L.parse_action(ok)
    assert action.name == "read_result_artifact"
    for doc, code in (
            ({"artifact_id": "private/analysis/x.json",
              "output_sha256": "a" * 64, "cursor": 0, "page_bytes": 1},
             "ARTIFACT_FORBIDDEN"),
            ({"artifact_id": "analysis/x.json", "output_sha256": "a" * 64,
              "cursor": -1, "page_bytes": 1}, "CURSOR_INVALID"),
            ({"artifact_id": "analysis/x.json", "output_sha256": "a" * 64,
              "cursor": 0, "page_bytes": 9000}, "PAGE_TOO_LARGE")):
        text = json.dumps({"action": "read_result_artifact", **doc})
        with pytest.raises(L.LoopError) as exc:
            L.parse_action(text)
        assert exc.value.code == code


def test_g2_capabilities_are_declared_not_a_campaign_profile():
    assert P.G2_CAPABILITIES == frozenset({
        P.CAP_DELIVERY_ATOMIC_V1, P.CAP_SCOPE_CONTRACT_V1,
        P.CAP_READ_RESULT_ARTIFACT, P.CAP_VISIBILITY_CAPTURE_V1})
    assert "rsi-g2" not in P.PROFILES
    with pytest.raises(P.ProfileError):
        P.profile_by_id("rsi-g2")


def test_legacy_submit_claim_event_has_no_origin(tmp_path):
    ctx = H.make_ctx(tmp_path / "camp")
    state = S.ResearchState(ctx)
    for target in (S.ROOT_AUTHORIZED, S.ROOT_CALIBRATED, S.ROOT_FROZEN,
                   S.ROOT_PROBED, S.ROOT_PRACTICED):
        state.advance_root(target, reason="g2 compat")
    state.journal.append("reply", {"response_id": "r1", "text": "{}",
                                   "study": "A"})
    # 直接推到 RESULTS_RELEASED（真实顺序由分派器完成；初始已是 EXPLORING）
    for target in (S.ST_CONFIRM_LOCKED, S.ST_CONFIRM_RUNNING,
                   S.ST_RESULTS_RELEASED):
        state._advance_study("A", target, reason="test")
    state.submit_claim(study="A", claims=[], report_text="r",
                       response_id="r1")
    claims = [e for e in state._events() if e.event == "claim"]
    assert claims and "origin" not in claims[-1].payload
    state2 = S.ResearchState(ctx)
    state2.journal.append("reply", {"response_id": "r2", "text": "{}",
                                    "study": "B"})
    for target in (S.ST_CONFIRM_LOCKED, S.ST_CONFIRM_RUNNING,
                   S.ST_RESULTS_RELEASED):
        state2._advance_study("B", target, reason="test")
    state2.submit_claim(study="B", claims=[], report_text="r",
                        response_id="r2",
                        origin={"reply_sha256": "a" * 64,
                                "action_sha256": "b" * 64})
    claims = [e for e in state2._events() if e.event == "claim"]
    assert claims[-1].payload["origin"]["reply_sha256"] == "a" * 64


def test_dispatcher_default_policy_is_legacy(tmp_path):
    from orchestrator.rsi_r1 import assembly as A
    ctx = H.make_ctx(tmp_path / "camp2")
    disp = A.CampaignDispatcher(
        ctx=ctx, state=None, admission=None, budget=None, study="A",
        runner=None, image=None, backend_factory=None)
    assert disp.delivery_policy == T.POLICY_LEGACY
    assert disp.g2 is False


def test_g2_dispatcher_requires_scope_contract(tmp_path):
    from orchestrator.rsi_r1 import assembly as A
    ctx = H.make_ctx(tmp_path / "camp3")
    with pytest.raises(A.AssemblyError) as exc:
        A.CampaignDispatcher(
            ctx=ctx, state=None, admission=None, budget=None, study="A",
            runner=None, image=None, backend_factory=None,
            profile=P.PROFILE_R13_DEF, task=_task_stub(),
            delivery_policy=T.POLICY_G2_ATOMIC)
    assert exc.value.code == "POLICY_BINDING_MISSING"


def _task_stub():
    from types import SimpleNamespace
    return SimpleNamespace(task_id="t", primary=SimpleNamespace(id="k"),
                           negative_control_spec=None)


def test_g2_confirm_payload_projects_candidate_output(tmp_path):
    from types import SimpleNamespace
    from orchestrator.rsi_r1 import assembly as A
    root = tmp_path / "camp4"
    ctx = H.make_ctx(root)
    contract = G2.ScopeContractV1.from_dict({
        "schema": G2.SCOPE_CONTRACT_SCHEMA, "schema_version": 1,
        "contract_id": "c", "task_id": "t", "task_spec_sha256": "a" * 64,
        "hypothesis_id": "h", "estimand_id": "e", "metric_id": "displacement_x_m",
        "proposition": "increase_gt", "delta": 0.001, "unit": "m",
        "target_population": "registered_distribution", "aggregation": "mean",
        "weighting": "equal", "block_to_contrast_formula": "mean",
        "formula_notes": "mean", "censoring_rule": "dropped",
        "coverage": "observed_blocks", "interval_level": 0.9,
        "allowed_semantic_labels": list(G2.SEMANTIC_LABELS)})
    disp = A.CampaignDispatcher(
        ctx=ctx, state=None, admission=None, budget=None, study="A",
        runner=None, image=None, backend_factory=None,
        profile=P.PROFILE_R13_DEF, task=_task_stub(),
        delivery_policy=T.POLICY_G2_ATOMIC, scope_contract=contract)
    output = {"contrasts": [{"contrast_id": "k",
                             "metric_id": "displacement_x_m",
                             "proposition": "increase_gt",
                             "estimate": 0.05, "lower": 0.01, "upper": 0.09,
                             "status": "SUPPORTED", "n_blocks": 4}]}
    plan = SimpleNamespace(sha256="d" * 64)
    payload = disp._g2_confirm_payload(["r1", "r2"], "s" * 64, plan,
                                       output, b'{"x":1}', b'{"y":2}')
    assert payload["ok"] is True
    envelope = payload["result_envelope"]
    assert envelope["primary"]["estimate"] == pytest.approx(0.05)
    G2.verify_envelope(envelope, scope_contract=contract)
    registry = json.loads(
        (root / "state" / "g2_artifacts.json").read_text(encoding="utf-8"))
    assert registry["plan_sha256"] == "d" * 64
    assert {a["artifact_id"] for a in registry["artifacts"]} == {
        "analysis/output.json", "analysis/receipt.json"}
