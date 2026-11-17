"""RV 零 POST 票据 B/C（2026-11-17）：修订状态持久化 + v3 原事件离线回归。

夹具 ``tests/fixtures/rv_draft_v3_replay.json`` 逐字取自 v3 冻结轮根
``rvfb3-prereg-20261117T060935Z/studies/B/state/journal.jsonl``（journal sha 在
夹具 provenance 里），只做裁剪（去掉 read 页数据），不改写候选提交载荷。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import research_report as RR
from orchestrator.rsi_r1 import rv_contract as RV
from orchestrator.rsi_r1 import rv_draft as DR
from orchestrator.rsi_r1 import rv_review as RVW
from orchestrator.rsi_r1 import rv_runtime as RT
from orchestrator.rsi_r1 import task_specs as TS

FIXTURE = Path(__file__).parent / "fixtures" / "rv_draft_v3_replay.json"


def _replay() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _plan() -> SimpleNamespace:
    """v3 冻结 brief 的 plan 形状：组件锚点 = component_single_factor。"""
    return SimpleNamespace(
        hypotheses=(SimpleNamespace(contrast_id="component_single_factor"),),
        primary=SimpleNamespace(id="component_single_factor"))


def _book(tmp_path) -> DR.DraftBook:
    return DR.DraftBook(ctx=RT.RVContext(Path(tmp_path) / "B"), study="B",
                        task=TS.build_new_b())


def _run(tmp_path, *, upto: int = 3):
    data = _replay()
    book = _book(tmp_path)
    task = TS.build_new_b()
    states = []
    for submit in data["submits"][:upto]:
        problems = RVW.precommit_report_problems(
            submit["report"], task=task, plan=_plan(),
            claims=submit["claims"])
        states.append(book.record(report=submit["report"],
                                  claims=submit["claims"],
                                  problems=problems))
    return data, book, states


def test_v3_replay_reproduces_three_rejections(tmp_path):
    _, _, states = _run(tmp_path)
    assert len(states) == 3
    first = " ".join(states[0]["unresolved_errors"])
    second = " ".join(states[1]["unresolved_errors"])
    third = " ".join(states[2]["unresolved_errors"])
    # 1：contrast 用 plan 组件锚点；角色规则与机器前缀同时在场。
    assert "primary_results[0].contrast_id" in first
    assert "expected 'tc_x_inertia'" in first and "(got 'component_single_factor')" in first
    assert "plan 组件锚点" in first
    # 2：contrast 已修正，只剩 alternatives 类型错误。
    assert "alternative_explanations" in second
    assert "contrast_id" not in second
    assert '"contrast_id"' not in second
    # 3：回退——contrast 与 estimands 同时再次失败。
    assert "primary_results[0].contrast_id" in third
    assert "explore_confirm_comparison.estimands" in third
    # 三次都被拒，未封存。
    assert all(s["sealed"] is False for s in states)


def test_draft_state_is_persistent_and_patch_is_host_computed(tmp_path):
    data, book, states = _run(tmp_path)
    state = book.state()
    assert state["draft_version"] == 3 and state["attempts"] == 3
    assert state["draft_sha"] == states[-1]["draft_sha"]
    assert state["public_schema_sha"] == RV.canonical_sha(RR.public_schema())
    # candidate_patch 由宿主按字节比较：候选每版整体重写（8/11 顶层键变）。
    patch = state["candidate_patch"]
    assert "primary_results" in patch["changed"]
    assert len(patch["changed"]) >= 2
    assert patch["claims_count"] == [2, 2]
    # 三版原样落盘，逐字节等于候选提交。
    for submit, version in zip(data["submits"], (1, 2, 3)):
        path = book.ctx.relative(book._draft_rel(version))
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["report"] == submit["report"]
        assert doc["claims"] == submit["claims"]
    attempts = book.ctx.relative(book._attempts_rel()).read_text().splitlines()
    assert [json.loads(line)["attempt"] for line in attempts] == [1, 2, 3]


def test_corrected_draft_survives_history_clipping(tmp_path):
    """第 2 版（contrast 已修正）大于 4096 B：历史会被裁剪，修订段不会。"""
    _, book, _ = _run(tmp_path, upto=2)
    state = book.state()
    assert state["draft_version"] == 2
    assert book._draft_doc(state["draft_rel"])["report"]["primary_results"][0][
        "contrast_id"] == "tc_x_inertia"
    context = book.render_context()
    assert '"contrast_id":"tc_x_inertia"' in context
    draft_section = context.split(DR.DRAFT_HEADER, 1)[1]
    assert len(draft_section.encode("utf-8")) > T.TOOL_REPLY_MAX_BYTES
    # 进入请求后仍在首条 user 消息里（与是否被历史裁剪无关）。
    req = T.build_request(
        system="s", task_brief="### 当前可见状态\n{}\n\n" + context,
        turns=[T.Turn(model_text="x" * 8000, result={"ok": True})] * 3,
        counter=lambda text: max(1, len(text) // 4),
        delivery_policy=T.POLICY_G2_ATOMIC, result_context=None)
    assert '"contrast_id":"tc_x_inertia"' in req.messages[0]["content"]


def test_rejections_never_claim_or_advance_state(tmp_path):
    data = _replay()
    events = data["events"]
    first = min(s["seq"] for s in data["submits"])
    after = [e for e in events if int(e["seq"]) >= first]
    assert not [e for e in after if e["event"] == "claim"]
    # 三次拒绝都在事件流里，且都是 side-effect 前的结构预检。
    rejects = [e for e in after if e["event"] == "action_result"
               and e["code"] == "REPORT_PRECOMMIT_INVALID"]
    assert len(rejects) == 3


def test_action_layer_classification_matches_events(tmp_path):
    data = _replay()
    # 全 journal：study 自生成阶段另有 3 次解析层拒绝（WRONG_TYPE×2、MALFORMED_JSON）。
    whole = RT.classify_action_results(data["action_results"])
    assert whole["counts"]["parse_layer"] == 4
    # 预注册口径的 suffix 窗口（seq ≥ 138）：1 次 MISSING_KEY，0 次 rule-bearing，
    # 3 次 dispatch 层（报告预检）不算动作层。
    window = [r for r in data["action_results"] if int(r["seq"]) >= 138]
    out = RT.classify_action_results(window)
    assert out["counts"]["parse_layer"] == 1
    assert out["counts"]["tool_layer"] == 0
    assert out["counts"]["dispatch"] == 3
    assert out["counts"]["rule_bearing_action_layer"] == 0
    assert out["statements"] == {
        "no_any_action_rejection": False,
        "no_cursor_type_rejection": True,
        "prereg_action_feedback_exercised": False,
    }
    parse = out["parse_layer_rejections"][0]
    assert parse["code"] == "MISSING_KEY"
    assert parse["seq"] == 139


def test_contrast_roles_single_source_for_prompt_and_error(tmp_path):
    task = TS.build_new_b()
    roles = RV.contrast_roles(task, _plan())
    assert roles["task_contrast_id"] == "tc_x_inertia"
    assert roles["plan_primary_contrast_id"] == "component_single_factor"
    assert roles["plan_component_ids"] == ["component_single_factor"]
    rendered = RV.render_contrast_roles(roles)
    assert "tc_x_inertia" in rendered
    assert "component_single_factor" in rendered
    assert "不是报告里的对比 id" in rendered


def test_identical_resubmission_keeps_version_but_counts_attempt(tmp_path):
    data = _replay()
    book = _book(tmp_path)
    task = TS.build_new_b()
    submit = data["submits"][0]
    problems = RVW.precommit_report_problems(
        submit["report"], task=task, plan=_plan(), claims=submit["claims"])
    first = book.record(report=submit["report"], claims=submit["claims"],
                        problems=problems)
    second = book.record(report=submit["report"], claims=submit["claims"],
                         problems=problems)
    assert first["draft_version"] == second["draft_version"] == 1
    assert second["attempts"] == 2
    assert second["draft_sha"] == first["draft_sha"]
