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
