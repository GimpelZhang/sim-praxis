#!/usr/bin/env python3
"""Task4 零付费测试：BinEval 5 题严格解析/active fail-closed、frames 管线
纯命令契约、流体/软体/触觉三类度量探针（详细计划 L500-506）。

全部 hermetic：VLM 一律假网关，无网络、无付费、不碰活库/生产 pending db；
标定件用 tmp_path 临时件，BinEvalJudge 经 calibration_file 注入。
"""
from __future__ import annotations

import json
import math
import shlex
from pathlib import Path

import numpy as np
import pytest

from orchestrator.judge import frames, modality_probes as mp
from orchestrator.judge.bineval import (
    AGREEMENT_ACTIVE_THRESHOLD,
    ANSWER_NO,
    ANSWER_YES,
    BinEvalParseError,
    QUESTION_IDS,
    agreement_rate,
    build_calibration_doc,
    build_prompt,
    evaluate_clips_dir,
    load_bineval_rubric,
    parse_bineval_answers,
    write_calibration,
)
from orchestrator.judge.frames import (
    JPEG_MAX_BYTES,
    JPEG_QUALITY_TIERS,
    contact_window_command,
    contact_window_plan,
    size_guard_command,
)
from orchestrator.judge.rung4 import (
    MODE_ACTIVE,
    MODE_RECORD_ONLY,
    MODE_UNCALIBRATED,
    BinEvalJudge,
    BinEvalResult,
    bineval_gate_verdict,
    combine_vector,
)

# 计划 L494-498 五问逐字基线（任何一字漂移即测试失败）。
PLAN_FIVE_QUESTIONS = (
    "接触时序与可见接触力表现是否一致（无隔空受力）？",
    "运动全程是否无穿模/粒子穿透刚体边界？",
    "物体（或人体）在受力后的位移方向是否与物理预期一致？",
    "任务目的是否达成（到位/入杯/保持平衡，按 track 目标槽位化）？",
    "视频是否无异常跳变（瞬移、帧序错乱、物体凭空出现/消失）？",
)
ALL_YES = {q: ANSWER_YES for q in QUESTION_IDS}


# --------------------------------------------------------------------------
# rubric：五题逐字 + 结构
# --------------------------------------------------------------------------

def test_rubric_has_exactly_five_questions_verbatim_from_plan():
    rubric = load_bineval_rubric()
    assert [q["id"] for q in rubric["questions"]] == list(QUESTION_IDS)
    assert [q["text"] for q in rubric["questions"]] == list(
        PLAN_FIVE_QUESTIONS)
    assert rubric["answer_set"] == ["yes", "no"]
    for q in rubric["questions"]:
        assert q["good_answer"] == "yes" and q["defect_if"] == "no"
    # q4 槽位枚举：三 track，且不接受自由文本槽位。
    assert rubric["track_slots"] == ["gait", "libero", "fluid"]
    assert set(rubric["track_goals"]) == {"gait", "libero", "fluid"}
    q4 = rubric["questions"][3]
    assert q4["id"] == "q4"
    for word in ("到位", "入杯", "保持平衡"):
        assert word in q4["text"]


def test_prompt_injects_only_enumerated_track_slot():
    rubric = load_bineval_rubric()
    p = build_prompt(rubric, "fluid")
    assert rubric["track_goals"]["fluid"] in p
    assert build_prompt(rubric, "gait") != p
    with pytest.raises(ValueError):
        build_prompt(rubric, "realworld")


# --------------------------------------------------------------------------
# 严格 JSON 解析
# --------------------------------------------------------------------------

def test_parse_strict_flat_five_keys():
    ans = parse_bineval_answers(
        '{"q1":"yes","q2":"no","q3":"yes","q4":"yes","q5":"yes"}')
    assert ans == {"q1": "yes", "q2": "no", "q3": "yes",
                   "q4": "yes", "q5": "yes"}


