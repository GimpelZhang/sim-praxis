#!/usr/bin/env python3
"""Task7 单测：Track1 spec_factory + evaluate 接缝 + CLI 装配（0 付费，全 fake）。

覆盖（计划 L502/L525-529，硬规则 1/2/7）：
- spec_factory：mujoco/sapien → 场景模板/引擎 env/命令/billing_task=7 映射；
  首见 staging（脚本 0700、versions.txt 0600）、run_idx 递增、gate2_notes
  注入；未知引擎与缺模板一律 GraphStateError（不静默跑错脚本）；
- evaluate 接缝：真实 telemetry.json + 真实 mp4 容器产物走 Rung1–3 文件判定
  与 Rung4 抽帧拼图；record_only 下恰好 1 次 call_visual 且脱敏 req/resp
  证据落盘 0600、raw_text_ref 补戳；uncalibrated 零网络零证据；
  telemetry/视频缺失均硬 GraphStateError（不得 Rung4=None 放行）；
- 硬规则1：接缝侧视觉只走 call_visual（stage 恒 visual_rollout），文本入口
  call() 一旦被触即测试失败；prompt 不含图像，图像只经 image_b64；
- CLI 装配：task=7 缺 --coding-batch-id/--visual-batch-id 即 SystemExit；
  齐备时注入可调用 agent_run/evaluate；非 Task7 两接缝为 None（Task3 语义）。

视频夹具是 Task5 标定仪器 write_clip_bundle（合成 2D 片，非仿真产物）：
本测只验接缝管道，不冒充真实 rollout 画面。
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.graph import task7
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge.clips import write_clip_bundle
from orchestrator.judge.rung4 import expected_answers, load_rubric

from tests.milestone1.test_judge import make_telemetry  # noqa: E402

RUBRIC = load_rubric()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def roots(tmp_path, monkeypatch):
    phase1 = tmp_path / "phase1"
    runs = tmp_path / "runs"
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(phase1))
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(runs))
    return {"phase1": phase1, "runs": runs, "tmp": tmp_path}


@pytest.fixture
def rollout_bundle(roots):
    """telemetry.json（公式好片）+ good.mp4（Task5 合成仪器）落盘。"""
    d = roots["tmp"] / "run_t7-r1"
    d.mkdir(parents=True)
    data = make_telemetry()
    tele = d / "telemetry.json"
    tele.write_text(json.dumps(data), encoding="utf-8")
    bundle = write_clip_bundle("good", d)
    return {"dir": d, "telemetry": tele, "video": Path(bundle["mp4"]),
            "data": data}


class FakeVisualGateway:
    """接缝唯一允许触网的是 call_visual；call() 被触即失败（硬规则1）。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.visual_calls: list[tuple] = []
        self.text_calls: list[tuple] = []

    def model_id(self, stage: str) -> str:
        assert stage == "visual_rollout", f"视觉 stage 被换成 {stage!r}"
        return "fixture-visual-model-000"

    def call_visual(self, prompt, image_b64, **kwargs):
        self.visual_calls.append((prompt, image_b64, kwargs))
        return self.raw, (11, 22)

    def call(self, stage, messages, **kwargs):  # pragma: no cover - 断言即失败
        self.text_calls.append((stage, messages))
        raise AssertionError("Task7 evaluate 不得走任何文本 stage（macro/coding）")


def _good_answers_raw() -> str:
    good = expected_answers(RUBRIC, good_clip=True)
    return json.dumps(
        {"answers": [{"id": qid, "answer": ans}
                     for qid, ans in good.items()],
         "low_confidence": False})


def _set_calibration(roots, mode: str) -> Path:
    cal_dir = roots["phase1"] / "task5_judge"
    cal_dir.mkdir(parents=True, exist_ok=True)
    cal = cal_dir / "calibration_6q.json"
    cal.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    return cal


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# spec_factory
# ---------------------------------------------------------------------------

