#!/usr/bin/env python3
"""Task5 单测：Rung1–4 分级验证（0 付费，视觉路径全 monkeypatch）。

覆盖（计划 L508-530）：
- 五类公式 telemetry 夹具：隧穿（逐子步穿模）、NaN、未到位、接触不稳/过短、
  jerk 尖峰 → 对应 Rung 拦截；好片全过；
- goal_reached 输入布尔被忽略（只从位姿时序算）；dt 锚定带外保守判负；
- Rung4：uncalibrated 零调用、active 分歧两方向（D8 banner）、record_only
  照显但无自动效力、vlm_error/low_confidence 升级、parse 非法值报错；
- 硬规则1：call_visual 经真实 ModelGateway 时 preflight 收到的 stage 恒为
  visual_rollout；macro/coding 文本入口收到 image block 在 preflight 前拒绝；
- 拼图选帧/PNG 尺寸、合成片 ground_truth 与可复现注入；
- calibrate CLI：prepare 落盘/latest 软链；run 混淆矩阵→active/record_only；
  无批次凭据 fail-closed 零调用；
- 图接缝 file_backed_evaluate 装配 Rung1–3，旧最小夹具不炸图。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.gateway.quota_ledger import (
    QuotaError,
    billing_context,
    write_approval,
)
from orchestrator.gateway.router import (
    NonRetryableError,
    VISION_STAGE,
    ModelGateway,
)
from orchestrator.graph import file_backed_evaluate
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge import calibrate as cal_mod
from orchestrator.judge.clips import render_clip
from orchestrator.judge.collage import (
    build_collage_png,
    select_frame_indices,
)
from orchestrator.judge.rung4 import (
    MODE_ACTIVE,
    MODE_RECORD_ONLY,
    MODE_UNCALIBRATED,
    Rung4Judge,
    combine_vector,
    load_rubric,
    parse_answers,
)
from orchestrator.judge.rungs import (
    evaluate_rollout,
    rung1_check,
    rung2_check,
    rung3_check,
    validate_telemetry,
)
from orchestrator.judge.seam import assemble_file_metrics
from orchestrator.judge.thresholds import jerk_threshold

RUBRIC = load_rubric()
QIDS = [q["id"] for q in RUBRIC["questions"]]


# ---------------------------------------------------------------------------
# 公式 telemetry 夹具（数值全部由公式生成，_fixture 自标；非按期望倒填）
# ---------------------------------------------------------------------------

def make_telemetry(defect: "str | None" = None, *, T: int = 90,
                   dt: float = 1.0 / 60.0, spf: int = 1) -> dict:
    """90 帧 @60Hz=1.5s：末 1.0s 恒力夹持；ee 直线运动（jerk=0）；物在盒心。"""
    ee = [[0.30 + 0.20 * k / (T - 1), 0.0, 0.40] for k in range(T)]
    center = [0.40, 0.0, 0.45]
    obj = [list(center) for _ in range(T)]
    active = [k >= T - 60 for k in range(T)]          # 连续 60 帧 = 1.0 s
    forces = [0.0 if not a else 5.0 for a in active]
    pens = [0.0] * (T * spf)
    data = {
        "_fixture": True,
        "_formula": "ee_x=0.30+0.20*k/(T-1); F=5·1[k>=T-60]; pen=0",
        "dt": dt, "ee_positions": ee, "object_poses": obj,
        "goal_box": {"center": center, "half_extents": [0.01, 0.01, 0.01]},
        "penetrations": pens, "contact_forces": forces,
        "contact_active": active, "substeps_per_frame": spf,
        "engine": "formula-fixture", "run_id": f"fix-{defect or 'good'}",
    }
    if defect == "tunnel":
        # 单物理子步 0.02 m 深接触（远超 0.005 m）
        pens[10 * spf + 1 if spf > 1 else 45] = 0.02
    elif defect == "nan":
        ee[10][1] = float("nan")
    elif defect == "miss_goal":
        for k in range(T - 30, T):                   # 末 0.5 s 全部在盒外
            obj[k][0] = center[0] + 0.30
    elif defect == "contact_unstable":
        data["contact_forces"] = [
            0.0 if not a else (5.0 if k % 2 == 0 else 1.0)
            for k, a in enumerate(active)]
    elif defect == "contact_short":
        data["contact_active"] = [k >= T - 30 for k in range(T)]  # 仅 0.5 s
        data["contact_forces"] = [
            0.0 if not a else 5.0 for a in data["contact_active"]]
    elif defect == "jerk_spike":
        # 第 45 帧单帧 +2 cm 尖峰；中心三阶差分 ≈ 0.02/dt³≈4320 m/s³
        ee[45][1] += 0.02
    return data


def test_00_schema_shapes():
    assert validate_telemetry(make_telemetry()) == []
    bad = make_telemetry()
    del bad["run_id"]
    errs = validate_telemetry(bad)
    assert any("run_id" in e for e in errs)
    bad2 = make_telemetry()
    bad2["penetrations"].append(0.0)
    assert any("penetrations 长度" in e for e in validate_telemetry(bad2))
    bad3 = make_telemetry()
    bad3["dt"] = True
    assert any("dt" in e for e in validate_telemetry(bad3))
    bad4 = make_telemetry()
    bad4["substeps_per_frame"] = True
    assert any("substeps" in e for e in validate_telemetry(bad4))
    bad5 = make_telemetry(T=3)
    assert any("至少 6 帧" in e for e in validate_telemetry(bad5))


def test_01_good_fixture_all_rungs_pass():
    res = evaluate_rollout(make_telemetry())
    assert res["schema_errors"] == []
    assert res["vector"] == {"rung1": True, "rung2": True, "rung3": True}
    assert res["rules_pass"] is True
    assert res["rung4"] is None                      # Task5 接缝不挂 VLM
    # goal 判定来源标注：忽略任何输入布尔
    assert res["rung2"]["checks"]["goal_from_poses"] is True


def test_02_rung1_tunnel_and_nan():
    tun = make_telemetry("tunnel")
    r1 = rung1_check(tun)
    assert r1["pass"] is False
    assert r1["checks"]["max_penetration_m"] == pytest.approx(0.02)
    # 锁内适配：逐子步接触深度，不得自称 swept CCD
    assert r1["checks"]["ccd_method"] == "per_substep_contact_depth"
    assert r1["checks"]["ccd_swept"] is False
    spf2 = make_telemetry("tunnel", spf=2)
    assert rung1_check(spf2)["checks"]["max_penetration_m"] == \
        pytest.approx(0.02)
    nanres = rung1_check(make_telemetry("nan"))
    assert nanres["pass"] is False
    assert any("ee_positions[10][1]" in p for p in nanres["nonfinite_paths"])


def test_03_rung2_goal_from_poses_ignores_input_bool():
    miss = rung2_check(make_telemetry("miss_goal"))
    assert miss["pass"] is False
    assert miss["checks"]["last_window_all_inside_box"] is False
    # 位姿在盒内但输入谎称 goal_reached=False → 仍通过
    good = make_telemetry()
    good["goal_reached"] = False
    assert rung2_check(good)["pass"] is True
    assert rung2_check(good)["checks"]["goal_input_bool_ignored"] is True
    # 位姿在盒外但输入谎称 goal_reached=True → 仍判负
    lie = make_telemetry("miss_goal")
    lie["goal_reached"] = True
    assert rung2_check(lie)["pass"] is False


def test_04_rung2_contact_stability():
    unst = rung2_check(make_telemetry("contact_unstable"))
    assert unst["pass"] is False
    assert unst["checks"]["contact_cv"] > 0.3
    short = rung2_check(make_telemetry("contact_short"))
    assert short["pass"] is False
    assert short["checks"]["contact_longest_run_s"] == pytest.approx(0.5)
    # 零力窗口：mean≈0 → cv=inf 不得除零
    zero = make_telemetry()
    zero["contact_forces"] = [0.0] * len(zero["contact_forces"])
    assert rung2_check(zero)["checks"]["contact_cv"] is None


def test_05_rung3_jerk_and_dt_band():
    sp = rung3_check(make_telemetry("jerk_spike"))
    assert sp["pass"] is False
    assert sp["checks"]["over_threshold_count"] >= 1
    assert sp["checks"]["jerk_max_mps3"] > 500.0
    good = rung3_check(make_telemetry())
    assert good["pass"] is True
    assert good["checks"]["jerk_max_mps3"] == pytest.approx(0.0, abs=1e-6)
    # dt 带外：10 Hz 平滑片段也保守判负，不出 jerk 值
    slow = rung3_check(make_telemetry(T=20, dt=0.1))
    assert slow["pass"] is False
    assert slow["checks"]["dt_band_ok"] is False
    # 阈值随 dt 缩放：30Hz→62.5；120Hz→4000
    assert jerk_threshold(1 / 30) == pytest.approx(62.5)
    assert jerk_threshold(1 / 120) == pytest.approx(4000.0)


def test_06_rung4_uncalibrated_makes_zero_calls(tmp_path):
    gw = FakeVisualGateway(vlm_answers("yes" for _ in QIDS))
    judge = Rung4Judge(gw, calibration_file=tmp_path / "nope.json")
    res = judge.judge("Zm9v", rules_pass=True)
    assert res.status == MODE_UNCALIBRATED
    assert gw.visual_calls == []                       # 未标定零网络（安全默认）
    combined = combine_vector(
        {"rung1": {"pass": True}, "rung2": {"pass": True},
         "rung3": {"pass": True}}, res)
    assert combined["escalate_human"] is False


def test_07_rung4_active_disagreement_d8_both_directions(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"mode": MODE_ACTIVE, "bad_clip_misses": [],
                               "good_clip_false_alarms": []}), encoding="utf-8")
    rules_ok = {"rung1": {"pass": True}, "rung2": {"pass": True},
                "rung3": {"pass": True}}
    rules_bad = {"rung1": {"pass": False}, "rung2": {"pass": True},
                 "rung3": {"pass": True}}
    # 规则否决但 VLM 全绿 → rules_fail_vlm_clean
    gw = FakeVisualGateway(vlm_answers("yes" for _ in QIDS))
    r = Rung4Judge(gw, calibration_file=cal).judge("Zm9v", rules_pass=False)
    assert r.disagreement == "rules_fail_vlm_clean"
    out = combine_vector(rules_bad, r)
    assert out["auto_release"] is False                # 规则可否决，VLM 永不放行
    assert "规则/VLM 分歧" in out["banner"]
    # 规则全过但 VLM 报 q1 缺陷 → rules_pass_vlm_defects
    gw2 = FakeVisualGateway(vlm_answers(
        "no" if q == "q1" else "yes" for q in QIDS))
    r2 = Rung4Judge(gw2, calibration_file=cal).judge("Zm9v", rules_pass=True)
    assert r2.disagreement == "rules_pass_vlm_defects"
    out2 = combine_vector(rules_ok, r2)
    assert out2["auto_release"] is True                # 规则为准仍自动放行…
    assert out2["escalate_human"] is True              # …但升级人裁决（D8）


def test_08_rung4_record_only_displays_without_power(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(
        {"mode": MODE_RECORD_ONLY, "bad_clip_misses": ["q1"],
         "good_clip_false_alarms": []}), encoding="utf-8")
    gw = FakeVisualGateway(vlm_answers("yes" for _ in QIDS))
    # 规则失败 + VLM 全绿：record_only 下仍实呼照显，但分歧标志强制清空
    r = Rung4Judge(gw, calibration_file=cal).judge("Zm9v", rules_pass=False)
    assert r.status == "ok" and len(gw.visual_calls) == 1
    assert r.disagreement is None
    rules_bad = {"rung1": {"pass": False}, "rung2": {"pass": True},
                 "rung3": {"pass": True}}
    out = combine_vector(rules_bad, r)
    assert "banner" not in out
    assert out["auto_release"] is False and out["escalate_human"] is True


def test_09_rung4_vlm_error_and_low_confidence(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"mode": MODE_ACTIVE, "bad_clip_misses": [],
                               "good_clip_false_alarms": []}), encoding="utf-8")
    gw = FakeVisualGateway(boom=True)
    r = Rung4Judge(gw, calibration_file=cal).judge("Zm9v", rules_pass=True)
    assert r.status == "vlm_error"                     # 故障不得变伪造通过
    rules_ok = {"rung1": {"pass": True}, "rung2": {"pass": True},
                "rung3": {"pass": True}}
    assert combine_vector(rules_ok, r)["escalate_human"] is True
    gw2 = FakeVisualGateway(vlm_answers(
        ("yes" for _ in QIDS), low_confidence=True))
    r2 = Rung4Judge(gw2, calibration_file=cal).judge("Zm9v", rules_pass=True)
    assert r2.low_confidence is True
    assert combine_vector(rules_ok, r2)["escalate_human"] is True


def test_10_parse_answers_illegal_and_fenced():
    good = json.dumps(
        {"answers": [{"id": q, "answer": "yes"} for q in QIDS],
         "low_confidence": False})
    assert set(parse_answers(good, RUBRIC)["answers"]) == set(QIDS)
    fenced = f"```json\n{good}\n```"
    assert parse_answers(fenced, RUBRIC)["answers"]["q1"] == "yes"
    with pytest.raises(ValueError):
        parse_answers(good.replace('"yes"', '"maybe"'), RUBRIC)
    missing = json.dumps(
        {"answers": [{"id": q, "answer": "yes"} for q in QIDS if q != "q3"]})
    with pytest.raises(ValueError):
        parse_answers(missing, RUBRIC)
    with pytest.raises(json.JSONDecodeError):
        parse_answers("not json at all", RUBRIC)


def test_11_visual_stage_hard_bound(tmp_path, monkeypatch):
    """真实 ModelGateway：call_visual 内 stage 恒 visual_rollout；
    macro/coding 入口收到 image 在 preflight 前拒绝（零网络零记账）。"""
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path))
    ledger = FakeLedger()
    gw = ModelGateway(ledger=ledger, sleep=lambda _s: None)

    def fake_post(stage, model_id, blocks, **_kw):
        assert any(b.get("type") == "image" for b in blocks)
        text = json.dumps(
            {"answers": [{"id": q, "answer": "yes"} for q in QIDS]})
        return None, {"content": [{"type": "text", "text": text}],
                      "usage": {"input_tokens": 7, "output_tokens": 3}}
    monkeypatch.setattr(gw, "_post_messages", fake_post)
    with billing_context(task=5, batch_id="t5-unit-stage-0001"):
        raw, usage = gw.call_visual("prompt", "Zm9v")
    assert usage == (7, 3) and parse_answers(raw, RUBRIC)["n_questions"] == 6
    stages = [s for kind, s in ledger.calls if kind == "preflight"]
    assert stages == [VISION_STAGE] == ["visual_rollout"]

    # 文本阶段收到 image block：preflight 之前拒绝，账本零调用
    before = len(ledger.calls)
    with pytest.raises(NonRetryableError):
        gw.call("macro_ideation",
                [{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": "Zm9v"}}]}])
    with pytest.raises(NonRetryableError):
        gw.call("coding_debug",
                [{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": "Zm9v"}}]}])
    assert len(ledger.calls) == before
    # 视觉 stage 走文本入口同样被拒
    with pytest.raises(NonRetryableError):
        gw.call(VISION_STAGE, [{"role": "user", "content": "x"}])


def test_12_collage_selection_and_png():
    idx = select_frame_indices(60, 30)
    assert idx == [0, 12, 25, 30, 35, 59]               # 首/填补/接-5/接/接+5/末
    no_contact = select_frame_indices(60, None)
    assert no_contact[3] == 30                          # 无接触帧用中帧（排序后位 3）
    with pytest.raises(ValueError):
        select_frame_indices(0, 1)
    frames, _ = render_clip("good")
    png = build_collage_png([frames[i] for i in idx],
                            [f"f{i}" for i in idx])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    assert im.size == (3 * 320, 2 * (240 + 18))         # 2×3 + 标签条
    with pytest.raises(ValueError):
        build_collage_png(frames[:3])


def test_13_synthetic_clips_deterministic_and_ground_truth():
    g_frames, g_meta = render_clip("good")
    b_frames, b_meta = render_clip("bad")
    g2_frames, _ = render_clip("good")
    b2_frames, _ = render_clip("bad")
    # 固定种子：两片逐帧可复现
    assert g_frames[10].tobytes() == g2_frames[10].tobytes()
    assert b_frames[35].tobytes() == b2_frames[35].tobytes()
    # 中性帧标（无 kind 泄漏）：非注入帧逐像素相同；注入帧几何不同
    def _diff(a, b):
        return sum(abs(x - y) for x, y in zip(a.tobytes(), b.tobytes()))
    assert _diff(g_frames[10], b_frames[10]) == 0
    assert _diff(g_frames[35], b_frames[35]) > 0
    assert g_meta["injected_defects"] == []
    assert len(b_meta["injected_defects"]) == 2
    assert any("penetration" in s for s in b_meta["injected_defects"])
    assert b_meta["contact_frame"] == 30
    # 逐题真值：好片全 yes；坏片 q1–q5 注入缺陷、q6 末帧无注入仍 yes
    assert set(g_meta["expected_answers"].values()) == {"yes"}
    assert b_meta["expected_answers"]["q6"] == "yes"
    assert all(b_meta["expected_answers"][q] == "no"
               for q in ("q1", "q2", "q3", "q4", "q5"))


def test_14_calibrate_prepare_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    info = cal_mod.prepare()
    d = Path(info["dir"])
    assert (d / "good.mp4").is_file() and (d / "bad_collage.png").is_file()
    latest = d.parent / "latest"
    assert latest.is_symlink() and latest.resolve() == d.resolve()

    # 无批次凭据：fail-closed，假网关构造器被调用即测试失败
    def boom_factory(*_a, **_kw):
        raise AssertionError("凭据缺失时不得构造付费网关")
    monkeypatch.setattr(cal_mod, "ModelGateway", boom_factory)
    with pytest.raises(QuotaError):
        cal_mod.run_calibration("t5-nope-0001", str(d))

    # 开凭据：好片全 yes、坏片全 no → active，混淆矩阵 6/6
    write_approval(batch_id="t5-unit-cal-0001", task=5,
                   stage="visual_rollout", approved=2, beyond_normal=False,
                   note="Task5 单测-redacted-0001")
    gw = FakeVisualGateway(multi=multi_answers({
        "good": ("yes", False), "bad": ("no", False)}))
    monkeypatch.setattr(cal_mod, "ModelGateway",
                        lambda ledger=None: gw)
    monkeypatch.setattr(cal_mod, "QuotaLedger", lambda: FakeLedger())
    out = cal_mod.run_calibration("t5-unit-cal-0001", str(d))
    assert out["mode"] == MODE_ACTIVE
    # 好片 6×yes→tn6；坏片 q1–q5 判 no→tp5，q6 真值 yes 但答 no→fp1
    assert out["confusion_matrix"] == {"tp": 5, "fp": 1, "tn": 6, "fn": 0}
    assert out["bad_clip_misses"] == []
    assert out["bad_clip_extra_false_alarms"] == ["q6"]
    assert len(gw.visual_calls) == 2
    cal_file = tmp_path / "phase1" / "task5_judge" / "calibration_6q.json"
    saved = json.loads(cal_file.read_text(encoding="utf-8"))
    assert saved["residual_risk"]                         # 残余风险必须落盘
    assert (tmp_path / "phase1" / "task5_judge" /
            "cal_bad.req.json").is_file()

    # 坏片漏检（q1 误答 yes）→ record_only + fn=1；ev_tag 不覆盖首轮证据
    write_approval(batch_id="t5-unit-cal-0002", task=5,
                   stage="visual_rollout", approved=2, beyond_normal=False,
                   note="Task5 单测-redacted-0002")
    gw2 = FakeVisualGateway(multi=multi_answers({
        "good": ("yes", False),
        "bad": (None, False)}), bad_override={"q1": "yes"})
    monkeypatch.setattr(cal_mod, "ModelGateway",
                        lambda ledger=None: gw2)
    out2 = cal_mod.run_calibration("t5-unit-cal-0002", str(d), ev_tag="r2")
    assert out2["mode"] == MODE_RECORD_ONLY
    assert out2["bad_clip_misses"] == ["q1"]
    assert out2["confusion_matrix"] == {"tp": 4, "fp": 1, "tn": 6, "fn": 1}
    assert (tmp_path / "phase1" / "task5_judge" /
            "cal_bad_r2.req.json").is_file()
    assert (tmp_path / "phase1" / "task5_judge" /
            "cal_bad.req.json").is_file()                 # 首轮证据不被覆盖


def test_15_graph_seam_assembles_rungs(tmp_path):
    good = make_telemetry()
    m = assemble_file_metrics("file://x", good)
    assert m["rules_pass"] is True
    assert m["rungs"]["rung4"] is None
    assert m["vector"] == {"rung1": True, "rung2": True, "rung3": True}
    # 旧最小夹具（Task3 drill 形状）：schema 报错但不炸图，数据键保留
    minimal = {"_fixture": True, "engine": "fixture-engine",
               "run_id": "drill-1", "dt": 0.016,
               "penetrations": [0.0, 0.001, 0.002, 0.003]}
    m2 = assemble_file_metrics("file://y", minimal)
    assert m2["rules_pass"] is False and m2["schema_errors"]
    assert m2["data"]["penetrations"][3] == pytest.approx(0.003)
    # 经图默认接缝：好片落盘 → metrics 带分级向量
    p = tmp_path / "tele.json"
    p.write_text(json.dumps(good), encoding="utf-8")
    metrics = file_backed_evaluate({"telemetry_uri": str(p)})
    assert metrics["rules_pass"] is True
    assert metrics["provenance"] == "file:telemetry_uri"
    with pytest.raises(GraphStateError):
        file_backed_evaluate({})
    with pytest.raises(GraphStateError):
        file_backed_evaluate({"telemetry_uri": str(tmp_path / "missing.json")})


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def vlm_answers(it, *, low_confidence: bool = False) -> str:
    return json.dumps(
        {"answers": [{"id": q, "answer": next(it)} for q in QIDS],
         "low_confidence": low_confidence})


class FakeVisualGateway:
    """记录视觉调用；文本入口被调用即测试失败（Rung4 永远不走文本 stage）。"""

    def __init__(self, text: "str | None" = None, *, boom: bool = False,
                 multi: "dict | None" = None,
                 bad_override: "dict | None" = None) -> None:
        self.visual_calls: list[tuple] = []
        self.text_calls: list[tuple] = []
        self._text = text
        self._boom = boom
        self._multi = multi
        self._bad_override = bad_override or {}

    def model_id(self, stage: str) -> str:
        return "vlm-fixture-model-redacted-0001"

    def call_visual(self, prompt, image_b64, max_tokens=512, **kw):
        if self._boom:
            raise RuntimeError("vlm boom-redacted")
        self.visual_calls.append((prompt, image_b64, max_tokens))
        if self._multi is not None:
            n = len(self.visual_calls)
            kind = "good" if n == 1 else "bad"
            default, low = self._multi[kind]
            if default is None:
                ans = {q: self._bad_override.get(q, "no") for q in QIDS}
            else:
                ans = {q: default for q in QIDS}
            return json.dumps(
                {"answers": [{"id": q, "answer": a}
                             for q, a in ans.items()],
                 "low_confidence": low}), (1, 1)
        return self._text, (1, 1)

    def call(self, stage, messages, **kw):
        self.text_calls.append((stage, messages))
        raise AssertionError(f"Rung4 禁止文本入口：{stage}")


def multi_answers(table: dict) -> dict:
    return table


class FakeLedger:
    def __init__(self):
        self.calls: list[tuple] = []

    def preflight(self, stage, **kw):
        self.calls.append(("preflight", stage))
        return {"status": "ok"}

    def record(self, stage, **kw):
        self.calls.append(("record", stage))
        return {"seq": len(self.calls)}
