#!/usr/bin/env python3
"""Task8 单测：Track2 DR 对照 batch 的 spec_factory + evaluate 接缝 + CLI 装配。

零付费、零联网（硬规则 2）：Genesis batch 产物用合成夹具按生产 schema 落盘，
视觉网关全 fake（call() 文本入口被触即失败，硬规则 1）。覆盖计划 L561-572：

- spec_factory：仅 genesis 合法 → batch 子命令/三臂聚合产物名/billing_task=8/
  GS_ENABLE_ZEROCOPY=0/2400s 超时/v2 标定件 stage；carla/mujoco 与内容层缺方案/
  坏标定一律 GraphStateError；Gate2 打回递增 run_idx 且不继承旧补丁
  （Task0b 起 genesis 方案在 tests 合成 fixture 内容层，正本在 KB solutions/）；
- evaluate：gait_rungs 三级文件判定（不信 outcomes，series 重算）+ 两张对照
  分屏各一次 Rung4；uncalibrated 零实呼，record_only 恰好 2 次实呼、12 题、
  证据 0600；缺 summary/分屏硬失败；tilt 两级舍入容差 0.01°；
- L571：contradiction_control_not_robust / worsened 是合法结论而非失败；
- 卡片渲染按 metrics_kind 走 Task8 batch 分支；
- CLI：task=8 缺双批次即 SystemExit；7/8 注入接缝、其余 None；Gate2 三问
  说明只在 task=8 透传。
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import csv
import json
import math
import stat
from pathlib import Path

import pytest

from orchestrator.graph import task8
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge.gait_clips import write_gait_clip_bundle
from orchestrator.judge.gait_rungs import (
    contrast_block,
    evaluate_batch,
    recompute_run,
    rung2_check,
    validate_telemetry_structure,
)
from orchestrator.judge.gait_thresholds import POSTPUSH_WINDOW_S
from orchestrator.judge.rung4 import Rung4Result
from orchestrator.solutions import resolve_solution

# Task0b：单测不依赖活 KB 仓，ENGINE_SCENARIOS 挂的 genesis 方案 id 解析到
# tests/fixtures/solutions 合成方案（manifest 走 resolver 全量校验）。
FIXTURE_SOLUTIONS_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "solutions")


@pytest.fixture(autouse=True)
def _fixture_solutions_root(monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT",
                       str(FIXTURE_SOLUTIONS_ROOT))


# CSV_FIELDS 直接从 fixture genesis 脚本 AST 提取（不 import genesis：宿主未必
# 装引擎本体）；fixture 的 35 列逐字复制 KB 正本，漂移即本文件多处断言失败。
_GENESIS_FIXTURE = (
    FIXTURE_SOLUTIONS_ROOT / "track2_questions"
    / "01_ice_floor_humanoid_dr" / "genesis_humanoid_dr.py")
_TEMPLATE_SRC = _GENESIS_FIXTURE.read_text(encoding="utf-8")
CSV_FIELDS = ast.literal_eval(next(
    n.value for n in ast.walk(ast.parse(_TEMPLATE_SRC))
    if isinstance(n, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "CSV_FIELDS"
            for t in n.targets)))
assert len(CSV_FIELDS) == 35

PUSH_T_BY_SEED = [3.0, 2.7, 3.3]
MU_BY_ARM = {"normal": 0.7, "ice_nominal": 0.05,
             "ice_dr": {0: 0.0897, 1: 0.1125, 2: 0.3385}}


# ---------------------------------------------------------------------------
# 合成 batch 夹具（series 先行，outcomes 由生产重算器派生 → 天然自洽）
# ---------------------------------------------------------------------------

def _series(*, fell_at: "int | None", push_t: float,
            slip_level: float) -> dict:
    n = 400
    t = [round(0.02 * i, 2) for i in range(n)]
    z, tilt, vx, slip_r, slip_l, f_r, f_l, tx = [], [], [], [], [], [], [], []
    for i in range(n):
        if fell_at is None or i < fell_at:
            z.append(1.27)
            tilt.append(3.0 + 1.5 * math.sin(0.25 * i))
            v = 0.03 + 0.01 * math.sin(0.4 * i)
            fr = 80.0 + 10.0 * math.sin(0.3 * i)
        else:
            # 倾覆段：倾角上 130°、质心落地、双足以 slip_level 滑脱
            u = min(1.0, (i - fell_at) / 25.0)
            z.append(round(1.27 - 0.87 * u, 4))
            tilt.append(round(60.0 + 70.0 * u, 3))
            v = slip_level * u
            fr = 800.0 + 400.0 * u
        # 推力窗（35N×.2s）给一个温和力峰；序列值非负、界内
        if push_t <= t[i] < push_t + 0.2:
            fr += 250.0
        z_i = z[-1]
        f_r.append(round(max(fr, 0.0), 3))
        f_l.append(round(max(fr * 0.92, 0.0), 3))
        slip_r.append(round(max(v, 0.0), 4))
        slip_l.append(round(max(v * 0.85, 0.0), 4))
        vx.append(round(v * 0.5, 4))
        tx.append(0.01 * i)
    return {"t": t, "torso_z": z, "torso_x": tx, "tilt_deg": tilt,
            "vx": vx, "contact_force_R": f_r, "contact_force_L": f_l,
            "slip_v_R": slip_r, "slip_v_L": slip_l}


def _run_data(arm: str, seed: int, fell_at: "int | None") -> dict:
    mu = MU_BY_ARM[arm]
    mu = mu[seed] if isinstance(mu, dict) else mu
    push_t = PUSH_T_BY_SEED[seed]
    level = 0.05 if arm == "normal" else (8.0 if arm == "ice_nominal" else 14.0)
    data = {
        "schema": "simpraxis.task8.track2.telemetry.v1",
        "engine": "genesis", "engine_version": "1.3.3",
        "sim": {"use_deterministic_algorithms": True,
                "gs_enable_zerocopy": False,
                "telem_hz": 50, "dur_s": 8.0},
        "params": {"arm": arm, "mu": mu,
                   "push": {"n": 35.0, "dur": 0.2, "t": push_t}},
        "series": _series(fell_at=fell_at, push_t=push_t, slip_level=level),
    }
    return data


def _outcomes_from(rec: dict) -> dict:
    return {
        "fell": rec["fell"], "fall_frame": rec["fall_frame"],
        "fall_time_s": rec["fall_time_s"],
        "torso_z_min": rec["torso_z_min"],
        "tilt_max_deg": rec["tilt_max_deg"],
        "vx_mean_tail": rec["vx_mean_tail"],
        "torso_x_last": rec["torso_x_last"],
        "slip_dist_m": {f: rec["feet"][f]["slip_dist_m"] for f in ("R", "L")},
        "slip_v_max_mps": {f: rec["feet"][f]["slip_vmax_mps"]
                           for f in ("R", "L")},
        "slip_v_max_postpush_mps": {
            f: rec["feet"][f]["slip_vmax_postpush_mps"] for f in ("R", "L")},
        "contact_force_peak_N": {f: rec["feet"][f]["contact_force_peak_N"]
                                 for f in ("R", "L")},
        "contact_frames": {f: rec["feet"][f]["contact_frames"]
                           for f in ("R", "L")},
    }


def _csv_row(run_id: str, arm: str, seed: int, data: dict, rec: dict) -> dict:
    row = {k: "" for k in CSV_FIELDS}
    row.update({
        "run_id": run_id, "arm": arm, "seed": seed, "mu": data["params"]["mu"],
        "push_n": 35, "push_t": data["params"]["push"]["t"], "push_dur": 0.2,
        "success": int(not rec["fell"]), "fell": int(rec["fell"]),
        "fall_frame": "" if rec["fall_frame"] is None else rec["fall_frame"],
        "fall_time_s": rec["fall_time_s"] if rec["fall_time_s"] is not None else "",
        "torso_z_min": rec["torso_z_min"], "tilt_max_deg": rec["tilt_max_deg"],
        "vx_mean_tail": rec["vx_mean_tail"],
        "slip_dist_R_m": rec["feet"]["R"]["slip_dist_m"],
        "slip_dist_L_m": rec["feet"]["L"]["slip_dist_m"],
        "slip_vmax_R_mps": rec["feet"]["R"]["slip_vmax_mps"],
        "slip_vmax_L_mps": rec["feet"]["L"]["slip_vmax_mps"],
        "slip_vmax_postpush_R_mps":
            rec["feet"]["R"]["slip_vmax_postpush_mps"],
        "slip_vmax_postpush_L_mps":
            rec["feet"]["L"]["slip_vmax_postpush_mps"],
        "force_peak_R_N": rec["feet"]["R"]["contact_force_peak_N"],
        "force_peak_L_N": rec["feet"]["L"]["contact_force_peak_N"],
        "contact_frames_R": rec["feet"]["R"]["contact_frames"],
        "contact_frames_L": rec["feet"]["L"]["contact_frames"],
        "sim_wall_s": 12.3, "video_relpath": "",
    })
    return row


def _calibration_v2() -> dict:
    return {"schema": "simpraxis.task8.track2.push_window_calibration.v2",
            "selected_window": {"push_n_N": 35, "push_dur_s": 0.2},
            "cross_process_replication": {"all_bit_exact": True}}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    phase1 = tmp_path / "phase1"
    runs = tmp_path / "runs"
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(phase1))
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(runs))
    return {"phase1": phase1, "runs": runs, "tmp": tmp_path}


def build_synthetic_batch(wd: Path, *, falls: dict | None = None) -> Path:
    """落盘一个结构合法的三臂×3 籽 batch。falls: {(arm,seed): fall_frame}。"""
    (wd / "telemetry").mkdir(parents=True)
    (wd / "plots").mkdir()
    falls = falls or {}
    rows = []
    for arm in ("normal", "ice_nominal", "ice_dr"):
        for seed in range(3):
            run_id = f"{arm}_s{seed}"
            data = _run_data(arm, seed, falls.get((arm, seed)))
            from orchestrator.judge.gait_rungs import recompute_run
            rec = recompute_run(data)
            data["outcomes"] = _outcomes_from(rec)
            (wd / "telemetry" / f"{run_id}.json").write_text(
                json.dumps(data), encoding="utf-8")
            rows.append(_csv_row(run_id, arm, seed, data, rec))
    with open(wd / "dr_controls_vs_experimental.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    (wd / "push_window_calibration.json").write_text(
        json.dumps(_calibration_v2()), encoding="utf-8")
    (wd / "summary.json").write_text(json.dumps({
        "schema": "simpraxis.task8.track2.batch_summary.v1", "seeds": 3,
        "disturbance": {"push_n": 35, "push_dur_s": 0.2,
                        "push_t_by_seed": PUSH_T_BY_SEED},
        "dr_envelope": {"mu_loguniform": [0.05, 0.4],
                        "Kp_loguniform": [10000, 1000000],
                        "Kd_loguniform": [15, 200]}}, ensure_ascii=False),
        encoding="utf-8")
    (wd / "SUMMARY.md").write_text("# synthetic batch", encoding="utf-8")
    for name in ("collage.png", "cover.png",
                 "plots/contact_forces.png", "plots/slip_comparison.png"):
        (wd / name).write_bytes(b"\x89PNG\r\n")
    # 两张对照分屏必须是真 mp4（接缝要抽帧）：用步态合成仪器各写一份，
    # 合成目录在 batch 目录外的临时目录，用完整个清掉。
    import shutil
    import tempfile
    clip_tmp = Path(tempfile.mkdtemp(prefix="_clipgen_", dir=wd.parent))
    try:
        for _, split_name in task8.SPLITS:
            b = write_gait_clip_bundle("good", clip_tmp / split_name)
            Path(b["mp4"]).replace(wd / split_name)
    finally:
        shutil.rmtree(clip_tmp)
    return wd


@pytest.fixture
def batch_dir(roots):
    wd = roots["tmp"] / "run_t8-r1"
    # normal 全存活；冰面名义臂全倒；DR 两籽推力前自溃、一籽后倒（同正式批）
    return build_synthetic_batch(wd, falls={
        ("ice_nominal", 0): 257, ("ice_nominal", 1): 261,
        ("ice_nominal", 2): 264,
        ("ice_dr", 0): 62, ("ice_dr", 1): 93, ("ice_dr", 2): 254})


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class FakeVisualGateway:
    """接缝唯一允许触网的是 call_visual（恒 visual_rollout）；call() 即失败。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.visual_calls: list[tuple] = []

    def model_id(self, stage: str) -> str:
        assert stage == "visual_rollout"
        return "fixture-visual-model-000"

    def call_visual(self, prompt, image_b64, **kwargs):
        self.visual_calls.append((prompt, image_b64, kwargs))
        return self.raw, (11, 22)

    def call(self, *a, **k):  # pragma: no cover - 文本入口被触即失败
        raise AssertionError("Task8 evaluate 不得走任何文本 stage")


