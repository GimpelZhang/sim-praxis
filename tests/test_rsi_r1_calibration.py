"""RSI-R1 T1b 校准（``calibration.py``）离线测试：合成 fixture oracle，不触网、不跑容器。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from orchestrator.rsi_r1 import calibration as CAL
from orchestrator.rsi_r1 import metrics as M
from orchestrator.rsi_r1 import rubric as R
from orchestrator.rsi_r1 import scenes as S

FIXTURE = Path(__file__).parent / "fixtures" / "rsi_r1" / "calibration_oracle_stub.py"
DT_A = 0.0005
DT_B = 0.001


# ---------------------------------------------------------------------------
# 假 campaign 上下文（只实现校准模块用到的面）
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(self, root: Path, envelope: str = "r1-test") -> None:
        self.root = Path(root)
        self.envelope_id = envelope

    def subdir(self, name: str) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def relative(self, relpath) -> Path:
        return self.root / relpath

    def write_text(self, relpath, text) -> Path:
        p = self.relative(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, relpath, obj) -> Path:
        return self.write_text(relpath,
                               json.dumps(obj, sort_keys=True, indent=1) + "\n")

    def read_json(self, relpath):
        return json.loads(self.relative(relpath).read_text(encoding="utf-8"))

    def exists(self, relpath) -> bool:
        return self.relative(relpath).is_file()


@pytest.fixture()
def ctx(tmp_path):
    return FakeCtx(tmp_path)


@pytest.fixture(scope="module")
def oracle():
    return CAL.load_oracle(FIXTURE)


# ---------------------------------------------------------------------------
# 矩阵
# ---------------------------------------------------------------------------

def test_matrix_has_at_least_8_blocks_per_study_and_unique_runs():
    entries = CAL.calibration_matrix()
    specs = CAL.specs_for_matrix(entries)
    assert len({s.run_id for s in specs}) == len(specs)
    a_blocks = {e["block_id"] for e in entries
                if e["study"] == "A" and e["role"] == "negative_base"}
    b_blocks = {e["block_id"] for e in entries if e["study"] == "B"}
    assert len(a_blocks) >= 8 and len(b_blocks) >= 8
    assert len([s for s in specs if s.study_id == "A"]) == 19
    assert len([s for s in specs if s.study_id == "B"]) == 9


def test_matrix_b_tc_over_dt_is_integer():
    for spec in CAL.specs_for_matrix(CAL.calibration_matrix()):
        if spec.study_id != "B":
            continue
        dt_us = spec.ticks["dt_ticks_us"]
        tc_us = spec.ticks["tc_ticks_us"]
        assert tc_us % dt_us == 0


def test_matrix_a_pairs_differ_only_in_torsional_friction():
    entries = CAL.calibration_matrix()
    by_block = {}
    for e in entries:
        if e["study"] == "A" and e["role"] in ("negative_base", "negative_sham"):
            by_block.setdefault(e["block_id"], {})[e["role"]] = e["params"]
    assert len(by_block) == 8
    for pair in by_block.values():
        changed = [k for k in pair["negative_base"]
                   if pair["negative_base"][k] != pair["negative_sham"][k]]
        assert changed == ["torsional_friction_m"]
        assert pair["negative_base"]["torsional_friction_m"] == CAL.A_BASE_TORSIONAL
        assert pair["negative_sham"]["torsional_friction_m"] == CAL.A_SHAM_TORSIONAL


def test_calibration_book_counts_a_settle_steps(tmp_path):
    spec_a = next(s for s in CAL.specs_for_matrix(CAL.calibration_matrix())
                  if s.study_id == "A")
    cap = spec_a.nsteps + CAL.P.A_SETTLE_STEPS - 1
    with pytest.raises(Exception) as exc:
        CAL.CalibrationBook(max_runs=10, max_steps=cap).reserve(spec_a)
    assert "RESOURCE_EXCEEDED" in str(exc.value)
    # 同帽下普通 ResourceBook 会放行——差异正是整定步
    from orchestrator.rsi_r1 import experiments as E
    E.ResourceBook(max_runs=10, max_steps=cap).reserve(spec_a)
    book = CAL.CalibrationBook(max_runs=10, max_steps=10_000_000)
    book.reserve(spec_a)
    assert book.used_steps == spec_a.nsteps + CAL.P.A_SETTLE_STEPS


# ---------------------------------------------------------------------------
# 预注册
# ---------------------------------------------------------------------------

def test_prereg_roundtrip_and_selfhash(ctx):
    doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE,
                                       registered_at_utc="2026-11-11T00:00:00Z")
    path = CAL.write_calibration_prereg(ctx, doc)
    assert path.name == "calibration_prereg.json"
    loaded = CAL.load_calibration_prereg(ctx)
    assert loaded["document_sha256"] == doc["document_sha256"]
    assert loaded["matrix"] == doc["matrix"]
    # 幂等复用
    CAL.write_calibration_prereg(ctx, doc)


def test_prereg_tamper_fails_selfhash(ctx):
    doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE,
                                       registered_at_utc="2026-11-11T00:00:00Z")
    CAL.write_calibration_prereg(ctx, doc)
    doc["matrix"][0]["nsteps"] += 1
    ctx.write_json(CAL.PREREG_RELPATH, doc)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_calibration_prereg(ctx)
    assert exc.value.code == "PREREG_SELFTEST"


def test_prereg_refuses_when_raw_already_exists(ctx):
    raw = ctx.subdir("observations") / "calibration_a"
    raw.mkdir(parents=True)
    (raw / "run-x.jsonl").write_text("{}\n", encoding="utf-8")
    doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.write_calibration_prereg(ctx, doc)
    assert exc.value.code == "RAW_BEFORE_PREREG"


def test_prereg_drift_detected(ctx):
    doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE)
    CAL.assert_prereg_matches_sources(doc, oracle_path=FIXTURE)
    doc["matrix"][0]["nsteps"] += 1
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.assert_prereg_matches_sources(doc, oracle_path=FIXTURE)
    assert exc.value.code == "MATRIX_DRIFT"


def test_prereg_refuses_conflicting_existing_file(ctx):
    doc = CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE)
    CAL.write_calibration_prereg(ctx, doc)
    other = dict(doc, purpose="另一份")
    other["document_sha256"] = CAL._document_sha(other)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.write_calibration_prereg(ctx, other)
    assert exc.value.code == "PREREG_DRIFT"


# ---------------------------------------------------------------------------
# oracle 边界
# ---------------------------------------------------------------------------

def test_oracle_load_missing(tmp_path):
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_oracle(tmp_path / "nope.py")
    assert exc.value.code == "ORACLE_MISSING"


def test_oracle_rejects_wrong_schema(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("ORACLE_SCHEMA='x'\ndef study_a_oracle(**k): pass\n"
                 "def study_b_rmse(**k): pass\n", encoding="utf-8")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_oracle(p)
    assert exc.value.code == "ORACLE_INTERFACE"


def test_oracle_must_live_in_private(tmp_path):
    ctx = FakeCtx(tmp_path)
    public_copy = ctx.relative("oracle.py")
    public_copy.write_bytes(FIXTURE.read_bytes())
    sha = CAL.oracle_sha256(FIXTURE)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL._assert_oracle_private(ctx, public_copy, sha)
    assert exc.value.code == "ORACLE_NOT_PRIVATE"
    private = ctx.relative(CAL.ORACLE_RELPATH)
    private.parent.mkdir(parents=True)
    private.write_bytes(FIXTURE.read_bytes())
    CAL._assert_oracle_private(ctx, private, sha)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL._assert_oracle_private(ctx, private, "0" * 64)
    assert exc.value.code == "ORACLE_DRIFT"


# ---------------------------------------------------------------------------
# 零 POST permit
# ---------------------------------------------------------------------------

def test_ledger_call_rows_only_counts_calls(tmp_path):
    p = tmp_path / "ledger.jsonl"
    assert CAL.ledger_call_rows(p) == 0
    p.write_text(json.dumps({"event": "selfcheck", "seq": 1}) + "\n"
                 + json.dumps({"event": "call", "seq": 2}) + "\n",
                 encoding="utf-8")
    assert CAL.ledger_call_rows(p) == 1


def test_zero_post_permit_refuses_when_lock_present(ctx):
    ledger = ctx.root / "ledger.jsonl"
    (ctx.subdir("contract") / "paid_lock.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
    assert exc.value.code == "PAID_LOCK_PRESENT"


def test_zero_post_permit_refuses_when_ledger_has_calls(ctx):
    ledger = ctx.root / "ledger.jsonl"
    ledger.write_text(json.dumps({"event": "call", "seq": 2}) + "\n",
                      encoding="utf-8")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
    assert exc.value.code == "NOT_ZERO_POST"


def test_zero_post_permit_records_sender_refusal(ctx):
    ledger = ctx.root / "ledger.jsonl"
    doc = CAL.issue_zero_post_permit(ctx, ledger_path=ledger)
    assert doc["kind"] == "zero_post_control"
    assert doc["model_posts_allowed"] == 0
    assert doc["sender_refusal"]["code"] == "LOCK_MISSING"
    assert doc["sender_refusal"]["transport_factory_called"] is False
    CAL.assert_permit_usable(ctx, doc, ledger_path=ledger)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.assert_permit_usable(ctx, dict(doc, kind="paid"), ledger_path=ledger)
    assert exc.value.code == "PERMIT_KIND"


# ---------------------------------------------------------------------------
# 检查语义（合成 raw；不跑容器）
# ---------------------------------------------------------------------------

def _a_record(*, n=8000, contact_dim=3, force=4.905, x_final=0.52,
              stop_at=200):
    x = [0.5 - (0.5 - x_final) * (i + 1) / n for i in range(n)]
    vx = [0.4 if i < stop_at else 0.0 for i in range(n)]
    return {
        "run_id": "run-a1", "nsteps": n,
        "time_s": [(i + 1) * DT_A for i in range(n)],
        "qpos": [[x[i], 0.0, 0.05, 1.0, 0.0, 0.0, 0.0] for i in range(n)],
        "qvel": [[vx[i], 0.0, 0.0, 0.0, 0.0, 0.0] for i in range(n)],
        "contact_dim_per_step": [contact_dim] * n,
        "contact_force_n": [force] * n,
        "params_applied": {"mass_kg": "0.5", "v0_mps": "0.4",
                           "sliding_friction": "0.4",
                           "torsional_friction_m": "0.001"},
    }


A_ENTRY = {"study": "A", "role": "negative_base", "block_id": "b01",
           "condition_id": "base"}


def test_study_a_checks_pass_on_consistent_synthetic(ctx, oracle):
    rec = _a_record()
    meta = {"initial_z_m": 0.05}
    metric = M.compute_study_a(rec, initial_x_m=0.5)
    checks = []
    row = CAL._study_a_checks(A_ENTRY, rec, meta, metric, oracle, checks)
    ids = {c["id"]: c for c in checks}
    assert set(ids) == {f"A-EXPO-CONTACT-{rec['run_id']}",
                       f"A-EXPO-FORCE-{rec['run_id']}",
                       f"A-EXPO-Z-{rec['run_id']}",
                       f"A-EXPO-V0-{rec['run_id']}",
                       f"A-MOVED-{rec['run_id']}",
                       f"A-ORACLE-DISP-{rec['run_id']}",
                       f"A-ORACLE-STOP-{rec['run_id']}"}
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
    assert float(row["metrics"]["values"][M.DISPLACEMENT]) == pytest.approx(0.02)


def test_study_a_checks_catch_bad_contact_and_force(ctx, oracle):
    rec = _a_record(contact_dim=2, force=0.0)
    meta = {"initial_z_m": 0.05}
    metric = M.compute_study_a(rec, initial_x_m=0.5)
    checks = []
    CAL._study_a_checks(A_ENTRY, rec, meta, metric, oracle, checks)
    bad = {c["id"] for c in checks if not c["ok"]}
    assert f"A-EXPO-CONTACT-{rec['run_id']}" in bad
    assert f"A-EXPO-FORCE-{rec['run_id']}" in bad


def test_contact_stats_gap():
    fraction, gap = CAL._contact_stats({"contact_dim_per_step":
                                        [3, 3, 2, 2, 2, 3, 3]})
    assert fraction == pytest.approx(4 / 7)
    assert gap == 3


def _b_record(*, n=4000, amp=0.2, freq=1.0, stride=8, events=()):
    times = [(i + 1) * DT_B for i in range(n)]
    target = [amp * math.sin(2.0 * math.pi * freq * t) for t in times]
    return {
        "run_id": "run-b1", "nsteps": n,
        "time_s": times,
        "qpos": [[0.0] for _ in range(n)],
        "qvel": [[0.0] for _ in range(n)],
        "target_q": target,
        "control_update_index": [i // stride for i in range(n)],
        "instability_events": list(events),
        "params_applied": {"dt_s": "0.001", "tc_s": "0.008",
                           "target_amplitude_rad": str(amp),
                           "target_freq_hz": str(freq), "inertia_ratio": "1.0",
                           "controller": "PD", "kp": "5", "kd": "0.1"},
    }


B_ENTRY = {"study": "B", "role": "b_cal", "block_id": "b01",
           "condition_id": "cal_b01"}


def test_study_b_checks_wiring_true_on_synthetic(oracle):
    rec = _b_record()
    metric = M.compute_study_b(rec, amplitude_rad=0.2)
    checks = []
    CAL._study_b_checks(B_ENTRY, rec, metric, oracle, checks)
    ids = {c["id"]: c for c in checks}

    def fam(prefix):
        return next(v for k, v in ids.items() if k.startswith(prefix))

    assert fam("B-EXEC-STRIDE")["ok"] is True
    assert fam("B-EXEC-TARGET")["ok"] is True
    assert fam("B-NO-INSTABILITY")["ok"] is True
    assert "sim" in fam("B-ORACLE")["values"]


def test_study_b_checks_instability_flagged(oracle):
    rec = _b_record(events=[{"time_s": 1.0, "reason": "ABS_Q_GT_4"}])
    metric = M.compute_study_b(rec, amplitude_rad=0.2)
    checks = []
    CAL._study_b_checks(B_ENTRY, rec, metric, oracle, checks)
    unstable = next(c for c in checks if c["id"].startswith("B-NO-INSTABILITY"))
    assert unstable["ok"] is False


def test_verdict_semantics():
    sci = [{"id": "A-NEG-b01", "ok": False}]
    integrity = [{"id": "GLOBAL-PERMIT", "ok": False}]
    assert CAL._verdict([{"id": "x", "ok": True}]) == "CALIBRATION_PASS"
    assert CAL._verdict(sci) == "DESIGN_NOT_READY"
    assert CAL._verdict(integrity) == "INVALID"
    assert CAL._verdict(sci + integrity) == "INVALID"


def test_check_registry_rejects_unregistered_and_counts():
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL._validate_check_registry([])
    assert exc.value.code == "CHECK_COUNT"
    fam = CAL.CHECK_FAMILIES[0][0]
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL._validate_check_registry([{"id": f"{fam}-b01", "ok": True},
                                      {"id": "NOPE-1", "ok": True}])
    assert exc.value.code == "CHECK_UNREGISTERED"


def test_render_report_markdown_smoke():
    report = {
        "verdict": "DESIGN_NOT_READY", "envelope_id": "e", "prereg_sha256": "a" * 64,
        "oracle_sha256": "b" * 64, "container_image": "img", "blocked": None,
        "budget": {"total_runs": 28, "max_runs": 250, "total_steps": 226000,
                   "max_steps": 2000000, "pre_probe_estimate_runs": 14,
                   "pre_probe_estimate_steps": 112000},
        "checks": [{"id": "A-NEG-b01", "ok": True, "detail": "", "values": {}},
                   {"id": "A-SENS", "ok": False, "detail": "|Δ|=0.004", "values": {}}],
        "failures": [{"id": "A-SENS", "detail": "|Δ|=0.004"}],
        "runs": [{"run_id": "run-a1", "role": "negative_base",
                  "metrics": {"values": {"displacement_x_m": "-0.02"},
                              "censored": {}},
                  "oracle": {"displacement_m": 0.0204, "stop_time_s": 0.102}}],
    }
    text = CAL.render_report_markdown(report)
    assert "DESIGN_NOT_READY" in text
    assert "A-SENS" in text
    assert "run-a1" in text
    assert "calibration_gap_analysis.md" in text


def test_check_family_counts_match_matrix_offline(oracle):
    """全矩阵 28 run 的离线复用：检查家族计数必须与预注册逐族相等。"""
    from types import SimpleNamespace
    from orchestrator.rsi_r1 import experiments as E

    entries = CAL.calibration_matrix()
    specs = CAL.specs_for_matrix(entries)
    spec_by = {(s.study_id, s.block_id, s.condition_id): s for s in specs}
    checks = []
    runs_doc = []
    for e in entries:
        spec = spec_by[(e["study"], e["block_id"], e["condition_id"])]
        if e["study"] == "A":
            rec = _a_record(n=800)
            rec["run_id"] = spec.run_id
            rec["params_applied"] = dict(e["params"])
            meta = {"initial_z_m": 0.05}
            metric = M.compute_study_a(rec, initial_x_m=0.5)
            runs_doc.append(CAL._study_a_checks(e, rec, meta, metric, oracle, checks))
        else:
            rec = _b_record()
            rec["run_id"] = spec.run_id
            rec["params_applied"] = dict(e["params"])
            metric = M.compute_study_b(rec, amplitude_rad=0.2)
            runs_doc.append(CAL._study_b_checks(e, rec, metric, oracle, checks))
    CAL._a_group_checks(runs_doc, checks)
    CAL._b_group_checks(runs_doc, checks)
    CAL._replay_checks(runs_doc, checks)
    ev_b = SimpleNamespace(outcomes=[
        SimpleNamespace(run_id=s.run_id, ok=True,
                        record={"saturation_count": 1 if s.block_id == CAL.B_SATURATION_BLOCK else 0})
        for s in specs if s.study_id == "B"])
    CAL._fill_b_saturation(ev_b, [s for s in specs if s.study_id == "B"], checks)
    for fam, _, _ in CAL.CHECK_FAMILIES:              # 全局完整性检查由 run 流程追加
        if fam.startswith("GLOBAL-"):
            checks.append({"id": fam, "ok": True, "detail": "", "values": {}})
    CAL._validate_check_registry(checks)          # 计数不符即抛 CHECK_COUNT
    assert len(checks) == sum(c for _, _, c in CAL.CHECK_FAMILIES)
    assert CAL._verdict(checks) in ("CALIBRATION_PASS", "DESIGN_NOT_READY")


# ---------------------------------------------------------------------------
# v2（2026-11-11 用户批准的修正设计）：首步位移门 / 尺度感知容差 / 相位第二证据 /
# 前次尝试取证。全部离线合成，不跑容器。
# ---------------------------------------------------------------------------

FIXTURE_V2 = (Path(__file__).parent / "fixtures" / "rsi_r1"
              / "calibration_oracle_stub_v2.py")


@pytest.fixture(scope="module")
def oracle_v2():
    return CAL.load_oracle(FIXTURE_V2, schema=CAL.ORACLE_SCHEMA_V2)


def test_design_tables_and_family_counts():
    assert CAL.design_for(1)["key"] == "v1"
    assert CAL.design_for("v2")["version"] == 2
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.design_for(3)
    assert exc.value.code == "DESIGN_UNKNOWN"
    assert sum(c for _, _, c in CAL.V2_CHECK_FAMILIES) == 200
    names = [f for f, _, _ in CAL.V2_CHECK_FAMILIES]
    assert "B-ORACLE-PHASE" in names and "GLOBAL-PRIOR-ATTEMPT" in names
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL._validate_check_registry([], CAL.V2_CHECK_FAMILIES)
    assert exc.value.code == "CHECK_COUNT"


def test_oracle_v2_rejects_v1_schema():
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_oracle(FIXTURE_V2, schema=CAL.ORACLE_SCHEMA)
    assert exc.value.code == "ORACLE_INTERFACE"
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_oracle(FIXTURE, schema=CAL.ORACLE_SCHEMA_V2)
    assert exc.value.code == "ORACLE_INTERFACE"


def test_v2_first_step_displacement_gate(ctx, oracle_v2):
    """v2 门：|x[0]−x0−v0·dt| ≤ 1e-5 m；v1 门（前 5 步速度）仍 0.01 m/s。"""
    meta = {"initial_x_m": 0.5, "initial_z_m": 0.05}
    rec = _a_record()
    rec["qpos"][0][0] = 0.5 + 0.4 * DT_A            # 首步位移恰为 v0·dt
    metric = M.compute_study_a(rec, initial_x_m=0.5)
    checks = []
    CAL._study_a_checks(A_ENTRY, rec, meta, metric, oracle_v2, checks,
                        design=CAL.DESIGN_V2)
    v0 = next(c for c in checks if c["id"].startswith("A-EXPO-V0"))
    assert v0["ok"] is True
    assert v0["values"]["v0_first_step_disp_error_m"] == pytest.approx(0.0, abs=1e-15)
    # 2e-5 m 的偏移即失败（门 1e-5）
    rec["qpos"][0][0] += 2e-5
    metric = M.compute_study_a(rec, initial_x_m=0.5)
    checks = []
    CAL._study_a_checks(A_ENTRY, rec, meta, metric, oracle_v2, checks,
                        design=CAL.DESIGN_V2)
    v0 = next(c for c in checks if c["id"].startswith("A-EXPO-V0"))
    assert v0["ok"] is False
    assert "qvel 先于首次 mj_step" in CAL.DESIGN_V2["families"][
        [f for f, _, _ in CAL.DESIGN_V2["families"]].index("A-EXPO-V0")][1]


def _b_metric(value: float) -> M.StudyMetrics:
    from decimal import Decimal
    return M.StudyMetrics(study_id="B",
                          values={M.TRACKING_RMSE: Decimal(repr(float(value)))},
                          censored={M.TRACKING_RMSE: False})


def test_v2_b_oracle_scale_aware_tolerance(oracle_v2):
    """差的量级落在 (0.01, 0.01+1%·ref] ⇒ v1 失败、v2 通过。"""
    rec = _b_record()
    params = rec["params_applied"]
    ref = oracle_v2.study_b_rmse(
        inertia_ratio=1.0, amplitude_rad=0.2, freq_hz=1.0, tc_s=0.008,
        dt_s=0.001, controller="PD", kp=5.0, kd=0.1,
        duration_s=float(S.DURATION_S),
        window_start_s=float(R.TRACKING_WINDOW_START_S),
        window_end_s=float(R.TRACKING_WINDOW_END_S))["normalized_rmse"]
    sim = ref + 0.01 + 0.5 * 0.01 * ref
    entry = {"study": "B", "role": "b_cal", "block_id": "b03",
             "condition_id": "cal_b03"}
    checks_v1: "list[dict]" = []
    CAL._study_b_checks(entry, rec, _b_metric(sim), oracle_v2, checks_v1)
    assert next(c for c in checks_v1 if c["id"].startswith("B-ORACLE"))["ok"] is False
    checks_v2: "list[dict]" = []
    CAL._study_b_checks(entry, rec, _b_metric(sim), oracle_v2, checks_v2,
                        design=CAL.DESIGN_V2)
    ok = next(c for c in checks_v2 if c["id"].startswith("B-ORACLE"))
    assert ok["ok"] is True and ok["values"]["tol"] > 0.01
    # 非欠阻尼 block 不产生相位检查
    assert not any(c["id"].startswith("B-ORACLE-PHASE") for c in checks_v2)


def test_b_phase_check_matches_reference_and_catches_scale(oracle_v2):
    rec = _b_record()
    params = rec["params_applied"]
    trace = oracle_v2.study_b_trace(
        inertia_ratio=1.0, amplitude_rad=0.2, freq_hz=1.0, tc_s=0.008,
        dt_s=0.001, controller="PD", kp=5.0, kd=0.1, duration_s=float(S.DURATION_S))
    rec["qpos"] = [[q] for q in trace["q"]]
    checks: "list[dict]" = []
    CAL._b_phase_check(B_ENTRY, rec, oracle_v2, checks)
    c = next(x for x in checks if x["id"].startswith("B-ORACLE-PHASE"))
    assert c["ok"] is True and c["values"]["period_rel"] == 0.0
    # 幅值 ×1.5 ⇒ 包络差 50% 超 2% 门（周期不变）
    rec["qpos"] = [[1.5 * q] for q in trace["q"]]
    checks = []
    CAL._b_phase_check(B_ENTRY, rec, oracle_v2, checks)
    c = next(x for x in checks if x["id"].startswith("B-ORACLE-PHASE"))
    assert c["ok"] is False and c["values"]["envelope_rel"] == pytest.approx(0.5)
    # 常量响应 ⇒ 不可辨识 ⇒ 失败（不得当通过）
    rec["qpos"] = [[0.0] for _ in trace["q"]]
    checks = []
    CAL._b_phase_check(B_ENTRY, rec, oracle_v2, checks)
    c = next(x for x in checks if x["id"].startswith("B-ORACLE-PHASE"))
    assert c["ok"] is False and "不可辨识" in c["detail"]


def test_b_phase_check_requires_trace_api(oracle):
    """v1 oracle 没有 study_b_trace ⇒ 相位检查 fail-closed，不抛异常。"""
    checks: "list[dict]" = []
    CAL._b_phase_check(B_ENTRY, _b_record(), oracle, checks)
    assert checks[0]["ok"] is False and "study_b_trace" in checks[0]["detail"]


def _seed_prior_attempt(ctx):
    for rel in CAL.PRIOR_ATTEMPT_FILES:
        ctx.write_text(rel, "{}\n" if rel.endswith(".json") else "prior\n")
    obs = ctx.subdir("observations")
    for batch in CAL.PRIOR_ATTEMPT_RAW_BATCHES:
        d = obs / batch
        d.mkdir(parents=True, exist_ok=True)
        (d / "run-prior.jsonl").write_text("{}\n", encoding="utf-8")


def test_prior_attempt_manifest_and_drift(ctx):
    _seed_prior_attempt(ctx)
    manifest = CAL.prior_attempt_manifest(ctx)
    assert manifest["n_files"] == len(CAL.PRIOR_ATTEMPT_FILES)
    assert manifest["n_raws"] == len(CAL.PRIOR_ATTEMPT_RAW_BATCHES)
    CAL.verify_prior_attempt(ctx, manifest)
    ctx.write_text(CAL.GAP_RELPATH, "被改写\n")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.verify_prior_attempt(ctx, manifest)
    assert exc.value.code == "PRIOR_ATTEMPT_DRIFT"


def test_prior_attempt_manifest_missing(ctx):
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.prior_attempt_manifest(ctx)
    assert exc.value.code == "PRIOR_ATTEMPT_MISSING"


def test_v2_prereg_requires_prior_attempt(ctx):
    _seed_prior_attempt(ctx)
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.build_calibration_prereg(ctx=ctx, oracle_path=FIXTURE_V2,
                                     design=CAL.DESIGN_V2)
    assert exc.value.code == "PRIOR_ATTEMPT_MISSING"


def test_v2_prereg_roundtrip_uses_v2_paths_and_schema(ctx):
    _seed_prior_attempt(ctx)
    prior = CAL.prior_attempt_manifest(ctx)
    doc = CAL.build_calibration_prereg(
        ctx=ctx, oracle_path=FIXTURE_V2, design=CAL.DESIGN_V2,
        supersedes={"prereg_relpath": CAL.PREREG_RELPATH},
        prior_attempt=prior, registered_at_utc="2026-11-11T06:00:00Z")
    assert doc["schema"] == CAL.PREREG_SCHEMA_V2
    assert doc["design_version"] == 2
    assert doc["prior_attempt"]["files_sha256"] == prior["files_sha256"]
    path = CAL.write_calibration_prereg(ctx, doc, CAL.DESIGN_V2)
    assert path.name == "calibration_prereg_v2.json"
    loaded = CAL.load_calibration_prereg(ctx, CAL.DESIGN_V2)
    assert loaded["document_sha256"] == doc["document_sha256"]
    # v1 路径不受影响：v2 预注册不能冒充 v1（读到的是 prior 占位件）
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.load_calibration_prereg(ctx, CAL.DESIGN_V1)
    assert exc.value.code == "PREREG_SCHEMA"


def test_v2_raw_scope_only_counts_v2_batches(ctx):
    _seed_prior_attempt(ctx)                        # v1 batch 有 raw
    assert CAL._raw_paths(ctx, CAL.DESIGN_V2) == []
    assert len(CAL._raw_paths(ctx, CAL.DESIGN_V1)) == 2
    d = ctx.subdir("observations") / CAL.BATCH_A_V2
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-v2.jsonl").write_text("{}\n", encoding="utf-8")
    assert len(CAL._raw_paths(ctx, CAL.DESIGN_V2)) == 1
    doc = CAL.build_calibration_prereg(
        ctx=ctx, oracle_path=FIXTURE_V2, design=CAL.DESIGN_V2,
        supersedes={"x": 1}, prior_attempt=CAL.prior_attempt_manifest(ctx))
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.write_calibration_prereg(ctx, doc, CAL.DESIGN_V2)
    assert exc.value.code == "RAW_BEFORE_PREREG"


def test_oracle_v2_equivalence_pass_and_detect(ctx):
    priv = ctx.subdir("private") / "calibration_private"
    priv.mkdir(parents=True, exist_ok=True)
    (priv / "oracle.py").write_bytes(FIXTURE.read_bytes())
    (priv / "oracle_v2.py").write_bytes(FIXTURE_V2.read_bytes())
    eq = CAL.assert_oracle_v2_equivalence(ctx)
    assert eq["max_abs_diff"] == 0.0
    assert len(eq["per_block_abs_diff"]) == 9   # 8 block + b02 复跑
    # 改大小写不动长度会被同秒 mtime 的 __pycache__ 命中；用表达式改长度
    text = FIXTURE_V2.read_text(encoding="utf-8").replace("REFINE = 4", "REFINE = 4 * 2")
    (priv / "oracle_v2.py").write_text(text, encoding="utf-8")
    with pytest.raises(CAL.CalibrationError) as exc:
        CAL.assert_oracle_v2_equivalence(ctx)
    assert exc.value.code == "ORACLE_EQUIVALENCE"


def test_collect_run_checks_threads_design_end_to_end(oracle):
    """生产检查收集器必须把 design 透传到每个 run（T1b-v2 abort 的根因回归）。

    v2 预注册下若漏传 design，``B-ORACLE-PHASE`` 计数为 0 ⇒ CHECK_COUNT（而容器
    已经跑完）。本测试直接调用 ``run_calibration`` 所用的同一收集器，两套家族逐族复核。
    """
    from types import SimpleNamespace

    entries = CAL.calibration_matrix()
    specs = CAL.specs_for_matrix(entries)
    spec_by = {(s.study_id, s.block_id, s.condition_id): s for s in specs}
    a_entries = [e for e in entries if e["study"] == "A"]
    b_entries = [e for e in entries if e["study"] == "B"]
    a_specs = [spec_by[(e["study"], e["block_id"], e["condition_id"])] for e in a_entries]
    b_specs = [spec_by[(e["study"], e["block_id"], e["condition_id"])] for e in b_entries]
    a_out, a_metas = [], {}
    for e, spec in zip(a_entries, a_specs):
        rec = _a_record(n=800)
        rec["run_id"] = spec.run_id
        rec["params_applied"] = dict(e["params"])
        rec["qpos"][0][0] = 0.5 + float(e["params"]["v0_mps"]) * DT_A
        a_out.append(SimpleNamespace(run_id=spec.run_id, ok=True, record=rec))
        a_metas[spec.run_id] = {"initial_x_m": 0.5, "initial_z_m": 0.05}
    b_out = []
    for e, spec in zip(b_entries, b_specs):
        rec = _b_record()
        rec["run_id"] = spec.run_id
        rec["params_applied"] = dict(e["params"])
        rec["saturation_count"] = 1
        b_out.append(SimpleNamespace(run_id=spec.run_id, ok=True, record=rec))
    ev_a = SimpleNamespace(outcomes=tuple(a_out))
    ev_b = SimpleNamespace(outcomes=tuple(b_out))

    def run(design):
        runs_doc, checks = CAL.collect_run_checks(
            a_entries=a_entries, a_specs=a_specs, ev_a=ev_a, a_metas=a_metas,
            b_entries=b_entries, b_specs=b_specs, ev_b=ev_b, oracle=oracle,
            design=design)
        for fam, _, _ in design["families"]:
            if fam.startswith("GLOBAL-"):
                checks.append({"id": fam, "ok": True, "detail": "", "values": {}})
        CAL._validate_check_registry(checks, design["families"])   # 计数不符即抛
        return runs_doc, checks

    runs_v1, checks_v1 = run(CAL.DESIGN_V1)
    assert len(runs_v1) == 28
    assert len(checks_v1) == sum(c for _, _, c in CAL.CHECK_FAMILIES)
    assert not any(c["id"].startswith("B-ORACLE-PHASE-") for c in checks_v1)

    runs_v2, checks_v2 = run(CAL.DESIGN_V2)
    assert len(runs_v2) == 28
    assert len(checks_v2) == sum(c for _, _, c in CAL.V2_CHECK_FAMILIES)
    phase = [c for c in checks_v2 if c["id"].startswith("B-ORACLE-PHASE-")]
    assert len(phase) == 3, [c["id"] for c in phase]
    block_of = {r["run_id"]: r["block_id"] for r in runs_v2}
    assert {block_of[c["id"].split("B-ORACLE-PHASE-")[1]] for c in phase} == {
        "b01", "b02", "b06"}