@pytest.mark.parametrize("raw", [
    '{"q1":"yes","q2":"yes","q3":"yes","q4":"yes"}',                    # 缺 q5
    '{"q1":"yes","q2":"yes","q3":"yes","q4":"yes","q5":"yes","q6":"no"}',
    '{"q1":"maybe","q2":"yes","q3":"yes","q4":"yes","q5":"yes"}',       # 非 yes/no
    '{"q1":1,"q2":"yes","q3":"yes","q4":"yes","q5":"yes"}',             # 非字符串
    '{"answers":[{"id":"q1","answer":"yes"}]}',                         # 6q 结构
    '```json\n{"q1":"yes","q2":"yes","q3":"yes","q4":"yes","q5":"yes"}```',
    '我很确定：{"q1":"yes","q2":"yes","q3":"yes","q4":"yes","q5":"yes"}',
    '{"q1":"yes","q2":"yes","q3":"yes","q4":"yes","q5":"yes"} 我很有把握',
    '{"q1":"YES","q2":"YES","q3":"YES","q4":"YES","q5":"YES"} extra',
    "not json",
    "",
    "   ",
])
def test_parse_rejects_bad_json_missing_extra_values_confidence(raw):
    with pytest.raises(BinEvalParseError):
        parse_bineval_answers(raw)


def test_agreement_rate_strict_and_count():
    labels = dict(ALL_YES)
    answers = dict(ALL_YES)
    assert agreement_rate(answers, labels) == 1.0
    answers["q2"] = ANSWER_NO
    answers["q5"] = ANSWER_NO
    assert math.isclose(agreement_rate(answers, labels), 0.6)
    with pytest.raises(ValueError):
        agreement_rate({q: "yes" for q in ("q1", "q2")}, labels)
    with pytest.raises(ValueError):
        agreement_rate(dict(ALL_YES, q1="x"), labels)


# --------------------------------------------------------------------------
# 标定目录机判：active fail-closed
# --------------------------------------------------------------------------

def _write_clip(d: Path, name: str, track: str, labels: dict,
                answers: "dict | None" = None):
    cd = d / name
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "ground_truth.json").write_text(
        json.dumps({"track": track, "labels": labels}), encoding="utf-8")
    if answers is not None:
        (cd / "bineval_answer.json").write_text(
            json.dumps({"answers": answers}), encoding="utf-8")


def _populate_clips(d: Path, n: int, *, miss_one_defect: bool = False,
                    low_agreement: bool = False, omit_answer: bool = False,
                    all_good: bool = False):
    """10..n 片：每片 1 个已知缺陷（q2=no），模型默认逐字命中。"""
    defect_labels = dict(ALL_YES); defect_labels["q2"] = ANSWER_NO
    for i in range(n):
        name = f"clip_{i:02d}"
        if all_good:
            labels, answers = ALL_YES, dict(ALL_YES)
        elif i == 3 and miss_one_defect:
            labels, answers = defect_labels, dict(ALL_YES)   # 漏检
        elif i == 4 and low_agreement:
            labels = dict(ALL_YES)
            labels["q2"] = labels["q5"] = ANSWER_NO
            answers = dict(ALL_YES)                          # 3/5 = 0.6
        else:
            labels, answers = defect_labels, dict(defect_labels)
        if omit_answer and i == 7:
            _write_clip(d, name, "libero", labels, None)
        else:
            _write_clip(d, name, ("gait", "libero", "fluid")[i % 3],
                        labels, answers)


def test_calibration_active_when_thresholds_met(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 10)
    summary = evaluate_clips_dir(d)
    assert summary["can_active"] is True
    assert summary["defect_recall"] == 1.0
    assert summary["min_agreement"] == 1.0
    out = tmp_path / "calibration_bineval.json"
    doc = write_calibration(summary, out, batch_id="T4-B1-visual",
                            model="fake")
    assert doc["mode"] == MODE_ACTIVE
    assert not (tmp_path / "bineval_failure_report.json").exists()