def _gait_raw() -> str:
    rubric = task8.load_gait_rubric()
    return json.dumps({"answers": [{"id": q["id"], "answer": "yes"}
                                   for q in rubric["questions"]],
                       "low_confidence": False})


def _set_gait_calibration(roots, mode: str) -> Path:
    d = roots["phase1"] / "task8_track2_judge"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "calibration_gait.json"
    p.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# spec_factory
# ---------------------------------------------------------------------------

def test_01_spec_factory_genesis_mapping(roots, monkeypatch):
    cal_src = roots["tmp"] / "cal_src.json"
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", cal_src)

    spec = task8.make_spec_factory("tt", "T8-B1-coding")(
        {"chosen": {"sim_engine": "genesis", "name": "DR 方案"},
         "agent_runs": [], "events": []})

    assert spec.billing_task == 8
    assert spec.batch_id == "T8-B1-coding"
    assert spec.telemetry_relpath == "summary.json"
    assert spec.video_relpath == "split_normal_vs_ice_nominal_s0.mp4"
    assert spec.cover_relpath == "cover.png"
    assert spec.manifest_relpath == "manifest.json"
    assert spec.run_timeout_s == 2400
    target = Path(spec.cmd[1])
    assert spec.cmd[0] == task8.VENV_PY
    assert target.name == "genesis_humanoid_dr.py"
    assert spec.cmd[2:] == ["batch", "--out-dir", str(spec.workdir),
                            "--seeds", "3", "--calibration-json",
                            str(spec.workdir / "push_window_calibration.json")]
    assert spec.workdir == (roots["runs"] / "phase1" / "task8_track2"
                            / "q1_genesis" / "run_tt_1")
    assert target.is_file() and _mode(target) == 0o700
    manifest = resolve_solution(
        task8.ENGINE_SCENARIOS["genesis"]["solution_id"])
    source = manifest.solution_dir / "genesis_humanoid_dr.py"
    assert target.read_bytes() == source.read_bytes()
    cal = spec.workdir / "push_window_calibration.json"
    assert cal.is_file() and _mode(cal) == 0o600
    assert json.loads(cal.read_text())["selected_window"]["push_n_N"] == 35
    assert spec.env_extra["GS_ENABLE_ZEROCOPY"] == "0"
    assert spec.env_extra["PYOPENGL_PLATFORM"] == "egl"
    assert spec.env_extra["HOME"] == "/mnt/caches/home"
    assert spec.env_extra["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert spec.env_extra["SIMPRAXIS_USE_PROXY"] == "0"


def test_02_spec_factory_run_idx_and_gate2_notes(roots, monkeypatch):
    cal_src = roots["tmp"] / "cal_src.json"
    cal_src.write_text(json.dumps(_calibration_v2()), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", cal_src)
    factory = task8.make_spec_factory("tt", "T8-B1-coding")

    s2 = factory({
        "chosen": {"sim_engine": "genesis"},
        "agent_runs": [{"run_idx": 1}],
        "events": [{"type": "gate2_rejected",
                    "human_notes": "DR 包络需覆盖油污油膜"}]})
    assert s2.workdir.name == "run_tt_2"
    assert s2.gate2_notes == "DR 包络需覆盖油污油膜"
    # 干净新副本 + 新标定件（coding 补丁不跨 run 继承）
    assert Path(s2.cmd[1]).is_file()


def test_03_spec_factory_rejects_non_genesis_and_missing_solution(
        roots, monkeypatch, tmp_path):
    factory = task8.make_spec_factory("tt", "T8-B1-coding")
    for engine in ("mujoco", "sapien", "carla"):
        with pytest.raises(GraphStateError, match="Q1 只支持"):
            factory({"chosen": {"sim_engine": engine},
                     "agent_runs": [], "events": []})

    # Task0b：方案 id 合 C4 语法但内容层缺目录 → resolver fail-closed。
    monkeypatch.setitem(
        task8.ENGINE_SCENARIOS, "genesis",
        {"solution_id": "track2_questions/99_missing_fixture", "env": {}})
    with pytest.raises(GraphStateError, match="方案 stage 失败"):
        factory({"chosen": {"sim_engine": "genesis"},
                 "agent_runs": [], "events": []})


def test_04_spec_factory_calibration_guards(roots, monkeypatch, tmp_path):
    factory = task8.make_spec_factory("tt", "T8-B1-coding")
    state = {"chosen": {"sim_engine": "genesis"},
             "agent_runs": [], "events": []}

    missing = tmp_path / "nope.json"
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", missing)
    with pytest.raises(GraphStateError, match="选窗标定件不存在"):
        factory(state)

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", bad_json)
    with pytest.raises(GraphStateError, match="非法 JSON"):
        factory(state)

    wrong = tmp_path / "wrong.json"
    d = _calibration_v2()
    d["schema"] = "simpraxis.task8.track2.push_window_calibration.v1"
    wrong.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(task8, "CALIBRATION_SOURCE", wrong)
    with pytest.raises(GraphStateError, match="schema 不符"):
        factory(state)


# ---------------------------------------------------------------------------
# evaluate 接缝
# ---------------------------------------------------------------------------

def test_10_evaluate_uncalibrated_zero_network(roots, batch_dir):
    gw = FakeVisualGateway(_gait_raw())

    def _boom(*a, **k):  # pragma: no cover - 未标定绝不发网
        raise AssertionError("uncalibrated 不得发视觉实呼")

    gw.call_visual = _boom
    seam = task8.make_evaluate_seam(gw, "T8-B1-visual", "t8")
    metrics = seam({"telemetry_uri": str(batch_dir / "summary.json"),
                    "agent_runs": [{}]})

    assert metrics["metrics_kind"] == "task8_gait_batch"
    assert metrics["rules_pass"] is True and metrics["auto_release"] is True
    assert metrics["vector"] == {"rung1": True, "rung2": True, "rung3": True}
    assert metrics["seeds"] == 3
    assert set(metrics["groups"]) == {"normal", "ice_nominal", "ice_dr"}
    assert metrics["groups"]["normal"]["success_rate"] == 1.0
    assert metrics["groups"]["ice_dr"]["fall_frames"] == [62, 93, 254]
    contrast = metrics["contrast"]
    assert contrast["dr_vs_nominal"]["direction"] in (
        "improved", "worsened", "mixed")
    assert "l571_note" in contrast
    r4 = metrics["rungs"]["rung4"]
    assert r4["status"] == "uncalibrated" and r4["mode"] == "uncalibrated"
    # metrics 经 JSON-safe 序列化（numpy 标量兜底），只保证结构相等
    assert metrics["rung4"] == r4
    assert metrics["artifacts"]["csv"] == "dr_controls_vs_experimental.csv"
    assert metrics["artifacts"]["splits"] == [n for _, n in task8.SPLITS]
    # 零证据、零拼图以外产物
    evd = roots["phase1"] / "task8_track2" / "evidence"
    assert not evd.exists() or not list(evd.iterdir())
    for key, _ in task8.SPLITS:
        collage = batch_dir / f"rung4_collage_{key}.png"
        assert collage.is_file() and _mode(collage) == 0o600


def test_11_evaluate_record_only_two_calls_and_evidence(roots, batch_dir):
    _set_gait_calibration(roots, "record_only")
    gw = FakeVisualGateway(_gait_raw())
    seam = task8.make_evaluate_seam(gw, "T8-B1-visual", "t8")
    metrics = seam({"telemetry_uri": str(batch_dir / "summary.json"),
                    "agent_runs": [{}]})

    assert len(gw.visual_calls) == 2            # 两张分屏各一次
    for prompt, image_b64, kwargs in gw.visual_calls:
        assert "data:image" not in prompt
        assert isinstance(image_b64, str) and len(image_b64) > 1000
        assert kwargs["max_tokens"] == 512
    r4 = metrics["rung4"]
    assert r4["status"] == "ok" and r4["mode"] == "record_only"
    assert r4["n_items"] == 12 and r4["n_defects"] == 0
    assert r4["disagreement"] is None           # record_only 分歧强制清空
    assert {it["split"] for it in r4["items"]} == {n for _, n in task8.SPLITS}
    assert [s["split"] for s in r4["splits"]] == [n for _, n in task8.SPLITS]
    evd = roots["phase1"] / "task8_track2" / "evidence"
    for key, _ in task8.SPLITS:
        req = evd / f"rung4_t8-r1_{key}.req.json"
        resp = evd / f"rung4_t8-r1_{key}.resp.json"
        assert req.is_file() and resp.is_file()
        assert _mode(req) == 0o600 and _mode(resp) == 0o600
        req_j = json.loads(req.read_text())
        assert req_j["stage"] == "visual_rollout"
        assert req_j["batch_id"] == "T8-B1-visual"
        assert req_j["kind"] == "task8_split_rung4"
    assert r4["raw_text_ref"] is None
    # split 级 raw_text_ref 已补戳到证据
    assert all(s["raw_text_ref"] for s in r4["splits"])
    assert "banner" not in metrics


def test_12_evaluate_missing_summary_or_split_hard_fails(roots, batch_dir):
    seam = task8.make_evaluate_seam(
        FakeVisualGateway(_gait_raw()), "T8-B1-visual", "t8")
    with pytest.raises(GraphStateError, match="telemetry_uri"):
        seam({"agent_runs": [{}]})
    with pytest.raises(GraphStateError, match="summary 不存在"):
        seam({"telemetry_uri": str(roots["tmp"] / "nope.json"),
              "agent_runs": [{}]})

    (batch_dir / task8.SPLITS[1][1]).unlink()
    with pytest.raises(GraphStateError, match="对照分屏视频缺失"):
        seam({"telemetry_uri": str(batch_dir / "summary.json"),
              "agent_runs": [{}]})


def test_13_tilt_rounding_tolerance_and_rung2_disagreement(roots, batch_dir):
    """series round3→重算 round2 与 outcomes round2 末位差 0.01° 容忍；0.05 抓出。"""
    p = batch_dir / "telemetry" / "ice_nominal_s1.json"
    data = json.loads(p.read_text())
    declared = float(data["outcomes"]["tilt_max_deg"])

    data["outcomes"]["tilt_max_deg"] = round(declared + 0.01, 2)
    p.write_text(json.dumps(data), encoding="utf-8")
    r = evaluate_batch(batch_dir)
    assert r["rung2"]["pass"] is True

    data["outcomes"]["tilt_max_deg"] = round(declared + 0.05, 2)
    p.write_text(json.dumps(data), encoding="utf-8")
    r = evaluate_batch(batch_dir)
    assert r["rung2"]["pass"] is False
    diffs = r["rung2"]["checks"]["disagreements"]
    assert any("tilt_max_deg" in d for d in diffs)


def test_14_aggregate_rung4_statuses():
    def _res(status, mode, *, defects=0, low=False, disagree=None):
        items = [{"id": f"q{i}", "answer": "no" if i <= defects else "yes",
                  "defect": i <= defects} for i in range(1, 7)]
        return Rung4Result(status=status, mode=mode, items=items,
                           n_defects=defects, low_confidence=low,
                           disagreement=disagree)

    unc = task8._aggregate_rung4([
        {"split": "a.mp4", "result": _res("uncalibrated", "uncalibrated"),
         "collage_png": "x"},
        {"split": "b.mp4", "result": _res("uncalibrated", "uncalibrated"),
         "collage_png": "x"}])
    assert unc["status"] == "uncalibrated" and unc["mode"] == "uncalibrated"

    err = task8._aggregate_rung4([
        {"split": "a.mp4", "result": _res("ok", "active"),
         "collage_png": "x"},
        {"split": "b.mp4",
         "result": _res("vlm_error", "active"), "collage_png": "x"}])
    assert err["status"] == "vlm_error" and err["n_items"] == 12

    disagree = task8._aggregate_rung4([
        {"split": "a.mp4",
         "result": _res("ok", "active",
                        disagree="rules_pass_vlm_defects"),
         "collage_png": "x"},
        {"split": "b.mp4", "result": _res("ok", "active", defects=1),
         "collage_png": "x"}])
    assert disagree["status"] == "ok"
    assert disagree["disagreement"].startswith("a.mp4:")
    assert disagree["n_defects"] == 1


def test_15_contrast_l571_contradiction_is_a_conclusion():
    """正式批同款形态：对照自身不稳健 + DR 方向 worsened，不调参、只报告。"""
    def _g(fell, mean):
        return {"n": 3, "n_fell": fell,
                "postpush_vmax_mean_mps": mean}
    groups = {"normal": _g(2, 4.828), "ice_nominal": _g(3, 3.98),
              "ice_dr": _g(3, 13.119)}
    c = contrast_block(groups)
    assert c["control_stands"] is False
    assert c["ordered_slip"] is False
    assert c["dr_vs_nominal"]["direction"] == "worsened"
    assert c["hypothesis_status"] == "contradiction_control_not_robust"

    # 可分辨形态：对照全活、冰/干比 ≥2、DR 双向改善 → resolved/improved
    good = {"normal": _g(0, 0.3), "ice_nominal": _g(3, 6.0),
            "ice_dr": _g(0, 0.5)}
    c2 = contrast_block(good)
    assert c2["hypothesis_status"] == "resolved"
    assert c2["dr_vs_nominal"]["direction"] == "improved"


# ---------------------------------------------------------------------------
# Gate2 三问说明 + 卡片分支
# ---------------------------------------------------------------------------

def test_20_explanation_genesis_grounded_and_l571():
    state = {"chosen": {"sim_engine": "genesis",
                        "name": "接触域随机化零样本部署方案"}}
    expl = task8.build_gate2_explanation(state, {"duration_ms": 16000})
    assert set(expl) == {"task", "problem", "video"}
    assert "接触域随机化零样本部署方案" in expl["task"]
    assert all(k in expl["task"] for k in ("normal", "ice_nominal", "ice_dr"))
    assert "35 N×0.2 s" in expl["task"]
    assert "μ∈[0.05,0.4]" in expl["task"]
    assert "矛盾本身就是结论" in expl["problem"]
    assert "禁止调参到好看为止" in expl["problem"]
    assert "≥2" in expl["problem"] and "预注册" in expl["problem"]
    assert "Rung4" in expl["problem"] and "永不单独放行" in expl["problem"]
    assert "split_normal_vs_ice_nominal_s0.mp4" in expl["video"]
    assert "约 16 s" in expl["video"]
    # 协议外引擎不编故事
    assert task8.build_gate2_explanation(
        {"chosen": {"sim_engine": "carla"}}, {}) is None


def test_21_cards_task8_batch_branch():
    # 全合成 metrics（数值取自正式 pregate 批的 groups/contrast，渲染分支测试
    # 不重新跑判定器；真判定器路径在 test_10/13 覆盖）。
    metrics = {
        "metrics_kind": "task8_gait_batch", "engine": "genesis",
        "run_id": "t8-r1", "workdir": "/tmp/wd", "seeds": 3,
        "vector": {"rung1": True, "rung2": True, "rung3": True},
        "rules_pass": True, "schema_errors": [],
        "rungs": {"rung1": {"pass": True, "checks": {
                      "artifacts_complete": True, "missing_artifacts": [],
                      "n_runs": 9, "seeds": 3, "all_finite": True,
                      "calibration": {"all_bit_exact": True,
                                      "selected_window": {"push_n_N": 35,
                                                          "push_dur_s": 0.2}}}},
                  "rung2": {"pass": True, "checks": {"disagreements": []}},
                  "rung3": {"pass": True, "checks": {
                      "slip_v_bound_mps": 50.0,
                      "contact_force_bound_N": 10000.0,
                      "timeline_regular_dt_s": 0.02,
                      "disturbance": "35N×0.2s", "violations": []}},
                  "rung4": {"status": "uncalibrated", "mode": "uncalibrated",
                            "items": [], "splits": []}},
        "groups": {
            "normal": {"n": 3, "success_rate": 0.3333, "n_fell": 2,
                       "fall_frames": [None, 231, 254],
                       "postpush_vmax_max_mps": 7.21,
                       "postpush_vmax_mean_mps": 4.828,
                       "slip_dist_mean_m": 0.1424,
                       "tilt_max_deg_max": 169.96},
            "ice_nominal": {"n": 3, "success_rate": 0.0, "n_fell": 3,
                            "fall_frames": [257, 261, 264],
                            "postpush_vmax_max_mps": 5.644,
                            "postpush_vmax_mean_mps": 3.98,
                            "slip_dist_mean_m": 0.1834,
                            "tilt_max_deg_max": 157.42},
            "ice_dr": {"n": 3, "success_rate": 0.0, "n_fell": 3,
                       "fall_frames": [62, 93, 254], "mu_per_seed": [
                           0.0897, 0.1125, 0.3385],
                       "postpush_vmax_max_mps": 19.198,
                       "postpush_vmax_mean_mps": 13.119,
                       "slip_dist_mean_m": 0.4364,
                       "tilt_max_deg_max": 139.36}},
        "contrast": {"control_stands": False,
                     "ice_over_normal_postpush_ratio": 0.824,
                     "preregistered_ratio": 2.0, "ordered_slip": False,
                     "dr_vs_nominal": {"fewer_falls": False,
                                       "lower_postpush_slip": False,
                                       "direction": "worsened"},
                     "hypothesis_status": "contradiction_control_not_robust",
                     "l571_note": "矛盾/不可分辨即结论"},
        "dr_envelope": {"mu_loguniform": [0.05, 0.4]},
        "disturbance": {"push_n": 35},
        "artifacts": {"csv": "dr_controls_vs_experimental.csv",
                      "summary": "summary.json", "collage": "collage.png",
                      "splits": []}}
    from orchestrator.feishu_gateway.cards import _rungs_metrics_md
    md = _rungs_metrics_md(metrics)
    assert "Task8 DR 对照 batch" in md
    assert "0.3333" in md and "[62, 93, 254]" in md
    assert "contradiction_control_not_robust" in md and "worsened" in md
    assert "L571" in md and "矛盾/不可分辨即结论" in md
    assert "未标定" in md and "dr_controls_vs_experimental.csv" in md


# ---------------------------------------------------------------------------
# CLI 装配
# ---------------------------------------------------------------------------

class _Dummy:
    pass


def _cli_args(**over):
    args = argparse.Namespace(
        track="track2", index=None, question="油污冰块地面 DR 怎么设计？",
        task_title="", thread="tt", batch_id="T8-B0-macro", task=8,
        coding_batch_id="T8-B1-coding", visual_batch_id="T8-B1-visual")
    for k, v in over.items():
        setattr(args, k, v)
    return args


@pytest.fixture
def patched_cli(monkeypatch):
    from orchestrator.feishu_gateway import client as feishu_client
    from orchestrator.graph import cli

    monkeypatch.setattr(cli, "ModelGateway", lambda *a, **k: _Dummy())
    monkeypatch.setattr(cli, "VectorEngine", lambda *a, **k: _Dummy())
    monkeypatch.setattr(cli, "DirectionSetter", lambda *a, **k: _Dummy())
    monkeypatch.setattr(cli, "LearningStore", lambda *a, **k: _Dummy())
    monkeypatch.setattr(cli, "make_learning_seam", lambda *a, **k: _Dummy())
    monkeypatch.setattr(feishu_client, "gateway_from_env",
                        lambda **kw: _Dummy())
    return cli


def test_30_cli_task8_requires_both_batch_ids(roots, patched_cli):
    with pytest.raises(SystemExit, match="coding-batch-id"):
        patched_cli._production_deps(
            _cli_args(coding_batch_id=""))
    with pytest.raises(SystemExit, match="coding-batch-id"):
        patched_cli._production_deps(
            _cli_args(visual_batch_id=""))


def test_31_cli_task8_injects_seams(roots, patched_cli):
    deps, payload, _ = patched_cli._production_deps(_cli_args())
    assert payload == {"question": "油污冰块地面 DR 怎么设计？"}
    assert callable(deps.agent_run) and callable(deps.evaluate)


def test_32_cli_task7_still_injects(roots, patched_cli):
    # 回归：cli 改 7/8 并支后 task=7 装配不得受影响
    deps, _, _ = patched_cli._production_deps(
        _cli_args(task=7, track="track1", question="MuJoCo 探针",
                  batch_id="T7-B0-macro",
                  coding_batch_id="T7", visual_batch_id="T7V"))
    assert callable(deps.agent_run) and callable(deps.evaluate)


def test_33_cli_other_tasks_keep_none_seams(roots, patched_cli):
    deps, _, _ = patched_cli._production_deps(_cli_args(task=2))
    assert deps.agent_run is None and deps.evaluate is None


class _CapturingFeishu:
    def __init__(self):
        self.gate2: list[tuple] = []

    def request_gate1_approval(self, *a, **k):
        return {"decision": "approve"}

    def request_gate2_approval(self, *a, **k):
        self.gate2.append((a, k))
        return {"decision": "approve"}

    def close(self):
        pass


def test_34_cli_gate2_passes_explanation_only_task8(
        roots, patched_cli, monkeypatch):
    from orchestrator.feishu_gateway import client as feishu_client
    fake = _CapturingFeishu()
    monkeypatch.setattr(feishu_client, "gateway_from_env", lambda **kw: fake)

    deps, _, _ = patched_cli._production_deps(_cli_args())
    state = {"chosen": {"name": "DR 方案", "sim_engine": "genesis"},
             "metrics": {}, "agent_runs": [{"duration_ms": 16000}]}
    assert deps.gate2(state)["decision"] == "approve"
    expl = fake.gate2[-1][1]["explanation"]
    assert set(expl) == {"task", "problem", "video"}
    assert "三臂" in expl["task"] and "矛盾本身就是结论" in expl["problem"]


# ---------------------------------------------------------------------------
# L-14：4–8 帧手算玩具（期望值硬编码，不复用 recompute_run 自证）
# ---------------------------------------------------------------------------

def _toy_series_data(*, slip, force, z, tilt, push_t=0.04):
    n = len(z)
    t = [round(0.02 * i, 4) for i in range(n)]
    return {
        "series": {
            "t": t, "torso_z": z, "torso_x": [0.0] * n, "tilt_deg": tilt,
            "vx": [0.0] * n,
            "contact_force_R": force, "contact_force_L": force,
            "slip_v_R": slip, "slip_v_L": slip,
        },
        "params": {"arm": "normal",
                   "push": {"n": 35.0, "dur": 0.2, "t": push_t}},
    }


def test_40_toy_hand_computed_no_fall_slip_with_abs():
    """6 帧手算：接触 i∈{1,2,3}（力门 2N），滑移含一帧负值 −0.1 m/s。

    slip_dist = (|0.2|+|0.4|+|-0.1|)*0.02 = 0.014（abs 口径）；
    若误用带符号和会得 (0.2+0.4-0.1)*0.02 = 0.010（L-13 防的就是它）；
    推力 t=.04，方窗 [.04,2.54) 覆盖 i=2..5，窗内峰速 0.4 m/s。
    """
    data = _toy_series_data(
        slip=[0.0, 0.2, 0.4, -0.1, 0.0, 0.0],
        force=[0.0, 5.0, 5.0, 5.0, 0.0, 0.0],
        z=[1.27] * 6, tilt=[3.0] * 6)
    rec = recompute_run(data)
    # 硬编码期望
    assert rec["fell"] is False
    assert rec["fall_frame"] is None and rec["fall_time_s"] is None
    assert rec["torso_z_min"] == 1.27 and rec["tilt_max_deg"] == 3.0
    assert rec["feet"]["R"]["contact_frames"] == 3
    assert rec["feet"]["R"]["slip_dist_m"] == 0.014
    assert rec["feet"]["R"]["slip_dist_m"] != 0.010  # 带符号和会被 abs 纠正
    assert rec["feet"]["R"]["slip_vmax_mps"] == 0.4
    assert rec["feet"]["R"]["slip_vmax_postpush_mps"] == 0.4
    assert rec["feet"]["R"]["contact_force_peak_N"] == 5.0
    # L 脚同形
    assert rec["feet"]["L"]["slip_dist_m"] == 0.014


def test_41_toy_hand_computed_fall_frame_and_time():
    """6 帧手算：z 在 i=3 跌破 0.85m 判据（该帧 tilt 仍 4°，非倾角触发）。"""
    data = _toy_series_data(
        slip=[0.0] * 6, force=[0.0] * 6,
        z=[1.27, 1.27, 1.27, 0.80, 0.70, 0.60],
        tilt=[4.0, 4.0, 4.0, 4.0, 4.0, 4.0])
    rec = recompute_run(data)
    assert rec["fell"] is True
    assert rec["fall_frame"] == 3
    assert rec["fall_time_s"] == 0.06     # 3 / 50Hz
    assert rec["torso_z_min"] == 0.6


def test_42_toy_rung2_declared_signed_slip_is_caught():
    """outcomes 自报带符号滑移和 0.1 vs 重算 abs 口径 0.14：Rung2 必须抓出。

    期望值全部手写（不经 recompute_run 倒填），容差 0.05 相对 + 0.005 绝对
    （max≈0.007）拦不住 0.04 的差。
    """
    data = _toy_series_data(
        slip=[0.0, 2.0, 4.0, -1.0, 0.0, 0.0],
        force=[0.0, 5.0, 5.0, 5.0, 0.0, 0.0],
        z=[1.27] * 6, tilt=[3.0] * 6)
    outcomes = {
        "fell": False, "fall_frame": None, "fall_time_s": None,
        "torso_z_min": 1.27, "tilt_max_deg": 3.0,
        "vx_mean_tail": 0.0, "torso_x_last": 0.0,
        # 重算真值是 (2+4+1)*.02=.14；自报 .10（带符号 (2+4-1)*.02）
        "slip_dist_m": {"R": 0.10, "L": 0.10},
        "slip_v_max_mps": {"R": 4.0, "L": 4.0},
        "slip_v_max_postpush_mps": {"R": 4.0, "L": 4.0},
        "contact_force_peak_N": {"R": 5.0, "L": 5.0},
        "contact_frames": {"R": 3, "L": 3},
    }
    data["outcomes"] = outcomes
    r2 = rung2_check({"toy_run": data}, [])
    assert r2["pass"] is False
    diffs = r2["checks"]["disagreements"][0]
    assert "slip_dist_R" in diffs and "slip_dist_L" in diffs
    assert diffs["slip_dist_R"]["recomputed"] == 0.14
    assert diffs["slip_dist_R"]["declared"] == 0.1


# ---------------------------------------------------------------------------
# L-3：outcomes 形状 / CSV 缺列 → schema_errors（裸 KeyError 不许逃出）
# ---------------------------------------------------------------------------

def test_43_validate_outcomes_shape_rejects_bad_shapes():
    base = _run_data("normal", 0, None)
    good = _outcomes_from(recompute_run(base))
    # 合成夹具结构全合法（含 outcomes 形状）
    assert validate_telemetry_structure(
        {**base, "outcomes": good}, "normal_s0") == []

    bad_cases = [
        {"fell": "yes"},                                   # fell 非布尔
        {"fall_frame": -1},                                # 负帧号
        {"fall_frame": True},                              # bool 不算 int
        {"torso_z_min": "1.2"},                            # 数值字段给串
        {"slip_dist_m": {"R": 0.1}},                       # 缺 L
        {"slip_dist_m": "n/a"},                            # 非 {R,L} 对象
        {"contact_frames": {"R": 3}},                      # 缺 L
        {"contact_frames": {"R": "x", "L": 3}},            # R 非整数
    ]
    for patch in bad_cases:
        broken = json.loads(json.dumps(good))
        broken.update(patch)
        errs = validate_telemetry_structure(
            {**base, "outcomes": broken}, "normal_s0")
        assert any("outcomes" in e for e in errs), \
            f"坏形状未被抓：{patch}；errs={errs}"


def test_44_missing_csv_column_is_schema_error(roots, batch_dir):
    csv_path = batch_dir / "dr_controls_vs_experimental.csv"
    rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))
    kept = [c for c in CSV_FIELDS if c != "slip_vmax_R_mps"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=kept)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in kept})
    result = evaluate_batch(batch_dir)
    assert result["ok"] is False and result["schema_errors"]
    assert any("缺列" in e and "slip_vmax_R_mps" in e
               for e in result["schema_errors"])


