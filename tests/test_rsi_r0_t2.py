"""RSI-R0 T2 契约测试：九实例冻结表、场景/故障注入、候选面闸口、参考判据。

全部离线：不启容器、不联网、不触达账本。容器侧的真实验证在
``scripts/rsi_r0/t2_reference.py``（参考实跑）与其产物 ``protocol/t2_golden.json``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.rsi_r0 import trusted
from orchestrator.rsi_r0.contracts import INSTANCE_IDS, instance_family
from orchestrator.rsi_r0.faults import FaultError, apply_fault, validate_fault
from orchestrator.rsi_r0.image_pin import IMAGE_REF, REPO
from orchestrator.rsi_r0.instances import PLANS, assert_frozen_table, plan_for
from orchestrator.rsi_r0.scenes import (
    SceneParamError,
    build_pendulum_scene,
    build_push_scene,
    build_scene,
    validate_params,
)
from orchestrator.rsi_r0.trusted import (
    CANDIDATE_PARAM_BOUNDS,
    CandidateRequestError,
    ExperimentSession,
)

PUSH = {"box_mass": 1.0, "box_half": 0.05, "friction": 0.25, "timestep": 0.002}
PEND = {"pendulum_length": 0.8, "pendulum_mass": 0.4, "damping": 0.3, "timestep": 0.002}

PRIVATE_KEYS = {"fault", "expected", "expected_direction", "equivalence_bound",
                "levels", "tolerance", "reference_gains", "probe_forces", "nominal"}


# ---------------------------------------------------------------------------
# 冻结表
# ---------------------------------------------------------------------------
def test_frozen_table_is_self_consistent():
    assert_frozen_table()
    assert tuple(p.instance_id for p in PLANS) == INSTANCE_IDS
    assert len(PLANS) == 9


def test_validation_instances_precede_development_in_lookup():
    # 计划 §3.3：验证顺序固定为 E-v1,D-v1,R-v1,E-v2,D-v2,R-v2；dev 另行先跑。
    orders = {p.instance_id: p.order_index for p in PLANS}
    assert orders["E-dev"] < orders["E-v1"] < orders["D-v1"] < orders["R-v1"]
    assert orders["E-v1"] < orders["E-v2"] < orders["D-v2"] < orders["R-v2"]


def test_each_family_has_three_mechanistically_distinct_instances():
    e2 = plan_for("E-v2").public["scene_params"]
    assert "obstacle_x" in e2 and "obstacle_x" not in plan_for("E-v1").public["scene_params"]
    d_kinds = {plan_for(i).private["fault"]["kind"] for i in ("D-dev", "D-v1", "D-v2")}
    assert len(d_kinds) == 3, f"D 族三个实例必须是三种不同故障，实际 {d_kinds}"
    assert plan_for("R-v2").private["expected"] == "INCONCLUSIVE"


def test_public_face_never_carries_private_keys():
    for plan in PLANS:
        payload = json.dumps(plan.public, ensure_ascii=False)
        for key in PRIVATE_KEYS:
            assert f'"{key}"' not in payload, f"{plan.instance_id} 公开面含私有键 {key}"


def test_d_validation_instances_are_indistinguishable_from_their_task_books():
    """D-v1/D-v2 公开面必须逐字相同：真值只能由物理辨识得到，不能从任务书读出来。"""
    a, b = plan_for("D-v1"), plan_for("D-v2")
    assert json.dumps(a.public, sort_keys=True) == json.dumps(b.public, sort_keys=True)
    assert a.private["fault"]["kind"] != b.private["fault"]["kind"]
    # 候选假设集合必须三项齐列（否则等于直接告诉候选答案）
    text = json.dumps(a.public["goal"], ensure_ascii=False)
    for kind in ("scene_tilt", "friction_scale", "force_scale"):
        assert kind in text


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
def test_scene_builders_are_deterministic():
    assert build_push_scene(PUSH) == build_push_scene(dict(PUSH))
    assert build_pendulum_scene(PEND) == build_pendulum_scene(dict(PEND))


def test_pendulum_pivot_is_above_the_floor():
    """回归：支点放在 z=0 时整根杆埋在地板里，被接触钉住（2026-11-08 实测）。"""
    xml = build_pendulum_scene(PEND)
    assert 'pos="0 0 0.950000"' in xml          # z0 = L + 0.15 = 0.95
    assert 'energy="enable"' in xml            # data.energy 不开启恒为 0


def test_validate_params_rejects_unknown_key():
    with pytest.raises(SceneParamError):
        validate_params({"box_mass": 1.0, "magic": 1.0})


@pytest.mark.parametrize("bad", [
    {"box_mass": 0.0}, {"box_mass": 1e9}, {"friction": -1.0}, {"timestep": 1.0},
    {"box_mass": True}, {"box_mass": "1.0"},
])
def test_validate_params_rejects_out_of_bounds_and_non_numeric(bad):
    with pytest.raises(SceneParamError):
        validate_params({**PUSH, **bad})


def test_push_scene_requires_obstacle_pair():
    with pytest.raises(SceneParamError):
        build_push_scene({**PUSH, "obstacle_x": 0.3})
    with pytest.raises(SceneParamError):
        build_push_scene({**PUSH, "obstacle_r": 0.08})


def test_push_scene_tilt_rotates_gravity():
    xml = build_push_scene({**PUSH, "tilt_deg": 6.0})
    assert 'gravity="1.025' in xml and 'gravity="0.000000' not in xml


def test_build_scene_rejects_unknown_template():
    with pytest.raises(SceneParamError):
        build_scene("carla", PUSH)


# ---------------------------------------------------------------------------
# 故障注入
# ---------------------------------------------------------------------------
def test_friction_fault_scales_every_friction_attribute():
    out = apply_fault(build_push_scene(PUSH), {"kind": "friction_scale", "scale": 2.0})
    # 0.25 → 0.50（滑动）、0.005 保持（扭转）、0.0001 → 0.0001（滚动，_fmt 去尾零）
    assert 'friction="0.5' in out
    scaled = [line for line in out.splitlines() if 'friction="' in line]
    assert len(scaled) == 1, f"应只有一个 friction 属性，实际 {scaled}"
    parts = scaled[0].split('friction="')[1].split('"')[0].split()
    assert [float(p) for p in parts] == pytest.approx([0.5, 0.005, 0.0001])


def test_tilt_fault_rewrites_gravity_and_is_not_symmetric():
    out = apply_fault(build_push_scene(PUSH), {"kind": "scene_tilt", "scale": 6.0})
    # 9.81·sin(6°)=1.025424，9.81·cos(6°)=9.756260（_fmt 六位小数）
    assert 'gravity="1.025424 0 -9.756260"' in out


def test_force_fault_leaves_scene_untouched():
    xml = build_push_scene(PUSH)
    assert apply_fault(xml, {"kind": "force_scale", "scale": 0.4}) == xml


@pytest.mark.parametrize("fault", [
    {"kind": "mass_scale", "scale": 3.0},       # 已下线：与 force_scale 物理不可辨识
    {"kind": "friction_scale", "scale": 999.0},
    {"kind": "scene_tilt", "scale": 90.0},
    {"kind": "force_scale", "scale": "0.4"},
    {"kind": "force_scale"},
])
def test_fault_validation_is_fail_closed(fault):
    with pytest.raises(FaultError):
        validate_fault(fault)


# ---------------------------------------------------------------------------
# 候选面闸口（scene_xml 永不由候选提供）
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_service(monkeypatch):
    calls = []

    def _fake(request, *, image_digest, gpus=False, timeout_s=180):
        calls.append(json.loads(json.dumps(request)))
        return {"ok": True, "run_id": "run_test", "steps_run": int(request["steps"]),
                "events_sha256": "deadbeef"}
    monkeypatch.setattr(trusted, "call_service", _fake)
    return calls


def _session(kind="push_control", max_steps=3000, host_fields=None):
    return ExperimentSession(kind=kind, image_digest=IMAGE_REF,
                             scene_builder=lambda _p: "<mujoco/>",
                             max_steps=max_steps, host_fields=host_fields)


def test_candidate_cannot_submit_scene_xml(fake_service):
    sess = _session()
    with pytest.raises(CandidateRequestError):
        sess.run({"target_x": 0.5, "scene_xml": "<mujoco/>"})
    assert fake_service == []


def test_candidate_cannot_submit_host_fields(fake_service):
    sess = _session()
    for key in ("force_scale", "fault"):
        with pytest.raises(CandidateRequestError):
            sess.run({"target_x": 0.5, key: 1.0})
    assert fake_service == []


def test_host_fields_are_injected_by_the_host_not_the_candidate(fake_service):
    sess = _session(host_fields={"force_scale": 0.4})
    sess.run({"target_x": 0.5, "target_y": 0.0, "kp": 10.0, "kd": 1.0,
              "max_force": 5.0, "steps": 10})
    assert fake_service[0]["force_scale"] == 0.4
    assert "scene_xml" in fake_service[0]        # 宿主构造，不是候选给的


def test_session_rejects_out_of_bounds_and_non_numeric(fake_service):
    sess = _session()
    with pytest.raises(CandidateRequestError):
        sess.run({"kp": 99999.0})
    with pytest.raises(CandidateRequestError):
        sess.run({"kp": True})
    with pytest.raises(CandidateRequestError):
        sess.run({"steps": 2.5})


def test_session_caps_steps_at_the_instance_limit(fake_service):
    sess = _session(max_steps=500)
    with pytest.raises(CandidateRequestError):
        sess.run({"kp": 1.0, "steps": 501})
    assert sess.run({"kp": 1.0, "steps": 500})["steps_run"] == 500


def test_session_detects_steps_run_mismatch(monkeypatch):
    def _bad(request, **_kw):
        return {"ok": True, "run_id": "r", "steps_run": 1, "events_sha256": "x"}
    monkeypatch.setattr(trusted, "call_service", _bad)
    with pytest.raises(trusted.TrustedServiceError):
        _session().run({"kp": 1.0, "steps": 10})


def test_host_only_field_whitelist_is_narrow():
    with pytest.raises(CandidateRequestError):
        _session(host_fields={"scene_xml": "<mujoco/>"})


def test_candidate_bounds_cover_exactly_the_declared_interfaces():
    for plan in PLANS:
        allowed = set(CANDIDATE_PARAM_BOUNDS[plan.public["interface"]["kind"]])
        declared = set(plan.public["interface"]["keys"]) | {"steps", "damping"}
        assert set(plan.public["interface"]["keys"]) <= allowed, plan.instance_id
        assert declared <= allowed | {"damping"}, plan.instance_id


# ---------------------------------------------------------------------------
# 镜像 pin
# ---------------------------------------------------------------------------
def test_image_ref_is_digest_pinned_not_tag_float():
    assert IMAGE_REF == f"{REPO}@sha256:" + IMAGE_REF.split("sha256:")[1]
    assert "@sha256:" in IMAGE_REF and len(IMAGE_REF.split("sha256:")[1]) == 64


def test_no_tool_passes_a_tag_reference():
    import inspect
    src = inspect.getsource(trusted)
    assert ':phase0"' not in src, "禁止按 tag 引用镜像（tag 可被重新指向）"


def test_host_fallback_is_rejected():
    with pytest.raises(trusted.HostFallbackRejected):
        trusted.assert_no_host_fallback("reference_execute")


# ---------------------------------------------------------------------------
# 参考判据（纯函数）
# ---------------------------------------------------------------------------
def test_settling_time_censors_when_never_settles():
    from orchestrator.rsi_r0.reference_analysis import settling_time
    moving = {"angle_series": [[i, 0.5, 0.5] for i in range(100)]}
    assert settling_time(moving, 0.002)["censored"] is True
    settled = {"angle_series": [[i, 0.0, 0.0] for i in range(100)]}
    assert settling_time(settled, 0.002) == {"settle_s": 0.0, "censored": False, "steps": 100}
    half = {"angle_series": [[i, 0.5, 0.5] for i in range(10)]
            + [[i, 0.0, 0.0] for i in range(10, 100)]}
    assert settling_time(half, 0.002)["settle_s"] == pytest.approx(0.02)


def test_fault_classification_separates_the_three_kinds():
    from orchestrator.rsi_r0.reference_analysis import classify_fault
    nominal = {"mass": 1.0, "friction": 0.25, "g": 9.81}
    rules = {"asymmetry_min": 0.5, "deviation_min": 0.15}
    # 对称、斜率不变、截距翻倍 ⇒ 摩擦
    got = classify_fault(1.0, 4.905, 1.0, 4.905, nominal, rules)
    assert got["kind"] == "friction_scale" and got["scale"] == pytest.approx(2.0)
    # 对称、斜率变 0.4 倍 ⇒ 执行增益
    got = classify_fault(0.4, 2.4525, 0.4, 2.4525, nominal, rules)
    assert got["kind"] == "force_scale" and got["scale"] == pytest.approx(0.4)
    # 非对称 ⇒ 倾斜（6° 的 |B- - B+| = 2 g sin6°）
    import math
    b_plus = 2.4525 * math.cos(math.radians(6)) - 9.81 * math.sin(math.radians(6))
    b_minus = 2.4525 * math.cos(math.radians(6)) + 9.81 * math.sin(math.radians(6))
    got = classify_fault(1.0, b_plus, 1.0, b_minus, nominal, rules)
    assert got["kind"] == "scene_tilt" and got["scale"] == pytest.approx(6.0, abs=0.05)
    # 全对称且都在界内 ⇒ 无故障
    assert classify_fault(1.0, 2.4525, 1.0, 2.4525, nominal, rules)["kind"] == "none"


def test_research_verdicts_follow_the_preregistered_rule():
    from orchestrator.rsi_r0.reference_analysis import research_verdict
    priv = {"expected": "SUPPORTED", "expected_direction": "damping_up_settling_down",
            "equivalence_bound": 0.05, "support_threshold": 0.15}
    assert research_verdict({"1.0": 0.48, "-1.0": 0.47}, priv) == "SUPPORTED"
    # 方向不一致 ⇒ 不能算支持
    assert research_verdict({"1.0": 0.48, "-1.0": -0.47}, priv) == "INCONCLUSIVE"
    # 低于等效界 ⇒ 诚实判等效（不是"支持"）
    assert research_verdict({"1.0": 0.004, "-1.0": 0.005}, priv) == "INCONCLUSIVE_EQUIVALENT"
    # 介于等效界与支持阈之间 ⇒ 不确定
    assert research_verdict({"1.0": 0.10, "-1.0": 0.09}, priv) == "INCONCLUSIVE"
    # 有删失 ⇒ 不下结论
    assert research_verdict({"1.0": None, "-1.0": 0.4}, priv) == "CENSORED"


# ---------------------------------------------------------------------------
# 任务合同（交卷格式）
# ---------------------------------------------------------------------------
def _public_task(instance_id):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "t2_build", root / "scripts" / "rsi_r0" / "t2_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.public_task(plan_for(instance_id))


def test_every_task_declares_the_answer_contract():
    for plan in PLANS:
        task = _public_task(plan.instance_id)
        answer = task["answer"]
        assert answer["required"], plan.instance_id
        assert "run_id" in " ".join(answer["required"]), plan.instance_id


def test_d_validation_tasks_differ_only_in_identity_fields():
    """D-v1/D-v2 的任务书除身份字段外必须逐字相同——否则答案能从题面读出来。"""
    a, b = _public_task("D-v1"), _public_task("D-v2")
    identity = {"instance_id", "order_index", "task_id"}
    strip = lambda d: {k: v for k, v in d.items() if k not in identity}  # noqa: E731
    assert json.dumps(strip(a), sort_keys=True, ensure_ascii=False) == \
        json.dumps(strip(b), sort_keys=True, ensure_ascii=False)


def test_call_record_carries_scene_identity_for_replay(fake_service):
    """评测器要用 scene_sha256 证明"跑的是本题场景"，用 steps_run 证明真步进。"""
    sess = _session()
    sess.run({"kp": 1.0, "steps": 10})
    rec = sess.calls[0]
    assert rec["steps_run"] == 10
    assert len(rec["scene_sha256"]) == 64


# ---------------------------------------------------------------------------
# 产物口径：stage_models.yaml 只允许**一套**哈希约定（评审 MINOR-3）
# ---------------------------------------------------------------------------

def test_admission_records_stage_models_hash_in_the_engine_convention():
    """admission 记录的口径必须与 model_approval/t4_lock 相同，且键名写明口径。

    旧版这里记原始字节 sha256（键名 ``sha256``），而引擎侧记
    ``sha256_of(text)``——同一份文件两个值、两个都叫"sha256"，对照必误判漂移。
    """
    import importlib.util
    from pathlib import Path

    from orchestrator.rsi_r0.contracts import sha256_of

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "rsi_r0_admission_probe", repo / "scripts" / "rsi_r0" / "admission.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    doc = mod.collect_stage_models()
    text = (repo / "orchestrator" / "gateway" / "stage_models.yaml").read_text(
        encoding="utf-8")
    hash_keys = sorted(k for k in doc if "sha256" in k)
    assert hash_keys == ["sha256_of_text"], hash_keys
    assert doc["sha256_of_text"] == sha256_of(text)


def test_admission_refuses_to_overwrite_a_frozen_campaign(tmp_path, monkeypatch):
    """campaign id 不可复用：T0 的手写 gates.json 不许盖掉已开跑 campaign 的机算件。"""
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "rsi_r0_admission_probe2", repo / "scripts" / "rsi_r0" / "admission.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.delenv("SIMPRAXIS_DATA_ROOT", raising=False)
    campaign = tmp_path / mod.R0_FAMILY_DIRNAME / "frozen"
    (campaign / "protocol").mkdir(parents=True)
    (campaign / "protocol" / "gates.json").write_text("{}", encoding="utf-8")
    with pytest.raises(mod.AdmissionError, match="不可复用"):
        mod.run_admission("frozen", 0, data_root=str(tmp_path))
