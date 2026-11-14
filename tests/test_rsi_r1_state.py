"""RSI-R1 T2：状态机、append-only 日志、产物谱系与确认单批（AC10/AC14）。

全部离线：tmp 根下跑，不触网、不调模型、不跑仿真。覆盖：

* root 状态只进不退、跳步拒绝、CLOSED 吸收；
* study 五个前置状态 + 终局只来自 judge；
* 候选产物必须绑定「已登记回复 + 由它派发的动作」——预造 successor 不可信；
* 维护侧改动把 study 翻成 ASSISTED（assisted 结论永不 VERIFIED）；
* 确认锁/单批/幂等发布/崩溃恢复三分叉；
* 日志断链/篡改在第一次读时就抛。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import state as ST
from tests.rsi_r1_helpers import make_ctx, make_dirs, plan_doc

DESIGN_SRC = "def build(spec):\n    return spec\n"
ANALYSIS_SRC = "def analyse(obs):\n    return {'x': 0.0}\n"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _state(tmp_path: Path) -> ST.ResearchState:
    make_dirs(tmp_path)
    return ST.ResearchState(make_ctx(tmp_path))


def _reply_and_action(st: ST.ResearchState, doc: dict, rid: str) -> str:
    st.record_reply(response_id=rid, text=json.dumps(doc), phase="explore")
    return st.record_action(response_id=rid, action=doc)["action_sha256"]


def _register_sources(st: ST.ResearchState, *, study: str = "A") -> "tuple[str, str]":
    action = {"action": "write_artifact", "kind": "design", "source": DESIGN_SRC}
    sha = _reply_and_action(st, action, "r-design")
    d = st.register_artifact(study=study, kind="design", source=DESIGN_SRC,
                             response_id="r-design", action_sha256=sha)
    action = {"action": "write_artifact", "kind": "analysis",
              "source": ANALYSIS_SRC}
    sha = _reply_and_action(st, action, "r-analysis")
    a = st.register_artifact(study=study, kind="analysis", source=ANALYSIS_SRC,
                             response_id="r-analysis", action_sha256=sha)
    return d.source_sha256, a.source_sha256


def _frozen_study(tmp_path: Path, *, study: str = "A"):
    st = _state(tmp_path)
    design_sha, analysis_sha = _register_sources(st, study=study)
    doc = plan_doc(study, phase="confirm", design_source_sha=design_sha,
                   analysis_source_sha=analysis_sha)
    lock = st.freeze_confirm(study=study, plan=doc, design_sha256=design_sha,
                             analysis_sha256=analysis_sha)
    return st, lock, doc


# ---------------------------------------------------------------------------
# root 状态机
# ---------------------------------------------------------------------------

def test_root_forward_only_and_closed_is_absorbing(tmp_path: Path) -> None:
    st = _state(tmp_path)
    assert st.root_state() == ST.ROOT_INIT
    st.advance_root(ST.ROOT_AUTHORIZED, reason="t")
    st.advance_root(ST.ROOT_CALIBRATED, reason="t")

    with pytest.raises(ST.StateError) as exc:
        st.advance_root(ST.ROOT_AUTHORIZED, reason="back")
    assert exc.value.code == "BACKWARD_TRANSITION"

    with pytest.raises(ST.StateError) as exc:
        st.advance_root(ST.ROOT_PROBED, reason="skip")
    assert exc.value.code == "STATE_SKIP"

    with pytest.raises(ST.StateError) as exc:
        st.advance_root(ST.ROOT_CALIBRATED, reason="noop")
    assert exc.value.code == "NOOP_TRANSITION"

    # 从任意非终态可直达 CLOSED
    st.close_root(reason="done")
    assert st.root_state() == ST.ROOT_CLOSED
    with pytest.raises(ST.StateError) as exc:
        st.advance_root(ST.ROOT_FROZEN, reason="reopen")
    assert exc.value.code == "ROOT_CLOSED"
    # 重复 close 幂等（不写第二行）
    n = len(st._events())
    st.close_root(reason="again")
    assert len(st._events()) == n


# ---------------------------------------------------------------------------
# 产物谱系（AC10）
# ---------------------------------------------------------------------------

def test_candidate_artifact_requires_bound_reply_and_action(tmp_path: Path) -> None:
    st = _state(tmp_path)

    with pytest.raises(ST.StateError) as exc:
        st.register_artifact(study="A", kind="design", source=DESIGN_SRC,
                             response_id="ghost", action_sha256="f" * 64)
    assert exc.value.code == "UNKNOWN_REPLY"

    st.record_reply(response_id="r1", text='{"action":"propose_plan"}',
                    phase="explore")
    with pytest.raises(ST.StateError) as exc:
        st.register_artifact(study="A", kind="design", source=DESIGN_SRC,
                            response_id="r1", action_sha256="f" * 64)
    assert exc.value.code == "UNBOUND_SOURCE"

    # 候选必须携带 action_sha256
    with pytest.raises(ST.StateError) as exc:
        st.register_artifact(study="A", kind="design", source=DESIGN_SRC,
                             response_id="r1")
    assert exc.value.code == "MISSING_KEY"

    # 正确路径：同一回复派发的动作
    action = {"action": "write_artifact", "kind": "design", "source": DESIGN_SRC}
    sha = _reply_and_action(st, action, "r2")
    rec = st.register_artifact(study="A", kind="design", source=DESIGN_SRC,
                               response_id="r2", action_sha256=sha)
    assert rec.producer == ST.PRODUCER_CANDIDATE and not rec.assisted
    assert (tmp_path / "designs" / f"{rec.source_sha256}.py").is_file()


def test_record_action_dedupe_is_study_scoped(tmp_path: Path) -> None:
    """同一 (response_id, action) 在两个 study 下必须**各自**落一条动作事件。

    2026-11-12 T5 全量演练实测：A 与 B 各有一个运输实例、序号都从 1 起，首呼
    回复逐字相同 ⇒ response_id 相同 ⇒ 旧去重把 B 的动作当成 A 的重复静默吞掉，
    ``_binding`` 按 study 过滤后判 ``STATE_UNKNOWN``，整条 B 链处死。
    幂等去重的本意是「同一 study 内重放同一动作不重记」，故单位是 study。
    """
    st = _state(tmp_path)
    doc = {"action": "write_artifact", "kind": "design", "source": DESIGN_SRC}
    st.record_reply(response_id="0001-same", text=json.dumps(doc),
                    phase="explore")
    st.record_action(response_id="0001-same", action=doc, study="A")
    st.record_action(response_id="0001-same", action=doc, study="B")

    actions = [e for e in st.journal.events() if e.event == "action"]
    assert [e.payload.get("study") for e in actions] == ["A", "B"]

    # 同一 study 内重复登记仍是幂等（不重记）
    st.record_action(response_id="0001-same", action=doc, study="A")
    again = [e for e in st.journal.events() if e.event == "action"]
    assert len(again) == 2


def test_maintainer_edit_marks_study_assisted_and_rejects_action_sha(
        tmp_path: Path) -> None:
    st = _state(tmp_path)
    with pytest.raises(ST.StateError) as exc:
        st.register_artifact(study="A", kind="design", source="x",
                             action_sha256="a" * 64)
    assert exc.value.code == "MISSING_KEY"

    rec = st.register_artifact(study="A", kind="analysis",
                               source="print(1)\n")
    assert rec.producer == ST.PRODUCER_MAINTAINER and rec.assisted
    assert st.study_state("A") == ST.ST_ASSISTED
    # ASSISTED 是终态：不得再往前
    with pytest.raises(ST.StateError) as exc:
        st.freeze_confirm(study="A", plan=plan_doc("A"),
                          design_sha256="a" * 64, analysis_sha256="b" * 64)
    assert exc.value.code == "STUDY_TERMINAL"


# ---------------------------------------------------------------------------
# 计划修订
# ---------------------------------------------------------------------------

def test_plan_revisions_are_contiguous_and_only_in_exploring(tmp_path: Path) -> None:
    st = _state(tmp_path)
    with pytest.raises(ST.StateError) as exc:
        st.append_plan("A", plan_doc("A", phase="explore", revision=2))
    assert exc.value.code == "REVISION_GAP"

    st.append_plan("A", plan_doc("A", phase="explore", revision=1))
    # explore 计划不得携带 confirm 的源码绑定语义之外的东西；下一修订必须是 2
    with pytest.raises(ST.StateError) as exc:
        st.append_plan("A", plan_doc("A", phase="explore", revision=1))
    assert exc.value.code == "REVISION_GAP"
    with pytest.raises(ST.StateError) as exc:
        st.append_plan("A", plan_doc("A", phase="confirm", revision=2))
    assert exc.value.code == "PLAN_PHASE_INVALID"

    ok = st.append_plan("A", plan_doc("A", phase="explore", revision=2))
    assert ok["revision"] == 2


# ---------------------------------------------------------------------------
# 确认锁 / 单批 / 发布 / 恢复（AC14）
# ---------------------------------------------------------------------------

def test_freeze_confirm_binds_token_and_plan_becomes_immutable(tmp_path: Path) -> None:
    st, lock, doc = _frozen_study(tmp_path)
    assert st.study_state("A") == ST.ST_CONFIRM_LOCKED
    assert lock.token == ST.ResearchState.confirm_lock(st, "A").token
    assert lock.plan_sha256 == ST.C.ResearchPlan.from_dict(doc).sha256
    assert lock.block_count == 32 and lock.metric_id == "displacement_x_m"

    # 冻结后计划不可再改
    with pytest.raises(ST.StateError) as exc:
        st.append_plan("A", plan_doc("A", phase="explore"))
    assert exc.value.code == "PLAN_NOT_EDITABLE"


def test_freeze_confirm_requires_registered_sources(tmp_path: Path) -> None:
    st = _state(tmp_path)
    with pytest.raises(ST.StateError) as exc:
        st.freeze_confirm(study="A", plan=plan_doc("A"),
                          design_sha256="a" * 64, analysis_sha256="b" * 64)
    assert exc.value.code == "UNBOUND_SOURCE"


def test_confirm_batch_runs_once_and_publish_is_idempotent(tmp_path: Path) -> None:
    st, lock, _ = _frozen_study(tmp_path)

    with pytest.raises(ST.StateError) as exc:
        st.begin_confirm(study="A", token="0" * 32, specs_sha256="c" * 64)
    assert exc.value.code == "CONFIRM_TOKEN_MISMATCH"

    intent = st.begin_confirm(study="A", token=lock.token, specs_sha256="c" * 64)
    assert st.study_state("A") == ST.ST_CONFIRM_RUNNING

    with pytest.raises(ST.StateError) as exc:
        st.begin_confirm(study="A", token=lock.token, specs_sha256="d" * 64)
    assert exc.value.code == "CONFIRM_ALREADY_RUN"

    with pytest.raises(ST.StateError) as exc:
        st.publish_confirm(intent_id=intent.intent_id, manifest_sha256="e" * 64,
                           validated=True)
    assert exc.value.code == "INTENT_OPEN"

    st.complete_confirm(intent_id=intent.intent_id, result_sha256="e" * 64,
                        outcome=ST.OUTCOME_OK)
    # 同结果重复登记幂等；不同结果不得覆盖
    st.complete_confirm(intent_id=intent.intent_id, result_sha256="e" * 64,
                        outcome=ST.OUTCOME_OK)
    with pytest.raises(ST.StateError) as exc:
        st.complete_confirm(intent_id=intent.intent_id, result_sha256="f" * 64,
                            outcome=ST.OUTCOME_OK)
    assert exc.value.code == "INTENT_ALREADY_CLOSED"

    with pytest.raises(ST.StateError) as exc:
        st.publish_confirm(intent_id=intent.intent_id, manifest_sha256="e" * 64,
                           validated=False)
    assert exc.value.code == "NOT_VALIDATED"

    assert st.publish_confirm(intent_id=intent.intent_id,
                              manifest_sha256="e" * 64, validated=True) is True
    assert st.study_state("A") == ST.ST_RESULTS_RELEASED
    n = len(st._events())
    assert st.publish_confirm(intent_id=intent.intent_id,
                              manifest_sha256="0" * 64, validated=True) is True
    assert len(st._events()) == n, "重复发布不得写第二行"
    assert st.study_status("A")["published_manifest_sha256"] == "e" * 64


def test_infra_failure_blocks_whole_batch_without_backfilling(tmp_path: Path) -> None:
    st, lock, _ = _frozen_study(tmp_path, study="B")
    intent = st.begin_confirm(study="B", token=lock.token, specs_sha256="c" * 64)
    st.complete_confirm(intent_id=intent.intent_id, result_sha256="e" * 64,
                        outcome=ST.OUTCOME_INFRA_FAIL)
    assert st.study_state("B") == ST.ST_BLOCKED
    with pytest.raises(ST.StateError) as exc:
        st.publish_confirm(intent_id=intent.intent_id, manifest_sha256="e" * 64,
                           validated=True)
    assert exc.value.code == "CONFIRM_BLOCKED"


def test_recovery_never_reruns(tmp_path: Path) -> None:
    st, lock, _ = _frozen_study(tmp_path)
    assert st.recover("A").mode == ST.RECOVERY_IDLE

    intent = st.begin_confirm(study="A", token=lock.token, specs_sha256="c" * 64)
    # 结果未知：STOP_UNKNOWN，绝不自动重跑
    assert st.recover("A").mode == ST.RECOVERY_STOP_UNKNOWN

    # 有可信结果：只做幂等发布
    st.complete_confirm(intent_id=intent.intent_id, result_sha256="e" * 64,
                        outcome=ST.OUTCOME_OK)
    decision = st.recover("A")
    assert decision.mode == ST.RECOVERY_RESUME_PUBLISH
    assert decision.intent_id == intent.intent_id


def test_recovery_accepts_trusted_result_without_rerun(tmp_path: Path) -> None:
    st, lock, _ = _frozen_study(tmp_path)
    st.begin_confirm(study="A", token=lock.token, specs_sha256="c" * 64)
    decision = st.recover("A", trusted_result_sha256="d" * 64)
    assert decision.mode == ST.RECOVERY_RESUME_PUBLISH


# ---------------------------------------------------------------------------
# 提交与终局
# ---------------------------------------------------------------------------

def _released(tmp_path: Path, *, study: str = "A") -> ST.ResearchState:
    st, lock, _ = _frozen_study(tmp_path, study=study)
    intent = st.begin_confirm(study=study, token=lock.token, specs_sha256="c" * 64)
    st.complete_confirm(intent_id=intent.intent_id, result_sha256="e" * 64,
                        outcome=ST.OUTCOME_OK)
    st.publish_confirm(intent_id=intent.intent_id, manifest_sha256="e" * 64,
                       validated=True)
    return st


CLAIM = {"hypothesis_id": "h1", "proposition": "decrease_gt",
         "metric_id": "displacement_x_m", "delta": 0.001,
         "estimate": 0.02, "lower": 0.01, "upper": 0.03}


def test_submit_claim_requires_released_state_and_bounded_report(tmp_path: Path) -> None:
    st, lock, _ = _frozen_study(tmp_path)
    st.record_reply(response_id="r-sub", text="{}", phase="confirm")
    with pytest.raises(ST.StateError) as exc:
        st.submit_claim(study="A", claims=[CLAIM], report_text="x",
                        response_id="r-sub")
    assert exc.value.code == "CLAIM_STATE"

    st = _released(tmp_path / "second")
    st.record_reply(response_id="r-sub", text="{}", phase="confirm")
    with pytest.raises(ST.StateError) as exc:
        st.submit_claim(study="A", claims=[CLAIM], report_text="  ",
                        response_id="r-sub")
    assert exc.value.code == "REPORT_MISSING"
    with pytest.raises(ST.StateError) as exc:
        st.submit_claim(study="A", claims=[CLAIM],
                        report_text="x" * (ST.REPORT_MAX_BYTES + 1),
                        response_id="r-sub")
    assert exc.value.code == "REPORT_TOO_LARGE"
    with pytest.raises(ST.StateError) as exc:
        st.submit_claim(study="A", claims=[CLAIM], report_text="ok",
                        response_id="ghost")
    assert exc.value.code == "UNKNOWN_REPLY"

    out = st.submit_claim(study="A", claims=[CLAIM],
                          report_text="报告正文" * 10, response_id="r-sub")
    assert st.study_state("A") == ST.ST_CLAIM_SUBMITTED
    packet = (tmp_path / "second" / "research_packets" / "A"
              / f"{out['report_artifact_id']}.txt")
    assert packet.read_text(encoding="utf-8") == "报告正文" * 10


def test_verdict_only_from_judge_and_only_from_claim_submitted(tmp_path: Path) -> None:
    st = _released(tmp_path)
    with pytest.raises(ST.StateError) as exc:
        st.conclude("A", ST.ST_VERIFIED, reason="premature")
    assert exc.value.code == "CONCLUSION_STATE"

    st.record_reply(response_id="r-sub", text="{}", phase="confirm")
    st.submit_claim(study="A", claims=[CLAIM], report_text="ok",
                    response_id="r-sub")
    with pytest.raises(ST.StateError) as exc:
        st.conclude("A", ST.ST_BLOCKED, reason="judge cannot say that")
    assert exc.value.code == "UNKNOWN_VERDICT"

    st.conclude("A", ST.ST_VALID_FAIL, reason="delta drift")
    assert st.study_state("A") == ST.ST_VALID_FAIL


def test_cancel_is_recorded_once_and_keeps_denominator(tmp_path: Path) -> None:
    st = _state(tmp_path)
    st.cancel_study("B", reason="budget reallocation")
    st.cancel_study("B", reason="duplicate call")
    cancelled = [ev for ev in st._events() if ev.event == "study_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "budget reallocation"


# ---------------------------------------------------------------------------
# 日志完整性
# ---------------------------------------------------------------------------

def test_journal_is_append_only_and_tamper_evident(tmp_path: Path) -> None:
    st = _state(tmp_path)
    st.advance_root(ST.ROOT_AUTHORIZED, reason="t")
    path = tmp_path / "state" / "journal.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    doc = json.loads(lines[0])
    doc["payload"]["to"] = ST.ROOT_CLOSED
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    with pytest.raises(ST.StateError) as exc:
        ST.ResearchState(make_ctx(tmp_path))
    assert exc.value.code == "JOURNAL_TAMPERED"


def test_journal_detects_broken_chain(tmp_path: Path) -> None:
    st = _state(tmp_path)
    st.advance_root(ST.ROOT_AUTHORIZED, reason="a")
    st.advance_root(ST.ROOT_CALIBRATED, reason="b")
    path = tmp_path / "state" / "journal.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[1], lines[0]]) + "\n", encoding="utf-8")
    with pytest.raises(ST.StateError) as exc:
        ST.ResearchState(make_ctx(tmp_path))
    assert exc.value.code == "JOURNAL_TAMPERED"


def test_snapshot_reports_both_studies(tmp_path: Path) -> None:
    st, _, _ = _frozen_study(tmp_path)
    snap = st.snapshot()
    assert snap["schema"] == ST.SNAPSHOT_SCHEMA
    assert set(snap["studies"]) == {"A", "B"}
    assert snap["studies"]["A"]["state"] == ST.ST_CONFIRM_LOCKED
    assert snap["studies"]["B"]["state"] == ST.ST_EXPLORING
    assert snap["root_state"] == ST.ROOT_INIT
    json.dumps(snap, ensure_ascii=False, allow_nan=False)


def test_append_once_deduplicates_under_concurrency(tmp_path: Path) -> None:
    """CR-55 回归：查重与追加必须在同一 flock 临界区。

    旧实现先在锁外扫 ``events()`` 再调 ``append``（在锁内重新读），两个进程
    （此处用线程 + 独立 fd 同构）可同时看不到对方而各追加一行同键事件。
    """
    st = _state(tmp_path)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        st.journal.append_once("gate2", {"card_id": "c-1"}, key="card_id")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = [e for e in st.journal.events() if e.event == "gate2"]
    assert len(events) == 1