def test_01_spec_factory_mujoco_mapping(roots):
    factory = task7.make_spec_factory("tt", "T7-B1-coding", task_title="t")
    spec = factory({"chosen": {"sim_engine": "mujoco"},
                    "agent_runs": [], "events": []})

    assert spec.billing_task == 7
    assert spec.batch_id == "T7-B1-coding"
    assert spec.telemetry_relpath == "telemetry.json"
    assert spec.video_relpath == "video.mp4"
    assert spec.cover_relpath == "cover.png"
    assert spec.manifest_relpath == "manifest.json"
    assert spec.gate2_notes is None

    target = Path(spec.cmd[1])
    assert spec.cmd[0] == task7.VENV_PY
    assert target.name == "mujoco_contact_push.py"
    assert spec.cmd[2:] == ["--out-dir", str(spec.workdir),
                            "--run-id", "tt-r1"]
    assert spec.workdir == roots["runs"] / "phase1" / "task7_track1" / "run_tt_1"
    # staging：首见复制真实模板并收紧权限
    assert target.is_file()
    assert _mode(target) == 0o700
    template = task7.ENGINE_SCENARIOS["mujoco"]["template"]
    assert target.read_bytes() == template.read_bytes()
    versions = spec.workdir / "versions.txt"
    assert versions.is_file() and _mode(versions) == 0o600
    assert versions.read_text(encoding="utf-8").strip()
    # 引擎 env + 持久缓存/直连约定
    assert spec.env_extra["MUJOCO_GL"] == "egl"
    assert spec.env_extra["PYOPENGL_PLATFORM"] == "egl"
    assert spec.env_extra["HOME"] == "/mnt/caches/home"
    assert spec.env_extra["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert spec.env_extra["SIMPRAXIS_USE_PROXY"] == "0"


def test_02_spec_factory_sapien_and_run_idx(roots):
    factory = task7.make_spec_factory("tt", "T7-B1-coding")
    s1 = factory({"chosen": {"sim_engine": "sapien"},
                  "agent_runs": [], "events": []})
    assert Path(s1.cmd[1]).name == "maniskill_pushcube.py"
    assert s1.cmd[-1] == "tt-r1"
    assert "MUJOCO_GL" not in s1.env_extra
    assert s1.env_extra["PYOPENGL_PLATFORM"] == "egl"
    assert "sapien" in s1.task_title

    # Gate2 打回后再跑：全新 run 目录 + 新 run_idx + 人工意见注入
    s2 = factory({"chosen": {"sim_engine": "sapien"},
                  "agent_runs": [{"run_idx": 1}],
                  "events": [{"type": "gate2_rejected",
                              "human_notes": "接触段仍抖，降速"}]})
    assert s2.workdir.name == "run_tt_2"
    assert s2.cmd[-1] == "tt-r2"
    assert s2.gate2_notes == "接触段仍抖，降速"
    assert Path(s2.cmd[1]).is_file()


def test_03_spec_factory_unknown_engine_and_missing_template(roots, monkeypatch, tmp_path):
    factory = task7.make_spec_factory("tt", "T7-B1-coding")
    with pytest.raises(GraphStateError, match="场景映射缺"):
        factory({"chosen": {"sim_engine": "genesis"},
                 "agent_runs": [], "events": []})

    monkeypatch.setitem(
        task7.ENGINE_SCENARIOS, "mujoco",
        {"template": tmp_path / "no_such_template.py",
         "script": "mujoco_contact_push.py", "env": {}})
    with pytest.raises(GraphStateError, match="场景模板不存在"):
        factory({"chosen": {"sim_engine": "mujoco"},
                 "agent_runs": [], "events": []})


# ---------------------------------------------------------------------------
# evaluate 接缝
# ---------------------------------------------------------------------------

def test_10_evaluate_uncalibrated_zero_network(roots, rollout_bundle):
    gw = FakeVisualGateway(_good_answers_raw())

    def _boom(*a, **k):  # pragma: no cover - uncalibrated 绝不发网
        raise AssertionError("uncalibrated 不得发视觉实呼")

    gw.call_visual = _boom
    seam = task7.make_evaluate_seam(gw, "T7-B1-visual", "t7")
    metrics = seam({
        "telemetry_uri": str(rollout_bundle["telemetry"]),
        "agent_runs": [{"video_path": str(rollout_bundle["video"])}],
    })

    r4 = metrics["rungs"]["rung4"]
    assert r4["status"] == "uncalibrated" and r4["mode"] == "uncalibrated"
    assert r4["raw_text_ref"] is None
    assert metrics["vector"] == {"rung1": True, "rung2": True, "rung3": True}
    assert metrics["auto_release"] is True          # 放行只看 Rung1–3
    assert metrics["provenance"] == "file:telemetry_uri+video:rung4_collage"
    # 零证据文件
    evd = roots["phase1"] / "task7_track1" / "evidence"
    assert not evd.exists() or not list(evd.iterdir())
    collage = rollout_bundle["dir"] / "rung4_collage.png"
    assert collage.is_file() and _mode(collage) == 0o600


def test_11_evaluate_record_only_writes_evidence(roots, rollout_bundle):
    _set_calibration(roots, "record_only")
    gw = FakeVisualGateway(_good_answers_raw())
    seam = task7.make_evaluate_seam(gw, "T7-B1-visual", "t7")
    metrics = seam({
        "telemetry_uri": str(rollout_bundle["telemetry"]),
        "agent_runs": [{"video_path": str(rollout_bundle["video"])}],
    })

    assert len(gw.visual_calls) == 1
    assert gw.text_calls == []
    prompt, image_b64, kwargs = gw.visual_calls[0]
    assert isinstance(prompt, str) and "data:image" not in prompt
    assert isinstance(image_b64, str) and len(image_b64) > 1000
    assert kwargs["max_tokens"] == 512

    r4 = metrics["rungs"]["rung4"]
    assert r4["status"] == "ok" and r4["mode"] == "record_only"
    assert r4["n_defects"] == 0
    # record_only：items 照显但分歧标志强制清空（L527），不产生自动效力
    assert len(r4["items"]) == len(RUBRIC["questions"])
    assert r4["disagreement"] is None
    assert "banner" not in metrics
    assert metrics["auto_release"] is True

    evd = roots["phase1"] / "task7_track1" / "evidence"
    req_p = evd / "rung4_t7-r1.req.json"
    resp_p = evd / "rung4_t7-r1.resp.json"
    assert req_p.is_file() and resp_p.is_file()
    assert _mode(req_p) == 0o600 and _mode(resp_p) == 0o600
    req = json.loads(req_p.read_text(encoding="utf-8"))
    resp = json.loads(resp_p.read_text(encoding="utf-8"))
    assert req["stage"] == "visual_rollout"
    assert req["model"] == "fixture-visual-model-000"
    assert req["batch_id"] == "T7-B1-visual"
    assert req["kind"] == "task7_real_rollout_rung4"
    assert req["image_b64_len"] == len(image_b64)
    assert req["collage_png"].endswith("rung4_collage.png")
    assert resp["ok"] is True
    assert resp["tokens_in"] == 11 and resp["tokens_out"] == 22
    assert _good_answers_raw() in resp["text"] or "yes" in resp["text"]
    assert r4["raw_text_ref"] == "phase1/task7_track1/evidence/rung4_t7-r1.req.json"


def test_12_evaluate_missing_telemetry_hard_fails(roots, rollout_bundle):
    seam = task7.make_evaluate_seam(FakeVisualGateway(_good_answers_raw()),
                                    "T7-B1-visual", "t7")
    with pytest.raises(GraphStateError, match="telemetry_uri"):
        seam({"agent_runs": [{"video_path": str(rollout_bundle["video"])}]})
    with pytest.raises(GraphStateError, match="telemetry 不存在"):
        seam({"telemetry_uri": str(roots["tmp"] / "nope.json"),
              "agent_runs": [{"video_path": str(rollout_bundle["video"])}]})


def test_13_evaluate_missing_video_hard_fails(roots, rollout_bundle):
    seam = task7.make_evaluate_seam(FakeVisualGateway(_good_answers_raw()),
                                    "T7-B1-visual", "t7")
    base_state = {"telemetry_uri": str(rollout_bundle["telemetry"])}
    with pytest.raises(GraphStateError, match="真实视频缺失"):
        seam(dict(base_state, agent_runs=[]))
    with pytest.raises(GraphStateError, match="真实视频缺失"):
        seam(dict(base_state,
                  agent_runs=[{"video_path": str(roots["tmp"] / "nope.mp4")}]))


# ---------------------------------------------------------------------------
# CLI 装配（_production_deps）
# ---------------------------------------------------------------------------

class _Dummy:
    pass


def _cli_args(**over):
    args = argparse.Namespace(
        track="track2", index=None, question="实证问题？", task_title="",
        thread="tt", batch_id="T7-B0-macro", task=7,
        coding_batch_id="T7-B1-coding", visual_batch_id="T7-B1-visual")
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


def test_20_cli_task7_requires_both_batch_ids(roots, patched_cli):
    with pytest.raises(SystemExit, match="coding-batch-id"):
        patched_cli._production_deps(
            _cli_args(coding_batch_id="", visual_batch_id="T7-B1-visual"))
    with pytest.raises(SystemExit, match="coding-batch-id"):
        patched_cli._production_deps(
            _cli_args(coding_batch_id="T7-B1-coding", visual_batch_id=""))


def test_21_cli_task7_injects_seams(roots, patched_cli):
    deps, payload, feishu = patched_cli._production_deps(_cli_args())
    assert payload == {"question": "实证问题？"}
    assert callable(deps.agent_run) and callable(deps.evaluate)
    assert feishu is not None


def test_22_cli_non_task7_keeps_none_seams(roots, patched_cli):
    deps, _payload, _feishu = patched_cli._production_deps(
        _cli_args(task=2))
    assert deps.agent_run is None
    assert deps.evaluate is None


class _RecordingGraph:
    """langgraph 0.2.28 回归哨：recursion_limit 只准在 config，不准作 kwarg。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def invoke(self, payload, config=None, **kwargs):
        self.calls.append((payload, config, kwargs))
        return {"events": [], "chosen": {}}


class _Closeable:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_23_invoke_recursion_limit_only_in_config(monkeypatch, patched_cli):
    import contextlib

    for resume in (False, True):
        rec = _RecordingGraph()
        feishu = _Closeable()
        monkeypatch.setattr(
            patched_cli, "_production_deps",
            lambda args: (object(), {}, feishu))
        monkeypatch.setattr(
            patched_cli, "_compiled",
            lambda deps, db_path=None: contextlib.nullcontext(rec))

        rc = patched_cli._invoke(_cli_args(task=2, index=None,
                                           question="q", batch_id="B"),
                                 resume)
        assert rc == 0
        payload, config, kwargs = rec.calls[-1]
        assert config["recursion_limit"] == patched_cli.RECURSION_LIMIT
        assert config["configurable"]["thread_id"] == "tt"
        assert "recursion_limit" not in kwargs  # TypeError on langgraph 0.2.28
        assert payload is None if resume else True
        assert feishu.closed


# ---------------------------------------------------------------------------
# Gate2 卡片自解释说明（真人 Gate2 2026-10-15 逐字要求）
# ---------------------------------------------------------------------------

def test_24_build_gate2_explanation_mujoco_grounded(roots):
    run_dir = roots["tmp"] / "run_tt_1"
    run_dir.mkdir(parents=True)
    tele = run_dir / "telemetry.json"
    tele.write_text("{}", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps({"n_frames": 187}), encoding="utf-8")
    state = {"chosen": {"name": "MJX/MuJoCo-Warp GPU 吞吐探针",
                        "sim_engine": "mujoco"},
             "metrics": {"engine": "mujoco"}}
    rec = {"telemetry_uri": str(tele), "duration_ms": 3000}

    expl = task7.build_gate2_explanation(state, rec)
    assert set(expl) == {"task", "problem", "video"}
    assert "MJX/MuJoCo-Warp GPU 吞吐探针" in expl["task"]
    assert "MuJoCo" in expl["task"] and "min-jerk" in expl["task"]
    # 阈值取自常量：5mm 穿模 / 1s 接触 / CV 0.3，文案不漂移
    assert "5 mm" in expl["problem"]
    assert "1 s" in expl["problem"] and "CV ≤ 0.3" in expl["problem"]
    assert "Rung4" in expl["problem"] and "永不单独放行" in expl["problem"]
    assert "本段约 187 帧、3.0 s" in expl["video"]


def test_25_build_gate2_explanation_sapien_and_unknown_engine(roots):
    state = {"chosen": {"name": "SAPIEN 探针", "sim_engine": "sapien"},
             "metrics": {"engine": "sapien"}}
    expl = task7.build_gate2_explanation(state, {})
    assert "ManiSkill3" in expl["task"] and "PushCube" in expl["task"]
    assert "Vulkan" in expl["video"]
    # 无 manifest/时长不报错，只是不挂长度
    assert "本段约" not in expl["video"]

    # 协议外引擎不硬编故事：卡片退回纯指标
    assert task7.build_gate2_explanation(
        {"chosen": {"sim_engine": "genesis"}}, {}) is None


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


def test_26_cli_gate2_seam_passes_explanation_only_task7(
        roots, patched_cli, monkeypatch):
    from orchestrator.feishu_gateway import client as feishu_client

    run_dir = roots["tmp"] / "run_tt_1"
    run_dir.mkdir(parents=True)
    tele = run_dir / "telemetry.json"
    tele.write_text("{}", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps({"n_frames": 187}), encoding="utf-8")
    fake = _CapturingFeishu()
    monkeypatch.setattr(feishu_client, "gateway_from_env", lambda **kw: fake)

    # Task7：explanation 被组装并透传
    deps, _, _ = patched_cli._production_deps(_cli_args())
    state = {"chosen": {"name": "MJX 吞吐探针", "sim_engine": "mujoco"},
             "metrics": {"engine": "mujoco"},
             "agent_runs": [{"telemetry_uri": str(tele),
                             "duration_ms": 3000, "video_path": None,
                             "intranet_url": None, "cover_png": None}]}
    assert deps.gate2(state)["decision"] == "approve"
    _, kwargs = fake.gate2[-1]
    expl = kwargs["explanation"]
    assert set(expl) == {"task", "problem", "video"}
    assert "MJX 吞吐探针" in expl["task"] and "本段约 187 帧、3.0 s" in expl["video"]

    # 非 Task7：不组装，explanation=None（Task3 语义，纯指标卡）
    fake2 = _CapturingFeishu()
    monkeypatch.setattr(feishu_client, "gateway_from_env", lambda **kw: fake2)
    deps2, _, _ = patched_cli._production_deps(_cli_args(task=2))
    deps2.gate2({"metrics": {}, "agent_runs": []})
    assert fake2.gate2[-1][1]["explanation"] is None