def test_45_l6_schema_error_raises_before_any_visual_call(roots, batch_dir):
    """L-6：record_only 下结构错误也必须在实呼前熔断（gw 被触即失败）。"""
    _set_gait_calibration(roots, "record_only")
    p = batch_dir / "telemetry" / "normal_s0.json"
    data = json.loads(p.read_text())
    data["outcomes"]["fell"] = "not-a-bool"
    p.write_text(json.dumps(data), encoding="utf-8")

    def _boom(*a, **k):  # pragma: no cover - 结构熔断后绝不允许发视觉实呼
        raise AssertionError("schema 错误未在 Rung4 实呼前熔断")

    gw = FakeVisualGateway(_gait_raw())
    gw.call_visual = _boom
    seam = task8.make_evaluate_seam(gw, "T8-B1-visual", "t8")
    with pytest.raises(GraphStateError, match="结构校验失败"):
        seam({"telemetry_uri": str(batch_dir / "summary.json"),
              "agent_runs": [{}]})


# ---------------------------------------------------------------------------
# L-8：跨文件常量一致性（零成本 AST：不 import genesis 引擎）
# ---------------------------------------------------------------------------

def test_46_postpush_window_literal_matches_threshold_constant():
    # Task0b：经 resolver 从内容层取 fixture genesis 脚本（引擎仓不再留副本）。
    manifest = resolve_solution(
        task8.ENGINE_SCENARIOS["genesis"]["solution_id"])
    script = (manifest.solution_dir / manifest.entrypoint).read_text()
    tree = ast.parse(script)

    class _WindowVisitor(ast.NodeVisitor):
        def __init__(self):
            self.literal = None

        def visit_Assign(self, node):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "in_window":
                    # push["t"] + <literal>
                    b = node.value
                    if (isinstance(b, ast.Compare) and len(b.ops) == 2
                            and isinstance(b.comparators[1], ast.BinOp)
                            and isinstance(b.comparators[1].op, ast.Add)):
                        rhs = b.comparators[1].right
                        if isinstance(rhs, ast.Constant):
                            self.literal = rhs.value
            self.generic_visit(node)

    v = _WindowVisitor()
    v.visit(tree)
    assert v.literal is not None, "未在 genesis 脚本找到 in_window 字面量"
    assert v.literal == 2.5
    assert v.literal == POSTPUSH_WINDOW_S, (
        "genesis 脚本推后方窗与 gait_thresholds.POSTPUSH_WINDOW_S 漂移；"
        "两处与标定件 v2 必须同源（见 gait_thresholds docstring）")


