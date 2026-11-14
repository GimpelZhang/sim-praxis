"""RSI-R1.2 T1：公开资源合同与最终请求审计（Plan §2.1/§2.2；AC10/11/15/16）。

全部离线：审计只读请求字节与资源账快照，transport 是内存假实现。
要点：合同块逐字嵌入请求；删除/篡改/旧版混入/私有点位一律**发送前**拒绝；
动态 SHA 必须来自真实公开结果（不得预填）；审计件按 request_sha 命名落盘。
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from orchestrator.common import research_transport as T
from orchestrator.rsi_r1 import loop as L
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import public_request as PR
from orchestrator.rsi_r1 import resources as RS

PROFILE = P.PROFILE_R12_DEF
PLAN_SHA = "1" * 64
NONCE = "n-1"


def _doc(envelope_id: str = "env-1") -> dict:
    return P.public_contract_document(PROFILE, envelope_id=envelope_id,
                                      budget_sha256="a" * 64)


def _ledger(tmp_path) -> RS.ResourceLedger:
    return RS.ResourceLedger(tmp_path / "res")


def _specs(n: int):
    import types
    return [types.SimpleNamespace(nsteps=100) for _ in range(n)]


def _dynamic(**over) -> dict:
    dyn = {"plan_sha256": PLAN_SHA, "analysis_source_sha256": None,
           "confirm_manifest_sha256": None, "nonce": NONCE}
    dyn.update(over)
    return dyn


def _brief(led: RS.ResourceLedger, doc: dict, *, study: str = "A",
           phase: str = "explore", resources: "dict | None" = None,
           refs: "dict | None" = None, dyn: "dict | None" = None) -> str:
    dyn = dyn if dyn is not None else _dynamic()
    snap = resources if resources is not None else led.snapshot(study, phase=phase)
    state = L.make_visible_state_v2(
        study=study, study_state="explore", artifacts={}, last_batch=None,
        budget_slots_remaining=20, confirm_token=None, phase=phase,
        resources=snap, latest_sha=dyn,
        references=refs if refs is not None else {"plan": PLAN_SHA,
                                                  "nonce": NONCE})
    return PR.task_brief_with_contract(state, doc)


def _request(brief: str) -> T.Request:
    return T.build_request(system=L.render_system(), task_brief=brief, turns=(),
                           counter=lambda s: len(s) // 4)


def _audit(brief: str, doc: dict, *, dyn: "dict | None" = None, **kw):
    return PR.audit_request(_request(brief), profile=PROFILE, contract_doc=doc,
                            phase=kw.pop("phase", "explore"),
                            expected_dynamic=dyn if dyn is not None else _dynamic(),
                            **kw)


# ---------------------------------------------------------------------------
# AC10：期望值只从 typed limits / profile 派生（同源，不抄字面量）
# ---------------------------------------------------------------------------

def test_expected_values_match_typed_limits_single_source() -> None:
    ev = PR.expected_values(PROFILE)
    limits = PROFILE.resource_limits
    assert ev["explore_runs"] == limits.explore_runs_per_study == 64
    assert ev["explore_batches"] == limits.explore_batches_per_study == 8
    assert ev["confirm_blocks"] == limits.confirm_blocks == 32
    assert ev["confirm_conditions"] == limits.confirm_max_conditions == 6
    assert ev["confirm_runs"] == limits.confirm_runs_per_study == 192
    assert ev["replay_runs"] == limits.replay_runs_per_study == 192
    assert ev["total_runs"] == limits.total_runs
    assert ev["total_steps"] == limits.total_steps
    assert ev["steps_per_run"] == limits.steps_per_run == 8000
    assert ev["sandbox"] == limits.sandbox_executions_per_study == 32
    assert ev["active_s"] == limits.active_wallclock_s == 7200
    assert ev["container_s"] == limits.container_wallclock_s == 43200
    assert ev["gpu_s"] == limits.gpu_wallclock_s == 14400
    assert ev["request_timeout_s"] == limits.request_timeout_s
    assert ev["analysis_timeout_s"] == limits.analysis_timeout_s
    assert ev["physics_run_timeout_s"] == limits.physics_run_timeout_s
    assert ev["lane_caps"] == {lane: PROFILE.lane_post_caps[lane]
                               for lane in PROFILE.lane_order}
    assert ev["posts_total"] == PROFILE.hard_cap == 124
    assert ev["family_cap"] == PROFILE.family_cap == 475
    assert ev["family_history"] == PROFILE.family_history_posts == 31
    assert ev["aux_subenvelopes"] == dict(P.AUX_SUBENVELOPES)
    assert ev["aux_total"] == P.AUX_SUBENVELOPE_TOTAL
    # 合同文本由同一 profile 渲染：每个期望值都必须在文本里逐字出现
    text = PR.contract_text(_doc())
    assert f"累计上限: {ev['explore_runs']} run" in text
    assert f"batch 上限: {ev['explore_batches']} 个" in text
    assert f"确认: {ev['confirm_blocks']} 配对 block" in text
    assert f"最多 {ev['confirm_conditions']} 条件" in text
    assert f"复跑: ≤ {ev['replay_runs']} run" in text
    assert f"总量: {ev['total_runs']} run / {ev['total_steps']} step" in text
    assert f"每 run ≤ {ev['steps_per_run']} step" in text
    assert f"候选沙盒执行 ≤ {ev['sandbox']} 次" in text
    assert f"合计: {int(P.AUX_SUBENVELOPE_TOTAL)} step" in text


# ---------------------------------------------------------------------------
# 正控：合法请求通过，审计件按 request_sha 落盘（0600/0700）
# ---------------------------------------------------------------------------

def test_valid_request_audit_passes_and_writes_binding_artifact(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(10), batch_id="b1")
    doc = _doc()
    brief = _brief(led, doc)
    req = _request(brief)
    audit_dir = tmp_path / "audit"
    report = PR.audit_request(req, profile=PROFILE, contract_doc=doc,
                              phase="explore", expected_dynamic=_dynamic())
    assert report.ok
    assert report.code is None
    assert len(report.sha256) == 64
    snd = PR.AuditedSender(lambda r: "unused", audit_dir=audit_dir,
                           profile=PROFILE, contract_doc=doc,
                           phase="explore", expected_dynamic=_dynamic())
    out = snd._write(report)
    assert out.parent == audit_dir
    assert out.name == f"{report.doc['request_sha256']}.json"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(audit_dir).st_mode) == 0o700
    stored = json.loads(out.read_text())
    assert stored["request_sha256"] == report.doc["request_sha256"]
    assert stored["verdict"] == "ok"
    assert stored["hashes"]["contract_block_sha256"] == \
        report.doc["hashes"]["contract_block_sha256"]
    # 资源期望值来自 typed limits（validator 同源）
    assert stored["resource_values"]["explore_runs"] == 64
    assert stored["checks"] and all(c["ok"] for c in stored["checks"])


def test_valid_request_audit_doc_hashes_and_sources(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    brief = _brief(led, doc)
    req = _request(brief)
    report = _audit(brief, doc)
    assert report.doc["schema"] == PR.AUDIT_SCHEMA
    assert report.doc["profile_id"] == PROFILE.profile_id
    for key in ("serialized_sha256", "system_sha256", "task_sha256",
                "history_sha256", "contract_block_sha256", "state_sha256"):
        assert len(report.doc["hashes"][key]) == 64
    assert report.doc["request_sha256"] == report.doc["hashes"]["serialized_sha256"]
    assert report.doc["request_sha256"] == req.sha256
    assert report.doc["local_tokens"] == len(req.serialized) // 4
    sources = [str(s) for s in report.doc["sources"]]
    assert any(s.startswith("profile:") for s in sources)
    assert any(s.startswith("limits:") for s in sources)
    assert any(s.startswith("public_contract:") for s in sources)
    assert any(s.startswith("state:") for s in sources)
    assert report.doc["limits_sha256"] == \
        RS.limits_document(PROFILE)["limits_sha256"]
    assert len(report.doc["public_contract_sha256"]) == 64
    assert report.doc["forbidden"]["markers"] == list(PR.PRIVATE_MARKERS)


# ---------------------------------------------------------------------------
# AC11：删除 / 改值 / 旧版混入 / 裁剪必需条目 → 发送前拒绝
# ---------------------------------------------------------------------------

def test_deleted_cap_line_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    brief = _brief(led, doc)
    bad = brief.replace("- 累计上限: 64 run\n", "")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code in ("PUBLIC_CONTRACT_MISSING", "PUBLIC_CONTRACT_DRIFT")


def test_changed_cap_value_96_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    bad = _brief(led, doc).replace("- 累计上限: 64 run", "- 累计上限: 96 run")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_DRIFT"


def test_changed_cap_value_63_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    bad = _brief(led, doc).replace("- 累计上限: 64 run", "- 累计上限: 63 run")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_DRIFT"


def test_deleted_cumulative_rule_line_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    brief = _brief(led, doc)
    lines = [ln for ln in brief.splitlines()
             if not ln.startswith("- ") or "每 study 累计上限" not in ln]
    bad = "\n".join(lines)
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code in ("PUBLIC_CONTRACT_MISSING", "PUBLIC_CONTRACT_DRIFT")


def test_legacy_contract_header_mixed_in_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    bad = _brief(led, doc).replace("# RSI-R1.2 公开资源合同",
                                   "# RSI-R1.2 公开资源合同 v1（冻结旧版）")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code in ("PUBLIC_CONTRACT_DRIFT", "PUBLIC_CONTRACT_MISSING")


def test_whole_contract_block_missing_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    bad = _brief(led, doc).replace(PR.contract_block(doc), "")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_MISSING"


def test_required_sandbox_line_trimmed_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    brief = _brief(led, doc)
    bad = "\n".join(ln for ln in brief.splitlines()
                    if not ln.startswith("- 每 study 候选沙盒执行"))
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code in ("PUBLIC_CONTRACT_MISSING", "PUBLIC_CONTRACT_DRIFT")


# ---------------------------------------------------------------------------
# AC15：动态引用（plan/analysis/confirm/nonce）必须来自真实公开结果
# ---------------------------------------------------------------------------

def test_dynamic_ref_missing_field_refused() -> None:
    dyn = _dynamic()
    del dyn["plan_sha256"]
    with pytest.raises(P.ProfileError) as exc:
        L.make_visible_state_v2(
            study="A", study_state="explore", artifacts={}, last_batch=None,
            budget_slots_remaining=1, confirm_token=None, phase="explore",
            resources={"study": "A", "explore": {}, "confirm": {},
                       "replay": {}, "runs_total": {}, "steps": {},
                       "sandbox": {}, "wallclock": {}, "aux": {}},
            latest_sha=dyn, references={})
    assert exc.value.code == "DYNAMIC_REF_INVALID"


def test_dynamic_ref_non_hex_plan_refused() -> None:
    with pytest.raises(P.ProfileError) as exc:
        L.make_visible_state_v2(
            study="A", study_state="explore", artifacts={}, last_batch=None,
            budget_slots_remaining=1, confirm_token=None, phase="explore",
            resources={"study": "A", "explore": {}, "confirm": {},
                       "replay": {}, "runs_total": {}, "steps": {},
                       "sandbox": {}, "wallclock": {}, "aux": {}},
            latest_sha=_dynamic(), references={"plan": "not-a-sha"})
    assert exc.value.code == "DYNAMIC_REF_INVALID"


def test_wrong_plan_sha_refused_by_audit(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    dyn = _dynamic(plan_sha256="2" * 64)
    bad = _brief(led, doc, dyn=dyn, refs={"plan": "2" * 64, "nonce": NONCE})
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)  # 审计方持有的真实公开值是 1*64
    assert exc.value.code == "DYNAMIC_REF_INVALID"


def test_nonce_reference_must_match_latest_nonce(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    # 构造期即拒：references.nonce 与 latest_sha.nonce 不一致
    with pytest.raises(P.ProfileError) as exc:
        _brief(led, doc, refs={"plan": PLAN_SHA, "nonce": "n-2"})
    assert exc.value.code == "DYNAMIC_REF_INVALID"
    # 审计层独立兜底：直接篡改请求内 state JSON 的 references.nonce
    brief = _brief(led, doc)
    state_doc, tail = PR.parse_state_block(_request(brief))
    state_doc["references"]["nonce"] = "n-2"
    brief2 = (PR.STATE_HEADER + "\n"
              + json.dumps(state_doc, ensure_ascii=False, sort_keys=True, indent=1)
              + "\n\n" + tail)
    with pytest.raises(PR.AuditError) as exc2:
        _audit(brief2, doc)
    assert exc2.value.code == "DYNAMIC_REF_INVALID"


def test_phase_mismatch_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    bad = _brief(led, doc, phase="explore")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc, phase="confirm")
    assert exc.value.code == "PUBLIC_CONTRACT_DRIFT"


# ---------------------------------------------------------------------------
# 私有点位：canary / 固定标记
# ---------------------------------------------------------------------------

def test_private_canary_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    canary = "CANARY-PRIVATE-7f3a"
    bad = _brief(led, doc).replace("请给出你的下一个动作",
                                   f"{canary} 请给出你的下一个动作")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc, forbidden=(canary,))
    assert exc.value.code == "PUBLIC_CONTRACT_PRIVATE"
    assert canary not in str(exc.value)  # 报错不回显私有值


def test_fixed_private_markers_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    ok = _audit(_brief(led, doc), doc)
    assert ok.ok
    bad = _brief(led, doc).replace("请给出你的下一个动作",
                                   "hidden_seed=abc 请给出你的下一个动作")
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_PRIVATE"


# ---------------------------------------------------------------------------
# 可见状态 / resources 快照完整度
# ---------------------------------------------------------------------------

def test_state_block_missing_or_nonjson_refused() -> None:
    doc = _doc()
    bad = PR.STATE_HEADER + "\nnot json\n\n" + PR.contract_block(doc)
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_MISSING"


def test_resources_missing_reserved_field_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    snap = json.loads(json.dumps(led.snapshot("A")))
    del snap["confirm"]["reserved"]
    bad = _brief(led, doc, resources=snap)
    with pytest.raises(PR.AuditError) as exc:
        _audit(bad, doc)
    assert exc.value.code == "PUBLIC_CONTRACT_MISSING"


def test_resources_reflect_freeze_reservation_in_request(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=1,
                       replay_runs=32, confirm_steps=0, replay_steps=0,
                       posts_remaining_after_charge=8)
    led.consume_confirm("A", kind="confirm", runs=16, steps=0)
    doc = _doc()
    report = _audit(_brief(led, doc), doc)
    snap = report.doc["resource_values"]
    assert snap["confirm_runs"] == 192
    assert report.ok
    # 请求里逐字包含 used 16 / reserved 16 的精确快照
    brief = _brief(led, doc)
    state_doc, _tail = PR.parse_state_block(_request(brief))
    res = state_doc["resources"]
    assert res["confirm"]["used"] == 16
    assert res["confirm"]["reserved"] == 16
    assert res["replay"]["reserved"] == 32
    assert res["runs_total"]["reserved"] == 64


# ---------------------------------------------------------------------------
# AuditedSender：拒绝时绝不触达 transport
# ---------------------------------------------------------------------------

def test_audited_sender_success_once(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    calls: "list[T.Request]" = []
    snd = PR.AuditedSender(lambda r: calls.append(r) or "REPLY",
                           audit_dir=tmp_path / "audit", profile=PROFILE,
                           contract_doc=doc, phase="explore",
                           expected_dynamic=_dynamic())
    assert snd.send_once(_request(_brief(led, doc))) == "REPLY"
    assert len(calls) == 1


def test_audited_sender_refusal_never_calls_transport(tmp_path) -> None:
    led = _ledger(tmp_path)
    doc = _doc()
    calls: "list[T.Request]" = []
    snd = PR.AuditedSender(lambda r: calls.append(r) or "REPLY",
                           audit_dir=tmp_path / "audit", profile=PROFILE,
                           contract_doc=doc, phase="explore",
                           expected_dynamic=_dynamic())
    bad = _brief(led, doc).replace("- 累计上限: 64 run", "- 累计上限: 65 run")
    with pytest.raises(PR.AuditError) as exc:
        snd.send_once(_request(bad))
    assert exc.value.code == "PUBLIC_CONTRACT_DRIFT"
    assert calls == []
    # 拒绝件同样留审计取证（可追责）
    files = list((tmp_path / "audit").iterdir())
    assert len(files) == 1
    stored = json.loads(files[0].read_text())
    assert stored["verdict"] == "refused"
    assert stored["code"] == "PUBLIC_CONTRACT_DRIFT"


def test_recovery_invalidate_map_matches_engine_single_source():
    """D-5：公开合同的失效映射必须与 recovery.INVALIDATE_FROM 同源。"""
    from orchestrator.rsi_r1 import protocol as PT
    from orchestrator.rsi_r1 import recovery as RCV

    doc = P.public_contract_document(
        P.PROFILE_R12_DEF, envelope_id="env-x", budget_sha256="0" * 64,
        family_used_before=47)
    rec = doc["recovery"]
    names = P.CANONICAL_POSITIONS
    want = {names[pos]: list(names[first:])
            for pos, first in sorted(RCV.INVALIDATE_FROM.items())}
    assert rec["invalidate_on"] == want
    assert "必须重新 propose_plan" in rec["invalidate_rule"]


def test_prompt_discloses_invalidation_rule():
    """D-5：prompt 必须公开「改写 design ⇒ 重提 plan」的失效规则。"""
    from orchestrator.rsi_r1 import protocol as PT

    block = PT._ACTIONS_BLOCK
    assert "失效规则" in block
    for token in ("改写 design", "propose_plan", "改写 plan", "run_batch",
                  "改写 analysis", "analyze", "只烧预算"):
        assert token in block, token


def test_prompt_requires_bitwise_estimator_reuse():
    """2026-11-13 A 轮实测：必须逐字复用宿主估计量（差 1.4e-6 即拒）。"""
    from orchestrator.rsi_r1 import protocol as PT

    block = PT._ACTIONS_BLOCK
    assert "数值复现" in block
    for token in ("metric_value()", "interval()", "float64", "20261111",
                  "linear", "不要 round"):
        assert token in block, token


def test_study_brief_discloses_budget_discipline():
    """2026-11-14：B 曾在 48 呼内未收敛 ⇒ prompt 必须给预算纪律。"""
    from orchestrator.rsi_r1 import protocol as PT

    brief = PT.render_study_brief("B")
    assert "预算纪律" in brief
    for token in ("freeze_confirm", "剩余", "≥8", "冻结后禁止"):
        assert token in brief, token
