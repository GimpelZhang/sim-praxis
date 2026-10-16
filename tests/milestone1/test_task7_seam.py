#!/usr/bin/env python3
"""Task7 单测：Track1 spec_factory + evaluate 接缝 + CLI 装配（0 付费，全 fake）。

覆盖（计划 L502/L525-529，硬规则 1/2/7）：
- spec_factory：mujoco/sapien → 方案 id/引擎 env/命令/billing_task=7 映射；
  resolver stage（入口脚本 0700、versions.txt 0600）、run_idx 递增、
  gate2_notes 注入；未知引擎与内容层缺方案/sha 不符一律 GraphStateError
  （不静默跑错脚本；Task0b 起方案在 tests 合成 fixture 内容层）；
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
import contextlib
import json
import stat
from pathlib import Path

import pytest

from orchestrator.graph import task7
from orchestrator.graph.nodes import GraphStateError
from orchestrator.judge.clips import write_clip_bundle
from orchestrator.judge.rung4 import expected_answers, load_rubric
from orchestrator.solutions import resolve_solution

from tests.milestone1.test_judge import make_telemetry  # noqa: E402

RUBRIC = load_rubric()

# Task0b：单测不依赖活 KB 仓，ENGINE_SCENARIOS 挂的方案 id 全部解析到
# tests/fixtures/solutions 下的合成最小方案（manifest 走 resolver 全量校验）。
FIXTURE_SOLUTIONS_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "solutions")


@pytest.fixture(autouse=True)
def _fixture_solutions_root(monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_SOLUTIONS_ROOT",
                       str(FIXTURE_SOLUTIONS_ROOT))


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
    # staging：resolver 校验 manifest/sha256 后落件并收紧权限
    assert target.is_file()
    assert _mode(target) == 0o700
    manifest = resolve_solution(
        task7.ENGINE_SCENARIOS["mujoco"]["solution_id"])
    source = manifest.solution_dir / "mujoco_contact_push.py"
    assert target.read_bytes() == source.read_bytes()
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


def test_03_spec_factory_unknown_engine_and_missing_solution(roots, monkeypatch, tmp_path):
    factory = task7.make_spec_factory("tt", "T7-B1-coding")
    with pytest.raises(GraphStateError, match="场景映射缺"):
        factory({"chosen": {"sim_engine": "genesis"},
                 "agent_runs": [], "events": []})

    # Task0b：方案 id 合 C4 语法但内容层缺方案目录 → resolver fail-closed，
    # 不网络兜底、不就地生成。
    monkeypatch.setitem(
        task7.ENGINE_SCENARIOS, "mujoco",
        {"solution_id": "track1_tools/99_missing_fixture", "env": {}})
    with pytest.raises(GraphStateError, match="方案 stage 失败"):
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


# ---------------------------------------------------------------------------
# L-1/L-7：Task7 Rung4 调用级重放与失败证据
# ---------------------------------------------------------------------------

def test_30_replay_second_seam_run_zero_paid_and_evidence_untouched(
        roots, rollout_bundle, monkeypatch):
    """首调成功后整节点重放：0 二次付费；req/resp 证据不被覆盖。

    L-1/C2：命中时不仅底层 gateway 0 调用，也不得进入 billing_context
    （preflight/ledger 行只可能在该上下文内产生），故计数上下文进入次数。
    """
    _set_calibration(roots, "record_only")
    gw = FakeVisualGateway(_good_answers_raw())
    billing_entries: list[dict] = []
    real_billing = task7.billing_context

    @contextlib.contextmanager
    def _spy_billing(**kw):
        billing_entries.append(kw)
        with real_billing(**kw) as ctx:
            yield ctx

    monkeypatch.setattr(task7, "billing_context", _spy_billing)
    seam = task7.make_evaluate_seam(gw, "T7-B1-visual", "t7")
    state = {"telemetry_uri": str(rollout_bundle["telemetry"]),
             "agent_runs": [{"video_path": str(rollout_bundle["video"])}]}
    seam(state)
    assert len(gw.visual_calls) == 1
    assert len(billing_entries) == 1          # miss 实呼恰进一次计费上下文

    evd = roots["phase1"] / "task7_track1" / "evidence"
    files = sorted(evd.glob("rung4_t7-r1.*.json"))
    assert len(files) == 2
    before = {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in files}
    cache_dir = roots["phase1"] / ".call_cache"
    assert cache_dir.is_dir() and _mode(cache_dir) == 0o700
    cached = list(cache_dir.iterdir())
    assert len(cached) == 1 and _mode(cached[0]) == 0o600

    metrics2 = seam(state)                     # kill-9 后整节点重放
    assert len(gw.visual_calls) == 1           # 零新增实呼
    assert len(billing_entries) == 1           # 命中：不进 billing_context
    for name, (content, mtime) in before.items():
        f = evd / name
        assert f.read_bytes() == content
        assert f.stat().st_mtime_ns == mtime
    assert metrics2["rungs"]["rung4"]["raw_text_ref"] == \
        "phase1/task7_track1/evidence/rung4_t7-r1.req.json"


class _ExplodingVisual:
    def model_id(self, stage):
        return "fixture-visual-model-000"

    def call_visual(self, *a, **k):
        raise RuntimeError("simulated visual outage")


def test_31_l7_visual_exception_writes_fail_resp_and_no_cache(roots):
    """实呼异常：except 内补脱敏 ok:false resp 后重抛；成功才写缓存。"""
    from orchestrator.common.callcache import CallReplay
    cache_dir = roots["phase1"] / ".call_cache"
    cache = CallReplay(cache_dir)
    evg = task7._VisualEvidenceGateway(
        _ExplodingVisual(), thread="t7", run_id="t7-r1",
        batch_id="T7-B1-visual", collage_path="x.png",
        cache=cache, cache_key="task7_rung4_t7-r1")
    with pytest.raises(RuntimeError, match="visual outage"):
        evg.call_visual("prompt", "x" * 2000, max_tokens=512)
    evd = roots["phase1"] / "task7_track1" / "evidence"
    resp = json.loads((evd / "rung4_t7-r1.resp.json").read_text())
    assert resp["ok"] is False and "visual outage" in resp["error"]
    assert list(cache_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# L-2：节点外协议异常归一 human_failure（不吞 GatewayPausedError）
# ---------------------------------------------------------------------------

class _StateSnap:
    def __init__(self, values, next_nodes=()):
        self.values = values
        self.next = tuple(next_nodes)


class _RaisingGraph:
    def __init__(self, exc, *, next_nodes=("evaluate",)):
        self.exc = exc
        self.next_nodes = next_nodes

    def invoke(self, *a, **k):
        raise self.exc

    def get_state(self, config):
        return _StateSnap({"thread_id": "tt", "track": "track1",
                           "events": []}, self.next_nodes)


class _FailFeishu:
    """记录 gate2 调用：异常归一结案绝不允许再发 Gate2 卡。"""
    def __init__(self):
        self.gate2_calls = 0
        self.closed = False

    def request_gate2_approval(self, *a, **k):  # pragma: no cover
        self.gate2_calls += 1
        return {"decision": "approve"}

    def close(self):
        self.closed = True


def test_32_cli_graph_state_error_closes_human_failure_exit3(
        monkeypatch, patched_cli, roots):
    from orchestrator.graph.nodes import GraphDeps

    run_dir = roots["tmp"] / "graph_runs" / "tt"
    deps = GraphDeps(run_dir=run_dir, persist=None)
    feishu = _FailFeishu()
    monkeypatch.setattr(
        patched_cli, "_production_deps",
        lambda args: (deps, {}, feishu))
    import contextlib
    rec = _RaisingGraph(GraphStateError("task8 evaluate：结构校验失败 x"))
    monkeypatch.setattr(
        patched_cli, "_compiled",
        lambda d, db_path=None: contextlib.nullcontext(rec))

    rc = patched_cli._invoke(
        _cli_args(task=2, index=None, question="q", batch_id="B"), False)
    assert rc == 3
    assert feishu.closed and feishu.gate2_calls == 0
    failure = json.loads((run_dir / "FAILURE.json").read_text())
    assert failure["status"] == "human_failure"
    assert failure["error_type"] == "GraphStateError"
    assert failure["stage"] == "agent_run"
    assert "结构校验失败" in failure["reason"]
    assert failure["thread_id"] == "tt"


@pytest.mark.parametrize("exc", [
    KeyError("metrics"),
    json.JSONDecodeError("Expecting value", "doc", 0),
])
def test_33_cli_key_and_json_errors_also_close(
        monkeypatch, patched_cli, roots, exc):
    from orchestrator.graph.nodes import GraphDeps
    run_dir = roots["tmp"] / "graph_runs" / "tt"
    deps = GraphDeps(run_dir=run_dir, persist=None)
    feishu = _FailFeishu()
    monkeypatch.setattr(patched_cli, "_production_deps",
                        lambda args: (deps, {}, feishu))
    import contextlib
    rec = _RaisingGraph(exc)
    monkeypatch.setattr(
        patched_cli, "_compiled",
        lambda d, db_path=None: contextlib.nullcontext(rec))
    rc = patched_cli._invoke(
        _cli_args(task=2, index=None, question="q", batch_id="B"), False)
    assert rc == 3 and feishu.gate2_calls == 0
    failure = json.loads((run_dir / "FAILURE.json").read_text())
    assert failure["error_type"] == type(exc).__name__


def test_34_cli_gateway_paused_propagates_without_closure(
        monkeypatch, patched_cli, roots):
    """预算熔断/编码限额不得被包装成 human_failure：原样上抛、无 FAILURE.json。"""
    from orchestrator.agent_runner.runner import CodingLimitError
    from orchestrator.gateway.router import GatewayPausedError
    from orchestrator.graph.nodes import GraphDeps

    paused = GatewayPausedError(
        {"stage": "visual_rollout", "resume_at_iso": None,
         "reason": "fixture pause"})
    for exc in (paused, CodingLimitError("coding limit")):
        run_dir = roots["tmp"] / f"runs_{type(exc).__name__}"
        deps = GraphDeps(run_dir=run_dir, persist=None)
        feishu = _FailFeishu()
        monkeypatch.setattr(patched_cli, "_production_deps",
                            lambda args: (deps, {}, feishu))
        import contextlib
        rec = _RaisingGraph(exc)
        monkeypatch.setattr(
            patched_cli, "_compiled",
            lambda d, db_path=None: contextlib.nullcontext(rec))
        with pytest.raises(type(exc)):
            patched_cli._invoke(
                _cli_args(task=2, index=None, question="q", batch_id="B"),
                False)
        assert feishu.closed and feishu.gate2_calls == 0
        assert not (run_dir / "FAILURE.json").exists()


# ---------------------------------------------------------------------------
# H-2/C1：Gate2 30s 截窗纯函数 + seam 传参；C4：ID 边界校验
# ---------------------------------------------------------------------------

def _trim_fixture(tmp_path, *, contact, n_frames, frame_dt=0.02,
                  with_manifest=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tele = tmp_path / "telemetry.json"
    active = ([False] * contact + [True] * max(1, n_frames - contact)
              if contact is not None else [False] * n_frames)
    tele.write_text(json.dumps({"dt": frame_dt,
                                "contact_active": active[:n_frames]}),
                    encoding="utf-8")
    if with_manifest:
        (tmp_path / "manifest.json").write_text(
            json.dumps({"n_frames": n_frames, "frame_dt_s": frame_dt}),
            encoding="utf-8")
    return tele


def test_40_compute_gate2_trim_task7(monkeypatch, patched_cli, roots):
    cli = patched_cli
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)  # 无 ffprobe

    # 接触 @0.2s，片长 40s：中心窗被左边界夹到 0
    tele = _trim_fixture(roots["tmp"], contact=10, n_frames=2000)
    rec = {"telemetry_uri": str(tele), "video_path": str(roots["tmp"] / "v.mp4")}
    trim = cli.compute_gate2_trim(7, {}, rec)
    assert set(trim) == {"trim_start", "trim_seconds",
                         "contact_moment_s", "source_duration_s"}
    assert trim["trim_seconds"] == 30.0
    assert trim["contact_moment_s"] == 0.2
    assert trim["source_duration_s"] == 40.0
    assert trim["trim_start"] == 0.0

    # 接触 @20s：中心 5s；右界 40-30=10 不触发
    tele2 = _trim_fixture(roots["tmp"] / "b", contact=1000, n_frames=2000)
    rec2 = {"telemetry_uri": str(tele2), "video_path": "x.mp4"}
    trim2 = cli.compute_gate2_trim(7, {}, rec2)
    assert trim2["trim_start"] == 5.0 and trim2["contact_moment_s"] == 20.0

    # 接触缺失：从头截
    tele3 = _trim_fixture(roots["tmp"] / "c", contact=None, n_frames=2000)
    trim3 = cli.compute_gate2_trim(7, {}, {"telemetry_uri": str(tele3),
                                           "video_path": "x.mp4"})
    assert trim3["contact_moment_s"] is None and trim3["trim_start"] == 0.0

    # fps 回退 telemetry.dt（无 manifest）；片长取 rec.duration_ms=40s
    tele4 = _trim_fixture(roots["tmp"] / "d", contact=25, n_frames=2000,
                          with_manifest=False)
    trim4 = cli.compute_gate2_trim(
        7, {}, {"telemetry_uri": str(tele4), "video_path": "x.mp4",
                "duration_ms": 40000})
    assert trim4["contact_moment_s"] == 0.5
    assert trim4["source_duration_s"] == 40.0 and trim4["trim_start"] == 0.0

    # 源片 ≤30s 不截窗
    tele5 = _trim_fixture(roots["tmp"] / "e", contact=10, n_frames=1000)
    assert cli.compute_gate2_trim(
        7, {}, {"telemetry_uri": str(tele5), "video_path": "x.mp4"}) is None


def test_41_compute_gate2_trim_task8_and_ffprobe_pref(
        monkeypatch, patched_cli, roots):
    cli = patched_cli
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    trim = cli.compute_gate2_trim(8, {}, {"duration_ms": 40000})
    assert trim["contact_moment_s"] is None and trim["trim_start"] == 0.0
    assert trim["source_duration_s"] == 40.0
    assert set(trim) == {"trim_start", "trim_seconds",
                         "contact_moment_s", "source_duration_s"}
    assert cli.compute_gate2_trim(8, {}, {"duration_ms": 30000}) is None
    assert cli.compute_gate2_trim(2, {}, {}) is None

    # ffprobe 可用时片长以 ffprobe 为准（42s，右界 12，接触 20s→起点 5s）
    tele = _trim_fixture(roots["tmp"] / "f", contact=1000, n_frames=2000)
    monkeypatch.setattr(cli, "_ffprobe_duration_s", lambda p: 42.0)
    trim2 = cli.compute_gate2_trim(
        7, {}, {"telemetry_uri": str(tele), "video_path": "x.mp4",
                "duration_ms": 8000})
    assert trim2["source_duration_s"] == 42.0 and trim2["trim_start"] == 5.0


def test_42_gate2_seam_passes_trim_kwarg(
        roots, patched_cli, monkeypatch):
    from orchestrator.feishu_gateway import client as feishu_client
    cli = patched_cli
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    # 40s 片（manifest 推算）→ trim dict
    run_dir = roots["tmp"] / "run_long"
    run_dir.mkdir(parents=True)
    tele = _trim_fixture(run_dir, contact=1000, n_frames=2000)
    fake = _CapturingFeishu()
    monkeypatch.setattr(feishu_client, "gateway_from_env", lambda **kw: fake)
    deps, _, _ = cli._production_deps(_cli_args())
    deps.gate2({"chosen": {"sim_engine": "mujoco"}, "metrics": {},
                "agent_runs": [{"telemetry_uri": str(tele),
                                "video_path": "x.mp4"}]})
    trim = fake.gate2[-1][1]["trim"]
    assert trim["trim_seconds"] == 30.0 and trim["trim_start"] == 5.0

    # 3s 短片 → trim=None
    run2 = roots["tmp"] / "run_short"
    run2.mkdir(parents=True)
    tele2 = run2 / "telemetry.json"
    tele2.write_text("{}", encoding="utf-8")
    (run2 / "manifest.json").write_text(json.dumps({"n_frames": 187}),
                                        encoding="utf-8")
    fake2 = _CapturingFeishu()
    monkeypatch.setattr(feishu_client, "gateway_from_env", lambda **kw: fake2)
    deps2, _, _ = cli._production_deps(_cli_args())
    deps2.gate2({"chosen": {"sim_engine": "mujoco"}, "metrics": {},
                 "agent_runs": [{"telemetry_uri": str(tele2),
                                 "duration_ms": 3000, "video_path": None}]})
    assert fake2.gate2[-1][1]["trim"] is None


def test_43_cli_invalid_ids_exit_2(patched_cli):
    cli = patched_cli
    base = ["run", "--track", "track1", "--index", "1"]
    # 非法 thread（含空格/!）在边界拒绝，退出 2，不进装配
    rc = cli.main(base + ["--thread", "bad id!", "--batch-id", "B"])
    assert rc == 2
    rc = cli.main(base + ["--thread", "tt", "--batch-id", "bad/batch"])
    assert rc == 2
    # coding/visual 批次同样先验
    rc = cli.main(base + ["--thread", "tt", "--batch-id", "B",
                          "--coding-batch-id", "bad coding",
                          "--visual-batch-id", "V"])
    assert rc == 2
