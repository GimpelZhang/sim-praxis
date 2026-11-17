"""RSI-R1-N T2 零 POST 预演面（Plan §4）：能机判的逐面证据。

本文件覆盖 Plan §4 表中可在单元层面真实执行的面；仍需真实容器/进程的面
（槽进程 worker 的 allow/deny、真实 sender 全链）在 T2 正式预演脚本中由
既有 R1.3 仪器测试与 `offline_suite` 提供证据，本文件不冒充。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r1 import assembly as A
from orchestrator.rsi_r1 import contracts_r13 as C13
from orchestrator.rsi_r1 import design_intent as DI
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import protocol as PT
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import review_capacity as RC
from orchestrator.rsi_r1 import review_runner as RN
from orchestrator.rsi_r1 import runtime as RT
from orchestrator.rsi_r1 import rv_draft as DR
from orchestrator.rsi_r1 import rv_review as RVW
from orchestrator.rsi_r1 import task_specs as TS


def _intent(task: TS.TaskSpecV13) -> dict:
    nuisance = next(iter(task.nuisance_schema))
    support = task.nuisance_schema[nuisance]["support"]
    return {
        "schema": P.DESIGN_INTENT_SCHEMA_V13, "schema_version": 1,
        "phase_role": "confirmation",
        "estimand": {"metric_id": task.estimand["metric_id"],
                     "contrast_ids": list(task.estimand["contrast_ids"])},
        "nuisance": {nuisance: {"mode": "varying",
                                "assignment_owner": "host",
                                "support": [str(support[0]), str(support[1])]}},
        "replication": {"unit_id": "block", "independent_unit": "block",
                        "deterministic_repeat_purpose": "determinism"},
        "phase_comparability": {"relation": "same_target",
                                "rationale": "确认集在同一目标总体"},
    }


@pytest.mark.parametrize("builder", [TS.build_r1n_a, TS.build_r1n_b])
def test_face_public_template_feeds_own_gate(builder):
    task = builder()
    brief = PT.render_study_brief(task.split == "holdout" and "A" or "A",
                                  profile=P.PROFILE_R1N_DEF, task=task)
    assert task.task_id in brief
    assert task.primary.id in brief
    if task.negative_control_spec:
        assert task.negative_control_spec["contrast"]["id"] in brief
    parsed = DI.parse_design_intent(_intent(task),
                                    profile=P.PROFILE_R1N_DEF, task_spec=task)
    assert parsed.estimand["metric_id"] == task.estimand["metric_id"]
    # 错误 metric 必须被宿主自己的门拒绝（模板→门自洽）
    bad = _intent(task)
    bad["estimand"]["metric_id"] = "stop_time_s"
    with pytest.raises(DI.DesignError):
        DI.parse_design_intent(bad, profile=P.PROFILE_R1N_DEF, task_spec=task)
    # 候选不得声称自己实现 host 随机化
    bad2 = _intent(task)
    name = next(iter(bad2["nuisance"]))
    bad2["nuisance"][name]["assignment_owner"] = "candidate"
    with pytest.raises(DI.DesignError):
        DI.parse_design_intent(bad2, profile=P.PROFILE_R1N_DEF, task_spec=task)


def test_face_worst_case_review_packet_within_caps():
    tok = RC.load_tokenizer()
    caps = RC.registered_caps(P.PROFILE_R1N_DEF)
    for task in (TS.build_r1n_a(), TS.build_r1n_b()):
        blocks = [f"b{i:02d}" for i in range(1, 33)]
        assignments = [{"block_id": b, "nuisance": {"n": "0.35"}}
                       for b in blocks]
        values = {b: {c.id: 0.12 + 0.001 * i + 0.01 * j
                      for j, c in enumerate(task.conditions)}
                  for i, b in enumerate(blocks)}
        report = {"schema": RR.REPORT_SCHEMA, "schema_version": 1,
                  "task_id": task.task_id,
                  "question_and_hypotheses": "q" * 400,
                  "methods_and_assignment_responsibility": "m" * 600,
                  "primary_results": [], "heterogeneity_and_scope": [],
                  "explore_confirm_comparison": {},
                  "alternative_explanations": [], "limitations": [],
                  "next_steps": []}
        study = "A" if task.task_id.startswith("r1n-a") else "B"
        packet = RN.build_packet(
            study=study, task=task, intent=_intent(task),
            design_audit={"schema": "simpraxis.rsi_r13.design_audit.v1",
                          "schema_version": 1,
                          "pre": {"ok": True}, "post": {"ok": True}},
            assignments=assignments, values=values, report=report,
            evidence_files={"analysis/output.json": b"{}"},
            candidate_excerpts=[], sources={"task": b"t", "plan": b"p"})
        raw = RN.packet_bytes(packet)
        tokens = len(tok.encode(raw.decode("utf-8")).ids)
        assert len(raw) <= caps["packet_bytes"]
        assert tokens <= caps["input_tokens"]
        # 包语义守恒：实际 nuisance 从 assignments 填充、主对比全条件在场
        table = packet["block_table"]
        assert len(table) == 32
        for row in table:
            assert row["nuisance"] == {"n": "0.35"}
            assert set(row["values"]) == set(task.primary.condition_ids)
        assert packet["primary_contrast"]["id"] == task.primary.id
        assert packet["assignment_summary"]["nuisance"] == task.nuisance_schema


def test_face_slot_table_and_cross_lane_independence(tmp_path):
    budget = C13.BudgetV13(
        schema=P.BUDGET_SCHEMA_V13, envelope_id="e",
        profile_id=P.PROFILE_R1N, posts_by_lane=dict(P.LANE_POST_CAPS_R1N),
        max_usd=Decimal("0"),
        max_runs_by_lane={lane: 200 for lane in P.LANE_ORDER_R1N},
        max_steps=10_000)
    slots = budget.slots()
    assert len(slots) == 132
    assert [s["lane"] for s in slots[:12]] == ["probe"] * 12
    ctx = RT.CampaignContext(envelope_id="e", root=tmp_path,
                            protected_roots=())
    book = A.SlotBook(ctx, budget)
    book.set_lane("study_A")
    permit = L.Permit(envelope_id="e", lock_sha256="a" * 64,
                      budget_sha256=budget.sha256, phase="rv")
    for i in range(48):
        book.reserve_atomic(call_id=f"a{i}", permit=permit)
    with pytest.raises(L.LoopError) as exc:
        book.reserve_atomic(call_id="a48", permit=permit)
    assert exc.value.code == "BUDGET_REFUSED"
    # 另一 lane 仍有自己的 48 槽（不借额、不互停）
    book.set_lane("study_B")
    book.reserve_atomic(call_id="b0", permit=permit)
    assert book.count("study_B") == 1 and book.count("study_A") == 48


def test_face_action_and_report_feedback_rules():
    # 动作层：cursor 类型规则（R1-N 复用同一解析器）
    base = {"action": "read_result_artifact",
            "artifact_id": "analysis/output.json",
            "output_sha256": "a" * 64, "page_bytes": 1024}
    with pytest.raises(L.LoopError) as exc:
        L.parse_action(json.dumps({**base, "cursor": None}), public_rules=True,
                       allowed_actions=("read_result_artifact", "submit_claim"))
    assert exc.value.code == "CURSOR_INVALID"
    assert "expected non-negative integer" in exc.value.detail
    assert "got null" in exc.value.detail
    # 报告层：R1-N 任务上的 expected/got + ID 角色
    task = TS.build_r1n_a()
    report = {"schema": RR.REPORT_SCHEMA, "schema_version": 1,
              "task_id": task.task_id, "question_and_hypotheses": {},
              "methods_and_assignment_responsibility": "m",
              "primary_results": [{"contrast_id": "component_single_factor",
                                   "analysis_receipt_sha": "a" * 64,
                                   "result_locator": {}}],
              "heterogeneity_and_scope": [], "explore_confirm_comparison": {},
              "alternative_explanations": [], "limitations": [],
              "next_steps": []}
    problems = RVW.precommit_report_problems(report, task=task)
    text = " ".join(problems)
    assert "expected non-empty string (got object)" in text
    assert "expected 'friction_x_mass'" in text


def test_face_precommit_and_revision_state(tmp_path):
    task = TS.build_r1n_a()
    ctx = RT.CampaignContext(envelope_id="e", root=tmp_path,
                            protected_roots=())
    book = DR.DraftBook(ctx=ctx, study="A", task=task)
    bad = {"schema": RR.REPORT_SCHEMA, "schema_version": 1,
           "task_id": task.task_id, "question_and_hypotheses": "q",
           "methods_and_assignment_responsibility": "m",
           "primary_results": [{"contrast_id": "wrong",
                                "analysis_receipt_sha": "a" * 64,
                                "result_locator": {}}],
           "heterogeneity_and_scope": [], "explore_confirm_comparison": {},
           "alternative_explanations": [], "limitations": [], "next_steps": []}
    problems = RVW.precommit_report_problems(bad, task=task)
    state = book.record(report=bad, claims=[], problems=problems)
    assert state["sealed"] is False and state["unresolved_errors"]
    context = book.render_context()
    assert "修订状态" in context and "draft_sha" in context
    assert '"contrast_id":"wrong"' in context.replace(" ", "")


def test_face_root_separation_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RSI_R1N_ROOT", str(tmp_path / "roots"))
    monkeypatch.delenv("SIMPRAXIS_RSI_R1N_CAMPAIGN", raising=False)
    with pytest.raises(RP.RsiR1NCampaignUnsetError):
        RP.rsi_r1n_campaign_root()
    monkeypatch.setenv("SIMPRAXIS_RSI_R1N_CAMPAIGN", "r1n-preview")
    RP.assert_rsi_r1n_roots_isolated()
    root = RP.rsi_r1n_campaign_root().resolve(strict=False)
    for name, prot in RP.rsi_r1n_protected_write_roots().items():
        assert root != Path(prot).resolve(strict=False)


def _sender_stack(tmp_path):
    """真实 build_production_sender→CampaignAdmission→SlotBook→(mock)gateway。"""
    import hashlib
    from types import SimpleNamespace

    repo = Path(__file__).resolve().parents[1]

    def sha(path: str) -> str:
        return hashlib.sha256((repo / path).read_bytes()).hexdigest()

    from orchestrator.rsi_r1 import contracts_r13 as _C13
    from orchestrator.rsi_r1 import lock as _LK
    from orchestrator.rsi_r1 import runtime as _RT
    from orchestrator.rsi_r1 import state as _S

    (tmp_path / "contract").mkdir(parents=True, exist_ok=True)
    ctx = _RT.CampaignContext(envelope_id="e", root=tmp_path,
                              protected_roots=())
    budget = _C13.BudgetV13(
        schema=P.BUDGET_SCHEMA_V13, envelope_id="e",
        profile_id=P.PROFILE_R1N, posts_by_lane=dict(P.LANE_POST_CAPS_R1N),
        max_usd=Decimal("0"),
        max_runs_by_lane={lane: 200 for lane in P.LANE_ORDER_R1N},
        max_steps=10_000)
    ctx.write_json("contract/budget.json", budget.to_dict())
    ctx.write_json("contract/envelope.json",
                   {"envelope_id": "e", "mode": "offline_validation"})
    ctx.write_json("contract/lock.json", {
        "schema": L.LOCK_SCHEMA, "schema_version": 1, "envelope_id": "e",
        "budget_sha256": budget.sha256, "stage": "coding_debug",
        "phase": "paid",
        "dependencies": {"orchestrator/rsi_r1/loop.py":
                         sha("orchestrator/rsi_r1/loop.py")}})
    lock = L.load_lock(_LK.lock_reader(ctx))
    state = _S.ResearchState(ctx)
    for target in (_S.ROOT_AUTHORIZED, _S.ROOT_CALIBRATED, _S.ROOT_FROZEN):
        if _S.ROOT_ORDER.index(target) > _S.ROOT_ORDER.index(state.root_state()):
            state.advance_root(target, reason="preview")
    spend = A.SpendContract(mode=A.MODE_OFFLINE_VALIDATION)
    admission = A.CampaignAdmission(ctx=ctx, state=state, budget=budget,
                                    lock_sha256=lock.sha256, spend=spend,
                                    lane="probe")
    slots = A.SlotBook(ctx, budget)
    admission.attach_slots(slots)
    envelope = SimpleNamespace(envelope_id="e")
    calls: "list[object]" = []

    def transport(request):
        calls.append(request)
        return L.Reply(text="ok", response_id="r1", attempts_http=1)

    sender = L.build_production_sender(
        lock_reader=_LK.lock_reader(ctx), transport_factory=lambda: transport,
        admission=admission, budget=slots, envelope=envelope,
        spend_contract=spend, sha_of=sha)
    return ctx, sender, admission, slots, envelope, spend, lock, calls


def test_face_real_sender_wiring(tmp_path):
    ctx, sender, admission, slots, envelope, spend, lock, calls = _sender_stack(
        tmp_path)
    from types import SimpleNamespace
    permit = admission.check_all(envelope=envelope, root_lock=lock,
                                 phase="paid", spend_contract=spend)
    reservation = slots.reserve_atomic(call_id=f"e:{'a' * 16}", permit=permit)
    request = SimpleNamespace(sha256="a" * 64)
    reply = sender.send_once(request, reservation)
    assert reply.text == "ok" and len(calls) == 1
    # call_id 与请求不匹配 → 终态拒绝，且不再触达 transport
    with pytest.raises(L.LoopError) as exc:
        sender.send_once(SimpleNamespace(sha256="b" * 64), reservation)
    assert exc.value.code == "LOCK_MISMATCH" and len(calls) == 1
    # LOCK 依赖漂移 → 装配期拒绝
    import hashlib
    from orchestrator.rsi_r1 import lock as _LK
    with pytest.raises(L.LoopError) as exc:
        L.build_production_sender(
            lock_reader=_LK.lock_reader(ctx),
            transport_factory=lambda: (lambda r: L.Reply("x", "r", 1)),
            admission=admission, budget=slots, envelope=envelope,
            spend_contract=spend,
            sha_of=lambda p: "0" * 64)
    assert exc.value.code == "LOCK_DRIFT"
    # probe lane 12 槽：第 13 个申请拒绝（不借额）
    for i in range(1, 12):
        slots.reserve_atomic(call_id=f"e:{i:016x}", permit=permit)
    with pytest.raises(L.LoopError) as exc:
        slots.reserve_atomic(call_id=f"e:{99:016x}", permit=permit)
    assert exc.value.code == "BUDGET_REFUSED"


def test_face_review_echo_contract():
    from orchestrator.rsi_r1 import r13_review_run as RRU
    slot = RN.ReviewSlot("a-rev-1", "reviewer-1", "sess-1")
    packet = {"study": "A", "review_id": slot.review_id,
              "reviewer_id": slot.reviewer_id, "payload": {"k": "v"}}
    doc, raw, sha = RRU.seal_packet(packet, slot)
    assert doc["review_id"] == slot.review_id
    assert doc["reviewer_id"] == slot.reviewer_id
    assert doc["packet_sha256"] == sha and len(raw) > 0
    caps = RN.public_contract(P.PROFILE_R1N_DEF)["request"]["caps"]
    assert caps["input_tokens"] == P.REVIEW_INPUT_TOKENS_R1N
    assert caps["packet_bytes"] == P.REVIEW_PACKET_BYTES_R1N