# ---------------------------------------------------------------------------
# L-1/L-7：Rung4 调用级重放与失败证据（task8 双分屏）
# ---------------------------------------------------------------------------

def test_47_replay_second_seam_run_zero_paid_and_evidence_untouched(
        roots, batch_dir, monkeypatch):
    """首调成功后整节点重放：0 二次付费，req/resp 证据不被覆盖。

    L-1/C2：命中时不进 billing_context（ledger 行只可能在该上下文内产生），
    两 split 重放均零计费上下文进入。
    """
    _set_gait_calibration(roots, "record_only")
    gw = FakeVisualGateway(_gait_raw())
    billing_entries: list[dict] = []
    real_billing = task8.billing_context

    @contextlib.contextmanager
    def _spy_billing(**kw):
        billing_entries.append(kw)
        with real_billing(**kw) as ctx:
            yield ctx

    monkeypatch.setattr(task8, "billing_context", _spy_billing)
    seam = task8.make_evaluate_seam(gw, "T8-B1-visual", "t8")
    state = {"telemetry_uri": str(batch_dir / "summary.json"),
             "agent_runs": [{}]}
    seam(state)
    assert len(gw.visual_calls) == 2
    assert len(billing_entries) == 2          # 两 split 各一次 miss 实呼

    evd = roots["phase1"] / "task8_track2" / "evidence"
    files = sorted(evd.glob("rung4_t8-r1_*.json"))
    assert len(files) == 4                    # 两 split × req/resp
    before = {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in files}
    cache_dir = roots["phase1"] / ".call_cache"
    assert cache_dir.is_dir()
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    cached = list(cache_dir.iterdir())
    assert len(cached) == 2                  # 两分屏各一条
    for c in cached:
        assert stat.S_IMODE(c.stat().st_mode) == 0o600

    # 整节点重放（kill-9 落节点返回前的 LangGraph 语义）
    metrics2 = seam(state)
    assert len(gw.visual_calls) == 2         # 零新增实呼
    assert len(billing_entries) == 2         # 命中：不进 billing_context
    for name, (content, mtime) in before.items():
        f = evd / name
        assert f.read_bytes() == content     # 证据内容不被覆盖
        assert f.stat().st_mtime_ns == mtime  # 证据文件不被重写
    # 重放仍能组出带 raw 引用的 Rung4 结果
    assert metrics2["rung4"]["status"] == "ok"
    assert all(s["raw_text_ref"] for s in metrics2["rung4"]["splits"])