def test_calibration_record_only_when_defect_missed(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 10, miss_one_defect=True)
    summary = evaluate_clips_dir(d)
    assert summary["defect_recall"] == pytest.approx(9 / 10)
    assert summary["can_active"] is False
    doc = write_calibration(summary, tmp_path / "calibration_bineval.json")
    assert doc["mode"] == MODE_RECORD_ONLY
    report = json.loads((tmp_path / "bineval_failure_report.json")
                        .read_text(encoding="utf-8"))
    assert report["result"] == "D7_NOT_ACHIEVED"
    assert any("召回" in r for r in report["reasons"])
    assert any(c["clip"] == "clip_03" for c in report["per_clip"])


def test_calibration_record_only_when_per_clip_agreement_below_07(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 10, low_agreement=True)
    summary = evaluate_clips_dir(d)
    assert summary["min_agreement"] == 0.6
    assert AGREEMENT_ACTIVE_THRESHOLD == 0.7
    assert summary["can_active"] is False
    doc = write_calibration(summary, tmp_path / "cal.json")
    assert doc["mode"] == MODE_RECORD_ONLY
    assert any("0.6" in r for r in doc["record_only_reasons"])


def test_calibration_record_only_when_fewer_than_10_clips(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 9)
    summary = evaluate_clips_dir(d)
    assert summary["can_active"] is False
    doc = build_calibration_doc(summary)
    assert doc["mode"] == MODE_RECORD_ONLY
    assert any("片数" in r for r in doc["record_only_reasons"])


def test_calibration_record_only_when_answer_missing_or_bad(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 10, omit_answer=True)
    assert evaluate_clips_dir(d)["can_active"] is False
    # 坏答案（解析失败）同样 fail-closed，不得跳过该片凑 active。
    d2 = tmp_path / "synth2"
    _populate_clips(d2, 10)
    (d2 / "clip_07" / "bineval_answer.json").write_text(
        "我非常确定，但不是 JSON", encoding="utf-8")
    s2 = evaluate_clips_dir(d2)
    assert s2["can_active"] is False and s2["errors"]


def test_calibration_requires_at_least_one_defect_clip(tmp_path):
    d = tmp_path / "synth"
    _populate_clips(d, 10, all_good=True)
    summary = evaluate_clips_dir(d)
    assert summary["n_defect_clips"] == 0
    assert summary["can_active"] is False      # 无缺陷片无法验证召回


# --------------------------------------------------------------------------
# BinEvalJudge：缺件/record_only 零影响；active 三红态
# --------------------------------------------------------------------------

class _FakeGateway:
    def __init__(self, raw: "str | Exception" = None):
        self.raw = raw if raw is not None else json.dumps(ALL_YES)
        self.calls = 0

    def call_visual(self, prompt, image_b64, **kwargs):
        self.calls += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw, (10, 20)


def _cal_file(tmp_path, mode, **fields):
    p = tmp_path / "calibration_bineval.json"
    doc = {"mode": mode, "min_agreement": 1.0, "defect_recall": 1.0,
           "n_clips": 12}
    doc.update(fields)
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_missing_calibration_is_uncalibrated_and_never_calls_gateway(tmp_path):
    gw = _FakeGateway()
    judge = BinEvalJudge(gw, calibration_file=tmp_path / "absent.json")
    r = judge.judge("img", True, "libero")
    assert isinstance(r, BinEvalResult)
    assert r.status == MODE_UNCALIBRATED and r.mode == MODE_UNCALIBRATED
    assert r.escalate_human is False and gw.calls == 0
    v = bineval_gate_verdict(True, r)
    assert v["auto_release"] is True and v["escalate_human"] is False


def test_record_only_channel_has_zero_effect_despite_conflict(tmp_path):
    labels_no = dict(ALL_YES); labels_no["q2"] = "no"
    gw = _FakeGateway(json.dumps(labels_no))   # VLM 报缺陷
    judge = BinEvalJudge(gw, calibration_file=_cal_file(tmp_path,
                                                        MODE_RECORD_ONLY))
    r = judge.judge("img", True, "gait")       # rules 过 + VLM 缺陷 = 分歧
    assert r.status == "ok" and r.disagreement is None
    assert r.escalate_human is False
    v = bineval_gate_verdict(True, r)
    assert v["auto_release"] is True


