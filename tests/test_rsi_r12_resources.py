"""RSI-R1.2 T1：资源账（Plan §2.1；AC12/13/14/16/57）。

全部离线：资源账只是 append-only JSONL + flock + prev-sha 链，
不触发任何 backend / 容器 / 网络。矩阵形状由 ``experiments.validate_matrix``
单一实现裁决，本文件只验证**累计**帽、预留语义与公开快照精度。
"""
from __future__ import annotations

import copy
import json
import os
import stat
import threading

import pytest

from orchestrator.rsi_r1 import contracts as C
from orchestrator.rsi_r1 import experiments as E
from orchestrator.rsi_r1 import profiles as P
from orchestrator.rsi_r1 import resources as RS
from tests.rsi_r1_helpers import doc_of, plan_doc

A_PARAMS = dict(mass_kg="0.5", v0_mps="0.4", sliding_friction="0.4",
                torsional_friction_m="0.01")


def _ledger(tmp_path) -> RS.ResourceLedger:
    return RS.ResourceLedger(tmp_path / "res")


def _specs(n: int, nsteps: int = 100):
    import types
    return [types.SimpleNamespace(nsteps=nsteps) for _ in range(n)]


def _plan(study: str = "A") -> C.ResearchPlan:
    return doc_of(plan_doc(study, phase="explore"))


def _cell(cid: str, block: str) -> dict:
    return {"condition_id": cid, "block_id": block,
            "params": copy.deepcopy(A_PARAMS)}


def _matrix(plan: C.ResearchPlan, cells) -> dict:
    return {"schema": E.MATRIX_SCHEMA, "schema_version": 1,
            "study_id": plan.study_id, "plan_sha": plan.sha256,
            "cells": list(cells)}


# ---------------------------------------------------------------------------
# AC12：63/64 边界通过；65 / 96 在前执行拒绝，物理 run 恒 0
# ---------------------------------------------------------------------------

def test_explore_cumulative_63_and_64_pass_65_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(63), batch_id="b1")
    snap = led.snapshot("A")
    assert snap["explore"]["used"] == 63
    assert snap["explore"]["remaining"] == 1
    led.reserve_explore("A", _specs(1), batch_id="b2")
    assert led.snapshot("A")["explore"]["remaining"] == 0
    before = led.verify()["rows"]
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_explore("A", _specs(1), batch_id="b3")
    assert exc.value.code == "RESOURCE_EXCEEDED"
    # 拒绝不落行、不产生任何物理 run
    assert led.verify()["rows"] == before
    assert led.snapshot("A")["explore"]["used"] == 64


def test_explore_96_refused_without_truncation_or_selection(tmp_path) -> None:
    led = _ledger(tmp_path)
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_explore("A", _specs(96), batch_id="b1")
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert "不截断" in str(exc.value)
    assert led.verify()["rows"] == 0
    assert led.snapshot("A")["explore"]["used"] == 0


def test_validate_matrix_v2_96_cells_refused_before_physical_run(tmp_path) -> None:
    led = _ledger(tmp_path)
    plan = _plan()
    cells = [_cell("base", f"b{i:02d}") for i in range(96)]
    with pytest.raises(E.ExperimentError) as exc:
        RS.validate_matrix_v2(_matrix(plan, cells), plan, ledger=led, study="A",
                              batch_id="b1")
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert led.verify()["rows"] == 0


def test_validate_matrix_v2_65_cells_refused_before_physical_run(tmp_path) -> None:
    led = _ledger(tmp_path)
    plan = _plan()
    cells = [_cell("base", f"b{i:02d}") for i in range(65)]
    with pytest.raises(E.ExperimentError) as exc:
        RS.validate_matrix_v2(_matrix(plan, cells), plan, ledger=led, study="A",
                              batch_id="b1")
    # 逐 cell 形状闸取 min(64, 累计余量) —— 65 在任何预留前即拒
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert led.verify()["rows"] == 0


def test_validate_matrix_v2_reserves_exactly_once_on_success(tmp_path) -> None:
    led = _ledger(tmp_path)
    plan = _plan()
    cells = [_cell("base", f"b{i:02d}") for i in range(4)]
    specs, reservation = RS.validate_matrix_v2(_matrix(plan, cells), plan,
                                               ledger=led, study="A",
                                               batch_id="b1")
    assert len(specs) == 4
    assert reservation.runs == 4
    snap = led.snapshot("A")
    assert snap["explore"]["used"] == 4
    assert snap["explore"]["batches_used"] == 1