class _ExplodingVisualGateway:
    def __init__(self):
        self.calls = 0

    def model_id(self, stage):
        return "fixture-visual-model-000"

    def call_visual(self, *a, **k):
        self.calls += 1
        raise RuntimeError("simulated visual outage")


def test_48_l7_visual_exception_writes_fail_resp_and_no_cache(roots, batch_dir):
    """实呼异常：except 内补脱敏 ok:false resp 后重抛；成功才写缓存。"""
    from orchestrator.common.callcache import CallReplay
    cache_dir = roots["phase1"] / ".call_cache"
    cache = CallReplay(cache_dir)
    split_key = task8.SPLITS[0][0]
    evg = task8._VisualEvidenceGateway(
        _ExplodingVisualGateway(), thread="t8", run_id="t8-r1",
        batch_id="T8-B1-visual", split_key=split_key,
        collage_path=str(batch_dir / "rung4_collage_x.png"),
        cache=cache, cache_key=f"task8_rung4_t8-r1_{split_key}")
    with pytest.raises(RuntimeError, match="visual outage"):
        evg.call_visual("prompt", "x" * 2000, max_tokens=512)
    evd = roots["phase1"] / "task8_track2" / "evidence"
    resp = json.loads(
        (evd / f"rung4_t8-r1_{split_key}.resp.json").read_text())
    assert resp["ok"] is False and "visual outage" in resp["error"]
    assert list(cache_dir.iterdir()) == []     # 失败不留缓存（安全方向：重呼）