def test_conflict_forces_gate2_red_in_both_xor_directions(tmp_path):
    cal = _cal_file(tmp_path, MODE_ACTIVE)
    # rules 过、VLM 报缺陷
    one_no = dict(ALL_YES); one_no["q1"] = "no"
    j1 = BinEvalJudge(_FakeGateway(json.dumps(one_no)), calibration_file=cal)
    r1 = j1.judge("img", True, "fluid")
    assert r1.disagreement == "rules_pass_vlm_defects"
    assert r1.escalate_human is True
    assert bineval_gate_verdict(True, r1)["auto_release"] is False
    # rules 挂、VLM 全绿
    j2 = BinEvalJudge(_FakeGateway(json.dumps(ALL_YES)), calibration_file=cal)
    r2 = j2.judge("img", False, "fluid")
    assert r2.disagreement == "rules_fail_vlm_clean"
    assert r2.escalate_human is True
    # 双方一致通过时不升级
    j3 = BinEvalJudge(_FakeGateway(json.dumps(ALL_YES)), calibration_file=cal)
    r3 = j3.judge("img", True, "gait")
    assert r3.escalate_human is False and r3.vlm_pass is True
    assert bineval_gate_verdict(True, r3)["auto_release"] is True


def test_low_agreement_forces_red_without_calling_vlm(tmp_path):
    # active 件自证 min_agreement=0.6：件不可信，不发视觉也不静默降级。
    cal = _cal_file(tmp_path, MODE_ACTIVE, min_agreement=0.6)
    gw = _FakeGateway()
    judge = BinEvalJudge(gw, calibration_file=cal)
    r = judge.judge("img", True, "libero")
    assert r.status == "bad_active_calibration"
    assert r.escalate_human is True and gw.calls == 0
    v = bineval_gate_verdict(True, r)
    assert v["auto_release"] is False and "D7" in v["banner"]


def test_timeout_fail_closed_when_active(tmp_path):
    cal = _cal_file(tmp_path, MODE_ACTIVE)

    class _Timeout(Exception):
        pass

    judge = BinEvalJudge(_FakeGateway(_Timeout("timeout 120s")),
                         calibration_file=cal)
    r = judge.judge("img", True, "gait")
    assert r.status == "vlm_error" and r.escalate_human is True
    assert bineval_gate_verdict(True, r)["escalate_human"] is True
    # record_only 下视觉故障只留痕，不强制升级（无 active 资格不翻成自动通过，
    # 规则不过仍升级）。
    judge2 = BinEvalJudge(_FakeGateway(_Timeout("timeout")),
                          calibration_file=_cal_file(tmp_path,
                                                     MODE_RECORD_ONLY))
    r2 = judge2.judge("img", True, "gait")
    assert r2.status == "vlm_error" and r2.escalate_human is False


def test_bad_json_from_vlm_counts_as_vlm_error_when_active(tmp_path):
    judge = BinEvalJudge(
        _FakeGateway("抱歉我无法输出 JSON"),
        calibration_file=_cal_file(tmp_path, MODE_ACTIVE))
    r = judge.judge("img", True, "gait")
    assert r.status == "vlm_error" and r.escalate_human is True


def test_bineval_none_result_keeps_phase1_gate_snapshot():
    # 字节级不回归：无 BinEval 通道时裁决恒等于「只看规则」的 Phase1 形态。
    v = bineval_gate_verdict(True, None)
    assert v == {"rules_pass": True, "bineval_status": None,
                 "bineval_mode": None, "escalate_human": False,
                 "auto_release": True}
    rules = {"rung1": {"pass": True}, "rung2": {"pass": True},
             "rung3": {"pass": True}}
    combined = combine_vector(rules, None)
    assert combined["auto_release"] is True
    assert combined["escalate_human"] is False
    assert combined["rung4"] is None


# --------------------------------------------------------------------------
# frames 管线：纯命令契约
# --------------------------------------------------------------------------