# ---------------------------------------------------------------------------
# AC13：第 9 个 batch 即使有 run 余额也拒；预留反映在 remaining
# ---------------------------------------------------------------------------

def test_ninth_batch_rejected_even_with_run_budget_left(tmp_path) -> None:
    led = _ledger(tmp_path)
    for i in range(8):
        led.reserve_explore("A", _specs(1), batch_id=f"b{i}")
    snap = led.snapshot("A")
    assert snap["explore"]["used"] == 8
    assert snap["explore"]["remaining"] == 56
    assert snap["explore"]["batches_remaining"] == 0
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_explore("A", _specs(1), batch_id="b8")
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert "即使仍有 56 run 余额也拒绝" in str(exc.value)
    # 同 batch_id 内追加矩阵不算新 batch
    led.reserve_explore("A", _specs(1), batch_id="b0")
    assert led.snapshot("A")["explore"]["batches_used"] == 8


def test_unknown_and_rejected_reservations_do_not_leak_into_used(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(2), batch_id="b1")
    with pytest.raises(RS.ResourceError):
        led.reserve_explore("A", _specs(99), batch_id="b2")
    snap = led.snapshot("A")
    assert snap["explore"]["used"] == 2
    # 其他 study 的预留不影响本 study 的 used
    led.reserve_explore("B", _specs(5), batch_id="b1")
    assert led.snapshot("A")["explore"]["used"] == 2
    assert led.snapshot("B")["explore"]["used"] == 5


# ---------------------------------------------------------------------------
# AC14：探索不强制 32 block；确认恰 32 block、≤6 条件、≤192 run
# ---------------------------------------------------------------------------

def test_explore_has_no_block_requirement(tmp_path) -> None:
    led = _ledger(tmp_path)
    plan = _plan()
    cells = [_cell("base", f"b{i:02d}") for i in range(4)]
    specs, _ = RS.validate_matrix_v2(_matrix(plan, cells), plan, ledger=led,
                                     study="A", batch_id="b1")
    assert len(specs) == 4  # 与确认的 32 block 无关


def test_confirm_shape_exact_32_blocks(tmp_path) -> None:
    led = _ledger(tmp_path)
    assert led.check_confirm_shape(blocks=32, n_conditions=1) == 32
    assert led.check_confirm_shape(blocks=32, n_conditions=6) == 192
    with pytest.raises(RS.ResourceError) as exc:
        led.check_confirm_shape(blocks=31, n_conditions=1)
    assert exc.value.code == "CONFIRM_BLOCK_COUNT"
    with pytest.raises(RS.ResourceError) as exc:
        led.check_confirm_shape(blocks=32, n_conditions=7)
    assert exc.value.code == "MAX_CONDITIONS"
    with pytest.raises(RS.ResourceError) as exc:
        led.check_confirm_shape(blocks=32, n_conditions=2, runs=63)
    assert exc.value.code == "RESOURCE_EXCEEDED"


# ---------------------------------------------------------------------------
# AC16：freeze 原子预留 confirm+replay、POST 余量门、consume 精确扣减
# ---------------------------------------------------------------------------

def test_freeze_reserves_confirm_and_replay_then_consume_deducts(tmp_path) -> None:
    led = _ledger(tmp_path)
    fr = led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=1,
                            replay_runs=32, confirm_steps=32 * 8000,
                            replay_steps=32 * 8000,
                            posts_remaining_after_charge=8)
    assert fr.confirm_runs == 32 and fr.replay_runs == 32
    assert fr.post_margin == 8
    snap = led.snapshot("A")
    assert snap["confirm"] == {**snap["confirm"], "used": 0, "reserved": 32,
                               "remaining": 160}
    assert snap["replay"]["used"] == 0 and snap["replay"]["reserved"] == 32
    led.consume_confirm("A", kind="confirm", runs=20, steps=20 * 8000)
    snap = led.snapshot("A")
    assert snap["confirm"]["used"] == 20
    assert snap["confirm"]["reserved"] == 12
    assert snap["confirm"]["remaining"] == 192 - 32
    # freeze 预留 confirm+replay 共 2×32×8000；consume 20×8000 后余 12+32 份
    assert snap["steps"]["reserved"] == (12 + 32) * 8000
    led.consume_confirm("A", kind="replay", runs=32, steps=32 * 8000)
    snap = led.snapshot("A")
    assert snap["replay"]["used"] == 32 and snap["replay"]["reserved"] == 0


def test_consume_more_than_reserved_refused(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=1,
                       replay_runs=0, confirm_steps=0, replay_steps=0,
                       posts_remaining_after_charge=8)
    with pytest.raises(RS.ResourceError) as exc:
        led.consume_confirm("A", kind="confirm", runs=33, steps=0)
    assert exc.value.code == "RESOURCE_EXCEEDED"
    with pytest.raises(RS.ResourceError):  # 无冻结时不能凭空消费
        led.consume_confirm("A", kind="replay", runs=1, steps=0)


def test_freeze_post_margin_gate_no_lane_borrowing(tmp_path) -> None:
    led = _ledger(tmp_path)
    # study lane：扣当次 POST 后余 7 < 8 → 拒（不得借 probe/practice 余额）
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=1,
                           replay_runs=32, confirm_steps=0, replay_steps=0,
                           posts_remaining_after_charge=7)
    assert exc.value.code == "BUDGET_UNREACHABLE"
    # probe/practice lane：门是 2
    led.reserve_freeze("P", lane="probe", blocks=32, n_conditions=1,
                       replay_runs=32, confirm_steps=0, replay_steps=0,
                       posts_remaining_after_charge=2)
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_freeze("P2", lane="probe", blocks=32, n_conditions=1,
                           replay_runs=32, confirm_steps=0, replay_steps=0,
                           posts_remaining_after_charge=1)
    assert exc.value.code == "BUDGET_UNREACHABLE"
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_freeze("A", lane="nope", blocks=32, n_conditions=1,
                           replay_runs=0, confirm_steps=0, replay_steps=0,
                           posts_remaining_after_charge=8)
    assert exc.value.code == "LANE_UNKNOWN"


def test_freeze_step_limits(tmp_path) -> None:
    led = _ledger(tmp_path)
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=1,
                           replay_runs=32, confirm_steps=32 * 8000 + 1,
                           replay_steps=0, posts_remaining_after_charge=8)
    assert exc.value.code == "STEP_LIMIT"
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_explore("A", _specs(1, nsteps=8001))
    assert exc.value.code == "STEP_LIMIT"


# ---------------------------------------------------------------------------
# 沙盒：被前执行拒绝的设计调用也计数
# ---------------------------------------------------------------------------

def test_sandbox_cap_counts_rejected_pre_execution(tmp_path) -> None:
    led = _ledger(tmp_path)
    for i in range(32):
        led.reserve_sandbox("A", purpose=f"t{i}")
    snap = led.snapshot("A")
    assert snap["sandbox"]["used"] == 32 and snap["sandbox"]["remaining"] == 0
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_sandbox("A", purpose="gate-refused",
                            rejected_before_execution=True)
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert led.snapshot("A")["sandbox"]["used"] == 32


# ---------------------------------------------------------------------------
# 墙钟
# ---------------------------------------------------------------------------