def test_frame_pipeline_commands():
    plan = contact_window_plan("video.mp4", None, contact_moment_s=1.2)
    assert plan["executed"] is False
    assert plan["fps"] == 10 and plan["short_edge"] == 512
    assert plan["tile"] == "3x2" and plan["jpeg_max_bytes"] == 100_000
    assert [s["quality"] for s in plan["steps"]] == list(JPEG_QUALITY_TIERS)
    for step in plan["steps"]:
        cmd = step["command"]
        argv = shlex.split(cmd)                  # 必须可被 shell 切词
        assert argv[0] == "ffmpeg"
        assert "fps=10" in cmd
        assert "512" in cmd and "tile=3x2" in cmd
        assert "-q:v" in cmd and f" {step['quality']}" in cmd
        guard = step["guard"]
        assert "-le" in guard and str(JPEG_MAX_BYTES) in guard
        assert shlex.split(guard)                # 守卫同样可切词


def test_frame_leading_dash_filename_gets_dot_slash_guard():
    cmd = contact_window_command("-evil.mp4", "-out.jpg")
    assert " ./-evil.mp4" in cmd
    assert " ./-out.jpg" in cmd
    # 普通文件名不画蛇添足
    assert "./video.mp4" not in contact_window_command("video.mp4", "o.jpg")


def test_frame_window_centers_on_contact_and_rejects_bad_numbers():
    cmd = contact_window_command("v.mp4", "o.jpg", contact_moment_s=1.0)
    assert "-ss 0.700" in cmd and "-t 0.600" in cmd
    no_contact = contact_window_command("v.mp4", "o.jpg")
    assert "-ss 0.000" in no_contact
    with pytest.raises(ValueError):
        contact_window_command("v.mp4", "o.jpg", contact_moment_s=float("nan"))
    with pytest.raises(ValueError):
        contact_window_command("v.mp4", "o.jpg", contact_moment_s=-1.0)
    with pytest.raises(ValueError):
        contact_window_command("v.mp4", "o.jpg", quality=1)
    with pytest.raises(ValueError):
        contact_window_command("v.mp4", "o.jpg", fps=True)
    with pytest.raises(ValueError):
        size_guard_command("o.jpg", limit=0)


# --------------------------------------------------------------------------
# 三探针：正常数组全过
# --------------------------------------------------------------------------

T = 20


def _fluid_good():
    counts = np.array([50 + min(t, 10) * 5 for t in range(T)], float)
    boundary = np.zeros(T)
    cup = np.zeros(T)
    cup[10:] = np.linspace(40, 90, 10)
    return counts, boundary, cup


def _mesh_good():
    base = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1.0]])
    verts = np.stack([base + 0.01 * t for t in range(T)])
    return np.array([[0, 1, 2, 3]]), verts, np.linspace(1, 2, T)


def _tactile_good():
    n = 4
    normal = np.ones((T, n))
    shear = np.zeros((T, n, 2))
    shear[:, :, 0] = 0.1
    return normal, shear, np.full(n, 0.5)


def test_probes_good_arrays_pass():
    r = mp.fluid_conservation(*_fluid_good())
    assert r["pass"] and all(r["checks"].values()) and not r["violations"]
    r = mp.soft_mesh_integrity(*_mesh_good())
    assert r["pass"] and all(r["checks"].values())
    r = mp.tactile_friction_cone(*_tactile_good())
    assert r["pass"] and all(r["checks"].values())


# 每类缺陷 ≥3 组数组，全部必须拦截（pass=False）。
def test_fluid_defect_interception_table():
    counts, boundary, cup = _fluid_good()
    defects = []
    c1 = counts.copy(); c1[15] = 60.0                    # 凭空消失
    defects.append((c1, boundary, cup))
    b1 = boundary.copy(); b1[-1] = 10.0                  # 10% 溢出
    defects.append((counts, b1, cup))
    cup1 = cup.copy(); cup1[10:] = np.linspace(10, 40, 10)  # 入杯仅 40%
    defects.append((counts, boundary, cup1))
    c2 = counts.copy(); c2[5] = 200.0
    b2 = boundary.copy(); b2[5] = 205.0                  # 账目不平
    defects.append((c2, b2, cup))
    assert len(defects) >= 3
    for args in defects:
        r = mp.fluid_conservation(*args)
        assert r["pass"] is False and r["violations"]