def test_wallclock_per_study_and_global_caps(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.record_wallclock("A", active_s=7200, container_s=43200, gpu_s=14400)
    with pytest.raises(RS.ResourceError):
        led.record_wallclock("A", active_s=1)
    # 容器/GPU 是整轮合计：别的 study 再记也超
    with pytest.raises(RS.ResourceError):
        led.record_wallclock("B", container_s=1)
    with pytest.raises(RS.ResourceError):
        led.record_wallclock("B", gpu_s=1)
    # 活跃是 per-study：B 仍有全额
    led.record_wallclock("B", active_s=7200)
    snap = led.snapshot("B")
    assert snap["wallclock"]["active_remaining_s"] == 0
    assert snap["wallclock"]["container_remaining_s"] == 0


# ---------------------------------------------------------------------------
# AC57：aux 五子信封逐 spec 与合计 = AUX_SUBENVELOPE_TOTAL（单一真值）
# ---------------------------------------------------------------------------

def test_aux_subenvelopes_per_spec_and_total(tmp_path) -> None:
    led = _ledger(tmp_path)
    assert sum(P.AUX_SUBENVELOPES.values()) == P.AUX_SUBENVELOPE_TOTAL
    for name, cap in P.AUX_SUBENVELOPES.items():
        led.reserve_aux("A", envelope=name, steps=cap)
        with pytest.raises(RS.ResourceError):
            led.reserve_aux("A", envelope=name, steps=1)
    snap = led.snapshot("A")
    assert snap["aux"]["total"]["used"] == P.AUX_SUBENVELOPE_TOTAL
    assert snap["aux"]["total"]["remaining"] == 0
    with pytest.raises(RS.ResourceError):
        led.reserve_aux("A", envelope="unknown", steps=1)


# ---------------------------------------------------------------------------
# 账本身份：防篡改、权限、并发
# ---------------------------------------------------------------------------

def test_ledger_tamper_detected(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(2), batch_id="b1")
    path = led.path
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row["runs"] = 1  # 改内容不改 sha
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    with pytest.raises(RS.ResourceError) as exc:
        led.rows()
    assert exc.value.code == "RESOURCE_LEDGER_TAMPERED"


def test_ledger_permissions(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(1), batch_id="b1")
    d = stat.S_IMODE(os.stat(led.dir).st_mode)
    f = stat.S_IMODE(os.stat(led.path).st_mode)
    assert d == 0o700
    assert f == 0o600


def test_ledger_concurrent_reservations_are_exact(tmp_path) -> None:
    led = _ledger(tmp_path)
    errors: "list[BaseException]" = []

    def worker(i: int) -> None:
        try:
            led.reserve_explore("A", _specs(4), batch_id=f"b{i}")
        except BaseException as exc:  # pragma: no cover - 失败即断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    snap = led.snapshot("A")
    assert snap["explore"]["used"] == 32
    assert snap["explore"]["batches_used"] == 8
    info = led.verify()
    assert info["rows"] == 8
    assert len(info["head_sha256"]) == 64


# ---------------------------------------------------------------------------
# typed limits 单源投影
# ---------------------------------------------------------------------------

def test_limits_document_is_single_source_projection() -> None:
    doc = RS.limits_document()
    limits = P.PROFILE_R12_DEF.resource_limits
    assert doc["limits"] == limits.to_dict()
    assert doc["limits"]["explore_runs_per_study"] == 64
    assert doc["limits"]["confirm_blocks"] == 32
    assert doc["limits"]["confirm_max_conditions"] == 6
    assert doc["limits"]["confirm_runs_per_study"] == 192
    assert doc["limits"]["replay_runs_per_study"] == 192
    assert doc["total_posts"] == P.PROFILE_R12_DEF.hard_cap == 124
    assert doc["family_cap"] == P.FAMILY_CAP == 293
    assert doc["aux_total"] == P.AUX_SUBENVELOPE_TOTAL
    assert doc["confirm_post_margin"] == 8
    assert doc["protocol_post_margin"] == 2
    assert len(doc["limits_sha256"]) == 64
    assert doc["lane_post_caps"] == {lane: P.PROFILE_R12_DEF.lane_post_caps[lane]
                                     for lane in P.PROFILE_R12_DEF.lane_order}


# ---------------------------------------------------------------------------
# T1 装配接缝：r12 分派器必须挂同一资源账，可见面为 V2（快照 + 动态 SHA）
# ---------------------------------------------------------------------------

def _fake_dispatcher(led, tmp_path):
    import types
    from orchestrator.rsi_r1 import assembly as A
    state = types.SimpleNamespace(
        study_status=lambda study: {
            "state": "EXPLORING", "artifacts": {"analysis": ["a" * 64]},
            "confirm_lock": None},
        journal=types.SimpleNamespace(events=lambda: []))
    admission = types.SimpleNamespace(
        lane="study_A", posts_used=lambda lane: 3)
    budget = types.SimpleNamespace(posts_by_lane={"study_A": 48})
    ctx = types.SimpleNamespace(exists=lambda rel: False)
    return A.CampaignDispatcher(
        ctx=ctx, state=state, admission=admission, budget=budget,
        study="A", runner=None, image=None, backend_factory=None,
        profile=P.PROFILE_R12_DEF, resource_ledger=led)


def test_dispatcher_requires_ledger_for_r12(tmp_path) -> None:
    from orchestrator.rsi_r1 import assembly as A
    with pytest.raises(A.AssemblyError) as exc:
        A.CampaignDispatcher(ctx=tmp_path, state=None, admission=None,
                             budget=None, study="A", runner=None, image=None,
                             backend_factory=None, profile=P.PROFILE_R12_DEF)
    assert exc.value.code == "RESOURCE_LEDGER_MISSING"
    # 旧 profile 不得挂 R1.2 账（两向 fail-closed）
    led = _ledger(tmp_path)
    with pytest.raises(A.AssemblyError) as exc:
        A.CampaignDispatcher(ctx=tmp_path, state=None, admission=None,
                             budget=None, study="A", runner=None, image=None,
                             backend_factory=None, profile=P.PROFILE_LEGACY_DEF,
                             resource_ledger=led)
    assert exc.value.code == "PROFILE_MISMATCH"


def test_dispatcher_visible_state_v2_uses_snapshot(tmp_path) -> None:
    led = _ledger(tmp_path)
    led.reserve_explore("A", _specs(6), batch_id="b1")
    disp = _fake_dispatcher(led, tmp_path)
    view = disp.visible_state()
    public = view.as_public()
    assert public["phase"] == "explore"
    assert public["resources"]["explore"]["used"] == 6
    assert public["resources"]["explore"]["remaining"] == 58
    assert public["latest_sha"]["analysis_source_sha256"] == "a" * 64
    assert public["latest_sha"]["nonce"] and len(public["latest_sha"]["nonce"]) == 64
    # 旧 8 键仍在（精确 public view 是**加法**）
    for key in ("study", "state", "artifacts", "last_batch",
                "budget_slots_remaining", "confirm_token", "plan", "nonce"):
        assert key in public


def test_toy_lanes_do_not_consume_study_science_quotas(tmp_path) -> None:
    """probe/practice 的 toy 演练按 lane 分域，不挤占 study 科学配额。

    2026-11-12 T5 全量演练实测：probe（study A）+ practice（study A/B）各做
    一次 32×3 toy 确认，三次 96 run 全记在 study A/B 名下 ⇒ 正式 study A 的
    「确认 96 run > 余量 0（帽 192）」拒冻结（RESOURCE_EXCEEDED/R1-S5-04）。
    冻结合同要求确认恰 32 block（toy 链不能缩水），toy 物理成本归 aux 子信封
    ``offline_probe_practice_recovery``；科学 run 帽只约束 study lane。
    """
    led = _ledger(tmp_path)
    steps = 32 * 3 * 8000          # A：96 run × 8000 step（宿主固定 dt=0.0005）
    for lane, study in (("probe", "A"), ("practice", "A"), ("practice", "B")):
        led.reserve_explore(study, _specs(6, 8000), batch_id=f"{lane}-{study}",
                            lane=lane)
        led.reserve_freeze(study, lane=lane, blocks=32, n_conditions=3,
                           replay_runs=96, confirm_steps=steps,
                           replay_steps=steps, posts_remaining_after_charge=2)
    # 科学配额未被 toy 演练吃掉：study A 仍可整额冻结一次确认 + 复跑
    res = led.reserve_freeze("A", lane="study_A", blocks=32, n_conditions=3,
                             replay_runs=96, confirm_steps=steps,
                             replay_steps=steps, posts_remaining_after_charge=8)
    assert res.confirm_runs == 96 and res.replay_runs == 96
    snap_study = led.snapshot("A", lane="study_A")
    snap_toy = led.snapshot("A", lane="probe")
    assert snap_study["confirm"]["reserved"] == 96
    assert snap_study["confirm"]["used"] == 0
    assert snap_toy["confirm"]["reserved"] == 96
    # 整轮 study run 帽只数 study lane（toy 的 192×3 不计入）
    snap_any = led.snapshot("A")
    assert snap_any["runs_total"]["used"] + snap_any["runs_total"]["reserved"] == 192


def test_toy_freeze_steps_capped_by_aux_subenvelope(tmp_path) -> None:
    """toy lane 的**物理**步数由 aux 子信封兜底：超帽在执行前拒绝。

    toy 物理成本 2,448,000 = 3 × (48,000 探索 + 768,000 确认)，恰好塞满
    ``offline_probe_practice_recovery``；第 4 条 toy 链（probe/study B）的
    确认预留会被子信封帽拒绝——即便它自己在 per-lane/per-study 帽内。
    """
    led = _ledger(tmp_path)
    cap = int(P.AUX_SUBENVELOPES["offline_probe_practice_recovery"])
    steps = 32 * 3 * 8000
    assert cap == 3 * (steps + 6 * 8000)          # 先钉死这笔算术
    for lane, study in (("probe", "A"), ("practice", "A"), ("practice", "B")):
        led.reserve_explore(study, _specs(6, 8000), batch_id=f"{lane}-{study}",
                            lane=lane)
        led.reserve_freeze(study, lane=lane, blocks=32, n_conditions=3,
                           replay_runs=96, confirm_steps=steps,
                           replay_steps=steps, posts_remaining_after_charge=2)
    with pytest.raises(RS.ResourceError) as exc:
        led.reserve_freeze("B", lane="probe", blocks=32, n_conditions=3,
                           replay_runs=96, confirm_steps=steps,
                           replay_steps=steps, posts_remaining_after_charge=2)
    assert exc.value.code == "RESOURCE_EXCEEDED"
    assert "offline_probe_practice_recovery" in str(exc.value)