def test_mesh_defect_interception_table():
    tet, verts, energy = _mesh_good()
    defects = []
    vi = verts.copy(); vi[5, :, 2] *= -1                 # 四面体反转
    defects.append((tet, vi, energy))
    vd = verts.copy(); vd[5] = verts[0].mean(axis=0)     # 近零体积退化
    defects.append((tet, vd, energy))
    es = energy.copy(); es[7] = 100.0                    # 能量突跳
    defects.append((tet, verts, es))
    en = energy.copy(); en[9] = -3.0                     # 负能量
    defects.append((tet, verts, en))
    assert len(defects) >= 3
    for args in defects:
        r = mp.soft_mesh_integrity(*args)
        assert r["pass"] is False and r["violations"]


def test_tactile_defect_interception_table():
    normal, shear, mu = _tactile_good()
    defects = []
    s1 = shear.copy(); s1[5, 0, 0] = 1.0                  # 锥外 |shear|>μN
    defects.append((normal, s1, mu))
    n1 = normal.copy(); n1[6, 1] = -1.0                  # 法向力负
    defects.append((n1, shear, mu))
    n2 = normal.copy(); n2[8, 2] = 50.0                  # 力值突跳
    defects.append((n2, shear, mu))
    s2 = shear.copy(); s2[10, 3, 1] = 3.0                # 静止接触侧越锥
    defects.append((normal, s2, mu))
    assert len(defects) >= 3
    for args in defects:
        r = mp.tactile_friction_cone(*args)
        assert r["pass"] is False and r["violations"]


# 坏输入：bool / NaN / Inf / 形状 / list 一律抛 ValueError（不是 pass=False）。
def test_probes_reject_bool_nan_inf_shape_and_lists():
    counts, boundary, cup = _fluid_good()
    tet, verts, energy = _mesh_good()
    normal, shear, mu = _tactile_good()
    cases = [
        (mp.fluid_conservation, (counts.astype(bool), boundary, cup)),
        (mp.fluid_conservation, (counts, boundary,
                                 cup.astype(bool))),
        (mp.fluid_conservation,
         (np.full(T, np.nan), boundary, cup)),
        (mp.fluid_conservation,
         (np.full(T, np.inf), boundary, cup)),
        (mp.fluid_conservation, (counts, boundary, cup[:5])),
        (mp.fluid_conservation,
         (counts.tolist(), boundary.tolist(), cup.tolist())),
        (mp.soft_mesh_integrity,
         (tet.astype(np.float64), verts, energy)),      # tet 必须整数
        (mp.soft_mesh_integrity,
         (np.array([[True, True, True, True]]), verts, energy)),
        (mp.soft_mesh_integrity, (tet, verts, energy[:5])),
        (mp.soft_mesh_integrity,
         (tet, verts.copy().astype(float),
          np.where(np.arange(T) == 3, np.nan, energy))),
        (mp.soft_mesh_integrity,
         (np.array([[0, 1, 2, 99]]), verts, energy)),   # 索引越界
        (mp.tactile_friction_cone, (normal, shear,
                                    np.where(np.arange(4) == 0, np.inf, mu))),
        (mp.tactile_friction_cone,
         (normal, shear[:, :, 0], mu)),                  # shear 缺一维
        (mp.tactile_friction_cone, (normal.astype(bool), shear, mu)),
        (mp.tactile_friction_cone, (normal, shear, np.zeros(4))),  # μ=0
        (mp.tactile_friction_cone,
         (normal.tolist(), shear.tolist(), mu.tolist())),
    ]
    assert len(cases) >= 12
    for fn, args in cases:
        with pytest.raises(ValueError):
            fn(*args)
